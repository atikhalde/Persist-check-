"""NIFTY direction + sector RS / persistent leaders.

Port of the decision core from
`NIFTY Institutional Core + Flow [v4.0 Compact Table]`.

Institutional OI / PCR / MaxPain are not available on yfinance and are omitted.
VIX, Nifty structure, 15-sector breadth, live flow and persistent +/- are kept.

Scanner pick (user rule):
  only the strongest PERSISTENT bull or bear sector that COMPLEMENTS
  the committed Nifty direction that day. CHOP / NEUTRAL / conflict → WAIT.
  Exception (sector follow): Nifty NEUTRAL + a qualified #1 PERSIST + sector
  → scan that sector LONG (see ``_follow_pick``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import ta
from .data import sanitize_index_5m
from .session import (
    IST,
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
# Confidence the complementary pick needs before it will name a sector. Below it
# the block says WAIT — and, by the locked rule, nothing is scanned. It was a
# literal 55 in three places; a WAIT block could not say what it waits for.
PICK_MIN_CONF = 55
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
    # How many 5m bars this sector's own index is behind the Nifty decision bar.
    # The index feed gaps far more often than the stock feed, and a sector scored
    # on a 30-minute-old bar describes a tape that no longer exists.
    lag_bars: int = 0
    stale: bool = False


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
    # Sector follow (Nifty NEUTRAL): more leading sectors scanned with the pick,
    # and which of the scanned sectors have live flow agreeing with persistence.
    extra_sectors: list = field(default_factory=list)
    flow_aligned: list = field(default_factory=list)
    # Sectors left out of the pick because their index stopped updating:
    # [(name, lag_bars)]. Shown on the market block, never silently dropped.
    stale_sectors: list = field(default_factory=list)


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
    prev_pick: dict | None = None,
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

    for ts, row in today.iterrows():
        bars_today += 1
        mom = market_open_minutes(to_ist(ts))
        c, h, l = float(row["Close"]), float(row["High"]), float(row["Low"])
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
    dir_tier = ("TREND-LOCK" if dir_conf >= 85 else "STRONG" if dir_conf >= 70
                else "BUILDING" if dir_conf >= PICK_MIN_CONF else "WEAK")

    # Prefer concrete if committed
    commit_side = concrete if concrete != 0 else dir_state

    # The label must describe the side the picker and the order action actually
    # use (commit_side), not the raw dir_state. A concrete lock can survive a
    # dir_state reset, and labelling that "NEUTRAL" produced alerts that read
    # "Market NEUTRAL (88%)" on a BUY — a market the locked rule says to skip.
    strong_bull = commit_side == 1 and mtf_bull == 3 and above_vwap >= 4 and adx >= STRONG_ADX_MIN and npos >= 60
    strong_bear = commit_side == -1 and mtf_bear == 3 and below_vwap >= 4 and adx >= STRONG_ADX_MIN and npos <= 40
    dir_label = _direction_label(commit_side, strong_bull, strong_bear)

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

    # A sector whose index has stopped printing is scored on its last bar but
    # cannot be picked, and it cannot sit in PERSIST + either: the list is read
    # as "what is moving now".
    stale_sectors = [(s.name, s.lag_bars)
                     for s in sorted((s for s in snaps.values() if s.stale),
                                     key=lambda s: -s.lag_bars)]
    fresh = [s for s in snaps.values() if not s.stale] or list(snaps.values())
    # The lists mean what they say: PERSIST + holds sectors whose live flow is
    # bullish, PERSIST − the bearish ones. They used to be sorted by persist
    # alone, so a sector that was up on the day but had rolled over (flow
    # bearish, persist_bull still high) could sit in "PERSIST +" — and the picker
    # had to skip it after the fact. Both pools are what the two pickers scan.
    persist_plus = sorted((s for s in fresh if s.side == 1),
                          key=lambda s: s.persist_bull, reverse=True)
    persist_minus = sorted((s for s in fresh if s.side == -1),
                           key=lambda s: s.persist_bear, reverse=True)
    live_ranked = sorted(fresh, key=lambda s: s.score, reverse=True)
    live_qual = next((s for s in live_ranked if s.qualified), live_ranked[0])

    pick, pick_side, reason = _complementary_pick(
        commit_side, dir_conf, regime, persist_plus, persist_minus, vix_chg, n_day_ret
    )
    # Persistence is a smoothed score: without this, two sectors a couple of
    # points apart trade the pick back and forth on consecutive candles.
    pick, pick_side, reason = _sticky_pick(
        pick, pick_side, reason, persist_plus, persist_minus, prev_pick, snaps
    )

    action = "WAIT"
    if pick and pick_side == 1 and commit_side == 1:
        action = "SCAN LONG"
    elif pick and pick_side == -1 and commit_side == -1:
        action = "SCAN SHORT"
    extra_sectors: list[str] = []
    flow_aligned: list[str] = []
    follow = _follow_fallback(pick, commit_side, dir_conf, persist_plus)
    if follow:
        pick, pick_side, action = follow["pick"], follow["side"], follow["action"]
        extra_sectors, flow_aligned = follow["extra"], follow["aligned"]
        reason = follow["reason"]

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
        dir_label=dir_label,
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
        extra_sectors=extra_sectors,
        flow_aligned=flow_aligned,
        stale_sectors=stale_sectors,
    )


def _direction_label(side: int, strong_bull: bool = False, strong_bear: bool = False) -> str:
    """Label for the side the engine actually trades.

    ``side`` is the commit side, so it is never NEUTRAL while a sector is being
    picked: NEUTRAL must mean "no pick, WAIT" everywhere in the alert.
    """
    if side > 0:
        return "STRONG BULL" if strong_bull else "BULL"
    if side < 0:
        return "STRONG BEAR" if strong_bear else "BEAR"
    return "NEUTRAL"


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


def sector_max_lag_bars() -> int:
    """Bars a sector index may trail the Nifty decision bar and still be picked (2).

    Yahoo's cash indices gap far more often than stocks do. Before this check the
    picker scored whatever the last available bar was: on a synthetic replay a
    sector whose index stopped printing 30 minutes earlier still won the pick
    (persist 92, day +1.37%) on a tape that had already moved on. ``0`` requires
    the sector to print the decision bar itself, ``-1`` disables the check.
    """
    try:
        return int(os.environ.get("SECTOR_MAX_LAG_BARS", "2"))
    except ValueError:
        return 2


def _lag_bars(sector_bar, decision_bar) -> int:
    """Whole 5m bars between a sector's last bar and the decision bar (0 if none)."""
    if decision_bar is None or sector_bar is None:
        return 0
    try:
        a = to_ist(decision_bar) if not isinstance(decision_bar, pd.Timestamp) else decision_bar
        b = to_ist(sector_bar) if not isinstance(sector_bar, pd.Timestamp) else sector_bar
        if a.tzinfo is None:
            a = a.tz_localize(IST)
        if b.tzinfo is None:
            b = b.tz_localize(IST)
        return max(0, int(round((a - b).total_seconds() / 300.0)))
    except Exception:
        return 0


