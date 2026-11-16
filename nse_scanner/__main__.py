"""CLI

  python -m nse_scanner once
  python -m nse_scanner live
  python -m nse_scanner live-1m          # 1m projected scan, Telegram bot 3
  python -m nse_scanner once-1m          # one projected pass (diagnostic)
  python -m nse_scanner doctor
  python -m nse_scanner once --dry-run --sector IT
  python -m nse_scanner early-check --date YYYY-MM-DD --sector IT
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
    p.add_argument("cmd", choices=["once", "live", "once-1m", "live-1m", "doctor", "backtest", "early-check", "dual", "live-dual", "sector-follow", "live-sector-follow"], help="once | live | once-1m | live-1m | doctor | backtest | early-check | dual | live-dual | sector-follow | live-sector-follow")
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
        print(f"{bar:%Y-%m-%d %H:%M}  minute {minute}  {kind} alerts={len(out)}")
        for s in out:
            print(f"  {s['name']:12} {s['symbol']:12} {s['side']:4} {s['price']} @ {s['time']}")
        return 0
    if args.cmd == "early-check":
        from datetime import date as _date
        from .early_check import render, run

        d = _date.fromisoformat(args.date)
        print(render(run(d, args.sector, args.lead), d, args.lead))
        return 0
    if args.cmd == "backtest":
        from datetime import date as _date
        from pathlib import Path
        from .backtest import replay_session, render_report

        d = _date.fromisoformat(args.date)
        res = replay_session(d)
        text = render_report(res)
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
    from .scan1m import cancel_message, first_minute, market_source
    fm = first_minute()
    source = market_source()
    lead = ("(pick projected from the forming candle)" if source != "ledger"
            else "(no projection — the ledger holds closed candles only)")
    from .trade import (breakeven_after_t1, min_target_pct, min_target_r)
    from .pipeline import max_entries_per_pick, rank_by_sector_lead, _conf_delta
    from .market import pick_min_persist, pick_rs_margin, pick_sticky_margin, sector_max_lag_bars
    print(f"5m alert rules: max {max_entries_per_pick() or 'no'} entries per pick · "
          f"ranked by {'sector lead' if rank_by_sector_lead() else 'score'} · "
          f"targets >= {min_target_r():.1f}R / {min_target_pct():.2f}% · "
          f"market block on a {_conf_delta()}-point conf move · "
          f"breakeven stop after T1 {'on' if breakeven_after_t1() else 'off'}")
    lag = sector_max_lag_bars()
    from .market import follow_on_weak
    print(f"sector pick: rs margin {pick_rs_margin():.2f}pp · "
          f"persist floor {pick_min_persist():.0f} · "
          f"hold the incumbent by {pick_sticky_margin():.0f} · "
          f"index freshness {'off' if lag < 0 else f'{lag} bars'} · "
          f"weak-day follow {'on' if follow_on_weak() else 'off (conf < 55 stands aside)'}")
    from .scan1m import early_retries, leader_catchup, leader_max_minutes, leader_max_r
    catchup = (f"on (max {leader_max_minutes()} min old, <= {leader_max_r():.1f}R)"
               if leader_catchup() else "off")
    print(f"1m scan knobs: first minute {fm} (up to {5 - fm} min early) · "
          f"market source {source} {lead} · "
          f"re-projection after a cancel: up to {early_retries()} more per name · "
          f"cancel messages {'on' if cancel_message() else 'off'} · "
          f"leader catch-up {catchup}")
    if token and chat:
        ok = telegram.send("NSE scanner doctor ping", dry_run=os.environ.get("DRY_RUN") == "1")
        print("Telegram send:", "ok" if ok else "FAILED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
