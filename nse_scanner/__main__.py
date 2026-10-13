"""CLI

  python -m nse_scanner once
  python -m nse_scanner live
  python -m nse_scanner doctor
  python -m nse_scanner once --dry-run --sector IT
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
    p.add_argument("cmd", choices=["once", "live", "doctor", "backtest"], help="once | live | doctor | backtest")
    p.add_argument("--dry-run", action="store_true", help="print alerts, do not call Telegram")
    p.add_argument("--sector", default=None, help="force sector code (AUTO, IT, BANK, …)")
    p.add_argument("--backfill", action="store_true", help="allow scanning the last session if today has no bars (holiday/weekend)")
    p.add_argument("--date", default="2026-10-01", help="backtest session YYYY-MM-DD (IST)")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = p.parse_args(argv)
    _setup_log(args.log_level)
    if args.dry_run:
        os.environ["DRY_RUN"] = "1"

    if args.cmd == "doctor":
        return _doctor()
    if args.cmd == "once":
        from .pipeline import run_once

        out = run_once(force_sector=args.sector, backfill=args.backfill)
        print(
            f"{out['bar']}  {out['market']}  pick={out['pick']}  {out['action']}  new_alerts={len(out['signals'])}"
        )
        for s in out["signals"]:
            print(f"  {s['name']:12} {s['symbol']:12} {s['side']:4} {s['price']} @ {s['time']}")
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
    from .pipeline import live_loop

    live_loop()
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
    print("Telegram token:", "set" if token else "MISSING")
    print("Telegram chat:", "set" if chat else "MISSING")
    if token and chat:
        ok = telegram.send("NSE scanner doctor ping", dry_run=os.environ.get("DRY_RUN") == "1")
        print("Telegram send:", "ok" if ok else "FAILED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
