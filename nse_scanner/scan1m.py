"""1-minute projected scan — Pine's "1m ➜ 5m projection engine", Telegram bot #3.

The indicator this repo ports can run on a **1-minute chart**: the forming 5m
candle is rebuilt from the 1m prints as they arrive, so a signal appears up to
four minutes before that candle closes. Until now the live scan only looked at
the *completed* 5m candle, plus one labelled heads-up 60 s before the close
(``pipeline.run_early``).

This module adds the missing mode as its own scan, on its own bot:

* every minute of a forming 5m candle, ``data.provisional_5m`` rebuilds that
  candle from the 1m prints seen so far;
* the **unchanged** 5m engine ``stock.scan_stock`` runs on
  ``[completed 5m bars] + [projected bar]`` — same 22 conditions, same 12 BUY +
  19 SELL exception codes (plus NORMAL/ENTRY), same windows/locks/gap/wick/
  liquidity/score gates, same market pick, same side, same EMA9 stop and pivot
  targets. Nothing about the rules is forked
  here; only *when* the alert is emitted;
* at the candle's close the completed candle is evaluated again and the
  **confirmed** entry is sent to the same bot. A projection that does not
  survive the close is dropped silently (``SCAN1M_CANCEL_MESSAGE=1`` to report
  it) — the confirmed stream stays clean.

The 5m scan on bots 1/2 is untouched: this module never calls
``telegram.send``, only ``telegram.send_bot3``.

Configuration (environment):

===========================  ====================================================
``TELEGRAM_BOT_TOKEN_3``     bot 3 token (required for delivery)
``TELEGRAM_CHAT_ID_3``       bot 3 chat ID (required for delivery)
``SCAN1M_FIRST_MINUTE``      first minute of the 5m bar that may alert (1..4, 1)
``SCAN1M_MAX_STALE_SECONDS``  skip a stock whose newest 1m print is older (100)
``SCAN1M_GRACE_SECONDS``     seconds after a minute boundary before acting (6)
``SCAN1M_EARLY_PER_DAY``     projected alerts per stock per session (1)
``SCAN1M_MARKET_SOURCE``     ``ledger`` | ``own`` | ``auto`` (auto)
``SCAN1M_CANCEL_MESSAGE``    ``1`` also report a projection that failed (0)
``SCAN1M_SUMMARY``           ``1`` send a session summary at 15:33 (1)
===========================  ====================================================
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, time as clock_time, timedelta
from types import SimpleNamespace

import pandas as pd

from . import data, state, telegram
from .market import run_market, SECTOR_ORDER
from .oi_filter import should_take_entry
from .session import IST, now_ist, to_ist
from .stock import scan_stock
from .trade import build_plan

log = logging.getLogger("nse_scanner.scan1m")

# Its own ledger: the 1m scan's dedupe must never suppress (or be suppressed by)
# the 5m scan's. The 5m ledger is only ever read, never written, by this module.
LEDGER = state.ROOT / "state" / "scan1m_state.json"

BAR_MINUTES = 5

# bar label -> market pick, so a 5m bar is resolved once instead of per minute.
_pick_cache: dict[str, tuple[float, dict | None]] = {}
_PICK_RETRY = 20.0          # negative cache, seconds


# ── configuration ────────────────────────────────────────────────────────────
def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _dry() -> bool:
    return os.environ.get("DRY_RUN", "0") == "1"


def first_minute() -> int:
    """Earliest minute of the 5m candle that may alert (1 → up to 4 min early)."""
    return _env_int("SCAN1M_FIRST_MINUTE", 1, 1, 4)


def max_stale() -> float:
    return _env_float("SCAN1M_MAX_STALE_SECONDS", 100.0)


def grace() -> float:
    return _env_float("SCAN1M_GRACE_SECONDS", 6.0)


def early_per_day() -> int:
    return _env_int("SCAN1M_EARLY_PER_DAY", 1, 0, 20)


def market_source() -> str:
    value = os.environ.get("SCAN1M_MARKET_SOURCE", "auto").strip().lower()
    return value if value in ("ledger", "own", "auto") else "auto"


def cancel_message() -> bool:
    return os.environ.get("SCAN1M_CANCEL_MESSAGE", "0") == "1"


def want_summary() -> bool:
    return os.environ.get("SCAN1M_SUMMARY", "1") != "0"


# ── ledger ───────────────────────────────────────────────────────────────────
def ledger_path():
    if _dry():
        return LEDGER.with_name(LEDGER.stem + ".dry.json")
    return LEDGER


def load_ledger() -> dict:
    path = ledger_path()
    if not path.exists():
        return state.reset_if_new_day({}, str(now_ist().date()))
    return state.reset_if_new_day(state.load(path), str(now_ist().date()))


def save_ledger(st: dict) -> None:
    state.save(st, ledger_path())


_ledger_warned: set[str] = set()


def _warn_ledger_once(bar: datetime) -> None:
    """Say *why* ``ledger`` mode produced nothing — once per candle, not per minute."""
    key = f"{bar:%Y-%m-%d %H:%M}"
    if key in _ledger_warned:
        return
    _ledger_warned.add(key)
    path = state.STATE_PATH
    if not path.exists():
        log.warning(
            "SCAN1M_MARKET_SOURCE=ledger, but %s does not exist on this machine — the 5m scan "
            "writes it on its own runner, so there is no pick to reuse and nothing is projected. "
            "Use SCAN1M_MARKET_SOURCE=own (or auto) here.", path.name)
        return
    log.warning(
        "SCAN1M_MARKET_SOURCE=ledger: %s has no usable pick for %s (last 5m decision: %s) — "
        "nothing projected on this candle", path.name, key,
        (_five_minute_scan() or {}).get("bar") or "none")


def _five_minute_scan() -> dict:
    """The 5m scan's last decision (read-only) — same sector, same side.

    Reusing it guarantees both scans alert on the same sector and the same
    direction; it is only read, so the 5m ledger is never affected.
    """
    try:
        return (state.load(state.STATE_PATH) or {}).get("scan") or {}
    except Exception as exc:            # a corrupt 5m ledger must not stop bot 3
        log.warning("5m ledger unreadable (%s); deciding the market here", type(exc).__name__)
        return {}


# ── market / sector pick ─────────────────────────────────────────────────────
def _own_pick(decision_bar: datetime) -> dict | None:
    """Compute the pick the same way the 5m scan does, on the decision bar."""
    uni = data.load_universe()
    period_5m = os.environ.get("YF_PERIOD_5M", "7d")
    period_1d = os.environ.get("YF_PERIOD_1D", "90d")
    index_map = {name: uni["sectors"][name]["index"] for name in SECTOR_ORDER
                 if name in uni["sectors"]}
    pack = data.fetch_symbol(uni["nifty"], period_5m, period_1d)
    nifty_5 = pack.get("m5")
    if nifty_5 is None or nifty_5.empty:
        return None
    nifty_5 = nifty_5.loc[nifty_5.index <= decision_bar]
    if nifty_5.empty or to_ist(nifty_5.index[-1]) != decision_bar:
        return None
    idx_tickers = [uni["vix"]] + list(index_map.values())
    m5 = data.fetch_many(idx_tickers, period_5m, "5m")
    d1 = data.fetch_daily(idx_tickers, period_1d)
    m5 = {sym: (df.loc[df.index <= decision_bar] if df is not None and not df.empty else df)
          for sym, df in m5.items()}
    d1[uni["nifty"]] = pack.get("d1")
    md = run_market(
        nifty_5,
        d1.get(uni["nifty"]),
        m5.get(uni["vix"]),
        d1.get(uni["vix"]),
        {name: m5.get(sym) for name, sym in index_map.items()},
        {name: d1.get(sym) for name, sym in index_map.items()},
    )
    if md.bar_time != decision_bar.strftime("%Y-%m-%d %H:%M"):
        return None
    scanning = md.pick_sector != "WAIT" and md.action != "WAIT" and md.pick_side in (1, -1)
    return {
        "bar": md.bar_time,
        "sector": md.pick_sector if scanning else "WAIT",
        "side": md.pick_side if scanning else 0,
        "label": md.dir_label,
        "conf": md.dir_conf,
        "extra": list(getattr(md, "extra_sectors", None) or ()) if scanning else [],
        "follow": bool(scanning and str(getattr(md, "pick_reason", "")).startswith("SECTOR FOLLOW")),
    }


def pick_for(bar: datetime, *, projection: bool) -> dict | None:
    """Sector pick for the candle ``bar``.

    ``projection=True``: judging the still-forming candle ``bar``, so only the
    pick of the **last completed** candle (``bar - 5m``) is valid — a pick is a
    persistence measure and is re-validated at that candle's close.
    ``projection=False``: ``bar`` itself has just closed, so its own pick (or,
    while the 5m scan is still finishing, the previous one) is accepted.
    """
    if projection:
        allowed = [(bar - timedelta(minutes=BAR_MINUTES)).strftime("%Y-%m-%d %H:%M")]
        decision = bar - timedelta(minutes=BAR_MINUTES)
    else:
        allowed = [bar.strftime("%Y-%m-%d %H:%M"),
                   (bar - timedelta(minutes=BAR_MINUTES)).strftime("%Y-%m-%d %H:%M")]
        decision = bar

    key = f"{decision:%Y-%m-%d %H:%M}|{projection}"
    hit = _pick_cache.get(key)
    if hit:
        cached = hit[1]
        usable = cached is not None and cached.get("sector") not in (None, "WAIT")
        if usable or time.time() - hit[0] < _PICK_RETRY:
            return cached

    found: dict | None = None
    source = market_source()
    if source in ("ledger", "auto"):
        scan = _five_minute_scan()
        if scan.get("bar") in allowed and scan.get("sector"):
            found = {
                "bar": scan.get("bar"),
                "sector": scan["sector"],
                "side": int(scan["side"]) if scan.get("side") in (1, -1) else 0,
                "label": scan.get("label") or "",
                "conf": scan.get("conf") or 0,
                "extra": list(scan.get("extra") or []),
                "follow": bool(scan.get("follow")),
            }
    if found is None and source == "ledger":
        _warn_ledger_once(decision)          # silent failure is the worst kind
    if found is None and source in ("own", "auto"):
        try:
            found = _own_pick(decision)
        except Exception:
            log.exception("market pick failed for %s", decision.strftime("%H:%M"))
            found = None
    _pick_cache[key] = (time.time(), found)
    return found


def _scan_list(uni: dict, pick: dict) -> dict[str, str]:
    """``{stock: sector}`` — the 5m scan's own function (pick first, then follows)."""
    from .pipeline import _scan_list as scan_list

    return scan_list(uni, pick["sector"], pick.get("extra"))


