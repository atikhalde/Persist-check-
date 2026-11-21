"""The whole board, every candle: every name in every sector, scored.

The audit's ``leadership`` table only holds the sectors a scan happened to look
at — 20 Nov: 1,152 rows over the 8 sectors that were picked or scanned that day,
and nothing at all from the 7 that were not. So "who else was moving, and what
did they score while they did it" could only be answered for the names the bot
had already chosen to look at, which is the wrong way round: the misses are
exactly the sectors it never scanned.

This records the full board instead. Once per five-minute candle, from 09:20 to
15:15, **every name in ``config/universe.yaml``** is scored with the same
``name_rank`` the trim uses (strength · volume · structure against its own
sector's move), and stored next to its sector's own numbers — persistence, day
and intraday move, gate state, whether it was the pick.

Three outputs, all from the same rows:

* ``board.SUMMARY`` in the audit JSON — one row per name for the session: first,
  peak, trough and last move, its best rank, how many candles it was in its
  sector's top five, whether it was entered, what it was refused as, and what
  the trade made;
* a ``board-<session>-<bot>.jsonl`` companion, one line per candle, holding every
  name's numbers — 72 lines of ~9 KB, readable by ``pandas.read_json(lines=True)``;
* a printable HTML board (``board-<session>-<bot>.html``) — the sector matrix,
  the name summary and a name × candle heat map per sector, laid out for A4
  landscape so the browser's own "Save as PDF" produces the PDF.

Cost, and why the period knob exists: 190 symbols scored every five minutes is
190 frames per sweep. At the production ``YF_PERIOD_5M`` of ``7d`` that is ~430
MB of Yahoo payload a day and a rate-limit complaint; ``AUDIT_BOARD_PERIOD``
defaults to ``1d`` — the current session only, which is all ``name_rank`` reads
(it takes the session's own slice for the range, volume median and EMA9). Set it
to ``7d`` if you want the EMA9 seeded across days and do not mind the traffic.
"""
from __future__ import annotations

import html as _html
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import data

log = logging.getLogger("nse_scanner.board")

BAR = 5

#: One row per name per candle, as parallel lists under each sector. Short keys
#: because this is written 190 times a candle and read back by index.
FIELDS = ("n", "rank", "s", "v", "t", "lead", "mv", "rk")

#: The first candle a sweep can judge, and the last: 09:15 is what the 09:20
#: scan sees closed, 15:10 is what the 15:15 scan sees.
_FIRST_CANDLE = 9 * 60 + 15


# ── knobs ───────────────────────────────────────────────────────────────────
def board_enabled() -> bool:
    """Record the full board (``AUDIT_BOARD``, 1)."""
    return os.environ.get("AUDIT_BOARD", "1").strip() != "0"


def board_every_min() -> int:
    """Minutes between sweeps (``AUDIT_BOARD_EVERY_MIN``, 5).

    Only 5 (every candle), 15 or 30 make sense; anything that is not a divisor
    of the candle grid is rounded down to the nearest one so the sweeps stay on
    the same candles all session.
    """
    try:
        every = int(os.environ.get("AUDIT_BOARD_EVERY_MIN", "5"))
    except ValueError:
        return 5
    return every if every in (5, 10, 15, 30, 60) else 5


def _clock(var: str, default: str) -> int:
    text = os.environ.get(var, default).strip()
    try:
        hh, mm = text.split(":")[:2]
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        hh, mm = default.split(":")
        return int(hh) * 60 + int(mm)


def board_from() -> str:
    """First scan clock time that records (``AUDIT_BOARD_FROM``, 09:20)."""
    mins = _clock("AUDIT_BOARD_FROM", "09:20")
    return f"{mins // 60:02d}:{mins % 60:02d}"


def board_to() -> str:
    """Last scan clock time that records (``AUDIT_BOARD_TO``, 15:15)."""
    mins = _clock("AUDIT_BOARD_TO", "15:15")
    return f"{mins // 60:02d}:{mins % 60:02d}"


