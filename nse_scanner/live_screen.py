"""Bot 4's own loop — the screen runs on its own schedule, not inside the 5m scan.

Why this module exists
---------------------
The screen was first wired into ``pipeline.run_once``: it borrowed the board the 5m
scan had just scored. That was cheap and consistent, and it had one fatal property —
**bot 4 died with bot 1**. If the 5m job hit its budget, crashed, or was never
started, the short list went silent that day, and a silent scanner is
indistinguishable from a day with no setups.

So bot 4 now runs its own process, on its own clock, with its own state, its own
Telegram channel and its own workflow. Bots 1/2/3 are untouched: nothing here reads
bot 1's ledger (`state/scan_state.json`), nothing here sends to bots 1/2/3, and no
5m/1m behaviour changes. Two processes, two ledgers, one shared set of formulas.

The one thing it must not duplicate is *arithmetic*
-----------------------------------------------
A second copy of a formula is a second truth. So this module owns no formula: it
calls the same functions the 5m scan calls, in this order —

    market.run_market(...)      -> the index side, per-sector persistence/rs/intra
    board._score(...)           -> every name's strength/volume/structure/lead
    screen.candidates/consider  -> the gates, the ranking, the cards
    trade.plan_at(...)          -> entry, stop, T1-T3

— and, like the board, it fetches names **only for the sectors that pass the sector
gates**. On a day where nothing qualifies that is 13 index fetches per candle and no
stock fetches at all; on a broad day it is two or three sectors.

What it does per candle
-----------------------
* ``EARLY_ALERT_SECONDS`` before the close → ``early_pass`` builds the forming candle
  from 1m bars (``data.provisional_5m``) and sends a **PROVISIONAL** card;
* at the close (+ a poll for the published bar) → ``close_pass`` judges the closed
  candle and sends **CONFIRMED** / **CANCELLED** for the card it settled;
* 15:33 → the audit PDF, once.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, time as clock_time, timedelta

from . import board, data, market, screen, screen_pdf, telegram
from .session import is_weekday, next_5m_close, now_ist, to_ist

log = logging.getLogger("nse_scanner")

BAR_MINUTES = 5
#: How long to wait for Yahoo to publish the bar that just closed. The loop has a
#: whole candle of slack, but a card sent on the *previous* bar would be a lie, so
#: the wait is bounded and a missing bar is reported, never guessed.
PUBLISH_WAIT_S = float(os.environ.get("SCREEN_PUBLISH_WAIT_S", "120"))


def name_period() -> str:
    """Yahoo period for the names' 5m frames (``SCREEN_PERIOD``, 7d).

    7d on purpose, not the board's 1d: EMA9 and the previous session's pivots both
    have to be seeded for a stop and a T1-T3 to mean what they mean in every other
    part of this repository. The board can use 1d because it only ranks names; bot 4
    puts a stop on them.
    """
    return os.environ.get("SCREEN_PERIOD", "7d").strip() or "7d"


# ── the market side, computed here ───────────────────────────────────────────
def index_frames(uni: dict, *, last=None) -> tuple[dict, dict]:
    """5m and daily frames for the Nifty, the VIX and every sector index.

    Trimmed to ``last`` before anything is scored, so the decision bar is the candle
    being judged and never a later one: a market read one candle ahead would make
    every setup look like it had a perfect sector behind it.
    """
    idx = {uni["nifty"]: "nifty", uni["vix"]: "vix"}
    for code, spec in (uni.get("sectors") or {}).items():
        sym = spec.get("index")
        if sym:
            idx[sym] = code
    syms = list(idx)
    m5 = data.fetch_many(syms, "7d", "5m")
    d1 = data.fetch_daily(syms, os.environ.get("YF_PERIOD_1D", "90d"))
    if last is not None:
        m5 = {s: (df.loc[df.index <= last] if df is not None and not df.empty else df)
              for s, df in m5.items()}
        d1 = {s: (df.loc[df.index <= last] if df is not None and not df.empty else df)
              for s, df in d1.items()}
    return m5, d1


def analyse(uni: dict, *, last=None) -> tuple:
    """``(md, m5, d1)`` — the market read for the candle ending at ``last``."""
    m5, d1 = index_frames(uni, last=last)
    sectors_5 = {code: m5.get(spec.get("index"))
                 for code, spec in (uni.get("sectors") or {}).items()}
    sectors_d = {code: d1.get(spec.get("index"))
                 for code, spec in (uni.get("sectors") or {}).items()}
    md = market.run_market(
        m5.get(uni["nifty"]), d1.get(uni["nifty"]),
        m5.get(uni["vix"]), d1.get(uni["vix"]),
        sectors_5, sectors_d,
    )
    return md, m5, d1


def _row(md) -> dict:
    """The board's timeline-cell shape, from this process's own market read.

    ``board._score`` reads ``[bull, bear, day%, intra%, qualified]`` per sector; that
    is exactly what ``MarketDecision.sectors`` holds, so bot 4 can feed the board's
    own scorer without touching bot 1's ledger or re-deriving anything.
    """
    return {"sec": {name: [s.get("bull"), s.get("bear"), s.get("day_ret"),
                           s.get("intra_ret"), 1 if s.get("qualified") else 0]
                    for name, s in (getattr(md, "sectors", {}) or {}).items()}}


def _cell(md, code: str, spec: dict) -> dict:
    """One sector in the board's cell shape, from this process's market read."""
    snap = (getattr(md, "sectors", {}) or {}).get(code) or {}
    side = int(snap.get("side") or 1)
    persist = snap.get("persist_bull" if side == 1 else "persist_bear")
    return {"side": side, "persist": round(float(persist or 0)),
            "bull": round(float(snap.get("bull") or 0)),
            "bear": round(float(snap.get("bear") or 0)),
            "day": round(float(snap.get("day_ret") or 0), 2),
            "intra": round(float(snap.get("intra_ret") or 0), 2),
            "gate": bool(snap.get("qualified")),
            "pick": code == getattr(md, "pick_sector", ""),
            "n": list(spec.get("stocks") or [])}


def skeleton(uni: dict, md, st4: dict) -> dict:
    """Every sector with its names listed but nothing scored — for the sector gates.

    ``screen.ready_sectors`` decides on sector-level numbers only, so this is enough
    to know which books are worth fetching at all. The alternative — fetching 180
    names every five minutes to discover that no sector qualifies — is what makes a
    second scanner expensive for no reason.
    """
    return {"b": getattr(md, "bar_time", ""), "tilt": int(md.dir_state or 0),
            "pick": md.pick_sector or "",
            "sectors": {code: _cell(md, code, spec)
                        for code, spec in (uni.get("sectors") or {}).items()}}


#: How many qualifying sectors' names are fetched per pass. The gates usually pass
#: none or one; a broad tape can pass eight. Every extra book is fifteen symbols to
#: download, so the two passes take the strongest sectors first — the same 3 the 5m
#: loop's early pass fetches, and 6 at the close where there is a candle of slack.
#: The cap is the one place bot 4 can differ from bot 1: on a tape with more than
#: six qualifying books, a name in the seventh can surface in bot 1 and not here.
EARLY_SECTORS = 3
CLOSE_SECTORS = 6


def sectors_to_fetch(uni: dict, md, st4: dict, *, limit: int | None = None) -> list[str]:
    """Which sectors' names are worth a fetch this candle, strongest first."""
    slots = skeleton(uni, md, st4)
    slopes = screen._slopes(st4, slots)
    return [code for code, _, _ in screen.ready_sectors(slots, md, slopes=slopes,
                                                       limit=limit)]


