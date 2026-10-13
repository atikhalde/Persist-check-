"""yfinance fetch for Nifty, VIX, sector indices, and NSE stocks."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import yaml
import yfinance as yf

from .session import IST

log = logging.getLogger("nse_scanner.data")

ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_PATH = ROOT / "config" / "universe.yaml"


def load_universe(path: Path | None = None) -> dict:
    with open(path or UNIVERSE_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# TV still uses NSE:LTIM; Yahoo listed the rename LTIM → LTM in 2026.
YAHOO_ALIASES = {
    "LTIM": "LTM.NS",
}


def yahoo_stock(symbol: str) -> str:
    s = symbol.strip().upper()
    if s in YAHOO_ALIASES:
        return YAHOO_ALIASES[s]
    if s.endswith(".NS") or s.startswith("^"):
        return s
    return f"{s}.NS"


def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    cols = {c.lower(): c for c in df.columns}
    out = pd.DataFrame(index=df.index)
    for name in ("Open", "High", "Low", "Close", "Volume"):
        key = name.lower()
        if key in cols:
            out[name] = pd.to_numeric(df[cols[key]], errors="coerce")
        elif name in df.columns:
            out[name] = pd.to_numeric(df[name], errors="coerce")
        else:
            out[name] = 0.0 if name == "Volume" else pd.NA
    if out.index.tz is None:
        out.index = out.index.tz_localize(IST)
    else:
        out.index = out.index.tz_convert(IST)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out = out.dropna(subset=["Close"])
    return out


def _history(ticker: str, period: str, interval: str, retries: int = 3) -> pd.DataFrame:
    last = None
    for i in range(retries):
        try:
            df = yf.Ticker(ticker).history(
                period=period,
                interval=interval,
                auto_adjust=False,
                prepost=False,
                actions=False,
            )
            return _normalize_ohlcv(df)
        except Exception as e:
            last = e
            log.warning("fetch %s %s %s failed (%s) retry %s", ticker, interval, period, e, i + 1)
            time.sleep(1.2 * (i + 1))
    log.error("giving up on %s: %s", ticker, last)
    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


def fetch_symbol(ticker: str, period_5m: str = "7d", period_1d: str = "90d") -> dict:
    return {
        "ticker": ticker,
        "m5": _history(ticker, period_5m, "5m"),
        "d1": _history(ticker, period_1d, "1d"),
    }


def fetch_many(tickers: Iterable[str], period: str, interval: str) -> dict[str, pd.DataFrame]:
    tickers = list(dict.fromkeys(tickers))
    if not tickers:
        return {}
    out: dict[str, pd.DataFrame] = {}
    chunk = 40
    for i in range(0, len(tickers), chunk):
        batch = tickers[i : i + chunk]
        try:
            raw = yf.download(
                batch,
                period=period,
                interval=interval,
                auto_adjust=False,
                group_by="ticker",
                threads=True,
                progress=False,
                actions=False,
            )
        except Exception as e:
            log.warning("batch download failed (%s) — falling back per ticker", e)
            for t in batch:
                out[t] = _history(t, period, interval)
            continue
        if raw is None or raw.empty:
            for t in batch:
                out[t] = _history(t, period, interval)
            continue
        if len(batch) == 1:
            out[batch[0]] = _normalize_ohlcv(raw)
            continue
        for t in batch:
            try:
                if t in raw.columns.get_level_values(0):
                    out[t] = _normalize_ohlcv(raw[t])
                else:
                    out[t] = _history(t, period, interval)
            except Exception:
                out[t] = _history(t, period, interval)
    return out


def resample_1m_to_5m(df1m: pd.DataFrame) -> pd.DataFrame:
    """Aggregate 1m OHLCV to NSE 5m bars (label = bar open)."""
    if df1m is None or df1m.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    x = df1m.copy().sort_index()
    if x.index.tz is None:
        x.index = x.index.tz_localize(IST)
    else:
        x.index = x.index.tz_convert(IST)
    out = x.resample("5min", label="left", closed="left").agg(
        Open=("Open", "first"),
        High=("High", "max"),
        Low=("Low", "min"),
        Close=("Close", "last"),
        Volume=("Volume", "sum"),
    )
    return out.dropna(subset=["Close"])


def patch_zero_volume(df5: pd.DataFrame, df5_from_1m: pd.DataFrame | None) -> pd.DataFrame:
    """Yahoo NSE 5m often stores the 09:15 auction bar with Volume=0.

    Pine/TV has the real opening volume on that bar. RelVol, VWAP, OBV,
    scond08 and the W1 `entry_sell_today` lock all see 0 and misfire
    (false 09:30 EX1 instead of EX8). Copy volume from 1m-aggregated 5m
    onto any 5m bar whose Volume is 0. OHLC stays on the 5m series.
    """
    if df5 is None or df5.empty:
        return df5
    out = df5.copy()
    if df5_from_1m is None or df5_from_1m.empty:
        return out
    src = df5_from_1m
    if src.index.tz is None:
        src = src.copy()
        src.index = src.index.tz_localize(IST)
    else:
        src = src.copy()
        src.index = src.index.tz_convert(IST)
    vol = np.array(out["Volume"].to_numpy(dtype=float), copy=True)
    for i, ts in enumerate(out.index):
        if vol[i] == 0.0 and ts in src.index:
            v = src.at[ts, "Volume"]
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv == fv and fv > 0:
                vol[i] = fv
    out = out.copy()
    out["Volume"] = vol
    return out


def apply_1m_volume(df5: pd.DataFrame, df1m: pd.DataFrame | None) -> pd.DataFrame:
    """Best-effort 09:15 Volume=0 repair. Safe no-op if 1m is missing."""
    if df5 is None or df5.empty or df1m is None or df1m.empty:
        return df5
    try:
        return patch_zero_volume(df5, resample_1m_to_5m(df1m))
    except Exception as e:
        log.warning("1m volume patch skipped: %s", e)
        return df5


def last_session_date(df: pd.DataFrame):
    if df is None or df.empty:
        return None
    return df.index[-1].tz_convert(IST).date()


def sanitize_index_5m(df: pd.DataFrame) -> pd.DataFrame:
    """Drop Yahoo's fake cash-index 5m tail.

    After 15:10 IST Yahoo often repeats a zero-range bar at 15:15/15:20, then
    injects the official 15:30 close as a 15:25 candle. TV's last 5m (and the
    indicator freeze at Open+370m) stays on the last *traded* print (~22317 on
    12 Oct, not 22363).
    """
    if df is None or df.empty:
        return df
    x = df.sort_index()
    minutes = np.array(
        [
            (ts.tz_convert(IST) if getattr(ts, "tzinfo", None) else ts).hour * 60
            + (ts.tz_convert(IST) if getattr(ts, "tzinfo", None) else ts).minute
            for ts in x.index
        ],
        dtype=int,
    )
    rng = (x["High"].to_numpy(dtype=float) - x["Low"].to_numpy(dtype=float))
    dead = (minutes >= 15 * 60 + 15) & (np.abs(rng) < 1e-6)
    x = x.loc[~dead]
    if len(x) >= 2:
        last_ts = x.index[-1]
        t = last_ts.tz_convert(IST) if getattr(last_ts, "tzinfo", None) else last_ts
        if t.hour == 15 and t.minute >= 25:
            last, prev = x.iloc[-1], x.iloc[-2]
            prev_c = float(prev["Close"])
            if abs(float(last["Open"]) - prev_c) <= max(0.05, abs(prev_c) * 1e-4):
                x = x.iloc[:-1]
    return x
