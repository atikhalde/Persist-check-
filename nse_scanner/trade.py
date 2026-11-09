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
from .session import session_date
from .stock import StockSignal, fill_nse_grid

TICK = Decimal("0.01")
BUFFER = Decimal("0.0002")  # 0.02%, not 2%


@dataclass(frozen=True)
class TradePlan:
    entry: float
    ema9: float
    sl: float
    risk: float
    t1: float | None
    t2: float | None
    t3: float | None
    pivot_names: tuple[str | None, ...] = (None, None, None)


def ema9_on_bar(df5: pd.DataFrame, bar) -> float | None:
    """Return EMA9 using the same filled 5m grid as the stock scanner."""
    if df5 is None or df5.empty:
        return None
    x = fill_nse_grid(df5.loc[df5.index <= bar])
    if x is None or x.empty or x.index[-1] != bar:
        return None
    value = float(ta.ema(x["Close"].to_numpy(float), 9)[-1])
    return value if isfinite(value) and value > 0 else None


def named_pivot_targets(side: str, entry: float, df5: pd.DataFrame, bar,
                  df1: pd.DataFrame | None = None) -> list[tuple[str | None, float | None]]:
    """Next three classic daily pivots, based only on the previous session.

    Never substitute R multiples or invent levels beyond S3/R3. Missing or
    exhausted pivots remain unavailable; EMA9 trailing still continues.
    """
    today = session_date(bar)
    sessions = {}
    for day, group in df5.sort_index().groupby([session_date(t) for t in df5.sort_index().index]):
        if day < today:
            sessions[day] = (float(group["High"].max()), float(group["Low"].min()),
                             float(group["Close"].iloc[-1]))
    if df1 is not None and not df1.empty:
        for t, row in df1.sort_index().iterrows():
            day = session_date(t)
            if day < today:
                sessions[day] = tuple(float(row[k]) for k in ("High", "Low", "Close"))
    if not sessions:
        return [(None, None)] * 3
    h, l, c = sessions[max(sessions)]
    if not all(isfinite(v) and v > 0 for v in (h, l, c)) or h < l or not l <= c <= h:
        return [(None, None)] * 3
    pp = (h + l + c) / 3
    levels = [l - 2 * (h - pp), pp - (h - l), 2 * pp - h,
              pp, 2 * pp - l, pp + (h - l), h + 2 * (pp - l)]
    rounded = {}
    for name, value in zip(("S3", "S2", "S1", "PP", "R1", "R2", "R3"), levels):
        if value > 0:
            price = float(Decimal(str(value)).quantize(TICK, rounding=ROUND_HALF_UP))
            rounded.setdefault(price, []).append(name)
    prices = sorted((v for v in rounded if v > entry)) if side == "BUY" else sorted(
        (v for v in rounded if v < entry), reverse=True)
    targets = [("/".join(rounded[v]), v) for v in prices[:3]]
    return (targets + [(None, None)] * 3)[:3]


def pivot_targets(side: str, entry: float, df5: pd.DataFrame, bar,
                  df1: pd.DataFrame | None = None) -> list[float | None]:
    """Price-only view of the next pivot levels."""
    return [price for _, price in named_pivot_targets(side, entry, df5, bar, df1)]


def build_plan(sig: StockSignal, df5: pd.DataFrame, bar,
               df1: pd.DataFrame | None = None) -> TradePlan | None:
    """Stop below/above entry candle EMA9 by 0.02%, targets at the next daily pivot levels.

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
    elif sig.side == "SELL":
        stop = (level * (1 + BUFFER)).quantize(TICK, rounding=ROUND_CEILING)
        risk = stop - entry
    else:
        return None
    if risk <= 0:
        return None
    targets = named_pivot_targets(sig.side, float(entry), df5, bar, df1)
    return TradePlan(float(entry), ema, float(stop), float(risk),
                     *(price for _, price in targets),
                     pivot_names=tuple(name for name, _ in targets))


def trail_broken(side: str, close: float, ema: float) -> bool:
    """Only a completed candle CLOSE beyond EMA9 triggers an exit alert."""
    return close < ema if side == "BUY" else close > ema if side == "SELL" else False