def payload_for(uni: dict, md, frames: dict, last) -> dict:
    """The board's own payload shape, scored by the board's own scorer."""
    secs, names, stale = board._score(frames, uni, _row(md), last)
    if stale:
        log.debug("bot 4: %d name frame(s) had not published %s", stale,
                  to_ist(last).strftime("%H:%M"))
    return {"b": to_ist(last).strftime("%H:%M"), "scan": to_ist(last).strftime("%H:%M"),
            "tilt": int(md.dir_state or 0), "pick": md.pick_sector or "", "sectors": secs}


def name_frames(uni: dict, sectors: list[str], *, last=None) -> dict:
    """5m frames for the names of ``sectors`` — and only those."""
    tickers = [data.yahoo_stock(raw) for code in sectors
               for raw in ((uni.get("sectors") or {}).get(code) or {}).get("stocks") or []]
    frames = data.fetch_many(tickers, name_period(), "5m")
    if last is not None:
        frames = {s: (df.loc[df.index <= last] if df is not None and not df.empty else df)
                  for s, df in frames.items()}
    return frames


# ── the two passes ───────────────────────────────────────────────────────────
def last_closed(now: datetime | None = None) -> datetime:
    """The most recent candle whose close has passed (its open time)."""
    now = now or now_ist()
    return (now.replace(minute=(now.minute // BAR_MINUTES) * BAR_MINUTES, second=0,
                        microsecond=0) - timedelta(minutes=BAR_MINUTES))


def _nifty_last(uni: dict):
    """The newest bar on the Nifty's feed — one symbol, so polling stays cheap."""
    try:
        df = data.fetch_many([uni["nifty"]], "7d", "5m").get(uni["nifty"])
    except Exception:                       # noqa: BLE001 — a poll may fail, retry
        return None
    if df is None or getattr(df, "empty", True):
        return None
    try:
        return to_ist(df.index[-1])
    except (TypeError, ValueError, IndexError):
        return None


def wait_for_bar(uni: dict, last, *, deadline_s: float | None = None) -> bool:
    """Poll until the index feed carries the bar ``last``. ``False`` = it never came.

    Judging the previous candle because Yahoo was slow is the exact failure this
    scanner exists to avoid, so a missing bar is reported and skipped — the PDF's
    missed-candle count is where it shows up.
    """
    deadline = now_ist() + timedelta(seconds=deadline_s if deadline_s is not None
                                      else PUBLISH_WAIT_S)
    want = to_ist(last)
    while True:
        got = _nifty_last(uni)
        if got is not None and got == want:
            return True
        if now_ist() >= deadline:
            log.warning("bot 4: the %s bar was not published before the deadline — "
                        "this candle is skipped and counted as missed, never judged on "
                        "an older bar", to_ist(last).strftime("%H:%M"))
            return False
        time.sleep(5)


def close_pass(*, send=None, save=None, now: datetime | None = None,
               last=None, uni: dict | None = None, md=None, frames: dict | None = None) -> dict:
    """Judge the closed candle and settle the provisional card it named.

    ``md``/``frames``/``uni`` are for a caller that already has them (the tests, and
    the CLI's single pass); by default everything is fetched here, which is what the
    loop does.
    """
    send = send or telegram.send_bot4
    save = save or screen.save
    now = now or now_ist()
    last = last or last_closed(now)
    try:
        st4 = screen.load()
        if md is None or frames is None:
            uni = uni or data.load_universe()
            md, _, _ = analyse(uni, last=last)
            wanted = sectors_to_fetch(uni, md, st4, limit=CLOSE_SECTORS)
            frames = name_frames(uni, wanted) if wanted else {}
        payload = payload_for(uni, md, frames, last)
        surfaced = screen.consider(st4, payload, frames, md, last, send=send,
                                   save_state=save, now=now)
        # `consider` passes dry_run itself, but these update lines are sent here:
        # without it, DRY_RUN=1 would still post CONFIRMED/CANCELLED to bot 4.
        dry = screen._dry()
        for text in screen.updates(st4):
            if (send(text, dry_run=dry) if send is telegram.send_bot4 else send(text)):
                save(st4)
        return {"bar": payload.get("b"), "candidates": len(surfaced),
                "surfaced": surfaced, "setups": len(st4.get("setups") or [])}
    except Exception:                       # noqa: BLE001 — never take the loop down
        log.exception("bot 4 close pass failed for %s",
                      to_ist(last).strftime("%H:%M") if last is not None else "?")
        return {"bar": None, "candidates": 0, "surfaced": [], "setups": 0}


def early_pass(forming, *, send=None, now: datetime | None = None) -> list[dict]:
    """Provisional cards on the still-forming candle, ``EARLY_ALERT_SECONDS`` early."""
    send = send or telegram.send_bot4
    now = now or now_ist()
    if not screen.early_enabled():
        return []
    st4 = screen.load()
    try:
        uni = data.load_universe()
        # The sector read is the last CLOSED candle's: persistence is a slow measure,
        # and the forming candle has no published sector index row to read.
        prev = to_ist(forming) - timedelta(minutes=BAR_MINUTES)
        md, _, _ = analyse(uni, last=prev)
        wanted = sectors_to_fetch(uni, md, st4, limit=EARLY_SECTORS)
        if not wanted:
            log.info("bot 4 early %s: no sector passes the gates — nothing to project",
                     to_ist(forming).strftime("%H:%M"))
            return []
        tickers = {data.yahoo_stock(raw): raw for code in wanted
                   for raw in ((uni.get("sectors") or {}).get(code) or {}).get("stocks") or []}
        sm5 = data.fetch_many(tickers, name_period(), "5m")
        sm1 = data.fetch_many(tickers, "7d", "1m")
        frames = {}
        for sym in tickers:
            df = data.provisional_5m(sm5.get(sym), sm1.get(sym), forming, now,
                                     float(os.environ.get("EARLY_MAX_STALE_SECONDS", "100")))
            if df is not None:
                frames[sym] = df
        if not frames:
            log.warning("bot 4 early %s: no live 1m data for %d name(s) — no provisional "
                        "card (a delayed feed is not projected)",
                        to_ist(forming).strftime("%H:%M"), len(tickers))
            return []
        payload = payload_for(uni, md, frames, forming)
        out = screen.provisional(st4, payload, frames, md, forming, send=send, now=now)
        log.info("bot 4 early %s: %d sector(s), %d/%d name(s) with live 1m data, "
                 "%d provisional card(s)", to_ist(forming).strftime("%H:%M"), len(wanted),
                 len(frames), len(tickers), len(out))
        return out
    except Exception:                       # noqa: BLE001
        log.exception("bot 4 early pass failed for %s", to_ist(forming).strftime("%H:%M"))
        return []


def end_of_day(*, dry_run: bool | None = None) -> bool:
    """Send the audit PDF once per session. ``True`` = it is reported."""
    st4 = screen.load()
    if not st4.get("session") or st4.get("reported"):
        return bool(st4.get("reported"))
    dry = screen._dry() if dry_run is None else dry_run
    out = screen_pdf.report(st4, dry_run=dry)
    if out.get("sent"):
        st4["reported"] = True
        screen.save(st4)
        log.info("bot 4 audit sent: %s", out.get("path"))
        return True
    log.error("bot 4 audit PDF was NOT delivered — %s (re-send: python -m nse_scanner "
              "screen-pdf)", out.get("path"))
    return False


# ── the loop ─────────────────────────────────────────────────────────────────
def _lead() -> int:
    """Seconds before a 5m close at which the provisional pass runs."""
    try:
        return max(0, min(240, int(os.environ.get("EARLY_ALERT_SECONDS", "60"))))
    except ValueError:
        return 60


def _grace() -> int:
    try:
        return max(0, min(120, int(os.environ.get("POLL_GRACE_SECONDS", "10"))))
    except ValueError:
        return 10


def _sleep_until(target: datetime) -> None:
    while True:
        left = (target - now_ist()).total_seconds()
        if left <= 0:
            return
        time.sleep(min(left, 30))


def end_of_session(*, now: datetime | None = None) -> bool:
    """At 15:33: send the audit — unless the 5m scan is alive and sends it itself."""
    do, why = should_screen(now=now)
    if not do:
        log.info("bot 4 end-of-day: %s — the 5m scan sends the audit", why)
        return False
    return end_of_day()


def _announce(why: str) -> None:
    """One line to bot 4 the first time it screens a session — and why it is on."""
    st4 = screen.load()
    if st4.get("announced"):
        return
    if telegram.send_bot4(
            f"🎯 Bot 4 screen is live on its own loop — up to {screen.max_day()} setups "
            f"a day, provisional {_lead()}s before each close. Not an entry signal: "
            f"you choose.\n{why}", dry_run=screen._dry()):
        st4["announced"] = True
        screen.save(st4)


def live_loop_screen(*, max_minutes: float | None = None,
                     exit_at_close: bool = False) -> str:
    """Bot 4's own clock. Returns ``"budget"``, ``"closed"`` or ``"holiday"``.

    Same handover contract as the 5m loop (``max_minutes`` + ``exit_at_close``) so a
    GitHub job can run it for 170 minutes and hand over. Unlike the 5m loop it does
    not depend on that job: this workflow's own crons restart it if it dies, and its
    only link to the 5m scan is ``should_screen()`` — it screens when that loop is
    not running.
    """
    started = now_ist()
    # Only a closed session ends the run: a pre-open start must wait for 09:14, not
    # report "closed" and leave the day with no screen at all.
    if exit_at_close and (not is_weekday(started) or started.time() >= clock_time(15, 33)):
        log.info("outside the NSE session (%s IST) — nothing to do",
                 started.strftime("%a %H:%M"))
        return "closed"
    data.enable_daily_cache()
    from .knobs import describe
    for line in describe():
        log.info("%s", line)
    log.info("bot 4 loop IST: screen %s · provisional %ss before each close · %s "
             "setups/day · failover %s · channel %s",
             "on" if screen.screen_enabled() else "off", _lead(), screen.max_day(),
             "on (stands down when the 5m scan runs)" if failover_enabled()
             else "OFF — always screens on its own read",
             "configured" if all(telegram.fourth_config_from_env()) else "MISSING")
    closed_streak = 0
    while True:
        now = now_ist()
        # `is not None`, not truthiness: --max-minutes 0 (a smoke run) must return,
        # and a falsy 0 here would silently mean "loop for ever".
        if max_minutes is not None and (now - started).total_seconds() / 60.0 >= max_minutes:
            log.info("bot 4: runtime budget of %.0f min reached — handing over", max_minutes)
            return "budget"
        if not is_weekday(now):
            if exit_at_close:
                return "closed"
            time.sleep(1800)
            continue
        t = now.time()
        if t < clock_time(9, 14):
            _sleep_until(now.replace(hour=9, minute=14, second=0, microsecond=0))
            continue
        if t >= clock_time(15, 33):
            try:
                end_of_session()
            except Exception:               # noqa: BLE001
                log.exception("bot 4 end-of-day failed")
            if exit_at_close:
                return "closed"
            log.info("bot 4: session over, sleep until tomorrow")
            time.sleep(1800)
            continue
        nxt = next_5m_close(now)
        forming = nxt - timedelta(minutes=BAR_MINUTES)
        in_session = clock_time(9, 15) <= forming.time() <= clock_time(15, 25)
        lead = _lead()
        early_at = nxt - timedelta(seconds=lead)
        if lead and in_session and now_ist() < early_at + timedelta(seconds=min(20, lead / 2)):
            log.info("bot 4: sleep until %s (provisional pass for %s)",
                     early_at.strftime("%H:%M:%S"), forming.strftime("%H:%M"))
            _sleep_until(early_at)
            do, why = should_screen()
            if do:
                _announce(why)
                early_pass(forming)
            else:
                log.info("bot 4 provisional %s: %s", forming.strftime("%H:%M"), why)
        log.info("bot 4: sleep until %s + %ss (close pass for %s)",
                 nxt.strftime("%H:%M"), _grace(), forming.strftime("%H:%M"))
        _sleep_until(nxt + timedelta(seconds=_grace()))
        if not in_session:
            continue
        do, why = should_screen()
        if not do:
            log.info("bot 4 close %s: %s", forming.strftime("%H:%M"), why)
            continue
        _announce(why)
        uni = data.load_universe()
        if not wait_for_bar(uni, forming):
            closed_streak += 1
            if closed_streak >= 3 and forming.time() >= clock_time(9, 30):
                log.info("bot 4: no bar published for three candles — treating %s as a "
                         "holiday", forming.date())
                if exit_at_close:
                    return "holiday"
            continue
        closed_streak = 0
        try:
            close_pass(now=now_ist(), last=forming, uni=uni)
        except Exception:                   # noqa: BLE001
            log.exception("bot 4 close pass failed")


# ── failover: bot 4 stands down while the 5m scan is alive ───────────────────
# Two processes measuring persistence will mostly agree and occasionally not — a
# bar in flight, a rounded digit. Neither is wrong, but a short list that quotes
# two different numbers for the same candle is worse than a short list that quotes
# one, so bot 4 does not compete: it screens only when the 5m loop is not running.
# That is the day it matters — the day bot 1 fell over.
ALIVE_WORKFLOW = "live-scan.yml"


def failover_enabled() -> bool:
    """``SCREEN_FAILOVER`` (1): stand down while the 5m scan is running.

    ``0`` makes bot 4 always screen on its own read — independent, at the price of
    two processes quoting possibly-different persistence on the same bar.
    """
    return os.environ.get("SCREEN_FAILOVER", "1").strip() != "0"


def _run_age_min(run: dict, now: datetime) -> float:
    stamp = run.get("created_at") or run.get("run_started_at") or ""
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return 1e9
    return (now - when.astimezone(when.tzinfo)).total_seconds() / 60.0


def five_minute_alive(*, now: datetime | None = None,
                      max_queue_min: float = 10.0) -> tuple[bool, str]:
    """Is the 5m scan screening this candle? ``(alive, reason)``.

    Asked over the REST API rather than through a shared file: the two workflows run
    on different machines, and a file would have to be carried between them by an
    artifact or a cache — both of which are written at the *end* of a run, i.e. up to
    three hours after the answer changed.

    Unknown is not alive: with no token, or an API that does not answer, bot 4
    screens. A silent screen is the failure this whole module exists to prevent;
    a duplicated card on a name that already surfaced is caught by the ledger's
    once-a-day guard.
    """
    now = now or now_ist()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if not repo or not token:
        return False, "no GITHUB_REPOSITORY/GITHUB_TOKEN — cannot see the 5m loop, so screening"
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/"
           f"{ALIVE_WORKFLOW}/runs?per_page=20")
    try:
        import json
        import urllib.request
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "nse-bot4-screen",
        })
        with urllib.request.urlopen(req, timeout=8) as fh:
            runs = json.loads(fh.read().decode("utf-8")).get("workflow_runs") or []
    except Exception as exc:                # noqa: BLE001
        return False, f"could not list {ALIVE_WORKFLOW} runs ({exc}) — so screening"
    for run in runs:
        status = str(run.get("status") or "")
        age = _run_age_min(run, now)
        if status == "in_progress":
            return True, (f"5m scan run {run.get('id')} is in progress "
                          f"(started {age:.0f} min ago)")
        if status in ("queued", "waiting", "pending") and age <= max_queue_min:
            return True, (f"5m scan run {run.get('id')} is queued "
                          f"({age:.0f} min) — waiting for it")
    if runs:
        newest = runs[0]
        return False, (f"no 5m scan run is active (newest: {newest.get('status')}, "
                       f"{_run_age_min(newest, now):.0f} min old) — screening this candle")
    return False, f"no {ALIVE_WORKFLOW} runs on record — screening this candle"


def should_screen(*, now: datetime | None = None) -> tuple[bool, str]:
    """``(do_it, why)`` — the failover decision for this candle, logged either way."""
    if not screen.screen_enabled():
        return False, "SCREEN_BOT=0 — the screen is switched off"
    if not failover_enabled():
        return True, "SCREEN_FAILOVER=0 — bot 4 always screens on its own read"
    alive, why = five_minute_alive(now=now)
    if alive:
        return False, f"{why}; bot 4 stands down"
    return True, why