# ── frames ───────────────────────────────────────────────────────────────────
_five_cache: dict[tuple[str, str], dict] = {}


def five_minute_history(tickers: dict[str, str], bar: datetime) -> dict[str, pd.DataFrame]:
    """5m history for a forming candle — fixed within it, so fetched once.

    One candle only is kept: reusing the previous candle's frame would drop the
    bar that has just closed and run the engine on stale history.
    """
    key = (",".join(sorted(tickers.values())), bar.isoformat())
    if key not in _five_cache:
        for k in [k for k in _five_cache if k[1] != bar.isoformat()]:
            _five_cache.pop(k, None)
        _five_cache[key] = data.fetch_many(tickers.values(),
                                           os.environ.get("YF_PERIOD_5M", "7d"), "5m")
    return _five_cache[key]


_one_cache: dict[tuple[str, str], dict] = {}


def one_minute_frames(tickers: dict[str, str], bar: datetime) -> dict[str, pd.DataFrame]:
    """1m bars for this minute: the session's history cache + the live prints.

    The 5m scan repairs Yahoo's zero-volume 5m bars from 1m across the **whole**
    history (``data.apply_1m_volume``), which feeds SMA20(volume) → relVol →
    cond08 and the volume score. To arrive at the same numbers here, prior days
    come from one 7d fetch per session (they can no longer change) and only
    today's prints are downloaded again every minute.
    """
    names = ",".join(sorted(tickers.values()))
    today = bar.strftime("%Y-%m-%d")
    key = (names, today)
    hist = _one_cache.get(key)
    if hist is None:
        for k in [k for k in _one_cache if k[1] != today]:
            _one_cache.pop(k, None)
        hist = data.fetch_many(tickers.values(), "7d", "1m")
        _one_cache[key] = hist
    fresh = data.fetch_many(tickers.values(), os.environ.get("SCAN1M_1M_PERIOD", "1d"), "1m")
    out: dict[str, pd.DataFrame] = {}
    for sym in tickers.values():
        old, live = hist.get(sym), fresh.get(sym)
        if old is None or old.empty:
            out[sym] = live
            continue
        keep = old.loc[[str(d) < today for d in old.index.date]]      # prior sessions only
        if live is None or live.empty:
            out[sym] = keep if len(keep) else old
            continue
        merged = pd.concat([keep, live])
        out[sym] = merged[~merged.index.duplicated(keep="last")].sort_index()
    return out


