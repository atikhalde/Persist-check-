"""The trader's ledger: every trade in rupees, with costs, and the day's book.

An R multiple tells you whether the *rule* worked. It does not tell you what the
day paid. This module turns the session's trade records into the thing a trader
actually keeps: one row per position with the size, both prices, the costs and
the money; then the totals a desk asks for (win rate, expectancy, profit factor,
the equity curve and its worst drawdown).

Nothing here is invented. Rows come from ``stats.closed`` (filed by
``pipeline._close_trade`` at the exit alert) and from the positions still open at
the close, marked with their last checked candle. Costs come from ``costs.py``,
whose one knob (``COST_BPS``) is printed with every report.
"""
from __future__ import annotations

from . import costs


def _minutes(entry_bar, exit_time) -> float | None:
    """How long the trade was held, from its entry candle to its exit candle."""
    try:
        start = str(entry_bar or "")
        end = str(exit_time or "")
        if "T" not in start or ":" not in end:
            return None
        h0, m0 = int(start.split("T")[1][:2]), int(start.split("T")[1][3:5])
        h1, m1 = int(end[:2]), int(end[3:5])
        return float((h1 * 60 + m1) - (h0 * 60 + m0))
    except (ValueError, IndexError):
        return None


def closed_rows(st: dict) -> list[dict]:
    """One row per closed trade, in the order they closed, with a running total."""
    rows: list[dict] = []
    cum = 0.0
    for rec in (st.get("stats") or {}).get("closed") or []:
        entry = float(rec.get("entry") or 0.0)
        exit_ = float(rec.get("exit") or 0.0)
        qty = int(rec.get("qty") or costs.quantity(entry))
        risk = float(rec.get("risk") or 0.0)
        if not risk and rec.get("stop0") is not None:
            # A record written before ``risk`` was stored still knows its stop, so
            # the rupee risk (and every figure derived from it) is recoverable.
            try:
                risk = abs(entry - float(rec["stop0"]))
            except (TypeError, ValueError):
                risk = 0.0
        money = costs.round_trip(rec.get("side") or "BUY", entry, exit_, qty)
        gross = float(rec.get("gross", money["gross"]))
        net = float(rec.get("net", money["net"]))
        cum += net
        long = str(rec.get("side") or "BUY").upper() != "SELL"
        move_pct = ((exit_ - entry) / entry * 100.0) if entry else 0.0
        if not long:
            move_pct = -move_pct
        rows.append({
            "name": rec.get("raw"), "sector": rec.get("sector") or "",
            "side": rec.get("side"), "qty": qty,
            "entry_at": rec.get("entry_bar"), "entry": round(entry, 2),
            "stop0": rec.get("stop0"), "risk_per_share": round(risk, 2),
            "risk_rs": round(risk * qty, 2),
            "t1": rec.get("t1"), "t2": rec.get("t2"), "t3": rec.get("t3"),
            "exit_at": rec.get("time"), "exit": round(exit_, 2),
            "why": rec.get("why"), "targets_hit": rec.get("t_hit") or 0,
            "hold_min": _minutes(rec.get("entry_bar"), rec.get("time")),
            "move_pct": round(move_pct, 2),
            "gross_rs": round(gross, 2), "costs_rs": round(float(rec.get("costs",
                                                                        money["costs"])), 2),
            "net_rs": round(net, 2),
            "gross_r": round(float(rec.get("r") or 0.0), 2),
            "net_r": round(float(rec.get("net_r") if rec.get("net_r") is not None
                                 else (net / (risk * qty) if risk and qty else 0.0)), 2),
            "cum_rs": round(cum, 2),
        })
    return rows


