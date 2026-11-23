"""The day's audit: one JSON file and one workbook, with everything the report says.

Telegram is a reading surface, not a database. The report gives the day in
numbered messages; this gives the same day as data — every candle's tilt and
every sector's numbers, every blocked signal with its reason, every name a
sector put forward with the three ranking legs, the full trader ledger with
costs, and the diagnostics that explain a candle nobody judged.

Both files are written under ``AUDIT_DIR`` (``reports``) and sent as Telegram
documents next to the messages. The workbook is written by the repo's own
dependency-free writer (``xlsx.py``) so the workflow installs nothing new.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import board as board_book
from . import costs, knobs, ledger, xlsx

THRESH_70 = 70
THRESH_85 = 85


# ── the sector day, read back from the timeline ─────────────────────────────
def _score(row: dict, name: str, side: int) -> float | None:
    cell = (row.get("sec") or {}).get(name)
    if not cell:
        return None
    return float(cell[0] if side == 1 else cell[1])


def _runs(bars: list[tuple[str, bool]]) -> list[dict]:
    """Contiguous stretches where the flag is true: gate-open periods, pick runs."""
    out: list[dict] = []
    start = None
    count = 0
    previous = None
    for bar, on in bars:
        if on:
            if start is None:
                start = bar
            count += 1
            previous = bar
        elif start is not None:
            out.append({"from": start, "to": previous, "candles": count})
            start, count = None, 0
    if start is not None:
        out.append({"from": start, "to": previous, "candles": count})
    return out


def sector_stats(st: dict, threshold: int = THRESH_70) -> dict[str, dict]:
    """Per sector: score range, candles above the gate, when the gate was open.

    Read from ``stats.timeline`` (one row per candle), so it describes the tape
    rather than the report: every number here can be checked against the JSON.
    """
    timeline = (st.get("stats") or {}).get("timeline") or []
    hist = (st.get("stats") or {}).get("sector_hist") or {}
    out: dict[str, dict] = {}
    for name in sorted({n for row in timeline for n in (row.get("sec") or {})}):
        cells = [(row.get("b"), (row.get("sec") or {}).get(name), row) for row in timeline]
        cells = [(b, c, row) for b, c, row in cells if c]
        if not cells:
            continue
        side = int((hist.get(name) or {}).get("side") or 0)
        if side not in (1, -1):
            side = 1 if float(cells[-1][1][0]) >= float(cells[-1][1][1]) else -1
        scores = [(b, float(c[0] if side == 1 else c[1])) for b, c, _ in cells]
        values = [v for _, v in scores]
        picks = [(b, (row.get("pick") or "") == name) for b, _, row in cells]
        gate = [(b, bool(c[4])) for b, c, _ in cells]
        crossings = []
        for (b0, v0), (b1, v1) in zip(scores, scores[1:]):
            for mark in (threshold, THRESH_85):
                if (v0 < mark) != (v1 < mark):          # it crossed the line
                    crossings.append({"bar": b1, "from": round(v0),
                                      "to": round(v1), "level": mark,
                                      "dir": "up" if v1 > v0 else "down"})
        out[name] = {
            "side": side, "first": scores[0][0], "last": scores[-1][0],
            "min": min(values), "max": max(values), "now": values[-1],
            "candles": len(values),
            f"above_{threshold}": sum(1 for v in values if v >= threshold),
            "above_85": sum(1 for v in values if v >= THRESH_85),
            "gate_candles": sum(1 for _, on in gate if on),
            "gate_periods": _runs(gate),
            "pick_candles": sum(1 for _, on in picks if on),
            "pick_periods": _runs(picks),
            "transitions": crossings,
            "entries": 0,
            # Bot 1 keeps a per-sector histogram; bot 3 does not, so its day %
            # used to read 0.00 for every sector — the one number that says which
            # sector actually moved. The tape carries it per candle, so fall back
            # to the last cell.
            "day_ret": round(float((hist.get(name) or {}).get("day_ret")
                                   if (hist.get(name) or {}).get("day_ret") is not None
                                   else cells[-1][1][2] or 0.0), 2),
            "series": [[b, round(v)] for b, v in scores],
        }
    # entries per sector, from the trades themselves
    for rec in (st.get("stats") or {}).get("closed") or []:
        if rec.get("sector") in out:
            out[rec["sector"]]["entries"] = out[rec["sector"]].get("entries", 0) + 1
    for pos in (st.get("positions") or {}).values():
        if pos.get("sector") in out:
            out[pos["sector"]]["entries"] = out[pos["sector"]].get("entries", 0) + 1
    return out


# ── the leadership table, flattened ────────────────────────────────────────
def leadership_rows(st: dict) -> list[dict]:
    """Every top-N row a sector produced, per candle, with its three legs."""
    rows = []
    for entry in (st.get("stats") or {}).get("leadership") or []:
        bar = entry.get("b")
        for sector, names in (entry.get("sec") or {}).items():
            for i, item in enumerate(names or [], start=1):
                rows.append({"bar": bar, "sector": sector, "rank_in_sector": i,
                             "name": item.get("name"), "rank": item.get("rank"),
                             "strength": item.get("strength"),
                             "volume": item.get("volume"),
                             "structure": item.get("structure"),
                             "lead_pp": item.get("lead"),
                             "scanned": bool(item.get("kept"))})
    return rows


def blocked_rows(st: dict) -> list[dict]:
    """Every signal that did not become an entry, with the reason it recorded."""
    rows = []
    for kind, items in ((st.get("stats") or {}).get("rejected") or {}).items():
        for key, detail in (items or {}).items():
            rows.append({"rule": kind, "key": key, "detail": detail})
    return rows


# ── the audit document itself ──────────────────────────────────────────────
def build(st: dict, *, bot: str = "5m") -> dict:
    """The whole session as data — what the JSON file holds and the sheets show."""
    from .pipeline import _REJECT_TEXT

    stats = st.get("stats") or {}
    closed = ledger.closed_rows(st)
    opened = ledger.open_rows(st)
    books = ledger.totals(closed, opened)
    rule_text = {kind: text for kind, text in _REJECT_TEXT}
    blocked = blocked_rows(st)
    for row in blocked:
        row["meaning"] = rule_text.get(row["rule"], "")
    return {
        "session": st.get("session"),
        "bot": bot,
        "generated_at": _now(),
        "market": st.get("market") or {},
        "sector": st.get("sector") or {},
        "scan": {k: v for k, v in (st.get("scan") or {}).items() if k != "log"},
        "counts": {
            "candles": stats.get("candles"), "judged": stats.get("picks"),
            "entries": stats.get("entries"), "early_sent": stats.get("early_sent"),
            "confirmed": stats.get("confirmed"), "cancelled": stats.get("cancelled"),
            "catchup": stats.get("catchup"), "late_entries": stats.get("late_entries"),
            "alerts": len(st.get("sent") or {}),
        },
        "books": books,
        "trades": closed,
        "open_trades": opened,
        "blocked": blocked,
        "weak": stats.get("weak") or {},
        "cancel_why": stats.get("cancel_why") or {},
        "trim": stats.get("trim") or {},
        "alert_capped": stats.get("alert_capped") or {},
        "sectors": sector_stats(st),
        "timeline": [
            {"bar": row.get("b"), "tilt": row.get("tilt"), "label": row.get("label"),
             "conf": row.get("conf"), "regime": row.get("regime"),
             "pick": row.get("pick"), "side": row.get("side"),
             "sectors": {name: {"persist_bull": c[0], "persist_bear": c[1],
                                "day_pct": c[2], "intra_pct": c[3], "qualified": bool(c[4])}
                         for name, c in (row.get("sec") or {}).items()}}
            for row in stats.get("timeline") or []],
        "leadership": leadership_rows(st),
        "board": {
            "meta": dict((st.get("stats") or {}).get("board", {}).get("meta") or {}),
            "summary": board_book.summary(st, closed=closed, opened=opened, blocked=blocked),
            "files": [],
        },
        "diagnostics": {
            "pending": stats.get("pending") or [],
            "checked": stats.get("checked") or [],
            "unjudged": stats.get("unjudged") or [],
            "no_bar": stats.get("no_bar") or [],
            "feed_behind": stats.get("feed_behind"),
            "scan_late": stats.get("scan_late"), "scan_lag_max": stats.get("scan_lag_max"),
            "lag_sum": stats.get("lag_sum"), "lag_max": stats.get("lag_max"),
            "lag_n": stats.get("lag_n"),
            "early_age_sum": stats.get("early_age_sum"),
            "early_age_n": stats.get("early_age_n"),
            "early_skip": stats.get("early_skip") or {},
            "no_index": stats.get("no_index") or {},
            "catchup_skip": stats.get("catchup_skip") or {},
            "fresh_leaders": stats.get("fresh_leaders") or {},
            "undelivered": len(st.get("undelivered") or {}),
            "secondary_failures": _secondary_failures(),
        },
        "rules": {name: {"default": default, "effective": _effective(name, default)}
                  for name, default in knobs.AUDITED},
        "costs": {"bps_per_side": costs.cost_bps(), "min_per_order": costs.cost_min(),
                  "capital_per_trade": costs.trade_capital()},
        "alerts": {raw: rec for raw, rec in (st.get("sent") or {}).items()},
    }


def _now() -> str:
    from .session import now_ist

    return now_ist().strftime("%Y-%m-%d %H:%M:%S")


def _effective(name: str, default: str) -> str:
    try:
        return knobs.effective(name, default)
    except Exception:                      # a knob that cannot be resolved is not fatal
        return default


def _secondary_failures() -> int:
    from . import telegram

    return telegram.secondary_failures()


# ── files ───────────────────────────────────────────────────────────────────
def directory() -> Path:
    return Path(os.environ.get("AUDIT_DIR", "reports"))


def paths(session: str, bot: str = "") -> tuple[Path, Path]:
    """``(json, xlsx)`` for a session — the two attachments.

    The bot is part of the name: the 5m and the 1m scan describe two different
    days and must never overwrite each other's audit on a shared disk.
    """
    stem = f"audit-{session or 'session'}" + (f"-{bot}" if bot else "")
    base = directory() / stem
    return base.with_suffix(".json"), base.with_suffix(".xlsx")


def _summary_sheet(audit: dict) -> list[list]:
    b = audit.get("books") or {}
    c = audit.get("counts") or {}
    rows = [["key", "value"],
            ["session", audit.get("session")], ["bot", audit.get("bot")],
            ["generated_at", audit.get("generated_at")],
            ["market", (audit.get("market") or {}).get("label")],
            ["market confidence", (audit.get("market") or {}).get("conf")],
            ["regime", (audit.get("market") or {}).get("regime")],
            ["last pick", (audit.get("sector") or {}).get("name")],
            ["candles scanned", c.get("candles")], ["candles with a pick", c.get("judged")],
            ["entries", c.get("entries")], ["alerts on record", c.get("alerts")],
            ["early alerts", c.get("early_sent")], ["confirmed", c.get("confirmed")],
            ["cancelled", c.get("cancelled")],
            ["blocked signals", len(audit.get("blocked") or [])],
            ["closed trades", b.get("trades")], ["wins", b.get("wins")],
            ["losses", b.get("losses")], ["win rate %", b.get("win_rate")],
            ["gross ₹", b.get("gross_rs")], ["costs ₹", b.get("costs_rs")],
            ["net ₹", b.get("net_rs")], ["net R", b.get("net_r")],
            ["avg net R", b.get("avg_net_r")], ["profit factor", b.get("profit_factor")],
            ["best ₹", b.get("best_rs")], ["worst ₹", b.get("worst_rs")],
            ["max drawdown ₹", b.get("max_dd_rs")],
            ["risk deployed ₹", b.get("risk_rs")],
            ["return on risk %", b.get("return_on_risk_pct")],
            ["open trades", b.get("open_trades")], ["open net ₹", b.get("open_net_rs")],
            ["avg hold (min)", b.get("avg_hold_min")],
            ["cost model bps/side", (audit.get("costs") or {}).get("bps_per_side")],
            ["cost floor/order ₹", (audit.get("costs") or {}).get("min_per_order")],
            ["capital/trade ₹", (audit.get("costs") or {}).get("capital_per_trade")]]
    return rows


def _trade_sheet(audit: dict) -> list[list]:
    head = ["#", "name", "sector", "side", "qty", "entry_at", "entry", "stop0",
            "risk/share", "risk ₹", "T1", "T2", "T3", "exit_at", "exit", "why",
            "targets hit", "mfe R", "mae R", "hold min", "move %", "gross ₹", "costs ₹",
            "net ₹", "gross R", "net R", "gave back R", "cum ₹"]
    rows = [head]
    for i, t in enumerate(audit.get("trades") or [], start=1):
        rows.append([i, t.get("name"), t.get("sector"), t.get("side"), t.get("qty"),
                     t.get("entry_at"), t.get("entry"), t.get("stop0"),
                     t.get("risk_per_share"), t.get("risk_rs"), t.get("t1"), t.get("t2"),
                     t.get("t3"), t.get("exit_at"), t.get("exit"), t.get("why"),
                     t.get("targets_hit"), t.get("mfe_r"), t.get("mae_r"),
                     t.get("hold_min"), t.get("move_pct"),
                     t.get("gross_rs"), t.get("costs_rs"), t.get("net_rs"),
                     t.get("gross_r"), t.get("net_r"), t.get("give_back_r"),
                     t.get("cum_rs")])
    for t in audit.get("open_trades") or []:
        rows.append(["open", t.get("name"), t.get("sector"), t.get("side"), t.get("qty"),
                     "", t.get("entry"), t.get("stop"), t.get("risk_rs"), "",
                     "", "", "", "", t.get("mark"), "still open", t.get("targets_hit"),
                     t.get("mfe_r"), t.get("mae_r"), "", "", t.get("gross_rs"),
                     t.get("costs_rs"), t.get("net_rs"), t.get("gross_r"), t.get("net_r"),
                     "", ""])
    return rows


def _blocked_sheet(audit: dict) -> list[list]:
    rows = [["rule", "meaning", "key", "detail"]]
    for row in audit.get("blocked") or []:
        rows.append([row.get("rule"), row.get("meaning"), row.get("key"),
                     row.get("detail")])
    return rows


def _timeline_sheet(audit: dict) -> list[list]:
    rows = [["bar", "tilt", "label", "conf", "regime", "pick", "side", "sector",
             "persist bull", "persist bear", "day %", "intra %", "gate open"]]
    for row in audit.get("timeline") or []:
        for name, cell in (row.get("sectors") or {}).items():
            rows.append([row.get("bar"), row.get("tilt"), row.get("label"),
                         row.get("conf"), row.get("regime"), row.get("pick"),
                         row.get("side"), name, cell.get("persist_bull"),
                         cell.get("persist_bear"), cell.get("day_pct"),
                         cell.get("intra_pct"), int(bool(cell.get("qualified")))])
    return rows


def _sector_sheet(audit: dict) -> list[list]:
    rows = [["sector", "side", "first", "last", "min", "max", "now",
             "candles", "candles >=70", "candles >=85", "gate-open candles",
             "gate-open periods", "pick candles", "pick runs", "transitions",
             "entries", "day %"]]
    for name, s in (audit.get("sectors") or {}).items():
        rows.append([name, s.get("side"), s.get("first"), s.get("last"), s.get("min"),
                     s.get("max"), s.get("now"), s.get("candles"), s.get("above_70"),
                     s.get("above_85"), s.get("gate_candles"),
                     len(s.get("gate_periods") or []), s.get("pick_candles"),
                     len(s.get("pick_periods") or []), len(s.get("transitions") or []),
                     s.get("entries"), s.get("day_ret")])
    return rows


def _sector_series_sheet(audit: dict) -> list[list]:
    rows = [["bar", "sector", "score"]]
    for name, s in (audit.get("sectors") or {}).items():
        for bar, value in s.get("series") or []:
            rows.append([bar, name, value])
    return rows


def _leadership_sheet(audit: dict) -> list[list]:
    rows = [["bar", "sector", "rank in sector", "name", "combined rank", "strength",
             "volume", "structure", "lead pp", "scanned"]]
    for row in audit.get("leadership") or []:
        rows.append([row.get("bar"), row.get("sector"), row.get("rank_in_sector"),
                     row.get("name"), row.get("rank"), row.get("strength"),
                     row.get("volume"), row.get("structure"), row.get("lead_pp"),
                     int(bool(row.get("scanned")))])
    return rows


def _diagnostics_sheet(audit: dict) -> list[list]:
    rows = [["key", "value"]]
    for key, value in (audit.get("diagnostics") or {}).items():
        if isinstance(value, (list, dict)):
            value = json.dumps(value, default=str)
        rows.append([key, value])
    rows.append(["", ""])
    rows.append(["knob", "effective"])
    for name, spec in (audit.get("rules") or {}).items():
        rows.append([name, (spec or {}).get("effective")])
    return rows


SHEETS = {
    "Summary": _summary_sheet,
    "Trades": _trade_sheet,
    "Blocked": _blocked_sheet,
    "Timeline": _timeline_sheet,
    "Sectors": _sector_sheet,
    "SectorSeries": _sector_series_sheet,
    "Leadership": _leadership_sheet,
    "Diagnostics": _diagnostics_sheet,
}


def write(st: dict, *, bot: str = "5m") -> tuple[Path, Path, dict]:
    """Write every attachment and return ``(json, xlsx, audit)``.

    The board is written twice: its per-name summary is inside the audit above,
    and its per-candle rows go to a JSONL companion (one line per candle) with a
    printable HTML page beside it — 13,000 rows in the audit's indented JSON
    would have made the file unreadable for the sake of a grid nobody reads
    there. Both paths are listed in ``audit["board"]["files"]`` so the
    end-of-day message attaches them without knowing their names.
    """
    audit = build(st, bot=bot)
    session = str(st.get("session") or "session")
    json_path, xlsx_path = paths(session, bot)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        files = board_book.write_files(st, audit, session=session, bot=bot,
                                       directory=json_path.parent)
    except Exception:                       # a page that fails must not cost the audit
        files = []
    audit["board"]["files"] = [str(p) for p in files]
    json_path.write_text(json.dumps(audit, indent=2, default=str), encoding="utf-8")
    xlsx.write(xlsx_path, {name: builder(audit) for name, builder in SHEETS.items()})
    return json_path, xlsx_path, audit
