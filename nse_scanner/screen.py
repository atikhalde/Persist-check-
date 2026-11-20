"""Bot 4 — the name-first screen: a short list of setups, both sides, no auto-entry.

Why this exists
---------------
Three sessions of logs say the same thing three ways. On 18 Nov the CDUR book was
refused 452 times as ``historical/wrong-side``, on 19 Nov the AUTO book 871 times,
on 20 Nov every bank 148 times — because the 5m engine only enters a name whose
crossover fired *on the candle being judged*. The pick then arrives 30-60 candles
after its own leaders have fired, and the sector's whole book is unenterable for
the rest of the day. Meanwhile 6 of 20 Nov's 7 entries were board positions #3-#6
with strength 45-64 and volume/structure maxed: the tail of the move, bought
because the leaders were structurally ineligible.

This module inverts that. It has no pick, no trim, no "fired on this candle":

* every name of every sector is a candidate on every candle;
* a name is a *setup* when its **state** qualifies — the sector's persistence is
  high **and not falling**, the name is at/near its own session extreme and still
  moving that way, and its stop is still close enough to be worth taking;
* the sector is a **feature**, not a permission: a strong sector in a bear tape is
  tradeable when its persistence is high enough (19 Nov: AUTO persist 98 while the
  market was short — the day's only +5.7R trade);
* **both sides** are screened on every candle. Each sector is scored on its own
  side, so a short sector produces short setups with mirrored levels;
* at most ``SCREEN_MAX_DAY`` setups a day (6), and **nothing is entered**. The
  human picks. Every setup is tracked to the close so the day's PDF can say what
  each one did.

Evidence the gates are built on (docs/analysis-2026-11-20-*.md, audit-2026-11-17-01.md):
persistence at entry split 17 Nov cleanly — ``>= 80`` four trades, four T1 hits,
+6.2R; ``<= 73`` three trades, no T1, -2.8R — and the score was informative only
when it had *risen* over several candles, noise when born on the open. Every loss
that day was taken with no committed market side. ``structure < 60`` measured
-0.04pp forward with a 6% hit rate; ``lead >= 3pp`` measured -0.08pp. Those four
numbers are the four gates.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import data, telegram, trade
from .session import now_ist, to_ist

log = logging.getLogger("nse_scanner")

BAR_MINUTES = 5
#: The screen's own ledger, anchored to the repository like ``state.STATE_PATH``.
#: Bot 4 never reads or writes bot 1's state — and it cannot share bot 1's loader
#: either, because that loader rejects any file without ``sent``/``positions``.
STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "screen_state.json"


# ── knobs ────────────────────────────────────────────────────────────────────
def _f(name: str, default: float, lo: float = 0.0, hi: float = 1e6) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if lo <= value <= hi else default


def _i(name: str, default: int, lo: int = 0, hi: int = 1000) -> int:
    return int(_f(name, float(default), float(lo), float(hi)))


def screen_enabled() -> bool:
    """``SCREEN_BOT`` (1): run the name-first screen alongside the 5m scan."""
    return os.environ.get("SCREEN_BOT", "1").strip() != "0"


def max_day() -> int:
    """``SCREEN_MAX_DAY`` (6): setups surfaced per session — the user's short list."""
    return _i("SCREEN_MAX_DAY", 6, 1, 20)


def min_score() -> float:
    """``SCREEN_MIN_SCORE`` (55): the floor of the ranking below."""
    return _f("SCREEN_MIN_SCORE", 55, 0, 100)


def persist_min() -> float:
    """``SCREEN_PERSIST_MIN`` (78): the sector's own persistence must be at least this.

    17 Nov split at 80 (>=80: +6.2R, <=73: -2.8R); 78 keeps one candle of slack
    because the split was one day and one sector.
    """
    return _f("SCREEN_PERSIST_MIN", 78, 0, 100)


def slope_min() -> float:
    """``SCREEN_SLOPE_MIN`` (0): persistence change over ``SLOPE_BARS`` candles.

    The score is informative when it has *risen* over several candles and noise
    when it was born on the open (17 Nov: FMCG 73 at 09:25 decayed to 62 in one
    candle). Zero means "not falling"; a positive value demands a rise.
    """
    return _f("SCREEN_SLOPE_MIN", 0, -100, 100)