def board_period() -> str:
    """Yahoo period for the sweep's 5m frames (``AUDIT_BOARD_PERIOD``, 1d)."""
    return os.environ.get("AUDIT_BOARD_PERIOD", "1d").strip() or "1d"


# ── the ledger book ─────────────────────────────────────────────────────────
def book(st: dict) -> dict:
    """``stats.board`` — the sweeps, the meta and nothing else."""
    return st.setdefault("stats", {}).setdefault("board", {})


def bars(st: dict) -> list[dict]:
    return book(st).setdefault("bars", [])


def _meta(st: dict) -> dict:
    meta = book(st).setdefault("meta", {})
    meta.setdefault("fields", list(FIELDS))
    meta.setdefault("period", board_period())
    meta.setdefault("every_min", board_every_min())
    meta.setdefault("from", board_from())
    meta.setdefault("to", board_to())
    meta.setdefault("sweeps", 0)
    meta.setdefault("names", 0)
    meta.setdefault("stale", 0)
    meta.setdefault("skipped", [])
    meta.setdefault("errors", [])
    meta.setdefault("seconds", [])
    return meta


def _cells(st: dict, hhmm: str) -> dict:
    """The timeline row for the candle ``hhmm`` — every sector's numbers.

    The timeline is the one place the engine already writes all fifteen sectors
    per candle (``persist_bull``, ``persist_bear``, ``day_ret``, ``intra_ret``,
    gate). Reading it here means the board cannot disagree with the report about
    what a sector scored.
    """
    for row in reversed((st.get("stats") or {}).get("timeline") or []):
        if str(row.get("b")) == hhmm:
            return row
    return {}


def _grid_offset(last: datetime) -> int:
    """Minutes from 09:15 to ``last`` — the sweep grid's index, from 0."""
    return int((last.hour * 60 + last.minute) - _FIRST_CANDLE)


# ── the sweep ───────────────────────────────────────────────────────────────
#: The frames of the most recent successful sweep, kept for the name-first screen
#: (bot 4). The board fetches the whole universe to score it; the screen judges the
#: same names on the same candle, so re-fetching them would double the network cost
#: of every candle and hand the screen *different* bars from the ones the board
#: counted. Replaced (never appended) so nothing outlives its candle.
_LAST_FRAMES: dict = {}


def last_frames() -> dict:
    """The frames the last successful sweep scored — for readers of the same candle."""
    return _LAST_FRAMES