def _sector_snap(name, df5, d1, sid, nifty_df, vol_mult, last_ts) -> SectorSnap:
    """Pine `sectorMetric` + v3.9 `pBull`/`pBear` on the last *fresh* bar.

    ``last_ts`` is the decision bar. A sector whose index stops short of it is
    scored on its own last bar and flagged ``stale`` once it trails by more than
    ``SECTOR_MAX_LAG_BARS``; the pickers skip stale sectors and the market block
    names them.
    """
    x = df5.copy().sort_index()
    lag = _lag_bars(x.index[-1], last_ts)
    max_lag = sector_max_lag_bars()
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
        lag_bars=lag,
        stale=bool(max_lag >= 0 and lag > max_lag),
    )


SECTOR_FOLLOW_PREFIX = "SECTOR FOLLOW"


def _is_leading(s, need: float) -> bool:
    return bool(
        s.side == 1 and s.persist_bull >= need
        and not getattr(s, "stale", False)
        and s.day_ret >= BULL_MIN_DAY and s.pos >= BULL_MIN_POS and s.rs > 0
        and s.bull >= FLOW_MIN and s.recent > -RECENT_MOVE_MIN
    )


def _follow_leaders(plus) -> list:
    """Sectors to scan LONG while Nifty itself is NEUTRAL (no committed side).

    The complementary pick needs a committed Nifty direction, so on a flat index
    it waits all day even when a sector is plainly leading. This is the fallback
    for that case. A sector counts as a leader when it is in the PERSIST + top 3
    and is leading right now: persist score high enough, up on the day, in the
    upper part of its day range, positive RS vs Nifty, bullish live flow, and
    not currently rolling over. (The stricter live-flow "qualified" flag also
    demands a fresh push in the last few bars, so it flickers inside a steady
    trend; it is reported as "flow aligned" but is not required.)

    Order = scan priority: sectors named in ``SF_ALIGNED_SECTORS`` (your chart's
    SMART$/MARKET/SECTOR/FLOW agreement, which this scanner cannot see) first,
    then by persist score. The first is the pick; the rest are scanned with it,
    so a quiet leader does not hide an entry in the next one.

    Longs only — a flat index gives no reason to short a weak sector.
    ``SECTOR_FOLLOW=0`` switches it off, ``SECTOR_FOLLOW_MIN_PERSIST`` (55) is
    the score needed, ``SECTOR_FOLLOW_MAX`` (2) the number of sectors scanned.
    """
    if os.environ.get("SECTOR_FOLLOW", "1") == "0" or not plus:
        return []
    try:
        need = float(os.environ.get("SECTOR_FOLLOW_MIN_PERSIST") or 55)
    except ValueError:
        need = 55.0
    try:
        most = max(1, int(os.environ.get("SECTOR_FOLLOW_MAX") or 2))
    except ValueError:
        most = 2
    manual = _manual_aligned()
    leaders = [s for s in plus[:3] if _is_leading(s, need)]
    leaders.sort(key=lambda s: (s.name not in manual, -s.persist_bull))
    return leaders[:most]


