"""Sector follow alert: scans persistent sectors even when Nifty is NEUTRAL.

Completely separate from the original scanner. Original alerts are 100%
untouched. This module only fires when the original scanner would say WAIT
but a persistent sector exists that deserves a look.

Easy removal: delete this file and revert the two CLI command additions in
__main__.py. Nothing else changes.
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

log = logging.getLogger("nse_scanner.sector_follow")


def _dry() -> bool:
    return os.environ.get("DRY_RUN", "0") == "1"


def _max_lag() -> int:
    return int(os.environ.get("MAX_ALERT_LAG_SECONDS", "180"))


def _recent_bar(bar: datetime, now: datetime) -> bool:
    age = (now - (bar + timedelta(minutes=5))).total_seconds()
    return 0 <= age <= _max_lag()


def format_sector_follow_alert(sig, md, plan, reading=None) -> str:
    """Separate message format — clearly labeled SECTOR FOLLOW."""
    arrow = "🟢" if sig.side == "BUY" else "🔴"
    close_time = (datetime.strptime(sig.time, "%H:%M") + timedelta(minutes=5)).strftime("%H:%M")
    oi_line = ""
    if reading is not None:
        oi_line = (f"OI Regime: <b>{reading.regime.value}</b>\n"
                   f"OI chg: {reading.oi_chg_pct:+.2f}%  |  Price chg: {reading.price_chg_pct:+.2f}%\n")
    return (
        f"{arrow} <b>SECTOR FOLLOW</b> | <b>{sig.side}</b> | <b>{sig.name}</b>  {sig.symbol}\n"
        f"Sector <b>{md.pick_sector}</b>  |  Market {md.dir_label} ({md.dir_conf}%)\n"
        f"Entry (candle close) <b>{plan.entry:.2f}</b>\n"
        f"SL {plan.sl:.2f}\n"
        f"{oi_line}"
        f"Reason: persistent sector scanned despite {md.dir_label} market\n"
        f"Score {sig.score} {sig.strength}   {sig.reason}\n"
        f"5m bar {sig.time}–{close_time} IST\n"
        f"Signal calculated {now_ist().strftime('%H:%M:%S')} IST\n"
    )


def _sf_state_path():
    return state.STATE_PATH.with_name("sector_follow_sent.json")


def _load_sf_state() -> dict:
    p = _sf_state_path()
    if p.exists():
        return state.load(p)
    return {"session": "", "sent": {}, "positions": {}}


def _save_sf_state(st: dict) -> None:
    state.save(st, path=_sf_state_path())


def run_once_sector_follow(*, force_sector: str | None = None) -> dict:
    """Scan persistent sectors even when Nifty is NEUTRAL/TRANSITION.

    Only fires when the original scanner would WAIT (action not SCAN LONG/
    SCAN SHORT). Returns signals for separate Telegram alerts. Does not touch
    original state, original sent ledger, or original Telegram messages.
    """
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

    # If original scanner is already scanning this sector, don't duplicate.
    if md.action in ("SCAN LONG", "SCAN SHORT"):
        return {"bar": md.bar_time, "signals": [], "skipped": "original_scanning"}

    # Build candidate list from sector snaps: persistent sectors only.
    snaps = md.sectors or {}
    candidates = []
    for name, snap in snaps.items():
        persist_bull = float(snap.get("persist_bull", 0) or 0)
        persist_bear = float(snap.get("persist_bear", 0) or 0)
        qualified = bool(snap.get("qualified", False))
        side = int(snap.get("side", 0) or 0)
        score = max(persist_bull, persist_bear)
        if score > 0:
            candidates.append({
                "name": name,
                "side": side,
                "score": score,
                "qualified": qualified,
                "persist_bull": persist_bull,
                "persist_bear": persist_bear,
            })

    if not candidates:
        return {"bar": md.bar_time, "signals": [], "skipped": "no_persistent_sector"}

    # Sort: live-flow qualified first, then by score descending.
    candidates.sort(key=lambda c: (not c["qualified"], -c["score"]))

    # When Nifty is NEUTRAL, only scan bull (PERSIST +) sectors — we don't
    # want to short into a neutral index.
    if md.dir_state == 0:
        candidates = [c for c in candidates if c["side"] == 1]
        if not candidates:
            return {"bar": md.bar_time, "signals": [],
                    "skipped": "no_bull_sector_neutral_market"}

    # Fall-through: try each candidate in score order. If the top candidate
    # (e.g. FIN at score 65) has no fresh fire on the current bar, move to
    # the next (e.g. REALTY at score 61) until one produces an alert.
    sent = _load_sf_state()
    # Reset dedupe on new session.
    if sent.get("session") != session:
        sent = {"session": session, "sent": {}, "positions": {}}
    sent.setdefault("sent", {})
    sent.setdefault("positions", {})

    oi_source = os.environ.get("OI_SOURCE", "stub")
    oi_file = os.environ.get("OI_FILE", "data/oi_history.csv")
    oi_reader = CSV_OIReader(oi_file) if oi_source == "csv" else StubOIReader()

    new_signals = []
    picked_sector = None
    picked_side = 0

    for cand in candidates:
        pick_name = cand["name"]
        pick_side = cand["side"]
        want_buy = pick_side == 1

        sec_cfg = uni["sectors"][pick_name]
        stocks = sec_cfg["stocks"]
        tickers = [data.yahoo_stock(s) for s in stocks]
        sm5 = data.fetch_many(tickers, period_5m, "5m")
        sm5 = {sym: df.loc[df.index <= last] if df is not None and not df.empty else df
               for sym, df in sm5.items()}
        current = [sym for sym in tickers if sm5.get(sym) is not None
                   and not sm5[sym].empty and to_ist(sm5[sym].index[-1]) == last]

        if not current:
            log.info("Sector follow %s: no stock bars for bar %s",
                     pick_name, last.strftime("%H:%M"))
            continue

        sm1 = data.fetch_many(current, "7d", "1m")
        sd1 = data.fetch_many(current, period_1d, "1d")

        for raw, ysym in zip(stocks, tickers):
            if ysym not in current:
                continue
            if raw in sent["sent"] or raw in sent["positions"]:
                continue

            df5 = data.apply_1m_volume(sm5[ysym], sm1.get(ysym))
            try:
                fires = scan_stock(raw, df5, sd1.get(ysym), keep_all=True)
            except Exception as e:
                log.exception("Sector follow scan failed %s: %s", raw, e)
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

            # OI × Price confirmation: only accept Long Build-up (price ↑, OI ↑)
            # for buys. Rejects short-covering rallies that reverse (like FIN on 12-Nov).
            reading = oi_reader.snapshot(raw, last)
            if not should_take_entry(best.side, reading):
                log.info("Sector follow OI rejected %s: %s", raw,
                         reading.regime.value if reading else "no_data")
                continue

            # Override md for this alert only (local copy — original md unchanged).
            md.pick_sector = pick_name
            md.pick_side = pick_side
            md.action = "SCAN LONG" if pick_side == 1 else "SCAN SHORT"
            md.pick_reason = f"SECTOR FOLLOW {pick_name} (persistent, market {md.dir_label})"

            if not telegram.send(format_sector_follow_alert(best, md, plan, reading), dry_run=_dry()):
                log.warning("Sector follow Telegram failed for %s", raw)
                continue

            new_signals.append({**best.__dict__, "ema9": plan.ema9, "risk": plan.risk,
                                "t1": plan.t1, "t2": plan.t2, "t3": plan.t3,
                                "oi_regime": reading.regime.value if reading else None,
                                "oi_chg_pct": reading.oi_chg_pct if reading else None})

            sent["sent"][raw] = {
                "code": best.code, "name": best.name, "time": best.time,
                "price": best.price,
            }
            sent["positions"][raw] = {
                "side": best.side, "entry": plan.entry, "sl": plan.sl,
                "entry_ema9": plan.ema9, "t1": plan.t1, "t2": plan.t2, "t3": plan.t3,
                "entry_bar": last.isoformat(), "last_bar": last.isoformat(),
            }
            _save_sf_state(sent)
            picked_sector = pick_name
            picked_side = pick_side
            log.info("SECTOR FOLLOW ALERT %s %s entry=%.2f sl=%.2f @ %s",
                     best.name, raw, plan.entry, plan.sl, best.time)

            # One entry per cycle — stop after first successful alert.
            break

        if new_signals:
            break

    return {"bar": md.bar_time, "signals": new_signals,
            "market": md.dir_label, "pick": picked_sector or "",
            "action": "SCAN LONG" if picked_side == 1 else "WAIT"}


def live_loop_sector_follow(*, grace: int | None = None) -> None:
    """Live loop that scans persistent sectors even when Nifty is NEUTRAL."""
    from .session import next_5m_close, is_weekday

    grace = int(os.environ.get("POLL_GRACE_SECONDS", grace or 12))
    log.info("Sector follow live loop IST, grace=%ss after each 5m close", grace)
    telegram.send("NSE sector follow scanner started (persistent sectors, neutral market OK).",
                  dry_run=_dry())

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
            run_once_sector_follow()
        except Exception:
            log.exception("sector follow scan cycle failed")
            time.sleep(15)
