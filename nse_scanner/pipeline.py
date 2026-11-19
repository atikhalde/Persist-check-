"""One scan cycle: market lock → sector stocks → Telegram on new entries."""

from __future__ import annotations

import logging
import os
import time
from types import SimpleNamespace
from datetime import datetime, time as clock_time, timedelta
from math import isfinite

import pandas as pd

from . import data, state, telegram
from .market import (lapsed_stickiness, pick_sticky_log, previous_pick,
                     record_pick, run_market, SECTOR_ORDER)
from .oi_data import CSV_OIReader, StubOIReader
from .oi_filter import should_take_entry
from .session import IST, now_ist, session_date, to_ist
from .stock import StockSignal, scan_stock
from . import trade
from .trade import TradePlan, build_plan, ema9_frame, trail_broken

log = logging.getLogger("nse_scanner")


def _dry() -> bool:
    return os.environ.get("DRY_RUN", "0") == "1"


def _conf_delta() -> int:
    """Send a market block when confidence moves this many points (``MARKET_CONF_DELTA``, 10).

    16 Nov: 99% STRONG BULL at 09:45 slid to 94% by the 10:20 entry and to 67% at
    10:25 — with no message in between, because a block only went out when the
    label or the pick changed. Three entries were taken during that slide.
    """
    try:
        return max(0, int(os.environ.get("MARKET_CONF_DELTA", "10")))
    except ValueError:
        return 10


def max_entries_per_pick() -> int:
    """Entries allowed per pick (sector + side) per session, 0 = no cap (**0**).

    It used to be 3, because 16 Nov put six PHA names into the book on one candle
    in universe order and the cluster was out within 45 minutes for -1.6R. A
    ceiling is the wrong instrument for that: it keeps whichever three names
    happen to arrive first and throws away the fourth runner. The quality gate
    below is what keeps the sideways and reversing names out, so the count is
    left open. A positive value restores the ceiling.
    """
    try:
        value = int(os.environ.get("MAX_ENTRIES_PER_PICK", "0"))
    except ValueError:
        return 0
    return value if value >= 0 else 0


def entry_min_rank() -> float:
    """The absolute floor of the combined rank to be entered (``ENTRY_MIN_RANK``, 40)."""
    try:
        return float(os.environ.get("ENTRY_MIN_RANK", "40"))
    except ValueError:
        return 40.0


def entry_rank_band() -> float:
    """How close to its sector's best a name must rank (``ENTRY_RANK_BAND``, 0.75).

    The "real running stocks" rule, in place of a count: on each candle, within
    each sector, the names that fired are ranked and only those within this
    fraction of the sector's best are entered. 0.75 keeps the runners and drops
    the laggards of the same move; ``0`` enters every name that clears the
    absolute floor, ``1`` restores "best only".
    """
    try:
        return max(0.0, min(1.0, float(os.environ.get("ENTRY_RANK_BAND", "0.75"))))
    except ValueError:
        return 0.75


def momentum_verdict(df5: pd.DataFrame, last, close: float | None, long_side: bool,
                     sector_intra: float | None) -> tuple[bool, str, float]:
    """Is this name *running*, or has it reversed? ``(ok, why-not, rank)``.

    Two absolute tests, both about the name's own tape — the relative half (the
    sector's runners against its laggards) needs the whole sector and is applied
    by the caller with :func:`entry_rank_band`.

    * **reversing** — the close is on the wrong side of its EMA9 by more than a
      tenth of a percent, with the last three candles mostly against: a name
      rolling over is not entered, however good its day has been.
    * **weak momentum** — the combined rank under ``ENTRY_MIN_RANK`` (40): no
      strength, no volume and poor structure is a name that is not moving.

    No candle to judge (a feed that has not published) is not this gate's
    business: it says ok and the caller's own checks stand.
    """
    score, parts = name_rank(df5, last, close, long_side, sector_intra)
    if not parts:
        return True, "", score
    side = 1.0 if long_side else -1.0
    try:
        ema9 = trade.ema9_on_bar(df5, last)
    except Exception:
        ema9 = None
    if close is not None and ema9 is not None and float(close) > 0:
        off = (float(close) - ema9) * side / float(close)
        if off < -0.001 and parts.get("structure", 100) < 50:
            return False, ("reversing — under its EMA9 with the last candles against it"
                           if long_side else
                           "reversing — over its EMA9 with the last candles against it"), score
    if score < entry_min_rank():
        return False, f"weak momentum rank {score:.0f}", score
    return True, "", score


def rank_band_drop(candidates: list[dict]) -> list[dict]:
    """Drop the laggards of each sector's own move: keep the runners.

    Per sector and candle, the best combined rank sets the bar; a name below
    ``ENTRY_RANK_BAND`` (0.75) of it is the same move at a worse price — the
    "sideways or low quality" name that a count of three used to let through.
    """
    band = entry_rank_band()
    best: dict[str, float] = {}
    for cand in candidates:
        sector = cand.get("sector") or ""
        if cand.get("rank") is not None:
            best[sector] = max(best.get(sector, 0.0), float(cand["rank"]))
    if not best or band <= 0:
        return []
    keep, dropped = [], []
    for cand in candidates:
        sector = cand.get("sector") or ""
        rank = cand.get("rank")
        top = best.get(sector)
        if rank is None or top is None or float(rank) >= band * top:
            keep.append(cand)
        else:
            dropped.append(cand)
    candidates[:] = keep
    return dropped


def scan5m_leader_catchup() -> bool:
    """Report a stock whose signal fired before its sector was picked (1).

    18 Nov: CDUR was the pick from 09:45 with PERSIST − 93→99 and a qualified
    live flow, and no CDUR name was alerted all morning — because the names had
    fired their crossovers at 09:20–09:40, while CDUR was not yet the pick. The
    5m entry loop only accepts a fire on the decision candle, so every one was
    filed as ``fired before their sector was picked`` and the only trace was a
    line in the 15:33 summary. Bot 3 has had this alert since the 1m round; bot 1
    did not. ``0`` restores the silence.
    """
    return os.environ.get("SCAN5M_LEADER_CATCHUP", "1").strip() != "0"


def late_max_minutes() -> float:
    """How old a pre-pick fire may be and still be worth a catch-up (30 min)."""
    try:
        return max(5.0, min(120.0, float(os.environ.get("SCAN5M_LEADER_MAX_MINUTES", "40"))))
    except ValueError:
        return 30.0


def late_max_r() -> float:
    """Skip a catch-up once the move from the fire already exceeds this (2.0R)."""
    try:
        return max(0.5, float(os.environ.get("SCAN5M_LEADER_MAX_R", "2.0")))
    except ValueError:
        return 2.0


_LATE_WINDOW_MINUTES = 10


def _late_due(st: dict, md, bar) -> bool:
    """Evaluate catch-ups around a pick change, not on every candle.

    Bot 3 has always windowed this (``catchup_due``); bot 1 checked a pre-pick
    fire on **every** candle of a pick. On 18 Nov, CDUR was the pick for 51
    candles: 20 names × 48 candles = 963 name-checks reported as "older than the
    window" against bot 3's 62, a number that means nothing to a reader and hides
    the one thing it should say. Same rule, same ten minutes, its own ledger key.
    """
    if not scan5m_leader_catchup():
        return False
    key = f"{getattr(md, 'pick_sector', None)}|{getattr(md, 'pick_side', None)}"
    prev = st.get("late_pick") or {}
    if prev.get("key") != key:
        st["late_pick"] = {"key": key, "since": bar.isoformat()}
        return True
    try:
        since = pd.Timestamp(prev.get("since") or bar.isoformat())
    except (TypeError, ValueError):
        st["late_pick"] = {"key": key, "since": bar.isoformat()}
        return True
    if since.tzinfo is None and bar.tzinfo is not None:
        since = since.tz_localize(bar.tzinfo)
    return (bar - since) <= timedelta(minutes=_LATE_WINDOW_MINUTES)


def scan_require_target() -> bool:
    """Refuse a 5m entry with no pivot target at all (1).

    17 Nov: HINDUNILVR was entered ``T2 N/A · T3 N/A`` (−0.6R) and DABUR
    ``T1 N/A · T2 N/A · T3 N/A`` (−0.8R) — two of the day's ten entries had no
    exit level at all. ``MIN_TARGET_R`` refuses a target that is too close; this
    refuses the absence of one, which is bot 3's rule (``SCAN1M_REQUIRE_TARGET``).
    ``0`` restores the old rule.
    """
    return os.environ.get("SCAN5M_REQUIRE_TARGET", "1").strip() != "0"


def _plan_has_target(plan) -> bool:
    return any(getattr(plan, key, None) is not None for key in ("t1", "t2", "t3"))


def name_rank_mode() -> str:
    """How the names inside a scanned sector are ranked (``NAME_RANK``).

    ``combined`` (default) = strength, volume and structure together;
    ``lead`` = the day's lead alone, as before.
    """
    mode = os.environ.get("NAME_RANK", "combined").strip().lower()
    return mode if mode in ("combined", "lead") else "combined"


#: Weights of the three legs of the combined rank. Strength is what the trade is
#: about, volume is what says the move is real, structure is what says it is not
#: exhausted. They sum to 1.
RANK_WEIGHTS = {"strength": 0.5, "volume": 0.25, "structure": 0.25}


def _clamp01(x: float) -> float:
    return 0.0 if x != x else max(0.0, min(1.0, x))