def sweep(st: dict | None = None, *, now: datetime | None = None,
          frames: dict | None = None) -> dict | None:
    """Score the whole board on the candle that just closed. ``None`` = not now.

    Never raises: the board is a record, and a record that can take the day's
    scans down with it is worse than a missing candle. A refusal is counted in
    ``meta.skipped`` with the candle, a failure in ``meta.errors``.
    """
    from .scan1m import _bar_open, load_ledger, save_ledger
    from .session import now_ist

    if not board_enabled():
        return None
    now = now or now_ist()
    own = st is None
    try:
        bar = _bar_open(now)
        last = bar - timedelta(minutes=BAR)
        hhmm = last.strftime("%H:%M")
        clock = now.hour * 60 + now.minute
        if not (_clock("AUDIT_BOARD_FROM", "09:20") <= clock
                <= _clock("AUDIT_BOARD_TO", "15:15")):
            return None
        every = board_every_min()
        if _grid_offset(last) % every:
            return None
        st = st if st is not None else load_ledger()
        meta = _meta(st)
        if meta.get("slow"):
            return None
        if any(str(r.get("b")) == hhmm for r in bars(st)):
            return None
        row = _cells(st, hhmm)
        if not row.get("sec"):
            # No sector numbers for this candle: the scan never judged it. Say
            # so once instead of writing a board of nulls.
            if hhmm not in meta["skipped"]:
                meta["skipped"].append(hhmm)
            if own:
                save_ledger(st)
            return None
        started = time.monotonic()
        uni = data.load_universe()
        wanted = [data.yahoo_stock(raw) for spec in (uni.get("sectors") or {}).values()
                  for raw in (spec.get("stocks") or [])]
        # A caller that already holds frames for its scanned sector hands them
        # over: those are the production frames (7 days, so EMA9 is seeded the way
        # the trim seeds it) and fetching them again would only make the sweep
        # slower and its numbers *less* like the log's.
        held = set(wanted)
        known = {sym: df for sym, df in (frames or {}).items() if sym in held}
        missing = [sym for sym in dict.fromkeys(wanted) if sym not in known]
        if missing:
            known.update(data.fetch_many(missing, board_period(), "5m"))
        frames = known
        _LAST_FRAMES.clear()
        _LAST_FRAMES.update(frames)
        secs, names, stale = _score(frames, uni, row, last)
        elapsed = round(time.monotonic() - started, 2)
        meta["names"] = meta.get("names", 0) + names
        meta["stale"] = meta.get("stale", 0) + stale
        meta["seconds"] = (meta.get("seconds") or [])[-19:] + [elapsed]
        if not names:
            # No name's frame had published this candle: an empty board of empty
            # sectors is noise, so the candle is named once and left out — but the
            # names that were skipped for it are still counted.
            if hhmm not in meta["skipped"]:
                meta["skipped"].append(hhmm)
            if own:
                save_ledger(st)
            return None
        payload = {"b": hhmm, "scan": bar.strftime("%H:%M"),
                   "tilt": int(row.get("tilt") or 0), "pick": row.get("pick") or "",
                   "sectors": secs}
        bars(st).append(payload)
        del bars(st)[:-130]
        meta["sweeps"] = meta.get("sweeps", 0) + 1
        if elapsed > 45:
            # 45 s inside a sixty-second loop is not a sweep, it is an outage
            # waiting to happen. Stop, and leave the reason in the file.
            meta["slow"] = f"{hhmm} took {elapsed:.0f}s — further sweeps stopped"
        if own:
            save_ledger(st)
        log.debug("board %s: %d name(s) scored in %.2fs", hhmm, names, elapsed)
        return payload
    except Exception as e:                  # noqa: BLE001 — a record never breaks the scan
        log.exception("board sweep failed at %s", now.strftime("%H:%M:%S"))
        try:
            meta = _meta(st if st is not None else {})
            meta["errors"] = ((meta.get("errors") or [])[-4:]
                              + [f"{now:%H:%M} {type(e).__name__}: {e}"])
            if own and st is not None:
                save_ledger(st)             # the failure is part of the record
        except Exception:                   # noqa: BLE001
            pass
        return None


def _score(frames: dict, uni: dict, row: dict, last: datetime) -> tuple[dict, int, int]:
    """Every name of every sector, ranked inside its own sector.

    A name is scored when its newest bar **is** the candle being judged — the
    same test ``sector_leaders`` applies before it ranks anything, so a stale
    feed cannot put an old close into today's board. The rest are counted.
    """
    from .pipeline import _session_open, name_rank
    from .session import to_ist

    out: dict[str, dict] = {}
    scored = stale = 0
    for sector, spec in (uni.get("sectors") or {}).items():
        cell = (row.get("sec") or {}).get(sector)
        if not cell:
            continue
        bull, bear, day, intra = (float(cell[0] or 0), float(cell[1] or 0),
                                  float(cell[2] or 0), float(cell[3] or 0))
        side = 1 if bull >= bear else -1
        found: list[tuple] = []
        for raw in (spec.get("stocks") or []):
            df = frames.get(data.yahoo_stock(raw))
            if df is None or getattr(df, "empty", True):
                continue
            try:
                df = df.loc[df.index <= last]
                if df.empty:
                    continue
                if to_ist(df.index[-1]) != last:
                    stale += 1
                    continue
                close = float(df["Close"].iloc[-1])
                if not close:
                    continue
                rank, parts = name_rank(df, last, close, side == 1, intra)
                if rank <= 0:
                    continue
                open_px = _session_open(df, last)
                move = (round((close - open_px) / open_px * 100.0, 2)
                        if open_px else None)
            except Exception:               # noqa: BLE001 — one bad frame is not the board
                stale += 1
                continue
            found.append((float(rank), raw, parts, move))
        found.sort(key=lambda t: (-t[0], t[1]))
        scored += len(found)
        out[sector] = {
            "side": side, "bull": round(bull), "bear": round(bear),
            "persist": round(bull if side == 1 else bear),
            "day": day, "intra": intra, "gate": bool(cell[4]) if len(cell) > 4 else False,
            "pick": (row.get("pick") or "") == sector,
            "n": [t[1] for t in found],
            "rank": [round(t[0], 1) for t in found],
            "s": [(t[2] or {}).get("strength") for t in found],
            "v": [(t[2] or {}).get("volume") for t in found],
            "t": [(t[2] or {}).get("structure") for t in found],
            "lead": [(t[2] or {}).get("lead") for t in found],
            "mv": [t[3] for t in found],
            "rk": list(range(1, len(found) + 1)),
        }
    return out, scored, stale


