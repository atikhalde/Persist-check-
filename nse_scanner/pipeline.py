"""One scan cycle: market lock → sector stocks → Telegram on new entries."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, time as clock_time, timedelta
from math import isfinite

import pandas as pd

from . import data, state, telegram
from .market import run_market, SECTOR_ORDER
from .session import IST, now_ist, session_date, to_ist
from .stock import StockSignal, scan_stock
from .trade import TradePlan, build_plan, ema9_frame, trail_broken

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
    return " · ".join(parts)


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


def format_stock(sig: StockSignal, md, plan: TradePlan) -> str:
    arrow = "🟢" if sig.side == "BUY" else "🔴"
    close_time = (datetime.strptime(sig.time, "%H:%M") + timedelta(minutes=5)).strftime("%H:%M")
    below = "below" if sig.side == "BUY" else "above"
    targets = _target_text(plan)
    return (
        f"{arrow} <b>{sig.side}</b> | <b>{sig.name}</b>  {sig.symbol}\n"
        f"Sector <b>{md.pick_sector}</b>  |  Market {md.dir_label} ({md.dir_conf}%)\n"
        f"Entry (candle close) <b>{plan.entry:.2f}</b>\n"
        f"SL {plan.sl:.2f} (0.02% {below} EMA9)\n"
        f"Risk {plan.risk:.2f}  |  next pivot target: {targets}\n"
        f"{_basis_text(plan)}"
        f"Score {sig.score} {sig.strength}   {sig.reason}\n"
        f"5m bar {sig.time}–{close_time} IST (signal confirmed at close)\n"
        f"Signal calculated {now_ist().strftime('%H:%M:%S')} IST"
    )


def _move_pct(pos: dict, price: float) -> float:
    """Signed move from entry in the trade's favour (+) or against it (-), in %."""
    delta = (price - pos["entry"]) if pos["side"] == "BUY" else (pos["entry"] - price)
    return delta / pos["entry"] * 100.0


def _r_of(pos: dict, price: float) -> str:
    risk = abs(float(pos["entry"]) - float(pos.get("sl") or 0))
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
    return _event_line(raw, pos, bar, f"🎯 <b>TARGET HIT — {pos['side']}</b>", " · ".join(bits))


def format_sl_hit(raw: str, pos: dict, bar: datetime, level: float) -> str:
    """One line when the initial SL is touched (intrabar, evaluated on a closed candle)."""
    return _event_line(raw, pos, bar, f"🛑 <b>SL HIT — {pos['side']}</b>",
                       f"SL {level:.2f} ({_move_pct(pos, level):+.2f}%{_r_of(pos, level)})")


def format_trail_exit(raw: str, pos: dict, bar: datetime, close: float, ema: float) -> str:
    """One line when a completed candle closes beyond EMA9."""
    direction = "below" if pos["side"] == "BUY" else "above"
    return _event_line(raw, pos, bar, f"🔔 <b>EMA9 EXIT — {pos['side']}</b>",
                       f"close {close:.2f} {direction} EMA9 {ema:.2f}"
                       f" ({_move_pct(pos, close):+.2f}%{_r_of(pos, close)})")


def _max_lag() -> int:
    """Maximum acceptable delay after a five-minute candle CLOSE."""
    return int(os.environ.get("MAX_ALERT_LAG_SECONDS", "180"))


def _recent_bar(bar: datetime, now: datetime) -> bool:
    age = (now - (bar + timedelta(minutes=5))).total_seconds()
    return 0 <= age <= _max_lag()


def _save(st: dict) -> None:
    # A dry-run must not consume tomorrow's real alert or today's alert.
    if not _dry():
        state.save(st)


def _alert_prefix(raw: str, pos: dict) -> str:
    """Ledger namespace for one trade: symbol + the candle it was entered on.

    A later trade on the same symbol therefore gets its own alerts, while the same
    trade can never be announced twice.
    """
    return f"{raw}:{pos.get('entry_bar') or pos.get('last_bar')}:"


def _alert_key(raw: str, pos: dict, tag: str) -> str:
    return _alert_prefix(raw, pos) + tag


