"""5m entry risk levels and close-confirmed EMA9 trailing exits.

Levels are informational (not broker orders or intrabar stop executions).
The entry signal is evaluated only on a completed, real Yahoo 5m candle.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from math import isfinite

import pandas as pd

from . import ta
from .stock import StockSignal, fill_nse_grid

TICK = Decimal("0.01")
BUFFER = Decimal("0.0002")  # 0.02%, not 2%


@dataclass(frozen=True)
class TradePlan:
    entry: float
    ema9: float
    sl: float
    risk: float
    t1: float
    t2: float
    t3: float


def ema9_on_bar(df5: pd.DataFrame, bar) -> float | None:
    """Return EMA9 using the same filled 5m grid as the stock scanner."""
    if df5 is None or df5.empty:
        return None
    x = fill_nse_grid(df5.loc[df5.index <= bar])
    if x is None or x.empty or x.index[-1] != bar:
        return None
    value = float(ta.ema(x["Close"].to_numpy(float), 9)[-1])
    return value if isfinite(value) and value > 0 else None


def build_plan(sig: StockSignal, df5: pd.DataFrame, bar) -> TradePlan | None:
    """Stop below/above entry candle EMA9 by 0.02%, targets at 1/2/3 R.

    Round stop *away* from EMA9 to the 0.01 price tick. No entry alert is
    actionable if this stop is on the wrong side of the entry close.
    """
    if df5 is None or df5.empty or df5.index[-1] != bar:
        return None
    close = float(df5["Close"].iloc[-1])
    if not isfinite(close) or round(close, 2) != sig.price:
        return None
    ema = ema9_on_bar(df5, bar)
    if ema is None:
        return None
    entry = Decimal(str(sig.price)).quantize(TICK)
    level = Decimal(str(ema))
    if sig.side == "BUY":
        stop = (level * (1 - BUFFER)).quantize(TICK, rounding=ROUND_FLOOR)
        risk = entry - stop
        direction = 1
    elif sig.side == "SELL":
        stop = (level * (1 + BUFFER)).quantize(TICK, rounding=ROUND_CEILING)
        risk = stop - entry
        direction = -1
    else:
        return None
    if risk <= 0:
        return None
    targets = [(entry + direction * risk * n).quantize(TICK, rounding=ROUND_HALF_UP)
               for n in (1, 2, 3)]
    return TradePlan(float(entry), ema, float(stop), float(risk), *map(float, targets))


def trail_broken(side: str, close: float, ema: float) -> bool:
    """Only a completed candle CLOSE beyond EMA9 triggers an exit alert."""
    return close < ema if side == "BUY" else close > ema if side == "SELL" else False
