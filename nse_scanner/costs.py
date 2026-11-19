"""What a trade costs, and what it left behind — the ledger's arithmetic.

An entry alert has always spoken in R and in %. Neither is money: the report has
to say what the day earned, and a paper P&L that ignores brokerage, taxes and
slippage flatters every scalp — a 0.3R winner on a ₹1,00,000 position is ₹300
gross and less than half of that net.

The model is deliberately one knob, not a tax table. ``COST_BPS`` is the all-in
cost **per side** as a fraction of turnover: on NSE intraday equity that is
brokerage (0.03%), STT on the sell (0.025%), exchange fee (0.00325%), GST on
brokerage+fees, stamp duty on the buy and SEBI charges — about 6 bps round trip
— plus the spread and the entry alert's own slippage. ``COST_MIN`` is the floor
per order (₹20 at most discount brokers), so a small position is not costed as if
it were free.
"""
from __future__ import annotations

import os


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def cost_bps() -> float:
    """All-in cost per side, in basis points of turnover (``COST_BPS``, 5)."""
    return max(0.0, _num("COST_BPS", 5.0))


def cost_min() -> float:
    """Floor cost per order, rupees (``COST_MIN``, 20)."""
    return max(0.0, _num("COST_MIN", 20.0))


def trade_capital() -> float:
    """Notional one entry is sized at, rupees (``TRADE_CAPITAL``, 100000)."""
    return max(1.0, _num("TRADE_CAPITAL", 100_000.0))


def quantity(entry: float) -> int:
    """Whole shares one entry buys at ``entry`` for ``TRADE_CAPITAL``."""
    try:
        qty = int(trade_capital() // float(entry))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0
    return max(0, qty)


def round_trip(side: str, entry: float, exit: float, qty: int) -> dict:
    """Gross, cost and net for one closed trade.

    ``side`` is the trade's own (``BUY`` / ``SELL``): a short sells first and
    buys back, so its gross is ``entry - exit``.
    """
    entry, exit = float(entry or 0.0), float(exit or 0.0)
    qty = int(qty or 0)
    long = str(side).upper() != "SELL"
    gross = ((exit - entry) if long else (entry - exit)) * qty
    buy_turn = entry * qty
    sell_turn = exit * qty
    per_side = cost_bps() / 10_000.0
    costs = max(buy_turn * per_side, cost_min()) + max(sell_turn * per_side, cost_min())
    return {
        "qty": qty,
        "gross": round(gross, 2),
        "costs": round(costs, 2),
        "net": round(gross - costs, 2),
        "bps": cost_bps(),
    }


def headline(records: list[dict]) -> str:
    """``4 trades · gross ₹1,240 · costs ₹96 · net ₹1,144`` for the report."""
    if not records:
        return ""
    gross = sum(float(r.get("gross") or 0) for r in records)
    costs = sum(float(r.get("costs") or 0) for r in records)
    net = sum(float(r.get("net") or 0) for r in records)
    return (f"{len(records)} trade(s) · gross ₹{gross:,.0f} · costs ₹{costs:,.0f} · "
            f"net ₹{net:,.0f}")
