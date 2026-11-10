"""OI data models and providers for backtesting the OI x Price filter.

Self-contained module. Does not modify any existing scanner logic.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Protocol


class OIRegime(str, Enum):
    LONG_BUILDUP = "LONG_BUILDUP"      # price up, OI up   — strong bullish
    SHORT_COVERING = "SHORT_COVERING"  # price up, OI down  — weak bullish, fade risk
    LONG_UNWINDING = "LONG_UNWINDING"  # price down, OI down — weak bearish
    SHORT_BUILDUP = "SHORT_BUILDUP"    # price down, OI up  — strong bearish
    NEUTRAL = "NEUTRAL"


@dataclass(frozen=True)
class OISnapshot:
    symbol: str
    timestamp: datetime          # IST bar close
    oi: int
    close: float
    prev_oi: int | None = None
    prev_close: float | None = None

    @property
    def oi_chg_pct(self) -> float | None:
        if self.prev_oi is None or self.prev_oi == 0:
            return None
        return (self.oi - self.prev_oi) / self.prev_oi * 100.0

    @property
    def price_chg_pct(self) -> float | None:
        if self.prev_close is None or self.prev_close == 0:
            return None
        return (self.close - self.prev_close) / self.prev_close * 100.0

    @property
    def regime(self) -> OIRegime:
        if self.price_chg_pct is None or self.oi_chg_pct is None:
            return OIRegime.NEUTRAL
        pc, oc = self.price_chg_pct, self.oi_chg_pct
        if pc > 0.05 and oc > 0.5:
            return OIRegime.LONG_BUILDUP
        if pc > 0.05 and oc < -0.5:
            return OIRegime.SHORT_COVERING
        if pc < -0.05 and oc > 0.5:
            return OIRegime.SHORT_BUILDUP
        if pc < -0.05 and oc < -0.5:
            return OIRegime.LONG_UNWINDING
        return OIRegime.NEUTRAL


class OIReader(Protocol):
    def snapshot(self, symbol: str, ts: datetime) -> OISnapshot | None: ...


class CSV_OIReader:
    """Reads intraday OI from data/oi_history.csv.

    Columns: symbol,timestamp,oi,close
    timestamp in IST, e.g. 2026-11-09 09:20
    """

    def __init__(self, path: str | Path = "data/oi_history.csv"):
        self.path = Path(path)
        self._index: dict[tuple[str, str], dict] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        with self.path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = (row["symbol"].strip(), row["timestamp"].strip())
                self._index[key] = {
                    "oi": int(row["oi"]),
                    "close": float(row["close"]),
                }

    def snapshot(self, symbol: str, ts: datetime) -> OISnapshot | None:
        self._load()
        ts_key = ts.strftime("%Y-%m-%d %H:%M")
        cur = self._index.get((symbol, ts_key))
        prev_key = (symbol, (ts - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M"))
        prev = self._index.get(prev_key)
        if cur is None:
            return None
        return OISnapshot(
            symbol=symbol,
            timestamp=ts,
            oi=cur["oi"],
            close=cur["close"],
            prev_oi=prev["oi"] if prev else None,
            prev_close=prev["close"] if prev else None,
        )


class StubOIReader:
    """Synthetic OI reader for structural testing without real data."""
    def snapshot(self, symbol: str, ts: datetime) -> OISnapshot | None:
        return OISnapshot(
            symbol=symbol,
            timestamp=ts,
            oi=1_000_000,
            close=100.0,
            prev_oi=900_000,
            prev_close=99.0,
        )
