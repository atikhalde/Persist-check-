"""OI x Price filter matrix applied to candidate entry signals.

Self-contained module. Does not modify any existing scanner logic.
"""

from __future__ import annotations

from .oi_data import OIRegime, OISnapshot


def should_take_entry(
    side: str,
    reading: OISnapshot | None,
    min_oi_chg_pct: float = 1.0,
    min_price_chg_pct: float = 0.05,
) -> bool:
    """Return True only if OI confirms the entry direction.

    BUY  -> Long Build-up: price up, OI up (new money entering longs)
    SELL -> Short Build-up: price down, OI up (new money entering shorts)

    Fails closed: missing OI data => reject entry.
    """
    if reading is None:
        return False
    if reading.regime == OIRegime.NEUTRAL:
        return False
    if abs(reading.oi_chg_pct or 0.0) < min_oi_chg_pct:
        return False
    if abs(reading.price_chg_pct or 0.0) < min_price_chg_pct:
        return False

    if side == "BUY":
        return reading.regime == OIRegime.LONG_BUILDUP
    if side == "SELL":
        return reading.regime == OIRegime.SHORT_BUILDUP
    return False
