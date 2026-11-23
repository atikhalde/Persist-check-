"""Prioritize strengthening sectors and their improving leaders, without lookahead.

This is an experimental ordering signal, not a proven trading edge or a hard gate.
It uses only the last two consecutive *completed* board snapshots already in the
same-session ledger; no extra market requests, and unknown data preserves baseline
ordering. The existing trigger, freshness, leader and risk gates remain in force.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta


def _number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def assessment(st: dict, bar: datetime, sector: str, side: int, name: str) -> dict:
    """Return observable components or ``unknown``; never use the current bar.

    Board bar labels are candle opens, and the board is written after the alerts.
    Reading only records already in the ledger avoids assuming a candle's label
    means that its board was available at the label time.
    """
    unknown = {"status": "unknown"}
    if st.get("session") != str(bar.date()) or side not in (-1, 1):
        return unknown
    rows = ((st.get("stats") or {}).get("board") or {}).get("bars") or []
    latest = (bar - timedelta(minutes=5)).strftime("%H:%M")
    previous = (bar - timedelta(minutes=10)).strftime("%H:%M")
    by_bar = {row.get("b"): row for row in rows if isinstance(row, dict)}
    a = (by_bar.get(previous, {}).get("sectors") or {}).get(sector) or {}
    b = (by_bar.get(latest, {}).get("sectors") or {}).get(sector) or {}
    if not isinstance(a, dict) or not isinstance(b, dict):
        return unknown
    if a.get("side") != side or b.get("side") != side:
        return unknown
    try:
        ia = a["n"].index(name)
        ib = b["n"].index(name)
        before = [_number(a[key][ia]) for key in ("rank", "mv")]
        after = [_number(b[key][ib]) for key in ("rank", "mv")]
        pos = int(b["rk"][ib])
        persistence = [_number(a.get("persist")), _number(b.get("persist"))]
        intra = [_number(a.get("intra")), _number(b.get("intra"))]
    except (KeyError, ValueError, IndexError, TypeError):
        return unknown
    if pos < 1 or any(v is None for v in before + after + persistence + intra):
        return unknown
    dp = persistence[1] - persistence[0]
    ds = after[0] - before[0]
    di = side * (intra[1] - intra[0])
    dm = side * (after[1] - before[1])
    # Thresholds are intentionally simple and exploratory, not optimized on TCS.
    sector_fresh = dp > 0 and di > 0
    name_fresh = pos <= 5 and ds > 0 and dm > 0
    return {"status": "preferred" if sector_fresh and name_fresh else "other",
            "sector_persist_delta": round(dp, 2), "sector_move_delta_pp": round(di, 3),
            "score_delta": round(ds, 2), "name_move_delta_pp": round(dm, 3),
            "rank": pos, "board_bar": latest}


def prioritize(candidates: list[dict], st: dict, bar: datetime,
               sector_of: dict, side_of: dict, *, stage: str) -> None:
    """Stable ordering: prefer confirmed evidence; preserve baseline for all others.

    Keep one diagnostic per candidate/bar/stage, even when a minute is retried.
    Unknown or negative evidence does not silently reject a trade.
    """
    if not candidates:
        return
    decisions = st.setdefault("stats", {}).setdefault("fresh_leaders", {})
    for candidate in candidates:
        name = candidate.get("raw")
        sector = sector_of.get(name)
        result = assessment(st, bar, sector, side_of.get(name), name)
        candidate["fresh_leader"] = result
        decisions[f"{bar:%Y-%m-%d %H:%M}|{stage}|{name}"] = {
            "sector": sector, "side": side_of.get(name), **result}
    candidates.sort(key=lambda c: c["fresh_leader"]["status"] != "preferred")
