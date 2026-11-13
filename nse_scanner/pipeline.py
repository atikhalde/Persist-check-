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


def _follow_tag(md) -> str:
    """Marks alerts taken on a leading sector while Nifty itself is NEUTRAL."""
    return " (sector follow)" if str(getattr(md, "pick_reason", "")).startswith("SECTOR FOLLOW") else ""


def format_stock(sig: StockSignal, md, plan: TradePlan) -> str:
    arrow = "🟢" if sig.side == "BUY" else "🔴"
    close_time = (datetime.strptime(sig.time, "%H:%M") + timedelta(minutes=5)).strftime("%H:%M")
    below = "below" if sig.side == "BUY" else "above"
    targets = _target_text(plan)
    return (
        f"{arrow} <b>{sig.side}</b> | <b>{sig.name}</b>  {sig.symbol}\n"
        f"Sector <b>{md.pick_sector}</b>{_follow_tag(md)}  |  Market {md.dir_label} ({md.dir_conf}%)\n"
        f"Entry (candle close) <b>{plan.entry:.2f}</b>\n"
        f"SL {plan.sl:.2f} (0.02% {below} EMA9)\n"
        f"Risk {plan.risk:.2f}  |  next pivot target: {targets}\n"
        f"{_basis_text(plan)}"
        f"Score {sig.score} {sig.strength}   {sig.reason}\n"
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

def _bump(st: dict, key: str, n: float = 1) -> None:
    stats = st.setdefault("stats", {})
    stats[key] = stats.get(key, 0) + n


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
             f"Early passes {x.get('early_passes', 0)} · with live 1m data {x.get('early_live', 0)}"
             f" · {age(avg('early_age'))}",
             f"Early alerts {x.get('early_sent', 0)}: confirmed {x.get('confirmed', 0)}"
             f" · cancelled {x.get('cancelled', 0)} · unverified {x.get('unverified', 0)}",
             f"Entries {x.get('entries', 0)}"
             + (f" · sent {x['lag_sum'] / x['entries']:.0f}s after the close on average,"
                f" slowest {x.get('lag_max', 0):.0f}s" if x.get("entries") else ""),
             f"At the confirm scan: {age(avg('close_age'))}"]
    return "\n".join(lines)


def _early_lead() -> int:
    """Seconds before the 5m close at which the forming candle is evaluated (0 = off)."""
    try:
        return max(0, min(240, int(os.environ.get("EARLY_ALERT_SECONDS", "60"))))
    except ValueError:
        return 60


def _early_max_stale() -> float:
    return float(os.environ.get("EARLY_MAX_STALE_SECONDS", "100"))