def slope_bars() -> int:
    """``SCREEN_SLOPE_BARS`` (3): how far back the persistence slope is measured."""
    return _i("SCREEN_SLOPE_BARS", 3, 1, 12)


def structure_min() -> float:
    """``SCREEN_STRUCTURE_MIN`` (60): below this a name is not entering, it is drifting."""
    return _f("SCREEN_STRUCTURE_MIN", 60, 0, 100)


def strength_min() -> float:
    """``SCREEN_STRENGTH_MIN`` (55): the tail of a move has strength 45-64 on this tape."""
    return _f("SCREEN_STRENGTH_MIN", 55, 0, 100)


def lead_max() -> float:
    """``SCREEN_LEAD_MAX`` (2.0pp): beyond this the leader is extended, not leading.

    20 Nov measured ``lead >= 3pp`` at -0.08pp forward and a 9% hit rate; 17 Nov:
    the day's best trade had the *worst* lead. Lead orders names, it does not
    qualify them.
    """
    return _f("SCREEN_LEAD_MAX", 2.0, 0, 100)


def run_max_r() -> float:
    """``SCREEN_RUN_MAX_R`` (5.0): how far a name may have already run, in its own R."""
    return _f("SCREEN_RUN_MAX_R", 5.0, 0.5, 50)


def near_extreme_pct() -> float:
    """``SCREEN_NEAR_HIGH_PCT`` (0.20): how close to its own session extreme counts."""
    return _f("SCREEN_NEAR_HIGH_PCT", 0.20, 0.0, 5.0)


def counter_persist() -> float:
    """``SCREEN_COUNTER_PERSIST`` (90): a setup against the market needs this persistence.

    19 Nov: the pick was a short and AUTO (persist 98, +2.60% day) was never scanned;
    the day's best trade, BAJAJ-AUTO +5.7R, came only after the lock cleared. A
    strong sector is tradeable against a committed tape — but only when it is strong.
    """
    return _f("SCREEN_COUNTER_PERSIST", 90, 0, 100)


def cap_per_sector() -> int:
    """``SCREEN_CAP_PER_SECTOR`` (2): one sector is one risk factor."""
    return _i("SCREEN_CAP_PER_SECTOR", 2, 1, 10)


def cooldown_min() -> int:
    """``SCREEN_COOLDOWN_MIN`` (45): the same name is not re-surfaced inside this."""
    return _i("SCREEN_COOLDOWN_MIN", 45, 1, 240)


def last_entry() -> str:
    """``SCREEN_LAST_ENTRY`` (14:30): a setup after this has no time left to work."""
    return os.environ.get("SCREEN_LAST_ENTRY", "14:30").strip() or "14:30"


def knobs() -> dict:
    """Everything the screen acts on, for the alert footer and the PDF's page 1."""
    return {"SCREEN_MAX_DAY": max_day(), "SCREEN_MIN_SCORE": min_score(),
            "SCREEN_PERSIST_MIN": persist_min(), "SCREEN_SLOPE_MIN": slope_min(),
            "SCREEN_SLOPE_BARS": slope_bars(), "SCREEN_STRUCTURE_MIN": structure_min(),
            "SCREEN_STRENGTH_MIN": strength_min(), "SCREEN_LEAD_MAX": lead_max(),
            "SCREEN_RUN_MAX_R": run_max_r(), "SCREEN_NEAR_HIGH_PCT": near_extreme_pct(),
            "SCREEN_COUNTER_PERSIST": counter_persist(),
            "SCREEN_CAP_PER_SECTOR": cap_per_sector(),
            "SCREEN_COOLDOWN_MIN": cooldown_min(), "SCREEN_LAST_ENTRY": last_entry()}