def _bar_open(ts: datetime) -> datetime:
    ts = ts.astimezone(IST)
    return ts.replace(minute=(ts.minute // BAR_MINUTES) * BAR_MINUTES, second=0, microsecond=0)


def _in_session(bar: datetime) -> bool:
    return clock_time(9, 15) <= bar.time() <= clock_time(15, 20)


def projected_frame(df5: pd.DataFrame | None, df1m: pd.DataFrame | None,
                    bar: datetime, now: datetime) -> pd.DataFrame | None:
    """Completed 5m bars + the still-forming ``bar`` rebuilt from 1m prints."""
    if df5 is None or df5.empty or df1m is None or df1m.empty:
        return None
    frame = data.provisional_5m(df5, df1m, bar, now, max_stale())
    if frame is None:
        return None
    return data.apply_1m_volume(frame, df1m)


def closed_frame(df5: pd.DataFrame | None, df1m: pd.DataFrame | None,
                 bar: datetime) -> pd.DataFrame | None:
    """The candle ``bar`` as it closed — or ``None`` while the feed is behind.

    Yahoo publishes a 5m row while its candle is still forming, so "the row is
    there" does not prove it is complete (the 5m scan waits for the same thing,
    ``pipeline._unsettled``). The candle is judged only once the 1m feed shows a
    print inside its last minute; the 5m row is then preferred — the exact input
    the 5m scan uses — and the 1m prints are the fallback when it is missing.
    """
    if df5 is None or df5.empty:
        return None
    end = bar + timedelta(minutes=BAR_MINUTES)
    settled = False
    if df1m is not None and not df1m.empty:
        inside = df1m.loc[(df1m.index >= bar) & (df1m.index < end)]
        if len(inside):
            settled = to_ist(inside.index[-1]) >= end - timedelta(minutes=1)
            if not settled:
                return None                      # the feed has not reached the close
    upto = df5.loc[df5.index <= bar]
    if settled and len(upto) and to_ist(upto.index[-1]) == bar:
        last = upto.iloc[-1]
        if last["Close"] == last["Close"] and float(last.get("Volume") or 0) > 0:
            return data.apply_1m_volume(upto, df1m)
    if df1m is None or df1m.empty:
        return None
    rebuilt = data.provisional_5m(df5, df1m, bar, end, max_stale=10 ** 9)
    if rebuilt is None:
        return None
    return data.apply_1m_volume(rebuilt, df1m)


def _note_reject(st: dict, kind: str, raw: str, detail: str, *, per_bar: str | None = None) -> None:
    """Remember a signal that did not become an entry (same book as the 5m scan)."""
    book = st.setdefault("stats", {}).setdefault("rejected", {}).setdefault(kind, {})
    book.setdefault(f"{raw}@{per_bar}" if per_bar else raw, detail)


# ── formatting ───────────────────────────────────────────────────────────────
def _md(scan: dict, sector: str) -> SimpleNamespace:
    """The attributes ``pipeline.format_stock`` reads, from a scan dict."""
    reason = "SECTOR FOLLOW" if scan.get("follow") else ""
    return SimpleNamespace(dir_label=scan.get("label") or "", dir_conf=scan.get("conf") or 0,
                           pick_sector=sector, pick_reason=reason)


def _targets(plan) -> str:
    from .pipeline import _target_text      # same rendering as the 5m alerts

    return _target_text(plan)


def format_projected(sig, scan: dict, plan, bar: datetime, now: datetime, *, minute: int) -> str:
    arrow = "🟢" if sig.side == "BUY" else "🔴"
    close = bar + timedelta(minutes=BAR_MINUTES)
    left = max(0, int((close - now).total_seconds()))
    return (
        f"⚡ {arrow} <b>EARLY {sig.side}</b> (1m projection) | <b>{sig.name}</b>  {sig.symbol}\n"
        f"Sector <b>{scan.get('sector')}</b>  |  Market {scan.get('label')} ({scan.get('conf')}%)\n"
        f"Price now <b>{plan.entry:.2f}</b>  ·  5m bar {bar:%H:%M}–{close:%H:%M} still forming\n"
        f"<b>{left // 60}m {left % 60:02d}s left</b> — projected from 1m prints up to {now:%H:%M}\n"
        f"SL ≈ {plan.sl:.2f} (EMA9 so far)  |  Risk ≈ {plan.risk:.2f}\n"
        f"next pivot target: {_targets(plan)}\n"
        f"Score {sig.score} {sig.strength}   {sig.reason}\n"
        f"Rules: identical to the 5m scan · judged on minute {minute}/4 of the candle\n"
        f"Confirmed entry follows at the {close:%H:%M} close if the candle holds"
    )


def format_confirmed(sig, scan: dict, plan, *, early: dict | None, sector: str,
                     oi_line: str = "") -> str:
    from .pipeline import format_stock    # the exact 5m entry text

    text = format_stock(sig, _md(scan, scan.get("sector")), plan, sector=sector,
                        oi_line=oi_line)
    if early:
        diff = (plan.entry - early["price"]) if early["side"] == "BUY" else (early["price"] - plan.entry)
        pct = diff / early["price"] * 100.0 if early["price"] else 0.0
        head = (f"✅ <b>CONFIRMED</b> — projected {early['at']} @ {early['price']:.2f}"
                f" (close {pct:+.2f}% {'beyond' if diff >= 0 else 'back from'} it)\n")
    else:
        head = "✅ <b>CONFIRMED at close</b> (no projected alert for this candle)\n"
    return head + text


def format_summary(st: dict) -> str:
    from .pipeline import _summary_rejects, _summary_sectors

    stats = st.get("stats") or {}
    lines = [f"📊 <b>1m projected scan {st.get('session', '')}</b>",
             f"Candles confirmed {stats.get('candles', 0)} · with a sector pick {stats.get('picks', 0)}",
             _summary_sectors(stats),
             f"Projected alerts {stats.get('early', 0)} · of which confirmed at the close "
             f"{stats.get('confirmed', 0)} · did not hold {stats.get('cancelled', 0)}",
             f"Entries (close-confirmed, sent to this bot) {stats.get('confirmed', 0)}",
             _summary_rejects(st)]
    if stats.get("feed_behind"):
        lines.append(f"⚠ the 1m feed lagged past the close on {stats['feed_behind']} candle(s)")
    lines.append(f"Projected alerts fire {first_minute()}–4 minutes before the 5m close.")
    return "\n".join(line for line in lines if line)


# ── one minute of the projection ─────────────────────────────────────────────
def _early_allowed(prev: dict | None, session: str, bar: datetime) -> bool:
    """One projection per candle; ``SCAN1M_EARLY_PER_DAY`` caps the session.

    A projection that did not survive its close may be retried on a **later**
    candle of the same session, because the engine's own state moved on.
    """
    if not prev:
        return True
    if prev.get("session") != session:
        return True
    if prev.get("bar") == bar.isoformat():
        return False
    if int(prev.get("n") or 1) >= max(1, early_per_day()):
        return False
    return prev.get("result") == "cancelled"


def run_minute(bar: datetime | None = None, *, now: datetime | None = None) -> list[dict]:
    """Evaluate the forming candle ``bar`` from the 1m prints and alert bot 3.

    Returns the projected alerts sent (for the CLI and the tests). Nothing is
    written to ``sent``: only a close-confirmed entry becomes an entry.
    """
    now = now or now_ist()
    bar = _bar_open(bar or now)
    if bar.weekday() >= 5 or not _in_session(bar):
        return []
    minute = int((now - bar).total_seconds() // 60)
    if minute < first_minute() or minute > BAR_MINUTES - 1:
        log.debug("minute %s outside the projection window for %s", minute, bar)
        return []

    pick = pick_for(bar, projection=True)
    if not pick or pick.get("sector") in (None, "WAIT") or pick.get("side") not in (1, -1):
        log.info("1m %s: no current sector pick — nothing projected", bar.strftime("%H:%M"))
        return []
    session = str(bar.date())
    want = "BUY" if pick["side"] == 1 else "SELL"
    st = load_ledger()
    uni = data.load_universe()
    sector_of = _scan_list(uni, pick)
    if not sector_of:
        return []

    book = st.setdefault("early", {})
    sent = st.setdefault("sent", {})
    positions = st.setdefault("positions", {})
    names = [raw for raw in sector_of
             if raw not in sent and raw not in positions
             and _early_allowed(book.get(raw), session, bar)]
    if not names:
        return []

    tickers = {raw: data.yahoo_stock(raw) for raw in names}
    sm5 = five_minute_history(tickers, bar)
    sm1 = one_minute_frames(tickers, bar)
    sd1 = data.fetch_daily(tickers.values(), os.environ.get("YF_PERIOD_1D", "90d"))
    oi_reader = _oi_reader(session)

    hhmm = bar.strftime("%H:%M")
    out: list[dict] = []
    for raw, ysym in tickers.items():
        m1 = sm1.get(ysym)
        frame = projected_frame(sm5.get(ysym), m1, bar, now)
        if frame is None:
            continue
        try:
            fires = scan_stock(raw, frame, sd1.get(ysym), keep_all=True)
        except Exception:
            log.exception("1m projection failed for %s", raw)
            continue
        fresh = [s for s in fires if s.time == hhmm and s.side == want]
        if not fresh:
            continue
        best = fresh[-1]
        plan = build_plan(best, frame, bar, sd1.get(ysym))
        if plan is None:
            log.info("1m %s %s: no valid EMA9 stop yet — not projected", raw, best.name)
            continue
        reading = oi_reader.snapshot(raw, bar) if oi_reader else None
        if oi_reader and _oi_mode() == "require" and not should_take_entry(best.side, reading):
            continue
        stamp = now                     # the caller's clock (live loop → now_ist())
        if stamp >= bar + timedelta(minutes=BAR_MINUTES) - timedelta(seconds=2):
            log.info("1m projection for %s finished at the close; leaving it to the confirm pass", hhmm)
            continue
        best.price, best.sl = plan.entry, plan.sl
        text = format_projected(best, pick, plan, bar, stamp, minute=minute)
        if not telegram.send_bot3(text, dry_run=_dry()):
            continue
        prev = book.get(raw) or {}
        book[raw] = {"bar": bar.isoformat(), "session": session, "side": best.side,
                     "code": best.code, "name": best.name, "price": plan.entry,
                     "minute": minute, "at": stamp.strftime("%H:%M:%S"), "result": None,
                     "n": (int(prev["n"]) + 1) if prev.get("session") == session
                          and prev.get("n") else 1}
        stats = st.setdefault("stats", {})
        stats["early"] = stats.get("early", 0) + 1
        save_ledger(st)
        log.info("1m EARLY %s %s %s @ %.2f (minute %d, %.2f)", raw, best.side, best.name,
                 plan.entry, minute, stamp.timestamp())
        out.append({**best.__dict__, "early": True, "minute": minute})
    return out


# ── the close: confirm (silent on failure by request) ────────────────────────
def confirm_pass(bar: datetime | None = None, *, now: datetime | None = None) -> list[dict]:
    """Judge the closed candle ``bar`` exactly as the 5m scan judges it.

    Same list of names, same engine call, same side filter, same EMA9 stop, same
    OI gate, same "one entry per stock per session", same stale/late rejections
    and the same entry text — the only difference is the bot it is sent to.
    Projections of this candle that do not survive are resolved silently.
    """
    now = now or now_ist()
    bar = _bar_open(bar or (now - timedelta(minutes=BAR_MINUTES)))
    if bar.weekday() >= 5 or not _in_session(bar):
        return []
    if not _within_lag(bar, now):
        log.warning("1m %s close is %.0fs old — past the alert deadline; nothing confirmed",
                    bar.strftime("%H:%M"), (now - (bar + timedelta(minutes=BAR_MINUTES))).total_seconds())
        return []

    st = load_ledger()
    pick = pick_for(bar, projection=False)
    if not pick:
        # The pick for this candle is not published yet: leave its projections
        # unresolved so the next minute retries instead of writing them off.
        log.info("1m %s close: pick not available yet — will retry", bar.strftime("%H:%M"))
        return []
    stats = st.setdefault("stats", {})
    if pick.get("sector") in (None, "WAIT") or pick.get("side") not in (1, -1):
        _count_candle(stats, pick)
        _settle(st, bar, set(), None, "market/sector pick was WAIT")
        save_ledger(st)
        return []
    _count_candle(stats, pick)

    session = str(bar.date())
    want = "BUY" if pick["side"] == 1 else "SELL"
    uni = data.load_universe()
    sector_of = _scan_list(uni, pick)
    book = st.setdefault("early", {})
    sent = st.setdefault("sent", {})
    positions = st.setdefault("positions", {})
    names = [raw for raw in sector_of if raw not in sent and raw not in positions]
    if not names:
        save_ledger(st)
        return []

    tickers = {raw: data.yahoo_stock(raw) for raw in names}
    sm5 = data.fetch_many(tickers.values(), os.environ.get("YF_PERIOD_5M", "7d"), "5m")
    sm1 = one_minute_frames(tickers, bar)
    sd1 = data.fetch_daily(tickers.values(), os.environ.get("YF_PERIOD_1D", "90d"))
    oi_reader = _oi_reader(session)
    _note_feed_behind(st, sm1, bar, now)

    hhmm = bar.strftime("%H:%M")
    out: list[dict] = []
    confirmed: set[str] = set()
    checked: set[str] = set()
    closes: dict[str, float] = {}
    for raw, ysym in tickers.items():
        frame = closed_frame(sm5.get(ysym), sm1.get(ysym), bar)
        if frame is None:
            continue                    # still filling: retried, never written off
        checked.add(raw)
        try:
            if to_ist(frame.index[-1]) == bar:
                closes[raw] = float(frame["Close"].iloc[-1])
        except Exception:               # a close price is a nicety, never a reason to fail
            log.debug("no close price for %s at %s", raw, hhmm)
        try:
            fires = scan_stock(raw, frame, sd1.get(ysym), keep_all=True)
        except Exception:
            log.exception("1m confirm scan failed for %s", raw)
            continue
        fresh = [s for s in fires if s.time == hhmm and s.side == want]
        if not fresh:
            if fires:                   # same reject book as the 5m scan
                if any(s.time == hhmm for s in fires):
                    _note_reject(st, "opposite", raw, f"{raw} {hhmm}", per_bar=hhmm)
                before = [s.time for s in fires if s.side == want and s.time < hhmm]
                if before:
                    _note_reject(st, "earlier", raw, f"{raw} {min(before)}")
            continue
        best = fresh[-1]
        plan = build_plan(best, frame, bar, sd1.get(ysym))
        if plan is None:
            log.warning("1m SKIP %s @ %s: missing EMA9 or EMA9 stop not beyond entry close",
                        raw, best.time)
            _note_reject(st, "no_stop", raw, f"{raw} {best.time}", per_bar=best.time)
            continue
        reading = oi_reader.snapshot(raw, bar) if oi_reader else None
        if oi_reader and _oi_mode() == "require" and not should_take_entry(best.side, reading):
            _note_reject(st, "oi", raw, f"{raw} {best.time}", per_bar=best.time)
            continue
        early = book.get(raw) or None
        if early and early.get("bar") != bar.isoformat():
            early = None
        best.price, best.sl = plan.entry, plan.sl
        text = format_confirmed(best, pick, plan, early=early, sector=sector_of[raw],
                                oi_line=_oi_line(reading))
        if not telegram.send_bot3(text, dry_run=_dry()):
            log.warning("bot 3 delivery failed for %s; the next minute retries", raw)
            continue
        confirmed.add(raw)
        sent[raw] = {"code": best.code, "name": best.name, "time": best.time,
                     "price": plan.entry, "sl": plan.sl,
                     "at": now_ist().strftime("%H:%M:%S")}
        if early:
            early["result"] = "confirmed"
        stats["confirmed"] = stats.get("confirmed", 0) + 1
        save_ledger(st)
        log.info("1m CONFIRMED %s %s %s @ %.2f (sent %s IST)", raw, best.side, best.name,
                 plan.entry, now_ist().strftime("%H:%M:%S"))
        out.append({**best.__dict__, "confirmed": True})
    _settle(st, bar, confirmed, checked, "did not hold at the close", closes=closes)
    save_ledger(st)
    return out


def _count_candle(stats: dict, pick: dict | None) -> None:
    """Count each 5m candle once, with/without a pick — like the 5m summary."""
    bar_key = (pick or {}).get("bar")
    if not bar_key or stats.get("bar") == bar_key:
        return
    stats["bar"] = bar_key
    stats["candles"] = stats.get("candles", 0) + 1
    scanning = (pick or {}).get("sector") not in (None, "WAIT")
    stats["picks"] = stats.get("picks", 0) + (1 if scanning else 0)
    if scanning:
        side = "long" if pick.get("side") == 1 else "short"
        tag = ", follow" if pick.get("follow") else ""
        book = stats.setdefault("sectors", {})
        key = f"{pick['sector']} {side}{tag}"
        book[key] = book.get(key, 0) + 1


def _note_feed_behind(st: dict, frames1m: dict, bar: datetime, now: datetime) -> None:
    """Count candles whose 1m feed had not reached the close (log once)."""
    close_at = bar + timedelta(minutes=BAR_MINUTES)
    if (now - close_at).total_seconds() < 120:
        return
    newest = [df.index[-1] for df in frames1m.values() if df is not None and not df.empty]
    if not newest or max(newest) >= pd.Timestamp(close_at):
        return
    stats = st.setdefault("stats", {})
    stats["feed_behind"] = stats.get("feed_behind", 0) + 1
    if not stats.get("feed_warned"):
        stats["feed_warned"] = True
        log.warning("⚠ Yahoo 1m feed is behind: newest print %s, candle closed %s — "
                    "projections and confirmations are held back, not guessed",
                    max(newest), close_at.strftime("%H:%M"))


def _settle(st: dict, bar: datetime, confirmed: set[str], checked: set[str] | None = None,
            reason: str = "", closes: dict[str, float] | None = None) -> None:
    """Resolve projected alerts of ``bar`` that the close did not confirm.

    ``checked`` limits this to the stocks the close scan could actually judge:
    a candle whose feed has not published yet must be retried, not written off.
    """
    for raw, early in list((st.get("early") or {}).items()):
        if early.get("result") or early.get("bar") != bar.isoformat() or raw in confirmed:
            continue
        if checked is not None and raw not in checked:
            continue
        early["result"] = "cancelled"
        st.setdefault("stats", {})["cancelled"] = st.get("stats", {}).get("cancelled", 0) + 1
        log.info("1m projection for %s did not hold (%s) — no message", raw, reason)
        if cancel_message():
            price = early.get("price")
            px = (closes or {}).get(raw)
            move = ""
            if px is not None and price:
                pct = (px - price) / price * 100.0
                if str(early.get("side") or "").upper() == "SELL":
                    pct = -pct                    # "beyond" always means "in our favour"
                move = f" · close {px:.2f} ({pct:+.2f}%)"
            telegram.send_bot3(
                f"❌ <b>EARLY CANCELLED</b> — {early.get('side')} {early.get('name') or raw} ({raw})\n"
                f"projected {early.get('at')} @ {price:.2f}{move}\n"
                f"the {bar:%H:%M} 5m close did not confirm it — no entry recorded",
                dry_run=_dry())


# ── optional OI parity with the 5m scan ──────────────────────────────────────
def _within_lag(bar: datetime, now: datetime) -> bool:
    """Inside the same alert deadline the 5m scan uses after a candle's close."""
    from .pipeline import _max_lag

    return (now - (bar + timedelta(minutes=BAR_MINUTES))).total_seconds() <= _max_lag()


def unresolved(bar: datetime) -> set[str]:
    """Projected names of ``bar`` still waiting for a close verdict."""
    st = load_ledger()
    return {raw for raw, e in (st.get("early") or {}).items()
            if not e.get("result") and e.get("bar") == bar.isoformat()}


def _oi_mode() -> str:
    from .pipeline import _oi_mode as mode

    return mode()


def _oi_reader(session: str):
    from .pipeline import _oi_reader as reader

    return reader(session)


def _oi_line(reading) -> str:
    from .pipeline import _oi_line as line

    return line(reading)


# ── loop ─────────────────────────────────────────────────────────────────────
def tick(now: datetime) -> str:
    """What this minute is for: ``confirm`` | ``retry`` | ``project`` | ``idle``.

    ``confirm`` — a 5m candle has just closed: judge the closed candle.
    ``retry``   — that candle's feed was late: finish judging it.
    ``project`` — inside a forming candle (minutes 1..4): project it from 1m.
    """
    bar = _bar_open(now)
    minute = int((now - bar).total_seconds() // 60)
    prev = bar - timedelta(minutes=BAR_MINUTES)
    if minute == 0:
        return "confirm" if _in_session(prev) else "idle"
    if _in_session(prev) and unresolved(prev) and _within_lag(prev, now):
        return "retry"
    if first_minute() <= minute <= BAR_MINUTES - 1 and _in_session(bar):
        return "project"
    return "idle"


def _sleep_until(target: datetime) -> None:
    while True:
        left = (target - now_ist()).total_seconds()
        if left <= 0:
            return
        time.sleep(min(left, 30))


def live_loop_1m(*, max_minutes: float | None = None, exit_at_close: bool = False) -> str:
    """Minute-aligned loop: project inside the candle, confirm at its close."""
    from .session import is_weekday

    data.enable_daily_cache()
    started = now_ist()
    now = started
    if exit_at_close and (not is_weekday(now) or now.time() < clock_time(8, 30)
                          or now.time() >= clock_time(15, 33)):
        log.info("outside the NSE session (%s IST) — nothing to do", now.strftime("%a %H:%M"))
        return "closed"
    log.info("1m projected scan: alerts from minute %d of each 5m candle (up to %d min early); "
             "confirmed at the close; bot 3 only", first_minute(), BAR_MINUTES - first_minute())
    _announce()

    holiday = None
    while True:
        now = now_ist()
        if not is_weekday(now) or holiday == now.date():
            if exit_at_close:
                return "holiday" if holiday else "closed"
            time.sleep(1800)
            continue
        t = now.time()
        if t.hour < 9 or (t.hour == 9 and t.minute < 14):
            target = now.replace(hour=9, minute=14, second=0, microsecond=0)
            time.sleep(max(5, min((target - now).total_seconds(), 300)))
            continue
        if t.hour > 15 or (t.hour == 15 and t.minute >= 33):
            if want_summary():
                try:
                    st = load_ledger()
                    stats = st.setdefault("stats", {})
                    if not stats.get("reported") and (stats.get("early") or stats.get("confirmed")):
                        if telegram.send_bot3(format_summary(st), dry_run=_dry()):
                            stats["reported"] = True
                            save_ledger(st)
                except Exception:
                    log.exception("1m session summary failed")
            if exit_at_close:
                return "closed"
            time.sleep(1800)
            continue

        nxt = (now.replace(second=0, microsecond=0) + timedelta(minutes=1))
        _sleep_until(nxt + timedelta(seconds=grace()))
        now = now_ist()
        bar = _bar_open(now)
        try:
            action = tick(now)
            if action == "confirm":
                confirm_pass(bar - timedelta(minutes=BAR_MINUTES), now=now)
            elif action == "retry":
                # A candle whose feed published late is judged again (never a new
                # entry: ``sent`` and the lag deadline still apply).
                confirm_pass(bar - timedelta(minutes=BAR_MINUTES), now=now)
            elif action == "project":
                run_minute(bar, now=now)
        except state.InvalidStateError:
            raise
        except Exception:
            log.exception("1m cycle failed at %s", now.strftime("%H:%M:%S"))
            time.sleep(5)
        if max_minutes and (now_ist() - started).total_seconds() / 60.0 >= max_minutes:
            log.info("runtime budget of %.0f min reached — handing over", max_minutes)
            return "budget"


def _announce() -> None:
    """One start-up line per session, never repeated after a handover."""
    try:
        st = load_ledger()
        if st.get("announced"):
            return
        if telegram.send_bot3(
                f"1m projected scan started (bot 3 · IST · yfinance)\n"
                f"Alerts from minute {first_minute()} of each 5m candle "
                f"(up to {BAR_MINUTES - first_minute()} min before the close), "
                f"confirmed entry at the close.", dry_run=_dry()):
            st["announced"] = True
            save_ledger(st)
    except Exception:
        log.exception("1m announce failed")
