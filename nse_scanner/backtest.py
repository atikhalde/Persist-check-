"""Replay one NSE session bar-by-bar (market lock + stock fires)."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import date, datetime

import pandas as pd

from . import data
from .data import sanitize_index_5m
from .market import SECTOR_ORDER, run_market
from .session import IST, session_date, to_ist
from .stock import scan_stock, table_from_fires

log = logging.getLogger("nse_scanner.backtest")


def _slice(df: pd.DataFrame | None, ts) -> pd.DataFrame | None:
    if df is None or df.empty:
        return df
    return df.loc[:ts]


def replay_session(target: date) -> dict:
    uni = data.load_universe()
    nifty_sym = uni["nifty"]
    vix_sym = uni["vix"]
    index_map = {name: uni["sectors"][name]["index"] for name in SECTOR_ORDER if name in uni["sectors"]}

    log.info("fetching indices")
    idx = [nifty_sym, vix_sym] + list(index_map.values())
    m5 = data.fetch_many(idx, "7d", "5m")
    d1 = data.fetch_many(idx, "90d", "1d")
    nifty_5 = sanitize_index_5m(m5.get(nifty_sym))
    if nifty_5 is None or nifty_5.empty:
        raise RuntimeError("No Nifty 5m data")
    for n, sym in index_map.items():
        if m5.get(sym) is not None:
            m5[sym] = sanitize_index_5m(m5[sym])

    day_bars = nifty_5[[session_date(t) == target for t in nifty_5.index]]
    if day_bars.empty:
        raise RuntimeError(f"No Nifty 5m bars on {target}")

    path = []
    log.info("replaying %d Nifty 5m bars on %s", len(day_bars), target)
    for i, ts in enumerate(day_bars.index):
        sector_5 = {n: _slice(m5.get(sym), ts) for n, sym in index_map.items()}
        sector_d = {n: d1.get(sym) for n, sym in index_map.items()}
        md = run_market(
            _slice(nifty_5, ts),
            d1.get(nifty_sym),
            _slice(m5.get(vix_sym), ts),
            d1.get(vix_sym),
            sector_5,
            sector_d,
        )
        rec = {
            "time": to_ist(ts).strftime("%H:%M"),
            "dir": md.dir_label,
            "conf": md.dir_conf,
            "regime": md.regime,
            "pick": md.pick_sector,
            "side": md.pick_side,
            "action": md.action,
            "reason": md.pick_reason,
            "persist_plus": md.persist_plus,
            "persist_minus": md.persist_minus,
            "live_flow": md.live_flow,
            "nifty": md.nifty,
            "nifty_day_ret": md.nifty_day_ret,
            "vix_chg": md.vix_chg,
            "breadth": f"{md.breadth_adv}/{md.breadth_dec}",
        }
        path.append(rec)
        if i == 0 or i == len(day_bars) - 1 or rec["pick"] != path[max(0, i - 1)]["pick"] or rec["dir"] != path[max(0, i - 1)]["dir"]:
            log.info("%s  %s conf=%s  pick=%s  %s", rec["time"], rec["dir"], rec["conf"], rec["pick"], rec["action"])

    # dominant complementary pick after 09:45
    after = [r for r in path if r["time"] >= "09:45" and r["pick"] != "WAIT"]
    wait_n = sum(1 for r in path if r["time"] >= "09:45" and r["pick"] == "WAIT")
    counts = Counter((r["pick"], r["side"]) for r in after)
    dominant = counts.most_common(1)[0][0] if counts else ("WAIT", 0)

    episodes = []
    if path:
        cur = path[0]
        start = cur["time"]
        for r in path[1:]:
            if r["pick"] != cur["pick"] or r["side"] != cur["side"] or r["dir"] != cur["dir"]:
                episodes.append({**cur, "start": start, "end": r["time"]})
                cur = r
                start = r["time"]
        episodes.append({**cur, "start": start, "end": path[-1]["time"]})

    pick_name, pick_side = dominant
    stocks_by_sector: dict[str, list] = {}

    def _pack(s, sec_code: str) -> dict:
        return {
            "symbol": s.symbol,
            "code": s.code,
            "name": s.name,
            "side": s.side,
            "time": s.time,
            "price": s.price,
            "score": s.score,
            "strength": s.strength,
            "reason": s.reason,
            "sl": s.sl,
            "sector": sec_code,
        }

    def scan_sector(sec_code: str) -> tuple[list[dict], list[dict]]:
        """Return (TV-table rows, all plotshape fires) for this sector on target date."""
        if sec_code not in uni["sectors"]:
            return [], []
        names = uni["sectors"][sec_code]["stocks"]
        tickers = [data.yahoo_stock(s) for s in names]
        log.info("scanning %s (%d stocks) on %s", sec_code, len(names), target)
        sm5 = data.fetch_many(tickers, "30d", "5m")
        sm1 = data.fetch_many(tickers, "7d", "1m")
        sd1 = data.fetch_many(tickers, "90d", "1d")
        table, fires = [], []
        for raw, ysym in zip(names, tickers):
            df5 = sm5.get(ysym)
            if df5 is None or df5.empty:
                continue
            df5 = data.apply_1m_volume(df5, sm1.get(ysym))
            try:
                all_f = scan_stock(raw, df5, sd1.get(ysym), keep_all=True, as_of=target)
            except Exception as e:
                log.warning("scan %s failed: %s", raw, e)
                continue
            rows = table_from_fires(all_f, "15:25")
            table.extend(_pack(s, sec_code) for s in rows)
            fires.extend(_pack(s, sec_code) for s in all_f)
        table.sort(key=lambda x: x["time"])
        fires.sort(key=lambda x: x["time"])
        return table, fires

    official = []
    fires_by_sector: dict[str, list] = {}
    if pick_name != "WAIT":
        table, fires = scan_sector(pick_name)
        stocks_by_sector[pick_name] = table
        fires_by_sector[pick_name] = fires
        want = "BUY" if pick_side == 1 else "SELL"
        live_times = {r["time"] for r in path if r["pick"] == pick_name and r["side"] == pick_side}
        for s in table:
            if s["side"] != want:
                continue
            if s["time"] in live_times or not live_times:
                official.append(s)

    # diagnostic: persist leaders at the last bar
    last = path[-1]
    diag_secs = []
    for n, _sc in (last["persist_plus"][:1] + last["persist_minus"][:1]):
        if n not in diag_secs:
            diag_secs.append(n)
    if last["live_flow"] and last["live_flow"] not in diag_secs:
        diag_secs.append(last["live_flow"])
    diagnostic = {}
    diagnostic_fires = {}
    for sec in diag_secs:
        if sec not in stocks_by_sector:
            table, fires = scan_sector(sec)
            stocks_by_sector[sec] = table
            fires_by_sector[sec] = fires
        diagnostic[sec] = stocks_by_sector[sec]
        diagnostic_fires[sec] = fires_by_sector.get(sec, [])

    return {
        "date": str(target),
        "path": path,
        "episodes": episodes,
        "dominant": {"sector": pick_name, "side": pick_side, "bars": counts.get(dominant, 0), "wait_bars": wait_n},
        "official": official,
        "diagnostic": diagnostic,
        "diagnostic_fires": diagnostic_fires,
        "last": last,
    }


def render_report(res: dict) -> str:
    last = res["last"]
    dom = res["dominant"]
    side_txt = "LONG" if dom["side"] == 1 else "SHORT" if dom["side"] == -1 else "—"
    lines = []
    lines.append(f"MOCK TEST  {res['date']}  (yfinance 5m, IST)")
    lines.append("=" * 72)
    lines.append("EOD snapshot")
    lines.append(f"  Nifty {last['nifty']:,.1f}  day {last['nifty_day_ret']:+.2f}%  VIX Δ {last['vix_chg']:+.2f}%")
    lines.append(f"  DIR {last['dir']}  conf {last['conf']}%  regime {last['regime']}  breadth {last['breadth']}")
    plus = " · ".join(f"{n} {s:.0f}" for n, s in last["persist_plus"])
    minus = " · ".join(f"{n} {s:.0f}" for n, s in last["persist_minus"])
    lines.append(f"  PERSIST +  {plus}")
    lines.append(f"  PERSIST −  {minus}")
    lines.append(f"  Live flow  {last['live_flow']}")
    lines.append("")
    lines.append("Sector lock through the day (changes only)")
    lines.append("-" * 72)
    for e in res["episodes"]:
        st = "LONG" if e["side"] == 1 else "SHORT" if e["side"] == -1 else "—"
        lines.append(
            f"  {e['start']}–{e['end']}  DIR {e['dir']:<12}  PICK {e['pick']:<6} {st:<5}  {e['action']}"
        )
    lines.append("")
    lines.append("OFFICIAL PIPELINE (complementary persist only — what Telegram would send)")
    lines.append("-" * 72)
    if dom["sector"] == "WAIT":
        lines.append(f"  SECTOR: WAIT  (no complementary lock; wait bars after 09:45 = {dom['wait_bars']})")
        lines.append(f"  Reason: {last['reason']}")
        lines.append("  STOCK ALERTS: none")
    else:
        lines.append(f"  SECTOR: {dom['sector']}  {side_txt}  ({dom['bars']} bars)")
        if not res["official"]:
            lines.append("  STOCK ALERTS: none in this sector / side while locked")
        else:
            lines.append(f"  STOCK ALERTS: {len(res['official'])}")
            for s in res["official"]:
                lines.append(
                    f"    {s['time']}  {s['side']:<4}  {s['name']:<12}  {s['symbol']:<12}  {s['price']}  score {s['score']}"
                )
    lines.append("")
    lines.append("DIAGNOSTIC — TV TABLE rows (one per stock: first hit, rank upgrade, 90/290 cleared after 09:45)")
    lines.append("-" * 72)
    for sec, sigs in res["diagnostic"].items():
        lines.append(f"  {sec}: {len(sigs)} table row(s)")
        if not sigs:
            lines.append("    (empty table — no remaining code after 09:45)")
        for s in sigs:
            lines.append(
                f"    {s['time']}  {s['side']:<4}  {s['name']:<12}  {s['symbol']:<12}  {s['price']}  {s['reason']}"
            )
    lines.append("")
    lines.append("DIAGNOSTIC — ALL plotshape fires (including 90/290 that TV table later clears)")
    lines.append("-" * 72)
    for sec, sigs in res.get("diagnostic_fires", {}).items():
        lines.append(f"  {sec}: {len(sigs)} fire(s)")
        if not sigs:
            lines.append("    (none)")
        for s in sigs:
            lines.append(
                f"    {s['time']}  {s['side']:<4}  {s['name']:<12}  {s['symbol']:<12}  {s['price']}  {s['reason']}"
            )
    lines.append("")
    lines.append(f"Compare TABLE rows (code + time) with the TradingView master-scanner table on {res['date']}.")
    return "\n".join(lines)
