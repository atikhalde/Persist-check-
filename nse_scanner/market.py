"""NIFTY direction + sector RS / persistent leaders.

Port of the decision core from
`NIFTY Institutional Core + Flow [v4.0 Compact Table]`.

Institutional OI / PCR / MaxPain are not available on yfinance and are omitted.
VIX, Nifty structure, 15-sector breadth, live flow and persistent +/- are kept.

Scanner pick (user rule):
  only the strongest PERSISTENT bull or bear sector that COMPLEMENTS
  the committed Nifty direction that day. CHOP / NEUTRAL / conflict → WAIT.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import ta
from .data import sanitize_index_5m
from .session import (
    FREEZE_AFTER_MIN,
    IST,
    OPEN_BLOCK_MIN,
    OR_MINUTES,
    market_open_minutes,
    session_date,
    to_ist,
)

SECTOR_ORDER = [
    "AUTO",
    "FIN",
    "IT",
    "MET",
    "DEF",
    "ENE",
    "FMCG",
    "OIL",
    "PHA",
    "PSE",
    "PSUBK",
    "BANK",
    "REALTY",
    "CDUR",
    "MEDIA",
]

ADX_TREND_MIN = 20.0
STRONG_ADX_MIN = 25.0
MTF_NEED = 2
DIR_CONFIRM = 1
DIR_EXIT = 2
DIR_ALERT_MIN_CONF = 65
FLOW_MIN = 55.0
FLOW_SIDE_EDGE = 4.0
BULL_MIN_DAY = 0.20
BEAR_MIN_DAY = 0.20
BULL_MIN_POS = 60.0
BEAR_MAX_POS = 40.0
RECENT_MOVE_MIN = 0.07
RECENT_HOLD_MIN = 60.0
SECTOR_LOOKBACK = 8
SECTOR_SMOOTH = 5
RECENT_FLOW_BARS = 4
VOL_SCALE_REF = 14.0
VIX_SPIKE = 4.0
CONCRETE_COMMIT = 2
CONCRETE_MIN_CONF = 65
CONCRETE_WARMUP = 3
EARLY_ROT_BOOST = 8.0
EARLY_ROT_UNTIL = 135
CLUSTER_GAP = 1.5


def _pos_in_range(c, hi, lo) -> float:
    rng = hi - lo
    if rng <= 0:
        return 50.0
    return float(np.clip((c - lo) / rng * 100.0, 0.0, 100.0))


def _bull_map(x, lo, hi) -> float:
    return float(np.clip(100.0 * (x - lo) / max(hi - lo, 1e-4), 0.0, 100.0))


def _bear_map(x, lo, hi) -> float:
    return float(np.clip(100.0 * (hi - x) / max(hi - lo, 1e-4), 0.0, 100.0))


def _htf_last_close_vs_ema(df5: pd.DataFrame, minutes: int, ema_len: int = 20):
    """Completed HTF bar close vs EMA (lookahead_off)."""
    if df5.empty:
        return False, 0.0, 0.0
    rule = f"{minutes}min"
    origin = pd.Timestamp("1970-01-01 09:15", tz=IST)
    ohlc = (
        df5.resample(rule, label="left", closed="left", origin=origin)
        .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
        .dropna(subset=["Close"])
    )
    if len(ohlc) < ema_len + 2:
        return False, 0.0, 0.0
    e = ta.ema(ohlc["Close"].to_numpy(), ema_len)
    last_t = ohlc.index[-1]
    last_5 = df5.index[-1]
    bucket_end = last_t + pd.Timedelta(minutes=minutes)
    # lookahead_off: ignore the still-forming HTF bucket
    forming = last_5 + pd.Timedelta(minutes=5) <= bucket_end
    idx = -2 if forming and len(ohlc) >= 2 else -1
    c = float(ohlc["Close"].iloc[idx])
    ev = float(e[idx]) if not np.isnan(e[idx]) else c
    return c > ev, c, ev


def _prepare_index(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x = x.sort_index()
    sid = [session_date(t) for t in x.index]
    x["sid"] = sid
    x["EMA9"] = ta.ema(x["Close"], 9)
    x["EMA21"] = ta.ema(x["Close"], 21)
    x["EMA50"] = ta.ema(x["Close"], 50)
    x["EMA20"] = ta.ema(x["Close"], 20)
    _, _, x["ADX"] = ta.adx(x["High"], x["Low"], x["Close"], 14)
    x["VWAP"] = ta.session_vwap(x["High"], x["Low"], x["Close"], x["Volume"], sid)
    return x


@dataclass
class SectorSnap:
    name: str
    close: float
    day_ret: float
    intra_ret: float
    rs: float
    pos: float
    recent: float
    hold_bull: float
    hold_bear: float
    bull: float
    bear: float
    persist_bull: float
    persist_bear: float
    side: int
    score: float
    qualified: bool


@dataclass
class MarketDecision:
    session: str
    bar_time: str
    nifty: float
    nifty_day_ret: float
    nifty_intra: float
    vix: float
    vix_chg: float
    adx: float
    mtf_bull: int
    dir_state: int
    dir_label: str
    dir_conf: int
    dir_tier: str
    breadth_adv: int
    breadth_dec: int
    regime: str
    persist_plus: list
    persist_minus: list
    live_flow: str
    live_flow_side: int
    live_flow_score: float
    live_flow_qualified: bool
    pick_sector: str
    pick_side: int
    pick_reason: str
    action: str
    sectors: dict = field(default_factory=dict)


def _day_ohlc_so_far(df: pd.DataFrame, sid) -> tuple[float, float, float, float]:
    d = df[df["sid"] == sid]
    if d.empty:
        return np.nan, np.nan, np.nan, np.nan
    return (
        float(d["Open"].iloc[0]),
        float(d["High"].max()),
        float(d["Low"].min()),
        float(d["Close"].iloc[-1]),
    )


def _prior_close(d1: pd.DataFrame | None, df5: pd.DataFrame | None, sid, fallback: float) -> float:
    """Pine `request.security(..., "D", close[1], lookahead_off)`.

    Yahoo 1d history for several NSE sector indices is only *today's* bar, so
    using `d1.Close.iloc[-1]` made day_ret = 0. Prefer a daily bar strictly
    before `sid`; else the last 5m close of the previous session.
    """
    if d1 is not None and len(d1):
        for i in range(len(d1) - 1, -1, -1):
            if session_date(d1.index[i]) < sid:
                v = float(d1["Close"].iloc[i])
                if v == v and v != 0.0:
                    return v
    if df5 is not None and len(df5):
        sids = df5["sid"].to_numpy() if "sid" in df5.columns else np.array([session_date(t) for t in df5.index])
        closes = df5["Close"].to_numpy(dtype=float)
        for i in range(len(closes) - 1, -1, -1):
            if sids[i] < sid:
                v = float(closes[i])
                if v == v and v != 0.0:
                    return v
    return float(fallback) if fallback == fallback and fallback != 0.0 else 0.0


def _intra_by_session(df: pd.DataFrame) -> pd.Series:
    day_open = df.groupby("sid", sort=False)["Open"].transform("first")
    return (df["Close"] - day_open) / day_open.replace(0, np.nan) * 100.0


def run_market(
    nifty_5: pd.DataFrame,
    nifty_d: pd.DataFrame,
    vix_5: pd.DataFrame,
    vix_d: pd.DataFrame,
    sector_5: dict[str, pd.DataFrame],
    sector_d: dict[str, pd.DataFrame],
) -> MarketDecision:
    nifty_5 = sanitize_index_5m(nifty_5)
    sector_5 = {k: sanitize_index_5m(v) for k, v in (sector_5 or {}).items()}
    n = _prepare_index(nifty_5)
    if n.empty:
        raise RuntimeError("No Nifty 5m data")

    last = n.index[-1]
    last = to_ist(last)
    sid = session_date(last)
    today = n[n["sid"] == sid]
    if today.empty:
        today = n.iloc[-75:]
        sid = session_date(today.index[-1])
        last = to_ist(today.index[-1])

    n_open, n_high, n_low, n_close = _day_ohlc_so_far(n, sid)
    prev_close = _prior_close(nifty_d, n, sid, n_open)
    n_day_ret = (n_close - prev_close) / prev_close * 100.0 if prev_close else 0.0
    n_intra = (n_close - n_open) / n_open * 100.0 if n_open else 0.0

    vix_live = float(vix_5["Close"].iloc[-1]) if vix_5 is not None and len(vix_5) else np.nan
    # Pine: request.security(INDIAVIX, "D", [close, close[1]], lookahead_off).
    # Yahoo often has today's VIX daily close as NaN, so the last *completed*
    # daily is yesterday — same as TV on 12 Oct: 14.38 vs 15.28 = -5.89%.
    vix_completed: list[tuple] = []
    if vix_d is not None and len(vix_d):
        for ts, row in vix_d.iterrows():
            c = float(row["Close"])
            d = session_date(ts)
            if c == c and d < sid:
                vix_completed.append((d, c))
    if len(vix_completed) >= 2:
        vix_today = vix_completed[-1][1]
        vix_prev = vix_completed[-2][1]
    elif len(vix_completed) == 1:
        vix_today = vix_live if vix_live == vix_live else vix_completed[0][1]
        vix_prev = vix_completed[0][1]
    else:
        vix_today = vix_live
        vix_prev = vix_live
    vix_chg = (vix_today - vix_prev) / vix_prev * 100.0 if vix_prev else 0.0
    vol_mult = float(np.clip((vix_live / VOL_SCALE_REF) if vix_live else 1.0, 0.6, 2.5))

    # MTF
    mtf_flags = []
    for mins in (15, 30, 60):
        try:
            bull, _, _ = _htf_last_close_vs_ema(n[["Open", "High", "Low", "Close", "Volume"]], mins, 20)
            mtf_flags.append(1 if bull else 0)
        except Exception:
            mtf_flags.append(0)
    mtf_bull = int(sum(mtf_flags))
    mtf_bear = 3 - mtf_bull

    # Opening range on today
    or_high = np.nan
    or_low = np.nan
    above_vwap = 0
    below_vwap = 0
    bull_setup = 0
    bear_setup = 0
    dir_state = 0
    bull_break = 0
    bear_break = 0
    conc_commit = 0
    concrete = 0
    bars_today = 0

    row_i = {t: i for i, t in enumerate(n.index)}

    for ts, row in today.iterrows():
        bars_today += 1
        mom = market_open_minutes(to_ist(ts))
        c, o, h, l = float(row["Close"]), float(row["Open"]), float(row["High"]), float(row["Low"])
        e9, e21, e50 = float(row["EMA9"]), float(row["EMA21"]), float(row["EMA50"])
        vwap = float(row["VWAP"])
        adx = float(row["ADX"]) if not np.isnan(row["ADX"]) else 0.0
        if mom < OR_MINUTES or np.isnan(or_high):
            or_high = h if np.isnan(or_high) else max(or_high, h)
            or_low = l if np.isnan(or_low) else min(or_low, l)
        n_or_ready = mom >= OR_MINUTES and not np.isnan(or_high)
        vwap_slope = vwap - float(n["VWAP"].shift(3).loc[ts]) if ts in n.index else 0.0
        if np.isnan(vwap_slope):
            vwap_slope = 0.0
        or_up = n_or_ready and c > or_high
        or_dn = n_or_ready and c < or_low

        if c > vwap:
            above_vwap += 1
            below_vwap = 0
        elif c < vwap:
            below_vwap += 1
            above_vwap = 0

        bull_core = c > vwap and e9 > e21 and e21 > e50 and mtf_bull >= MTF_NEED and adx >= ADX_TREND_MIN
        bear_core = c < vwap and e9 < e21 and e21 < e50 and mtf_bear >= MTF_NEED and adx >= ADX_TREND_MIN
        bull_early = or_up and c > vwap and vwap_slope >= 0 and e9 > e21 and mtf_bull >= MTF_NEED and adx >= ADX_TREND_MIN * 0.85
        bear_early = or_dn and c < vwap and vwap_slope <= 0 and e9 < e21 and mtf_bear >= MTF_NEED and adx >= ADX_TREND_MIN * 0.85
        bull_s = bull_core or bull_early
        bear_s = bear_core or bear_early

        if bull_s:
            bull_setup += 1
            bear_setup = 0
        elif bear_s:
            bear_setup += 1
            bull_setup = 0
        else:
            bull_setup = bear_setup = 0

        day_hi = float(today.loc[:ts, "High"].max())
        day_lo = float(today.loc[:ts, "Low"].min())
        npos = _pos_in_range(c, day_hi, day_lo)
        intra = (c - n_open) / n_open * 100.0 if n_open else 0.0

        if dir_state == 0:
            if bull_setup >= DIR_CONFIRM:
                dir_state = 1
                bull_break = bear_break = 0
            elif bear_setup >= DIR_CONFIRM:
                dir_state = -1
                bull_break = bear_break = 0
        elif dir_state == 1:
            hard = (c < vwap and (e9 < e21 or mtf_bull <= 1)) and npos <= 35
            soft = c < vwap or e9 < e21 or mtf_bull <= 1 or npos <= 45 or intra <= -0.20
            if hard:
                dir_state = 0
                bull_setup = bear_setup = above_vwap = below_vwap = bull_break = 0
            else:
                bull_break = bull_break + 1 if soft else max(0, bull_break - 1)
                if bull_break >= DIR_EXIT:
                    dir_state = 0
                    bull_setup = bear_setup = above_vwap = below_vwap = bull_break = 0
        else:
            hard = (c > vwap and (e9 > e21 or mtf_bear <= 1)) and npos >= 65
            soft = c > vwap or e9 > e21 or mtf_bear <= 1 or npos >= 55 or intra >= 0.20
            if hard:
                dir_state = 0
                bull_setup = bear_setup = above_vwap = below_vwap = bear_break = 0
            else:
                bear_break = bear_break + 1 if soft else max(0, bear_break - 1)
                if bear_break >= DIR_EXIT:
                    dir_state = 0
                    bull_setup = bear_setup = above_vwap = below_vwap = bear_break = 0

        if dir_state == 1:
            conc_commit = conc_commit + 1 if conc_commit >= 0 else 1
        elif dir_state == -1:
            conc_commit = conc_commit - 1 if conc_commit <= 0 else -1
        else:
            conc_commit = 0

        warmed = bars_today > CONCRETE_WARMUP
        last_adx = adx
        last_vwap = vwap
        last_pos = npos
        last_or_up = or_up
        last_or_dn = or_dn
        last_slope = vwap_slope

        # concrete lock (simplified: confirm bars + min conf, no OI conflict)
        conf_now = _dir_conf(mtf_bull, mtf_bear, above_vwap, below_vwap, adx, npos, dir_state, or_up, or_dn, vwap_slope)
        if concrete == 0 and warmed:
            if conc_commit >= CONCRETE_COMMIT and conf_now >= CONCRETE_MIN_CONF and dir_state == 1:
                concrete = 1
            elif -conc_commit >= CONCRETE_COMMIT and conf_now >= CONCRETE_MIN_CONF and dir_state == -1:
                concrete = -1
        elif concrete != 0 and warmed and dir_state == -concrete and abs(conc_commit) >= CONCRETE_COMMIT:
            # tech flip
            if conf_now >= CONCRETE_MIN_CONF:
                concrete = dir_state

    last_row = today.iloc[-1]
    adx = float(last_row["ADX"]) if not np.isnan(last_row["ADX"]) else 0.0
    vwap = float(last_row["VWAP"])
    npos = _pos_in_range(n_close, n_high, n_low)
    dir_conf = _dir_conf(mtf_bull, mtf_bear, above_vwap, below_vwap, adx, npos, dir_state, last_or_up, last_or_dn, last_slope)
    strong_bull = dir_state == 1 and mtf_bull == 3 and above_vwap >= 4 and adx >= STRONG_ADX_MIN and npos >= 60
    strong_bear = dir_state == -1 and mtf_bear == 3 and below_vwap >= 4 and adx >= STRONG_ADX_MIN and npos <= 40
    dir_label = (
        "STRONG BULL" if strong_bull else
        "BULL" if dir_state == 1 else
        "STRONG BEAR" if strong_bear else
        "BEAR" if dir_state == -1 else
        "NEUTRAL"
    )
    dir_tier = "TREND-LOCK" if dir_conf >= 85 else "STRONG" if dir_conf >= 70 else "BUILDING" if dir_conf >= 55 else "WEAK"

    # Prefer concrete if committed
    commit_side = concrete if concrete != 0 else dir_state

    snaps: dict[str, SectorSnap] = {}
    for name in SECTOR_ORDER:
        df = sector_5.get(name)
        d1 = sector_d.get(name)
        if df is None or df.empty:
            continue
        snaps[name] = _sector_snap(name, df, d1, sid, n, vol_mult, last)

    if not snaps:
        raise RuntimeError("No sector index data")

    breadth_adv = sum(1 for s in snaps.values() if s.day_ret > 0)
    breadth_dec = len(snaps) - breadth_adv

    regime = (
        "TREND UP" if commit_side == 1 and breadth_adv >= 9 and adx >= STRONG_ADX_MIN else
        "TREND DOWN" if commit_side == -1 and breadth_dec >= 9 and adx >= STRONG_ADX_MIN else
        "CHOP" if commit_side == 0 and adx < ADX_TREND_MIN else
        "TRANSITION"
    )

    persist_plus = sorted(snaps.values(), key=lambda s: s.persist_bull, reverse=True)
    persist_minus = sorted(snaps.values(), key=lambda s: s.persist_bear, reverse=True)
    live_ranked = sorted(snaps.values(), key=lambda s: s.score, reverse=True)
    live_qual = next((s for s in live_ranked if s.qualified), live_ranked[0])

    pick, pick_side, reason = _complementary_pick(
        commit_side, dir_conf, regime, persist_plus, persist_minus, vix_chg, n_day_ret
    )

    action = "WAIT"
    if pick and pick_side == 1 and commit_side == 1:
        action = "SCAN LONG"
    elif pick and pick_side == -1 and commit_side == -1:
        action = "SCAN SHORT"

    return MarketDecision(
        session=str(sid),
        bar_time=last.strftime("%Y-%m-%d %H:%M"),
        nifty=round(n_close, 2),
        nifty_day_ret=round(n_day_ret, 3),
        nifty_intra=round(n_intra, 3),
        vix=round(float(vix_live), 2) if vix_live == vix_live else float("nan"),
        vix_chg=round(vix_chg, 2),
        adx=round(adx, 1),
        mtf_bull=mtf_bull,
        dir_state=int(commit_side),
        dir_label=dir_label if commit_side != 0 else "NEUTRAL",
        dir_conf=int(dir_conf),
        dir_tier=dir_tier,
        breadth_adv=breadth_adv,
        breadth_dec=breadth_dec,
        regime=regime,
        persist_plus=[(s.name, round(s.persist_bull, 1)) for s in persist_plus[:3]],
        persist_minus=[(s.name, round(s.persist_bear, 1)) for s in persist_minus[:3]],
        live_flow=live_qual.name,
        live_flow_side=live_qual.side,
        live_flow_score=round(live_qual.score, 1),
        live_flow_qualified=bool(live_qual.qualified),
        pick_sector=pick or "WAIT",
        pick_side=int(pick_side),
        pick_reason=reason,
        action=action,
        sectors={k: vars(v) for k, v in snaps.items()},
    )


def _dir_conf(mtf_bull, mtf_bear, above, below, adx, npos, dir_state, or_up, or_dn, slope) -> int:
    core = (abs(mtf_bull - mtf_bear) / 3.0) * 35 + (min(max(above, below), 6) / 6.0) * 25
    core += _bull_map(adx, 15, 35) * 0.25 + abs(npos - 50.0) * 0.3
    boost = 0.0
    if dir_state == 1 and or_up:
        boost += 5
    elif dir_state == -1 and or_dn:
        boost += 5
    elif dir_state == 1 and not or_up:
        boost -= 2
    elif dir_state == -1 and not or_dn:
        boost -= 2
    if dir_state == 1 and slope > 0:
        boost += 4
    elif dir_state == -1 and slope < 0:
        boost += 4
    elif dir_state != 0:
        boost -= 3
    return int(round(float(np.clip(core + boost, 0, 100))))


def _sector_snap(name, df5, d1, sid, nifty_df, vol_mult, last_ts) -> SectorSnap:
    """Pine `sectorMetric` + v3.9 `pBull`/`pBear` on the last available bar."""
    x = df5.copy().sort_index()
    x["sid"] = [session_date(t) for t in x.index]
    close = x["Close"].to_numpy(dtype=float)
    high = x["High"].to_numpy(dtype=float)
    low = x["Low"].to_numpy(dtype=float)
    opn = x["Open"].to_numpy(dtype=float)
    e20 = ta.ema(close, 20)
    x["EMA20"] = e20
    sids = x["sid"].to_numpy()

    today_idx = np.where(sids == sid)[0]
    if len(today_idx) == 0:
        today_idx = np.arange(max(0, len(x) - 80), len(x))
    last_i = int(today_idx[-1])
    c = float(close[last_i])
    o = float(opn[int(today_idx[0])])
    hi = float(np.nanmax(high[today_idx]))
    lo = float(np.nanmin(low[today_idx]))

    prev = _prior_close(d1, x, sid, o)
    day_ret = (c - prev) / prev * 100.0 if prev else 0.0
    intra = (c - o) / o * 100.0 if o else 0.0

    # rs = intraRet - nIntraRet  (per bar), then ta.ema(rs, 5)
    sec_intra = _intra_by_session(x)
    n = nifty_df
    if n is None or n.empty:
        n_intra_al = pd.Series(0.0, index=x.index)
    else:
        nn = n.copy()
        if "sid" not in nn.columns:
            nn["sid"] = [session_date(t) for t in nn.index]
        n_intra_al = _intra_by_session(nn).reindex(x.index, method="ffill")
    rs = (sec_intra - n_intra_al).to_numpy(dtype=float)
    rs_smooth_arr = ta.ema(rs, SECTOR_SMOOTH)

    above = np.where(~np.isnan(e20) & (close > e20), 100.0, 0.0)
    below = np.where(~np.isnan(e20) & (close < e20), 100.0, 0.0)
    hold_bull_all_arr = ta.sma(above, SECTOR_LOOKBACK)
    hold_bear_all_arr = ta.sma(below, SECTOR_LOOKBACK)
    hold_bull_r_arr = ta.sma(above, RECENT_FLOW_BARS)
    hold_bear_r_arr = ta.sma(below, RECENT_FLOW_BARS)

    n_bars = len(x)
    bull_raw = np.zeros(n_bars)
    bear_raw = np.zeros(n_bars)
    recent_arr = np.zeros(n_bars)

    last_sid = None
    d_h = d_l = np.nan
    or_h = or_l = np.nan
    bt = 0
    session_prev = np.nan

    for i in range(n_bars):
        if sids[i] != last_sid:
            session_prev = float(close[i - 1]) if i > 0 else prev
            d_h, d_l = high[i], low[i]
            or_h = or_l = np.nan
            bt = 1
            last_sid = sids[i]
        else:
            d_h = max(d_h, high[i])
            d_l = min(d_l, low[i])
            bt += 1
        mom = market_open_minutes(to_ist(x.index[i]))
        if mom < OR_MINUTES:
            or_h = high[i] if np.isnan(or_h) else max(or_h, high[i])
            or_l = low[i] if np.isnan(or_l) else min(or_l, low[i])

        pc = prev if sids[i] == sid else (session_prev if session_prev == session_prev and session_prev != 0.0 else opn[i])
        dr = (close[i] - pc) / pc * 100.0 if pc else 0.0
        pos_i = _pos_in_range(close[i], d_h, d_l)
        rec_bars = min(RECENT_FLOW_BARS, max(1, bt - 1))
        j = i - rec_bars
        cref = close[j] if j >= 0 else close[0]
        recent = (close[i] - cref) / cref * 100.0 if cref else 0.0
        recent_arr[i] = recent

        rs_s = rs_smooth_arr[i]
        if np.isnan(rs_s):
            rs_s = 0.0 if np.isnan(rs[i]) else float(rs[i])
        hb_all = 0.0 if np.isnan(hold_bull_all_arr[i]) else float(hold_bull_all_arr[i])
        hr_b = 0.0 if np.isnan(hold_bull_r_arr[i]) else float(hold_bull_r_arr[i])
        hb_bear = 0.0 if np.isnan(hold_bear_all_arr[i]) else float(hold_bear_all_arr[i])
        hr_bear = 0.0 if np.isnan(hold_bear_r_arr[i]) else float(hold_bear_r_arr[i])
        ev = close[i] if np.isnan(e20[i]) else float(e20[i])

        bull_base = (
            _bull_map(rs_s, -0.30 * vol_mult, 1.00 * vol_mult) * 0.20
            + _bull_map(dr, -0.20 * vol_mult, 1.50 * vol_mult) * 0.15
            + _bull_map(recent, -0.25 * vol_mult, 0.90 * vol_mult) * 0.20
            + pos_i * 0.15
            + hb_all * 0.10
            + hr_b * 0.20
        )
        bear_base = (
            _bear_map(rs_s, -1.00 * vol_mult, 0.30 * vol_mult) * 0.20
            + _bear_map(dr, -1.50 * vol_mult, 0.20 * vol_mult) * 0.15
            + _bear_map(recent, -0.90 * vol_mult, 0.25 * vol_mult) * 0.20
            + (100.0 - pos_i) * 0.15
            + hb_bear * 0.10
            + hr_bear * 0.20
        )
        bull_pen = 0.70 if (recent < -RECENT_MOVE_MIN and pos_i < 65) else 1.0
        bull_pen *= 0.70 if close[i] < ev else 1.0
        bull_pen *= 0.80 if hr_b < RECENT_HOLD_MIN else 1.0
        bear_pen = 0.70 if (recent > RECENT_MOVE_MIN and pos_i > 35) else 1.0
        bear_pen *= 0.70 if close[i] > ev else 1.0
        bear_pen *= 0.80 if hr_bear < RECENT_HOLD_MIN else 1.0

        n_or_ready = mom >= OR_MINUTES and not np.isnan(or_h) and not np.isnan(or_l)
        early = mom <= EARLY_ROT_UNTIL and n_or_ready
        rs_prev = rs_s if i == 0 or np.isnan(rs_smooth_arr[i - 1]) else float(rs_smooth_arr[i - 1])
        rs_accel = rs_s - rs_prev
        early_bull = (
            early and close[i] > or_h and close[i] > ev and rs_s > 0 and rs_accel >= 0
            and recent >= 0 and pos_i >= 55
        )
        early_bear = (
            early and close[i] < or_l and close[i] < ev and rs_s < 0 and rs_accel <= 0
            and recent <= 0 and pos_i <= 45
        )
        bull_raw[i] = min(100.0, bull_base * bull_pen + (EARLY_ROT_BOOST if early_bull else 0.0))
        bear_raw[i] = min(100.0, bear_base * bear_pen + (EARLY_ROT_BOOST if early_bear else 0.0))

    bull_ema = ta.ema(bull_raw, SECTOR_SMOOTH)
    bear_ema = ta.ema(bear_raw, SECTOR_SMOOTH)

    def _last(arr, fallback):
        v = arr[last_i]
        return float(v) if v == v else float(fallback)

    bull = _last(bull_ema, bull_raw[last_i])
    bear = _last(bear_ema, bear_raw[last_i])
    rs_smooth = _last(rs_smooth_arr, 0.0)
    hold_bull_r = _last(hold_bull_r_arr, 0.0)
    hold_bear_r = _last(hold_bear_r_arr, 0.0)
    recent = float(recent_arr[last_i])
    pos = _pos_in_range(c, hi, lo)

    side = 1 if bull >= bear else -1
    score = max(bull, bear)
    qual_bull = (
        day_ret >= BULL_MIN_DAY and pos >= BULL_MIN_POS and rs_smooth > 0
        and recent >= RECENT_MOVE_MIN and bull >= bear + FLOW_SIDE_EDGE and bull >= FLOW_MIN
    )
    qual_bear = (
        day_ret <= -BEAR_MIN_DAY and pos <= BEAR_MAX_POS and rs_smooth < 0
        and recent <= -RECENT_MOVE_MIN and bear >= bull + FLOW_SIDE_EDGE and bear >= FLOW_MIN
    )
    qualified = qual_bull if side == 1 else qual_bear

    p_bull = (
        _bull_map(day_ret, -0.20 * vol_mult, 1.50 * vol_mult) * 0.30
        + _bull_map(rs_smooth, -0.30 * vol_mult, 1.00 * vol_mult) * 0.30
        + pos * 0.25
        + bull * 0.15
    )
    p_bear = (
        _bear_map(day_ret, -1.50 * vol_mult, 0.20 * vol_mult) * 0.30
        + _bear_map(rs_smooth, -1.00 * vol_mult, 0.30 * vol_mult) * 0.30
        + (100.0 - pos) * 0.25
        + bear * 0.15
    )

    return SectorSnap(
        name=name,
        close=c,
        day_ret=day_ret,
        intra_ret=intra,
        rs=rs_smooth,
        pos=pos,
        recent=recent,
        hold_bull=hold_bull_r,
        hold_bear=hold_bear_r,
        bull=bull,
        bear=bear,
        persist_bull=p_bull,
        persist_bear=p_bear,
        side=side,
        score=score,
        qualified=bool(qualified),
    )


def _complementary_pick(commit_side, dir_conf, regime, plus, minus, vix_chg, n_day_ret):
    """Strongest persistent sector on the SAME side as committed Nifty direction."""
    if commit_side == 0 or regime == "CHOP" or dir_conf < 55:
        return None, 0, f"WAIT — market {regime} conf {dir_conf}"
    # VIX spike against the day → stand aside
    if vix_chg >= VIX_SPIKE and ((commit_side == 1 and n_day_ret < 0) or (commit_side == -1 and n_day_ret > 0)):
        return None, 0, f"WAIT — VIX spike {vix_chg:.1f}% fights direction"

    pool = plus if commit_side == 1 else minus
    want_side = commit_side
    for s in pool[:3]:
        complements = (
            (want_side == 1 and s.rs >= 0 and s.day_ret >= -0.05 and s.side != -1)
            or (want_side == -1 and s.rs <= 0 and s.day_ret <= 0.05 and s.side != 1)
        )
        # reject if the name is qualified the opposite way
        if want_side == 1 and s.qualified and s.side == -1:
            continue
        if want_side == -1 and s.qualified and s.side == 1:
            continue
        if not complements:
            continue
        tag = "PERSIST +" if want_side == 1 else "PERSIST −"
        q = "qualified" if s.qualified and s.side == want_side else "persistent"
        return s.name, want_side, f"{tag} {s.name} {q} (score {s.persist_bull if want_side==1 else s.persist_bear:.0f})"
    top = pool[0].name if pool else "?"
    return None, 0, f"WAIT — persist #{top} does not complement {('BULL' if commit_side==1 else 'BEAR')} market"
