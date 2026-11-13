"""Sector follow — now part of the main scanner.

This used to be a second, separate scanner (PRs #8/#9/#11) that nothing ever
started, so its alerts never arrived. The rule itself — *Nifty NEUTRAL, but a
PERSIST + sector is clearly leading → scan it LONG* — now lives in
``market._follow_leaders`` and runs inside the normal ``live`` loop, where it
gets early alerts, close confirmation and target / SL / EMA9-exit alerts like
any other pick.

The old commands still work and simply run the main scanner:

    python -m nse_scanner sector-follow        ->  once
    python -m nse_scanner live-sector-follow   ->  live

Settings: ``SECTOR_FOLLOW`` (1), ``SECTOR_FOLLOW_MIN_PERSIST`` (55),
``SECTOR_FOLLOW_MAX`` (2), ``SF_ALIGNED_SECTORS`` (sectors your chart shows as
SMART$/MARKET/SECTOR/FLOW aligned; they are scanned first).
"""

from __future__ import annotations

import logging

log = logging.getLogger("nse_scanner.sector_follow")


def run_once_sector_follow(*, force_sector: str | None = None) -> dict:
    from .pipeline import run_once

    return run_once(force_sector=force_sector)


def live_loop_sector_follow(**kwargs) -> str:
    from .pipeline import live_loop

    log.warning("live-sector-follow is the main live loop now; do not run it next to `live`")
    return live_loop(**kwargs)
