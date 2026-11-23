"""Bot 4 — the end-of-day audit PDF: every number the screen acted on, on paper.

The screen's whole value is that it can be **tuned**. A short list whose reasons
live only in a Telegram scroll cannot be tuned: the day after, nobody can say
whether the 11:05 refusal was structure at 58 or a sector persistence that had
fallen three points. So the report carries, in this order:

1. the verdict — what the short list came to, and the knobs it ran under;
2. the short list itself, one row per setup, with what each one did;
3. one card per setup: the levels, the legs, the lead, the run, the outcome;
4. the candle-by-candle scan: how many names qualified and what was refused;
5. the sector tape: persistence and slope through the session, per sector;
6. the market tape: what the index was doing so a counter-trend setup is visible;
7. the appendix: every gate, its knob, and **the number that refused**.

Nothing here is invented: every figure is read from ``state/screen_state.json``,
which the live loop writes on the candle it judges. A missing section prints the
reason it is missing rather than an empty table (a blank page is not evidence).

``build`` never raises: a report that can take the session down is worse than no
report. If ``reportlab`` is not installed it writes the same audit as ``.txt``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from . import screen
from .session import now_ist

log = logging.getLogger("nse_scanner")

REPORT_DIR = Path("reports")


# ── small helpers ────────────────────────────────────────────────────────────
def _fmt(value, digits: int = 2, dash: str = "—") -> str:
    if value is None:
        return dash
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _median(values) -> float | None:
    """The middle value — the honest one for a latency table with outliers."""
    vals = sorted(float(v) for v in (values or []) if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _int(value, dash: str = "—") -> str:
    if value is None:
        return dash
    try:
        return str(int(round(float(value))))
    except (TypeError, ValueError):
        return str(value)


def config_hash(st: dict | None = None) -> str:
    """A short, stable fingerprint of every knob — so two PDFs are comparable.

    Two sessions with the same hash ran the same rules; if a PDF's numbers moved
    and its hash did too, the knob is the first suspect, not the market.
    """
    knobs = dict((st or {}).get("knobs") or screen.knobs())
    blob = json.dumps(knobs, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha1(blob).hexdigest()[:10]


def _session_bars() -> list[str]:
    """Every 5m candle label of an NSE session, 09:15 through 15:25."""
    out, minute = [], 9 * 60 + 15
    while minute <= 15 * 60 + 25:
        out.append(f"{minute // 60:02d}:{minute % 60:02d}")
        minute += 5
    return out


def _timing_short(x: dict) -> str:
    """The table cell: ``P60 +9s`` = provisional 60s before the close, sent 9s after it."""
    lead = x.get("seconds_left") if x.get("provisional") else None
    age = x.get("age_s")
    cell = (f"P{_int(lead)} " if lead is not None else "") + \
           (f"+{_int(age)}s" if age is not None else "—")
    return cell


def _timing(x: dict, long: bool = False) -> str:
    """How a card got to the user: provisional lead, then the age after the close."""
    if x.get("provisional"):
        lead = x.get("seconds_left")
        base = f"PROVISIONAL {_int(lead)}s early" if lead is not None else "PROVISIONAL"
    elif x.get("confirmed"):
        base = "confirmed at the close"
    else:
        base = "close card (no provisional)"
    age = x.get("age_s")
    if age is not None:
        base += f" · {_int(age)}s after the close"
    if long and x.get("scanned_at"):
        base += f" · sent {x.get('scanned_at')}"
    return base


def _knobs_locked(st: dict) -> dict:
    """The knobs **as they were when the day started**, not as this process reads them.

    A knob changed mid-session would otherwise re-label a day it did not run.
    """
    return dict(st.get("knobs") or screen.knobs())


def _verdict(st: dict) -> str:
    """One paragraph, computed from the numbers — no adjectives that could be wrong."""
    s = screen.summary(st)
    setups = s.get("setups") or 0
    done = s.get("tracked") or 0
    if not setups:
        return ("No setup was surfaced in this ledger. Qualified candidates may have been "
                "ineligible for admission, including after the entry cutoff. Missing "
                "candle rows do not establish whether another runner screened them; "
                "no trading performance can be inferred without outcomes.")
    if not done:
        return (f"{setups} setup(s) surfaced and none has a candle after it yet: the "
                "session ended at the bar that surfaced them. Nothing can be concluded "
                "about them today; they are listed so tomorrow's tape can be compared.")
    prec = s.get("precision")
    worked, stopped = s.get("worked") or 0, s.get("stopped") or 0
    text = (f"{setups} setup(s) surfaced, {done} tracked: {worked} reached +1R before the "
            f"stop, {stopped} traded their stop, {done - worked - stopped} are still open. "
            f"Precision {_fmt(prec, 1)}% (the gate is 45%), average MFE "
            f"{_fmt(s.get('avg_mfe_r'))}R, average mark {_fmt(s.get('avg_eod_r'))}R.")
    stats = st.get("stats") or {}
    if stats.get("age_max") is not None:
        text += (f" Cards went out a median of {_fmt(_median(stats.get('ages')), 0)}s and at "
                 f"worst {_int(stats.get('age_max'))}s after their candle closed"
                 + (f"; {_int(stats.get('late'))} were past the 90s line, which is the "
                    "number SCREEN_EARLY exists to keep at zero."
                    if stats.get("late") else " — none past the 90s line.")
                 + (f" {_int(stats.get('provisional'))} provisional card(s) went out before "
                    f"the close, {_int(stats.get('confirmed'))} survived it and "
                    f"{_int(stats.get('cancelled'))} were cancelled."
                    if stats.get("provisional") else ""))
    if prec is not None and prec < 45 and worked < 3:
        text += (" Below the 45% bar on a sample this small the honest reading is "
                 "'undecided', not 'wrong': one setup moving the counter changes the "
                 "number by more than any knob.")
    return text


def _holdout(st: dict) -> list[tuple[str, str]]:
    """The frozen 20 Nov holdout, answerable from the state alone.

    The gate set on 20 Nov: the screen must take at least two of AXISBANK,
    INDUSINDBK, PERSISTENT, COFORGE, GODREJPROP and none of WIPRO, HCLTECH, TCS.
    """
    want = {"AXISBANK", "INDUSINDBK", "PERSISTENT", "COFORGE", "GODREJPROP"}
    avoid = {"WIPRO", "HCLTECH", "TCS"}
    took = {str(s.get("raw") or "").upper() for s in (st.get("setups") or [])}
    return [("must take (>=2 of 5)", ", ".join(sorted(took & want)) or "none"),
            ("must avoid (0)", ", ".join(sorted(took & avoid)) or "none")]


# ── the document ─────────────────────────────────────────────────────────────
def latency_split(st: dict) -> str:
    """Close-to-data-ready and processing intervals, not delivery latency.

    The first interval includes fetching and processing through data readiness; it
    is not a direct measure of feed publication or recipient delivery.
    """
    stats = st.get("stats") or {}
    feed = _median(stats.get("feed_s") or [])
    work = _median(stats.get("work_s") or [])
    if feed is None and work is None:
        return ""
    parts = []
    if feed is not None:
        parts.append(f"close-to-data-ready +{feed:.1f}s")
    if work is not None:
        parts.append(f"screen {work * 1000.0:.0f}ms")
    if stats.get("names_stale"):
        parts.append(f"{int(stats['names_stale'])} name(s) still stale")
    return " · ".join(parts)


def sector_rows(st: dict) -> list[dict]:
    """The sector tape as rows of ``{"b": hhmm, "sec": {sector: cell}}``.

    The console rebuilds audits from board artifacts up to thirty days old, and a
    ledger written by an earlier version of the screen may hold the tape as one
    flat dict instead of a list of per-candle rows. Either way the report is worth
    printing: an audit that silently degrades to plain text because of the *shape*
    of a key is worse than an audit with one sampled column.
    """
    secs = st.get("sectors") or []
    if isinstance(secs, dict):
        return [{"b": str(st.get("session") or ""), "sec": secs}]
    return [row for row in secs if isinstance(row, dict)]


def build(st: dict | None = None, path: Path | None = None) -> Path:
    """Write the audit PDF for ``st`` and return where it landed. Never raises."""
    st = st or {}
    session = str(st.get("session") or now_ist().date())
    path = Path(path) if path else (REPORT_DIR / f"screen-{session}.pdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_pdf(st, path)
    except ImportError:
        log.error("reportlab is not installed — writing the audit as text instead")
        path = path.with_suffix(".txt")
        path.write_text(_plain_text(st), encoding="utf-8")
    except Exception:                       # noqa: BLE001 — the day must survive its report
        log.exception("screen PDF failed; writing the audit as text instead")
        try:
            path = path.with_suffix(".txt")
            path.write_text(_plain_text(st), encoding="utf-8")
        except Exception:                   # noqa: BLE001
            log.exception("screen audit could not be written at all")
    return path


def _styles():
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm

    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontSize=17, spaceAfter=4),
        "sub": ParagraphStyle("s", parent=base["Normal"], fontSize=8.5,
                              textColor=colors.HexColor("#555555"), spaceAfter=10),
        "h": ParagraphStyle("h", parent=base["Heading2"], fontSize=11.5, spaceBefore=10,
                            spaceAfter=4),
        "body": ParagraphStyle("b", parent=base["Normal"], fontSize=8.6, leading=11.4),
        "small": ParagraphStyle("sm", parent=base["Normal"], fontSize=7.2, leading=9),
        "mono": ParagraphStyle("m", parent=base["Code"], fontSize=7.0, leading=8.6),
        "mm": mm,
    }


def _write_pdf(st: dict, path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import (CondPageBreak, KeepTogether, PageBreak, Paragraph,
                                    SimpleDocTemplate, Spacer, Table, TableStyle)

    sty = _styles()
    session = str(st.get("session") or now_ist().date())
    knobs = _knobs_locked(st)
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=13 * mm, rightMargin=13 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm,
                            title=f"Screen audit {session}", author="NSE scanner · bot 4")
    flow: list = []
    grey = TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
                       ("FONTSIZE", (0, 0), (-1, -1), 6.8),
                       ("LEADING", (0, 0), (-1, -1), 8.4),
                       ("VALIGN", (0, 0), (-1, -1), "TOP"),
                       ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee"))])

    def table(rows, widths, style=None):
        t = Table(rows, colWidths=widths, repeatRows=1)
        t.setStyle(style or grey)
        return t

    # ── page 1: the verdict ──────────────────────────────────────────────────
    flow.append(Paragraph("Bot 4 · name-first screen — end-of-day audit", sty["title"]))
    flow.append(Paragraph(
        f"Session {session} · generated {now_ist():%Y-%m-%d %H:%M:%S} IST · "
        f"config {config_hash(st)} · "
        f"{_int((st.get('stats') or {}).get('candles'))} candles screened · "
        "surfaces setups, never enters", sty["sub"]))
    flow.append(Paragraph("1 · Verdict", sty["h"]))
    flow.append(Paragraph(_verdict(st), sty["body"]))
    s = screen.summary(st)
    stats = st.get("stats") or {}
    flow.append(Spacer(1, 5))
    flow.append(table([
        ["setups", "tracked", "+1R first", "stopped", "precision", "avg MFE", "avg mark",
         "candidates", "candles"],
        [_int(s.get("setups")), _int(s.get("tracked")), _int(s.get("worked")),
         _int(s.get("stopped")),
         _fmt(s.get("precision"), 1) + ("%" if s.get("precision") is not None else ""),
         _fmt(s.get("avg_mfe_r")) + "R", _fmt(s.get("avg_eod_r")) + "R",
         _int(stats.get("candidates")), _int(stats.get("candles"))],
    ], [17 * mm] * 9))
    flow.append(Paragraph(
        "\"Candidates\" is name-candles that passed every gate before the daily cap, the "
        "sector cap and the cooldown; \"candles\" is how many closed 5m candles the screen "
        "recorded in this ledger. Candidates may be excluded by the entry cutoff, score "
        "floor, caps, cooldown or send failure; the counts alone do not identify why.",
        sty["small"]))

    flow.append(Paragraph("2 · Knobs in force (frozen at the open)", sty["h"]))
    flow.append(Paragraph(
        f"Config hash <b>{config_hash(st)}</b>. These are the values the day actually ran "
        "under — read from the session's own record, not from this process's environment.",
        sty["body"]))
    rows = [["knob", "value", "what it decides"]]
    meaning = {
        "SCREEN_MAX_DAY": "setups surfaced per session (your short list)",
        "SCREEN_MIN_SCORE": "floor of the ranking below",
        "SCREEN_PERSIST_MIN": "sector persistence needed to look at its book",
        "SCREEN_SLOPE_MIN": "persistence change over SLOPE_BARS candles (0 = not falling)",
        "SCREEN_SLOPE_BARS": "how far back the slope is measured",
        "SCREEN_STRUCTURE_MIN": "name structure floor (below 60 = drifting, measured -0.04pp)",
        "SCREEN_STRENGTH_MIN": "name strength floor (the tail of a move is 45-64)",
        "SCREEN_LEAD_MAX": "|lead| band in pp (>=3pp measured -0.08pp forward)",
        "SCREEN_RUN_MAX_R": "how far the name may have run, in its own R",
        "SCREEN_NEAR_HIGH_PCT": "how close to its own session extreme counts",
        "SCREEN_COUNTER_PERSIST": "persistence needed to screen against the market side",
        "SCREEN_CAP_PER_SECTOR": "setups per sector (one sector is one risk factor)",
        "SCREEN_COOLDOWN_MIN": "no name re-surfaced inside this",
        "SCREEN_LAST_ENTRY": "no setup after this (no time left to work)",
    }
    for k in sorted(knobs):
        rows.append([k, str(knobs[k]), meaning.get(k, "")])
    flow.append(table(rows, [40 * mm, 16 * mm, 112 * mm]))
    flow.append(Paragraph("3 · Frozen 20 Nov name-list comparison", sty["h"]))
    flow.append(Paragraph("This is a name-list intersection, not an independent holdout test: "
                          "it does not establish opportunity, availability or outcomes.", sty["small"]))
    flow.append(table([["test", "result"]] + [list(r) for r in _holdout(st)], [45 * mm, 123 * mm]))
    flow.append(Paragraph("How to read this", sty["h"]))
    flow.append(Paragraph(
        "<b>4</b> the short list, one row per setup · <b>5</b> one card per setup: levels, legs, "
        "lead, run, outcome · <b>6</b> the candle-by-candle scan, with the refusals counted on "
        "each candle · <b>7</b> the market tape · <b>8</b> the sector tape (persistence, slope, "
        "side) · <b>9</b> every gate with the knob behind it and the number that refused · "
        "<b>10</b> what the day could not see.<br/><br/>"
        "Refusals are first-failure name-candle events, not independent names. Gate order "
        "affects counts. Compare rejected and accepted candidates with subsequent outcomes "
        "across multiple sessions and an untouched holdout before changing thresholds.", sty["body"]))
    flow.append(PageBreak())

    # ── page 2: the short list ───────────────────────────────────────────────
    flow.append(Paragraph("4 · The short list", sty["h"]))
    setups = st.get("setups") or []
    if not setups:
        flow.append(Paragraph(
            "Nothing was surfaced today. Section 8 counts, gate by gate, the names that "
            "were refused and at which number.", sty["body"]))
    else:
        head = ["#", "bar", "timing", "side", "name", "sector", "entry", "SL", "risk%",
                "T1/T2/T3", "score", "prs", "lead", "run", "MFE/MAE", "1R", "stop",
                "mark", "R"]
        rows = [head]
        for x in setups:
            targets = " / ".join(_fmt(x.get(k), 1) for k in ("t1", "t2", "t3"))
            sent = _timing_short(x)
            rows.append([_int(x.get("id")), str(x.get("bar")), sent,
                         str(x.get("side")), str(x.get("raw")), str(x.get("sector")),
                         _fmt(x.get("entry")), _fmt(x.get("sl")),
                         _fmt(x.get("risk_pct")), targets,
                         _fmt(x.get("score"), 1), _int(x.get("persist")),
                         _fmt(x.get("lead"), 1), _fmt(x.get("run_r"), 1),
                         f"{_fmt(x.get('mfe_r'))} / {_fmt(x.get('mae_r'))}",
                         str(x.get("hit_1r") or "—"), str(x.get("stopped") or "—"),
                         _fmt(x.get("mark")), _fmt(x.get("eod_r"))])
        widths = [6 * mm, 9 * mm, 12 * mm, 8 * mm, 18 * mm, 11 * mm, 12 * mm, 12 * mm,
                  7 * mm, 19 * mm, 7 * mm, 6 * mm, 7 * mm, 6 * mm, 11 * mm, 7 * mm,
                  7 * mm, 11 * mm, 7 * mm]
        flow.append(table(rows, widths))
        flow.append(Paragraph(
            "1R / stop are the bar the event happened on. Mark is the last traded close the "
            "screen saw; R is measured from the setup's entry to that close, in its own risk. "
            "When one candle touches both +1R and the stop, the stop wins.", sty["small"]))

    flow.append(Paragraph("5 · One card per setup", sty["h"]))
    if not setups:
        flow.append(Paragraph("No cards: no setup was surfaced.", sty["body"]))
    for x in setups:
        legs = x.get("legs") or {}
        block = [
            Paragraph(f"<b>#{_int(x.get('id'))} · {x.get('side')} {x.get('raw')}</b> — "
                      f"{x.get('sector')} · surfaced {x.get('bar')} · score "
                      f"{_fmt(x.get('score'), 1)}", sty["body"]),
            Paragraph(
                f"entry {_fmt(x.get('entry'))} · SL {_fmt(x.get('sl'))} "
                f"({_fmt(x.get('risk_pct'))}% = 1R) · T1 {_fmt(x.get('t1'))} · "
                f"T2 {_fmt(x.get('t2'))} · T3 {_fmt(x.get('t3'))} "
                f"({', '.join(str(n) for n in (x.get('t_names') or []) if n) or 'measured target'})",
                sty["small"]),
            Paragraph(
                f"sector persist {_int(x.get('persist'))} (slope {_fmt(x.get('slope'), 1)}) · "
                f"lead {_fmt(x.get('lead'), 1)}pp · strength "
                f"{_fmt(legs.get('strength'), 0)} / volume {_fmt(legs.get('volume'), 0)} / "
                f"structure {_fmt(legs.get('structure'), 0)} · run {_fmt(x.get('run_r'), 1)}R · "
                f"{'with' if x.get('aligned') else 'against'} the market", sty["small"]),
            Paragraph("why: " + "; ".join(str(w) for w in (x.get("why") or [])), sty["small"]),
            Paragraph(
                f"outcome: {x.get('status')} · MFE {_fmt(x.get('mfe_r'))}R · "
                f"MAE {_fmt(x.get('mae_r'))}R · +1R at {x.get('hit_1r') or '—'} · "
                f"stop at {x.get('stopped') or '—'} · mark {_fmt(x.get('mark'))} "
                f"({_fmt(x.get('eod_r'))}R)", sty["small"]),
            Paragraph(f"timing: {_timing(x, long=True)}", sty["small"]),
            Spacer(1, 6),
        ]
        flow.append(KeepTogether(block))

    cancelled = st.get("cancelled") or []
    flow.append(Paragraph("5b · Provisional cards the close did not confirm", sty["h"]))
    if not cancelled:
        flow.append(Paragraph(
            "None. Every provisional card either survived its close or none was sent"
            + ("" if (st.get("stats") or {}).get("provisional") else
               " (SCREEN_EARLY may be off, or no name qualified before a close)")
            + ".", sty["body"]))
    else:
        rows = [["#", "bar", "side", "name", "sector", "score", "why the close dropped it"]]
        for x in cancelled:
            rows.append([_int(x.get("id")), str(x.get("bar")), str(x.get("side")),
                         str(x.get("raw")), str(x.get("sector")),
                         _fmt(x.get("score"), 1),
                         str(x.get("why_close") or "did not qualify on the closed candle")])
        flow.append(table(rows, [7 * mm, 11 * mm, 10 * mm, 26 * mm, 15 * mm, 11 * mm,
                                 84 * mm]))
        flow.append(Paragraph(
            "A cancelled card is not a loss: its slot was freed, so the day could still "
            "surface SCREEN_MAX_DAY real setups. A card canned because <b>no close pass "
            "ran for its candle</b> requires investigation of the runner and its ledger; "
            "the absence of a close pass here does not establish what another runner did.", sty["small"]))
    flow.append(PageBreak())

    # ── the candle-by-candle scan ────────────────────────────────────────────
    flow.append(Paragraph("6 · Candle-by-candle scan", sty["h"]))
    scan = st.get("scan") or []
    if not scan:
        flow.append(Paragraph(
            "No scan rows in this ledger. This does not establish whether another runner "
            "screened candles; inspect run logs and artifacts.", sty["body"]))
    else:
        # A missed candle is invisible in a table of the candles that were judged, so
        # the report states the count against the session's own grid (09:15-15:25).
        expected = _session_bars()
        judged = {str(r.get("b")) for r in scan}
        missing = [b for b in expected if b not in judged]
        flow.append(Paragraph(
            f"{len(judged & set(expected))} of the session's {len(expected)} five-minute "
            "candle labels are recorded in this ledger"
            + (f"; <b>{len(missing)} absent from this ledger</b> ({', '.join(missing[:12])}"
               + (" …" if len(missing) > 12 else "") + ") — check run logs and artifacts; absence here does not prove a candle was "
               "not screened and is not a refusal." if missing else " — none absent."), sty["body"]))
        rows = [["bar", "qualified", "best", "top names (score)", "refusals on this candle"]]
        for r in scan:
            top = ", ".join(f"{c.get('raw')} {_fmt(c.get('score'), 0)}"
                            for c in (r.get("top") or []))
            ref = ", ".join(f"{k} {v}" for k, v in sorted((r.get("counts") or {}).items()))
            rows.append([str(r.get("b")), _int(r.get("cands")), _fmt(r.get("best"), 1),
                         top or "—", ref or "—"])
        flow.append(table(rows, [14 * mm, 15 * mm, 12 * mm, 55 * mm, 72 * mm]))
        flow.append(Paragraph(
            "\"Qualified\" is how many names passed every gate on that candle, before the "
            "daily cap, the sector cap and the cooldown. A candle with 0 qualified and a "
            "long refusal column is the tuning material: the gate named there is the one "
            "that was binding.", sty["small"]))

    # A hard break here leaves a blank page whenever the scan table ends on a page
    # boundary; 60mm is "start a new page only if this section cannot fit its head".
    flow.append(CondPageBreak(60 * mm))
    flow.append(Paragraph("7 · Market tape", sty["h"]))
    market = st.get("market") or []
    if not market:
        flow.append(Paragraph("No market rows recorded.", sty["body"]))
    else:
        cells = [[str(m.get("b")), str(m.get("label") or ""), _fmt(m.get("conf"), 0),
                  _int(m.get("side")), str(m.get("regime") or "")] for m in market]
        # Two blocks side by side: a 72-candle day lands on one page instead of three.
        half = (len(cells) + 1) // 2
        rows = [["bar", "label", "conf", "side", "regime"] * 2]
        for i in range(half):
            left = cells[i]
            right = cells[half + i] if half + i < len(cells) else ["", "", "", "", ""]
            rows.append(left + right)
        flow.append(table(rows, [16 * mm, 21 * mm, 11 * mm, 11 * mm, 26 * mm] * 2))
        flow.append(Paragraph(
            "A setup marked \"against the market\" in its card was taken while this tape was "
            "committed to the other side; SCREEN_COUNTER_PERSIST is the only thing that "
            "allowed it.", sty["small"]))
    flow.append(PageBreak())

    # ── sector tape ──────────────────────────────────────────────────────────
    flow.append(CondPageBreak(60 * mm))
    flow.append(Paragraph("8 · Sector tape (persistence, slope, side)", sty["h"]))
    secs = sector_rows(st)
    if not secs:
        flow.append(Paragraph("No sector rows recorded.", sty["body"]))
    else:
        seen = sorted({k for row in secs for k in (row.get("sec") or {})})
        bars = [str(row.get("b")) for row in secs]
        # Sample five points of the day: enough to see a fade, short enough to print.
        idx = sorted({0, len(secs) // 4, len(secs) // 2, (3 * len(secs)) // 4, len(secs) - 1})
        rows = [["sector", "side", "peak", "min"] + [bars[i] for i in idx] + ["slope now"]]
        for name in seen:
            cells = [(row.get("sec") or {}).get(name) or {} for row in secs]
            persist = [c.get("persist") for c in cells if c.get("persist") is not None]
            sides = {c.get("side") for c in cells if c.get("side") is not None}
            last = cells[idx[-1]]
            rows.append([name, ("+" if sides == {1} else "-" if sides == {-1} else "+/-"),
                         _int(max(persist) if persist else None),
                         _int(min(persist) if persist else None)]
                        + [_int((cells[i].get("persist"))) for i in idx]
                        + [_fmt(last.get("slope"), 1)])
        flow.append(table(rows, [26 * mm, 10 * mm, 12 * mm, 12 * mm] + [14 * mm] * len(idx)
                          + [16 * mm]))
        flow.append(Paragraph(
            f"Peak and minimum are the sector's own persistence across the {len(secs)} "
            f"screened candles; the sampled bars are {', '.join(bars[i] for i in idx)}. "
            f"Slope now is the change over the last {knobs.get('SCREEN_SLOPE_BARS')} candle(s).",
            sty["small"]))

    # ── appendix: every gate and its refusal count ───────────────────────────
    flow.append(Paragraph("9 · Appendix — every gate, and the number that refused", sty["h"]))
    refused = stats.get("refused") or {}
    gate_knob = {
        "sector persist below floor": ("SCREEN_PERSIST_MIN", knobs.get("SCREEN_PERSIST_MIN")),
        "sector persistence falling": ("SCREEN_SLOPE_MIN", knobs.get("SCREEN_SLOPE_MIN")),
        "sector moving against its side": ("intra vs side", "the same sign"),
        "sector against the market": ("SCREEN_COUNTER_PERSIST", knobs.get("SCREEN_COUNTER_PERSIST")),
        "no candle for the name": ("freshness", "bar = judged candle"),
        "name not moving its sector's way": ("name tape", "close/EMA9/15m/extreme"),
        "structure below the floor": ("SCREEN_STRUCTURE_MIN", knobs.get("SCREEN_STRUCTURE_MIN")),
        "strength below the floor": ("SCREEN_STRENGTH_MIN", knobs.get("SCREEN_STRENGTH_MIN")),
        "lead outside the band": ("SCREEN_LEAD_MAX", knobs.get("SCREEN_LEAD_MAX")),
        "no levels": ("trade.plan_at", "stop/targets must exist"),
        "already run too far": ("SCREEN_RUN_MAX_R", knobs.get("SCREEN_RUN_MAX_R")),
    }
    rows = [["gate", "knob", "value", "events", "share"]]
    total = sum(int(v or 0) for v in refused.values()) or 0
    for gate in list(gate_knob) + [g for g in refused if g not in gate_knob]:
        knob, value = gate_knob.get(gate, ("—", "—"))
        n = int(refused.get(gate) or 0)
        rows.append([gate, str(knob), str(value), _int(n),
                     f"{100.0 * n / total:.1f}%" if total else "—"])
    rows.append(["TOTAL", "", "", _int(total), "100%" if total else "—"])
    flow.append(table(rows, [50 * mm, 38 * mm, 42 * mm, 20 * mm, 16 * mm]))
    flow.append(Paragraph(
        "These are first-failure events among observations reaching each gate, not "
        "independent names. Earlier gates and sector fetch limits affect later counts; "
        "a zero count does not prove a gate has slack or that all feeds were healthy.",
        sty["small"]))
    flow.append(Paragraph("10 · What the day could not see", sty["h"]))
    flow.append(Paragraph(
        "The screen judges only closed 5m candles of names whose bar published. A stale "
        "feed is counted as \"no candle for the name\" above and nothing else; there is no "
        "back-fill and no late entry. Setups are measured, not traded — no money, no "
        "position, no stop order was ever placed by this module.", sty["body"]))
    flow.append(Paragraph(
        f"<b>Reproduce:</b> python -m nse_scanner screen-pdf --session {session}  ·  "
        f"state: state/screen_state.json  ·  knobs hash {config_hash(st)}", sty["small"]))
    doc.build(flow)


# ── the fallback, and the caption ────────────────────────────────────────────
def _plain_text(st: dict) -> str:
    """The same audit as text, for a host without reportlab. Deliberately complete."""
    session = str(st.get("session") or now_ist().date())
    knobs = _knobs_locked(st)
    s = screen.summary(st)
    stats = st.get("stats") or {}
    lines = [f"BOT 4 — NAME-FIRST SCREEN AUDIT · {session} · config {config_hash(st)}",
             "", "VERDICT", _verdict(st), "", "SCORECARD",
             f"  setups {s.get('setups')} · tracked {s.get('tracked')} · +1R first "
             f"{s.get('worked')} · stopped {s.get('stopped')} · precision "
             f"{_fmt(s.get('precision'), 1)}% · avg MFE {_fmt(s.get('avg_mfe_r'))}R · "
             f"avg mark {_fmt(s.get('avg_eod_r'))}R",
             "", "KNOBS IN FORCE"]
    lines += [f"  {k} = {knobs[k]}" for k in sorted(knobs)]
    lines += ["", "FROZEN 20 NOV HOLDOUT (name-list comparison only; not validation)"] + [f"  {a}: {b}" for a, b in _holdout(st)]
    lines += ["", "TIMING",
              f"  median {_fmt(_median(stats.get('ages')), 0)}s · worst "
              f"{_int(stats.get('age_max'))}s after the close · {_int(stats.get('late'))} "
              f"past 90s · {_int(stats.get('provisional'))} provisional / "
              f"{_int(stats.get('confirmed'))} confirmed / {_int(stats.get('cancelled'))} "
              "cancelled"]
    if latency_split(st):                 # Yahoo's half and ours, never just the sum
        lines += [f"  {latency_split(st)}"]
    lines += ["", "THE SHORT LIST"]
    for x in st.get("setups") or []:
        lines.append(f"  #{x.get('id')} {x.get('bar')} {x.get('side')} {x.get('raw')} "
                     f"{x.get('sector')} entry {_fmt(x.get('entry'))} sl {_fmt(x.get('sl'))} "
                     f"t1 {_fmt(x.get('t1'))} score {_fmt(x.get('score'), 1)} "
                     f"mfe {_fmt(x.get('mfe_r'))} mae {_fmt(x.get('mae_r'))} "
                     f"hit1r {x.get('hit_1r')} stop {x.get('stopped')} R {_fmt(x.get('eod_r'))}")
        for w in x.get("why") or []:
            lines.append(f"      why: {w}")
    if not (st.get("setups") or []):
        lines.append("  (none surfaced)")
    lines += ["", "CANDLE-BY-CANDLE"]
    for r in st.get("scan") or []:
        lines.append(f"  {r.get('b')} qualified {r.get('cands')} best {_fmt(r.get('best'), 1)} "
                     f"{r.get('counts') or ''}")
    lines += ["", "MARKET"]
    for m in st.get("market") or []:
        lines.append(f"  {m.get('b')} {m.get('label')} {m.get('conf')}% side {m.get('side')}")
    lines += ["", "GATES (names refused)"]
    for k, v in sorted((stats.get("refused") or {}).items()):
        lines.append(f"  {k}: {v}")
    return "\n".join(lines) + "\n"


def text_summary(st: dict) -> str:
    """The one-screen message that carries the PDF. Telegram HTML, no bare ``<``."""
    s = screen.summary(st)
    session = str(st.get("session") or now_ist().date())
    rows = [f"📄 <b>SCREEN AUDIT {session}</b> — config <code>{config_hash(st)}</code>"]
    rows.append(f"short list: <b>{_int(s.get('setups'))}</b> · tracked "
                f"{_int(s.get('tracked'))} · +1R first {_int(s.get('worked'))} · stopped "
                f"{_int(s.get('stopped'))} · precision "
                f"{_fmt(s.get('precision'), 1)}%")
    stats = st.get("stats") or {}
    rows.append(f"avg MFE {_fmt(s.get('avg_mfe_r'))}R · avg mark {_fmt(s.get('avg_eod_r'))}R "
                f"· {_int(stats.get('candles'))} candles screened")
    if stats.get("age_max") is not None:
        rows.append(f"timing: median {_int(_median(stats.get('ages')))}s · worst "
                    f"{_int(stats.get('age_max'))}s after the close · "
                    f"{_int(stats.get('provisional'))} provisional, "
                    f"{_int(stats.get('confirmed'))} confirmed, "
                    f"{_int(stats.get('cancelled'))} cancelled")
    if latency_split(st):
        rows.append(f"latency: {latency_split(st)}")
    top = sorted((st.get("setups") or []), key=lambda x: -(x.get("mfe_r") or 0))[:3]
    if top:
        rows.append("best: " + " · ".join(
            f"{x.get('raw')} {_fmt(x.get('mfe_r'))}R" for x in top))
    take, avoid = _holdout(st)
    rows.append(f"20 Nov name-list comparison (not validation) — {take}: {avoid[0]}")
    rows.append("Every gate, its knob and the count that refused it are in the PDF.")
    return "\n".join(rows)


def report(st: dict | None = None, *, send=None, send_doc=None, dry_run: bool = False) -> dict:
    """Build the PDF and upload it to bot 4. Returns what happened; never raises."""
    from . import telegram

    st = st or {}
    send_doc = send_doc or telegram.send_document
    path = build(st)
    ok = False
    try:
        ok = bool(send_doc(path, text_summary(st), bot=4, dry_run=dry_run))
    except Exception:                       # noqa: BLE001 — the PDF exists either way
        log.exception("screen PDF upload failed")
    if not ok:
        log.error("screen audit PDF written to %s but NOT delivered (bot 4)", path)
    return {"path": path, "sent": ok}


def _cli(session: str | None = None) -> int:
    """``python -m nse_scanner screen-pdf`` — re-run a missed send, or print the path."""
    st = screen.load()
    stats = st.get("stats") or {}
    if not (st.get("setups") or stats.get("candles")):
        # An audit of nothing is not an audit: on a runner this means the board
        # artifact carried no state/screen_state.json, and sending bot 4 a PDF of
        # empty tables would look like a broken screen rather than a missing file.
        print("No screen ledger with a candle in it — nothing to audit. On a runner "
              "this means the board artifact had no state/screen_state.json.")
        return 1
    if session and str(st.get("session") or "") != session:
        log.warning("state holds session %s, not %s — reporting what is there",
                    st.get("session"), session)
    out = report(st, dry_run=os.environ.get("DRY_RUN", "0") == "1")
    print(f"audit: {out['path']}  sent={out['sent']}")
    return 0 if out["path"].exists() else 1