def _track_positions(st: dict, bar: datetime, period_5m: str) -> list[str]:
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
    levels are additionally remembered on the position (``hit_targets``) so they
    stay quiet when a trade carries overnight.

    Every candle since the last check is replayed, so a cycle that missed a bar
    (slow Yahoo, queued Actions run) still reports what happened, stamped with
    that candle's time. A failed Telegram POST stops the replay at that candle so
    the next cycle can retry it without resending what already went out.
    """
    positions = st.setdefault("positions", {})
    if not positions:
        return []
    ledger = st.setdefault("alerts", {})
    symbols = {raw: data.yahoo_stock(raw) for raw in positions}
    frames = data.fetch_many(symbols.values(), period_5m, "5m")
    pending: list[str] = []
    changed = False
    session = session_date(bar)
    for raw, pos in list(positions.items()):
        df = frames.get(symbols[raw])
        df = df.loc[df.index <= bar] if df is not None and not df.empty else df
        if df is None or df.empty or to_ist(df.index[-1]) != bar:
            pending.append(raw)
            log.info("hit-check pending %s: 5m candle %s not published", raw, bar)
            continue
        grid = ema9_frame(df)
        if grid is None or grid.empty:
            pending.append(raw)
            continue
        ema_at = dict(zip(grid.index, grid["EMA9"].to_numpy(float)))
        side = pos["side"]
        sl = float(pos.get("sl") or 0)
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

            hit_now = [(slot, lvl) for slot, lvl in targets
                       if slot not in done and f"T{slot}" not in announced
                       and (row["High"] >= lvl if side == "BUY" else row["Low"] <= lvl)]
            if hit_now:
                if not telegram.send(format_target_hit(raw, pos, to_ist(ts), hit_now),
                                     dry_run=_dry()):
                    pending.append(raw)
                    break
                for slot, _ in hit_now:
                    ledger[_alert_key(raw, pos, f"T{slot}")] = hhmm
                    announced.add(f"T{slot}")
                done.update(slot for slot, _ in hit_now)
                pos["hit_targets"] = sorted(done)
                changed = True
                _save(st)                       # before the next send: never twice
                log.info("TARGET HIT %s %s %s @ %s", raw, side,
                         ",".join(f"T{slot} {lvl}" for slot, lvl in hit_now), ts)

            sl_hit = bool(row["Low"] <= sl) if side == "BUY" else bool(row["High"] >= sl)
            if sl_hit:
                if "SL" in announced:           # already delivered: just reconcile
                    del positions[raw]
                    changed = True
                    log.warning("SL for %s already announced; dropping stale position", raw)
                    break
                if not telegram.send(format_sl_hit(raw, pos, to_ist(ts), sl), dry_run=_dry()):
                    pending.append(raw)
                    break
                ledger[_alert_key(raw, pos, "SL")] = hhmm
                del positions[raw]
                st.setdefault("sent", {}).setdefault(raw, {"code": "SL", "time": hhmm})
                changed = True
                _save(st)
                log.info("SL HIT %s %s sl=%.2f @ %s", raw, side, sl, ts)
                break

            ema = ema_at.get(ts)
            close = float(row["Close"])
            if ema is not None and isfinite(ema) and trail_broken(side, close, float(ema)):
                if "EXIT" in announced:         # already delivered: just reconcile
                    del positions[raw]
                    changed = True
                    log.warning("EMA9 exit for %s already announced; dropping stale position", raw)
                    break
                if not telegram.send(format_trail_exit(raw, pos, to_ist(ts), close, float(ema)),
                                     dry_run=_dry()):
                    pending.append(raw)
                    break
                ledger[_alert_key(raw, pos, "EXIT")] = hhmm
                del positions[raw]
                # Block a new entry on the same IST session after an exit.
                st.setdefault("sent", {}).setdefault(raw, {"code": "EXIT", "time": hhmm})
                changed = True
                _save(st)
                log.info("EMA9 EXIT %s %s close=%.2f ema9=%.4f @ %s", raw, side, close, ema, ts)
                break

            pos["last_bar"] = ts.isoformat()
            changed = True
    if changed:
        _save(st)
    return pending


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
    d1 = data.fetch_many(idx_tickers, period_1d, "1d")
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

    # market / sector change alerts
    prev_m = st.get("market") or {}
    prev_s = st.get("sector") or {}
    market_changed = prev_m.get("dir_state") != md.dir_state or prev_m.get("label") != md.dir_label
    sector_changed = prev_s.get("name") != md.pick_sector or prev_s.get("side") != md.pick_side
    if (market_changed or sector_changed) and (allow_stale or _recent_bar(last, now_ist())) and telegram.send(format_market(md), dry_run=_dry()):
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
        _save(st)
        return result

    sec_cfg = uni["sectors"][md.pick_sector]
    stocks = sec_cfg["stocks"]
    tickers = [data.yahoo_stock(s) for s in stocks]
    log.info("scanning %s (%d names) 5m entries", md.pick_sector, len(tickers))
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
    sd1 = data.fetch_many(current, period_1d, "1d")

    want_buy = md.pick_side == 1
    new_signals: list[dict] = []

    for raw, ysym in zip(stocks, tickers):
        if ysym not in current:
            continue
        # One entry per stock per IST session, regardless of rank upgrades or
        # later runs. A new session resets this in reset_if_new_day().
        if raw in sent or raw in positions:
            continue
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
            if fires:
                log.info("SKIP historical/wrong-side %s fires=%s; decision bar=%s",
                         raw, ",".join(f"{s.name}@{s.time}" for s in fires),
                         last.strftime("%H:%M"))
            continue
        best = fresh[-1]
        if not allow_stale and not _recent_bar(last, now_ist()):
            log.warning("scan finished too late for %s @ %s; dropping entry", raw, best.time)
            continue
        plan = build_plan(best, df5, last, sd1.get(ysym))
        if plan is None:
            log.warning("SKIP %s @ %s: missing EMA9 or EMA9 stop not beyond entry close", raw, best.time)
            continue
        best.price = plan.entry
        best.sl = plan.sl
        if not telegram.send(format_stock(best, md, plan), dry_run=_dry()):
            log.warning("Telegram failed for %s; leaving unsent for retry", raw)
            continue
        new_signals.append({**best.__dict__, "ema9": plan.ema9, "risk": plan.risk,
                            "t1": plan.t1, "t2": plan.t2, "t3": plan.t3})
        sent[raw] = {
            "code": best.code,
            "name": best.name,
            "time": best.time,
            "price": best.price,
        }
        positions[raw] = {
            "side": best.side, "entry": plan.entry, "sl": plan.sl,
            "entry_ema9": plan.ema9, "t1": plan.t1, "t2": plan.t2, "t3": plan.t3,
            "entry_bar": last.isoformat(), "last_bar": last.isoformat(),
        }
        _save(st)  # persist each successful send in long-running live mode
        log.info("ALERT %s %s entry=%.2f sl=%.2f EMA9=%.4f @ %s (sent %s IST)",
                 best.name, raw, plan.entry, plan.sl, plan.ema9, best.time,
                 now_ist().strftime("%H:%M:%S"))

    st["sent"] = sent
    st["session"] = session
    _save(st)
    result["signals"] = new_signals
    return result


def run_until_ready(*, expected_bar: datetime | None = None, force_sector: str | None = None) -> dict:
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
        delay = min(15, remaining)
        log.info("waiting for completed Yahoo bar %s (%.0fs until deadline); retry in %.0fs",
                 expected, remaining, delay)
        time.sleep(delay)
    if result is not None:
        result["signals"] = delivered
        if result.get("pending"):
            log.warning("Yahoo stock bars did not publish by %s IST: %s; no late entries sent",
                        deadline.strftime("%H:%M:%S"), ", ".join(result["pending"]))
        return result
    if last_error:
        raise last_error
    return run_once(force_sector=force_sector)


def live_loop(grace: int | None = None) -> None:
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
        # Same bounded retry path as Actions: skip an old signal, never replay
        # an earlier bar merely because Yahoo published the new one late.
        if nxt.hour == 9 and nxt.minute == 15:
            continue
        try:
            run_until_ready(expected_bar=nxt - timedelta(minutes=5))
        except Exception:
            log.exception("scan cycle failed")
            time.sleep(15)