def format_early(sig: StockSignal, scan: dict, plan: TradePlan, bar: datetime) -> str:
    arrow = "🟢" if sig.side == "BUY" else "🔴"
    close = bar + timedelta(minutes=5)
    now = now_ist()
    left = max(0, int((close - now).total_seconds()))
    return (
        f"⚡ {arrow} <b>EARLY {sig.side}</b> (unconfirmed) | <b>{sig.name}</b>  {sig.symbol}\n"
        f"Sector <b>{scan.get('sector')}</b>  |  Market {scan.get('label')} ({scan.get('conf')}%)\n"
        f"Price now <b>{plan.entry:.2f}</b>  ·  5m bar {bar.strftime('%H:%M')}–{close.strftime('%H:%M')}"
        f" still forming ({left}s left)\n"
        f"SL ≈ {plan.sl:.2f} (EMA9 so far)  |  Risk ≈ {plan.risk:.2f}\n"
        f"next pivot target: {_target_text(plan)}\n"
        f"Score {sig.score} {sig.strength}   {sig.reason}\n"
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


def _cancel_early(st: dict, raw: str, early: dict, reason: str, result: str = "cancelled",
                  close: float | None = None) -> bool:
    """Tell the user an early heads-up did not survive; once, then never again."""
    bar = pd.Timestamp(early["bar"])
    hhmm = (bar + timedelta(minutes=5)).strftime("%H:%M")
    head = "❌ <b>EARLY CANCELLED</b>" if result == "cancelled" else "⚠ <b>EARLY UNVERIFIED</b>"
    px = f" · close {close:.2f} vs early {early['price']:.2f}" if close is not None else ""
    tail = ("no confirmed entry — manage/exit if you already entered" if result == "cancelled"
            else "treat as unconfirmed")
    text = (f"{head} — {early['side']}  {raw} · {early.get('name', '')} {reason} "
            f"({hhmm} close){px} · {tail}")
    if not telegram.send(text, dry_run=_dry()):
        return False
    early["result"] = result
    _bump(st, result)
    _save(st)
    log.info("EARLY %s %s %s: %s", result.upper(), raw, early["side"], reason)
    return True


def _settle_early(st: dict, bar: datetime, skip: set[str], reason: str,
                  result: str = "cancelled", older: bool = False) -> None:
    """Resolve early alerts for ``bar`` that the close scan did not look at."""
    for raw, early in list((st.get("early") or {}).items()):
        if early.get("result") or raw in skip:
            continue
        ebar = pd.Timestamp(early["bar"])
        if ebar == bar or (older and ebar < bar):
            _cancel_early(st, raw, early, reason, result)


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
    if _early_lead() <= 0 or not bar < now < close or bar.weekday() >= 5:
        return []
    st = state.reset_if_new_day(state.load(), str(now.date()))
    scan = st.get("scan") or {}
    prev = (bar - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
    side = scan.get("side")
    if scan.get("bar") != prev or side not in (1, -1) or scan.get("sector") in (None, "WAIT"):
        log.info("early %s: no current sector pick from %s — skipped", bar.strftime("%H:%M"), prev)
        return []
    uni = data.load_universe()
    sector = uni["sectors"].get(scan["sector"])
    if not sector:
        return []
    sent, positions = st.setdefault("sent", {}), st.setdefault("positions", {})
    book = st.setdefault("early", {})
    names = [raw for raw in sector["stocks"]
             if raw not in sent and raw not in positions and raw not in book]
    if not names:
        return []
    tickers = {raw: data.yahoo_stock(raw) for raw in names}
    period_5m = os.environ.get("YF_PERIOD_5M", "7d")
    sm5 = data.fetch_many(tickers.values(), period_5m, "5m")
    sm1 = data.fetch_many(tickers.values(), "7d", "1m")
    sd1 = data.fetch_daily(tickers.values(), os.environ.get("YF_PERIOD_1D", "90d"))
    _note_feed_age(st, "early", sm1)
    want = "BUY" if side == 1 else "SELL"
    hhmm = bar.strftime("%H:%M")
    out: list[dict] = []
    live = 0
    for raw, ysym in tickers.items():
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
        text = format_early(best, scan, plan, bar) + f" · price from 1m bar {m1.index[-1]:%H:%M}"
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
    _bump(st, "early_passes")
    _bump(st, "early_live", 1 if live else 0)
    _bump(st, "early_sent", len(out))
    _save(st)
    return out


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


def _warn_feed_behind(st: dict, close_at: datetime, frames1m: dict) -> None:
    """Say once per session that alerts are being held back by a delayed feed."""
    stats = st.setdefault("stats", {})
    if stats.get("feed_warned") or (now_ist() - close_at).total_seconds() < 120:
        return
    newest = max((df.index[-1] for df in frames1m.values() if df is not None and not df.empty),
                 default=None)
    if newest is None:
        return
    behind = (pd.Timestamp(now_ist()) - newest).total_seconds()
    if telegram.send(f"⚠ <b>Yahoo feed is delayed</b> — newest 1m bar is {newest:%H:%M} "
                     f"({behind:.0f}s old) two minutes after the {close_at:%H:%M} close. Entry alerts are "
                     "held back rather than calculated on unfinished candles; if this continues, "
                     "few or no alerts will arrive today.", dry_run=_dry()):
        stats["feed_warned"] = True
        _save(st)


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
    # An EMA9 exit is decided by the candle's CLOSE: never read it half-published.
    # Older candles are still replayed, so a delayed feed postpones a hit alert
    # by a candle instead of losing it.
    waiting, _ = _unsettled(data.fetch_many(symbols.values(), "1d", "1m"),
                            symbols.values(), bar + timedelta(minutes=5))
    pending: list[str] = []
    changed = False
    session = session_date(bar)
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

    # The next early (pre-close) pass scans this pick; it must be this bar's.
    scanning = md.pick_sector != "WAIT" and md.action != "WAIT" and md.pick_side in (1, -1)
    st["scan"] = {"bar": md.bar_time, "sector": md.pick_sector if scanning else "WAIT",
                  "side": md.pick_side if scanning else 0,
                  "label": md.dir_label, "conf": md.dir_conf}

    if not allow_stale and (st.get("stats") or {}).get("bar") != md.bar_time:
        st.setdefault("stats", {})["bar"] = md.bar_time      # count each candle once
        _bump(st, "candles")
        _bump(st, "picks", 1 if scanning else 0)

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
        if not allow_stale:
            _settle_early(st, last, set(), "market/sector pick dropped to WAIT")
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

    want_buy = md.pick_side == 1
    new_signals: list[dict] = []
    handled: set[str] = set(missing)

    for raw, ysym in zip(stocks, tickers):
        if ysym not in current:
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
            if fires:
                log.info("SKIP historical/wrong-side %s fires=%s; decision bar=%s",
                         raw, ",".join(f"{s.name}@{s.time}" for s in fires),
                         last.strftime("%H:%M"))
            if early:
                _cancel_early(st, raw, early, "did not hold", close=float(df5["Close"].iloc[-1]))
            continue
        best = fresh[-1]
        if not allow_stale and not _recent_bar(last, now_ist()):
            log.warning("scan finished too late for %s @ %s; dropping entry", raw, best.time)
            continue
        plan = build_plan(best, df5, last, sd1.get(ysym))
        if plan is None:
            log.warning("SKIP %s @ %s: missing EMA9 or EMA9 stop not beyond entry close", raw, best.time)
            if early:
                _cancel_early(st, raw, early, "has no valid EMA9 stop",
                              close=float(df5["Close"].iloc[-1]))
            continue
        best.price = plan.entry
        best.sl = plan.sl
        text = format_stock(best, md, plan)
        if early:
            text = _confirm_header(early, plan.entry) + text
        if not telegram.send(text, dry_run=_dry()):
            log.warning("Telegram failed for %s; leaving unsent for retry", raw)
            continue
        if early:
            early["result"] = "confirmed"
            _bump(st, "confirmed")
        lag = max(0.0, (now_ist() - close_at).total_seconds())
        _bump(st, "entries")
        _bump(st, "lag_sum", lag)
        st["stats"]["lag_max"] = max(st["stats"].get("lag_max", 0), lag)
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

    if not allow_stale:
        # Early alerts on names the close scan never looked at (sector switched).
        _settle_early(st, last, handled, "is no longer in the scanned sector/side")
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
                                  result="unverified", older=True)
        except Exception:  # never mask the scan's own outcome
            log.exception("early-alert settlement failed")


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
        if (result is not None and result.get("market") == "CLOSED"
                and expected_bar.time() >= clock_time(9, 30)):
            break  # no bar at all today by 09:35 = holiday; polling cannot help
        delay = _retry_delay(close_time, remaining)
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
                text = None if (st1.get("stats") or {}).get("reported") else format_summary(st1)
                if text and telegram.send(text, dry_run=_dry()):
                    st1["stats"]["reported"] = True
                    _save(st1)
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