def open_rows(st: dict) -> list[dict]:
    """The positions still open at the close, marked and costed as if exited."""
    rows = []
    for raw, pos in (st.get("positions") or {}).items():
        entry = float(pos.get("entry") or 0.0)
        last = pos.get("last_close")
        qty = int(pos.get("qty") or costs.quantity(entry))
        risk = float(pos.get("risk") or 0.0)
        if last is None:
            rows.append({"name": raw, "side": pos.get("side"), "qty": qty,
                         "entry": round(entry, 2), "mark": None,
                         "gross_rs": None, "costs_rs": None, "net_rs": None,
                         "gross_r": None, "net_r": None, "why": "no candle after the entry",
                         "sector": pos.get("sector") or "", "targets_hit":
                         len(pos.get("hit_targets") or ())})
            continue
        mark = float(last)
        money = costs.round_trip(pos.get("side") or "BUY", entry, mark, qty)
        long = str(pos.get("side") or "BUY").upper() != "SELL"
        r = ((mark - entry) if long else (entry - mark)) / risk if risk else 0.0
        rows.append({
            "name": raw, "side": pos.get("side"), "qty": qty,
            "entry": round(entry, 2), "mark": round(mark, 2),
            "stop": pos.get("sl"), "risk_rs": round(risk * qty, 2),
            "gross_rs": money["gross"], "costs_rs": money["costs"], "net_rs": money["net"],
            "gross_r": round(r, 2),
            "net_r": round(money["net"] / (risk * qty), 2) if risk and qty else 0.0,
            "targets_hit": len(pos.get("hit_targets") or ()),
            "sector": pos.get("sector") or "", "why": "still open",
        })
    return rows


def _drawdown(curve: list[float]) -> tuple[float, float]:
    """Worst peak-to-trough of the running net, and the peak it fell from."""
    peak, worst, from_peak = 0.0, 0.0, 0.0
    for value in curve:
        peak = max(peak, value)
        if peak - value > worst:
            worst, from_peak = peak - value, peak
    return round(worst, 2), round(from_peak, 2)


def totals(rows: list[dict], opened: list[dict] | None = None) -> dict:
    """The day's book: money, hit rate, expectancy, profit factor, drawdown."""
    net = [float(r["net_rs"] or 0.0) for r in rows]
    gross = [float(r["gross_rs"] or 0.0) for r in rows]
    cost = [float(r["costs_rs"] or 0.0) for r in rows]
    wins = [v for v in net if v > 0]
    losses = [v for v in net if v <= 0]
    net_r = [float(r["net_r"] or 0.0) for r in rows]
    curve, running = [], 0.0
    for value in net:
        running += value
        curve.append(round(running, 2))
    worst, peak = _drawdown(curve or [0.0])
    holds = [float(r["hold_min"]) for r in rows if r.get("hold_min") is not None]
    open_net = sum(float(r["net_rs"] or 0.0) for r in (opened or []))
    risk = sum(float(r.get("risk_rs") or 0.0) for r in rows if r.get("risk_rs"))
    return {
        "trades": len(rows),
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(100.0 * len(wins) / len(rows), 1) if rows else 0.0,
        "gross_rs": round(sum(gross), 2), "costs_rs": round(sum(cost), 2),
        "net_rs": round(sum(net), 2),
        "net_r": round(sum(net_r), 2),
        "avg_win_rs": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss_rs": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "avg_net_r": round(sum(net_r) / len(net_r), 2) if net_r else 0.0,
        "profit_factor": (round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses)
                          else (None if wins else 0.0)),
        "best_rs": round(max(net), 2) if net else 0.0,
        "worst_rs": round(min(net), 2) if net else 0.0,
        "max_dd_rs": worst, "peak_rs": peak,
        "equity": curve,
        "risk_rs": round(risk, 2),
        "return_on_risk_pct": round(100.0 * sum(net) / risk, 1) if risk else 0.0,
        "open_trades": len(opened or []), "open_net_rs": round(open_net, 2),
        "avg_hold_min": round(sum(holds) / len(holds), 0) if holds else None,
    }


def summary_line(t: dict) -> str:
    """One Telegram line for the whole book."""
    if not t or not t.get("trades"):
        base = "No trade was closed today."
        if t and t.get("open_trades"):
            base += f" {t['open_trades']} still open ({t['open_net_rs']:+,.0f} ₹ marked)."
        return base
    pf = t["profit_factor"]
    return (f"{t['trades']} closed · {t['wins']}W/{t['losses']}L "
            f"({t['win_rate']:.0f}%) · gross ₹{t['gross_rs']:,.0f} − costs "
            f"₹{t['costs_rs']:,.0f} = <b>net ₹{t['net_rs']:,.0f}</b> · "
            f"{t['net_r']:+.1f}R · avg {t['avg_net_r']:+.2f}R · PF "
            f"{pf if pf is not None else '∞'} · worst drawdown ₹{t['max_dd_rs']:,.0f}")
