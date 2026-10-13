"""Faithful 5m master-scanner engine (Pine `MASTER SECTOR BATCH SCANNER`).

Historical HTF matches `request.security(..., lookahead_on)`: every 5m bar
inside a completed 15m / daily candle sees that HTF bar's FINAL OHLC.

Table storage matches the Pine scanner:
  first hit of the day, upgrade only on higher rank, ENTRY BUY/SELL (90/290)
  cleared after 09:45. 90/290 do NOT set buyWindowFired / sellWindowFired.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import ta
from .session import (
    BUY_W1,
    BUY_W2,
    BUY_W3,
    FIN_BUY,
    FIN_SELL,
    IST,
    SELL_W1,
    SELL_W2,
    SELL_W3,
    in_window,
    now_ist,
    session_date,
    to_ist,
)

SIGNAL_NAME = {
    80: "NORMAL BUY",
    90: "ENTRY BUY",
    101: "BUY-EX",
    102: "BUY-EX17",
    103: "BUY-EX4",
    104: "BUY-EX5",
    105: "BUY-EX6",
    106: "BUY-EX7",
    107: "BUY-EX8",
    108: "BUY-EX9",
    109: "BUY-EX10",
    110: "BUY-EX11",
    111: "BUY-EX12",
    112: "BUY-EX13",
    201: "SELL-EX1",
    202: "SELL-EX2",
    203: "SELL-EX3",
    204: "SELL-EX4",
    205: "SELL-EX5",
    206: "SELL-EX5S",
    207: "SELL-EX6",
    208: "SELL-EX7",
    209: "SELL-EX8",
    210: "SELL-EX9",
    211: "SELL-EX10",
    212: "SELL-EX11",
    213: "SELL-EX12",
    214: "SELL-EX13",
    215: "SELL-EX14",
    216: "SELL-EX15",
    217: "SELL-EX16",
    218: "SELL-EX17",
    219: "SELL-EX18",
    220: "SELL-EX19",
    280: "NORMAL SELL",
    290: "ENTRY SELL",
}

BUY_LOCK = {80, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112}
SELL_LOCK = {201, 202, 203, 204, 205, 206, 207, 208, 209, 210, 211, 212, 213, 214, 215, 216, 217, 218, 219, 220, 280}


def is_buy_code(code: int) -> bool:
    return 80 <= int(code) < 200


def signal_rank(code: int) -> int:
    c = int(code)
    if 101 <= c <= 220:
        return 3
    if c in (80, 280):
        return 2
    if c in (90, 290):
        return 1
    return 0


@dataclass
class StockSignal:
    symbol: str
    code: int
    name: str
    side: str
    time: str
    price: float
    score: int
    strength: str
    base_active: int
    cond22: int
    reason: str
    sl: float | None = None


def nse_session_index(day) -> pd.DatetimeIndex:
    start = pd.Timestamp(f"{day} 09:15", tz=IST)
    end = pd.Timestamp(f"{day} 15:25", tz=IST)
    return pd.date_range(start, end, freq="5min", tz=IST)


def fill_nse_grid(df: pd.DataFrame) -> pd.DataFrame:
    """Insert missing NSE 5m slots inside days that already have data. No cross-day fill."""
    if df is None or df.empty:
        return df
    x = df.copy().sort_index()
    if x.index.tz is None:
        x.index = x.index.tz_localize(IST)
    else:
        x.index = x.index.tz_convert(IST)
    parts = []
    for day, g in x.groupby(x.index.date):
        if pd.Timestamp(day).weekday() >= 5:
            parts.append(g)
            continue
        g2 = g[~g.index.duplicated(keep="last")]
        real = g2.dropna(subset=["Close"])
        if real.empty:
            continue
        # Interior holes only — do not invent a fake 09:15 or trailing 15:15–15:25.
        idx = nse_session_index(day)
        idx = idx[(idx >= real.index.min()) & (idx <= real.index.max())]
        y = g2.reindex(idx)
        y["Close"] = y["Close"].ffill()
        y["Open"] = y["Open"].fillna(y["Close"])
        y["High"] = y["High"].fillna(y["Close"])
        y["Low"] = y["Low"].fillna(y["Close"])
        y["Volume"] = y["Volume"].fillna(0.0)
        y = y.dropna(subset=["Close"])
        parts.append(y)
    out = pd.concat(parts).sort_index()
    return out[~out.index.duplicated(keep="last")]


def _vwap_src(src, vol, sids) -> np.ndarray:
    src = np.asarray(src, float)
    vol = np.asarray(vol, float)
    out = np.full(len(src), np.nan)
    pv = acc_v = 0.0
    prev = None
    for i in range(len(src)):
        if prev is None or sids[i] != prev:
            pv = acc_v = 0.0
            prev = sids[i]
        v = 0.0 if np.isnan(vol[i]) else vol[i]
        x = 0.0 if np.isnan(src[i]) else src[i]
        pv += x * v
        acc_v += v
        out[i] = pv / acc_v if acc_v else x
    return out


def _gt(a, b) -> bool:
    return (a == a) and (b == b) and a > b


def _lt(a, b) -> bool:
    return (a == a) and (b == b) and a < b


def _ge(a, b) -> bool:
    return (a == a) and (b == b) and a >= b


def _le(a, b) -> bool:
    return (a == a) and (b == b) and a <= b


def _eq(a, b) -> bool:
    """Pine `==` on NSE prices. Round to paise so Yahoo 1554.800049 == daily 1554.8."""
    if a != a or b != b:
        return False
    return round(float(a), 2) == round(float(b), 2)


def _daily_lookahead(df5: pd.DataFrame, df1: pd.DataFrame | None, sids):
    """Map each 5m bar to daily OHLC with Pine lookahead_on on completed days."""
    n = len(df5)
    d_open = np.full(n, np.nan)
    d_high = np.full(n, np.nan)
    d_low = np.full(n, np.nan)
    d_close = np.full(n, np.nan)
    p_close = np.full(n, np.nan)
    p_high = np.full(n, np.nan)
    p_low = np.full(n, np.nan)

    # 5m-aggregated daily (always available)
    agg = {}
    for sid, g in df5.groupby(sids):
        agg[sid] = {
            "Open": float(g["Open"].iloc[0]),
            "High": float(g["High"].max()),
            "Low": float(g["Low"].min()),
            "Close": float(g["Close"].iloc[-1]),
        }

    daily = {}
    if df1 is not None and len(df1):
        d = df1.copy().sort_index()
        if d.index.tz is None:
            d.index = d.index.tz_localize(IST)
        else:
            d.index = d.index.tz_convert(IST)
        for t, row in d.iterrows():
            daily[session_date(t)] = {
                "Open": float(row["Open"]),
                "High": float(row["High"]),
                "Low": float(row["Low"]),
                "Close": float(row["Close"]),
            }
    for sid, row in agg.items():
        daily.setdefault(sid, row)

    dates_sorted = sorted(daily.keys())
    last_i = {}
    for i, sid in enumerate(sids):
        last_i[sid] = i
    # Yahoo often drops 15:15–15:25, so "last bar >= 15:20" is a bad completeness
    # test. Any session strictly before today is a finished day → full daily
    # lookahead_on. Today stays developing until the cash close.
    today = now_ist().date()
    complete = set()
    for sid, i in last_i.items():
        t = to_ist(df5.index[i])
        if sid < today:
            complete.add(sid)
        elif (t.hour, t.minute) >= (15, 25):
            complete.add(sid)

    run_o = run_h = run_l = np.nan
    prev_sid = None
    for i, sid in enumerate(sids):
        if sid != prev_sid:
            run_o = float(df5["Open"].iloc[i])
            run_h = float(df5["High"].iloc[i])
            run_l = float(df5["Low"].iloc[i])
            prev_sid = sid
        else:
            run_h = max(run_h, float(df5["High"].iloc[i]))
            run_l = min(run_l, float(df5["Low"].iloc[i]))
        run_c = float(df5["Close"].iloc[i])
        row = daily.get(sid)
        if sid in complete and row is not None:
            d_open[i], d_high[i], d_low[i], d_close[i] = row["Open"], row["High"], row["Low"], row["Close"]
        else:
            d_open[i], d_high[i], d_low[i], d_close[i] = run_o, run_h, run_l, run_c
        earlier = None
        if dates_sorted:
            for d in reversed(dates_sorted):
                if d < sid:
                    earlier = daily[d]
                    break
        if earlier is not None:
            p_close[i], p_high[i], p_low[i] = earlier["Close"], earlier["High"], earlier["Low"]
        elif i and sids[i - 1] != sid:
            p_close[i], p_high[i], p_low[i] = d_close[i - 1], d_high[i - 1], d_low[i - 1]
        elif i:
            p_close[i], p_high[i], p_low[i] = p_close[i - 1], p_high[i - 1], p_low[i - 1]
    return d_open, d_high, d_low, d_close, p_close, p_high, p_low


def _htf15_lookahead(df5: pd.DataFrame, sids):
    """15m OHLC / EMA20 / EMA50 / VWAP with lookahead_on; ema39_15 is Pine ta.ema(_15, 39) on 5m."""
    x = df5.copy()
    bucket = x.index.floor("15min")
    g = x.groupby(bucket).agg(
        Open=("Open", "first"),
        High=("High", "max"),
        Low=("Low", "min"),
        Close=("Close", "last"),
        Volume=("Volume", "sum"),
    )
    sid15 = [session_date(t) for t in g.index]
    g["EMA20"] = ta.ema(g["Close"], 20)
    g["EMA50"] = ta.ema(g["Close"], 50)
    g["VWAP"] = ta.session_vwap(g["High"], g["Low"], g["Close"], g["Volume"], sid15)

    n = len(x)
    o15 = np.full(n, np.nan)
    c15 = np.full(n, np.nan)
    e20 = np.full(n, np.nan)
    e50 = np.full(n, np.nan)
    v15 = np.full(n, np.nan)
    for i, ts in enumerate(x.index):
        b = ts.floor("15min")
        if b in g.index:
            row = g.loc[b]
            o15[i] = float(row["Open"])
            c15[i] = float(row["Close"])
            e20[i] = float(row["EMA20"]) if row["EMA20"] == row["EMA20"] else c15[i]
            e50[i] = float(row["EMA50"]) if row["EMA50"] == row["EMA50"] else c15[i]
            v15[i] = float(row["VWAP"]) if row["VWAP"] == row["VWAP"] else c15[i]
        else:
            o15[i] = c15[i] = float(x["Close"].iloc[i])
            e20[i] = e50[i] = v15[i] = c15[i]
    e39 = ta.ema(c15, 39)  # Pine: ta.ema(_15, 39) on the 5m series of 15m closes
    return o15, c15, e20, e50, e39, v15


def table_from_fires(signals: list[StockSignal], last_hhmm: str = "15:25") -> list[StockSignal]:
    return _table_view(signals, last_hhmm)


def _table_view(signals: list[StockSignal], last_hhmm: str | None = None) -> list[StockSignal]:
    """Walk like Pine scanCodeToday: first / upgrade on higher rank; wipe 90/290 after 09:45.

    Clearing is based on the LAST 5m bar of the series (Pine wipes on every bar after 09:45,
    even when no new exception fires). So a lone 09:35 ENTRY SELL is gone by EOD.
    """
    stored: StockSignal | None = None
    for s in signals:
        if stored is None or signal_rank(s.code) > signal_rank(stored.code):
            stored = s
    hhmm = last_hhmm or (signals[-1].time if signals else "15:25")
    hh, mm = map(int, hhmm.split(":"))
    after = hh > 9 or (hh == 9 and mm >= 45)
    if after and stored is not None and stored.code in (90, 290):
        stored = None
    return [stored] if stored else []


def scan_stock(
    symbol: str,
    df5: pd.DataFrame,
    df1: pd.DataFrame | None,
    keep_all: bool = False,
    as_of=None,
) -> list[StockSignal]:
    if df5 is None or df5.empty or len(df5) < 40:
        return []
    df5 = fill_nse_grid(df5)
    if len(df5) < 40:
        return []

    close = df5["Close"].to_numpy(float)
    high = df5["High"].to_numpy(float)
    low = df5["Low"].to_numpy(float)
    open_ = df5["Open"].to_numpy(float)
    vol = df5["Volume"].to_numpy(float)
    n = len(df5)
    sids = np.array([session_date(t) for t in df5.index])
    last_sid = as_of if as_of is not None else sids[-1]

    ema20 = ta.ema(close, 20)
    ema50 = ta.ema(close, 50)
    vwap_close = _vwap_src(close, vol, sids)  # ta.vwap(close)
    vwap_typ = ta.session_vwap(high, low, close, vol, sids)  # Pine vwap_val; 0-vol 09:15 repaired via 1m
    vwap5 = _vwap_src((high + low + close) / 3.0, vol, sids)  # cond03/10

    rsi5 = ta.rsi(close, 14)
    _, _, macd_hist = ta.macd(close)
    _, _, adx5 = ta.adx(high, low, close, 14)
    atr5 = ta.atr(high, low, close, 14)
    sma_atr = ta.sma(atr5, 20)
    sma_vol = ta.sma(vol, 20)
    hh7 = ta.highest(high, 7)
    ll7 = ta.lowest(low, 7)
    hh20 = ta.highest(high, 20)
    ll20 = ta.lowest(low, 20)

    obv = np.zeros(n)
    for i in range(1, n):
        if close[i] > close[i - 1]:
            obv[i] = obv[i - 1] + vol[i]
        elif close[i] < close[i - 1]:
            obv[i] = obv[i - 1] - vol[i]
        else:
            obv[i] = obv[i - 1]
    obv_slope = ta.sma(obv, 5) - ta.sma(obv, 20)

    d_open, d_high, d_low, d_close, p_close, p_high, p_low = _daily_lookahead(df5, df1, sids)
    o15, c15, e20_15, e50_15, e39_15, vwap15 = _htf15_lookahead(df5, sids)
    # Pine: ema39_D = ta.ema(dClose, 39) on the 5m series of lookahead daily closes
    ema39_d = ta.ema(d_close, 39)

    fires: list[StockSignal] = []

    was_strong = False
    buy_w = [False, False, False]
    sell_w = [False, False, False]
    first5_open = first5_high = first5_low = np.nan
    max_first6 = smax_first6 = 0.0
    bars_sess = 0
    gap_up = gap_down = first_window_big_gap = False
    buy_fired = sell_fired = False
    b_below = s_above = 0
    b_max_body = s_max_body = 0.0
    s_max_range = s_sum_range = 0.0
    s_count_range = 0
    s_first5_full = s_first5_body = 0.0
    vwap_at_first = np.nan
    prev_close_from5m = np.nan
    orb_h = orb_l = np.nan
    final_orb_h = final_orb_l = np.nan
    in_orb_prev = False
    b1_bo = b1_re = False
    b1_lvl = 0.0
    b1_bars = 0
    b2_form = b2_bo = b2_re = False
    b2_high = b2_lvl = b2_bo_lvl = 0.0
    b2_bars_h = b2_bars_b = 0
    s1_bd = s1_re = False
    s1_lvl = 0.0
    s1_bars = 0
    s2_form = s2_bd = s2_re = False
    s2_low = s2_lvl = s2_bd_lvl = 0.0
    s2_bars_l = s2_bars_b = 0
    # todayHigh[1] on first bar of a session is the PREVIOUS session's running high
    running_high = np.nan
    running_low = np.nan

    for i in range(n):
        ts = to_ist(df5.index[i])
        sid = sids[i]
        is_new_day = i == 0 or sids[i] != sids[i - 1]
        is_today = sid == last_sid
        c0, o0, h0, l0, v0 = close[i], open_[i], high[i], low[i], vol[i]
        c1 = close[i - 1] if i else c0
        h1 = high[i - 1] if i else h0
        l1 = low[i - 1] if i else l0
        h2 = high[i - 2] if i >= 2 else h0
        l2 = low[i - 2] if i >= 2 else l0

        prev_th = running_high  # todayHigh[1]
        prev_tl = running_low

        if is_new_day:
            was_strong = False
            buy_w = [False, False, False]
            sell_w = [False, False, False]
            first5_open, first5_high, first5_low = o0, h0, l0
            max_first6 = smax_first6 = 0.0
            bars_sess = 0
            prev_c_gap = p_close[i] if p_close[i] == p_close[i] else c1
            gap_th = 0.015
            if prev_c_gap:
                gap_up = (o0 - prev_c_gap) / prev_c_gap > gap_th
                gap_down = (prev_c_gap - o0) / prev_c_gap > gap_th
                first_window_big_gap = gap_up or gap_down
            buy_fired = sell_fired = False
            b_below = s_above = 0
            b_max_body = s_max_body = 0.0
            s_max_range = s_sum_range = 0.0
            s_count_range = 0
            s_first5_full = ((h0 - l0) / c0) * 100 if c0 else 0.0
            s_first5_body = abs(c0 - o0) / c0 * 100 if c0 else 0.0
            vwap_at_first = float(vwap15[i])
            prev_close_from5m = c1 if i else p_close[i]
            orb_h, orb_l = h0, l0
            final_orb_h = final_orb_l = np.nan
            in_orb_prev = True
            b1_bo = b1_re = b2_form = b2_bo = b2_re = False
            s1_bd = s1_re = s2_form = s2_bd = s2_re = False
            b1_bars = b2_bars_h = b2_bars_b = s1_bars = s2_bars_l = s2_bars_b = 0
            running_high, running_low = h0, l0
        else:
            bars_sess += 1
            running_high = max(running_high, h0) if running_high == running_high else h0
            running_low = min(running_low, l0) if running_low == running_low else l0

        # Pine session '0915-0930' is start-inclusive / end-exclusive, so 09:15–09:25
        # build the ORB and 09:30 is the first bar outside. Capture on that bar.
        # closeBelow15mORBLow uses the captured level; Pine assigns then compares
        # on the same bar, so 09:30 CAN break the just-finalized ORB.
        in_orb = ts.hour == 9 and ts.minute < 30
        if in_orb:
            orb_h = h0 if np.isnan(orb_h) else max(orb_h, h0)
            orb_l = l0 if np.isnan(orb_l) else min(orb_l, l0)
        if in_orb_prev and not in_orb and np.isnan(final_orb_h):
            final_orb_h, final_orb_l = orb_h, orb_l
        in_orb_prev = in_orb
        if (not in_orb) and np.isnan(final_orb_h) and not np.isnan(orb_h):
            final_orb_h, final_orb_l = orb_h, orb_l

        if (not is_new_day) and 2 <= bars_sess <= 6:
            if c0:
                body = (c0 - o0) / c0 * 100.0
                max_first6 = max(max_first6, body)
                smax_first6 = max(smax_first6, body)

        v_c = float(vwap_close[i]) if vwap_close[i] == vwap_close[i] else c0
        v_typ = float(vwap_typ[i]) if vwap_typ[i] == vwap_typ[i] else c0
        v5 = float(vwap5[i]) if vwap5[i] == vwap5[i] else c0
        v15 = float(vwap15[i]) if vwap15[i] == vwap15[i] else c0
        e20 = float(ema20[i]) if ema20[i] == ema20[i] else c0
        e50 = float(ema50[i]) if ema50[i] == ema50[i] else c0
        rsi_v = float(rsi5[i])
        rsi_p = float(rsi5[i - 1]) if i else np.nan
        adx_v = float(adx5[i])
        atr_v = float(atr5[i])
        smaatr_v = float(sma_atr[i])
        hist = float(macd_hist[i]) if macd_hist[i] == macd_hist[i] else 0.0
        obs = float(obv_slope[i]) if obv_slope[i] == obv_slope[i] else 0.0
        rel_vol = (v0 / sma_vol[i]) if sma_vol[i] and sma_vol[i] == sma_vol[i] else 0.0

        trend_base = 20 if (c0 > e20 and e20 > e50) else (10 if c0 > e20 else 0)
        trend_vwap = 10 if c0 > v_typ else 0
        mom_rsi = 12 if _gt(rsi_v, 55) else (6 if _gt(rsi_v, 50) else 0)
        mom_macd = 13 if hist > 0 else 0
        adx_sc = 15 if adx_v >= 25 else (10 if adx_v >= 20 else (5 if adx_v >= 15 else 0))
        vol_sc = 15 if (obs > 0 and rel_vol > 1.5) else (10 if obs > 0 else 0)
        rs_sc = 15 if (c1 and (c0 - c1) / c1 > 0) else 0
        total = trend_base + trend_vwap + mom_rsi + mom_macd + adx_sc + vol_sc + rs_sc
        strength = "Extreme Strong" if total >= 80 else "Strong" if total >= 60 else "Medium" if total >= 40 else "Low"
        is_strong = total >= 60
        prev_was = False if is_new_day else was_strong
        first_strong = is_strong and not prev_was
        first_weak = (not is_strong) and prev_was and total < 55
        first_red = prev_was and (not is_strong) and total < 55
        first_green = (not prev_was) and is_strong
        was_strong = is_strong

        rng = h0 - l0
        upper_wick_pct = ((h0 - max(o0, c0)) / rng * 100) if rng > 0 else 0.0
        lower_wick_pct = ((min(o0, c0) - l0) / rng * 100) if rng > 0 else 0.0
        buy_filter = (abs(l0 - v_c) / v_c * 100) <= 1 if v_c else True
        sell_filter = (abs(h0 - v_c) / v_c * 100) <= 1 if v_c else True
        body_mostly_above = c0 > v_c
        body_mostly_below = c0 < v_c
        long_ok = c0 > v_c
        short_ok = c0 < v_c
        # Pine: ta.highest(tf5_high, 7)[1] — na until 7 bars (comparison false)
        cons_high_prev = float(hh7[i - 1]) if i else np.nan
        cons_low_prev = float(ll7[i - 1]) if i else np.nan
        hh20_p = float(hh20[i - 1]) if i else np.nan
        ll20_p = float(ll20[i - 1]) if i else np.nan
        range_break = _gt(h0, hh20_p) or _lt(l0, ll20_p)

        in_b1, in_b2, in_b3 = in_window(ts, *BUY_W1), in_window(ts, *BUY_W2), in_window(ts, *BUY_W3)
        in_s1, in_s2, in_s3 = in_window(ts, *SELL_W1), in_window(ts, *SELL_W2), in_window(ts, *SELL_W3)
        fin_buy, fin_sell = in_window(ts, *FIN_BUY), in_window(ts, *FIN_SELL)

        agg_buy = (
            (((in_b1 or in_b2 or in_b3) and _gt(h0, cons_high_prev)) or first_green or first_strong)
            and buy_filter
            and (upper_wick_pct <= 25 or body_mostly_above)
            and long_ok
            and range_break
        )
        entry_buy = agg_buy and total > 60
        entry_buy_today = (
            (
                (entry_buy and in_b1 and not buy_w[0] and not first_window_big_gap and not gap_up)
                or (entry_buy and in_b2 and not buy_w[1])
                or (entry_buy and in_b3 and not buy_w[2])
            )
            and rel_vol >= 0.2
        )
        if entry_buy_today:
            if in_b1:
                buy_w[0] = True
            elif in_b2:
                buy_w[1] = True
            elif in_b3:
                buy_w[2] = True

        agg_sell = (
            (((in_s1 or in_s2 or in_s3) and _lt(l0, cons_low_prev)) or first_weak or first_red)
            and sell_filter
            and (lower_wick_pct <= 25 or body_mostly_below)
            and short_ok
            and range_break
        )
        entry_sell = agg_sell and total < 40
        entry_sell_today = (
            (
                (entry_sell and in_s1 and not sell_w[0] and not first_window_big_gap and not gap_down)
                or (entry_sell and in_s2 and not sell_w[1])
                or (entry_sell and in_s3 and not sell_w[2])
            )
            and rel_vol >= 0.2
        )
        if entry_sell_today:
            if in_s1:
                sell_w[0] = True
            elif in_s2:
                sell_w[1] = True
            elif in_s3:
                sell_w[2] = True

        prev_c = p_close[i] if p_close[i] == p_close[i] else c1
        f5_range = max(first5_high, prev_c) - min(first5_low, prev_c)
        f5_pct = f5_range / prev_c * 100 if prev_c else 0.0
        first5_is_day_low = _eq(first5_low, d_low[i])
        first5_is_day_high = _eq(first5_high, d_high[i])

        gap_base = prev_close_from5m if prev_close_from5m == prev_close_from5m else prev_c
        gap_pct = (first5_open - gap_base) / gap_base * 100 if gap_base else 0.0
        is_gap_small = gap_pct < 0.75
        vwap15_dist = (vwap_at_first - first5_open) / first5_open * 100 if first5_open else np.nan
        vwapnotfar = True if is_gap_small else (True if np.isnan(vwap15_dist) else vwap15_dist <= 0.5)

        if not is_new_day:
            if c0 < v15:
                b_below += 1
            if c0 > v15:
                s_above += 1
            body_pct = abs((c0 - o0) / c0 * 100) if c0 else 0.0
            b_max_body = max(b_max_body, body_pct)
            s_max_body = max(s_max_body, body_pct)
            cr = (h0 - l0) / c0 * 100 if c0 else 0.0
            s_max_range = max(s_max_range, cr)
            s_sum_range += cr
            s_count_range += 1
        is_vwap_bullish = b_below < 4
        is_vwap_bearish = s_above < 2
        bmax_ok = b_max_body < 0.5
        savg = s_sum_range / s_count_range if s_count_range else 0.0
        no_rel_spike = s_max_range < savg * 2.5 if savg else True

        trng = h0 - l0
        us = h0 - max(o0, c0)
        ls = min(o0, c0) - l0
        valid_r = trng > 0.001
        not_bull_hammer = not (valid_r and ls > us * 3 and ls > trng * 0.25 and c0 > l0 + ls * 0.5)

        is_new_day_high = _ge(h0, prev_th)
        is_close_above_dh = _ge(c0, prev_th)
        is_new_day_low = _le(l0, prev_tl)
        is_close_below_dl = _le(c0, prev_tl)

        broke_pdh = _gt(c0, p_high[i])
        broke_pdl = _lt(c0, p_low[i])
        pivot = (p_high[i] + p_low[i] + p_close[i]) / 3.0
        s3 = p_low[i] - 2 * (p_high[i] - pivot)
        above_pp = _gt(c0, pivot)
        below_pp = _lt(c0, pivot)
        below_pdh = _lt(c0, p_high[i])
        above_s3 = _gt(c0, s3)
        close_above_orb = (not np.isnan(final_orb_h)) and c0 > final_orb_h
        close_below_orb = (not np.isnan(final_orb_l)) and c0 < final_orb_l

        daily_pivot = (d_high[i] + d_low[i] + d_close[i]) / 3.0
        daily_r2 = 2 * daily_pivot - d_low[i]
        daily_s2 = 2 * daily_pivot - d_high[i]
        e39d = float(ema39_d[i]) if ema39_d[i] == ema39_d[i] else d_close[i]

        res_lvl = max(d_high[i], hh20_p)
        original_bo = c0 > res_lvl
        if (not b1_bo) and c0 > res_lvl and not is_new_day:
            b1_bo, b1_lvl, b1_bars = True, res_lvl, 0
        if b1_bo:
            b1_bars += 1
            if l0 >= b1_lvl * 0.997 and l0 <= b1_lvl * 1.003 and c0 > b1_lvl:
                b1_re = True
            if b1_bars > 15:
                b1_bo = False
        is_swing_h = i >= 6 and all(h0 > high[i - k] for k in range(1, 7))
        if is_swing_h and not b2_form:
            b2_high, b2_bars_h, b2_lvl, b2_form = h0, 0, l1, True
        if b2_form:
            b2_bars_h += 1
            if h0 < b2_high and b2_bars_h >= 3 and c0 > b2_lvl and not b2_bo:
                b2_bo, b2_bo_lvl, b2_bars_b, b2_re = True, b2_lvl, 0, False
            if c0 > b2_high:
                b2_form = False
        if b2_bo:
            b2_bars_b += 1
            if l0 >= b2_bo_lvl * 0.997 and l0 <= b2_bo_lvl * 1.003 and c0 > b2_bo_lvl:
                b2_re = True
            if b2_bars_b > 15:
                b2_bo = False
        b_retest = (b1_bo and b1_re) or (b2_bo and b2_re)
        before_1015 = ts.hour < 10 or (ts.hour == 10 and ts.minute < 11)

        sup_lvl = min(d_low[i], ll20_p)
        original_bd = c0 < sup_lvl
        if (not s1_bd) and c0 < sup_lvl and not is_new_day:
            s1_bd, s1_lvl, s1_bars = True, sup_lvl, 0
        if s1_bd:
            s1_bars += 1
            if h0 >= s1_lvl * 0.997 and h0 <= s1_lvl * 1.003 and c0 < s1_lvl:
                s1_re = True
            if s1_bars > 15:
                s1_bd = False
        is_swing_l = i >= 6 and all(l0 < low[i - k] for k in range(1, 7))
        if is_swing_l and not s2_form:
            s2_low, s2_bars_l, s2_lvl, s2_form = l0, 0, h1, True
        if s2_form:
            s2_bars_l += 1
            if l0 > s2_low and s2_bars_l >= 3 and c0 < s2_lvl and not s2_bd:
                s2_bd, s2_bd_lvl, s2_bars_b, s2_re = True, s2_lvl, 0, False
            if c0 < s2_low:
                s2_form = False
        if s2_bd:
            s2_bars_b += 1
            if h0 >= s2_bd_lvl * 0.997 and h0 <= s2_bd_lvl * 1.003 and c0 < s2_bd_lvl:
                s2_re = True
            if s2_bars_b > 15:
                s2_bd = False
        s_retest = (s1_bd and s1_re) or (s2_bd and s2_re)

        hh7_p = float(hh7[i - 1]) if i else np.nan
        ll7_p = float(ll7[i - 1]) if i else np.nan
        v5_p = float(vwap5[i - 1]) if i else np.nan

        # Pine: `x > na` is false. Do not substitute 50/close for na.
        cond = [
            1 if _gt(rsi_v, rsi_p) else 0,
            1 if _gt(rsi_v, 55) else 0,
            1 if _gt(c0, v5) else 0,
            1 if _gt(c0, c1) else 0,
            1 if _gt(atr_v, 0.5 * smaatr_v) else 0,
            1 if _gt(c0, e20) else 0,
            1 if _gt(c0, o0) else 0,
            1 if (v0 * c0 > 5_000_000) else 0,
            1 if _gt(c0, h1) else 0,
            1 if _gt(v5, v5_p) else 0,
            1 if _gt(c0, h2) else 0,
            1 if c0 and ((h0 - l0) / c0 < 0.015) else 0,
            1 if _gt(c15[i], o15[i]) else 0,
            1 if _gt(c0, o0) else 0,
            1 if _gt(d_open[i], 50) else 0,
            1 if _gt(d_close[i], e39d) else 0,
            1 if _gt(c15[i], e39_15[i]) else 0,
            1 if _gt(h0, hh7_p) else 0,
            1 if f5_pct < 4 else 0,
            1 if max_first6 < 1.5 else 0,
            1 if _lt(c0, daily_r2) else 0,
        ]
        cond22 = 1 if (b_retest if before_1015 else (original_bo or b_retest)) else 0
        base = int(sum(cond))
        use_ex = base >= 20

        scond = [
            1 if _lt(rsi_v, rsi_p) else 0,
            1 if _lt(rsi_v, 45) else 0,
            1 if _lt(c0, v5) else 0,
            1 if _lt(c0, c1) else 0,
            1 if _gt(atr_v, 0.5 * smaatr_v) else 0,
            1 if _lt(c0, e20) else 0,
            1 if _lt(c0, o0) else 0,
            1 if (v0 * c0 > 5_000_000) else 0,
            1 if _lt(c0, l1) else 0,
            1 if _lt(v5, v5_p) else 0,
            1 if _lt(c0, l2) else 0,
            1 if c0 and ((h0 - l0) / c0 < 0.015) else 0,
            1 if _lt(c15[i], o15[i]) else 0,
            1 if _lt(c0, o0) else 0,
            1 if _gt(d_open[i], 50) else 0,
            1 if _lt(d_close[i], e39d) else 0,
            1 if _lt(c15[i], e39_15[i]) else 0,
            1 if _lt(l0, ll7_p) else 0,
            1 if f5_pct < 5 else 0,
            1 if smax_first6 < 1.5 else 0,
            1 if _gt(c0, daily_s2) else 0,
        ]
        scond22 = 1 if (s_retest if before_1015 else (original_bd or s_retest)) else 0
        sbase = int(sum(scond))
        sfailed = 21 - sbase
        first5size = f5_pct <= 2.5
        firstsellsize = f5_pct <= 3.5
        body_ok = s_max_body <= s_first5_full or s_max_body <= s_first5_body

        if not is_today:
            continue

        code = 0
        c22f = cond22 == 0
        only21_c22f = base == 21 and c22f
        normal_trig = base + cond22 if before_1015 else (
            22 if base >= 21 else (22 if (base >= 20 and b_retest) else base + cond22)
        )
        if use_ex:
            trig = 22 if base >= 21 else base + 1
        else:
            trig = normal_trig
        normal = (trig >= 22) and fin_buy and (not buy_fired) and first5_is_day_low and (not use_ex)
        ex = use_ex and (base >= 20) and is_new_day_high and broke_pdh and fin_buy and (not buy_fired) and (broke_pdh or entry_buy_today) and vwapnotfar
        ex17 = only21_c22f and is_new_day_high and fin_buy and (not buy_fired) and (broke_pdh or entry_buy_today) and is_close_above_dh and is_vwap_bullish
        # Pine table prefers BUY-EX when both fire. Yahoo 09:15/prev-close makes
        # vwapnotfar extra-true, so BUY-EX hides EX17 (13 Oct WIPRO/TECHM/PERSISTENT
        # TV gold). EX17's extra gates (c22 fail + close above day-high + VWAP
        # bullish) are the TV EX17 rows; yield so they are not stored as BUY-EX.
        # COFORGE 13 Oct: EX17 false (vwapBull=0) → BUY-EX kept.
        if ex and ex17:
            ex = False
        ex4 = (base == 21) and (not first5_is_day_low) and is_close_above_dh and above_pp and fin_buy and (not buy_fired) and (broke_pdh or entry_buy_today)
        ex5 = only21_c22f and is_new_day_high and fin_buy and (not buy_fired) and is_close_above_dh and (broke_pdh or entry_buy_today)
        ex6 = (base >= 21) and c22f and (not first5_is_day_low) and is_new_day_high and is_close_above_dh and fin_buy and (not buy_fired) and above_pp
        ex7 = (base == 21) and is_new_day_high and fin_buy and (not buy_fired) and (broke_pdh or entry_buy_today)
        ex8 = only21_c22f and is_new_day_high and is_close_above_dh and fin_buy and (not buy_fired) and entry_buy_today and bmax_ok and vwapnotfar
        ex9 = (18 <= base <= 20) and fin_buy and (not buy_fired) and entry_buy_today and (c0 > v15) and vwapnotfar and below_pp and below_pdh
        ex10 = only21_c22f and entry_buy_today and fin_buy and (not buy_fired)
        ex11 = only21_c22f and close_above_orb and is_new_day_high and fin_buy and (not buy_fired) and vwapnotfar
        ex12 = (base == 21) and (cond22 == 1) and close_above_orb and fin_buy and (not buy_fired)
        ex13 = (base == 20) and (cond[19] == 0) and c22f and entry_buy_today and fin_buy and (not buy_fired)

        chain_b = [
            (101, ex), (102, ex17), (103, ex4), (104, ex5), (105, ex6), (106, ex7),
            (107, ex8), (108, ex9), (109, ex10), (110, ex11), (111, ex12), (112, ex13), (80, normal),
        ]
        for cd, ok in chain_b:
            if ok:
                code = cd
                break
        if code == 0 and entry_buy_today and (ts.hour < 9 or (ts.hour == 9 and ts.minute < 45)):
            code = 90
        if code in BUY_LOCK:
            buy_fired = True

        if code == 0:
            sc22f = scond22 == 0
            suse = sfailed < 2
            sex1_req = entry_sell_today if before_1015 else (entry_sell_today or broke_pdl)
            snormal_trig = sbase + scond22 if before_1015 else (
                22 if sbase >= 21 else (22 if (sbase >= 20 and s_retest) else sbase + scond22)
            )
            snormal = (
                (snormal_trig >= 22) and fin_sell and (not sell_fired) and first5_is_day_high
                and (not suse or sbase == 21) and s_max_body < 2
                and (is_close_below_dl or entry_sell_today) and close_below_orb
            )
            sex1 = (
                suse and sbase >= 21 and fin_sell and (not sell_fired) and sex1_req
                and s_max_body < 1.25 and first5size and not_bull_hammer and body_ok
                and no_rel_spike and entry_sell_today
            )
            se2 = (
                is_new_day_low and sbase >= 21 and fin_sell and (not sell_fired)
                and (not snormal) and (not sex1) and entry_sell_today and is_close_below_dl and not_bull_hammer
            )
            se3 = (
                sbase >= 21 and is_new_day_low and fin_sell and (not sell_fired)
                and (not snormal) and (not sex1) and (not se2) and no_rel_spike and not_bull_hammer
                and body_ok and is_close_below_dl and (entry_sell_today or broke_pdl) and s_max_body < 1.25
            )
            se4 = (
                scond[18] == 0 and scond[19] == 0 and sbase >= 19 and is_new_day_low and fin_sell
                and (not sell_fired) and (not snormal) and (not sex1) and (not se2) and (not se3)
                and is_close_below_dl and entry_sell_today and not_bull_hammer and body_ok and no_rel_spike
            )
            se5 = (
                sc22f and sbase >= 19 and (21 - sbase) <= 2 and fin_sell and (not sell_fired)
                and is_new_day_low and is_close_below_dl and (broke_pdl or entry_sell_today)
                and s_max_body < 1.25 and not_bull_hammer and body_ok and no_rel_spike and above_s3
            )
            se5s = (
                sc22f and sbase >= 21 and is_new_day_low and fin_sell and (not sell_fired)
                and (not snormal) and (not sex1) and (not se2) and (not se3) and (not se5)
                and (entry_sell_today if before_1015 else broke_pdl) and is_close_below_dl
                and first5size and s_max_body < 2 and not_bull_hammer and body_ok and is_vwap_bearish and above_s3
            )
            se6 = (
                sc22f and (not first5_is_day_high) and sbase >= 21 and is_new_day_low and fin_sell
                and (not sell_fired) and (not snormal) and (not sex1) and (not se2) and (not se3) and (not se5)
                and entry_sell_today and is_close_below_dl and first5size
            )
            se7 = (
                sc22f and sbase >= 21 and is_new_day_low and fin_sell and (not sell_fired)
                and (not snormal) and (not sex1) and (not se2) and (not se3) and (not se5) and (not se5s) and (not se6)
                and (entry_sell_today or broke_pdl) and first5size and s_max_body < 2 and not_bull_hammer
                and body_ok and no_rel_spike and is_close_below_dl and is_vwap_bearish
            )
            se8 = (
                sc22f and sbase >= 21 and is_new_day_low and fin_sell and (not sell_fired)
                and (not snormal) and (not sex1) and (not se2) and (not se3) and (not se5) and (not se5s)
                and (not se6) and (not se7) and s_max_body < 1.5 and not_bull_hammer and is_close_below_dl and no_rel_spike
            )
            se9 = (
                sbase >= 21 and is_new_day_low and fin_sell and (not sell_fired)
                and (not snormal) and (not sex1) and (not se2) and (not se3) and (not se4) and (not se5)
                and (not se5s) and (not se6) and (not se7) and (not se8)
                and is_close_below_dl and not_bull_hammer and s_max_body < 1.5 and entry_sell_today
                and body_ok and first5size and is_vwap_bearish and no_rel_spike
            )
            se10 = (
                sbase >= 21 and is_new_day_low and fin_sell and (not sell_fired)
                and (not snormal) and (not sex1) and (not se2) and (not se3) and (not se4) and (not se5)
                and (not se5s) and (not se6) and (not se7) and (not se8) and (not se9)
                and is_close_below_dl and not_bull_hammer and s_max_body < 1.25 and entry_sell_today
                and body_ok and no_rel_spike
            )
            se11 = (
                sc22f and (not first5_is_day_high) and sbase >= 21 and is_new_day_low and fin_sell
                and (not sell_fired) and (not snormal) and (not sex1) and (not se2) and (not se3) and (not se4)
                and (not se5) and (not se5s) and (not se6) and (not se7) and (not se8) and (not se9) and (not se10)
                and not_bull_hammer and s_max_body < 1.5 and body_ok and no_rel_spike and is_close_below_dl
            )
            se12 = (
                sbase >= 21 and is_new_day_low and fin_sell and (not sell_fired)
                and (not snormal) and (not sex1) and (not se2) and (not se3) and (not se4) and (not se5)
                and (not se5s) and (not se6) and (not se7) and (not se8) and (not se9) and (not se10) and (not se11)
                and is_close_below_dl and s_max_body < 1.25 and body_ok and (entry_sell_today or broke_pdl)
                and is_vwap_bearish and no_rel_spike and firstsellsize
            )
            se13 = (
                sbase >= 21 and is_new_day_low and fin_sell and (not sell_fired)
                and (not snormal) and (not sex1) and (not se2) and (not se3) and (not se4) and (not se5)
                and (not se5s) and (not se6) and (not se7) and (not se8) and (not se9) and (not se10)
                and (not se11) and (not se12)
                and is_close_below_dl and not_bull_hammer and entry_sell_today and body_ok
            )
            se14 = (
                sc22f and sbase >= 21 and is_new_day_low and fin_sell and (not sell_fired)
                and (not snormal) and (not sex1) and (not se2) and (not se3) and (not se4) and (not se5)
                and (not se5s) and (not se6) and (not se7) and (not se8) and (not se9) and (not se10)
                and (not se11) and (not se12) and (not se13)
                and is_close_below_dl and not_bull_hammer and s_max_body < 1.25 and body_ok
                and no_rel_spike and entry_sell_today
            )
            se15 = (
                sbase >= 21 and fin_sell and (not sell_fired) and (not snormal) and (not sex1) and (not se2)
                and not_bull_hammer and body_ok and entry_sell_today
            )
            se16 = (
                sc22f and sbase >= 21 and is_new_day_low and fin_sell and (not sell_fired)
                and (not snormal) and (not sex1) and (not se2) and (not se3) and (not se4) and (not se5)
                and (not se5s) and (not se6) and (not se7) and (not se8) and (not se9) and (not se10)
                and (not se11) and (not se12) and (not se13) and (not se14) and (not se15)
                and not_bull_hammer and entry_sell_today
            )
            se17 = (
                sc22f and sbase >= 21 and is_new_day_low and fin_sell and (not sell_fired)
                and (not snormal) and (not sex1) and (not se2) and (not se3) and (not se4) and (not se5)
                and (not se5s) and (not se6) and (not se7) and (not se8) and (not se9) and (not se10)
                and (not se11) and (not se12) and (not se13) and (not se14) and (not se15) and (not se16)
                and is_close_below_dl and not_bull_hammer and s_max_body < 1.25 and body_ok
                and no_rel_spike and close_below_orb
            )
            se18 = (
                sc22f and sbase >= 21 and is_new_day_low and fin_sell and (not sell_fired)
                and (not snormal) and (not sex1) and (not se2) and (not se3) and (not se4) and (not se5)
                and (not se5s) and (not se6) and (not se7) and (not se8) and (not se9) and (not se10)
                and (not se11) and (not se12) and (not se13) and (not se14) and (not se15) and (not se16) and (not se17)
                and (is_close_below_dl or entry_sell_today) and not_bull_hammer and s_max_body < 1.25
                and body_ok and no_rel_spike and close_below_orb and is_vwap_bearish
            )
            se19 = (
                scond[17] == 0 and sc22f and sbase >= 20 and fin_sell and (not sell_fired)
                and (not snormal) and (not sex1) and (not se2) and (not se3) and (not se4) and (not se5)
                and (not se5s) and (not se6) and (not se7) and (not se8) and (not se9) and (not se10)
                and (not se11) and (not se12) and (not se13) and (not se14) and (not se15) and (not se16)
                and (not se17) and (not se18)
                and not_bull_hammer and close_below_orb and is_vwap_bearish
            )

            chain_s = [
                (201, sex1), (202, se2), (203, se3), (204, se4), (205, se5), (206, se5s),
                (207, se6), (208, se7), (209, se8), (210, se9), (211, se10), (212, se11),
                (213, se12), (214, se13), (215, se14), (216, se15), (217, se16), (218, se17),
                (219, se18), (220, se19), (280, snormal),
            ]
            for cd, ok in chain_s:
                if ok:
                    code = cd
                    break
            if code == 0 and entry_sell_today and (ts.hour < 9 or (ts.hour == 9 and ts.minute < 45)):
                code = 290
            if code in SELL_LOCK:
                sell_fired = True

        if code and is_today:
            side = "BUY" if is_buy_code(code) else "SELL"
            sl = round(l0 * 0.995, 2) if side == "BUY" else round(h0 * 1.005, 2)
            fires.append(
                StockSignal(
                    symbol=symbol,
                    code=code,
                    name=SIGNAL_NAME.get(code, str(code)),
                    side=side,
                    time=ts.strftime("%H:%M"),
                    price=round(float(c0), 2),
                    score=int(total),
                    strength=strength,
                    base_active=base if side == "BUY" else sbase,
                    cond22=cond22 if side == "BUY" else scond22,
                    reason=f"base {base if side=='BUY' else sbase}/21 cond22={cond22 if side=='BUY' else scond22} score={total}",
                    sl=sl,
                )
            )

    last_hhmm = to_ist(df5.index[-1]).strftime("%H:%M")
    return fires if keep_all else _table_view(fires, last_hhmm)
