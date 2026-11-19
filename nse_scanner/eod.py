"""The end-of-day report: everything the session did, and why it did not do more.

The daily summary that already existed answers "what was sent". This answers the
next question — *what did the day look like, and what should change* — from the
books the scanners already keep: every candle's pick, every sector's persistence
through the day, every signal that was blocked and the rule that blocked it,
every trade and what it returned, and the data that was missing.

It is written for Telegram, so it is long: the text is split into numbered
messages of at most ``EOD_MAX_CHARS`` characters, cut on section and line
boundaries, and the session is only marked ``reported`` when every part has been
delivered — a half-sent report is worse than none, because it reads as complete.
"""
from __future__ import annotations

import logging
import os

from . import telegram

log = logging.getLogger(__name__)

#: Telegram refuses a message longer than 4096 characters; leave room for the
#: part marker and an HTML entity or two.
DEFAULT_LIMIT = 3800


def report_enabled() -> bool:
    """Send the full end-of-day report instead of the short summary (``EOD_REPORT``, 1)."""
    return os.environ.get("EOD_REPORT", "1").strip() != "0"


def max_chars() -> int:
    try:
        return max(1000, min(4000, int(os.environ.get("EOD_MAX_CHARS", str(DEFAULT_LIMIT)))))
    except ValueError:
        return DEFAULT_LIMIT


def _clip(text, limit: int = 64) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1].rstrip(" ,—") + "…"


def _hhmm(ts) -> str:
    """``10:15`` out of any ISO timestamp the ledger may hold."""
    text = str(ts or "")
    if "T" in text:
        return text.split("T", 1)[1][:5]
    return text[-5:] if len(text) > 5 else text


def _pct(v) -> str:
    return f"{float(v):+.2f}%"


def _n(count: int, word: str, plural: str | None = None) -> str:
    """``1 entry`` / ``4 entries`` — the report is read at a glance."""
    return f"{count} {word if abs(count) == 1 else (plural or word + 's')}"


# ── sections ────────────────────────────────────────────────────────────────
def _head(st: dict, bot: str) -> list[str]:
    x = st.get("stats") or {}
    m = st.get("market") or {}
    scan = st.get("scan") or {}
    pick = scan.get("sector") or "WAIT"
    side = int(scan.get("side") or 0)
    scanned = int(x.get("candles") or 0)
    judged = len(x["judged"]) if isinstance(x.get("judged"), dict) else scanned
    takeover = [f"🗂 <b>END OF DAY — {st.get('session', '')} · bot {bot}</b>"]
    if m.get("label"):
        takeover.append(f"Market closed {m.get('label')} ({m.get('conf', '?')}%) · regime "
                        f"{m.get('regime', '?')} · last pick <b>{pick}</b> "
                        f"{'long' if side == 1 else 'short' if side == -1 else ''}".rstrip())
    else:
        takeover.append(f"Last pick <b>{pick}</b> "
                        f"{'long' if side == 1 else 'short' if side == -1 else ''}".rstrip())
    takeover.append(f"Candles scanned {scanned}"
                    + (f" · judged {judged}" if judged != scanned else "")
                    + f" · with a pick {x.get('picks', 0)}"
                    f" · entries {x.get('entries', 0)}")
    takeover.append(_sent(st))
    return takeover


def _sent(st: dict) -> str:
    """What actually went out, by kind — including what Telegram refused."""
    sent = st.get("sent") or {}
    kinds: dict[str, int] = {}
    for rec in sent.values():
        code = (rec or {}).get("code") or "?"
        kinds[code] = kinds.get(code, 0) + 1
    total = sum(kinds.values())
    if not total:
        return "Alerts on record: none"
    order = sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))
    # The alert ledger is keyed by stock, so it keeps the *last* code per name:
    # target hits for one name are counted on the trade, not here.
    line = (f"Alerts on record {total} (last code per name): "
            + " · ".join(f"{k} {n}" for k, n in order[:8]))
    refused = len(st.get("undelivered") or {})
    if refused:
        line += f" · <b>{refused} undelivered</b>"
    return line


