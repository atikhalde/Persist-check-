"""Live dual-alert engine: original scanner alert + separate OI-confirmed alert.

Original pipeline is completely untouched. OI alerts are sent as separate
Telegram messages with their own format and dedupe state.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, time as clock_time, timedelta

from . import data, state, telegram
from .market import run_market, SECTOR_ORDER
from .oi_data import CSV_OIReader, StubOIReader
from .oi_filter import should_take_entry
from .session import now_ist, to_ist
from .stock import scan_stock
from .trade import build_plan

log = logging.getLogger("nse_scanner.oi_live")


def _dry() -> bool:
    return os.environ.get("DRY_RUN", "0") == "1"


def _max_lag() -> int:
    return int(os.environ.get("MAX_ALERT_LAG_SECONDS", "180"))


def _recent_bar(bar: datetime, now: datetime) -> bool:
    age = (now - (bar + timedelta(minutes=5))).total_seconds()
    return 0 <= age <= _max_lag()


def format_oi_alert(sig, md, plan, reading) -> str:
    """Separate message format — clearly labeled OI CONFIRMED."""
    arrow = "🟢" if sig.side == "BUY" else "🔴"
    close_time = (datetime.strptime(sig.time, "%H:%M") + timedelta(minutes=5)).strftime("%H:%M")
    return (
        f"{arrow} <b>OI CONFIRMED</b> | <b>{sig.side}</b> | <b>{sig.name}</b>  {sig.symbol}\n"
        f"Sector <b>{md.pick_sector}</b>  |  Market {md.dir_label} ({md.dir_conf}%)\n"
        f"Entry (candle close) <b>{plan.entry:.2f}</b>\n"
        f"SL {plan.sl:.2f}\n"
        f"OI Regime: <b>{reading.regime.value}</b>\n"
        f"OI chg: {reading.oi_chg_pct:+.2f}%  |  Price chg: {reading.price_chg_pct:+.2f}%\n"
        f"Score {sig.score} {sig.strength}   {sig.reason}\n"
        f"5m bar {sig.time}–{close_time} IST\n"
        f"Signal calculated {now_ist().strftime('%H:%M:%S')} IST\n"
    )


def _load_oi_reader(oi_source: str, oi_file: str):
    if oi_source == "csv":
        return CSV_OIReader(oi_file)
    return StubOIReader()


def run_once_dual(*, force_sector: str | None = None, oi_source: str = "stub",
                  oi_file: str = "data/oi_history.csv") -> dict:
    """Run original scan (intact) + separate OI-confirmed alert pass.

    Returns {"original": {...}, "oi": {...}}.
    """
    from .pipeline import run_once as original_run_once

    # Pass 1: Original alert — completely intact, untouched
    original = original_run_once(force_sector=force_sector)

    # Pass 2: OI-confirmed alert — independent, separate Telegram message
    oi = _oi_pass(force_sector=force_sector, oi_source=oi_source, oi_file=oi_file)

    return {"original": original, "oi": oi}


def _oi_pass(*, force_sector, oi_source, oi_file):
    uni = data.load_universe()
    period_5m = os.environ.get("YF_PERIOD_5M", "7d")
    period_1d = os.environ.get("YF_PERIOD_1D", "90d")

    now = now_ist()
    today = str(now.date())
    closed_bar = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0) - timedelta(minutes=5)
    if (closed_bar.weekday() >= 5 or
            not clock_time(9, 15) <= closed_bar.time() <= clock_time(15, 25) or
            not _recent_bar(closed_bar, now)):
        return {"bar": "", "signals": [], "skipped": "not_recent_or_closed"}

    nifty_sym = uni["nifty"]
    vix_sym = uni["vix"]
    index_map = {name: uni["sectors"][name]["index"] for name in SECTOR_ORDER if name in uni["sectors"]}

    nifty_pack = data.fetch_symbol(nifty_sym, period_5m, period_1d)
    nifty_5 = nifty_pack["m5"]
    if nifty_5 is None or nifty_5.empty:
        return {"bar": "", "signals": [], "skipped": "no_nifty"}

    nifty_5 = nifty_5.loc[nifty_5.index <= now - timedelta(minutes=5)]
    if nifty_5.empty:
        return {"bar": "", "signals": [], "skipped": "no_closed_bar"}

    last = to_ist(nifty_5.index[-1])
    session = str(last.date())
    if session != today:
        return {"bar": last.strftime("%Y-%m-%d %H:%M"), "signals": [], "skipped": "session_mismatch"}

    if not _recent_bar(last, now):
        return {"bar": last.strftime("%Y-%m-%d %H:%M"), "signals": [], "skipped": "stale"}

    idx_tickers = [vix_sym] + list(index_map.values())
    m5 = data.fetch_many(idx_tickers, period_5m, "5m")
    d1 = data.fetch_many(idx_tickers, period_1d, "1d")
    m5 = {sym: df.loc[df.index <= last] if df is not None and not df.empty else df
          for sym, df in m5.items()}
    m5[nifty_sym] = nifty_5
    d1[nifty_sym] = nifty_pack["d1"]

    sector_5 = {name: m5.get(sym) for name, sym in index_map.items()}
    sector_d = {name: d1.get(sym) for name, sym in index_map.items()}

    md = run_market(nifty_5, d1.get(nifty_sym), m5.get(vix_sym), d1.get(vix_sym),
                    sector_5, sector_d)

    if md.bar_time != last.strftime("%Y-%m-%d %H:%M"):
        return {"bar": md.bar_time, "signals": [], "skipped": "sanitized"}

    if force_sector:
        force_sector = force_sector.upper()
        if force_sector in uni["sectors"]:
            md.pick_sector = force_sector
            md.pick_side = md.dir_state if md.dir_state != 0 else 1
            md.action = "SCAN LONG" if md.pick_side == 1 else "SCAN SHORT"
            md.pick_reason = f"FORCED {force_sector}"

    if md.pick_sector == "WAIT" or md.action == "WAIT":
        return {"bar": md.bar_time, "signals": [], "skipped": "wait"}

    sec_cfg = uni["sectors"][md.pick_sector]
    stocks = sec_cfg["stocks"]
    tickers = [data.yahoo_stock(s) for s in stocks]
    sm5 = data.fetch_many(tickers, period_5m, "5m")
    sm5 = {sym: df.loc[df.index <= last] if df is not None and not df.empty else df
           for sym, df in sm5.items()}
    current = [sym for sym in tickers if sm5.get(sym) is not None
               and not sm5[sym].empty and to_ist(sm5[sym].index[-1]) == last]

    if not current:
        return {"bar": md.bar_time, "signals": [], "skipped": "no_stock_bars"}

    sm1 = data.fetch_many(current, "7d", "1m")
    sd1 = data.fetch_many(current, period_1d, "1d")

    oi_reader = _load_oi_reader(oi_source, oi_file)
    want_buy = md.pick_side == 1
    new_signals = []

    # Separate dedupe state for OI alerts (does NOT touch original state)
    oi_state_path = state.STATE_PATH.with_name("oi_sent.json")
    oi_sent = state.load(oi_state_path) if oi_state_path.exists() else {}
    oi_sent.setdefault("sent", {})

    for raw, ysym in zip(stocks, tickers):
        if ysym not in current:
            continue
        # OI alerts are separate — do NOT check the original `sent` state
        if raw in oi_sent["sent"]:
            continue

        df5 = data.apply_1m_volume(sm5[ysym], sm1.get(ysym))
        try:
            fires = scan_stock(raw, df5, sd1.get(ysym), keep_all=True)
        except Exception as e:
            log.exception("OI scan failed %s: %s", raw, e)
            continue

        fresh = [s for s in fires if s.time == last.strftime("%H:%M")
                 and s.side == ("BUY" if want_buy else "SELL")]
        if not fresh:
            continue

        best = fresh[-1]
        if not _recent_bar(last, now):
            continue

        plan = build_plan(best, df5, last, sd1.get(ysym))
        if plan is None:
            continue

        best.price = plan.entry
        best.sl = plan.sl

        reading = oi_reader.snapshot(raw, last)
        if not should_take_entry(best.side, reading):
            log.info("OI filter rejected %s: %s", raw,
                     reading.regime.value if reading else "no_data")
            continue

        if not telegram.send(format_oi_alert(best, md, plan, reading), dry_run=_dry()):
            log.warning("OI Telegram failed for %s", raw)
            continue

        new_signals.append({**best.__dict__, "ema9": plan.ema9, "risk": plan.risk,
                            "t1": plan.t1, "t2": plan.t2, "t3": plan.t3,
                            "oi_regime": reading.regime.value,
                            "oi_chg_pct": reading.oi_chg_pct})

        oi_sent["sent"][raw] = {
            "code": best.code, "name": best.name, "time": best.time,
            "price": best.price, "oi_regime": reading.regime.value,
        }
        state.save(oi_sent, path=oi_state_path)
        log.info("OI ALERT %s %s entry=%.2f regime=%s @ %s",
                 best.name, raw, plan.entry, reading.regime.value, best.time)

    return {"bar": md.bar_time, "signals": new_signals,
            "market": md.dir_label, "pick": md.pick_sector,
            "action": md.action}


def live_loop_dual(*, grace: int | None = None, oi_source: str = "stub",
                   oi_file: str = "data/oi_history.csv") -> None:
    """Live loop that sends both original + OI alerts each cycle."""
    from .session import next_5m_close, is_weekday

    grace = int(os.environ.get("POLL_GRACE_SECONDS", grace or 12))
    log.info("OI dual live loop IST, grace=%ss after each 5m close", grace)
    telegram.send("NSE OI dual scanner started (original + OI confirmed).", dry_run=_dry())

    while True:
        now = now_ist()
        if not is_weekday(now):
            log.info("weekend — sleeping 30m")
            time.sleep(1800)
            continue
        t = now.time()
        if t.hour < 9 or (t.hour == 9 and t.minute < 14):
            target = now.replace(hour=9, minute=14, second=0, microsecond=0)
            sleep = max(5, (target - now).total_seconds())
            log.info("pre-open, sleep %.0fs", sleep)
            time.sleep(min(sleep, 300))
            continue
        if t.hour > 15 or (t.hour == 15 and t.minute >= 40):
            log.info("session over, sleep until tomorrow")
            time.sleep(1800)
            continue
        nxt = next_5m_close(now)
        wait = (nxt - now).total_seconds() + grace
        if wait > 0:
            log.info("sleep %.1fs until %s + %ss", wait, nxt.strftime("%H:%M"), grace)
            time.sleep(min(wait, 330))
        if nxt.hour == 9 and nxt.minute == 15:
            continue
        try:
            run_once_dual(force_sector=None, oi_source=oi_source, oi_file=oi_file)
        except Exception:
            log.exception("dual scan cycle failed")
            time.sleep(15)
