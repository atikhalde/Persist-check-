"""The end-of-day report: everything the session did, and what it cost.

The daily summary that already existed answered "what was sent". This answers the
next questions — *what did the day look like, what was refused and why, what did
it pay* — from the books the scanners keep while they run: every candle's tilt and
every sector's numbers, every signal that was blocked with the rule that blocked
it, every name each sector put forward with the three ranking legs, and the trader
ledger with size, costs and rupees.

Telegram is a reading surface, so the text goes out as numbered messages of at
most ``EOD_MAX_CHARS`` characters, cut on section and line boundaries. The same
day also ships as data — a JSON audit and an XLSX workbook (``audit.py``) — and
the session is only marked ``reported`` when every message *and* every attachment
arrived: a half-delivered report reads as complete, which is worse than none.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from . import audit, costs, ledger, telegram

log = logging.getLogger(__name__)

#: Telegram refuses a message longer than 4096 characters; leave room for the
#: part marker and an HTML entity or two.
DEFAULT_LIMIT = 3800
#: How many candles between timeline rows in the message (3 = every 15 minutes).
TIMELINE_EVERY = 3


def report_enabled() -> bool:
    """Send the full end-of-day report instead of the short summary (``EOD_REPORT``, 1)."""
    return os.environ.get("EOD_REPORT", "1").strip() != "0"


def audit_enabled() -> bool:
    """Attach the JSON audit and the workbook (``EOD_AUDIT``, 1)."""
    return os.environ.get("EOD_AUDIT", "1").strip() != "0"


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


def _rs(v) -> str:
    """Rupees, signed, no decimals — a ledger reads at a glance."""
    return f"{float(v):+,.0f}"


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
    judged = (len(x["judged"]) if isinstance(x.get("judged"), dict) and x["judged"]
              else scanned)
    out = [f"🗂 <b>END OF DAY — {st.get('session', '')} · bot {bot}</b>"]
    if m.get("label"):
        out.append(f"Market closed {m.get('label')} ({m.get('conf', '?')}%) · regime "
                   f"{m.get('regime', '?')} · last pick <b>{pick}</b> "
                   f"{'long' if side == 1 else 'short' if side == -1 else ''}".rstrip())
    else:
        out.append(f"Last pick <b>{pick}</b> "
                   f"{'long' if side == 1 else 'short' if side == -1 else ''}".rstrip())
    out.append(f"Candles scanned {scanned}"
               + (f" · judged {judged}" if judged != scanned else "")
               + f" · with a pick {x.get('picks', 0)} · entries {x.get('entries', 0)}")
    out.append(_sent(st))
    out.append(f"Blocked in all: {_n(len(audit.blocked_rows(st)), 'signal')} — "
               f"each one listed below with its rule")
    return out


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


def _picked_from_timeline(st: dict) -> dict[str, int]:
    """Candles per picked sector, read off the tape (bot 3 keeps no histogram)."""
    counts: dict[str, int] = {}
    for row in (st.get("stats") or {}).get("timeline") or []:
        pick = row.get("pick") or "WAIT"
        if pick != "WAIT":
            counts[pick] = counts.get(pick, 0) + 1
    return counts


def _day_shape(st: dict) -> list[str]:
    """Picks by sector and side, how often the pick changed, first and last times."""
    x = st.get("stats") or {}
    hist = x.get("sector_hist") or {}
    if not hist and not _pick_trail(st) and not _picked_from_timeline(st):
        return []
    picked = {name: h for name, h in hist.items() if int(h.get("picks") or 0)}
    counted = {name: int(h.get("picks") or 0) for name, h in picked.items()}
    if not counted:                # bot 3 (and any day with no histogram)
        counted = _picked_from_timeline(st)
    lines = ["<b>How the day was picked</b>"]
    if not hist and not counted:
        pass                       # only the pick trail: say nothing about sectors
    elif not counted and not picked:
        lines.append("No sector was picked all session — the market said WAIT on every candle.")
    else:
        order = sorted(counted.items(), key=lambda kv: -kv[1])
        lines.append("Pick (candles): " + " · ".join(
            f"{name} {n}" for name, n in order[:8]))
        longest = order[0]
        lines.append(f"Longest run {longest[0]} {longest[1]} candles "
                     f"({longest[1] * 5} min) · "
                     f"{len(counted)} sector(s) picked in all")
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
        # bot 3 keeps no pick log: the tape it writes per candle is the same
        # record, so the first pick and the number of changes still come out
        rows = (st.get("stats") or {}).get("timeline") or []
        log_book = {r["b"]: {"name": r.get("pick") or "WAIT", "side": r.get("side") or 0}
                    for r in rows if r.get("b")}
        if not log_book:
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


def _timeline(st: dict) -> list[str]:
    """The day's tape: market tilt, the pick and its flow, and when the pick moved."""
    rows = (st.get("stats") or {}).get("timeline") or []
    if not rows:
        return []
    # A fixed number of rows whatever the day did: ~every 15 minutes, stretched
    # when the session was long, and always including the last candle.
    step = max(TIMELINE_EVERY, -(-len(rows) // 24))
    chosen = rows[::step]
    if chosen[-1] is not rows[-1]:
        chosen = chosen + [rows[-1]]
    lines = ["<b>Sector momentum timeline</b>"
             f" (every {step * 5} min; the full matrix is in the audit)"]
    for row in chosen:
        sec = row.get("sec") or {}
        pick = row.get("pick") or "WAIT"
        label = f"{row.get('label') or '?'} {row.get('conf') or 0}"
        cell = sec.get(pick) if pick != "WAIT" else None
        if cell is None and sec:                       # no pick: show the leader
            side = 1 if row.get("tilt", 0) >= 0 else -1
            name = max(sec, key=lambda n: float(sec[n][0] if side == 1 else sec[n][1]))
            cell, pick = sec[name], f"{name} (leader)"
        bits = [f"{row.get('b')} {label}"]
        if cell:
            bits.append(f"{pick} · flow {cell[0]:.0f}▲/{cell[1]:.0f}▼ · "
                        f"day {_pct(cell[2])} · intra {_pct(cell[3])}")
        else:
            bits.append(pick)
        if row.get("regime"):
            bits.append(str(row["regime"]))
        lines.append(" · ".join(bits))
    changes = []
    previous = None
    for row in rows:
        pick = row.get("pick") or "WAIT"
        if previous is not None and pick != previous:
            changes.append(f"{row.get('b')} {previous}→{pick}")
        previous = pick
    if changes:
        lines.append(f"Pick moved {len(changes)} time(s): "
                     + ", ".join(changes[:8]) + (" …" if len(changes) > 8 else ""))
    return lines


def _persistence(st: dict) -> list[str]:
    """Score range, candles above the gate, when the gate was open, transitions."""
    stats = audit.sector_stats(st)
    if not stats:
        return []
    order = sorted(stats.items(), key=lambda kv: (-(kv[1].get("max") or 0), kv[0]))
    lines = ["<b>Persistent-sector performance</b>"]
    for name, s in order[:6]:
        flow = "P+" if s.get("side") == 1 else "P−"
        spells = s.get("gate_periods") or []
        span = ""
        if spells:
            span = " · gate open " + _n(len(spells), "spell") + " " + ", ".join(
                f"{p['from']}–{p['to']} ({p['candles']})" for p in spells[:3])
            if len(spells) > 3:
                span += f" +{len(spells) - 3}"
        cross = [t for t in (s.get("transitions") or []) if t.get("level") in (70, 85)]
        cross_txt = ""
        if cross:
            cross_txt = " · crossed " + ", ".join(
                f"{t['level']}{'↑' if t['dir'] == 'up' else '↓'}@{t['bar']}"
                for t in cross[:4])
            if len(cross) > 4:
                cross_txt += " …"
        runs = f" ({_n(len(s['pick_periods']), 'run')})" if s.get("pick_candles") else ""
        lines.append(f"{name} {flow} {int(s['min'])}–{int(s['max'])}→{int(s['now'])} · "
                     f"{s['above_70']}/{s['candles']} candles ≥70 · "
                     f"{s['gate_candles']} gate-open{span}"
                     f" · {_n(s['pick_candles'], 'candle')} as pick{runs}"
                     f"{cross_txt} · {_n(s.get('entries', 0), 'entry', 'entries')}")
    if len(order) > 6:
        lines.append(f"… {len(order) - 6} more sector(s) in the audit")
    return lines


#: The order a leadership mark is chosen in: what stopped the name beats why it
#: was not looked at. ``follower`` is last because a name on this list was, by
#: construction, inside its sector's top five — it was trimmed on *other* candles.
_MARK_ORDER = ("earlier", "no_target", "cap", "oi", "late", "weak", "opposite", "follower")


def _leadership(st: dict) -> list[str]:
    """Which names each scanned sector kept putting forward, and what became of them."""
    rows = [r for r in audit.leadership_rows(st) if r.get("scanned")]
    if not rows:
        return []
    entered = {r.get("raw") or r.get("name")
               for r in (st.get("stats") or {}).get("closed") or []}
    entered |= set((st.get("positions") or {}).keys())
    blocked: dict[str, set[str]] = {}
    for kind, items in ((st.get("stats") or {}).get("rejected") or {}).items():
        for key, detail in (items or {}).items():
            # the key is "NAME@10:35" (or plain NAME) and the detail starts with
            # the name too — either way the first token is the stock
            name = str(key).split("@")[0].split()[0]
            blocked.setdefault(name, set()).add(kind)
            if detail:
                blocked.setdefault(str(detail).split()[0], set()).add(kind)

    def _mark(name: str) -> str:
        """The reason worth printing — not merely the first one recorded.

        A name can be trimmed as a follower on one candle and refused as
        ``earlier`` on another; the follower note is then the least informative
        thing about it. 20 Nov: RBLBANK made BANK's top five on 32 candles and
        was labelled "follower" (it had been trimmed on one of the others) while
        AXISBANK and INDUSINDBK — the two names the day was actually missed on —
        are the ones that should have carried the mark.
        """
        kinds = blocked.get(name) or set()
        for kind in _MARK_ORDER:
            if kind in kinds:
                return kind
        return ""
    per: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row.get("sector"), row.get("name"))
        slot = per.setdefault(key, {"n": 0, "first": 0, "sum_r": 0.0, "sum_s": 0.0,
                                    "sum_v": 0.0, "sum_t": 0.0, "top": 0})
        slot["n"] += 1
        slot["first"] = slot["first"] or row.get("rank_in_sector") == 1
        slot["top"] += 1 if row.get("rank_in_sector") == 1 else 0
        for src, dst in (("rank", "sum_r"), ("strength", "sum_s"), ("volume", "sum_v"),
                         ("structure", "sum_t")):
            try:
                slot[dst] += float(row.get(src) or 0)
            except (TypeError, ValueError):
                pass
    lines = ["<b>Top-5 leadership activity</b>"]
    by_sector: dict[str, list] = {}
    for (sector, name), slot in per.items():
        by_sector.setdefault(sector, []).append((slot, name))
    for sector, items in sorted(by_sector.items(),
                                key=lambda kv: -max(i[0]["n"] for i in kv[1]))[:4]:
        items.sort(key=lambda t: (-t[0]["n"], -(t[0]["sum_r"] / max(t[0]["n"], 1))))
        bits = []
        for slot, name in items[:5]:
            n = max(slot["n"], 1)
            mark = _mark(name)
            tag = ("✚ entered" if name in entered else f"⊘ {mark}" if mark else "")
            bits.append(f"{name} {slot['sum_r'] / n:.0f} "
                        f"(s{slot['sum_s'] / n:.0f}/v{slot['sum_v'] / n:.0f}/"
                        f"t{slot['sum_t'] / n:.0f}) ×{slot['n']}"
                        + (f" 1st×{slot['top']}" if slot["top"] else "")
                        + (f" <i>{tag}</i>" if tag else ""))
        lines.append(f"{sector}: " + " · ".join(bits))
    lines.append("rank (s strength / v volume / t structure) · ×N = times in the top 5 · "
                 "1st×N = times ranked first · every candle's table is in the audit")
    return lines


def _minus_5(stamp: str, candles: int) -> str:
    """``stamp`` shifted back ``candles`` 5-minute slots (an ISO entry timestamp)."""
    if not candles:
        return stamp
    try:
        from datetime import datetime, timedelta
        from .session import IST

        when = datetime.fromisoformat(str(stamp))
        if when.tzinfo is None:
            when = when.replace(tzinfo=IST)
        return (when - timedelta(minutes=5 * candles)).isoformat()
    except (TypeError, ValueError):
        return stamp


def _entry_ranks(st: dict) -> list[str]:
    """Where each entry's name sat in its sector's board on the candle it was taken.

    The question this answers is the one that matters most about a day's book —
    did the entries go to the leaders, or to whatever was left after the leaders
    were refused? 20 Nov: IT's four entries were ranked 5th and 8th+ (three were
    not even in the listed top eight) while the four names above them were all
    refused. One line per trade, then the count, because a single ratio every day
    is how the entry filter gets judged over weeks rather than per tape.
    """
    rows = audit.leadership_rows(st)
    if not rows:
        return []
    board: dict[tuple, dict[str, int]] = {}
    sizes: dict[tuple, int] = {}
    for row in rows:
        key = (row.get("bar"), row.get("sector"))
        board.setdefault(key, {}).setdefault(str(row.get("name")), len(board.get(key, {})) + 1)
        sizes[key] = max(sizes.get(key, 0), len(board[key]))
    trades: list[tuple[str, str, str]] = []
    for rec in (st.get("stats") or {}).get("closed") or []:
        trades.append((rec.get("raw") or "", rec.get("sector") or "", rec.get("entry_bar") or ""))
    for raw, pos in (st.get("positions") or {}).items():
        trades.append((raw, pos.get("sector") or "", pos.get("entry_bar") or ""))
    bits, inside, outside = [], 0, 0
    for name, sector, entry_bar in trades:
        if not name or not sector or not entry_bar:
            continue
        # The board the entry was judged on is the one ranked on the *previous*
        # completed candle, so look there when the entry candle itself has none.
        listed: dict[str, int] = {}
        size = 0
        for back in range(0, 3):
            bar = _hhmm(_minus_5(entry_bar, back))
            listed = board.get((bar, sector)) or {}
            size = sizes.get((bar, sector)) or 0
            if listed:
                break
        if name in listed:
            rank = listed[name]
            bits.append(f"{name} {rank} of {size}")
            inside += 1 if rank <= 5 else 0
            outside += 1 if rank > 5 else 0
        elif size:
            bits.append(f"{name} outside the {size} listed")
            outside += 1
        else:
            continue
    total = inside + outside
    if not total:
        return []
    lines = ["<b>Were the entries leaders?</b>", " · ".join(bits)]
    lines.append(f"{inside} of {_n(total, 'entry', 'entries')} went to a name ranked in its "
                 f"sector's top five when it was taken"
                 + (f" · {outside} were what was left after the leaders were refused"
                    if outside else ""))
    return lines


def _blocked(st: dict) -> list[str]:
    """Every signal that did not become an entry — with the rule and its reason."""
    x = st.get("stats") or {}
    rejected = x.get("rejected") or {}
    weak = x.get("weak") or {}
    trim = x.get("trim") or {}
    why = x.get("cancel_why") or {}
    cap = x.get("alert_capped") or {}
    if not (rejected or weak or trim or why or cap):
        return []
    from .pipeline import _REJECT_TEXT

    lines = ["<b>Every blocked signal</b>"]
    known = {kind for kind, _ in _REJECT_TEXT}
    total = sum(len(v or {}) for v in rejected.values())
    if total:
        lines.append(f"{_n(total, 'signal')} blocked — listed one by one below")
    for kind, text in _REJECT_TEXT:
        items = rejected.get(kind) or {}
        if not items:
            continue
        lines.append(f"<b>{len(items)} — {text}</b>")
        lines.extend(f"• {_clip(detail, 90)}" for detail in items.values())
    for kind, items in rejected.items():
        if kind in known or not items:
            continue
        lines.append(f"<b>{len(items)} — {kind}</b>")
        lines.extend(f"• {_clip(detail, 90)}" for detail in items.values())
    if weak:
        lines.append("Momentum gate: " + " · ".join(
            f"{k} {v}" for k, v in sorted(weak.items(), key=lambda kv: -kv[1])))
    if why:
        lines.append("Early alerts cancelled: "
                     + " · ".join(f"{_clip(k, 48)} {v}" for k, v in
                                  sorted(why.items(), key=lambda kv: -kv[1])[:6]))
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
                                key=lambda kv: -int(kv[1].get("dropped") or 0))[:6]:
            lines.append(f"  · {sector}: kept {v.get('kept', 0)} · dropped {v.get('dropped', 0)}")
    return lines


