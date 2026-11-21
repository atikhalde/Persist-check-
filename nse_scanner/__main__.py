"""CLI

  python -m nse_scanner once
  python -m nse_scanner live
  python -m nse_scanner live-1m          # 1m projected scan, Telegram bot 3
  python -m nse_scanner once-1m          # one projected pass (diagnostic)
  python -m nse_scanner doctor
  python -m nse_scanner once --dry-run --sector IT
  python -m nse_scanner early-check --date YYYY-MM-DD --sector IT
  python -m nse_scanner screen            # bot 4: screen the last closed candle once
  python -m nse_scanner screen-pdf        # re-send bot 4's audit PDF for today
  python -m nse_scanner bot4-check        # prove bot 4's secrets work, end to end
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


def _load_env() -> None:
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if not candidate.exists():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)
        break


def _setup_log(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    _load_env()
    p = argparse.ArgumentParser(prog="nse_scanner", description="NIFTY sector + 5m stock Telegram scanner")
    p.add_argument("cmd", choices=["once", "live", "once-1m", "live-1m", "doctor", "backtest",
                                    "early-check", "dual", "live-dual", "sector-follow",
                                    "live-sector-follow", "screen", "screen-pdf",
                                    "bot4-check"],
                   help="once | live | once-1m | live-1m | doctor | backtest | early-check | "
                        "dual | live-dual | sector-follow | live-sector-follow | screen | screen-pdf")
    p.add_argument("--dry-run", action="store_true", help="print alerts, do not call Telegram")
    p.add_argument("--sector", default=None, help="force sector code (AUTO, IT, BANK, …)")
    p.add_argument("--backfill", action="store_true", help="diagnostic last-session scan; requires --dry-run")
    p.add_argument("--wait-for-bar", action="store_true", help="once: retry the just-closed bar until fresh data arrives or the 3m deadline")
    p.add_argument("--max-minutes", type=float, default=None,
                   help="live: return after this many minutes (job time limits); prints handoff=true")
    p.add_argument("--exit-at-close", action="store_true",
                   help="live: exit when the NSE session is over instead of sleeping until tomorrow")
    p.add_argument("--lead", type=int, default=60,
                   help="early-check: seconds before the 5m close at which the forming candle is judged")
    p.add_argument("--date", default="2026-10-01", help="backtest session YYYY-MM-DD (IST)")
    p.add_argument("--session", default=None,
                   help="screen-pdf: only report this session YYYY-MM-DD")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = p.parse_args(argv)
    if args.backfill and not args.dry_run:
        p.error("--backfill requires --dry-run; old signals must never reach Telegram")
    if args.wait_for_bar and args.cmd != "once":
        p.error("--wait-for-bar is only valid with once")
    if args.backfill and args.cmd in ("once-1m", "live-1m"):
        p.error("--backfill replays a past 5m session; the 1m scan is live-only "
                "(use: once --backfill --dry-run)")
    if args.wait_for_bar and args.backfill:
        p.error("--wait-for-bar cannot be combined with --backfill")
    _setup_log(args.log_level)
    if args.dry_run:
        os.environ["DRY_RUN"] = "1"

    if args.cmd == "doctor":
        return _doctor()
    if args.cmd == "once":
        from .pipeline import run_once, run_until_ready

        out = (run_until_ready(force_sector=args.sector) if args.wait_for_bar else
               run_once(force_sector=args.sector, backfill=args.backfill))
        print(
            f"{out['bar']}  {out['market']}  pick={out['pick']}  {out['action']}  new_alerts={len(out['signals'])}"
        )
        for s in out["signals"]:
            print(f"  {s['name']:12} {s['symbol']:12} {s['side']:4} {s['price']} @ {s['time']}")
        return 0
    if args.cmd == "dual":
        from .oi_live import run_once_dual

        out = run_once_dual(
            force_sector=args.sector,
            oi_source=os.environ.get("OI_SOURCE"),
            oi_file=os.environ.get("OI_FILE"),
        )
        orig = out["original"]
        oi = out["oi"]
        print(
            f"{orig['bar']}  {orig['market']}  pick={orig['pick']}  {orig['action']}  "
            f"orig_alerts={len(orig['signals'])}  oi_alerts={len(oi['signals'])}"
        )
        for s in orig["signals"]:
            print(f"  ORIG  {s['name']:12} {s['symbol']:12} {s['side']:4} {s['price']} @ {s['time']}")
        for s in oi["signals"]:
            print(f"  OI    {s['name']:12} {s['symbol']:12} {s['side']:4} {s['price']} @ {s['time']}  OI={s.get('oi_regime')}")
        return 0
    if args.cmd == "live-dual":
        from .oi_live import live_loop_dual

        live_loop_dual(oi_source=os.environ.get("OI_SOURCE"), oi_file=os.environ.get("OI_FILE"))
        return 0
    if args.cmd == "sector-follow":
        from .sector_follow import run_once_sector_follow

        out = run_once_sector_follow(force_sector=args.sector)
        print(
            f"{out['bar']}  {out['market']}  pick={out['pick']}  {out['action']}  "
            f"alerts={len(out['signals'])}"
        )
        for s in out["signals"]:
            print(f"  SF    {s['name']:12} {s['symbol']:12} {s['side']:4} {s['price']} @ {s['time']}")
        return 0
    if args.cmd == "live-sector-follow":
        from .sector_follow import live_loop_sector_follow

        live_loop_sector_follow()
        return 0
    if args.cmd == "once-1m":
        from datetime import timedelta

        from .scan1m import _bar_open, confirm_pass, run_minute
        from .session import now_ist

        now = now_ist()
        bar = _bar_open(now)
        minute = int((now - bar).total_seconds() // 60)
        out = (confirm_pass(bar - timedelta(minutes=5), now=now, force_sector=args.sector)
               if minute == 0 else run_minute(bar, now=now, force_sector=args.sector))
        kind = "confirmed" if minute == 0 else "projected"
        # After the alerts, never before them: the board is a record, and a record
        # must not stand between a signal and its deadline.
        from .board import sweep
        sweep(now=now)
        print(f"{bar:%Y-%m-%d %H:%M}  minute {minute}  {kind} alerts={len(out)}")
        for s in out:
            print(f"  {s['name']:12} {s['symbol']:12} {s['side']:4} {s['price']} @ {s['time']}")
        return 0
    if args.cmd == "bot4-check":
        return _bot4_check(dry_run=args.dry_run)
    if args.cmd == "screen":
        out = run_screen_once()
        if out["bar"]:
            print(f"{out['bar']}  qualified {out['candidates']}  surfaced "
                  f"{len(out['surfaced'])}  ledger {out['setups']} setup(s)")
            for s_ in out["surfaced"]:
                print(f"  {s_['side']:4} {s_['name']:12} {s_['sector']:8} "
                      f"entry {s_['entry']} sl {s_['sl']} score {s_['score']}  (not an entry)")
        return 0 if out["bar"] else 1
    if args.cmd == "screen-pdf":
        from .screen_pdf import _cli as _screen_pdf_cli

        return _screen_pdf_cli(args.session)
    if args.cmd == "early-check":
        from datetime import date as _date
        from .early_check import render, run

        d = _date.fromisoformat(args.date)
        print(render(run(d, args.sector, args.lead), d, args.lead))
        return 0
    if args.cmd == "backtest":
        from datetime import date as _date
        from pathlib import Path
        from .backtest import (divergence_rows, render_divergence, replay_session,
                               render_report)

        d = _date.fromisoformat(args.date)
        res = replay_session(d)
        text = render_report(res) + "\n\n" + render_divergence(
            divergence_rows(res.get("path") or []), args.date)
        outp = Path("state") / f"backtest_{args.date}.txt"
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(text + "\n", encoding="utf-8")
        print(text)
        print(f"\nSaved {outp}")
        return 0
    if args.cmd == "live-1m":
        from .scan1m import live_loop_1m

        reason = live_loop_1m(max_minutes=args.max_minutes, exit_at_close=args.exit_at_close,
                              force_sector=args.sector)
        print(f"1m live loop ended: {reason}")
        out = os.environ.get("GITHUB_OUTPUT")
        if out:  # same handover contract as `live`
            with open(out, "a", encoding="utf-8") as fh:
                fh.write(f"handoff={'true' if reason == 'budget' else 'false'}\n")
        return 0

    from .pipeline import live_loop

    reason = live_loop(max_minutes=args.max_minutes, exit_at_close=args.exit_at_close)
    print(f"live loop ended: {reason}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:  # lets the workflow start a successor only on a planned handover
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"handoff={'true' if reason == 'budget' else 'false'}\n")
    return 0


def _mask(value: str) -> str:
    """Say whether a secret arrived, never what it is."""
    if not value:
        return "MISSING"
    return f"set ({len(value)} chars, {value[:3]}…{value[-3:]})"


def _bot4_check(*, dry_run: bool = False, send=None, send_doc=None) -> int:
    """Prove the bot-4 channel works end to end, without needing a live session.

    The failure this exists for: a secret added under the wrong name, or as a
    *variable* instead of a secret, resolves to an empty string — and the first
    anyone knows of it is a setup that never arrives. So this checks the two
    values, sends one text, uploads one real PDF, and exits non-zero on any of it.
    """
    from . import screen, screen_pdf, telegram
    from .session import now_ist

    send = send or telegram.send_bot4
    send_doc = send_doc or telegram.send_document
    token, chat = telegram.fourth_config_from_env()
    print(f"IST now: {now_ist():%Y-%m-%d %H:%M:%S}")
    print(f"TELEGRAM_BOT_TOKEN_4: {_mask(token)}")
    print(f"TELEGRAM_CHAT_ID_4:   {_mask(chat)}")
    print(f"bot 4 screen: {'on' if screen.screen_enabled() else 'off'} · early pass "
          f"{'on' if screen.early_enabled() else 'off'} · {screen.max_day()}/day · "
          f"persist >= {screen.persist_min():.0f} · structure >= "
          f"{screen.structure_min():.0f} · strength >= {screen.strength_min():.0f}")
    if not token or not chat:
        print("\nFAILED: bot 4 has no credentials, so nothing can be sent.", file=sys.stderr)
        print("Add BOTH as repository secrets (Settings → Secrets and variables → "
              "Actions → Secrets, not Variables):", file=sys.stderr)
        print("  TELEGRAM_BOT_TOKEN_4 = the token @BotFather gave the bot-4 bot",
              file=sys.stderr)
        print("  TELEGRAM_CHAT_ID_4   = the chat the bot-4 bot posts into "
              "(the bot must be a member)", file=sys.stderr)
        return 2
    if dry_run:
        print("\nDRY-RUN: not calling Telegram; the credentials are present.")
        return 0

    text = ("🤖 <b>Bot 4 check</b> — this is the screen's own channel.\n"
            "If you can read this, setups will reach you here.\n"
            "Next scheduled pass: the 5m live scan, "
            "starting with the pre-close provisional card.")
    if not send(text):
        print("\nFAILED: Telegram refused the text. A wrong token fails here; a wrong "
              "chat id fails as \"chat not found\" or \"bot is not a member of the "
              "chat\".", file=sys.stderr)
        return 3
    print("\ntext: delivered to bot 4")

    state = screen.load()
    out = screen_pdf.report(state, send_doc=lambda path, caption, **kw: send_doc(
        path, caption, bot=4))
    print(f"pdf:  {out['path']} ({Path(out['path']).stat().st_size} bytes) · uploaded="
          f"{out['sent']}")
    if not out["sent"]:
        print("\nFAILED: the PDF was built but not delivered — sendDocument and "
              "sendMessage can fail independently (file too large, or the token lacks "
              "the right chat).", file=sys.stderr)
        return 4
    print("\nOK: bot 4's credentials, text and document upload all work.")
    return 0


def run_screen_once() -> dict:
    """One bot-4 pass on the last closed candle — the diagnostic twin of the hook.

    Same board sweep, same frames, same gates as the live loop; it simply does not
    wait for the loop. Entries are impossible here by construction: the screen only
    ever sends a setup card, and the market side is read from the board's own tilt
    (the screen treats the index as a feature, never as a caller of the market).
    """
    from types import SimpleNamespace

    from . import screen, telegram
    from .board import bars, last_frames, sweep
    from .session import now_ist, to_ist

    now = now_ist()
    payload = sweep(now=now)
    if payload is None:
        from .scan1m import load_ledger      # the board reads bot 1's own ledger

        book = bars(load_ledger())
        payload = book[-1] if book else None
    if not payload:
        print("screen: no board candle to screen (outside 09:20-15:15, already swept, "
              "or the scan never wrote sector numbers for this candle)")
        return {"bar": None, "candidates": 0, "surfaced": [], "setups": 0}

    frames = last_frames() or {}
    hhmm = str(payload.get("b"))
    last = None
    for df in frames.values():
        if df is None or getattr(df, "empty", True):
            continue
        hit = [ts for ts in df.index if to_ist(ts).strftime("%H:%M") == hhmm]
        if hit:
            last = to_ist(hit[-1])
            break
    if last is None:
        print(f"screen: no frame holds the {hhmm} candle — nothing to judge "
              "(run this while the live loop is up)")
        return {"bar": None, "candidates": 0, "surfaced": [], "setups": 0}

    st4 = screen.load()
    tape = (st4.get("market") or [{}])[-1]
    md = SimpleNamespace(dir_state=int(payload.get("tilt") or tape.get("side") or 0),
                         dir_label=tape.get("label") or "",
                         dir_conf=tape.get("conf"),
                         regime=tape.get("regime") or "")
    surfaced = screen.consider(st4, payload, frames, md, last, send=telegram.send_bot4)
    for text in screen.updates(st4):
        if telegram.send_bot4(text, dry_run=os.environ.get("DRY_RUN", "0") == "1"):
            screen.save(st4)
    last_row = (st4.get("scan") or [{}])[-1]
    return {"bar": hhmm, "candidates": last_row.get("cands", 0), "surfaced": surfaced,
            "setups": len(st4.get("setups") or [])}


def _doctor() -> int:
    from . import data, telegram
    from .session import now_ist

    print("IST now:", now_ist().strftime("%Y-%m-%d %H:%M:%S"))
    uni = data.load_universe()
    nifty = data.fetch_symbol(uni["nifty"])
    print(f"Nifty 5m bars: {len(nifty['m5'])}  last={nifty['m5'].index[-1] if len(nifty['m5']) else 'NA'}")
    vix = data.fetch_symbol(uni["vix"])
    print(f"VIX  5m bars: {len(vix['m5'])}  last={vix['m5'].index[-1] if len(vix['m5']) else 'NA'}")
    token, chat = telegram.config_from_env()
    secondary_token, secondary_chat = telegram.secondary_config_from_env()
    print("Telegram token:", "set" if token else "MISSING")
    print("Telegram chat:", "set" if chat else "MISSING")
    if secondary_token or secondary_chat:
        secondary_status = "set" if secondary_token and secondary_chat else "INCOMPLETE"
    else:
        secondary_status = "not configured"
    print("Telegram secondary:", secondary_status)
    third_token, third_chat = telegram.third_config_from_env()
    if third_token or third_chat:
        print("Telegram bot 3 (1m scan):",
              "set" if third_token and third_chat else "INCOMPLETE — set both _3 values")
    else:
        print("Telegram bot 3 (1m scan): not configured (live-1m prints instead of sending)")
    fourth_token, fourth_chat = telegram.fourth_config_from_env()
    if fourth_token or fourth_chat:
        print("Telegram bot 4 (screen):",
              "set" if fourth_token and fourth_chat else "INCOMPLETE — set both _4 values")
    else:
        print("Telegram bot 4 (screen): not configured (setups print instead of sending)")
    from . import screen as _screen
    print(f"bot 4 screen (early pass {'on' if _screen.early_enabled() else 'off'}): "
          f"{'on' if _screen.screen_enabled() else 'off'} · "
          f"max {_screen.max_day()}/day · score >= {_screen.min_score():.0f} · "
          f"persist >= {_screen.persist_min():.0f} (slope >= {_screen.slope_min():.0f} over "
          f"{_screen.slope_bars()}) · structure >= {_screen.structure_min():.0f} · "
          f"strength >= {_screen.strength_min():.0f} · |lead| <= {_screen.lead_max():.1f}pp · "
          f"run <= {_screen.run_max_r():.1f}R · counter-trend persist >= "
          f"{_screen.counter_persist():.0f} · {_screen.cap_per_sector()}/sector · "
          f"cooldown {_screen.cooldown_min()}m · last setup {_screen.last_entry()}")
    from .scan1m import cancel_message, first_minute, market_source
    fm = first_minute()
    source = market_source()
    lead = ("(pick projected from the forming candle)" if source != "ledger"
            else "(no projection — the ledger holds closed candles only)")
    from .trade import (breakeven_after_t1, min_target_pct, min_target_r)
    from .pipeline import (entry_min_rank, entry_rank_band, late_max_minutes, late_max_r,
                           max_entries_per_pick, rank_by_sector_lead,
                           scan5m_leader_catchup, scan_require_target, _conf_delta)
    from .market import pick_min_persist, pick_rs_margin, pick_sticky_margin, sector_max_lag_bars
    from .market import follow_extras, pick_sticky_log
    print(f"5m alert rules: max {max_entries_per_pick() or 'no'} entries per pick · "
          f"runners only (within {entry_rank_band() * 100:.0f}% of the sector's best rank, "
          f"floor {entry_min_rank():.0f}) · "
          f"ranked by {'sector lead' if rank_by_sector_lead() else 'score'} · "
          f"targets >= {min_target_r():.1f}R / {min_target_pct():.2f}% · "
          f"market block on a {_conf_delta()}-point conf move · "
          f"breakeven stop after T1 {'on' if breakeven_after_t1() else 'off'}")
    print(f"5m entry rules: entries without a pivot target "
          f"{'refused' if scan_require_target() else 'taken'} · "
          f"follow scans {'every leader' if follow_extras() else 'the picked sector only'} · "
          f"incumbent pick read from {'a per-bar log' if pick_sticky_log() else 'the last candle'} · "
          f"late leaders {'on' if scan5m_leader_catchup() else 'off'}"
          + (f" (<= {late_max_minutes():.0f} min, <= {late_max_r():.1f}R)"
             if scan5m_leader_catchup() else ""))
    lag = sector_max_lag_bars()
    from .market import (conf_scaled_floor, divergence_short, diverge_min_persist,
                         follow_on_weak, lowconf_min_persist, weak_follow_min_persist)
    floor = (f"persist floor {pick_min_persist():.0f}->{lowconf_min_persist():.0f} as conf falls"
             if conf_scaled_floor() else f"persist floor {pick_min_persist():.0f}")
    from .knobs import describe
    for line in describe():
        print(line)
    print(f"sector pick: rs margin {pick_rs_margin():.2f}pp · "
          f"{floor} · "
          f"hold the incumbent by {pick_sticky_margin():.0f} · "
          f"index freshness {'off' if lag < 0 else f'{lag} bars'} · "
          + (f"weak-day follow on (BULL only, persist >= {weak_follow_min_persist():.0f})"
             if follow_on_weak() else "weak-day follow off (conf < 55 stands aside)")
          + (f" · divergence short on (persist >= {diverge_min_persist():.0f})"
             if divergence_short() else " · divergence short off"))
    from .scan1m import (alert_cap, catchup_digest, early_retries, leader_catchup,
                         leader_max_minutes, leader_max_r, require_target)
    catchup = (f"on (max {leader_max_minutes()} min old, <= {leader_max_r():.1f}R)"
               if leader_catchup() else "off")
    print(f"1m scan knobs: first minute {fm} (up to {5 - fm} min early) · "
          f"market source {source} {lead} · "
          f"re-projection after a cancel: up to {early_retries()} more per name · "
          f"cancel messages {'on' if cancel_message() else 'off'} · "
          f"leader catch-up {catchup} · "
          f"alerts per candle {'no cap' if not alert_cap() else alert_cap()} · "
          f"late leaders {'one digest' if catchup_digest() else 'one per name'} · "
          f"entries without a target {'refused' if require_target() else 'taken'}")
    if token and chat:
        ok = telegram.send("NSE scanner doctor ping", dry_run=os.environ.get("DRY_RUN") == "1")
        print("Telegram send:", "ok" if ok else "FAILED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
