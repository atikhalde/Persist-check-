"""One scan cycle: market lock → sector stocks → Telegram on new entries."""

from __future__ import annotations

import logging
import os
from . import data, state, telegram
from .market import run_market, SECTOR_ORDER
from .session import now_ist, to_ist
from .state import signal_rank
from .stock import StockSignal, is_buy_code, scan_stock

log = logging.getLogger("nse_scanner")


def _dry() -> bool:
    return os.environ.get("DRY_RUN", "0") == "1"


def format_market(md) -> str:
    side = "BULL" if md.pick_side == 1 else "BEAR" if md.pick_side == -1 else "FLAT"
    plus = " · ".join(f"{n} {s:.0f}" for n, s in md.persist_plus)
    minus = " · ".join(f"{n} {s:.0f}" for n, s in md.persist_minus)
    vix_arrow = "↑" if md.vix_chg > 0 else "↓" if md.vix_chg < 0 else "→"
    return (
        f"<b>MARKET {md.dir_label}</b>  conf {md.dir_conf}% {md.dir_tier}\n"
        f"Nifty {md.nifty:,.1f}  day {md.nifty_day_ret:+.2f}%  intra {md.nifty_intra:+.2f}%\n"
        f"VIX {md.vix:.2f} {vix_arrow} {md.vix_chg:+.2f}%   ADX {md.adx:.0f}   MTF {md.mtf_bull}/3\n"
        f"Breadth {md.breadth_adv}▲/{md.breadth_dec}▼  {md.regime}\n"
        f"PERSIST +  {plus}\n"
        f"PERSIST −  {minus}\n"
        f"Live flow  {md.live_flow} {'Q' if md.live_flow_qualified else 'uQ'} ({md.live_flow_score:.0f})\n"
        f"\n<b>SECTOR PICK: {md.pick_sector}</b>  {side}\n"
        f"{md.pick_reason}\n"
        f"Action: <b>{md.action}</b>\n"
        f"<i>{md.bar_time} IST</i>"
    )


def format_stock(sig: StockSignal, md) -> str:
    arrow = "🟢" if sig.side == "BUY" else "🔴"
    return (
        f"{arrow} <b>{sig.name}</b>  {sig.symbol}\n"
        f"Sector <b>{md.pick_sector}</b>  |  Market {md.dir_label} ({md.dir_conf}%)\n"
        f"Price {sig.price}   SL {sig.sl}\n"
        f"Score {sig.score} {sig.strength}   {sig.reason}\n"
        f"Time {sig.time} IST"
    )