def _day_shape(st: dict) -> list[str]:
    """Picks by sector and side, how often the pick changed, first and last times."""
    x = st.get("stats") or {}
    hist = x.get("sector_hist") or {}
    if not hist and not _pick_trail(st):
        return []
    picked = {name: h for name, h in hist.items() if int(h.get("picks") or 0)}
    lines = ["<b>How the day was picked</b>"]
    if not picked and not hist:
        lines.append("No sector-pick history on record for this session.")
    elif not picked:
        lines.append("No sector was picked all session — the market said WAIT on every candle.")
    else:
        order = sorted(picked.items(), key=lambda kv: -int(kv[1].get("picks") or 0))
        lines.append("Pick (candles): " + " · ".join(
            f"{name} {int(h['picks'])}" for name, h in order[:8]))
        longest = order[0]
        lines.append(f"Longest run {longest[0]} {int(longest[1]['picks'])} candles "
                     f"({int(longest[1]['picks']) * 5} min) · "
                     f"{len(picked)} sector(s) picked in all")
    trail = _pick_trail(st)
    if trail:
        lines.append(trail)
    extra = x.get("extra") or {}
    if extra:
        lines.append("Scanned beside the pick: " + " · ".join(
            f"{n} {c}" for n, c in sorted(extra.items(), key=lambda kv: -kv[1])[:6]))
    return lines


def _pick_trail(st: dict) -> str:
    """First pick, last pick, and how often the incumbent changed.

    ``scan["log"]`` keeps the last 48 candles' pick, so this is the tail of the
    day — enough to see a pick that stuck and one that flip-flopped.
    """
    log_book = (st.get("scan") or {}).get("log")
    if not isinstance(log_book, dict) or not log_book:
        return ""
    bars = sorted(log_book)
    trail = [(b, rec) for b in bars
             if isinstance((rec := log_book[b]), dict) and rec]
    live = [(b, rec) for b, rec in trail if (rec.get("name") or "WAIT") != "WAIT"]
    if not live:
        return ""
    changes = sum(1 for (_, a), (_, b) in zip(trail, trail[1:])
                  if (a.get("name"), a.get("side")) != (b.get("name"), b.get("side")))
    span = f"first pick {_hhmm(live[0][0])} · last {_hhmm(live[-1][0])}"
    if len(trail) < 48:
        span += f" · {len(trail)} candles on record"
    return f"{span} · changed {_n(changes, 'time')}"


def _sector_table(st: dict) -> list[str]:
    """Each sector's persistence through the day, and what it produced."""
    x = st.get("stats") or {}
    hist = x.get("sector_hist") or {}
    if not hist:
        return []
    entries = {}
    for rec in x.get("closed") or []:
        entries[rec.get("sector")] = entries.get(rec.get("sector"), 0) + 1
    for raw, pos in (st.get("positions") or {}).items():
        entries[pos.get("sector") or ""] = entries.get(pos.get("sector") or "", 0) + 1
    rows = []
    for name, h in hist.items():
        series = h.get("series") or []
        first = series[0][1] if series else h.get("max")
        last = h.get("persist")
        arrow = "↑" if (last or 0) > (first or 0) + 3 else "↓" if (last or 0) < (first or 0) - 3 else "→"
        rows.append((float(last or 0), name, h, arrow, first, last))
    rows.sort(key=lambda r: (-r[0], r[1]))
    lines = ["<b>Sector persistence (the day)</b>"]
    for _, name, h, arrow, first, last in rows[:10]:
        spark = " ".join(str(v) for _, v in (h.get("series") or [])[-6:])
        bits = [f"{name} {int(h.get('max') or 0)}max/{int(last or 0)}now {arrow}"]
        if spark:
            bits.append(f"[{spark}]")
        if h.get("day_ret") is not None:
            bits.append(f"day {_pct(h['day_ret'])}")
        bits.append(f"{int(h.get('picks') or 0)} candles as pick")
        n = entries.get(name)
        if n:
            bits.append(_n(n, "entry", "entries"))
        lines.append(" · ".join(bits))
    if len(rows) > 10:
        lines.append(f"… {len(rows) - 10} more sectors tracked")
    return lines