# ── the session's performance, one row per name ─────────────────────────────
def summary(st: dict, *, closed: list[dict] | None = None,
            opened: list[dict] | None = None,
            blocked: list[dict] | None = None) -> list[dict]:
    """What every name did across the day's sweeps, and what it was to us.

    The board says what a name scored; this says what that came to — first and
    last move from its own open, the peak and trough it reached between them, its
    best rank in its sector, and how many candles it sat in the top five. Then
    the join the day is judged on: entered, refused as *what*, and the trade's
    net R.
    """
    # A name can sit in more than one sector — AXISBANK is in both BANK and FIN,
    # and the two score it against different indices, so the row is (sector, name).
    by_key = {(t.get("sector") or "", t.get("name")): t
              for t in [*(closed or []), *(opened or [])]}
    by_name = {t.get("name"): t for t in [*(closed or []), *(opened or [])]}
    refused: dict[str, str] = {}
    for row in blocked or []:
        key = str(row.get("key") or "").split("@")[0]
        if key and key not in refused:
            refused[key] = str(row.get("rule") or "")

    agg: dict[tuple, dict] = {}
    for bar in bars(st):
        for sector, sec in (bar.get("sectors") or {}).items():
            names = sec.get("n") or []
            moves = sec.get("mv") or []
            scores = sec.get("rank") or []
            places = sec.get("rk") or []
            for i, name in enumerate(names):
                a = agg.setdefault((sector, name), {
                    "name": name, "sector": sector, "side": sec.get("side"),
                    "candles": 0, "first_bar": bar.get("b"), "last_bar": bar.get("b"),
                    "first_mv": None, "last_mv": None, "peak_mv": None,
                    "trough_mv": None, "best_rank": None, "rank_sum": 0.0,
                    "best_pos": None, "top5": 0})
                move = moves[i] if i < len(moves) else None
                score = scores[i] if i < len(scores) else None
                place = places[i] if i < len(places) else i + 1
                a["candles"] += 1
                a["last_bar"], a["last_mv"] = bar.get("b"), move
                if a["first_mv"] is None:
                    a["first_bar"], a["first_mv"] = bar.get("b"), move
                if move is not None:
                    a["peak_mv"] = move if a["peak_mv"] is None else max(a["peak_mv"], move)
                    a["trough_mv"] = move if a["trough_mv"] is None else min(a["trough_mv"], move)
                if score is not None:
                    a["rank_sum"] += score
                    a["best_rank"] = score if a["best_rank"] is None else max(a["best_rank"], score)
                a["best_pos"] = place if a["best_pos"] is None else min(a["best_pos"], place)
                a["top5"] += 1 if place <= 5 else 0
    out = []
    for (sector, name), a in agg.items():
        trade = by_key.get((sector, name)) or by_name.get(name) or {}
        row = {k: v for k, v in a.items() if k != "rank_sum"}
        row["rank_avg"] = round(a["rank_sum"] / a["candles"], 1) if a["candles"] else None
        row["entered"] = bool(trade)
        row["net_r"] = trade.get("net_r")
        row["blocked"] = "" if trade else refused.get(name, "")
        out.append(row)
    out.sort(key=lambda r: (-(r.get("last_mv") if r.get("last_mv") is not None else -99),
                            -(r.get("best_rank") or 0)))
    return out