def run_once(*, force_sector: str | None = None, ping_on_wait: bool = False, backfill: bool = False) -> dict:
    uni = data.load_universe()
    period_5m = os.environ.get("YF_PERIOD_5M", "7d")
    period_1d = os.environ.get("YF_PERIOD_1D", "90d")

    nifty_sym = uni["nifty"]
    vix_sym = uni["vix"]
    index_map = {name: uni["sectors"][name]["index"] for name in SECTOR_ORDER if name in uni["sectors"]}

    log.info("fetching nifty 5m")
    nifty_pack = data.fetch_symbol(nifty_sym, period_5m, period_1d)
    nifty_5 = nifty_pack["m5"]
    if nifty_5 is None or nifty_5.empty:
        raise RuntimeError("No Nifty 5m data from yfinance")

    last = to_ist(nifty_5.index[-1])
    session = str(last.date())
    today = str(now_ist().date())
    allow_stale = backfill or bool(force_sector) or os.environ.get("BACKFILL", "0") == "1"
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

    log.info("fetching vix / %d sector indices", len(index_map))
    idx_tickers = [vix_sym] + list(index_map.values())
    m5 = data.fetch_many(idx_tickers, period_5m, "5m")
    d1 = data.fetch_many(idx_tickers, period_1d, "1d")
    m5[nifty_sym] = nifty_5
    d1[nifty_sym] = nifty_pack["d1"]

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
    )
    log.info(
        "market %s conf=%s pick=%s action=%s @ %s",
        md.dir_label,
        md.dir_conf,
        md.pick_sector,
        md.action,
        md.bar_time,
    )

    if force_sector:
        force_sector = force_sector.upper()
        if force_sector in uni["sectors"]:
            md.pick_sector = force_sector
            md.pick_side = md.dir_state if md.dir_state != 0 else 1
            md.action = "SCAN LONG" if md.pick_side == 1 else "SCAN SHORT"
            md.pick_reason = f"FORCED {force_sector}"

    # market / sector change alerts
    prev_m = st.get("market") or {}
    prev_s = st.get("sector") or {}
    market_changed = prev_m.get("dir_state") != md.dir_state or prev_m.get("label") != md.dir_label
    sector_changed = prev_s.get("name") != md.pick_sector or prev_s.get("side") != md.pick_side
    if market_changed or sector_changed:
        telegram.send(format_market(md), dry_run=_dry())
        st["market"] = {
            "dir_state": md.dir_state,
            "label": md.dir_label,
            "conf": md.dir_conf,
            "regime": md.regime,
        }
        st["sector"] = {"name": md.pick_sector, "side": md.pick_side, "reason": md.pick_reason}

    result = {
        "session": session,
        "bar": md.bar_time,
        "market": md.dir_label,
        "pick": md.pick_sector,
        "action": md.action,
        "signals": [],
    }

    if md.pick_sector == "WAIT" or md.action == "WAIT":
        if ping_on_wait and not market_changed and not sector_changed:
            log.info("WAIT — no complementary persistent sector")
        state.save(st)
        return result

    sec_cfg = uni["sectors"][md.pick_sector]
    stocks = sec_cfg["stocks"]
    tickers = [data.yahoo_stock(s) for s in stocks]
    log.info("scanning %s (%d names) 5m entries", md.pick_sector, len(tickers))
    sm5 = data.fetch_many(tickers, period_5m, "5m")
    sm1 = data.fetch_many(tickers, "7d", "1m")
    sd1 = data.fetch_many(tickers, period_1d, "1d")

    want_buy = md.pick_side == 1
    sent = st.setdefault("sent", {})
    new_signals: list[StockSignal] = []

    for raw, ysym in zip(stocks, tickers):
        df5 = sm5.get(ysym)
        df1 = sd1.get(ysym)
        if df5 is None or df5.empty:
            log.warning("no 5m data %s", ysym)
            continue
        df5 = data.apply_1m_volume(df5, sm1.get(ysym))
        try:
            sigs = scan_stock(raw, df5, df1)
        except Exception as e:
            log.exception("scan failed %s: %s", raw, e)
            continue
        if not sigs:
            continue
        # last (highest-rank) signal of the day
        best = sigs[0]
        for s in sigs[1:]:
            if signal_rank(s.code) >= signal_rank(best.code):
                best = s
        # only complementary side
        if want_buy and best.side != "BUY":
            continue
        if (not want_buy) and best.side != "SELL":
            continue
        prev_code = int((sent.get(raw) or {}).get("code") or 0)
        if prev_code and signal_rank(best.code) <= signal_rank(prev_code):
            continue
        new_signals.append(best)
        sent[raw] = {
            "code": best.code,
            "name": best.name,
            "time": best.time,
            "price": best.price,
        }
        telegram.send(format_stock(best, md), dry_run=_dry())
        log.info("ALERT %s %s %s @ %s", best.name, raw, best.price, best.time)

    st["sent"] = sent
    st["session"] = session
    state.save(st)
    result["signals"] = [s.__dict__ for s in new_signals]
    return result


def live_loop(grace: int | None = None) -> None:
    import time
    from .session import next_5m_close, is_weekday

    grace = int(os.environ.get("POLL_GRACE_SECONDS", grace or 12))
    log.info("live loop IST, grace=%ss after each 5m close", grace)
    telegram.send("NSE live scanner started (5m · IST · yfinance).", dry_run=_dry())
    while True:
        now = now_ist()
        if not is_weekday(now):
            log.info("weekend — sleeping 30m")
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
        if t.hour > 15 or (t.hour == 15 and t.minute >= 40):
            log.info("session over, sleep until tomorrow")
            time.sleep(1800)
            continue
        nxt = next_5m_close(now)
        wait = (nxt - now).total_seconds() + grace
        if wait > 0:
            log.info("sleep %.1fs until %s + %ss", wait, nxt.strftime("%H:%M"), grace)
            time.sleep(min(wait, 330))
        try:
            run_once()
        except Exception:
            log.exception("scan cycle failed")
            time.sleep(15)