def name_rank(df5: pd.DataFrame, last, close: float | None, long_side: bool,
              sector_intra: float | None) -> tuple[float, dict]:
    """0-100 rank of one name inside its sector: strength, volume, structure.

    "Not day-% alone" — the lead alone is a *distance travelled*, so it ranks the
    name that already ran hardest first and cannot tell a move with participation
    from a thin drift. The three legs:

    * **strength** — the lead vs the sector (pp), the number the entry cap already
      uses: 0.5 at parity, 1.0 at +2pp ahead (or 1.0 at 2pp behind, on a short).
    * **volume** — the decision candle's volume against the session's median 5m
      candle (``apply_1m_volume`` has already spread the 1m prints): 2x the median
      is full marks. No usable volume scores a neutral 0.5, not a zero.
    * **structure** — the mean of three readings in the trade's direction: where
      the close sits in the day's range, whether it is the right side of EMA9, and
      how many of the last three candles closed the trade's way.

    Returns ``(score, {"strength": %, "volume": %, "structure": %, "lead": pp})``.
    """
    if df5 is None or df5.empty or close is None:
        return 0.0, {}
    side = 1.0 if long_side else -1.0
    lead = sector_lead(df5, last, close, sector_intra, long_side)
    strength = _clamp01(0.5 + (float(lead) if lead is not None else 0.0) / 4.0)

    try:
        sid = session_date(last)
        day = df5.loc[[session_date(ts) == sid for ts in df5.index]]
    except Exception:                     # no calendar → judge the whole frame
        day = df5
    vol = 0.5
    try:
        vols = [float(v) for v in day["Volume"].tolist() if float(v) > 0]
        if len(vols) >= 3 and float(day["Volume"].iloc[-1]) > 0:
            med = sorted(vols)[len(vols) // 2]
            if med > 0:
                vol = _clamp01((float(day["Volume"].iloc[-1]) / med) / 2.0)
    except Exception:
        vol = 0.5

    try:
        hi, lo = float(day["High"].max()), float(day["Low"].min())
        pos = (float(close) - lo) / (hi - lo) if hi > lo else 0.5
        ema9 = trade.ema9_on_bar(df5, last)
        if ema9 is None:
            above = 0.5
        elif abs(float(close) - ema9) / float(close) <= 0.0005:   # sitting on it
            above = 0.5
        else:
            above = 1.0 if (float(close) - ema9) * side > 0 else 0.0
        tail = day.tail(3)
        # A doji is neither with the trade nor against it, so it cannot by itself
        # mark a name as reversed (a flat tape would then look like a reversal).
        way = [((float(c) - float(o)) * side) for o, c in zip(tail["Open"], tail["Close"])]
        bars_way = (sum(1 for d in way if d > 0) + 0.5 * sum(1 for d in way if d == 0)) \
            / max(1, len(way))
        structure = (pos if long_side else 1.0 - pos) * 0.5 + above * 0.25 + bars_way * 0.25
    except Exception:
        structure = 0.5

    score = 100.0 * (RANK_WEIGHTS["strength"] * strength + RANK_WEIGHTS["volume"] * vol
                     + RANK_WEIGHTS["structure"] * structure)
    return round(score, 1), {"strength": round(strength * 100), "volume": round(vol * 100),
                             "structure": round(structure * 100),
                             "lead": None if lead is None else round(float(lead), 1)}


def rank_by_sector_lead() -> bool:
    """Rank the names that clear the cap by how far they lead their sector (1).

    ``0`` restores ranking by the absolute 0-100 ``score``.
    """
    return os.environ.get("RANK_BY_SECTOR_LEAD", "1").strip() != "0"


def sector_top_n() -> int:
    """How many names of each picked sector are scanned (``SECTOR_TOP_N``, 5).

    A sector has two or three names that lead it and a tail that follows it: on
    19 Nov the AUTO book fired BHARATFORG, BOSCHLTD, EICHERMOT, M&M, MARUTI and
    MOTHERSON off the sector's own move, and the entry cap then had to throw the
    followers back out after the fact. ``0`` scans every name, as before.
    """
    try:
        return max(0, int(os.environ.get("SECTOR_TOP_N", "5")))
    except ValueError:
        return 5


def _session_open(df5: pd.DataFrame, last) -> float | None:
    """First traded price of the decision session in a stock's 5m frame."""
    if df5 is None or df5.empty:
        return None
    try:
        sid = session_date(last)
        for i, ts in enumerate(df5.index):
            if session_date(ts) == sid:
                return float(df5["Open"].iloc[i])
    except Exception:
        return None
    return None


def sector_lead(df5: pd.DataFrame, last, close: float | None,
                sector_intra: float | None, long_side: bool) -> float | None:
    """Stock move minus its sector index's move since each session open, in pp.

    The number the entry cap ranks on: positive always means "ahead of its
    sector in the direction of the trade". ``None`` when either side of the
    subtraction is missing — never a guess.
    """
    if close is None or sector_intra is None:
        return None
    open_px = _session_open(df5, last)
    if not open_px:
        return None
    try:
        stock_intra = (float(close) - float(open_px)) / float(open_px) * 100.0
        return (stock_intra - float(sector_intra)) * (1.0 if long_side else -1.0)
    except (TypeError, ValueError):
        return None


def _lead_vs_sector(df5: pd.DataFrame, last, close: float, md, sector: str | None,
                    long_side: bool) -> float | None:
    """``sector_lead`` with the sector's intraday % looked up on the decision."""
    snap = (getattr(md, "sectors", None) or {}).get(sector or "") or {}
    intra = snap.get("intra_ret") if snap else None
    return sector_lead(df5, last, close, intra, long_side)


def rank_candidates(candidates: list[dict]) -> None:
    """Order the names that clear the cap: most ahead of its sector first.

    In place, so both loops share one rule. ``lead`` of ``None`` (no sector
    snapshot, no session open) ranks as 0.0 — neutral, not "worst".
    """
    def key(c):
        score = -float(getattr(c.get("best"), "score", 0) or 0)
        if not rank_by_sector_lead():
            return (0.0, score)
        if c.get("rank") is not None:          # the trim's combined number
            return (-float(c["rank"]), score)
        lead = c.get("lead")
        return (-(lead if lead is not None else 0.0), score)
    candidates.sort(key=key)


def pick_cap_key(sector: str, side: int) -> str:
    """One sector on one side is one risk factor."""
    return f"{sector} {'long' if side == 1 else 'short'}"


def entry_cap_room(st: dict, key: str) -> bool:
    """Is another entry allowed on this pick this session?"""
    cap = max_entries_per_pick()
    if not cap:
        return True
    return int((st.get("pick_entries") or {}).get(key) or 0) < cap


def take_entry_slot(st: dict, key: str) -> None:
    """Count an entry against its pick."""
    book = st.setdefault("pick_entries", {})
    book[key] = int(book.get(key) or 0) + 1


def new_position(side: str, plan: TradePlan, bar_iso: str, session: str,
                 sector: str = "") -> dict:
    """The open-trade record — one builder, so the two loops cannot drift.

    Bot 3 used to hand-copy this dict and silently lost ``t_names``, ``sl0`` and
    ``risk``. Its TARGET HIT lines therefore read ``T1 101.00`` where bot 1's
    read ``T1 R1 101.00`` for the same trade, and it had no original stop on
    record once the stop had moved to breakeven.
    """
    return {
        "side": side, "entry": plan.entry, "sl": plan.sl, "sl0": plan.sl,
        "risk": plan.risk, "t_names": list(plan.pivot_names),
        "entry_ema9": plan.ema9, "t1": plan.t1, "t2": plan.t2, "t3": plan.t3,
        "entry_bar": bar_iso, "last_bar": bar_iso, "session": session,
        # Which book the trade belongs to: the end-of-day report scores entries
        # per sector, and at the close the state's ``scan`` names a later pick.
        "sector": sector,
    }


def _intra_of(snap: dict | None) -> float | None:
    """A sector snapshot's move from its own open, in % — ``None`` if unknown."""
    try:
        intra = (snap or {}).get("intra_ret")
        return round(float(intra), 3) if intra is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def format_market(md, prev_conf: int | None = None, *, sent=None) -> str:
    from .market import mtf_text
    side = "BULL" if md.pick_side == 1 else "BEAR" if md.pick_side == -1 else "FLAT"
    plus = " · ".join(f"{n} {s:.0f}" for n, s in md.persist_plus)
    minus = " · ".join(f"{n} {s:.0f}" for n, s in md.persist_minus)
    vix_arrow = "↑" if md.vix_chg > 0 else "↓" if md.vix_chg < 0 else "→"
    # The flow score has a side. 16 Nov printed "REALTY Q (85)" while REALTY was
    # the top of PERSIST − : in a BULL market that reads as a bullish flow and is
    # the opposite of what the number means.
    flow_side = getattr(md, "live_flow_side", 0)
    flow_arrow = "▲" if flow_side == 1 else "▼" if flow_side == -1 else "·"
    stale = getattr(md, "stale_sectors", None) or []
    stale_line = ("⚠ stale index  "
                  + " · ".join(f"{n} {b} bars behind" for n, b in stale) + "\n") if stale else ""
    # Lag inside the limit is legal and used, but it moves every PERSIST score
    # with it. Two blocks of the same candle then disagree with nothing on screen
    # to explain it (18 Nov: MET 78 vs MET 85, both stamped 09:45), and the block
    # does not say when it was computed either — the stamp is the candle's open.
    lag = {n: b for n, b in (getattr(md, "sector_lag", None) or {}).items()
           if b > 0 and n not in {x for x, _ in stale}}
    lag_line = ("index lag  "
                + " · ".join(f"{n} {b}" for n, b in sorted(lag.items(),
                                                            key=lambda kv: (-kv[1], kv[0])))
                + "\n") if lag else ""
    moved = ""
    if prev_conf is not None and abs(int(md.dir_conf) - int(prev_conf)) >= _conf_delta() > 0:
        moved = f"  <i>(was {int(prev_conf)}%)</i>"
    # A block that trades nothing must not open by announcing a direction. On
    # 16 Nov 11:55 it read "MARKET BULL conf 41% WEAK" over "WAIT — market
    # TRANSITION conf 41": a bullish call the engine then refused to act on, on a
    # tape that was 5▲/10▼. The lean is still shown — named as a lean.
    if side == "FLAT":
        lean = "" if md.dir_label == "NEUTRAL" else f"{md.dir_label} lean · "
        head = f"<b>MARKET STAND ASIDE</b>  {lean}conf {md.dir_conf}% {md.dir_tier}{moved}"
    else:
        head = f"<b>MARKET {md.dir_label}</b>  conf {md.dir_conf}% {md.dir_tier}{moved}"
        # A pick that goes against the lock must say so: "MARKET BULL" over
        # "SECTOR PICK: REALTY BEAR" is only readable if the divergence is named.
        commit = getattr(md, "commit_side", getattr(md, "dir_state", 0))
        if commit in (1, -1) and md.pick_side != commit:
            head += ("  ·  short on divergence" if md.pick_side == -1 else "  ·  long on divergence")
    # A qualified live flow on the other side is a real disagreement, and the
    # block used to print it three lines below the pick as if it were decoration:
    # 17 Nov 12:50 picked BANK long with `Live flow DEF▼ Q (72)` on the screen.
    if md.pick_side and getattr(md, "live_flow_qualified", False) \
            and getattr(md, "live_flow_side", 0) == -md.pick_side:
        head += f"  ·  live flow against ({md.live_flow})"
    # The day's persistent movers on each side, even when nothing is traded:
    # that is the question the block was silent on — "which sectors are moving?"
    watch = ""
    if side == "FLAT":
        movers = [f"{n} {word} {sc:.0f}"
                  for (pool, word) in ((md.persist_plus, "long"), (md.persist_minus, "short"))
                  for n, sc in list(pool or [])[:1]]
        if movers:
            watch = "No pick · persistent movers  " + " · ".join(movers) + "\n"
    return (
        f"{head}\n"
        f"Nifty {md.nifty:,.1f}  day {md.nifty_day_ret:+.2f}%  intra {md.nifty_intra:+.2f}%\n"
        f"VIX {md.vix:.2f} {vix_arrow} {md.vix_chg:+.2f}%   ADX {md.adx:.0f}   "
        f"{mtf_text(md.mtf_bull)}\n"
        f"Breadth {md.breadth_adv}▲/{md.breadth_dec}▼  {md.regime}\n"
        f"PERSIST +  {plus}\n"
        f"PERSIST −  {minus}\n"
        f"{stale_line}"
        f"{lag_line}"
        f"{watch}"
        f"Live flow  {md.live_flow}{flow_arrow} {'Q' if md.live_flow_qualified else 'uQ'} "
        f"({md.live_flow_score:.0f})\n"
        f"\n<b>SECTOR PICK: {md.pick_sector}</b>  {side}\n"
        f"{telegram.esc(md.pick_reason or '')}\n"
        f"Action: <b>{md.action}</b>\n"
        f"<i>{md.bar_time} IST"
        + (f" · calculated {sent:%H:%M:%S}" if sent is not None else "") + "</i>"
    )


# A pivot further than this many R — or this % of price — is not a 5m day-trade
# target. It is shown (never hidden) with a warning marker so it cannot be
# mistaken for T1. Both tests are needed: R alone is unstable because it depends
# on where the stop sits, while % of price is stop-independent.
FAR_TARGET_R = 4.0
FAR_TARGET_PCT = 2.0


def _distance(plan: TradePlan, value: float) -> tuple[float | None, float]:
    delta = value - plan.entry
    r = abs(delta) / plan.risk if plan.risk else None
    return r, delta / plan.entry * 100.0


def _target_text(plan: TradePlan) -> str:
    parts = []
    for i, (name, value) in enumerate(zip(plan.pivot_names, (plan.t1, plan.t2, plan.t3)), 1):
        if value is None:
            parts.append(f"T{i} N/A")
            continue
        r, pct = _distance(plan, value)
        bits = ([f"{r:.1f}R"] if r is not None else []) + [f"{pct:+.2f}%"]
        far = (r is not None and r > FAR_TARGET_R) or abs(pct) > FAR_TARGET_PCT
        parts.append(f"T{i} {name + ' ' if name else ''}{value:.2f} ({', '.join(bits)}{' ⚠' if far else ''})")
    text = " · ".join(parts)
    if getattr(plan, "skipped", ()):
        # Never a bare "<": the message is parsed as HTML, so "(< 0.5R)" made
        # Telegram answer "can't parse entities: Unsupported start tag" and every
        # alert carrying a skipped pivot fell back to plain text (19 Nov: HCLTECH,
        # MPHASIS, PSB, BAJAJ-AUTO all took the retry). "under" says the same.
        text += (" · skipped as too close to entry: "
                 + ", ".join(plan.skipped) + f" (under {trade.min_target_r():.1f}R)")
    return text


def _basis_text(plan: TradePlan) -> str:
    """Disclose the session the pivots were built from, so they can be audited."""
    b = plan.basis
    if b is None:
        return "Pivot basis N/A (no completed previous session)\n"
    line = (f"Pivot basis {b.day:%d %b}  H {b.high:.2f}  L {b.low:.2f}  C {b.close:.2f}"
            f"  range {b.range_pct:.2f}%")
    mult = b.range_multiple
    if mult:
        line += f" ({mult:.1f}× median)"
    if b.special:
        line += " ⚠ special session"
    if b.wide:
        line += " ⚠ wide basis — distant levels are reference only"
    elif b.compressed:
        line += " ⚠ compressed basis — levels inside a few ticks"
    return line + "\n"


def _follow_tag(md, sector: str | None = None) -> str:
    """Marks alerts taken on a leading sector while Nifty itself is NEUTRAL."""
    if not str(getattr(md, "pick_reason", "")).startswith("SECTOR FOLLOW"):
        return ""
    notes = ["sector follow"]
    if (sector or md.pick_sector) in (getattr(md, "flow_aligned", None) or ()):
        notes.append("flow aligned")
    return f" ({', '.join(notes)})"


def _scan_list(uni: dict, sector: str, extra=()) -> dict[str, str]:
    """``{stock: sector}`` for the pick plus any extra follow sectors, pick first."""
    out: dict[str, str] = {}
    for name in [sector, *(extra or ())]:
        for raw in (uni["sectors"].get(name) or {}).get("stocks", []):
            out.setdefault(raw, name)
    return out


def _scan_sides(uni: dict, sector: str, side: int, extra=(), alt: str = "",
                alt_side: int = 0) -> tuple[dict[str, str], dict[str, int]]:
    """``({stock: sector}, {stock: side})`` — the close scan, dual pick included.

    The dual pick is a *second* sector on the other side of the market, so the
    side cannot be one number for the whole scan any more. A name that both books
    hold keeps the pick's side: one name, one entry, one risk.
    """
    sector_of: dict[str, str] = {}
    side_of: dict[str, int] = {}
    for name in [sector, *(extra or ())]:
        for raw in (uni["sectors"].get(name) or {}).get("stocks", []):
            if raw not in sector_of:
                sector_of[raw] = name
                side_of[raw] = side
    if alt and alt_side in (1, -1):
        for raw in (uni["sectors"].get(alt) or {}).get("stocks", []):
            if raw not in sector_of:
                sector_of[raw] = alt
                side_of[raw] = alt_side
    return sector_of, side_of


def _sector_leaders(stocks: list[str], tickers: list[str], sector_of: dict[str, str],
                    side_of: dict[str, int], sm5: dict, current: list[str], md,
                    last) -> set[str]:
    """``sector_leaders`` with each sector's intraday % looked up on the decision."""
    intra_of = {sector: ((getattr(md, "sectors", None) or {}).get(sector) or {}).get("intra_ret")
                for sector in set(sector_of.values())}
    return sector_leaders(stocks, tickers, sector_of, side_of, sm5, current,
                          intra_of, last)


def sector_leaders(stocks: list[str], tickers: list[str], sector_of: dict[str, str],
                   side_of: dict[str, int], sm5: dict, current: list[str],
                   intra_of: dict, last) -> set[str]:
    """The names of each scanned sector worth judging: its top ``SECTOR_TOP_N``.

    Ranked by ``sector_lead`` — the stock's move from its own open minus its
    sector index's, signed by the side of the trade — which is the number the
    entry cap already ranks on, so the two rules agree on who leads. A name with
    no candle this bar cannot be ranked and sinks to the end of its sector.

    ``intra_of`` is ``{sector: move from its own open, %}``, so the 1m scan —
    which holds the pick rather than the whole decision — trims on the same
    numbers as the 5m scan.
    """
    n = sector_top_n()
    if not n:
        return set(stocks)
    combined = name_rank_mode() == "combined"
    current_set = set(current)
    groups: dict[str, list[tuple[float, str, dict]]] = {}
    for raw, sym in zip(stocks, tickers):
        score, parts = float("-inf"), {}
        if sym in current_set:
            df5 = sm5.get(sym)
            if df5 is not None and not df5.empty:
                close = float(df5["Close"].iloc[-1])
                intra = (intra_of or {}).get(sector_of[raw])
                long_side = side_of.get(raw, 1) == 1
                if combined:
                    score, parts = name_rank(df5, last, close, long_side, intra)
                    if score <= 0:
                        score = float("-inf")
                else:
                    lead = sector_lead(df5, last, close, intra, long_side)
                    score = lead if lead is not None else float("-inf")
                    parts = {"lead": lead}
        groups.setdefault(sector_of[raw], []).append((score, raw, parts))
    keep: set[str] = set()
    for sector, items in groups.items():
        items.sort(key=lambda t: (-t[0], t[1]))
        keep.update(raw for _, raw, _ in items[:n])
        if len(items) > n:
            def _fmt(item) -> str:
                score, raw, parts = item
                if score == float("-inf"):
                    return f"{raw} (no candle)"
                if not combined:
                    return f"{raw} {parts.get('lead') or 0.0:+.1f}pp"
                return (f"{raw} {score:.0f} (strength {parts.get('strength')} · "
                        f"volume {parts.get('volume')} · structure {parts.get('structure')})")
            log.info("%s: top %d of %d names by %s — %s", sector, n, len(items),
                     "combined rank" if combined else "lead",
                     ", ".join(_fmt(item) for item in items[:n]))
    return keep


# ── Futures open interest (optional) ─────────────────────────────────────────
def _oi_mode() -> str:
    """``off`` | ``annotate`` (default: add an OI line when data exists) | ``require``."""
    mode = os.environ.get("OI_FILTER", "annotate").strip().lower()
    return mode if mode in ("off", "annotate", "require") else "annotate"


def _oi_reader(session: str):
    """Reader for today's OI rows, or None when there is nothing to read.

    Yahoo has no open interest, so this only works when something fills
    ``OI_FILE`` (``symbol,timestamp,oi,close``; timestamp = the 5m bar's start in
    IST, the same label the alerts use). With no rows for today the filter is
    simply unavailable: it must never silence every alert because a data file is
    empty. ``OI_SOURCE=stub`` returns made-up numbers and is for tests only.
    """
    if _oi_mode() == "off":
        return None
    if os.environ.get("OI_SOURCE", "csv").strip().lower() == "stub":
        return StubOIReader()
    reader = CSV_OIReader(os.environ.get("OI_FILE", "data/oi_history.csv"))
    try:
        reader._load()
    except Exception as e:                      # a broken file must not stop the scanner
        log.warning("OI file unreadable (%s); OI filter unavailable", e)
        return None
    if not any(stamp.startswith(session) for _, stamp in reader._index):
        return None
    return reader


def _oi_line(reading) -> str:
    if reading is None or reading.oi_chg_pct is None or reading.price_chg_pct is None:
        return ""
    label = {"LONG_BUILDUP": "Long Build-up", "SHORT_BUILDUP": "Short Build-up",
             "SHORT_COVERING": "Short covering", "LONG_UNWINDING": "Long unwinding"}.get(
                 reading.regime.value, "Neutral")
    return (f"OI <b>{label}</b> · OI {reading.oi_chg_pct:+.2f}% · price "
            f"{reading.price_chg_pct:+.2f}% (5m)\n")


def format_stock(sig: StockSignal, md, plan: TradePlan, *, sector: str | None = None,
                 oi_line: str = "", lead: float | None = None,
                 dual: str = "") -> str:
    arrow = "🟢" if sig.side == "BUY" else "🔴"
    close_time = (datetime.strptime(sig.time, "%H:%M") + timedelta(minutes=5)).strftime("%H:%M")
    below = "below" if sig.side == "BUY" else "above"
    targets = _target_text(plan)
    # Stock move minus sector move, in percentage points, since each session open.
    lead_text = f"  ·  lead {lead:+.1f}pp vs sector" if lead is not None else ""
    return (
        f"{arrow} <b>{sig.side}</b> | <b>{sig.name}</b>  {sig.symbol}\n"
        f"Sector <b>{sector or md.pick_sector}</b>{_follow_tag(md, sector)}  |  Market {md.dir_label} ({md.dir_conf}%){lead_text}\n"
        + (f"<i>{telegram.esc(dual)}</i>\n" if dual else "")
        + f"{oi_line}"
        f"Entry (candle close) <b>{plan.entry:.2f}</b>\n"
        f"SL {plan.sl:.2f} (0.02% {below} EMA9)\n"
        f"Risk {plan.risk:.2f}  |  next pivot target: {targets}\n"
        f"{_basis_text(plan)}"
        f"Score {sig.score} {sig.strength}   {telegram.esc(sig.reason)}\n"
        f"5m bar {sig.time}–{close_time} IST (signal confirmed at close)\n"
        f"Signal calculated {now_ist().strftime('%H:%M:%S')} IST{_lag_text(sig.time)}"
    )


def _lag_text(hhmm: str) -> str:
    """Seconds between the 5m close and this calculation, printed on the alert."""
    now = now_ist()
    try:
        opened = datetime.strptime(hhmm, "%H:%M")
    except ValueError:
        return ""
    close = now.replace(hour=opened.hour, minute=opened.minute, second=0,
                        microsecond=0) + timedelta(minutes=5)
    lag = (now - close).total_seconds()
    return f" (+{lag:.0f}s after close)" if 0 <= lag < 3600 else ""


# --- early (pre-close) alerts -------------------------------------------------
# A close-confirmed 5m signal cannot be known before the candle closes, and the
# breakout that creates it normally happens INSIDE that candle. Waiting for the
# close therefore hands over the move. The live loop evaluates the forming
# candle (rebuilt from 1m bars) shortly before the close and sends a clearly
# labelled, unconfirmed heads-up; the close then confirms or cancels it.

# --- session statistics -------------------------------------------------------
# What actually happened today, sent once after the close: how many candles were
# scanned, whether Yahoo's 1m feed was live enough for early alerts, and how long
# after each close the confirmed alerts went out.

SECTOR_HIST_EVERY = 3          # one persistence sample every three candles


def _note_sector_day(st: dict, md, bar: str) -> None:
    """Keep the day's persistent-sector history, for the end-of-day report.

    Every sector's persistence is computed on every candle and then thrown away.
    The report needs the shape of the day: who led at 09:30, who was still
    leading at 14:00, and what the score did in between. One sample every three
    candles (15 minutes) is enough to draw that and cheap enough to keep.
    """
    book = st.setdefault("stats", {}).setdefault("sector_hist", {})
    snaps = getattr(md, "sectors", None) or {}
    if not snaps:
        return
    picked = md.pick_sector if md.pick_side in (1, -1) else ""
    for name, snap in snaps.items():
        side = int(snap.get("side") or 0) or 1
        persist = float(snap.get(f"persist_{'bull' if side == 1 else 'bear'}") or 0.0)
        h = book.get(name)
        if h is None:
            h = book[name] = {"first": bar, "max": persist, "min": persist, "n": 0,
                              "series": [], "picks": 0}
        h["n"] = int(h.get("n") or 0) + 1
        h["last"] = bar
        h["max"] = max(float(h.get("max") or 0.0), persist)
        h["min"] = min(float(h.get("min", 999.0)), persist)
        h["persist"] = persist
        h["side"] = side
        h["day_ret"] = round(float(snap.get("day_ret") or 0.0), 2)
        h["intra"] = round(float(snap.get("intra_ret") or 0.0), 2)
        h["rs"] = round(float(snap.get("rs") or 0.0), 2)
        h["pos"] = round(float(snap.get("pos") or 0.0), 1)
        h["qualified"] = bool(snap.get("qualified"))
        if name == picked:
            h["picks"] = int(h.get("picks") or 0) + 1
        series = h.setdefault("series", [])
        if not series or (h["n"] % SECTOR_HIST_EVERY == 0):
            series.append([bar[-5:], round(persist)])


def _note_weak(st: dict, why: str) -> None:
    """Count a momentum-gate refusal by *why*, not just as "weak"."""
    key = ("reversing" if "reversing" in why else
           "sideways" if "sideways" in why else
           "behind its sector's runners" if "runners" in why else
           "weak momentum")
    book = st.setdefault("stats", {}).setdefault("weak", {})
    book[key] = int(book.get(key, 0)) + 1


def _note_trim(st: dict, sector: str, kept: int, total: int) -> None:
    """How many names of a sector the trim dropped on this candle."""
    if total <= kept:
        return
    book = st.setdefault("stats", {}).setdefault("trim", {})
    slot = book.setdefault(sector, {"kept": 0, "dropped": 0, "candles": 0})
    slot["kept"] = int(slot.get("kept") or 0) + kept
    slot["dropped"] = int(slot.get("dropped") or 0) + (total - kept)
    slot["candles"] = int(slot.get("candles") or 0) + 1


def _close_trade(st: dict, raw: str, pos: dict, price: float, why: str, hhmm: str) -> None:
    """File a finished trade with its R, so the day's report can score it.

    Positions are deleted when their exit alert goes out, which left the day's
    summary able to count entries but not to say what they did.
    """
    try:
        entry = float(pos.get("entry") or 0.0)
        risk = float(pos.get("risk") or 0.0)
        long = pos.get("side") == "BUY"
        r = ((price - entry) if long else (entry - price)) / risk if risk else 0.0
        book = st.setdefault("stats", {}).setdefault("closed", [])
        book.append({"raw": raw, "side": pos.get("side"), "time": hhmm, "why": why,
                     "entry": round(entry, 2), "exit": round(float(price), 2),
                     "r": round(r, 2),
                     "t_hit": len(pos.get("hit_targets") or ()),
                     "sector": pos.get("sector") or (st.get("scan") or {}).get("sector") or ""})
    except Exception:                      # bookkeeping must never break an alert
        log.exception("could not file the closed trade for %s", raw)


def _eod():
    """The report module, imported lazily (it reads back from this one)."""
    from . import eod

    return eod


def _bump(st: dict, key: str, n: float = 1) -> None:
    stats = st.setdefault("stats", {})
    stats[key] = stats.get(key, 0) + n


def _note_reject(st: dict, kind: str, raw: str, detail: str, *, per_bar: str | None = None) -> None:
    """Remember a stock signal that did not become an alert (for the summary).

    Keyed so that the retries of one candle, and a stale signal that shows up on
    every later candle, are each counted once.
    """
    book = st.setdefault("stats", {}).setdefault("rejected", {}).setdefault(kind, {})
    book.setdefault(f"{raw}@{per_bar}" if per_bar else raw, detail)


_REJECT_TEXT = (
    ("earlier", "fired before their sector was picked"),
    ("opposite", "on the opposite side to the pick"),
    ("no_stop", "without a valid EMA9 stop"),
    ("oi", "not confirmed by open interest"),
    ("no_data", "name-checks had no 5m bar from the feed"),
    ("scan_error", "could not be scanned"),
    ("late", "scanned too late"),
    ("cap", "not taken — the pick already had its entries"),
    ("weak", "not running — sideways, reversing or weak momentum"),
    ("follower", "followers not scanned — outside the sector's top"),
    ("no_target", "without a pivot target"),
    ("alert_cap", "held back — more than the per-candle alert cap"),
)


def _live_text(x: dict) -> str:
    """``with live 1m data N`` read as N *names*; it counts passes.

    One live name was enough for a pass to count, so on a day when every pass had
    one the line said "57" beside "Early passes 57" and looked like a restatement
    of the names rather than a different unit.
    """
    passes = int(x.get("early_passes") or 0)
    live = int(x.get("early_live") or 0)
    if passes and live == passes:
        return "all with live 1m data"
    return f"{live} of them with live 1m data"


def _summary_no_index(x: dict) -> str:
    n = len(x.get("no_index") or {})
    if not n:
        return ""
    return (f"Candles skipped: {n} — Nifty had not published its bar when the run "
            f"reached the candle")


def _summary_sectors(x: dict) -> str:
    """Candles per picked sector — the pick only, once per candle.

    A follow scan *also* scans the other leaders' sectors, and those used to be
    counted in the same book: 17 Nov's 5m block read ``PSUBK 27 · REALTY 26 ·
    BANK 5 · CDUR 3 · FMCG 2 · REALTY 2 · PSUBK 1`` against ``with a sector pick
    62`` — 66 counts for 62 candles, and bot 3 (which counted the pick alone)
    reported the same day as 62. Two blocks, two arithmetics, one label. The
    pick is counted once; what it dragged along is a separate clause.
    """
    picked = x.get("sectors") or {}
    if not picked:
        return "Sectors picked: none — the market said WAIT on every scanned candle"
    parts = [f"{name} {n}" for name, n in sorted(picked.items(), key=lambda kv: -kv[1])]
    line = "Sectors picked (candles): " + " · ".join(parts)
    extra = x.get("extra") or {}
    if extra:
        line += (" · followed: "
                 + " · ".join(f"{name} {n}" for name, n in
                              sorted(extra.items(), key=lambda kv: (-kv[1], kv[0]))))
    return line


def _summary_rejects(st: dict) -> str:
    x = st.get("stats") or {}
    rejected = x.get("rejected") or {}
    alerted = set(st.get("sent") or {})
    # A name that got a labelled LATE LEADER *was* alerted — just not as an
    # entry. 18 Nov's summary listed seven names under "Signals not alerted:
    # fired before their sector was picked" (KALYANKJIL 09:20, TITAN 09:25, …)
    # that the same report's first line counted as "LATE LEADER catch-ups 7".
    active = alerted | set(st.get("catchup") or {})
    parts = []
    for kind, text in _REJECT_TEXT:
        items = {k: v for k, v in (rejected.get(kind) or {}).items()
                 if not (kind == "earlier" and k in active)}
        if not items:
            continue
        shown = ", ".join(list(items.values())[:6]) + (" …" if len(items) > 6 else "")
        if len(items) == 1 and text.startswith("name-checks"):
            text = "name-check had" + text[len("name-checks had"):]
        parts.append(f"{len(items)} {text} ({shown})")
    if parts:
        return "Signals not alerted: " + " · ".join(parts)
    if x.get("picks") and not x.get("entries"):
        return "Signals not alerted: none — no stock in the picked sectors gave a signal"
    return "Signals not alerted: none"


def _note_feed_age(st: dict, prefix: str, frames: dict) -> None:
    """Record how old the newest 1m bar of each stock is at this moment."""
    now = pd.Timestamp(now_ist())
    ages = [(now - df.index[-1]).total_seconds() for df in frames.values()
            if df is not None and not df.empty]
    ages = [a for a in ages if 0 <= a < 3600]
    if ages:
        _bump(st, prefix + "_age_sum", sum(ages))
        _bump(st, prefix + "_age_n", len(ages))


def format_summary(st: dict) -> str | None:
    x = st.get("stats") or {}
    if not x.get("candles"):
        return None
    avg = lambda k: x.get(k + "_sum", 0) / x[k + "_n"] if x.get(k + "_n") else None
    age = lambda v: "no 1m data" if v is None else f"newest 1m bar {v:.0f}s old on average"
    lines = [f"📊 <b>Scanner summary {st.get('session', '')}</b>",
             f"Candles scanned {x['candles']} of 75 · with a sector pick {x.get('picks', 0)}",
             _summary_sectors(x),
             _summary_no_index(x),
             f"Early passes {x.get('early_passes', 0)} · {_live_text(x)}"
             f" · {age(avg('early_age'))}",
             _summary_early_skip(x),
             f"Early alerts {x.get('early_sent', 0)}: confirmed {x.get('confirmed', 0)}"
             f" · cancelled {x.get('cancelled', 0)} · unverified {x.get('unverified', 0)}",
             _summary_not_held(x),
             f"Entries {x.get('entries', 0)}"
             + (f" · {x['late_entries']} from late leaders" if x.get("late_entries") else "")
             + (f" · sent {x['lag_sum'] / x['entries']:.0f}s after the close on average,"
                f" slowest {x.get('lag_max', 0):.0f}s" if x.get("entries") else ""),
             _summary_rejects(st),
             _summary_catchup(st),
             _summary_scan_lag(x),
             _summary_pending(st),
             _summary_mirror(),
             _summary_open(st),
             f"At the confirm scan: {age(avg('close_age'))}"]
    return "\n".join(line for line in lines if line)


def _summary_catchup(st: dict) -> str:
    """The LATE LEADER line: what was sent, and what was not worth sending."""
    from .scan1m import _CATCHUP_WHY, _CATCHUP_WHY_ORDER

    x = st.get("stats") or {}
    skips = x.get("catchup_skip") or {}
    sent = x.get("catchup") or 0
    if not sent and not skips:
        return ""
    refused = " · ".join(f"{_CATCHUP_WHY.get(k, k)} {skips[k]}"
                         for k in _CATCHUP_WHY_ORDER if skips.get(k))
    youngest = x.get("catchup_age_min")
    return (f"LATE LEADER catch-ups {sent} (signal fired before its sector was picked)"
            + (f" · not sent (per name-check): {refused}" if refused else "")
            + (f" · youngest refused fire {float(youngest):.0f} min old"
               if youngest is not None and refused else ""))


def _summary_mirror() -> str:
    """The mirror bot's silence, said out loud once at the end of the day.

    18 Nov: ``TELEGRAM_CHAT_ID_2`` named a chat bot 2 is not in, so every mirrored
    alert was refused — 30-odd of them — and the summary said nothing about it.
    """
    n = telegram.secondary_failures()
    if not n:
        return ""
    return (f"⚠ mirror alerts not delivered: {n} — TELEGRAM_CHAT_ID_2 names a chat that"
            f" bot 2 is not a member of (bot 1's own alerts were delivered)")


def _summary_pending(st: dict) -> str:
    """Open trades whose candles did not arrive — what was not checked, and why.

    "were never evaluated" was printed for every name left in the book at the
    close, including names that had been checked all day: 19 Nov's summary said
    BAJAJ-AUTO's stops, targets and EMA9 exit "were never evaluated" on the same
    page as its T1 and T2 hits. A name that has been checked before is owed the
    smaller, true sentence: its last candle was not evaluated.
    """
    stats = st.get("stats") or {}
    names = list(stats.get("pending") or ())
    if not names:
        return ""
    checked = set(stats.get("checked") or ())

    def show(items):
        return ", ".join(items[:6]) + (" …" if len(items) > 6 else "")

    never = [n for n in names if n not in checked]
    at_close = [n for n in names if n in checked]
    lines = []
    if never:
        lines.append(f"⚠ not checked this session (no 5m candle): {show(never)} — their "
                     f"stops, targets and EMA9 exit were never evaluated")
    if at_close:
        lines.append(f"⚠ not checked on the last candle (no 5m candle): {show(at_close)} — "
                     f"the stop, target and EMA9 check could not run there, so the exit "
                     f"is unconfirmed; check it yourself")
    return "\n".join(lines)


def _summary_open(st: dict) -> str:
    """The trades still open when the session ended — the ones with no exit alert.

    Every other line of the summary counts what was *sent*. A position that was
    opened at 15:20 has had no TARGET HIT, no SL HIT and no EMA9 EXIT, and bot 1
    will drop it at the next session's reset (a trade is intraday). Nothing else
    in the day's messages says which trades are still live.
    """
    positions = st.get("positions") or {}
    if not positions:
        # "every entry got its exit alert" is vacuous on a day with no entry at
        # all — 18 Nov had 73 candles, 57 picks and 0 entries, and the summary
        # still closed by promising exits for trades that never existed.
        if not int((st.get("stats") or {}).get("entries") or 0):
            return "Open at the close: none — no entry was opened today"
        return "Open at the close: none — every entry got its exit alert"
    shown = []
    for raw, pos in list(positions.items())[:5]:
        entry, stop = pos.get("entry"), pos.get("sl")
        # A record without a price is still a trade to name — never "0.00".
        text = f"{raw} {float(entry):.2f}" if entry is not None else f"{raw}"
        if stop is not None:
            text += f" (stop {float(stop):.2f})"
        shown.append(text)
    if len(positions) > 5:
        shown.append(f"+{len(positions) - 5} more")
    return ("Open at the close — no exit alert yet: "
            + " · ".join(shown)
            + ". A trade is intraday: it is not trailed into the next session.")


def _early_lead() -> int:
    """Seconds before the 5m close at which the forming candle is evaluated (0 = off)."""
    try:
        return max(0, min(240, int(os.environ.get("EARLY_ALERT_SECONDS", "60"))))
    except ValueError:
        return 60


def _early_max_stale() -> float:
    return float(os.environ.get("EARLY_MAX_STALE_SECONDS", "100"))


def format_early(sig: StockSignal, scan: dict, plan: TradePlan, bar: datetime,
                 dual: str = "") -> str:
    arrow = "🟢" if sig.side == "BUY" else "🔴"
    close = bar + timedelta(minutes=5)
    now = now_ist()
    left = max(0, int((close - now).total_seconds()))
    note = f"<i>{telegram.esc(dual)}</i>\n" if dual else ""
    return (
        f"⚡ {arrow} <b>EARLY {sig.side}</b> (unconfirmed) | <b>{sig.name}</b>  {sig.symbol}\n"
        f"Sector <b>{scan.get('sector')}</b>  |  Market {scan.get('label')} ({scan.get('conf')}%)\n"
        f"{note}"
        f"Price now <b>{plan.entry:.2f}</b>  ·  5m bar {bar.strftime('%H:%M')}–{close.strftime('%H:%M')}"
        f" still forming ({left}s left)\n"
        f"SL ≈ {plan.sl:.2f} (EMA9 so far)  |  Risk ≈ {plan.risk:.2f}\n"
        f"next pivot target: {_target_text(plan)}\n"
        f"Score {sig.score} {sig.strength}   {telegram.esc(sig.reason)}\n"
        f"Holds only if the {close.strftime('%H:%M')} close keeps the signal — "
        f"✅ confirm or ❌ cancel follows\n"
        f"Calculated {now.strftime('%H:%M:%S')} IST"
    )


def _open_early(st: dict, raw: str, bar: datetime) -> dict | None:
    e = (st.get("early") or {}).get(raw)
    return e if e and not e.get("result") and e.get("bar") == bar.isoformat() else None


def _confirm_header(early: dict, entry: float) -> str:
    diff = (entry - early["price"]) if early["side"] == "BUY" else (early["price"] - entry)
    pct = diff / early["price"] * 100.0 if early["price"] else 0.0
    return (f"✅ <b>CONFIRMED</b> — early alert {early['at']} @ {early['price']:.2f} "
            f"(close is {pct:+.2f}% {'beyond' if diff >= 0 else 'back from'} it)\n")


def _note_cancel(st: dict, key: str) -> None:
    """Count cancellations by the gate that refused them.

    18 Nov: ``Early alerts 1: confirmed 0 · cancelled 1 · unverified 0`` — one
    projection died and the summary never said which gate did it, while bot 3
    printed ``Not held: …`` for the same two projections. Both bots now use one
    glossary for the gates (``scan1m._CANCEL_WHY``), so the two reports describe
    the same event with the same words; a caller that passes a clause instead of
    a code still gets bucketed on it rather than dropped.
    """
    key = key.split(" (")[0].strip() or "unstated"
    why = st.setdefault("stats", {}).setdefault("cancel_why", {})
    why[key] = why.get(key, 0) + 1


def _note_early_skip(st: dict, why: str) -> None:
    """Count an early pass that never looked at a stock, and why.

    Both of these were silent: the pass returned an empty list, so the summary's
    ``Early passes N`` read as though the scan had looked and found nothing.
    """
    skips = st.setdefault("stats", {}).setdefault("early_skip", {})
    skips[why] = skips.get(why, 0) + 1


def _note_scan_lag(st: dict, last: datetime, *, stale: bool = False) -> None:
    """How late the close scan is judging this candle, once per candle.

    18 Nov asked the catch-up line to explain ``catch-ups 0`` and could not: the
    only lateness figure the summary had was for *entry alerts that were sent*,
    and none were. A scan that runs ten minutes behind its candle refuses every
    older fire as ``old`` and drops every fresh one as ``late``, and the day's
    report showed neither number.
    """
    if stale:
        return                       # a replayed candle measures the replay, not the day
    stats = st.setdefault("stats", {})
    stamp = last.strftime("%Y-%m-%d %H:%M")
    if stats.get("scan_lag_bar") == stamp:
        return                       # the retries of one candle are one measurement
    stats["scan_lag_bar"] = stamp
    lag = (now_ist() - (last + timedelta(minutes=5))).total_seconds()
    stats["scan_lag_max"] = max(float(stats.get("scan_lag_max", 0.0)), lag)
    if lag > _max_lag():
        stats["scan_late"] = stats.get("scan_late", 0) + 1


def _summary_scan_lag(x: dict) -> str:
    late = x.get("scan_late") or 0
    if not late:
        return ""
    return (f"⏱ close scan late: {late} of {x.get('candles', 0)} candles judged more than "
            f"{_max_lag()}s after their close (slowest {x.get('scan_lag_max', 0):.0f}s)")


def _summary_early_skip(x: dict) -> str:
    skips = x.get("early_skip") or {}
    if not skips:
        return ""
    return "Early pass skipped: " + " · ".join(
        f"{k} {n}" for k, n in sorted(skips.items(), key=lambda kv: (-kv[1], kv[0])))


def _summary_not_held(x: dict) -> str:
    """``Not held: <gate> N`` — bot 3's vocabulary, in bot 3's order.

    One glossary (``scan1m._CANCEL_WHY``) and one ordering, so the same two
    projections read the same way on both bots.
    """
    why = x.get("cancel_why") or {}
    if not why:
        return ""
    from .scan1m import _CANCEL_WHY, _CANCEL_WHY_ORDER

    order = {k: i for i, k in enumerate(_CANCEL_WHY_ORDER)}
    keys = sorted(why, key=lambda k: (order.get(k, len(order)), -why[k], k))
    return "Not held: " + " · ".join(f"{_CANCEL_WHY.get(k, k)} {why[k]}" for k in keys)


def _move_clause(early: dict, close: float | None, code: str | None) -> str:
    """The close's move in the trade's favour, and what it means for a dead signal.

    Bot 3 prints this; bot 1 showed ``close 1712.10 vs early 1714.80`` and left the
    reader to work out that the projection died *while the price helped* — the
    case that looks most like a bug (LGEINDIA, 18 Nov, +0.16%).
    """
    price = early.get("price") or 0.0
    if close is None or not price:
        return ""
    pct = (close - price) / price * 100.0
    if str(early.get("side") or "").upper() == "SELL":
        pct = -pct                       # "in our favour" always means "beyond"
    text = f" ({pct:+.2f}%)"
    if pct > 0 and code == "no_signal":
        text += " — price moved in our favour, the signal did not repeat"
    return text


def _cancel_early(st: dict, raw: str, early: dict, reason: str, result: str = "cancelled",
                  close: float | None = None, code: str | None = None) -> bool:
    """Tell the user an early heads-up did not survive; once, then never again.

    ``reason`` is a clause about the *alert*, not the stock, and ``who`` puts the
    name first: the old text read "BUY DIVISLAB · BUY-EX6 is no longer in the
    scanned sector/side", as if the signal code were the thing that moved.
    """
    bar = pd.Timestamp(early["bar"])
    hhmm = (bar + timedelta(minutes=5)).strftime("%H:%M")
    head = "❌ <b>EARLY CANCELLED</b>" if result == "cancelled" else "⚠ <b>EARLY UNVERIFIED</b>"
    px = (f" · close {close:.2f} vs early {early['price']:.2f}"
          f"{_move_clause(early, close, code)}" if close is not None else "")
    tail = ("no confirmed entry — manage/exit if you already entered" if result == "cancelled"
            else "treat as unconfirmed")
    who = f"{early['side']} {raw}" + (f" ({early['name']})" if early.get("name") else "")
    text = f"{head} — {who} · {reason} · {hhmm} close{px} · {tail}"
    if not telegram.send(text, dry_run=_dry()):
        return False
    early["result"] = result
    _bump(st, result)
    if result == "cancelled":
        _note_cancel(st, code or reason)
    _save(st)
    log.info("EARLY %s %s %s: %s", result.upper(), raw, early["side"], reason)
    return True


def _settle_early(st: dict, bar: datetime, skip: set[str], reason: str,
                  result: str = "cancelled", older: bool = False,
                  code: str | None = None) -> None:
    """Resolve early alerts for ``bar`` that the close scan did not look at."""
    for raw, early in list((st.get("early") or {}).items()):
        if early.get("result") or raw in skip:
            continue
        ebar = pd.Timestamp(early["bar"])
        if ebar == bar or (older and ebar < bar):
            _cancel_early(st, raw, early, reason, result, code=code)


def run_early(bar: datetime) -> list[dict]:
    """Heads-up alerts from the STILL-FORMING 5m candle ``bar`` (open time).

    Uses the market/sector pick of the previous completed candle (the pick is a
    persistence measure and is re-validated at the close), rebuilds the forming
    candle from live 1m bars and runs the unchanged stock engine on it. Fires
    are sent as ``EARLY … (unconfirmed)``; nothing is written to the entry
    ledger or the position book — only the close-confirmed scan does that.

    One early alert per stock per session, so a signal that flickers on and off
    cannot spam; the regular close alert is still available afterwards.
    """
    now = now_ist()
    close = bar + timedelta(minutes=5)
    if _early_lead() <= 0 or bar.weekday() >= 5:
        return []
    st = state.reset_if_new_day(state.load(), str(now.date()))
    scan = st.get("scan") or {}
    prev = (bar - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
    side = scan.get("side")
    if not bar < now < close:
        # A pass the loop reached after the candle closed cannot project: there is
        # no forming candle left to judge. 18 Nov bot 1 projected one name to bot
        # 3's two and the summary had no way to say whether a pass had been
        # skipped, been too late, or simply found nothing — the count alone.
        if now >= close:
            _note_early_skip(st, "after the candle closed")
            _save(st)
        return []
    if scan.get("bar") != prev or side not in (1, -1) or scan.get("sector") in (None, "WAIT"):
        log.info("early %s: no current sector pick from %s — skipped", bar.strftime("%H:%M"), prev)
        _note_early_skip(st, "no pick on the previous candle")
        _save(st)
        return []
    uni = data.load_universe()
    # The early pass sees the same two books as the close: on 19 Nov the AUTO
    # long was on the board at 09:45 while the pick was IT short, and the pass
    # that should have projected it was looking at IT only.
    alt = (scan.get("alt") or {}).get("sector") or ""
    alt_side = int((scan.get("alt") or {}).get("side") or 0)
    sector_of, side_of = _scan_sides(uni, scan["sector"], side, scan.get("extra"),
                                     alt, alt_side)
    if not sector_of:
        _note_early_skip(st, "no names in the picked sector")
        _save(st)
        return []
    sent, positions = st.setdefault("sent", {}), st.setdefault("positions", {})
    book = st.setdefault("early", {})
    names = [raw for raw in sector_of
             if raw not in sent and raw not in positions and raw not in book]
    if not names:
        _note_early_skip(st, "every name already alerted or held")
        _save(st)
        return []
    # Counted here, not at the end: "Early passes N" claims the scan *looked* at
    # these names, and the two returns above are passes that never looked.
    _bump(st, "early_passes")
    tickers = {raw: data.yahoo_stock(raw) for raw in names}
    period_5m = os.environ.get("YF_PERIOD_5M", "7d")
    sm5 = data.fetch_many(tickers.values(), period_5m, "5m")
    sm1 = data.fetch_many(tickers.values(), "7d", "1m")
    sd1 = data.fetch_daily(tickers.values(), os.environ.get("YF_PERIOD_1D", "90d"))
    _note_feed_age(st, "early", sm1)
    # Only each sector's leaders: the tail is the sector moving, not the name.
    # The ledger's own numbers: the picked sector's move and the dual's, exactly
    # as the close pass will rank them on the same candle.
    intra_of = {name: (scan.get("intra") if name == scan["sector"] else scan.get("alt_intra"))
                for name in set(sector_of.values())}
    # Ranked on the last *completed* candle, so a partial row Yahoo has already
    # published for the forming one cannot make every name unrankable.
    was = bar - timedelta(minutes=5)
    done = {sym: (df.loc[df.index <= was] if df is not None and not df.empty else df)
            for sym, df in sm5.items()}
    leaders = sector_leaders(
        list(tickers), list(tickers.values()), sector_of, side_of, done,
        [sym for sym in tickers.values()
         if done.get(sym) is not None and not done[sym].empty
         and to_ist(done[sym].index[-1]) == was],
        intra_of, was)
    hhmm = bar.strftime("%H:%M")
    out: list[dict] = []
    live = 0
    for raw, ysym in tickers.items():
        if raw not in leaders:
            log.info("follower %s @ %s: outside %s's top %d; not scanned",
                     raw, hhmm, sector_of[raw], sector_top_n())
            _note_reject(st, "follower", raw, f"{raw} {sector_of[raw]}")
            continue
        want = "BUY" if side_of.get(raw, side) == 1 else "SELL"
        m1 = sm1.get(ysym)
        df5 = data.provisional_5m(sm5.get(ysym), m1, bar, now_ist(), _early_max_stale())
        if df5 is None:
            continue
        live += 1
        df5 = data.apply_1m_volume(df5, m1)
        try:
            fires = scan_stock(raw, df5, sd1.get(ysym), keep_all=True)
        except Exception as e:
            log.exception("early scan failed %s: %s", raw, e)
            _note_reject(st, "scan_error", raw, f"{raw} {bar.strftime('%H:%M')}",
                         per_bar=bar.strftime("%H:%M"))
            continue
        fresh = [s for s in fires if s.time == hhmm and s.side == want]
        if not fresh:
            continue
        best = fresh[-1]
        plan = build_plan(best, df5, bar, sd1.get(ysym))
        if plan is None:
            continue
        stamp = now_ist()
        if stamp >= close - timedelta(seconds=3):
            log.info("early scan for %s finished at the close; leaving it to the close scan", hhmm)
            break
        best.price, best.sl = plan.entry, plan.sl
        shown = {**scan, "sector": sector_of[raw] + (" (sector follow)" if scan.get("follow") else "")}
        dual = str(scan.get("alt_reason") or "") if (alt and sector_of[raw] == alt) else ""
        text = (format_early(best, shown, plan, bar, dual=dual)
                + f" · price from 1m bar {m1.index[-1]:%H:%M}")
        if not telegram.send(text, dry_run=_dry()):
            continue
        book[raw] = {"bar": bar.isoformat(), "side": best.side, "code": best.code,
                     "name": best.name, "price": plan.entry, "at": stamp.strftime("%H:%M:%S")}
        _save(st)
        out.append({**best.__dict__, "early": True})
        log.info("EARLY %s %s price=%.2f sl=%.2f @ %s (sent %s IST, %.0fs before close)",
                 best.name, raw, plan.entry, plan.sl, hhmm, stamp.strftime("%H:%M:%S"),
                 (close - stamp).total_seconds())
    # If "live 1m data" stays at 0 the Yahoo feed is too delayed for early alerts.
    log.info("early %s %s %s: %d names, %d with live 1m data, %d alert(s), done %.0fs before close",
             hhmm, scan["sector"], want, len(names), live, len(out),
             (close - now_ist()).total_seconds())
    _bump(st, "early_live", 1 if live else 0)
    _bump(st, "early_sent", len(out))
    _save(st)
    return out


def _move_pct(pos: dict, price: float) -> float:
    """Signed move from entry in the trade's favour (+) or against it (-), in %."""
    delta = (price - pos["entry"]) if pos["side"] == "BUY" else (pos["entry"] - price)
    return delta / pos["entry"] * 100.0


def _risk_of(pos: dict) -> float:
    """One R in price: the entry-to-original-stop distance, never the moved stop."""
    if pos.get("risk"):
        return abs(float(pos["risk"]))
    entry = float(pos.get("entry") or 0)
    stop = float(pos.get("sl0") or pos.get("sl") or 0)
    return abs(entry - stop)


def _r_of(pos: dict, price: float) -> str:
    risk = _risk_of(pos)
    if risk <= 0:
        return ""
    signed = (price - pos["entry"]) if pos["side"] == "BUY" else (pos["entry"] - price)
    return f", {signed / risk:+.1f}R"


def _event_line(raw: str, pos: dict, bar: datetime, headline: str, detail: str) -> str:
    return (f"{headline}  {raw} · {detail} · 5m {to_ist(bar).strftime('%H:%M')}"
            f" · entry {pos['entry']:.2f}")


def format_target_hit(raw: str, pos: dict, bar: datetime,
                      hits: list[tuple[int, float]]) -> str:
    """One line for the pivot target(s) first touched in this completed candle."""
    names = list(pos.get("t_names") or ())
    bits = []
    for slot, level in hits:
        name = names[slot - 1] if len(names) >= slot and names[slot - 1] else ""
        bits.append(f"T{slot}{' ' + name if name else ''} {level:.2f}"
                    f" ({_move_pct(pos, level):+.2f}%{_r_of(pos, level)})")
    moved = pos.get("sl0") is not None and abs(float(pos.get("sl") or 0)
                                               - float(pos["sl0"])) > 1e-9
    if moved:
        bits.append(f"stop → breakeven {float(pos['sl']):.2f}")
    return _event_line(raw, pos, bar, f"🎯 <b>TARGET HIT — {pos['side']}</b>", " · ".join(bits))


def format_sl_hit(raw: str, pos: dict, bar: datetime, level: float) -> str:
    """One line when the stop is touched (intrabar, evaluated on a closed candle)."""
    moved = pos.get("sl0") is not None and abs(float(level) - float(pos["sl0"])) > 1e-9
    tag = " · stop moved to breakeven after T1" if moved else ""
    return _event_line(raw, pos, bar, f"🛑 <b>SL HIT — {pos['side']}</b>",
                       f"SL {level:.2f} ({_move_pct(pos, level):+.2f}%{_r_of(pos, level)}){tag}")


def format_trail_exit(raw: str, pos: dict, bar: datetime, close: float, ema: float) -> str:
    """One line when a completed candle closes beyond EMA9."""
    direction = "below" if pos["side"] == "BUY" else "above"
    return _event_line(raw, pos, bar, f"🔔 <b>EMA9 EXIT — {pos['side']}</b>",
                       f"close {close:.2f} {direction} EMA9 {ema:.2f}"
                       f" ({_move_pct(pos, close):+.2f}%{_r_of(pos, close)})")


def _max_lag() -> int:
    """Maximum acceptable delay after a five-minute candle CLOSE."""
    return int(os.environ.get("MAX_ALERT_LAG_SECONDS", "180"))


def _settle_seconds() -> float:
    """After this long past the close a published 5m candle is taken as final."""
    return float(os.environ.get("BAR_SETTLE_SECONDS", "45"))


def _has_print_since(df1m: pd.DataFrame | None, moment: datetime) -> bool:
    return df1m is not None and not df1m.empty and df1m.index[-1] >= pd.Timestamp(moment)


def _unsettled(frames1m: dict, symbols, close_at: datetime) -> tuple[set, bool]:
    """Symbols whose just-closed 5m candle may still be missing its last trades.

    Yahoo's 5m row exists while the candle is forming, so its presence proves
    nothing. A stock is settled once its own 1m feed shows a print at/after the
    close. Without one there are two very different cases:

    * other stocks of the batch already print past the close → this stock is
      simply quiet; it is accepted after ``BAR_SETTLE_SECONDS``;
    * nobody prints past the close → the whole feed is behind; every candle is
      still filling, so all of them wait (until the alert deadline if need be).

    Returns ``(unsettled, feed_behind)``. With no 1m data at all there is
    nothing to judge by and only the settle time applies. The same holds for
    the day's last candle: nothing prints after 15:30, so waiting for a print
    would wait forever.
    """
    symbols = list(symbols)
    since = (now_ist() - close_at).total_seconds()
    frames = {sym: frames1m.get(sym) for sym in symbols}
    if not any(df is not None and not df.empty for df in frames.values()):
        return (set(symbols) if since < _settle_seconds() else set()), False
    live = any(_has_print_since(df, close_at) for df in frames.values())
    behind = not live and close_at.time() < clock_time(15, 30)
    waiting = {sym for sym, df in frames.items()
               if not _has_print_since(df, close_at) and (behind or since < _settle_seconds())}
    return waiting, behind


def _warn_feed_behind(st: dict, close_at: datetime, frames1m: dict, *,
                      send=None, save=None) -> None:
    """Say once per session that alerts are being held back by a delayed feed.

    ``send``/``save`` default to bot 1 and the 5m ledger; the 1m scan passes
    bot 3 and its own ledger, so the notice is word-for-word the same.
    """
    send = send or telegram.send
    save = save or _save
    stats = st.setdefault("stats", {})
    if stats.get("feed_warned") or (now_ist() - close_at).total_seconds() < 120:
        return
    newest = max((df.index[-1] for df in frames1m.values() if df is not None and not df.empty),
                 default=None)
    if newest is None:
        return
    behind = (pd.Timestamp(now_ist()) - newest).total_seconds()
    if send(f"⚠ <b>Yahoo feed is delayed</b> — newest 1m bar is {newest:%H:%M} "
            f"({behind:.0f}s old) two minutes after the {close_at:%H:%M} close. Entry alerts are "
            "held back rather than calculated on unfinished candles; if this continues, "
            "few or no alerts will arrive today.", dry_run=_dry()):
        stats["feed_warned"] = True
        save(st)


def _recent_bar(bar: datetime, now: datetime) -> bool:
    age = (now - (bar + timedelta(minutes=5))).total_seconds()
    return 0 <= age <= _max_lag()


def _save(st: dict) -> None:
    # A dry-run must not consume tomorrow's real alert or today's alert.
    # (The dry-run live loop writes to its own scratch ledger instead.)
    if not _dry() or state.scratch_active():
        state.save(st)


def _alert_prefix(raw: str, pos: dict) -> str:
    """Ledger namespace for one trade: symbol + the candle it was entered on.

    A later trade on the same symbol therefore gets its own alerts, while the same
    trade can never be announced twice.
    """
    return f"{raw}:{pos.get('entry_bar') or pos.get('last_bar')}:"


def _alert_key(raw: str, pos: dict, tag: str) -> str:
    return _alert_prefix(raw, pos) + tag


def _track_positions(st: dict, bar: datetime, period_5m: str, *,
                     send=None, save=None) -> list[str]:
    """One-line hit alerts for open trades, independent of market/sector state.

    Three events only, each on ONE line, each announced **once per level**:

    * 🎯 TARGET HIT — the pivot target(s) first touched by that candle. Informational:
      the position stays open and later targets are still watched.
    * 🛑 SL HIT — the initial stop was touched intrabar. Closes the position.
    * 🔔 EMA9 EXIT — the candle *closed* beyond EMA9 (the documented trail rule).
      Closes the position.

    Repeat protection mirrors the entry alert: every send is recorded in
    ``st["alerts"]`` (written before the next Telegram call) and re-checked before
    any send, so re-driving the same candle — a retried run, a replayed ledger, a
    position record rebuilt by hand — cannot produce the same message twice. Target
    levels are additionally remembered on the position (``hit_targets``) so a
    level never fires twice within the trade.

    A trade is intraday: only candles of the **session it was entered in** are
    replayed. A position left in the ledger by an earlier session is dropped
    before anything is fetched — never trailed against today's bars (see
    ``state.position_session``).

    Every candle since the last check is replayed, so a cycle that missed a bar
    (slow Yahoo, queued Actions run) still reports what happened, stamped with
    that candle's time. A failed Telegram POST stops the replay at that candle so
    the next cycle can retry it without resending what already went out.

    ``send``/``save`` default to the 5m scan's own (``telegram.send`` and the 5m
    ledger); the 1m scan passes bot 3 and its own ledger, so it emits the *same*
    hit / stop / exit alerts on the same rules without touching the 5m state.
    """
    send = send or telegram.send
    save = save or _save
    positions = st.setdefault("positions", {})
    if not positions:
        _store_pending(st, [])
        return []
    ledger = st.setdefault("alerts", {})
    session = session_date(bar)
    session_key = str(session)
    # A trade belongs to the session it was entered in. Anything else here is a
    # leftover: it would be checked against TODAY's candles, which is how a PNB
    # entry from 11 Nov produced "TARGET HIT T1 136.75 · T2 141.02 · 5m 09:15"
    # on 16 Nov. Drop it without a message — the trade is not today's.
    for raw, pos in list(positions.items()):
        came_from = state.position_session(pos)
        if came_from and came_from != session_key:
            del positions[raw]
            log.warning("dropping %s: trade from %s is not today's (%s) — no hit alerts for it",
                        raw, came_from, session_key)
            # It is still the user's position. The machine stopped following it,
            # and that is a fact about the alert stream, not a log line.
            send(f"⚠ <b>TRADE ABANDONED</b> — {pos.get('side')} {raw} "
                 f"({float(pos.get('entry') or 0):.2f}, entered {came_from}) was dropped "
                 f"before an exit alert: a trade is intraday, so it is not trailed "
                 f"against {session_key}'s candles. Manage it yourself.",
                 dry_run=_dry())
            save(st)
    if not positions:
        _store_pending(st, [])          # nothing open: nothing can be unchecked
        save(st)
        return []
    symbols = {raw: data.yahoo_stock(raw) for raw in positions}
    frames = data.fetch_many(symbols.values(), period_5m, "5m")
    # An EMA9 exit is decided by the candle's CLOSE: never read it half-published.
    # Older candles are still replayed, so a delayed feed postpones a hit alert
    # by a candle instead of losing it.
    waiting, _ = _unsettled(data.fetch_many(symbols.values(), "1d", "1m"),
                            symbols.values(), bar + timedelta(minutes=5))
    pending: list[str] = []
    changed = False
    for raw, pos in list(positions.items()):
        df = frames.get(symbols[raw])
        df = df.loc[df.index <= bar] if df is not None and not df.empty else df
        newest = bar
        if symbols[raw] in waiting:
            pending.append(raw)
            log.info("hit-check %s: feed has not passed the %s candle's close; older candles only",
                     raw, bar.strftime("%H:%M"))
            newest = bar - timedelta(minutes=5)
            df = df.loc[df.index <= newest] if df is not None and not df.empty else df
        if df is None or df.empty or to_ist(df.index[-1]) != newest:
            if raw not in pending:
                pending.append(raw)
            log.info("hit-check pending %s: 5m candle %s not published", raw, bar)
            continue
        grid = ema9_frame(df)
        if grid is None or grid.empty:
            pending.append(raw)
            continue
        # The candles are here, so this trade IS being checked. The book lets the
        # close-of-day line tell "never checked" from "checked all day, last
        # candle missing from the feed" — 19 Nov printed the first about a trade
        # that had hit T1 and T2.
        book = st.setdefault("stats", {}).setdefault("checked", [])
        if raw not in book:
            book.append(raw)
        ema_at = dict(zip(grid.index, grid["EMA9"].to_numpy(float)))
        side = pos["side"]
        sl = float(pos.get("sl") or 0)          # the live stop (breakeven after T1)
        targets = [(i, float(pos[f"t{i}"])) for i in (1, 2, 3) if pos.get(f"t{i}") is not None]
        done = set(pos.get("hit_targets") or ())
        prefix = _alert_prefix(raw, pos)
        announced = {k[len(prefix):] for k in ledger if k.startswith(prefix)}
        stamp = pos.get("last_bar") or pos.get("entry_bar")
        cursor = pd.Timestamp(stamp) if stamp else None
        if cursor is not None and cursor.tzinfo is None:
            cursor = cursor.tz_localize(IST)
        replay = [ts for ts in grid.index
                  if (cursor is None or ts > cursor) and session_date(to_ist(ts)) == session]
        for ts in replay:
            row = grid.loc[ts]
            hhmm = to_ist(ts).strftime("%H:%M")

            # The stop in force at the OPEN of this bar. A T1 hit below may move
            # it to breakeven, which then binds from the NEXT bar: intrabar order
            # is unknown, so the bar that reaches T1 is not also judged against a
            # stop that only exists because T1 was reached.
            sl = float(pos.get("sl") or 0)
            hit_now = [(slot, lvl) for slot, lvl in targets
                       if slot not in done and f"T{slot}" not in announced
                       and (row["High"] >= lvl if side == "BUY" else row["Low"] <= lvl)]
            if hit_now:
                # A hit target that is then given back is the day's signature
                # loss (16 Nov: LUPIN +1.3R -> +0.2R, NESTLEIND +0.8R -> -0.1R).
                # Once T1 is touched the stop goes to entry, so the trade cannot
                # turn into a loser; the original stop stays on the record as
                # ``sl0`` so R and "SL HIT" still read against the real risk.
                # Moved before the send, so the T1 line can say so. It binds from
                # the NEXT bar: intrabar order is unknown.
                if 1 in {slot for slot, _ in hit_now} and trade.breakeven_after_t1():
                    pos.setdefault("sl0", pos.get("sl"))
                    before = float(pos.get("sl") or 0)
                    pos["sl"] = trade.breakeven_stop(side, float(pos["entry"]), before)
                    if abs(pos["sl"] - before) > 1e-9:
                        log.info("stop moved to breakeven %s %s: %.2f -> %.2f",
                                 raw, side, before, pos["sl"])
                if not send(format_target_hit(raw, pos, to_ist(ts), hit_now),
                            dry_run=_dry()):
                    pending.append(raw)
                    break
                for slot, _ in hit_now:
                    ledger[_alert_key(raw, pos, f"T{slot}")] = hhmm
                    announced.add(f"T{slot}")
                done.update(slot for slot, _ in hit_now)
                pos["hit_targets"] = sorted(done)
                changed = True
                save(st)                        # before the next send: never twice
                log.info("TARGET HIT %s %s %s @ %s", raw, side,
                         ",".join(f"T{slot} {lvl}" for slot, lvl in hit_now), ts)

            sl_hit = bool(row["Low"] <= sl) if side == "BUY" else bool(row["High"] >= sl)
            if sl_hit:
                if "SL" in announced:           # already delivered: just reconcile
                    _close_trade(st, raw, pos, sl, "SL HIT", hhmm)
                    del positions[raw]
                    changed = True
                    log.warning("SL for %s already announced; dropping stale position", raw)
                    break
                if not send(format_sl_hit(raw, pos, to_ist(ts), sl), dry_run=_dry()):
                    pending.append(raw)
                    break
                ledger[_alert_key(raw, pos, "SL")] = hhmm
                _close_trade(st, raw, pos, sl, "SL HIT", hhmm)
                del positions[raw]
                st.setdefault("sent", {}).setdefault(raw, {"code": "SL", "time": hhmm})
                changed = True
                save(st)
                log.info("SL HIT %s %s sl=%.2f @ %s", raw, side, sl, ts)
                break

            ema = ema_at.get(ts)
            close = float(row["Close"])
            if ema is not None and isfinite(ema) and trail_broken(side, close, float(ema),
                                                                  risk=_risk_of(pos)):
                if "EXIT" in announced:         # already delivered: just reconcile
                    _close_trade(st, raw, pos, close, "EMA9 EXIT", hhmm)
                    del positions[raw]
                    changed = True
                    log.warning("EMA9 exit for %s already announced; dropping stale position", raw)
                    break
                if not send(format_trail_exit(raw, pos, to_ist(ts), close, float(ema)),
                            dry_run=_dry()):
                    pending.append(raw)
                    break
                ledger[_alert_key(raw, pos, "EXIT")] = hhmm
                _close_trade(st, raw, pos, close, "EMA9 EXIT", hhmm)
                del positions[raw]
                # Block a new entry on the same IST session after an exit.
                st.setdefault("sent", {}).setdefault(raw, {"code": "EXIT", "time": hhmm})
                changed = True
                save(st)
                log.info("EMA9 EXIT %s %s close=%.2f ema9=%.4f @ %s", raw, side, close, ema, ts)
                break

            pos["last_bar"] = ts.isoformat()
            pos["last_close"] = round(float(row["Close"]), 2)   # scored at the close
            changed = True
    if changed or pending:
        _store_pending(st, pending)
        save(st)
    return pending


def _store_pending(st: dict, pending) -> None:
    """Record which open trades could not be checked — and forget the ones that can.

    A trade whose bars never arrive is replayed later, never dropped, but until
    this line the delay was invisible: no hit alert, no reason. The list has to be
    cleared as well as set: it used to survive the trades it described, so a
    session whose last position had closed still printed "not checked: PNB".
    """
    names = sorted(set(pending or ()))
    stats = st.setdefault("stats", {})
    if names:
        stats["pending"] = names
    else:
        stats.pop("pending", None)
    # The "was it checked at all" book describes the open book: a trade that has
    # closed is not in it any more, and neither is a name the next session
    # inherits from this one.
    live = set((st.get("positions") or {}).keys())
    checked = [n for n in (stats.get("checked") or ()) if n in live]
    if checked:
        stats["checked"] = checked
    else:
        stats.pop("checked", None)


def run_once(*, force_sector: str | None = None, ping_on_wait: bool = False, backfill: bool = False) -> dict:
    uni = data.load_universe()
    period_5m = os.environ.get("YF_PERIOD_5M", "7d")
    period_1d = os.environ.get("YF_PERIOD_1D", "90d")

    nifty_sym = uni["nifty"]
    vix_sym = uni["vix"]
    index_map = {name: uni["sectors"][name]["index"] for name in SECTOR_ORDER if name in uni["sectors"]}

    now = now_ist()
    today = str(now.date())
    allow_stale = backfill or os.environ.get("BACKFILL", "0") == "1"
    if allow_stale and not _dry():
        raise ValueError("Backfill is diagnostic only; pass --dry-run so old trades cannot reach Telegram")

    # Trailing exits must not depend on Nifty publication or the sector picker:
    # the stock can close below EMA9 while the index feed is stale or WAIT.
    trail_pending: list[str] = []
    if not allow_stale:
        st = state.reset_if_new_day(state.load(), today)
        closed_bar = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0) - timedelta(minutes=5)
        if (closed_bar.weekday() < 5 and clock_time(9, 15) <= closed_bar.time() <= clock_time(15, 25)
                and _recent_bar(closed_bar, now)):
            trail_pending = _track_positions(st, closed_bar, period_5m)

    log.info("fetching nifty 5m")
    nifty_pack = data.fetch_symbol(nifty_sym, period_5m, period_1d)
    nifty_5 = nifty_pack["m5"]
    if nifty_5 is None or nifty_5.empty:
        raise RuntimeError("No Nifty 5m data from yfinance")

    # Yahoo timestamps 5m candles by OPEN; the last row can still be forming.
    # Never evaluate or announce an unclosed candle.
    nifty_5 = nifty_5.loc[nifty_5.index <= now - timedelta(minutes=5)]
    if nifty_5.empty:
        return {"session": str(now.date()), "bar": "", "market": "PENDING", "pick": "WAIT", "action": "WAIT", "signals": []}
    last = to_ist(nifty_5.index[-1])
    session = str(last.date())
    if session != today and not allow_stale:
        log.info("no NSE bar for %s (last %s) — skip (pass --backfill to scan last session)", today, session)
        return {
            "session": session,
            "bar": last.strftime("%Y-%m-%d %H:%M"),
            "market": "CLOSED",
            "pick": "WAIT",
            "action": "WAIT",
            "signals": [],
        }

    # No catch-up trades: a signal calculated from an older candle is not an
    # actionable entry. In particular a late Actions dispatch must not replay
    # a morning table row 15-20 minutes after its timestamp.
    if not allow_stale and not _recent_bar(last, now):
        lag = (now - (last + timedelta(minutes=5))).total_seconds()
        log.warning("Nifty bar %s is not fresh (%.0fs since close); no alerts", last, lag)
        return {"session": session, "bar": last.strftime("%Y-%m-%d %H:%M"),
                "market": "STALE", "pick": "WAIT", "action": "WAIT", "signals": []}

    log.info("fetching vix / %d sector indices", len(index_map))
    idx_tickers = [vix_sym] + list(index_map.values())
    m5 = data.fetch_many(idx_tickers, period_5m, "5m")
    d1 = data.fetch_daily(idx_tickers, period_1d)
    # Align all inputs to the Nifty decision bar, not a newer in-progress
    # sector/stock candle that happened to arrive during the fetch.
    m5 = {sym: df.loc[df.index <= last] if df is not None and not df.empty else df
          for sym, df in m5.items()}
    m5[nifty_sym] = nifty_5
    d1[nifty_sym] = nifty_pack["d1"]

    if allow_stale:  # diagnostic only; does not touch the real position ledger
        st = state.reset_if_new_day(state.load(), session)

    sector_5 = {name: m5.get(sym) for name, sym in index_map.items()}
    sector_d = {name: d1.get(sym) for name, sym in index_map.items()}

    md = run_market(
        nifty_5,
        d1.get(nifty_sym),
        m5.get(vix_sym),
        d1.get(vix_sym),
        sector_5,
        sector_d,
        prev_pick=previous_pick(st.get("scan"), last),
        sticky_note=lapsed_stickiness(st.get("scan"), last),
    )
    log.info(
        "market %s conf=%s pick=%s action=%s @ %s",
        md.dir_label,
        md.dir_conf,
        md.pick_sector,
        md.action,
        md.bar_time,
    )
    if md.bar_time != last.strftime("%Y-%m-%d %H:%M"):
        log.warning("market sanitation moved decision to %s; refusing to alert for %s", md.bar_time, last)
        return {"session": session, "bar": md.bar_time, "market": "STALE",
                "pick": "WAIT", "action": "WAIT", "signals": []}

    if force_sector:
        force_sector = force_sector.upper()
        if force_sector in uni["sectors"]:
            md.pick_sector = force_sector
            md.pick_side = md.dir_state if md.dir_state != 0 else 1
            md.action = "SCAN LONG" if md.pick_side == 1 else "SCAN SHORT"
            md.pick_reason = f"FORCED {force_sector}"

    # The pick the early pass is currently using (before this bar overwrites it).
    prev_pick = (st.get("scan") or {}).get("sector")
    # The next early (pre-close) pass scans this pick; it must be this bar's.
    scanning = md.pick_sector != "WAIT" and md.action != "WAIT" and md.pick_side in (1, -1)
    extra = list(getattr(md, "extra_sectors", None) or ()) if scanning else []
    # The other side of the tape, when the engine found a leader there.
    alt = str(getattr(md, "alt_sector", "") or "") if scanning else ""
    alt_side = int(getattr(md, "alt_side", 0) or 0) if alt else 0
    if alt_side not in (1, -1) or alt in ("", md.pick_sector):
        alt, alt_side = "", 0
    pick_snap = (getattr(md, "sectors", None) or {}).get(md.pick_sector) or {}
    # The per-bar incumbent trail (PICK_STICKY_LOG=1 only): with it, the early
    # pass writing this candle cannot erase the candle before it.
    pick_log = {}
    if pick_sticky_log():
        pick_log = record_pick(st.get("scan"), md.bar_time,
                               md.pick_sector if scanning else "WAIT",
                               md.pick_side if scanning else 0)
    st["scan"] = {"bar": md.bar_time, "sector": md.pick_sector if scanning else "WAIT",
                  "side": md.pick_side if scanning else 0,
                  "label": md.dir_label, "conf": md.dir_conf,
                  "extra": extra, "follow": bool(scanning and _follow_tag(md)),
                  "alt": {"sector": alt, "side": alt_side} if alt else None,
                  # the sector index's move from its own open: bot 3 ranks and
                  # labels its entries on the same number as bot 1
                  "intra": _intra_of(pick_snap),
                  # the dual pick's own sector move, and the line bot 1 prints
                  # under its entries — bot 3 sends the same sentence
                  "alt_intra": _intra_of((getattr(md, "sectors", None) or {}).get(alt)) if alt else None,
                  "alt_reason": (getattr(md, "alt_reason", "") or "") if alt else ""}
    if pick_log:
        st["scan"]["log"] = pick_log

    if not allow_stale and (st.get("stats") or {}).get("bar") != md.bar_time:
        st.setdefault("stats", {})["bar"] = md.bar_time      # count each candle once
        _note_sector_day(st, md, md.bar_time)                # the day's sector shape
        _bump(st, "candles")
        _bump(st, "picks", 1 if scanning else 0)
        if scanning:
            side = "long" if md.pick_side == 1 else "short"
            tag = ", follow" if _follow_tag(md) else ""
            book = st["stats"].setdefault("sectors", {})
            key = f"{md.pick_sector} {side}{tag}"
            book[key] = book.get(key, 0) + 1          # the pick, once per candle
            followed = st["stats"].setdefault("extra", {})
            for name in extra:                        # what it dragged along
                followed[name] = followed.get(name, 0) + 1
            if alt:
                dual_key = f"{alt} {'long' if alt_side == 1 else 'short'}, dual"
                book[dual_key] = book.get(dual_key, 0) + 1

    # market / sector change alerts
    prev_m = st.get("market") or {}
    prev_s = st.get("sector") or {}
    market_changed = prev_m.get("dir_state") != md.dir_state or prev_m.get("label") != md.dir_label
    sector_changed = prev_s.get("name") != md.pick_sector or prev_s.get("side") != md.pick_side
    prev_conf = prev_m.get("conf")
    conf_moved = (prev_conf is not None and _conf_delta() > 0
                  and abs(int(md.dir_conf) - int(prev_conf)) >= _conf_delta())
    if (market_changed or sector_changed or conf_moved) and (allow_stale or _recent_bar(last, now_ist())) and telegram.send(format_market(md, prev_conf, sent=now_ist()), dry_run=_dry()):
        st["market"] = {
            "dir_state": md.dir_state,
            "label": md.dir_label,
            "conf": md.dir_conf,
            "regime": md.regime,
        }
        st["sector"] = {"name": md.pick_sector, "side": md.pick_side, "reason": md.pick_reason}
        _save(st)

    result = {
        "session": session,
        "bar": md.bar_time,
        "market": md.dir_label,
        "pick": md.pick_sector,
        "action": md.action,
        "signals": [],
    }
    if trail_pending:
        result["pending"] = trail_pending

    if md.pick_sector == "WAIT" or md.action == "WAIT":
        if ping_on_wait and not market_changed and not sector_changed:
            log.info("WAIT — no complementary persistent sector")
        if not allow_stale:
            _settle_early(st, last, set(), "not checked — the pick dropped to WAIT")
        _save(st)
        return result

    sector_of, side_of = _scan_sides(uni, md.pick_sector, md.pick_side, extra,
                                     alt, alt_side)
    stocks = list(sector_of)
    tickers = [data.yahoo_stock(s) for s in stocks]
    where = " + ".join([md.pick_sector, *extra] + ([f"{alt} (dual)"] if alt else []))
    log.info("scanning %s (%d names) 5m entries", where, len(tickers))
    oi_reader = _oi_reader(session)
    sm5 = data.fetch_many(tickers, period_5m, "5m")
    # Only fetch the expensive 1m volume repair/daily history if there is a
    # current completed stock candle to evaluate.
    sm5 = {sym: df.loc[df.index <= last] if df is not None and not df.empty else df
           for sym, df in sm5.items()}
    current = [sym for sym in tickers if sm5.get(sym) is not None
               and not sm5[sym].empty and to_ist(sm5[sym].index[-1]) == last]
    sent = st.setdefault("sent", {})
    positions = st.setdefault("positions", {})
    missing = [raw for raw, sym in zip(stocks, tickers)
               if sym not in current and raw not in sent and raw not in positions]
    if missing:
        log.info("waiting for %d stock bar(s) @ %s: %s", len(missing),
                 last.strftime("%H:%M"), ", ".join(missing))
        result.setdefault("pending", []).extend(raw for raw in missing if raw not in trail_pending)
    if not current:
        if missing:
            result["action"] = "PENDING"  # retry within the freshness deadline
        _save(st)
        return result
    sm1 = data.fetch_many(current, "7d", "1m")
    # A 5m row exists while its candle is still forming, so "the row is there"
    # does not prove Yahoo has the candle's last trades. Right after the close,
    # accept a stock only once its feed shows a print at/after the close; after
    # BAR_SETTLE_SECONDS the candle is taken as final (the pre-loop behaviour).
    close_at = last + timedelta(minutes=5)
    if not allow_stale:
        _note_feed_age(st, "close", sm1)
    if not allow_stale:
        waiting, behind = _unsettled(sm1, current, close_at)
        unsettled = [raw for raw, sym in zip(stocks, tickers)
                     if sym in waiting and raw not in sent and raw not in positions]
        if behind:
            _warn_feed_behind(st, close_at, sm1)
        if unsettled:
            log.info("waiting for %d stock feed(s) to pass the %s close%s: %s", len(unsettled),
                     close_at.strftime("%H:%M"), " (whole feed behind)" if behind else "",
                     ", ".join(unsettled))
            current = [sym for sym in current if sym not in waiting]
            missing = missing + unsettled
            result.setdefault("pending", []).extend(
                raw for raw in unsettled if raw not in result.get("pending", []))
    sd1 = data.fetch_daily(current, period_1d)

    leaders = _sector_leaders(stocks, tickers, sector_of, side_of, sm5, current, md, last)
    if not allow_stale and sector_top_n():
        per_sector: dict[str, list[int]] = {}
        for raw in stocks:
            slot = per_sector.setdefault(sector_of[raw], [0, 0])
            slot[0 if raw in leaders else 1] += 1        # [kept, dropped]
        for sector, (kept, dropped) in per_sector.items():
            _note_trim(st, sector, kept, kept + dropped)
    _note_scan_lag(st, last, stale=allow_stale)
    new_signals: list[dict] = []
    handled: set[str] = set(missing)
    candidates: list[dict] = []
    late: list[dict] = []
    # Once per candle, not once per name: the window is a property of the pick.
    late_open = _late_due(st, md, last)

    for raw, ysym in zip(stocks, tickers):
        # Only a sector's leaders are judged: the tail is the sector moving, and
        # its "signal" is the sector's, not the name's.
        if raw not in leaders:
            log.info("follower %s @ %s: outside %s's top %d; not scanned",
                     raw, last.strftime("%H:%M"), sector_of[raw], sector_top_n())
            if not allow_stale:
                _note_reject(st, "follower", raw, f"{raw} {sector_of[raw]}")
                # An early alert on a name the trim has just dropped is resolved
                # by name — the sweep below would file it under "the pick moved",
                # and on this candle the pick did not move.
                trimmed = _open_early(st, raw, last)
                if trimmed:
                    _cancel_early(st, raw, trimmed,
                                  "not checked — the name is outside its sector's top names",
                                  code="follower")
            continue
        # The pick's side for the pick's sector, the other one for a dual pick.
        want_buy = side_of.get(raw, md.pick_side) == 1
        if ysym not in current:
            # No published candle for a name the pick named: the scan looked and
            # found nothing to judge. Invisible before this line.
            log.info("no 5m candle for %s @ %s; not scanned", raw, last)
            if not allow_stale:
                _note_reject(st, "no_data", raw, f"{raw} {last.strftime('%H:%M')}",
                             per_bar=last.strftime("%H:%M"))
            continue
        # One entry per stock per IST session, regardless of rank upgrades or
        # later runs. A new session resets this in reset_if_new_day().
        if raw in sent or raw in positions:
            continue
        handled.add(raw)
        early = None if allow_stale else _open_early(st, raw, last)
        df5 = data.apply_1m_volume(sm5[ysym], sm1.get(ysym))
        try:
            fires = scan_stock(raw, df5, sd1.get(ysym), keep_all=True)
        except Exception as e:
            log.exception("scan failed %s: %s", raw, e)
            continue
        # The TradingView table retains the FIRST hit (and rank upgrades) for
        # the whole day. That table is not a stream of new entry signals.
        fresh = [s for s in fires if s.time == last.strftime("%H:%M")
                 and s.side == ("BUY" if want_buy else "SELL")]
        if not fresh:
            same_side_before: list[str] = []
            if fires:
                log.info("SKIP historical/wrong-side %s fires=%s; decision bar=%s",
                         raw, ",".join(f"{s.name}@{s.time}" for s in fires),
                         last.strftime("%H:%M"))
                if not allow_stale:
                    hhmm, want = last.strftime("%H:%M"), "BUY" if want_buy else "SELL"
                    if any(s.time == hhmm for s in fires):
                        _note_reject(st, "opposite", raw, f"{raw} {hhmm}", per_bar=hhmm)
                    before = [s.time for s in fires if s.side == want and s.time < hhmm]
                    same_side_before = before
                    if before:
                        _note_reject(st, "earlier", raw, f"{raw} {min(before)}")
                        if late_open:
                            _consider_late(
                                st, late, raw, df5, fires, want, last, sd1.get(ysym),
                                sector_of[raw],
                                intra=(getattr(md, "sectors", None) or {}).get(
                                    sector_of[raw], {}).get("intra_ret"))
            if early:
                # Two different deaths, one line before: the candle closed on the
                # other side (the signal flipped) or no crossover was left at all.
                hhmm_now = last.strftime("%H:%M")
                flipped = any(s.time == hhmm_now and s.side != want for s in fires)
                code = "opposite" if flipped and not same_side_before else "no_signal"
                reason = ("the closed candle fired the opposite side" if code == "opposite"
                          else "no signal on the closed candle")
                _cancel_early(st, raw, early, reason, code=code,
                              close=float(df5["Close"].iloc[-1]))
            continue
        best = fresh[-1]
        if not allow_stale and not _recent_bar(last, now_ist()):
            log.warning("scan finished too late for %s @ %s; dropping entry", raw, best.time)
            _note_reject(st, "late", raw, f"{raw} {best.time}", per_bar=best.time)
            if early:
                # The projection was never judged — say so instead of leaving it to
                # the end-of-run "could not be checked" sweep, which cannot tell a
                # late scan from a scan error.
                _cancel_early(st, raw, early, "the close scan ran past the alert deadline",
                              code="late", close=float(df5["Close"].iloc[-1]))
            continue
        plan = build_plan(best, df5, last, sd1.get(ysym))
        if plan is None:
            log.warning("SKIP %s @ %s: missing EMA9 or EMA9 stop not beyond entry close", raw, best.time)
            if not allow_stale:
                _note_reject(st, "no_stop", raw, f"{raw} {best.time}", per_bar=best.time)
            if early:
                _cancel_early(st, raw, early, "no EMA9 stop beyond the entry on the closed candle",
                              code="no_stop", close=float(df5["Close"].iloc[-1]))
            continue
        if not allow_stale and scan_require_target() and not _plan_has_target(plan):
            log.warning("SKIP %s @ %s: no pivot target (T1/T2/T3 N/A)", raw, best.time)
            _note_reject(st, "no_target", raw, f"{raw} {best.time}", per_bar=best.time)
            if early:
                _cancel_early(st, raw, early, "no pivot target — the trade would have had no exit",
                              code="no_target", close=float(df5["Close"].iloc[-1]))
            continue
        best.price = plan.entry
        best.sl = plan.sl
        reading = oi_reader.snapshot(raw, last) if oi_reader else None
        if oi_reader and _oi_mode() == "require" and not should_take_entry(best.side, reading):
            why = reading.regime.value if reading else "no OI row"
            log.info("OI filter rejected %s %s @ %s: %s", raw, best.side, best.time, why)
            _note_reject(st, "oi", raw, f"{raw} {best.time}", per_bar=best.time)
            if early:
                _cancel_early(st, raw, early, f"open interest veto at the close ({why})",
                              code="oi", close=float(df5["Close"].iloc[-1]))
            continue
        # No send here: candidates are ranked and capped below, so one pick
        # cannot alert six names in universe order.
        lead = None
        if rank_by_sector_lead():
            lead = _lead_vs_sector(df5, last, float(df5["Close"].iloc[-1]), md,
                                   sector_of[raw], want_buy)
        close_now = float(df5["Close"].iloc[-1])
        intra = (getattr(md, "sectors", None) or {}).get(sector_of[raw], {}).get("intra_ret")
        runs, why_not, cand_rank = momentum_verdict(df5, last, close_now, want_buy, intra)
        if not runs:
            # The count is open by default; this is what keeps a name out.
            log.info("not running: %s @%s — %s (rank %.0f)", raw, best.time, why_not, cand_rank)
            _note_weak(st, why_not)
            _note_reject(st, "weak", raw, f"{raw} {best.time} — {why_not}", per_bar=best.time)
            if early and not allow_stale:
                _cancel_early(st, raw, early, why_not, code="weak", close=close_now)
            continue
        candidates.append({"raw": raw, "best": best, "plan": plan, "reading": reading,
                           "early": early, "lead": lead,
                           "rank": cand_rank if name_rank_mode() == "combined" else None,
                           "sector": sector_of[raw], "close": close_now})

    # The runners of each sector, not the first three names to arrive: the band
    # drops the laggards of the same move (`ENTRY_RANK_BAND`, 0.75 of the sector's
    # best rank on this candle).
    for cand in rank_band_drop(candidates):
        _note_reject(st, "weak", cand["raw"], f"{cand['raw']} {cand['best'].time} — "
                     f"rank {cand['rank']:.0f} is behind its sector's runners",
                     per_bar=cand["best"].time)
        _note_weak(st, "behind its sector's runners")
        if cand.get("early") and not allow_stale:
            _cancel_early(st, cand["raw"], cand["early"],
                          "behind its sector's runners on momentum", code="weak",
                          close=cand.get("close"))

    cap = max_entries_per_pick()
    st.setdefault("pick_entries", {})
    # The cap keeps the names that are moving *within* their sector, not the
    # names with the highest absolute score; score breaks ties.
    rank_candidates(candidates)
    for cand in candidates:
        raw, early, plan, best = cand["raw"], cand["early"], cand["plan"], cand["best"]
        # One sector is one risk factor — counted per sector, not per message, so
        # a SECTOR FOLLOW scan of two sectors does not halve its own cap.
        pick_key = pick_cap_key(sector_of[raw], side_of.get(raw, md.pick_side))
        if not entry_cap_room(st, pick_key):
            log.info("cap: %s not taken — %s already has %d entries this session",
                     raw, pick_key, cap)
            if not allow_stale:
                _note_reject(st, "cap", raw, f"{raw} {best.time}", per_bar=best.time)
            if early:
                _cancel_early(st, raw, early,
                              f"{pick_key} already has {cap} entries this session",
                              code="cap", close=cand["close"])
            continue
        reading = cand["reading"]
        dual_note = md.alt_reason if sector_of[raw] == alt and alt else ""
        text = format_stock(best, md, plan, sector=sector_of[raw], oi_line=_oi_line(reading),
                            lead=cand.get("lead"), dual=dual_note)
        if early:
            text = _confirm_header(early, plan.entry) + text
        if not telegram.send(text, dry_run=_dry()):
            log.warning("Telegram failed for %s; leaving unsent for retry", raw)
            continue
        if early:
            early["result"] = "confirmed"
            _bump(st, "confirmed")
        take_entry_slot(st, pick_key)
        lag = max(0.0, (now_ist() - close_at).total_seconds())
        _bump(st, "entries")
        _bump(st, "lag_sum", lag)
        st["stats"]["lag_max"] = max(st["stats"].get("lag_max", 0), lag)
        new_signals.append({**best.__dict__, "lead": cand.get("lead"),
                            "ema9": plan.ema9, "risk": plan.risk,
                            "t1": plan.t1, "t2": plan.t2, "t3": plan.t3,
                            "sector": sector_of[raw],
                            "oi_regime": reading.regime.value if reading else None,
                            "oi_chg_pct": reading.oi_chg_pct if reading else None})
        sent[raw] = {
            "code": best.code,
            "name": best.name,
            "time": best.time,
            "price": best.price,
        }
        positions[raw] = new_position(best.side, plan, last.isoformat(), session,
                                     sector=sector_of[raw])
        _save(st)  # persist each successful send in long-running live mode
        log.info("ALERT %s %s entry=%.2f sl=%.2f EMA9=%.4f @ %s (sent %s IST)",
                 best.name, raw, plan.entry, plan.sl, plan.ema9, best.time,
                 now_ist().strftime("%H:%M:%S"))

    # A name whose crossover fired before its sector was picked used to be refused
    # here and forgotten — 18 Nov: the whole CDUR book. The fresh entries of this
    # candle go out first; then the labelled heads-up for what the pick missed.
    if late:
        _send_late_leaders(st, md, last, late, side_of=side_of)

    if not allow_stale:
        # Early alerts on names the close scan never looked at (sector switched).
        was = prev_pick if prev_pick not in (None, "", "WAIT") else (prev_s or {}).get("name")
        moved = (f"{was} → {md.pick_sector}" if was and was != md.pick_sector
                 else f"to {md.pick_sector}")
        _settle_early(st, last, handled, f"not checked — the pick moved {moved}",
                      code="sector_moved")
    st["sent"] = sent
    st["session"] = session
    _save(st)
    result["signals"] = new_signals
    return result


def run_until_ready(*, expected_bar: datetime | None = None, force_sector: str | None = None) -> dict:
    """``_run_until_ready`` plus a final word on every early alert of that bar.

    An early heads-up must never be left dangling: if the close scan could not
    judge it before the freshness deadline (bar unpublished, feed stale, scan
    error) the user is told it is unverified.
    """
    if expected_bar is None:
        now = now_ist()
        expected_bar = (now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
                        - timedelta(minutes=5))
    try:
        return _run_until_ready(expected_bar=expected_bar, force_sector=force_sector)
    finally:
        try:
            if (not _dry() or state.scratch_active()) and state.active_path().exists():
                st = state.load()
                if any(not e.get("result") for e in (st.get("early") or {}).values()):
                    _settle_early(st, expected_bar, set(), "could not be checked in time",
                                  result="unverified", older=True, code="unchecked")
        except Exception:  # never mask the scan's own outcome
            log.exception("early-alert settlement failed")


_LATE_NOTE = ("the crossover candle is over — this is the fire's own entry and stop; "
              "no position was opened for it")


def _consider_late(st: dict, late: list[dict], raw: str, df5, fires, want: str,
                   bar: datetime, daily, sector: str,
                   intra: float | None = None) -> None:
    """Queue a labelled LATE LEADER, or count why it is not worth one.

    The 5m entry loop only takes a fire on the decision candle: a name whose
    crossover happened before its sector was picked is refused as ``earlier`` and
    never revisited, so on 18 Nov CDUR could be the pick from 09:45 with a 93→99
    persistence and alert no CDUR stock at all. This is the same rule bot 3 has
    had since the 1m round, with the same gates: same session, inside
    ``SCAN5M_LEADER_MAX_MINUTES``, the fire candle's EMA9 stop untouched since,
    and the move from that fire no larger than ``SCAN5M_LEADER_MAX_R``.
    """
    if not scan5m_leader_catchup():
        return
    from .scan1m import catchup_candidate
    why: dict = {}
    try:
        hit = catchup_candidate(df5, fires, want, bar, now_ist(), daily, report=why,
                                max_minutes=late_max_minutes(), max_r=late_max_r())
    except Exception:
        log.exception("late-leader check failed for %s", raw)
        return
    if hit is None:
        if why.get("why"):
            from .scan1m import _note_old_age
            stats = st.setdefault("stats", {})
            skips = stats.setdefault("catchup_skip", {})
            skips[why["why"]] = skips.get(why["why"], 0) + 1
            _note_old_age(stats, why)
        return
    # The catch-up is held to the same quality gate as a fresh entry: a leader
    # that is now sideways or reversing is a name to watch, not to enter.
    try:
        runs, why_not, _ = momentum_verdict(df5, bar, hit["price"], want == "BUY", intra)
    except Exception:
        runs, why_not = True, ""
    late.append({"raw": raw, "hit": hit, "sector": sector,
                 "runs": runs, "why_not": why_not})


# ── the late leader as an entry, not only a heads-up ─────────────────────────
#: ``LATE_LEADER_ENTRY`` (1): a leader that fired before its sector was picked
#: may still be *entered* at the price now, with the fire candle's stop.
#:
#: The heads-up alone is what 19 Nov produced: at 10:01 bot 3 announced HAVELLS
#: (+1.8R), WHIRLPOOL (+1.8R), KALYANKJIL (+1.4R), LGEINDIA (+1.3R), DIXON
#: (+0.4R) and VOLTAS (+0.3R) as "LATE LEADER", labelled *not an entry*, and the
#: day's five entries were TECHM, TATAELXSI, HCLTECH, BAJAJ-AUTO and EXIDEIND.
#: AMBER and VOLTAS were never more than a word in that list.
#:
#: Two of those six were still live trades (VOLTAS, DIXON: under 0.5R from the
#: fire, stop untouched); the other four had already run 1.3R-1.8R, and the ones
#: that had passed T1 are refused by :func:`late_plan`. So the rule enters what
#: is still there and refuses what has already paid — it cannot turn the list
#: into a chase, and it keeps the existing age (30 min) and distance (2R) gates.
def late_leader_entry() -> bool:
    return os.environ.get("LATE_LEADER_ENTRY", "1").strip() != "0"


def late_plan(hit) -> TradePlan | None:
    """The late leader's trade taken *now*, or ``None`` if it is no longer there.

    Entry is the current price (the candle the scan is judging), the stop is the
    fire candle's untouched EMA9 stop, and the targets are the fire's pivots.
    Refused when the price is already through that stop (the setup is dead) or
    when the *nearest* target is no longer worth the risk being taken now: the
    remaining distance to T1 must still be at least ``MIN_TARGET_R`` (0.5) of the
    catch-up risk, the same rule a fresh entry obeys.

    That second test is what keeps this honest. 19 Nov, M&M at 10:00 was +2.0R
    from its fire — price 3461.00 with T1 R2 at 3462.77, 1.77 points of room
    against a 44-point stop taken from here: 0.04R. "Price is still before T1"
    would have entered that. "The target is still worth the risk" refuses it, and
    the name stays the heads-up it should be.
    """
    sig, plan = hit.get("sig"), hit.get("plan")
    if sig is None or plan is None or plan.risk is None or plan.t1 is None:
        return None
    price = float(hit.get("price") or 0.0)
    long = sig.side == "BUY"
    if price <= 0:
        return None
    risk = (price - plan.sl) if long else (plan.sl - price)
    if risk <= 0:
        return None
    room = (plan.t1 - price) if long else (price - plan.t1)
    if room < trade.min_target_r() * risk:
        return None
    return TradePlan(entry=price, ema9=plan.ema9, sl=plan.sl,
                     risk=risk, t1=plan.t1, t2=plan.t2, t3=plan.t3,
                     pivot_names=plan.pivot_names, basis=plan.basis, skipped=plan.skipped)


def format_late_entry(raw: str, hit: dict, plan: TradePlan, md, bar: datetime,
                      *, sector: str | None = None, bot: str = "5m",
                      dual: str = "") -> str:
    """The catch-up entry alert — every field says what it is, so it cannot be
    mistaken for a signal that fired on this candle."""
    sig, fire = hit["sig"], hit["plan"]
    arrow = "🟢" if sig.side == "BUY" else "🔴"
    risk_pct = plan.risk / plan.entry * 100.0 if plan.entry else 0.0
    return (
        f"⏰ {arrow} <b>LATE LEADER ENTRY — {sig.side}</b>  |  <b>{sig.name}</b>  {raw}\n"
        f"{telegram.esc(sector or md.pick_sector)}  |  Market {md.dir_label} ({md.dir_conf}%)\n"
        + (f"<i>{telegram.esc(dual)}</i>\n" if dual else "")
        + f"Fired <b>{sig.time}</b> as {sig.name} @ {fire.entry:.2f} · "
        f"sector picked {bar:%H:%M} · {hit['age']:.0f} min ago\n"
        f"Entry <b>{plan.entry:.2f}</b> (this candle's close) · SL {plan.sl:.2f} "
        f"(the fire candle's EMA9 stop, untouched since)\n"
        f"Risk {plan.risk:.2f} ({risk_pct:.2f}%) · {hit['r']:+.1f}R from the fire  |  "
        f"targets: {_target_text(plan)}\n"
        f"Score {sig.score} {sig.strength}   {telegram.esc(sig.reason)}\n"
        + (f"Taken from the 5m close scan ({bot}).\n" if bot == "5m"
           else "Taken by the 1m scanner on this minute.\n")
        + "<b>Catch-up entry</b> — the crossover fired before its sector was picked and "
        "the trade is still before T1. The stop is the fire candle's, not this candle's."
    )


def take_late_entry(st: dict, md, bar: datetime, item: dict, now: datetime, *,
                    bot: str = "5m", send=None, save=None, dry=None,
                    side_of: dict | None = None, dual: str = "") -> bool:
    """Send ``item`` as a catch-up entry and book it. ``False`` = keep the heads-up.

    ``send``/``save``/``dry`` are the caller's: bot 3 has its own ledger file and
    its own Telegram bot, and a shared bookkeeper must not write the 5m state.
    """
    raw, hit = item["raw"], item["hit"]
    if isinstance(md, dict):                 # bot 3 passes its pick as a dict
        md = SimpleNamespace(pick_sector=md.get("sector"), dir_label=md.get("label"),
                             dir_conf=md.get("conf"))
    if not late_leader_entry():
        return False
    plan = late_plan(hit)
    if plan is None:
        return False
    if scan_require_target() and not _plan_has_target(plan):
        return False
    if item.get("runs") is False:          # the name is sideways or reversing
        log.info("late leader %s not entered — %s", raw, item.get("why_not") or "not running")
        return False
    side = 1 if hit["sig"].side == "BUY" else -1
    key = pick_cap_key(item.get("sector") or md.pick_sector,
                       int((side_of or {}).get(raw, side)))
    if not entry_cap_room(st, key):
        log.info("cap: LATE LEADER ENTRY %s not taken — %s already has %d entries",
                 raw, key, max_entries_per_pick())
        return False
    send = send or telegram.send
    if not send(format_late_entry(raw, hit, plan, md, bar, sector=item.get("sector"),
                                 bot=bot, dual=dual), dry_run=(dry or _dry)()):
        log.warning("Telegram failed for the late-leader entry %s; leaving it unsent", raw)
        return False
    take_entry_slot(st, key)
    _bump(st, "entries")
    _bump(st, "late_entries")
    st.setdefault("sent", {})[raw] = {"code": hit["sig"].code, "name": hit["sig"].name,
                                      "time": hit["sig"].time, "price": plan.entry}
    st.setdefault("positions", {})[raw] = new_position(hit["sig"].side, plan,
                                                       bar.isoformat(),
                                                       str(session_date(bar)),
                                                       sector=(item or {}).get("sector") or "")
    st.setdefault("catchup", {})[raw] = {
        "bar": bar.strftime("%Y-%m-%d %H:%M"), "session": str(session_date(bar)),
        "fired": hit["sig"].time, "side": hit["sig"].side, "code": hit["sig"].code,
        "price": plan.entry, "at": now.strftime("%H:%M:%S"), "entry": True}
    (save or _save)(st)
    log.info("LATE LEADER ENTRY %s %s fired %s as %s at %.2f (%.1fR from the fire), "
             "sector %s picked %s", raw, hit["sig"].side, hit["sig"].time,
             hit["sig"].name, plan.entry, hit["r"], item.get("sector"),
             bar.strftime("%H:%M"))
    return True


def _send_late_leaders(st: dict, md, bar: datetime, late: list[dict],
                       *, side_of: dict | None = None) -> None:
    """Send the labelled heads-up — one per name, or one digest for a burst."""
    from .scan1m import catchup_digest, format_catchup, format_catchup_digest

    pick = {"sector": md.pick_sector, "label": md.dir_label, "conf": md.dir_conf}
    # A late leader from the other side of the tape says so, in bot 3's words.
    alt = str(getattr(md, "alt_sector", "") or "")
    alt_note = str(getattr(md, "alt_reason", "") or "")
    now = now_ist()
    sent = st.setdefault("sent", {})
    book = st.setdefault("catchup", {})
    todo = [item for item in late if item["raw"] not in sent and item["raw"] not in book]
    if not todo:
        return
    todo.sort(key=lambda item: -item["hit"]["r"])
    # What is still a live trade becomes an entry; what has already paid, been
    # stopped out or run past 2R stays a heads-up (``LATE_LEADER_ENTRY=0`` keeps
    # every one of them a heads-up, as on 19 Nov).
    rest = [item for item in todo
            if not take_late_entry(st, md, bar, item, now,
                                   side_of=side_of,
                                   dual=alt_note if item["sector"] == alt else "")]
    if not rest:
        return
    todo = rest
    digest = len(todo) > 1 and catchup_digest()
    all_sent = (telegram.send(format_catchup_digest(todo, pick, bar, now, bot="5m", closed=True),
                              dry_run=_dry()) if digest else True)
    if digest and not all_sent:
        return
    for item in todo:
        raw, hit = item["raw"], item["hit"]
        if not digest and not telegram.send(
                format_catchup(raw, hit, pick, bar, now, sector=item["sector"],
                               bot="5m", closed=True,
                               dual=alt_note if item["sector"] == alt else ""),
                dry_run=_dry()):
            continue
        book[raw] = {"bar": bar.strftime("%Y-%m-%d %H:%M"), "session": str(session_date(bar)),
                     "fired": hit["sig"].time, "side": hit["sig"].side,
                     "code": hit["sig"].code, "price": hit["price"],
                     "at": now.strftime("%H:%M:%S")}
        _bump(st, "catchup")
        _save(st)
        log.info("LATE LEADER %s %s fired %s as %s, sector %s picked %s (%.1fR, %.0f min late)",
                 raw, hit["sig"].side, hit["sig"].time, hit["sig"].name,
                 md.pick_sector, bar.strftime("%H:%M"), hit["r"], hit["age"])


#: How long a candle is polled while today's index bar is still missing (seconds).
#:
#: 18 Nov 09:40: the 09:35 Nifty bar was ten seconds late, and ``_run_until_ready``
#: gave up at once — "CLOSED market after 09:30 = holiday" — although the alert
#: deadline was 09:43. The bar was there by 09:45, so a minute of polling would
#: have judged the candle. A holiday is still decided by the live loop's own rule
#: (three closed candles in a row), not by one bar being a minute behind.
NO_BAR_WAIT = 120.0


def _note_no_index(expected: str) -> None:
    """Record a candle that was never judged because the index bar never arrived.

    Keyed by candle, so the retries of one candle count once and a candle that
    does arrive late — and is judged — is never recorded.
    """
    try:
        st = state.reset_if_new_day(state.load(), expected[:10])
        book = st.setdefault("stats", {}).setdefault("no_index", {})
        if expected not in book:
            book[expected] = 1
            _save(st)
    except Exception:                      # never break the scan over bookkeeping
        log.exception("could not record the missing index bar for %s", expected)


def _retry_delay(close_time: datetime, remaining: float) -> float:
    """Poll fast right after the close (where it buys entry time), then back off."""
    since = (now_ist() - close_time).total_seconds()
    return max(0.0, min(5 if since < 30 else 15, remaining))


def _run_until_ready(*, expected_bar: datetime | None = None, force_sector: str | None = None) -> dict:
    """Poll Yahoo for this specific closed bar, within the alert freshness limit.

    A workflow_dispatch often starts only seconds after the close; one fetch can
    see yesterday's/previous bar and otherwise miss the entry for good. Pin the
    target once so a slow run never rolls forward and announces an older fire.
    """
    now = now_ist()
    if expected_bar is None:
        close = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
        expected_bar = close - timedelta(minutes=5)
    expected = expected_bar.strftime("%Y-%m-%d %H:%M")
    close_time = expected_bar + timedelta(minutes=5)
    deadline = close_time + timedelta(seconds=_max_lag())
    log.info("IST %s: target 5m bar %s (closes %s; alert deadline %s)",
             now.strftime("%Y-%m-%d %H:%M:%S"), expected,
             close_time.strftime("%H:%M:%S"), deadline.strftime("%H:%M:%S"))
    if (expected_bar.weekday() >= 5 or
            not clock_time(9, 15) <= expected_bar.time() <= clock_time(15, 25) or
            now > deadline):
        return run_once(force_sector=force_sector)

    last_error: RuntimeError | None = None
    result: dict | None = None
    delivered: list[dict] = []
    closed_since: datetime | None = None
    while now_ist() <= deadline:
        try:
            result = run_once(force_sector=force_sector)
            delivered.extend(result.get("signals", []))
            if (result["bar"] == expected and result["action"] != "PENDING"
                    and not result.get("pending")):
                result["signals"] = delivered
                return result
        except state.InvalidStateError:
            raise  # retrying cannot repair a corrupt cache; fail closed now
        except RuntimeError as exc:
            last_error = exc
            log.warning("scan not ready for %s: %s", expected, exc)
        remaining = (deadline - now_ist()).total_seconds()
        if remaining <= 0:
            break
        if result is not None and result.get("market") == "CLOSED":
            # Keep polling: a bar one candle behind is Yahoo, not a holiday.
            closed_since = closed_since or now_ist()
            waited = (now_ist() - closed_since).total_seconds()
            if waited >= NO_BAR_WAIT:
                log.warning("no NSE bar for %s after %.0fs of polling — leaving this candle",
                            expected, waited)
                break
        else:
            closed_since = None
        delay = _retry_delay(close_time, remaining)
        log.info("waiting for completed Yahoo bar %s (%.0fs until deadline); retry in %.0fs",
                 expected, remaining, delay)
        time.sleep(delay)
    if result is not None:
        result["signals"] = delivered
        if result.get("market") == "CLOSED":
            _note_no_index(expected)       # this candle is never judged today
        if result.get("pending"):
            log.warning("Yahoo stock bars did not publish by %s IST: %s; no late entries sent",
                        deadline.strftime("%H:%M:%S"), ", ".join(result["pending"]))
        return result
    if last_error:
        raise last_error
    return run_once(force_sector=force_sector)


def _sleep_until(target: datetime) -> None:
    while True:
        left = (target - now_ist()).total_seconds()
        if left <= 0:
            return
        time.sleep(min(left, 60))


def live_loop(grace: int | None = None, *, max_minutes: float | None = None,
              exit_at_close: bool = False) -> str:
    """Always-on loop: early pass before each 5m close, confirmed scan after it.

    Per candle:
      close - EARLY_ALERT_SECONDS  → ``run_early``  (unconfirmed heads-up)
      close + POLL_GRACE_SECONDS   → ``run_until_ready`` (confirm / cancel / entry)

    ``max_minutes`` and ``exit_at_close`` exist for hosts with a job time limit
    (GitHub Actions): the loop returns ``"budget"`` right after a finished cycle
    so a successor can take over, ``"closed"`` once the session is over, or
    ``"holiday"`` when the exchange publishes no bar. Without them it runs
    forever, as before.
    """
    from .session import next_5m_close, is_weekday

    grace = int(os.environ.get("POLL_GRACE_SECONDS", grace or 10))
    lead = _early_lead()
    started = now_ist()
    now = now_ist()
    if exit_at_close and (not is_weekday(now) or now.time() < clock_time(8, 30)
                          or now.time() >= clock_time(15, 33)):
        log.info("outside the NSE session (%s IST) — nothing to do", now.strftime("%a %H:%M"))
        return "closed"
    data.enable_daily_cache()
    if _dry():
        state.use_scratch()   # full behaviour, real ledger untouched
    log.info("live loop IST: early pass %ss before each 5m close%s, confirm %ss after",
             lead, "" if lead else " (disabled)", grace)
    from .knobs import describe
    for line in describe():        # what this process is actually running with
        log.info("%s", line)
    # Announce once per session: a planned handover between jobs is not news.
    # The flag is stored, so restarts (handover, watchdog, holiday re-checks)
    # stay silent for the rest of the session.
    st0 = state.reset_if_new_day(state.load(), str(now.date()))
    if not st0.get("announced") and telegram.send(
            "NSE live scanner started (5m · IST · yfinance)"
            + (f" · early alerts {lead}s before close." if lead else "."), dry_run=_dry()):
        st0["announced"] = True
        _save(st0)
    closed_streak = 0
    holiday = None
    while True:
        now = now_ist()
        if not is_weekday(now) or holiday == now.date():
            if exit_at_close:
                return "holiday" if holiday else "closed"
            log.info("no session today — sleeping 30m")
            time.sleep(1800)
            continue
        t = now.time()
        # park until 09:14
        if t.hour < 9 or (t.hour == 9 and t.minute < 14):
            target = now.replace(hour=9, minute=14, second=0, microsecond=0)
            sleep = max(5, (target - now).total_seconds())
            log.info("pre-open, sleep %.0fs", sleep)
            time.sleep(min(sleep, 300))
            continue
        if t.hour > 15 or (t.hour == 15 and t.minute >= 33):
            try:  # one summary of the session, never repeated
                st1 = state.reset_if_new_day(state.load(), str(now.date()))
                if (st1.get("stats") or {}).get("reported"):
                    text = None
                elif _eod().report_enabled():
                    text = None          # the full report is sent below, in parts
                    if _eod().send_eod(st1, bot="5m", send=telegram.send, dry_run=_dry()):
                        st1["stats"]["reported"] = True
                        _save(st1)
                        log.info("end-of-day report sent (%d candles)", int(
                            (st1.get("stats") or {}).get("candles") or 0))
                    else:
                        log.error("end-of-day report was NOT delivered — Telegram refused a "
                                  "part; the session is over, so no later run will send it")
                else:
                    text = format_summary(st1)
                if text:
                    # Said out loud either way: 18 Nov's 1m summary went out (or did
                    # not) without a trace, and the log could not answer the question.
                    if telegram.send(text, dry_run=_dry()):
                        st1["stats"]["reported"] = True
                        _save(st1)
                        log.info("summary sent: %d candles, %d lines",
                                 int((st1.get("stats") or {}).get("candles") or 0),
                                 text.count("\n") + 1)
                    else:
                        log.error("summary was NOT delivered — Telegram refused it; the session "
                                  "is over, so no later run will send it")
            except Exception:
                log.exception("session summary failed")
            if exit_at_close:
                return "closed"
            log.info("session over, sleep until tomorrow")
            time.sleep(1800)
            continue
        nxt = next_5m_close(now)
        bar = nxt - timedelta(minutes=5)
        in_session = clock_time(9, 15) <= bar.time() <= clock_time(15, 25)
        early_at = nxt - timedelta(seconds=lead)
        if lead and in_session and now_ist() < early_at + timedelta(seconds=min(20, lead / 2)):
            log.info("sleep until %s (early pass for %s)", early_at.strftime("%H:%M:%S"),
                     bar.strftime("%H:%M"))
            _sleep_until(early_at)
            try:
                run_early(bar)
            except Exception:
                log.exception("early pass failed")
        log.info("sleep until %s + %ss", nxt.strftime("%H:%M"), grace)
        _sleep_until(nxt + timedelta(seconds=grace))
        # Same bounded retry path as Actions: skip an old signal, never replay
        # an earlier bar merely because Yahoo published the new one late.
        if not in_session:
            continue
        try:
            out = run_until_ready(expected_bar=bar)
            closed_streak = closed_streak + 1 if out.get("market") == "CLOSED" else 0
            if closed_streak >= 3 and bar.time() >= clock_time(9, 30):
                holiday = now_ist().date()
                log.info("no NSE bar published today — treating %s as a holiday", holiday)
        except Exception:
            log.exception("scan cycle failed")
            time.sleep(15)
        if max_minutes and (now_ist() - started).total_seconds() / 60.0 >= max_minutes:
            log.info("runtime budget of %.0f min reached — handing over", max_minutes)
            return "budget"