def _trades(st: dict) -> list[str]:
    """The trader's ledger: size, both prices, costs, rupees and R."""
    rows = ledger.closed_rows(st)
    opened = ledger.open_rows(st)
    books = ledger.totals(rows, opened)
    lines = ["<b>Trader ledger</b>"]
    if not rows and not opened:
        lines.append("No position was opened.")
        lines.append(f"Cost model: {costs.cost_bps():g} bps/side (min ₹{costs.cost_min():g} "
                     f"per order) on ₹{costs.trade_capital():,.0f} a trade")
        return lines
    for i, t in enumerate(rows, start=1):
        lines.append(
            f"{i}. {t['name']} {t['side']} {t['qty']}q @ {t['entry']:.2f}"
            f" ({_hhmm(t['entry_at'])}) → {t['exit']:.2f} ({t['exit_at']}) · "
            f"{t['why']}{', T' + str(t['targets_hit']) if t['targets_hit'] else ''}"
            f" · gross ₹{t['gross_rs']:,.0f} · costs ₹{t['costs_rs']:,.0f} · "
            f"<b>net {_rs(t['net_rs'])} ₹</b> · {t['gross_r']:+.1f}R gross / "
            f"{t['net_r']:+.1f}R net"
            + (f" · {t['hold_min']:.0f} min" if t.get("hold_min") is not None else ""))
    for t in opened:
        if t.get("mark") is None:
            lines.append(f"• {t['name']} {t['side']} {t['qty']}q @ {t['entry']:.2f} — "
                         f"still open, unmarked ({t['why']})")
            continue
        lines.append(
            f"• {t['name']} {t['side']} {t['qty']}q @ {t['entry']:.2f} → {t['mark']:.2f} "
            f"marked · gross ₹{t['gross_rs']:,.0f} · costs ₹{t['costs_rs']:,.0f} · "
            f"<b>net {_rs(t['net_rs'])} ₹</b> · {t['gross_r']:+.1f}R gross / "
            f"{t['net_r']:+.1f}R net · still open")
    lines.append(ledger.summary_line(books))
    if books.get("trades"):
        detail = (f"avg win ₹{books['avg_win_rs']:,.0f} · avg loss ₹{books['avg_loss_rs']:,.0f} · "
                  f"best {_rs(books['best_rs'])} · worst {_rs(books['worst_rs'])}")
        lines.append(detail)
        if books.get("risk_rs"):
            lines.append(f"risk deployed ₹{books['risk_rs']:,.0f} · return on risk "
                         f"{books['return_on_risk_pct']:+.1f}%"
                         + (f" · avg hold {books['avg_hold_min']:.0f} min"
                            if books.get("avg_hold_min") is not None else ""))
        if books.get("avg_mfe_r") is not None and books.get("avg_give_back_r") is not None:
            # The exit question, measured instead of felt: how much of the best
            # point of each trade the trail actually kept (next-session §4).
            kept = books["avg_mfe_r"] - books["avg_give_back_r"]
            lines.append(f"Best point vs kept: the trades ran {books['avg_mfe_r']:+.1f}R on "
                         f"average and the exits kept {kept:+.1f}R — "
                         f"{books['give_back_pct']:.0f}% of what they offered given back")
    lines.append(f"Cost model: {costs.cost_bps():g} bps/side (min ₹{costs.cost_min():g} per "
                 f"order) on ₹{costs.trade_capital():,.0f} a trade — COST_BPS / COST_MIN / "
                 f"TRADE_CAPITAL")
    return lines