def _manual_aligned() -> set:
    raw = os.environ.get("SF_ALIGNED_SECTORS", "")
    return {x.strip().upper() for x in raw.split(",") if x.strip()}


def _follow_reason(leaders, note: str = "Nifty NEUTRAL") -> str:
    s = leaders[0]
    manual = _manual_aligned()
    notes = [f"persist {s.persist_bull:.0f}", f"day {s.day_ret:+.2f}%", f"range pos {s.pos:.0f}"]
    if s.qualified:
        notes.append("flow aligned")
    if s.name in manual:
        notes.append("chart aligned")
    text = f"{SECTOR_FOLLOW_PREFIX} — {note}, {s.name} leads ({', '.join(notes)})"
    if len(leaders) > 1:
        text += "; also scanning " + ", ".join(f"{x.name} {x.persist_bull:.0f}" for x in leaders[1:])
    return text


def _follow_fallback(pick, commit_side, dir_conf, plus):
    """Fallback pick when the complementary path stood aside, or ``None``.

    ``commit_side == 0`` is the original case (Nifty has no side at all).
    ``SECTOR_FOLLOW_ON_WEAK=1`` adds "has a side, but too weak to pick with it".
    """
    if pick:
        return None
    if commit_side == 0:
        note = "Nifty NEUTRAL"
    elif follow_on_weak() and commit_side == 1 and dir_conf < PICK_MIN_CONF:
        note = f"Nifty BULL weak ({dir_conf}%)"
    else:
        return None
    leaders = _follow_leaders(plus)
    if not leaders:
        return None
    return {
        "pick": leaders[0].name,
        "side": 1,
        "action": "SCAN LONG",
        "reason": _follow_reason(leaders, note),
        "extra": [s.name for s in leaders[1:]],
        "aligned": [s.name for s in leaders if s.qualified],
    }


def _follow_pick(plus):
    """(sector, reason) of the first follow leader, or (None, "")."""
    leaders = _follow_leaders(plus)
    return (leaders[0].name, _follow_reason(leaders)) if leaders else (None, "")