# ── the files ───────────────────────────────────────────────────────────────
def write_files(st: dict, audit: dict, *, session: str, bot: str = "",
                directory: Path | None = None) -> list[Path]:
    """Write the JSONL companion and the printable HTML board.

    Returns the paths written, which the caller puts in the audit (`board.files`)
    so the end-of-day message can attach them without knowing their names.
    """
    from .audit import directory as audit_dir

    if not bars(st):
        return []                           # nothing swept: no empty page to explain
    out = directory or audit_dir()
    out.mkdir(parents=True, exist_ok=True)
    stem = f"board-{session or 'session'}" + (f"-{bot}" if bot else "")
    jsonl, page = out / f"{stem}.jsonl", out / f"{stem}.html"
    meta = dict((st.get("stats") or {}).get("board", {}).get("meta") or {})
    meta["missing"] = _gaps(bars(st))
    meta["last_bar"] = (bars(st) or [{}])[-1].get("b")
    with jsonl.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"meta": {**meta, "fields": list(FIELDS)}}, default=str) + "\n")
        for bar in bars(st):
            fh.write(json.dumps(bar, default=str, separators=(",", ":")) + "\n")
    page.write_text(render_html(audit, st, meta=meta), encoding="utf-8")
    return [jsonl, page]


def _gaps(rows: list[dict]) -> list[str]:
    """The candles between the first and the last recorded one that are absent.

    A session that stops at 11:05 leaves no other trace: every later sweep never
    happened, so nothing counted it. The HTML and the audit both read this.
    """
    seen = [str(r.get("b")) for r in rows if r.get("b")]
    if len(seen) < 2:
        return []
    mins = sorted(int(b[:2]) * 60 + int(b[3:]) for b in seen)
    have = set(mins)
    return [f"{m // 60:02d}:{m % 60:02d}"
            for m in range(mins[0], mins[-1] + 1, BAR) if m not in have]


def _e(text) -> str:
    return _html.escape(str(text if text is not None else ""))


def _num(value, nd: int = 2, sign: bool = False) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):+.{nd}f}" if sign else f"{float(value):.{nd}f}"
    except (TypeError, ValueError):
        return _e(value)


def _step(value, scale: float, buckets: int = 5) -> int:
    """How strong a cell is, 0..buckets — colour without a style per cell."""
    try:
        v = abs(float(value))
    except (TypeError, ValueError):
        return 0
    return min(buckets, int(v / scale * buckets))


def _move_class(value) -> str:
    if value is None:
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if not v:
        return ""
    step = _step(v, 3.0)
    return f"{'p' if v > 0 else 'n'}{step}" if step else ""


