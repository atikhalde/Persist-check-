"""Pine-like technicals (EMA / RMA / RSI / MACD / ADX / ATR / VWAP)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def _as_float(x: np.ndarray | pd.Series) -> np.ndarray:
    return np.asarray(x, dtype=float)


def sma(x, length: int) -> np.ndarray:
    s = pd.Series(_as_float(x))
    return s.rolling(length, min_periods=length).mean().to_numpy()


def rma(x, length: int) -> np.ndarray:
    """Wilder RMA — same seed as Pine ta.rma (SMA of first `length` bars)."""
    arr = _as_float(x)
    n = len(arr)
    out = np.full(n, np.nan)
    if n < length or length <= 0:
        return out
    alpha = 1.0 / length
    seed = arr[:length]
    if np.all(np.isnan(seed)):
        return out
    start = np.nanmean(seed)
    if np.isnan(start):
        # skip leading NaNs like Pine
        valid = np.where(~np.isnan(arr))[0]
        if len(valid) < length:
            return out
        i0 = valid[0]
        if i0 + length > n:
            return out
        start = np.nanmean(arr[i0 : i0 + length])
        out[i0 + length - 1] = start
        prev = start
        for i in range(i0 + length, n):
            v = arr[i]
            if np.isnan(v):
                out[i] = prev
            else:
                prev = prev * (1.0 - alpha) + v * alpha
                out[i] = prev
        return out
    out[length - 1] = start
    prev = start
    for i in range(length, n):
        v = arr[i]
        if np.isnan(v):
            out[i] = prev
        else:
            prev = prev * (1.0 - alpha) + v * alpha
            out[i] = prev
    return out


def ema(x, length: int) -> np.ndarray:
    """Pine ta.ema: SMA seed then EMA with alpha=2/(len+1)."""
    arr = _as_float(x)
    n = len(arr)
    out = np.full(n, np.nan)
    if n < length or length <= 0:
        return out
    alpha = 2.0 / (length + 1.0)
    seed = arr[:length]
    if np.all(np.isnan(seed)):
        return out
    start = np.nanmean(seed)
    out[length - 1] = start
    prev = start
    for i in range(length, n):
        v = arr[i]
        if np.isnan(v):
            out[i] = prev
        else:
            prev = alpha * v + (1.0 - alpha) * prev
            out[i] = prev
    return out


def rsi(close, length: int = 14) -> np.ndarray:
    c = _as_float(close)
    delta = np.diff(c, prepend=c[0] if len(c) else 0.0)
    delta[0] = np.nan
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_g = rma(gain, length)
    avg_l = rma(loss, length)
    rs = avg_g / np.where(avg_l == 0, np.nan, avg_l)
    out = 100.0 - (100.0 / (1.0 + rs))
    out = np.where(avg_l == 0, 100.0, out)
    return out


def atr(high, low, close, length: int = 14) -> np.ndarray:
    h, l, c = _as_float(high), _as_float(low), _as_float(close)
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    tr[0] = h[0] - l[0]
    return rma(tr, length)


def macd(close, fast: int = 12, slow: int = 26, signal: int = 9):
    c = _as_float(close)
    line = ema(c, fast) - ema(c, slow)
    sig = ema(line, signal)
    hist = line - sig
    return line, sig, hist


def adx(high, low, close, length: int = 14):
    """Returns plusDI, minusDI, adx — Pine dirmov / ta.dmi style."""
    h, l, c = _as_float(high), _as_float(low), _as_float(close)
    up = h - np.roll(h, 1)
    down = np.roll(l, 1) - l
    up[0] = 0.0
    down[0] = 0.0
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    tr[0] = h[0] - l[0]
    atr_n = rma(tr, length)
    sm_plus = rma(plus_dm, length)
    sm_minus = rma(minus_dm, length)
    plus_di = np.where(atr_n != 0, 100.0 * sm_plus / atr_n, 0.0)
    minus_di = np.where(atr_n != 0, 100.0 * sm_minus / atr_n, 0.0)
    denom = plus_di + minus_di
    dx = np.where(denom != 0, 100.0 * np.abs(plus_di - minus_di) / denom, 0.0)
    adx_v = rma(dx, length)
    return plus_di, minus_di, adx_v


def session_vwap(high, low, close, volume, session_id, fill_zero_vol: bool = True) -> np.ndarray:
    """Cumulative typical-price VWAP, reset on each session_id change.

    Pine `vwap_val`: if session volume is 0, VWAP = close.
    Yahoo cash indices (^NSEI, sector) often have Volume=0 on every bar —
    `fill_zero_vol=True` (default) treats 0 as 1 so VWAP is equal-weight
    typical price. Stocks should pass `fill_zero_vol=False`.
    """
    h, l, c, v = _as_float(high), _as_float(low), _as_float(close), _as_float(volume)
    tp = (h + l + c) / 3.0
    vol = np.nan_to_num(v, nan=0.0)
    if fill_zero_vol:
        vol = np.where(vol > 0, vol, 1.0)
    pv = tp * vol
    sid = np.asarray(session_id)
    out = np.full(len(c), np.nan)
    acc_pv = 0.0
    acc_v = 0.0
    prev = None
    for i in range(len(c)):
        if prev is None or sid[i] != prev:
            acc_pv = 0.0
            acc_v = 0.0
            prev = sid[i]
        acc_pv += pv[i]
        acc_v += vol[i]
        out[i] = acc_pv / acc_v if acc_v else c[i]
    return out


def highest(x, length: int) -> np.ndarray:
    # Pine ta.highest is na until `length` bars exist.
    return pd.Series(_as_float(x)).rolling(length, min_periods=length).max().to_numpy()


def lowest(x, length: int) -> np.ndarray:
    return pd.Series(_as_float(x)).rolling(length, min_periods=length).min().to_numpy()


def linreg(x, length: int) -> np.ndarray:
    s = pd.Series(_as_float(x))
    idx = np.arange(length, dtype=float)

    def _lr(arr):
        if np.any(np.isnan(arr)):
            return np.nan
        y = arr.astype(float)
        x_mean = idx.mean()
        y_mean = y.mean()
        den = ((idx - x_mean) ** 2).sum()
        if den == 0:
            return y_mean
        slope = ((idx - x_mean) * (y - y_mean)).sum() / den
        intercept = y_mean - slope * x_mean
        return intercept + slope * (length - 1)

    return s.rolling(length, min_periods=length).apply(_lr, raw=True).to_numpy()


def nz(v, alt=0.0):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return alt
    return v
