"""OI-confirmed alerts — now part of the main scanner.

This used to be a second pass (PR #6) that re-ran the whole scan and sent a
separate "OI CONFIRMED" message; nothing ever started it, and by default it
used made-up OI numbers. Open interest is now read inside the normal scan:

* ``OI_FILTER=annotate`` (default) — when ``OI_FILE`` has a row for the stock
  and candle, the entry alert gets an ``OI Long Build-Up · OI +1.8% …`` line.
* ``OI_FILTER=require`` — an entry is sent only if OI confirms it
  (BUY = long build-up, SELL = short build-up).
* ``OI_FILTER=off`` — ignore OI.

Yahoo has no open interest: with no rows for today in ``OI_FILE`` the filter is
unavailable and alerts are sent unchanged. The old commands still work:

    python -m nse_scanner dual        ->  once
    python -m nse_scanner live-dual   ->  live
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("nse_scanner.oi_live")


def _apply(oi_source: str | None, oi_file: str | None) -> None:
    if oi_source:
        os.environ["OI_SOURCE"] = oi_source
    if oi_file:
        os.environ["OI_FILE"] = oi_file


def run_once_dual(*, force_sector: str | None = None, oi_source: str | None = None,
                  oi_file: str | None = None) -> dict:
    """One scan. ``oi`` holds the alerts that carried an OI reading."""
    from .pipeline import run_once

    _apply(oi_source, oi_file)
    out = run_once(force_sector=force_sector)
    confirmed = [s for s in out.get("signals", []) if s.get("oi_regime")]
    return {"original": out, "oi": {**out, "signals": confirmed}}


def live_loop_dual(*, oi_source: str | None = None, oi_file: str | None = None, **kwargs) -> str:
    from .pipeline import live_loop

    _apply(oi_source, oi_file)
    log.warning("live-dual is the main live loop now; do not run it next to `live`")
    return live_loop(**kwargs)