def _score_class(value) -> str:
    """Persist: 85 and 70 are the two lines the engine itself draws."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if v >= 85:
        return "q3"
    if v >= 70:
        return "q2"
    if v >= 55:
        return "q1"
    return ""


_CSS = """
:root { color-scheme: light }
* { box-sizing: border-box }
body { margin: 14px; font: 12px/1.4 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       color: #10151c; background: #fff }
h1 { font-size: 19px; margin: 0 0 2px }
h2 { font-size: 14px; margin: 22px 0 6px; padding-bottom: 3px; border-bottom: 2px solid #10151c }
p.meta, p.note { margin: 2px 0 0; color: #4a5560; font-size: 11px }
table { border-collapse: collapse; margin-top: 6px }
th, td { border: 1px solid #d8dee5; padding: 1px 4px; white-space: nowrap;
         font-variant-numeric: tabular-nums }
th { background: #f1f4f8; font-weight: 600; text-align: left }
thead th { position: sticky; top: 0; z-index: 1 }
table.sortable th { cursor: pointer; user-select: none }
table.sortable th:hover { background: #e3e9f1 }
td.n, th.n { text-align: right }
tr.sec td { background: #f7f9fc; font-weight: 600 }
td.pick { box-shadow: inset 0 -2px 0 #10151c; font-weight: 700 }
.p1 { background: #eaf7ee } .p2 { background: #d2eeda } .p3 { background: #aee0bd }
.p4 { background: #82cd9b } .p5 { background: #4fae72; color: #fff }
.n1 { background: #fdeeee } .n2 { background: #fad7d7 } .n3 { background: #f5b4b4 }
.n4 { background: #ee8f8f } .n5 { background: #e06464; color: #fff }
.q1 { background: #fbfcfe } .q2 { background: #eef4ff } .q3 { background: #dbe8ff; font-weight: 700 }
td.hit { outline: 2px solid #1a7f37; outline-offset: -2px }
.legend span { display: inline-block; padding: 1px 6px; margin-right: 4px; border: 1px solid #d8dee5 }
table.grid td, table.grid th { padding: 0 2px; font-size: 9px }
table.grid th.b { writing-mode: vertical-rl; text-orientation: mixed; height: 44px;
                  font-weight: 500; vertical-align: bottom }
table.grid td.name, table.grid th.name { text-align: left; font-size: 10px;
                                         padding-right: 5px }
.small { font-size: 10px; color: #59626d }
.sector-block { page-break-inside: avoid }
.sector-block h2 { page-break-before: auto }
@page { size: A4 landscape; margin: 7mm }
@media print { body { margin: 0 } h2 { page-break-after: avoid }
               thead th { position: static } .noprint { display: none } }
"""

_SORT_JS = """
document.querySelectorAll('table.sortable thead tr').forEach(function (hr) {
  Array.prototype.forEach.call(hr.cells, function (th, i) {
    th.addEventListener('click', function () {
      var table = th.closest('table'), body = table.tBodies[0];
      var rows = Array.prototype.slice.call(body.rows);
      var dir = th.getAttribute('data-dir') === 'asc' ? -1 : 1;
      th.setAttribute('data-dir', dir === 1 ? 'asc' : 'desc');
      var val = function (r) {
        var c = r.cells[i]; if (!c) return -1e9;
        var raw = c.getAttribute('data-v'); if (raw === null) raw = c.textContent;
        var n = parseFloat(String(raw).replace(/[^0-9eE.+-]/g, ''));
        return isNaN(n) ? -1e9 : n;
      };
      rows.sort(function (a, b) { return dir * (val(a) - val(b)); });
      rows.forEach(function (r) { body.appendChild(r); });
    });
  });
});
"""


def render_html(audit: dict, st: dict, *, meta: dict | None = None) -> str:
    """One self-contained page: sector matrix, name summary, per-sector heat maps."""
    meta = meta if meta is not None else dict(
        (st.get("stats") or {}).get("board", {}).get("meta") or {})
    session = audit.get("session") or st.get("session") or ""
    bot = audit.get("bot") or ""
    rows = bars(st)
    names = audit.get("board", {}).get("summary") or summary(st)
    secs = sorted({s for bar in rows for s in (bar.get("sectors") or {})})
    clock = [str(bar.get("b")) for bar in rows]

    head = [
        f"<h1>Board — {_e(session)}" + (f" · {_e(bot)}" if bot else "") + "</h1>",
        f"<p class=meta>{len(rows)} candle(s) · {len(secs)} sector(s) · "
        f"{meta.get('names', 0)} name-scorings · every {meta.get('every_min', 5)} min · "
        f"scored on {_e(meta.get('period', '1d'))} of 5m data · "
        f"scan {_e(meta.get('from', '09:20'))}–{_e(meta.get('to', '15:15'))}</p>",
        "<p class=note>Each candle's numbers are the ones the engine had on that candle: "
        "the same <b>strength · volume · structure</b> rank the trim uses, against the "
        "sector's own move from its open. <b>mv</b> is the name's move from its own "
        "open, in %. A boxed cell is the sector that was picked on that candle.</p>",
        "<p class='note legend'>move: <span class='p3'>+</span><span class='p5'>+</span>"
        "<span class='n3'>−</span><span class='n5'>−</span> &nbsp; persist: "
        "<span class='q1'>55+</span><span class='q2'>70+</span><span class='q3'>85+</span></p>",
    ]

    # 1. the sector matrix — one row per sector, one column per candle
    parts = list(head)
    if rows:
        parts.append("<h2>Sector board</h2><table class=grid>")
        parts.append("<thead><tr><th class=name>sector</th><th class=n>side</th>"
                     + "".join(f"<th class='n b'>{_e(c)}</th>" for c in clock)
                     + "<th class=n>day %</th><th class=n>intra %</th><th class=n>picks</th></tr></thead><tbody>")
        for sector in secs:
            cells, day, intra, side, picks = [], None, None, None, 0
            for bar in rows:
                sec = (bar.get("sectors") or {}).get(sector)
                if not sec:
                    cells.append("<td></td>")
                    continue
                day, intra, side = sec.get("day"), sec.get("intra"), sec.get("side")
                picks += 1 if sec.get("pick") else 0
                cls = _score_class(sec.get("persist"))
                mark = " pick" if sec.get("pick") else ""
                cells.append(f"<td class='n {cls}{mark}' data-v='{sec.get('persist')}' "
                             f"title='{_e(sector)} {_e(bar.get('b'))} · persist "
                             f"{_e(sec.get('persist'))} · day {_num(sec.get('day'), 2, True)}% · "
                             f"intra {_num(sec.get('intra'), 2, True)}%'>{_e(sec.get('persist'))}</td>")
            parts.append(f"<tr><td><b>{_e(sector)}</b></td>"
                         f"<td class=n>{'long' if side == 1 else 'short' if side == -1 else ''}</td>"
                         + "".join(cells)
                         + f"<td class=n>{_num(day, 2, True)}</td>"
                         f"<td class=n>{_num(intra, 2, True)}</td>"
                         f"<td class=n>{picks}</td></tr>")
        parts.append("</tbody></table>")

    # 2. the day, per name
    if names:
        parts.append("<h2>Every name, the whole session</h2>"
                     "<p class=note>Sorted by last move; click a heading to sort. "
                     "<b>in 5</b> = candles it sat in its sector's top five. "
                     "<b>refused</b> is the rule that stopped it, if any.</p>"
                     "<table class=sortable><thead><tr>"
                     "<th>name</th><th>sector</th><th class=n>candles</th>"
                     "<th class=n>first %</th><th class=n>peak %</th><th class=n>trough %</th>"
                     "<th class=n>last %</th><th class=n>best score</th><th class=n>avg score</th>"
                     "<th class=n>best pos</th><th class=n>in 5</th>"
                     "<th>taken</th><th class=n>net R</th><th>refused</th>"
                     "</tr></thead><tbody>")
        for r in names:
            taken = "entered" if r.get("entered") else ""
            parts.append(
                f"<tr><td><b>{_e(r.get('name'))}</b></td><td>{_e(r.get('sector'))}</td>"
                f"<td class=n data-v='{_e(r.get('candles'))}'>{_e(r.get('candles'))}</td>"
                f"<td class='n {_move_class(r.get('first_mv'))}'>{_num(r.get('first_mv'), 2, True)}</td>"
                f"<td class='n {_move_class(r.get('peak_mv'))}'>{_num(r.get('peak_mv'), 2, True)}</td>"
                f"<td class='n {_move_class(r.get('trough_mv'))}'>{_num(r.get('trough_mv'), 2, True)}</td>"
                f"<td class='n {_move_class(r.get('last_mv'))}'>{_num(r.get('last_mv'), 2, True)}</td>"
                f"<td class=n data-v='{_e(r.get('best_rank'))}'>{_num(r.get('best_rank'), 1)}</td>"
                f"<td class=n>{_num(r.get('rank_avg'), 1)}</td>"
                f"<td class=n data-v='{_e(r.get('best_pos'))}'>{_e(r.get('best_pos'))}</td>"
                f"<td class=n data-v='{_e(r.get('top5'))}'>{_e(r.get('top5'))}</td>"
                f"<td>{taken}</td>"
                f"<td class='n {_move_class(r.get('net_r'))}'>{_num(r.get('net_r'), 2, True)}</td>"
                f"<td class=small>{_e(r.get('blocked'))}</td></tr>")
        parts.append("</tbody></table>")

    # 3. the heat map: every name against every candle, inside its own sector
    for sector in secs:
        present = [b for b in rows if (b.get("sectors") or {}).get(sector)]
        if not present:
            continue
        sec0 = present[0]["sectors"][sector]
        nameset = []
        for bar in present:
            for n in bar["sectors"][sector].get("n") or []:
                if n not in nameset:
                    nameset.append(n)
        parts.append(f"<div class=sector-block><h2>{_e(sector)} "
                     f"({'long' if sec0.get('side') == 1 else 'short'}) — "
                     f"{len(nameset)} name(s), {len(present)} candle(s)</h2>"
                     "<table class=grid><thead><tr><th class=name>name</th>"
                     + "".join(f"<th class='n b'>{_e(b.get('b'))}</th>" for b in present)
                     + "</tr></thead><tbody>")
        for name in nameset:
            cells = []
            for bar in present:
                sec = bar["sectors"][sector]
                try:
                    i = (sec.get("n") or []).index(name)
                except ValueError:
                    cells.append("<td></td>")
                    continue
                move = (sec.get("mv") or [None])[i]
                rank = (sec.get("rank") or [None])[i]
                top = int((sec.get("rk") or [99])[i])
                cls = _move_class(move)
                hit = " hit" if top <= 5 else ""
                cells.append(f"<td class='n {cls}{hit}' data-v='{_e(move)}' "
                             f"title='rank {_num(rank, 1)} · #{top} in sector'>{_num(move, 2, True)}</td>")
            parts.append(f"<tr><td class=name>{_e(name)}</td>" + "".join(cells) + "</tr>")
        parts.append("</tbody></table></div>")

    if not rows:
        parts.append("<p class=note>No board was recorded — "
                     "check <b>AUDIT_BOARD</b> and the diagnostics in the audit JSON.</p>")
    if (meta.get("errors") or meta.get("slow") or meta.get("skipped")
            or meta.get("missing")):
        parts.append("<h2>What the sweep did not do</h2><ul class=small>")
        if meta.get("slow"):
            parts.append(f"<li>{_e(meta.get('slow'))}</li>")
        for e in meta.get("errors") or []:
            parts.append(f"<li>{_e(e)}</li>")
        if meta.get("skipped"):
            parts.append(f"<li>candles with no sector numbers, not recorded: "
                         f"{_e(', '.join(map(str, meta['skipped'])))}</li>")
        if meta.get("missing"):
            parts.append(f"<li>candles missing inside the session "
                         f"(last recorded {_e(meta.get('last_bar'))}): "
                         f"{_e(', '.join(map(str, meta['missing'][:24])))}, "
                         f"{len(meta['missing'])} in all</li>")
        if meta.get("stale"):
            parts.append(f"<li>name-candles skipped because the frame had not "
                         f"published this candle: {_e(meta.get('stale'))}</li>")
        parts.append("</ul>")

    return ("<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>Board {_e(session)}</title><style>{_CSS}</style></head><body>"
            + "".join(parts)
            + f"<script>{_SORT_JS}</script></body></html>")