# ── state ────────────────────────────────────────────────────────────────────
def load(path: Path | None = None) -> dict:
    """Read the screen's ledger. A corrupt file must not stop the scan.

    Bot 1 fails *closed* on a corrupt ledger because re-sending a delivered entry
    is worse than silence. Bot 4 has the opposite duty: its opens nothing, so an
    unreadable file is dropped and the day continues with a fresh one — the loss
    is a missing audit, not a duplicate trade.
    """
    p = Path(path or STATE_PATH)
    if not p.exists():
        return {}
    try:
        data_ = json.loads(p.read_text(encoding="utf-8"))
        return data_ if isinstance(data_, dict) else {}
    except (OSError, ValueError) as exc:
        log.error("screen ledger at %s is unreadable (%s); starting a fresh one", p, exc)
        return {}


def save(st: dict, path: Path | None = None) -> None:
    p = Path(path or STATE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


def fresh(session: str) -> dict:
    """An empty screen ledger for one session."""
    return {"session": session, "setups": [], "scan": [], "sectors": [], "market": [],
            "stats": {"candles": 0, "candidates": 0, "surfaced": 0, "refused": {}},
            "reported": False, "knobs": knobs()}


def reset_if_new_day(st: dict, session: str) -> dict:
    """A new session starts from nothing — the screen keeps no setups overnight.

    **Mutates** ``st`` rather than returning a new dict: the caller holds the same
    object across ``consider`` and ``updates``, and a rebound ledger would leave
    the caller reading yesterday's list (and announcing yesterday's events).
    """
    st = st if isinstance(st, dict) else {}
    if str(st.get("session") or "") != session:
        st.clear()
        st.update(fresh(session))
    st.setdefault("knobs", knobs())
    return st


# ── features ─────────────────────────────────────────────────────────────────
def _session_open(df: pd.DataFrame, last) -> float | None:
    """Today's first traded price on the same session as ``last``."""
    try:
        today = [ts for ts in df.index if to_ist(ts).date() == to_ist(last).date()]
        if not today:
            return None
        first = df.loc[df.index <= today[0]]
        return float(first["Open"].iloc[-1]) if not first.empty else None
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def quote(df: pd.DataFrame, last) -> dict | None:
    """Everything a name's price tape says at the close of ``last`` — **without** EMA9.

    Split out from ``features`` because EMA9 is the expensive part: it re-fills the
    5m grid over seven days (measured: 7 ms per name, 1.25 s for a full board of
    132 names — too much to spend on names that a zero-cost gate is about to
    refuse). The cheap fields answer every numeric gate; the EMA is asked for only
    once, from the plan of a name that already earned one.
    """
    if df is None or getattr(df, "empty", True):
        return None
    try:
        df = df.loc[df.index <= last]
        if df.empty or to_ist(df.index[-1]) != last:
            return None
        close = float(df["Close"].iloc[-1])
        if not close:
            return None
        day = df.loc[[ts for ts in df.index if to_ist(ts).date() == to_ist(last).date()]]
        if day.empty:
            return None
        high, low = float(day["High"].max()), float(day["Low"].min())
        open_px = float(day["Open"].iloc[0])
        back = df["Close"].iloc[-4] if len(df) >= 4 else df["Close"].iloc[0]
        acc15 = (close / float(back) - 1.0) * 100.0 if float(back) else 0.0
        return {"close": close, "high": high, "low": low,
                "open": open_px, "acc15": round(acc15, 3),
                "at_high": close >= high * (1 - near_extreme_pct() / 100.0),
                "at_low": close <= low * (1 + near_extreme_pct() / 100.0)}
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def features(df: pd.DataFrame, last) -> dict | None:
    """``quote`` plus EMA9 — the name's full state on the closed candle ``last``.

    Everything here is readable at the close of ``last`` and nothing later: the
    screen must never see a candle the scan could not have seen.
    """
    feat = quote(df, last)
    if feat is None:
        return None
    feat["ema9"] = trade.ema9_on_bar(df.loc[df.index <= last], last)
    return feat


def _tape_pre_ok(feat: dict, long: bool) -> bool:
    """The two EMA-free halves of ``_side_ok``: the name's own 15 minutes and extreme.

    Checked *before* the plan, because the plan costs an EMA fill (~6 ms) and this
    costs nothing: on a broad day most names are already turning or are no longer at
    their extreme by the time their sector qualifies, and paying for their levels
    would only be paid to throw them away.
    """
    return (feat["acc15"] > 0 and feat["at_high"]) if long else (
        feat["acc15"] < 0 and feat["at_low"])


def _side_ok(feat: dict, long: bool, ema=None) -> bool:
    """The name's own tape agrees with the side it is being screened on."""
    ema = feat.get("ema9") if ema is None else ema
    if ema is None:
        return False
    return (feat["close"] > ema and feat["acc15"] > 0 and feat["at_high"]) if long else (
        feat["close"] < ema and feat["acc15"] < 0 and feat["at_low"])


def _run_r(feat: dict, risk: float, long: bool) -> float:
    """How far the name has already run from today's open, in its own R."""
    if not risk:
        return 0.0
    moved = (feat["close"] - feat["open"]) if long else (feat["open"] - feat["close"])
    return round(moved / risk, 2)


# ── ranking ──────────────────────────────────────────────────────────────────
#: What the ranking weighs, after the gates have already decided *whether*. The
#: weights are a knob-free statement of the evidence: strength is what the trade
#: is about (0.30), the sector's persistence and its slope are the edge that was
#: actually measured (0.20 + 0.15), structure says the move is not exhausted
#: (0.15), volume says it is real (0.10), and a name that has already run a long
#: way is marked down (0.10 penalty) rather than excluded.
WEIGHTS = {"strength": 0.30, "persist": 0.20, "slope": 0.15, "structure": 0.15,
           "volume": 0.10, "run": -0.10, "align": 0.10}


def _clamp(x: float) -> float:
    return 0.0 if x != x else max(0.0, min(1.0, x))


def rank_score(cand: dict) -> float:
    """0-100. Deterministic, documented, and comparable across sectors and sides.

    A 0-100 leg becomes a 0-1 weight **before** it is clamped: clamping first turns
    ``strength 78`` into ``1.0`` and then into 0.01, which scored every good name
    ~31 and surfaced nothing (caught by ``test_a_long_setup_carries_long_levels``).
    """
    def pct(value) -> float:
        return _clamp((value or 0) / 100.0)

    slope = _clamp(0.5 + cand["slope"] / 20.0)          # -10pp .. +10pp maps 0..1
    run = _clamp(cand["run_r"] / max(run_max_r(), 0.1))
    parts = {
        "strength": pct(cand["legs"].get("strength")),
        "persist": pct(cand["persist"]),
        "slope": slope,
        "structure": pct(cand["legs"].get("structure")),
        "volume": pct(cand["legs"].get("volume")),
        "run": run,
        "align": 1.0 if cand["aligned"] else 0.5,
    }
    total = sum(WEIGHTS[k] * parts[k] for k in WEIGHTS)
    return round(max(0.0, min(1.0, total)) * 100.0, 1)


# ── the screen ───────────────────────────────────────────────────────────────
def candidates(payload: dict, frames: dict, last, *, slopes: dict | None = None,
               md=None, report: dict | None = None) -> list[dict]:
    """Every name that passes the gates on this candle, in both directions.

    ``payload`` is the board's candle (``board.sweep``'s return): the per-sector
    persistence and each name's three legs, ranked inside its own sector.  The
    board already scores the whole universe; this function does not re-score it.

    ``report`` collects the count of every refusal, because "0 setups" is not a
    diagnosis and the PDF prints the reasons.
    """
    out: list[dict] = []
    report = report if report is not None else {}
    slopes = slopes or {}
    market_side = int(getattr(md, "dir_state", 0) or 0)
    for sector, cell in ((payload or {}).get("sectors") or {}).items():
        names = cell.get("n") or []
        if not names:
            continue
        side = int(cell.get("side") or 1)
        long = side == 1
        persist = float(cell.get("persist") or 0)
        slope = float(slopes.get(sector, 0.0))
        intra = float(cell.get("intra") or 0)
        if persist < persist_min():
            report["sector persist below floor"] = report.get(
                "sector persist below floor", 0) + len(names)
            continue
        if slope < slope_min():
            report["sector persistence falling"] = report.get(
                "sector persistence falling", 0) + len(names)
            continue
        if (intra > 0) != long:
            report["sector moving against its side"] = report.get(
                "sector moving against its side", 0) + len(names)
            continue
        aligned = market_side == 0 or market_side == side
        if not aligned and persist < counter_persist():
            report["sector against the market"] = report.get(
                "sector against the market", 0) + len(names)
            continue
        legs = {"strength": cell.get("s") or [], "volume": cell.get("v") or [],
                "structure": cell.get("t") or []}
        leads = cell.get("lead") or []
        for i, raw in enumerate(names):
            leg = {k: (v[i] if i < len(v) else None) for k, v in legs.items()}
            lead = leads[i] if i < len(leads) else None
            # The gates run cheapest first, and the count is charged to the first
            # gate a name fails: that is the number that actually decided it.
            feat = quote(frames.get(data.yahoo_stock(raw)), last)
            if feat is None:
                report["no candle for the name"] = report.get("no candle for the name", 0) + 1
                continue
            if (leg.get("structure") or 0) < structure_min():
                report["structure below the floor"] = report.get(
                    "structure below the floor", 0) + 1
                continue
            if (leg.get("strength") or 0) < strength_min():
                report["strength below the floor"] = report.get(
                    "strength below the floor", 0) + 1
                continue
            if lead is None or abs(float(lead)) > lead_max():
                report["lead outside the band"] = report.get("lead outside the band", 0) + 1
                continue
            if not _tape_pre_ok(feat, long):
                report["name not moving its sector's way"] = report.get(
                    "name not moving its sector's way", 0) + 1
                continue
            plan = trade.plan_at("BUY" if long else "SELL", _frame(frames, raw, last), last)
            if plan is None or not plan.risk:
                report["no levels"] = report.get("no levels", 0) + 1
                continue
            # The plan already computed EMA9 for this candle; asking ``features``
            # would fill the 5m grid a second time (~7 ms per name).
            if not _side_ok(feat, long, plan.ema9):
                report["name not moving its sector's way"] = report.get(
                    "name not moving its sector's way", 0) + 1
                continue
            run = _run_r(feat, float(plan.risk), long)
            if run > run_max_r():
                report["already run too far"] = report.get("already run too far", 0) + 1
                continue
            cand = {"raw": raw, "sector": sector, "side": "BUY" if long else "SELL",
                    "long": long, "bar": to_ist(last).strftime("%H:%M"),
                    "close": round(feat["close"], 2), "legs": leg,
                    "lead": round(float(lead), 2) if lead is not None else None,
                    "persist": round(persist), "slope": round(slope, 1),
                    "intra": round(intra, 2), "run_r": run, "aligned": aligned,
                    "plan": plan}
            cand["score"] = rank_score(cand)
            out.append(cand)
    out.sort(key=lambda c: (-c["score"], c["raw"]))
    return out


def _frame(frames: dict, raw: str, last):
    """The name's frame, already trimmed to the candle being judged."""
    df = frames.get(data.yahoo_stock(raw))
    return None if df is None else df.loc[df.index <= last]


def _slopes(st: dict, payload: dict) -> dict:
    """Persistence change per sector over ``SCREEN_SLOPE_BARS`` candles.

    Read from the screen's own sector tape (written every candle), so the slope is
    a property of the day and not of the 5m engine's pick history.
    """
    bars = st.get("sectors") or []
    n = slope_bars()
    if len(bars) <= n:
        return {}
    then = bars[-1 - n].get("sec") or {}
    out = {}
    for sector, cell in (payload.get("sectors") or {}).items():
        now = float(cell.get("persist") or 0)
        was = (then.get(sector) or {}).get("persist")
        out[sector] = round(now - float(was), 1) if was is not None else 0.0
    return out


def _record_sectors(st: dict, payload: dict, margins: dict) -> None:
    row = {"b": payload.get("b"), "sec": {}}
    for sector, cell in (payload.get("sectors") or {}).items():
        row["sec"][sector] = {"persist": round(float(cell.get("persist") or 0)),
                              "side": int(cell.get("side") or 1),
                              "intra": round(float(cell.get("intra") or 0), 2),
                              "day": round(float(cell.get("day") or 0), 2),
                              "gate": bool(cell.get("gate")),
                              "slope": margins.get(sector)}
    book = st.setdefault("sectors", [])
    if book and book[-1].get("b") == row["b"]:
        book[-1] = row
    else:
        book.append(row)
    del book[:-130]


def _surfaced_today(st: dict, raw: str) -> bool:
    return any(s.get("raw") == raw for s in (st.get("setups") or []))


def _cooldown_ok(st: dict, raw: str, bar) -> bool:
    """No name twice inside ``SCREEN_COOLDOWN_MIN`` (and never twice a day)."""
    return not _surfaced_today(st, raw)


def _sector_room(st: dict, sector: str) -> bool:
    used = sum(1 for s in (st.get("setups") or []) if s.get("sector") == sector)
    return used < cap_per_sector()


def _make_setup(st: dict, cand: dict, md) -> dict:
    plan = cand["plan"]
    long = cand["long"]
    risk_pct = (plan.risk / plan.entry * 100.0) if plan.entry else 0.0
    why = [f"sector {cand['sector']} persist {cand['persist']}"
           + (f" ({cand['slope']:+.0f} over the last {slope_bars()})" if cand["slope"] else ""),
           f"name at its {'high' if long else 'low'} of the day, "
           f"15-min {cand['legs'].get('strength') or 0:.0f} strength / "
           f"{cand['legs'].get('structure') or 0:.0f} structure",
           f"lead {cand['lead']:+.1f}pp vs its sector · run {cand['run_r']:.1f}R",
           f"market {getattr(md, 'dir_label', '?')} {getattr(md, 'dir_conf', '?')}%"
           + ("" if cand["aligned"] else " — taken against the tape on a persist "
              f"of {cand['persist']}")]
    return {"id": len(st.get("setups") or []) + 1, "bar": cand["bar"], "raw": cand["raw"],
            "name": cand["raw"], "sector": cand["sector"], "side": cand["side"],
            "entry": plan.entry, "sl": plan.sl, "risk": plan.risk,
            "risk_pct": round(risk_pct, 2), "t1": plan.t1, "t2": plan.t2, "t3": plan.t3,
            "t_names": list(plan.pivot_names), "score": cand["score"],
            "legs": cand["legs"], "lead": cand["lead"], "persist": cand["persist"],
            "slope": cand["slope"], "run_r": cand["run_r"], "aligned": cand["aligned"],
            "why": why, "status": "surfaced", "mfe_r": None, "mae_r": None,
            "hit_1r": None, "stopped": None, "eod_r": None, "mark": None,
            "scanned_at": None}


def consider(st: dict, payload: dict, frames: dict, md, last: datetime, *,
             send=None, save_state=None, now: datetime | None = None) -> list[dict]:
    """Screen one closed candle and surface what qualifies. Never raises.

    Called from the 5m scan right after the board sweep, with the same frames the
    board used — no second fetch, and the screen cannot see a candle the scan has
    not closed. ``send`` defaults to bot 4; nothing is ever entered.
    """
    if not screen_enabled() or not payload:
        return []
    send = send or telegram.send_bot4
    save_state = save_state or save
    now = now or now_ist()
    hhmm = str(payload.get("b") or to_ist(last).strftime("%H:%M"))
    try:
        st = reset_if_new_day(st, str(to_ist(last).date()))
        margins = _slopes(st, payload)
        report: dict = {}
        found = candidates(payload, frames, last, slopes=margins, md=md, report=report)
        _record_sectors(st, payload, margins)
        stats = st.setdefault("stats", {})
        stats["candles"] = stats.get("candles", 0) + 1
        stats["candidates"] = stats.get("candidates", 0) + len(found)
        refused = stats.setdefault("refused", {})
        for k, v in report.items():
            refused[k] = refused.get(k, 0) + v
        row = {"b": hhmm, "cands": len(found), "best": None, "top": [],
               "counts": dict(report)}
        if found:
            row["best"] = found[0]["score"]
            row["top"] = [{"raw": c["raw"], "sector": c["sector"], "side": c["side"],
                           "score": c["score"], "run_r": c["run_r"]} for c in found[:4]]
        scan = st.setdefault("scan", [])
        if scan and scan[-1].get("b") == hhmm:
            scan[-1] = row
        else:
            scan.append(row)
        del scan[:-130]
        st.setdefault("market", []).append(
            {"b": hhmm, "label": getattr(md, "dir_label", ""),
             "conf": getattr(md, "dir_conf", None),
             "side": getattr(md, "dir_state", None),
             "regime": getattr(md, "regime", "")})
        del st["market"][:-130]
        track(st, frames, last)
        surfaced = _surface(st, found, md, hhmm, send=send, now=now)
        save_state(st)
        return surfaced
    except Exception:                       # noqa: BLE001 — never cost a candle
        log.exception("screen failed at %s", hhmm)
        return []


def _surface(st: dict, found: list[dict], md, hhmm: str, *, send, now) -> list[dict]:
    """Take the best ``SCREEN_MAX_DAY`` of them, one alert each. Nothing is entered."""
    out: list[dict] = []
    if hhmm > last_entry():
        return out
    for cand in found:
        if len(st.get("setups") or []) >= max_day():
            break
        if cand["score"] < min_score():
            continue
        if _surfaced_today(st, cand["raw"]) or not _cooldown_ok(st, cand["raw"], hhmm):
            continue
        if not _sector_room(st, cand["sector"]):
            continue
        setup = _make_setup(st, cand, md)
        if not send(format_setup(setup, md), dry_run=_dry()):
            log.warning("bot 4 refused the setup for %s; leaving it unsent", cand["raw"])
            continue
        setup["scanned_at"] = now.strftime("%H:%M:%S")
        st.setdefault("setups", []).append(setup)
        st["stats"]["surfaced"] = st["stats"].get("surfaced", 0) + 1
        out.append(setup)
        log.info("SETUP #%d %s %s %s @ %s sl=%.2f (%.2f%%) score %.1f",
                 setup["id"], setup["side"], setup["sector"], setup["raw"],
                 setup["entry"], setup["sl"], setup["risk_pct"], setup["score"])
    return out


def _dry() -> bool:
    return os.environ.get("DRY_RUN", "0") == "1"


# ── tracking: what each surfaced setup did ───────────────────────────────────
def _since(df: pd.DataFrame, hhmm: str):
    """The candles of the setup's own session at or after the bar that surfaced it.

    ``features`` reports the *day's* high and low, which is right for screening and
    wrong for measuring: a setup surfaced at 11:00 would otherwise be credited with
    the 09:30 range it never traded through. Everything here starts at its own bar.
    """
    if df is None or getattr(df, "empty", True):
        return None
    try:
        day = to_ist(df.index[-1]).date()
        rows = df.loc[[ts for ts in df.index
                       if to_ist(ts).date() == day and to_ist(ts).strftime("%H:%M") >= hhmm]]
        return rows if not rows.empty else None
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def track(st: dict, frames: dict, last) -> None:
    """Mark every surfaced setup against the candles it has lived through.

    A setup is the user's decision, not a position, so nothing here is money: it
    is the measurement that lets the EOD PDF say whether the short list was right.
    ``+1R before the stop`` is the definition of a winner used throughout, and the
    stop wins when one candle touches both (intrabar order is unknowable here).
    """
    for setup in st.get("setups") or []:
        if setup.get("eod_r") is not None and setup.get("stopped"):
            continue
        df = _frame(frames, setup["raw"], last)
        window = _since(df, str(setup.get("bar") or "09:15"))
        if window is None or not len(window):
            continue
        entry, risk = float(setup["entry"]), float(setup["risk"])
        long = setup["side"] == "BUY"
        if not risk:
            continue
        high, low = float(window["High"].max()), float(window["Low"].min())
        fav = (high - entry) if long else (entry - low)
        adv = (low - entry) if long else (entry - high)
        setup["mfe_r"] = round(max(fav / risk, setup.get("mfe_r") or 0.0), 2)
        setup["mae_r"] = round(min(adv / risk, setup.get("mae_r") or 0.0), 2)
        close = float(window["Close"].iloc[-1])
        setup["mark"] = round(close, 2)
        setup["eod_r"] = round(((close - entry) if long else (entry - close)) / risk, 2)
        # Intrabar order is unknown; when a candle touches both, the stop wins.
        stopped = (low <= float(setup["sl"])) if long else (high >= float(setup["sl"]))
        one_r = (high >= entry + risk) if long else (low <= entry - risk)
        setup["status"] = ("stopped" if setup.get("stopped") else
                           "worked" if setup.get("hit_1r") else "open")
        if setup.get("hit_1r") is None and setup.get("stopped") is None:
            if stopped:
                setup["stopped"] = setup.get("bar")
                setup["status"] = "stopped"
            elif one_r:
                setup["hit_1r"] = setup.get("bar")
                setup["status"] = "worked"


def summary(st: dict) -> dict:
    """The short list's own scorecard, for the EOD text and the PDF's page 2."""
    setups = st.get("setups") or []
    done = [s for s in setups if s.get("mfe_r") is not None]
    worked = [s for s in done if s.get("hit_1r")]
    stopped = [s for s in done if s.get("stopped")]
    return {"setups": len(setups), "tracked": len(done), "worked": len(worked),
            "stopped": len(stopped), "precision": (round(100.0 * len(worked) / len(done), 1)
                                                   if done else None),
            "avg_mfe_r": (round(sum(s["mfe_r"] for s in done) / len(done), 2)
                          if done else None),
            "avg_eod_r": (round(sum(s["eod_r"] for s in done if s.get("eod_r") is not None)
                                / max(1, len([s for s in done if s.get("eod_r") is not None])), 2)
                          if done else None)}


# ── messages ─────────────────────────────────────────────────────────────────
def _tgt(plan_name, value) -> str:
    return f"{plan_name} {value:.2f}" if value is not None else "—"


def format_setup(setup: dict, md) -> str:
    """The setup card. Says what it is, what it costs and that it is not an entry."""
    arrow = "🟢" if setup["side"] == "BUY" else "🔴"
    legs = setup.get("legs") or {}
    return (
        f"🎯 {arrow} <b>SETUP #{setup['id']} — {setup['side']}</b>  |  "
        f"<b>{setup['name']}</b>  {setup['sector']}  ·  score <b>{setup['score']:.1f}</b>\n"
        f"5m bar {setup['bar']} closed  ·  Market {getattr(md, 'dir_label', '?')} "
        f"({getattr(md, 'dir_conf', '?')}%)\n"
        f"Entry <b>{setup['entry']:.2f}</b> (this candle's close) · SL {setup['sl']:.2f} "
        f"({setup['risk_pct']:.2f}% risk) · {_tgt('T1', setup.get('t1'))} · "
        f"{_tgt('T2', setup.get('t2'))} · {_tgt('T3', setup.get('t3'))}\n"
        f"sector persist {setup['persist']} ({setup['slope']:+.0f}) · lead "
        f"{setup['lead']:+.1f}pp · strength {legs.get('strength') or 0:.0f} / volume "
        f"{legs.get('volume') or 0:.0f} / structure {legs.get('structure') or 0:.0f} · "
        f"run {setup['run_r']:.1f}R\n"
        + "\n".join(f"• {w}" for w in setup.get("why") or [])
        + "\n<b>Not an entry alert</b> — the screen surfaces, you decide. "
          f"Levels are the {setup['bar']} candle's own; re-check the price before acting."
    )


def format_update(setup: dict, kind: str) -> str:
    """One line when a surfaced setup reaches +1R or breaks its stop."""
    arrow = "🎯" if kind == "worked" else "🛑"
    what = "+1R reached" if kind == "worked" else "stop traded"
    return (f"{arrow} <b>SETUP #{setup['id']} {what}</b> — {setup['side']} "
            f"<b>{setup['name']}</b> {setup['sector']} · entry {setup['entry']:.2f} "
            f"· SL {setup['sl']:.2f} · at {setup.get('bar')} "
            f"· MFE {setup.get('mfe_r')}R / MAE {setup.get('mae_r')}R")


def updates(st: dict) -> list[str]:
    """Texts for setups that changed state since the last check (sent by the loop)."""
    out = []
    for setup in st.get("setups") or []:
        done = setup.get("hit_1r") or setup.get("stopped")
        if done and not setup.get("announced"):
            setup["announced"] = True
            out.append(format_update(setup, "worked" if setup.get("hit_1r") else "stopped"))
    return out
