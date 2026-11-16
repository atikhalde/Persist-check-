"""5m entry risk levels and close-confirmed EMA9 trailing exits.

Levels are informational (not broker orders or intrabar stop executions).
The entry signal is evaluated only on a completed, real Yahoo 5m candle.

Classic daily pivots (TradingView "Traditional") from the previous session:

    PP = (H + L + C) / 3
    R1 = 2PP - L        S1 = 2PP - H
    R2 = PP + (H - L)   S2 = PP - (H - L)
    R3 = H + 2(PP - L)  S3 = L - 2(H - PP)

The levels can only be as sane as the session behind them. Every plan now
carries a :class:`PivotBasis` (the exact session, its OHLC, its range) so the
alert can disclose where the numbers came from and flag an outlier day, rather
than silently printing a level that is many R away because the previous
session happened to be a 5%-range reversal day.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from math import isfinite
from statistics import median

import pandas as pd

from . import ta
from .session import session_date
from .stock import StockSignal, fill_nse_grid

log = logging.getLogger("nse_scanner")

TICK = Decimal("0.01")
BUFFER = Decimal("0.0002")  # 0.02%, not 2%

# --- pivot data-quality gates ------------------------------------------------
PIVOT_RANGE_LOOKBACK = 20   # completed sessions used for the robust range
PIVOT_RANGE_OUTLIER = 2.5   # basis range > this x median -> "wide basis day"
PIVOT_MIN_SAMPLES = 5       # below this the median is not trusted (no verdict)
PIVOT_RANGE_COMPRESSED = 0.4  # basis range < this x median -> special/half session
MAX_BASIS_GAP_DAYS = 7      # stale feed: longest NSE holiday run is well under
FEED_TOLERANCE = 0.005      # 0.5% max 5m-vs-daily disagreement on H/L/C

LEVEL_NAMES = ("S3", "S2", "S1", "PP", "R1", "R2", "R3")


# --- trade-quality gates (16 Nov 2026 feed) -----------------------------------
# Three numbers decide when a 5m trade is worth announcing and when it is over.
# They are knobs so a day can be compared with them on and off.

def _env_float(name: str, default: float, lo: float = 0.0, hi: float = 10_000.0) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if lo <= value <= hi else default


def min_target_r() -> float:
    """A pivot closer than this many R is not a target (``MIN_TARGET_R``, 0.5).

    GODREJCP on 16 Nov was entered at 877.25 with T1 at 878.00 — 75 paise, 0.2R,
    +0.09%. Its "TARGET HIT +0.2R" was noise, and the entry was into immediate
    resistance. Such a level is now skipped and disclosed, and the ladder moves
    to the next pivot.
    """
    return _env_float("MIN_TARGET_R", 0.5)


def min_target_pct() -> float:
    """The same gate as a % of price (``MIN_TARGET_PCT``, 0.10), stop-independent."""
    return _env_float("MIN_TARGET_PCT", 0.10)


def exit_buffer(close: float, risk: float | None) -> float:
    """Noise band around EMA9 before a close counts as a broken trail.

    ``min(EXIT_EMA9_BUFFER_PCT% of price, EXIT_EMA9_BUFFER_R x risk)`` — the
    smaller of a fixed band and one in units of the trade's own risk. 16 Nov:
    AUROPHARMA was let go 24 paise past its EMA9 (1.6% of its stop) and VBL by 4
    paise; nine of ten exits were breaches of 0.009–0.13%.
    """
    pct = _env_float("EXIT_EMA9_BUFFER_PCT", 0.10)
    mult = _env_float("EXIT_EMA9_BUFFER_R", 0.25)
    bands = [close * pct / 100.0]
    if risk:
        bands.append(float(risk) * mult)
    band = min(bands)
    return band if band > 0 else 0.0


def breakeven_after_t1() -> bool:
    """Move the stop to entry once T1 is touched (``BREAKEVEN_AFTER_T1``, on)."""
    return os.environ.get("BREAKEVEN_AFTER_T1", "1") != "0"


def breakeven_stop(side: str, entry: float, sl: float) -> float:
    """The stop to carry after T1: at worst entry, never worse than it was."""
    if side == "BUY":
        return max(float(sl), float(entry))
    if side == "SELL":
        return min(float(sl), float(entry))
    return float(sl)


@dataclass(frozen=True)
class PivotBasis:
    """The completed session the pivot levels are built from."""

    day: date
    high: float
    low: float
    close: float
    source: str                      # "1d" (authoritative) or "5m" (fallback)
    range_pct: float                 # (H - L) / C * 100
    median_range_pct: float | None   # robust baseline; None when too little history

    @property
    def range_multiple(self) -> float | None:
        if not self.median_range_pct:
            return None
        return self.range_pct / self.median_range_pct

    @property
    def special(self) -> bool:
        """Sat/Sun basis = Muhurat / special session, which compresses the range."""
        return self.day.weekday() >= 5

    @property
    def compressed(self) -> bool:
        """A crushed basis day pulls every level into a few ticks."""
        mult = self.range_multiple
        return mult is not None and mult < PIVOT_RANGE_COMPRESSED

    @property
    def wide(self) -> bool:
        """True when this session's range is an outlier vs recent sessions.

        A wide basis day is exactly what stretches R3 far beyond R2, because
        R3 - R2 = PP - L grows with the range. The level is still the correct
        classic pivot; it is just a poor day-trade target.
        """
        mult = self.range_multiple
        return mult is not None and mult > PIVOT_RANGE_OUTLIER


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
    basis: PivotBasis | None = None
    skipped: tuple[str, ...] = field(default=())   # pivots too close to be targets


def ema9_on_bar(df5: pd.DataFrame, bar) -> float | None:
    """Return EMA9 using the same filled 5m grid as the stock scanner."""
    if df5 is None or df5.empty:
        return None
    x = fill_nse_grid(df5.loc[df5.index <= bar])
    if x is None or x.empty or x.index[-1] != bar:
        return None
    value = float(ta.ema(x["Close"].to_numpy(float), 9)[-1])
    return value if isfinite(value) and value > 0 else None


def ema9_frame(df5: pd.DataFrame) -> pd.DataFrame | None:
    """Filled 5m grid with an EMA9 column (causal: matches ema9_on_bar bar by bar)."""
    x = fill_nse_grid(df5)
    if x is None or x.empty:
        return None
    return x.assign(EMA9=ta.ema(x["Close"].to_numpy(float), 9))


def _same_ohlc(a: tuple[float, float, float], b: tuple[float, float, float]) -> bool:
    if not all(isfinite(v) for v in a + b):
        return False
    scale = max(abs(v) for v in b) or 1.0
    return max(abs(x - y) for x, y in zip(a, b)) <= FEED_TOLERANCE * scale


def _previous_sessions(df5: pd.DataFrame, df1: pd.DataFrame | None, today: date):
    """Previous-session OHLC from the 5m aggregates and the daily bars.

    Both feeds are kept: the 5m frame can cover a day the daily feed omits and
    vice versa. Daily bars win when the two disagree, and the disagreement is
    logged — it means a split/bad print/stale feed, which would otherwise move
    every pivot level without a trace.
    """
    from_5m: dict[date, tuple[float, float, float]] = {}
    if df5 is not None and not df5.empty:
        x = df5.sort_index()
        # Group by a real column: pandas 3 turns a one-element list of keys
        # into a 1-tuple key, which used to raise TypeError on a short frame.
        x = x.assign(_sid=[session_date(t) for t in x.index])
        for day, group in x.groupby("_sid", sort=True):
            if day < today:
                vals = (float(group["High"].max()),
                        float(group["Low"].min()),
                        float(group["Close"].iloc[-1]))
                if all(isfinite(v) for v in vals):
                    from_5m[day] = vals
    from_1d: dict[date, tuple[float, float, float]] = {}
    if df1 is not None and not df1.empty:
        for ts, row in df1.sort_index().iterrows():
            day = session_date(ts)
            if day >= today:
                continue  # forming / future daily candle is never a basis
            vals = tuple(float(row[k]) for k in ("High", "Low", "Close"))
            if all(isfinite(v) and v > 0 for v in vals):
                from_1d[day] = vals

    merged = dict(from_5m)
    sources = {day: "5m" for day in from_5m}
    for day, vals in from_1d.items():
        if day in merged and not _same_ohlc(merged[day], vals):
            log.warning("pivot basis %s: 5m %s vs daily %s disagree by >%.1f%%; using daily",
                        day, merged[day], vals, FEED_TOLERANCE * 100)
        merged[day] = vals
        sources[day] = "1d"
    return merged, sources


def _median_range_pct(sessions: dict[date, tuple[float, float, float]],
                      newest: date) -> float | None:
    """Typical (H - L)/C over the sessions before the basis day."""
    pcts = []
    for day in sorted((d for d in sessions if d < newest), reverse=True)[:PIVOT_RANGE_LOOKBACK]:
        h, l, c = sessions[day]
        if c > 0 and h >= l:
            pcts.append((h - l) / c * 100.0)
    if len(pcts) < PIVOT_MIN_SAMPLES:
        return None
    return float(median(pcts))


def pivot_basis(df5: pd.DataFrame, bar, df1: pd.DataFrame | None = None) -> PivotBasis | None:
    """Select and validate the previous completed session for the pivots."""
    today = session_date(bar)
    sessions, sources = _previous_sessions(df5, df1, today)
    if not sessions:
        return None
    newest = max(sessions)
    gap = (today - newest).days
    if gap > MAX_BASIS_GAP_DAYS:
        log.warning("pivot basis %s is %d calendar days before %s — stale feed, no pivots",
                    newest, gap, today)
        return None
    h, l, c = sessions[newest]
    if not all(isfinite(v) and v > 0 for v in (h, l, c)) or h < l or not l <= c <= h:
        log.warning("pivot basis %s has unusable OHLC %s", newest, (h, l, c))
        return None
    basis = PivotBasis(day=newest, high=h, low=l, close=c, source=sources[newest],
                       range_pct=(h - l) / c * 100.0,
                       median_range_pct=_median_range_pct(sessions, newest))
    if basis.wide:
        log.warning("pivot basis %s range %.2f%% is %.1fx the %.2f%% median — extended levels will be far",
                    basis.day, basis.range_pct, basis.range_multiple or 0.0,
                    basis.median_range_pct or 0.0)
    if basis.compressed:
        log.warning("pivot basis %s range %.2f%% is only %.1fx the %.2f%% median — compressed levels",
                    basis.day, basis.range_pct, basis.range_multiple or 0.0,
                    basis.median_range_pct or 0.0)
    return basis


def _all_levels(side: str, entry: float, basis: PivotBasis | None
                ) -> list[tuple[str, float]]:
    """Every classic pivot beyond ``entry``, nearest first (no truncation)."""
    if basis is None:
        return []
    h, l, c = basis.high, basis.low, basis.close
    pp = (h + l + c) / 3.0
    levels = [l - 2 * (h - pp), pp - (h - l), 2 * pp - h,
              pp, 2 * pp - l, pp + (h - l), h + 2 * (pp - l)]
    rounded: dict[float, list[str]] = {}
    for name, value in zip(LEVEL_NAMES, levels):
        if value > 0:
            price = float(Decimal(str(value)).quantize(TICK, rounding=ROUND_HALF_UP))
            rounded.setdefault(price, []).append(name)
    prices = sorted((v for v in rounded if v > entry)) if side == "BUY" else sorted(
        (v for v in rounded if v < entry), reverse=True)
    return [("/".join(rounded[v]), v) for v in prices]


def _keep_targets(side: str, entry: float, risk: float,
                  levels: list[tuple[str, float]]
                  ) -> tuple[list[tuple[str, float]], tuple[str, ...]]:
    """Drop pivots too close to entry, and say which ones.

    A level inside ``MIN_TARGET_R``/``MIN_TARGET_PCT`` of the entry is not a
    trade target: it is the resistance the entry was made into. Announcing it as
    "TARGET HIT +0.2R" is noise, so the ladder starts at the first level that is
    far enough away and every skipped level is disclosed on the alert.
    """
    floor_r, floor_pct = min_target_r(), min_target_pct()
    kept: list[tuple[str, float]] = []
    skipped: list[str] = []
    for name, value in levels:
        delta = abs(value - entry)
        r = delta / risk if risk else None
        pct = delta / entry * 100.0 if entry else 0.0
        if (r is not None and r < floor_r) or pct < floor_pct:
            bits = ([f"{r:.1f}R"] if r is not None else []) + [f"{pct:+.2f}%"]
            skipped.append(f"{name} {value:.2f} ({', '.join(bits)})")
            continue
        kept.append((name, value))
    return kept, tuple(skipped)


def _next_levels(side: str, entry: float, basis: PivotBasis | None
                 ) -> list[tuple[str | None, float | None]]:
    targets = _all_levels(side, entry, basis)[:3]
    return (list(targets) + [(None, None)] * 3)[:3]


def named_pivot_targets(side: str, entry: float, df5: pd.DataFrame, bar,
                        df1: pd.DataFrame | None = None,
                        basis: PivotBasis | None = None
                        ) -> list[tuple[str | None, float | None]]:
    """Next three classic daily pivots, based only on the previous session.

    Never substitute R multiples or invent levels beyond S3/R3. Missing or
    exhausted pivots remain unavailable; EMA9 trailing still continues.
    """
    return _next_levels(side, entry, basis if basis is not None else pivot_basis(df5, bar, df1))


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
    basis = pivot_basis(df5, bar, df1)
    kept, skipped = _keep_targets(sig.side, float(entry), float(risk),
                                  _all_levels(sig.side, float(entry), basis))
    targets = (kept[:3] + [(None, None)] * 3)[:3]
    return TradePlan(float(entry), ema, float(stop), float(risk),
                     *(price for _, price in targets),
                     pivot_names=tuple(name for name, _ in targets),
                     basis=basis, skipped=skipped)


def trail_broken(side: str, close: float, ema: float, risk: float | None = None) -> bool:
    """A completed candle CLOSE beyond EMA9 **and** beyond the noise band.

    The band is :func:`exit_buffer` — 0.10% of price or 0.25R, whichever is
    smaller, so a 4-paise poke through the average (VBL, 16 Nov) no longer closes
    a trade. ``EXIT_EMA9_BUFFER_PCT=0`` and ``EXIT_EMA9_BUFFER_R=0`` restore the
    old bare comparison.
    """
    if side not in ("BUY", "SELL"):
        return False
    band = exit_buffer(close, risk)
    return close < ema - band if side == "BUY" else close > ema + band