def _health(st: dict) -> list[str]:
    """Data and delivery problems — the day's silent failures."""
    x = st.get("stats") or {}
    from .pipeline import (_summary_catchup, _summary_early_skip, _summary_mirror,
                           _summary_no_index, _summary_pending, _summary_scan_lag)

    lines = ["<b>Data diagnostics</b>"]
    for text in (_summary_no_index(x), _summary_scan_lag(x), _summary_pending(st),
                 _summary_catchup(st), _summary_early_skip(x), _summary_mirror()):
        if text:
            lines.append("• " + text)
    lag_n = int(x.get("lag_n") or 0)
    if lag_n:
        avg = float(x.get("lag_sum") or 0.0) / lag_n
        lines.append(f"• alerts went out {avg:.0f}s after the close on average "
                     f"(slowest {float(x.get('lag_max') or 0):.0f}s, {lag_n} candle(s))")
    if x.get("feed_behind"):
        lines.append(f"• the 1m feed lagged past the close on {x['feed_behind']} candle(s)")
    if x.get("no_bar"):
        names = list(x["no_bar"])
        lines.append(f"• candles the feed never published: {len(names)} — "
                     + ", ".join(names[:8]) + (" …" if len(names) > 8 else "")
                     + " (no bar to judge, so no entry was possible)")
    if x.get("unjudged"):
        names = list(x["unjudged"])
        lines.append(f"• candles never judged: {len(names)} — "
                     + ", ".join(names[:8]) + (" …" if len(names) > 8 else ""))
    if x.get("checked"):
        lines.append(f"• open trades checked against the feed: {len(x['checked'])}")
    if x.get("alert_capped_total"):
        lines.append(f"• alerts held back by the per-candle cap: {x['alert_capped_total']}")
    if len(lines) == 1:
        lines.append("No feed, scan or delivery problems recorded.")
    return lines