def _blocked(st: dict) -> list[str]:
    """Every signal that was *not* taken, by the rule that stopped it."""
    x = st.get("stats") or {}
    rejected = x.get("rejected") or {}
    weak = x.get("weak") or {}
    trim = x.get("trim") or {}
    why = x.get("cancel_why") or {}
    cap = x.get("alert_capped") or {}
    if not (rejected or weak or trim or why or cap):
        return []
    from .pipeline import _REJECT_TEXT

    lines = ["<b>Blocked signals — by rule</b>"]
    known = {kind for kind, _ in _REJECT_TEXT}
    total = 0
    for kind, text in _REJECT_TEXT:
        items = rejected.get(kind) or {}
        if not items:
            continue
        total += len(items)
        shown = ", ".join(_clip(v) for v in list(items.values())[:6])
        shown += " …" if len(items) > 6 else ""
        lines.append(f"• {len(items)} — {text}: {shown}")
    for kind, items in rejected.items():
        if kind in known or not items:
            continue
        total += len(items)
        lines.append(f"• {len(items)} — {kind}: "
                     + ", ".join(_clip(v) for v in list(items.values())[:4]))
    if total:
        lines.insert(1, f"{_n(total, 'signal')} blocked in all")
    if weak:
        lines.append("Momentum gate: " + " · ".join(
            f"{k} {v}" for k, v in sorted(weak.items(), key=lambda kv: -kv[1])))
    if why:
        lines.append("Early alerts cancelled: "
                     + " · ".join(f"{_clip(k, 48)} {v}" for k, v in
                                  sorted(why.items(), key=lambda kv: -kv[1])[:4]))
    if isinstance(cap, dict) and cap:
        lines.append("Held back by the per-candle alert cap: "
                     + " · ".join(f"{k} {v}" for k, v in
                                  sorted(cap.items(), key=lambda kv: -kv[1])[:4]))
    elif isinstance(cap, int) and cap:
        lines.append(f"Held back by the per-candle alert cap: {cap} alert(s)")
    if trim:
        kept = sum(int(v.get("kept") or 0) for v in trim.values())
        dropped = sum(int(v.get("dropped") or 0) for v in trim.values())
        lines.append(f"Sector trim (SECTOR_TOP_N): kept {kept} name-checks, dropped {dropped}")
        for sector, v in sorted(trim.items(),
                                key=lambda kv: -int(kv[1].get("dropped") or 0))[:5]:
            lines.append(f"  · {sector}: kept {v.get('kept', 0)} · dropped {v.get('dropped', 0)}")
    return lines


def _trades(st: dict) -> list[str]:
    """Every entry: what it was, and what it did."""
    x = st.get("stats") or {}
    closed = list(x.get("closed") or [])
    open_pos = st.get("positions") or {}
    if not closed and not open_pos:
        return ["<b>Trades</b>", "None opened."]
    lines = ["<b>Trades</b>"]
    for rec in closed[-12:]:
        lines.append(f"• {rec.get('raw')} {rec.get('side')} {rec.get('time')} "
                     f"{(rec.get('entry'))} → {(rec.get('exit'))} "
                     f"<b>{float(rec.get('r') or 0):+.1f}R</b> ({rec.get('why')}"
                     + (f", T{rec.get('t_hit')} hit" if rec.get("t_hit") else "") + ")")
    for raw, pos in open_pos.items():
        r = None
        try:
            risk = float(pos.get("risk") or 0)
            if risk and pos.get("last_close") is not None:
                long = pos.get("side") == "BUY"
                r = ((float(pos["last_close"]) - float(pos["entry"])) if long
                     else (float(pos["entry"]) - float(pos["last_close"]))) / risk
        except (TypeError, ValueError):
            r = None
        lines.append(f"• {raw} {pos.get('side')} open from "
                     f"{_hhmm(pos.get('entry_bar'))} @ {pos.get('entry')}"
                     f" · stop {pos.get('sl')}"
                     + (f" · <b>{r:+.1f}R</b> at the close" if r is not None else " · unmarked")
                     + (f" · T{'T'.join(str(t) for t in pos.get('hit_targets') or ())} hit"
                        if pos.get("hit_targets") else ""))
    by_why: dict[str, int] = {}
    for rec in closed:
        by_why[str(rec.get("why") or "?")] = by_why.get(str(rec.get("why") or "?"), 0) + 1
    if by_why:
        lines.append("Closed by: " + " · ".join(f"{k} {v}" for k, v in
                                               sorted(by_why.items(), key=lambda kv: -kv[1])))
    scored = [float(r.get("r") or 0) for r in closed]
    if scored:
        wins = sum(1 for r in scored if r > 0)
        lines.append(f"Closed {len(scored)}: {wins} up / {len(scored) - wins} down · "
                     f"{sum(scored):+.1f}R in all · best {max(scored):+.1f}R · "
                     f"worst {min(scored):+.1f}R")
    return lines


def _health(st: dict) -> list[str]:
    """Data and delivery problems — the day's silent failures."""
    x = st.get("stats") or {}
    from .pipeline import (_summary_catchup, _summary_early_skip, _summary_mirror,
                           _summary_no_index, _summary_pending, _summary_scan_lag)

    lines = ["<b>Health</b>"]
    for text in (_summary_no_index(x), _summary_scan_lag(x), _summary_pending(st),
                 _summary_catchup(st), _summary_early_skip(x), _summary_mirror()):
        if text:
            lines.append("• " + text)
    if x.get("feed_behind"):
        lines.append(f"• the 1m feed lagged past the close on {x['feed_behind']} candle(s)")
    if x.get("unjudged"):
        names = list(x["unjudged"])
        lines.append(f"• candles never judged: {len(names)} — "
                     + ", ".join(names[:6]) + (" …" if len(names) > 6 else ""))
    if len(lines) == 1:
        lines.append("No feed, scan or delivery problems recorded.")
    return lines