def _complementary_pick(commit_side, dir_conf, regime, plus, minus, vix_chg, n_day_ret):
    """Strongest persistent sector on the SAME side as committed Nifty direction."""
    if commit_side == 0 or regime == "CHOP" or dir_conf < PICK_MIN_CONF:
        return None, 0, f"WAIT — market {regime} conf {dir_conf} < {PICK_MIN_CONF}"
    # VIX spike against the day → stand aside
    if vix_chg >= VIX_SPIKE and ((commit_side == 1 and n_day_ret < 0) or (commit_side == -1 and n_day_ret > 0)):
        return None, 0, f"WAIT — VIX spike {vix_chg:.1f}% fights direction"

    pool = plus if commit_side == 1 else minus
    want_side = commit_side
    margin = max(0.0, pick_rs_margin())
    floor = max(0.0, pick_min_persist())
    passed: list[str] = []
    for s in pool[:3]:
        ok, why = _complements(s, want_side, margin)
        if not ok:
            passed.append(f"{s.name} ({why})")
            continue
        score = _persist_of(s, want_side)
        if floor and score < floor:
            passed.append(f"{s.name} (persist {score:.0f} < {floor:.0f})")
            continue
        tag = "PERSIST +" if want_side == 1 else "PERSIST −"
        q = "qualified" if s.qualified and s.side == want_side else "persistent"
        text = f"{tag} {s.name} {q} (score {score:.0f})"
        return s.name, want_side, _passed_over(text, passed)
    top = pool[0].name if pool else "?"
    text = f"WAIT — persist #{top} does not complement {('BULL' if commit_side==1 else 'BEAR')} market"
    return None, 0, _passed_over(text, passed)


def previous_pick(scan: dict | None, bar) -> dict | None:
    """The pick in force on the candle *before* ``bar`` (``{"name", "side"}``).

    The incumbent for ``PICK_STICKY_MARGIN``. It must be the immediately
    preceding candle: an entry carried over from an hour ago (a restarted loop,
    a stale ledger) is not "the current pick", and holding it would be a way to
    keep trading a sector nobody is watching any more.
    """
    scan = scan or {}
    if scan.get("sector") in (None, "", "WAIT") or scan.get("side") not in (1, -1):
        return None
    try:
        was, now = pd.Timestamp(scan.get("bar")), pd.Timestamp(bar)
    except (TypeError, ValueError):
        return None
    if (was.tzinfo is None) != (now.tzinfo is None):
        was = was.tz_localize(now.tzinfo) if was.tzinfo is None else was.tz_convert(now.tzinfo)
        now = now.tz_localize(was.tzinfo) if now.tzinfo is None else now
    if was != now - pd.Timedelta(minutes=5):
        return None
    return {"name": scan["sector"], "side": int(scan["side"])}


def _persist_of(s, want_side: int) -> float:
    """The persistence score that matters on this side."""
    return float(s.persist_bull if want_side == 1 else s.persist_bear)


def _complements(s, want_side: int, margin: float) -> tuple[bool, str]:
    """Is this sector a usable long (``want_side == 1``) / short complement?

    Returns ``(ok, why-not)`` so every caller — the picker, the sticky rule — can
    say what it refused and why. The gate is deliberately thin: the pools are
    already filtered to the sector's own side, the persist floor is applied by
    the caller, and what is left is "this sector is not fighting the trade".
    """
    if getattr(s, "stale", False):
        return False, f"index {getattr(s, 'lag_bars', 0)} bars behind"
    if int(getattr(s, "side", 0)) != want_side:
        return False, f"flow {'bearish' if want_side == 1 else 'bullish'}"
    edge = float(s.rs) if want_side == 1 else -float(s.rs)
    if edge < -margin:
        return False, f"rs {s.rs:+.2f}"
    if (want_side == 1 and s.day_ret < -0.05) or (want_side == -1 and s.day_ret > 0.05):
        return False, f"day {s.day_ret:+.2f}%"
    return True, ""


def pick_min_persist() -> float:
    """Persistence a sector needs before it may be picked at all (``50``).

    The complementary path used to accept any of the top three that was merely
    *not fighting* the market — no floor at all — while the ``SECTOR_FOLLOW``
    fallback (used only when Nifty is NEUTRAL) demands 55. The engine was
    strictest exactly where it was least sure, and a sector at persist 40 could
    trade. ``0`` restores the old behaviour.
    """
    try:
        return float(os.environ.get("PICK_MIN_PERSIST", "50"))
    except ValueError:
        return 50.0


def pick_sticky_margin() -> float:
    """Points a challenger must beat the incumbent sector by to take the pick (5).

    Persistence is a smoothed score, so two sectors a couple of points apart
    swap the pick on consecutive candles (16 Nov: PHA 56 at 09:35, FMCG 59 at
    09:50). Since the per-pick cap and the pending early alert are keyed on the
    pick, every such swap costs a slot and a cancelled heads-up. ``0`` restores
    the switch-on-any-margin behaviour.
    """
    try:
        return max(0.0, float(os.environ.get("PICK_STICKY_MARGIN", "5")))
    except ValueError:
        return 5.0