def _rules() -> list[str]:
    """The knobs in force — the context every number above is read against."""
    from . import knobs as knob_mod
    from .pipeline import (entry_min_rank, entry_rank_band, late_max_minutes,
                           max_entries_per_pick, sector_top_n)
    from .trade import min_target_r

    # The attachment and message knobs belong in the audit, not in the rules a
    # reader is asked to judge the day against.
    infra = {"AUDIT_DIR", "EOD_MAX_CHARS", "EOD_AUDIT", "EOD_REPORT"}
    diff = [(k, v) for k, v in knob_mod.differing().items() if k not in infra]
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
    skips = x.get("catchup_skip") or {}
    stale = int(skips.get("old") or 0)
    if stale and stale > 3 * max(caught, 1):
        out.append(f"⚠ {_n(stale, 'leader catch-up')} refused as older than the window — "
                   f"a sector picked hours after its leaders fired cannot buy them "
                   f"(PICK_CATCHUP re-opens the window on a pick change)")
    books = ledger.totals(ledger.closed_rows(st), ledger.open_rows(st))
    if books.get("trades") and books.get("costs_rs") and books.get("gross_rs"):
        if books["costs_rs"] > 0.35 * abs(books["gross_rs"]):
            out.append(f"⚠ costs ate ₹{books['costs_rs']:,.0f} of ₹{abs(books['gross_rs']):,.0f} "
                       f"gross — fewer, bigger trades (or a tighter COST_BPS) would have "
                       f"kept more")
        if books.get("profit_factor") is not None and books["profit_factor"] < 1:
            out.append(f"⚠ profit factor {books['profit_factor']} — the losses outweighed "
                       f"the wins; the entries that worked are named above")
    if books.get("max_dd_rs"):
        out.append(f"⚠ the book fell ₹{books['max_dd_rs']:,.0f} from its peak — "
                   f"the exits that caused it are in the ledger")
    if not out:
        out.append("Nothing stands out: the refused names were refused by the rules "
                   "above, at the counts shown.")
    return ["<b>What to look at tomorrow</b>"] + [f"• {line}" for line in out]