def _rules() -> list[str]:
    """The knobs in force — the context every number above is read against."""
    from . import knobs as knob_mod
    from .pipeline import (entry_min_rank, entry_rank_band, late_max_minutes,
                           max_entries_per_pick, sector_top_n)
    from .trade import min_target_r

    diff = list(knob_mod.differing().items())
    lines = ["<b>Rules in force</b>",
             f"entries: cap {max_entries_per_pick() or 'none'} per pick · runners within "
             f"{entry_rank_band() * 100:.0f}% of the sector's best · rank floor "
             f"{entry_min_rank():.0f} · top {sector_top_n() or 'all'} names per sector · "
             f"targets ≥ {min_target_r():.1f}R · catch-up window {late_max_minutes():.0f} min"]
    if diff:
        shown = ", ".join(f"{k}={v}" for k, v in list(diff)[:10])
        lines.append(f"knobs changed from default: {shown}"
                     + (" …" if len(diff) > 10 else ""))
    else:
        lines.append("no knob changed from its default")
    return lines


def _observations(st: dict) -> list[str]:
    """What the day's own numbers say to change — computed, not guessed."""
    x = st.get("stats") or {}
    out: list[str] = []
    hist = x.get("sector_hist") or {}
    picked = {n: int(h.get("picks") or 0) for n, h in hist.items() if int(h.get("picks") or 0)}
    entries = x.get("entries") or 0
    if picked and not entries:
        lead = max(picked.items(), key=lambda kv: kv[1])
        out.append(f"⚠ {lead[0]} was the pick for {lead[1]} candles and produced no entry — "
                   f"check its names' momentum gate line above")
    weak = x.get("weak") or {}
    blocked = sum(int(v) for v in weak.values())
    if entries and blocked > 5 * entries:
        out.append(f"⚠ {blocked} names refused by the momentum gate against "
                   f"{_n(entries, 'entry', 'entries')} — ENTRY_RANK_BAND may be too "
                   f"tight for this tape")
    early = int(x.get("early_sent") or 0)
    conf = int(x.get("confirmed") or 0)
    if early and conf * 2 < early:
        why = x.get("cancel_why") or {}
        top = max(why.items(), key=lambda kv: kv[1])[0] if why else "unknown"
        out.append(f"⚠ {early - conf} of {early} early alerts died before their close "
                   f"(most often: {top})")
    rejected = x.get("rejected") or {}
    earlier = len((rejected.get("earlier") or {}))
    caught = int(x.get("catchup") or 0) + int(x.get("late_entries") or 0)
    if earlier and caught * 4 < earlier:
        out.append(f"⚠ {_n(earlier, 'signal')} fired before their sector was picked and "
                   f"only {caught} were taken or announced — the pick is late, not the "
                   f"signal")
    if not out:
        out.append("Nothing stands out: the refused names were refused by the rules "
                   "above, at the counts shown.")
    return ["<b>What to look at tomorrow</b>"] + [f"• {line}" for line in out]


# ── the report ──────────────────────────────────────────────────────────────
def format_eod(st: dict, *, bot: str = "5m") -> str:
    """The whole day, in one (long) text. Split it with :func:`chunks` to send."""
    blocks = [_head(st, bot), _day_shape(st), _sector_table(st), _blocked(st),
              _trades(st), _health(st), _rules(), _observations(st)]
    return "\n\n".join("\n".join(b for b in block if b) for block in blocks if block)


def chunks(text: str, limit: int | None = None) -> list[str]:
    """Split the report into messages Telegram will accept, on blank lines first."""
    limit = limit or max_chars()
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf = ""
    for block in text.split("\n\n"):
        piece = block if not buf else "\n\n" + block
        if len(buf) + len(piece) <= limit:
            buf += piece
            continue
        if buf:
            parts.append(buf)
            buf = ""
        while len(block) > limit:                  # one block longer than a message
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            parts.append(block[:cut])
            block = block[cut:].lstrip("\n")
        buf = block
    if buf:
        parts.append(buf)
    total = len(parts)
    return [f"({i}/{total})\n{part}" if total > 1 else part
            for i, part in enumerate(parts, 1)]


def send_eod(st: dict, *, bot: str = "5m", send=None, dry_run: bool = False) -> bool:
    """Send every part of the report. ``True`` only when all of them arrived."""
    send = send or telegram.send
    parts = chunks(format_eod(st, bot=bot))
    ok = True
    for i, part in enumerate(parts, 1):
        if not send(part, dry_run=dry_run):
            log.error("end-of-day report part %d/%d was not delivered", i, len(parts))
            ok = False
    if ok:
        log.info("end-of-day report sent: %d part(s)", len(parts))
    return ok