def follow_on_weak() -> bool:
    """May the sector-follow path run when Nifty *has* a side but is weak (``0``)?

    Two gates sat next to each other and neither opened on 16 Nov 11:55:
    ``_complementary_pick`` stands aside below ``PICK_MIN_CONF`` (41 < 55), and
    ``_follow_leaders`` only ran with **no** committed side at all. A tape that
    read ``MARKET BULL conf 41% WEAK``, ``PERSIST + FMCG 93``, ``PERSIST − REALTY
    91`` therefore had no path to a sector: the picker refused the direction's
    confidence and the fallback refused the direction's existence.

    With this on, a *weak* committed BULL falls back to the same strict leader
    gates the NEUTRAL case uses (persist >= 55, up on the day, upper part of the
    range, positive RS, bullish live flow, not rolling over) — much stricter
    than the complementary path it replaces. Longs only: the bear mirror is not
    written, so a weak BEAR still waits.

    Default ``0``: the locked rule (conf < 55 -> no scan) is unchanged unless you
    ask for it. ``SECTOR_FOLLOW=0`` still turns both paths off.
    """
    return os.environ.get("SECTOR_FOLLOW_ON_WEAK", "0") != "0"


def _find_sector(name: str, want_side: int, plus, minus, snaps=None):
    """The sector snapshot for ``name`` on this side, or None."""
    pool = list(plus if want_side == 1 else minus)
    for s in pool:
        if s.name == name:
            return s
    extra = snaps.values() if isinstance(snaps, dict) else (snaps or ())
    for s in extra:
        if getattr(s, "name", None) == name:
            return s
    return None


def _sticky_pick(pick, pick_side, reason, plus, minus, prev, snaps=None):
    """Hold the incumbent pick unless a challenger clearly beats it.

    ``prev`` is the pick in force on the previous candle (``{"name", "side"}``).
    The incumbent must still pass the same gate it passed when it was chosen —
    a sector that has gone stale, rolled over or fallen below the persistence
    floor is *not* held, so this can only ever delay a switch, never keep a
    broken pick.
    """
    margin = pick_sticky_margin()
    if not pick or margin <= 0 or not prev:
        return pick, pick_side, reason
    name = str(prev.get("name") or "")
    side = int(prev.get("side") or 0)
    if not name or name == "WAIT" or side != pick_side or name == pick:
        return pick, pick_side, reason
    incumbent = _find_sector(name, side, plus, minus, snaps)
    challenger = _find_sector(pick, pick_side, plus, minus, snaps)
    if incumbent is None or challenger is None:
        return pick, pick_side, reason
    ok, _ = _complements(incumbent, side, max(0.0, pick_rs_margin()))
    if not ok:
        return pick, pick_side, reason
    inc_score = _persist_of(incumbent, side)
    if inc_score < max(0.0, pick_min_persist()):
        return pick, pick_side, reason
    new_score = _persist_of(challenger, pick_side)
    if new_score - inc_score >= margin:
        return pick, pick_side, reason
    tag = "PERSIST +" if side == 1 else "PERSIST −"
    q = "qualified" if incumbent.qualified and incumbent.side == side else "persistent"
    held = (f"{tag} {name} {q} (score {inc_score:.0f}) · held — "
            f"{pick} {new_score:.0f} is {new_score - inc_score:+.1f}, "
            f"needs {margin:.0f} to take the pick")
    return name, side, held


def pick_rs_margin() -> float:
    """How far a sector may lag Nifty and still complement a BULL pick (0.05pp).

    ``rs`` is in percentage points of index move, so ``0.05`` is five basis
    points — noise. Without it, a sector that leads PERSIST + by 30 points was
    passed over for reading ``rs -0.01`` and the block said nothing about it;
    ``0`` restores the bare ``rs >= 0`` test.
    """
    try:
        return float(os.environ.get("PICK_RS_MARGIN", "0.05"))
    except ValueError:
        return 0.05


def _passed_over(text: str, passed: list[str], most: int = 2) -> str:
    """Name the sectors the pick walked past, and why — never skip one silently."""
    if not passed:
        return text
    return text + " · passed over " + ", ".join(passed[:most])