# ── the report ──────────────────────────────────────────────────────────────
def format_eod(st: dict, *, bot: str = "5m") -> str:
    """The whole day, in one (long) text. Split it with :func:`chunks` to send."""
    blocks = [_head(st, bot), _day_shape(st), _timeline(st), _persistence(st),
              _leadership(st), _entry_ranks(st), _blocked(st), _trades(st), _health(st),
              _rules(),
              _observations(st)]
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


def send_eod(st: dict, *, bot: str = "5m", send=None, send_doc=None,
             dry_run: bool = False) -> bool:
    """Send every numbered message and both attachments. ``True`` only if all arrived.

    ``send_doc`` defaults to :func:`telegram.send_document` with the right bot;
    a caller that only wants the text (a test, a dry run) can pass a stub.
    """
    send = send or telegram.send
    if send_doc is None:
        target = 3 if bot == "1m" else 1
        send_doc = lambda path, caption="": telegram.send_document(       # noqa: E731
            path, caption, bot=target, dry_run=dry_run)
    parts = chunks(format_eod(st, bot=bot))
    ok = True
    for i, part in enumerate(parts, 1):
        if not send(part, dry_run=dry_run):
            log.error("end-of-day report part %d/%d was not delivered", i, len(parts))
            ok = False
    if audit_enabled():
        try:
            json_path, xlsx_path, payload = audit.write(st, bot=bot)
        except Exception:
            log.exception("could not write the audit files — the text report above stands")
        else:
            books = payload.get("books") or {}
            caption = (f"Audit {st.get('session')} · {books.get('trades', 0)} trade(s) · "
                       f"net ₹{books.get('net_rs', 0):,.0f}")
            extra = [Path(p) for p in (payload.get("board") or {}).get("files") or []]
            for path in (json_path, xlsx_path, *extra):
                if not send_doc(str(path), caption):
                    log.error("audit attachment %s was not delivered", path)
                    ok = False
    if ok:
        log.info("end-of-day report sent: %d part(s) + attachments", len(parts))
    return ok
