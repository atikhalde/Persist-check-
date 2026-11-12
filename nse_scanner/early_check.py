"""Measure early (pre-close) alerts against the close-confirmed signal.

    python -m nse_scanner early-check --date 2026-11-12 --sector PSUBK --lead 60

For every stock of the sector the session is replayed candle by candle exactly
as the live loop sees it:

* ``lead`` seconds before each 5m close the forming candle is rebuilt from the
  1m bars published so far and the unchanged stock engine is run on it
  (the EARLY alert);
* at the close the same engine runs on the completed candle (today's alert).

The report says how often an early alert was confirmed by its own close, how
often it was cancelled, how many close signals had no early warning, and what
the price difference was. It needs Yahoo 1m history, so only the last ~7
calendar days can be checked. The market/sector lock is NOT applied here: this
isolates one question — is the forming candle a trustworthy preview of its
close — over the largest possible sample.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

from . import data, stock
from .session import session_date

log = logging.getLogger("nse_scanner.early_check")


def _fires_on(raw: str, df5, d1, hhmm: str, clock) -> list:
    """Engine fires on the last candle, with 'now' pinned to the replayed moment."""
    real = stock.now_ist
    stock.now_ist = lambda: clock          # today's daily candle must stay "forming"
    try:
        return [s for s in stock.scan_stock(raw, df5, d1, keep_all=True) if s.time == hhmm]
    finally:
        stock.now_ist = real


def replay_symbol(raw: str, df5: pd.DataFrame, df1m: pd.DataFrame, d1: pd.DataFrame | None,
                  day: date, lead: int = 60) -> list[dict]:
    """Rows of {symbol, bar, kind, side, name, early_px, close_px, gain_pct}."""
    if df5 is None or df5.empty or df1m is None or df1m.empty:
        return []
    if d1 is not None and not d1.empty:
        d1 = d1.loc[[session_date(t) < day for t in d1.index]]
    df5 = df5.loc[[session_date(t) <= day for t in df5.index]]
    df1m = df1m.loc[[session_date(t) <= day for t in df1m.index]]
    full = data.apply_1m_volume(df5, df1m)
    rows: list[dict] = []
    early: dict | None = None        # the one early alert of the session
    early_used = False
    for ts in [t for t in df5.index if session_date(t) == day]:
        close_at = ts + timedelta(minutes=5)
        hhmm = ts.strftime("%H:%M")
        pending = None
        if not early_used:
            seen = close_at - timedelta(seconds=lead)
            prov = data.provisional_5m(df5, df1m.loc[df1m.index < seen], ts, seen)
            if prov is not None:
                fires = _fires_on(raw, data.apply_1m_volume(prov, df1m.loc[df1m.index < seen]),
                                  d1, hhmm, seen)
                if fires:
                    early_used = True
                    pending = early = {"side": fires[-1].side, "name": fires[-1].name,
                                       "px": float(prov["Close"].iloc[-1])}
        final = _fires_on(raw, full.loc[full.index <= ts], d1, hhmm, close_at)
        close_px = float(full.loc[ts, "Close"])
        base = {"symbol": raw, "bar": hhmm, "close_px": close_px}
        if pending and final and final[-1].side == pending["side"]:
            sign = 1.0 if pending["side"] == "BUY" else -1.0
            rows.append({**base, "kind": "confirmed", "side": pending["side"], "name": final[-1].name,
                         "early_px": pending["px"],
                         "gain_pct": sign * (close_px - pending["px"]) / pending["px"] * 100.0})
        elif pending:
            rows.append({**base, "kind": "cancelled", "side": pending["side"], "name": pending["name"],
                         "early_px": pending["px"], "gain_pct": None})
        if final and not (pending and final[-1].side == pending["side"]):
            rows.append({**base, "kind": "close-only", "side": final[-1].side, "name": final[-1].name,
                         "early_px": None, "gain_pct": None})
        if final:
            break                    # live: one entry per stock per session
    return rows


def run(day: date, sector: str | None, lead: int = 60) -> list[dict]:
    uni = data.load_universe()
    names = [sector.upper()] if sector else list(uni["sectors"])
    stocks = list(dict.fromkeys(s for n in names for s in uni["sectors"][n]["stocks"]))
    tick = {raw: data.yahoo_stock(raw) for raw in stocks}
    log.info("fetching 5m / 1m / 1d for %d stocks", len(stocks))
    m5 = data.fetch_many(tick.values(), "7d", "5m")
    m1 = data.fetch_many(tick.values(), "7d", "1m")
    d1 = data.fetch_many(tick.values(), "90d", "1d")
    rows: list[dict] = []
    for i, (raw, ysym) in enumerate(tick.items(), 1):
        log.info("replaying %s (%d/%d)", raw, i, len(tick))
        rows += replay_symbol(raw, m5.get(ysym), m1.get(ysym), d1.get(ysym), day, lead)
    return rows


def render(rows: list[dict], day: date, lead: int) -> str:
    out = [f"EARLY-ALERT CHECK  {day}  (forming candle judged {lead}s before its close)", ""]
    out.append(f"{'stock':12} {'bar':5} {'result':10} {'side':4} {'signal':12} "
               f"{'early px':>10} {'close px':>10} {'close vs early':>14}")
    for r in sorted(rows, key=lambda r: (r["bar"], r["symbol"])):
        early = f"{r['early_px']:.2f}" if r["early_px"] is not None else "-"
        gain = f"{r['gain_pct']:+.2f}%" if r["gain_pct"] is not None else "-"
        out.append(f"{r['symbol']:12} {r['bar']:5} {r['kind']:10} {r['side']:4} {r['name']:12} "
                   f"{early:>10} {r['close_px']:>10.2f} {gain:>14}")
    conf = [r for r in rows if r["kind"] == "confirmed"]
    canc = [r for r in rows if r["kind"] == "cancelled"]
    only = [r for r in rows if r["kind"] == "close-only"]
    sent = len(conf) + len(canc)
    out.append("")
    out.append(f"early alerts sent      : {sent}")
    if sent:
        out.append(f"  confirmed at close   : {len(conf)} ({len(conf) / sent * 100:.0f}%)")
        out.append(f"  cancelled at close   : {len(canc)} ({len(canc) / sent * 100:.0f}%)")
    closes = len(conf) + len(only)
    if closes:
        out.append(f"close signals          : {closes}, of which {len(conf)} "
                   f"({len(conf) / closes * 100:.0f}%) had an early alert")
    if conf:
        gains = sorted(r["gain_pct"] for r in conf)
        out.append(f"close vs early price   : median {gains[len(gains) // 2]:+.2f}%  "
                   f"min {gains[0]:+.2f}%  max {gains[-1]:+.2f}%  "
                   "(+ = the close was already further along; that is what waiting costs)")
    if not rows:
        out.append("no early or close signal on this session for these stocks")
    return "\n".join(out)
