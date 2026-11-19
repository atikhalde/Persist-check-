"""The opening range: one range, 25 minutes, for both sides.

The engine ran a 30-minute range (09:15-09:45) for most of its life, then briefly
one range per side (30 for a buy, 15 for a sell). It is back to a single range,
now 25 minutes: the bars of 09:15, 09:20, 09:25, 09:30 and 09:35 are in it, the
09:40 bar is the first one it has closed on, so ``mom >= 25`` is the gate and the
first bar a pick can be committed on is **09:40** — one candle earlier than the
old 30, for longs and shorts alike.

``market_open_minutes`` is 0 at 09:15, so 09:35 is 20 and 09:40 is 25.
"""
from unittest import TestCase
from unittest.mock import patch

import numpy as np
import pandas as pd

from nse_scanner import market, session
from nse_scanner.market import OpeningRange, _dir_conf
from nse_scanner.session import IST
from test_sector_pick import DAYS, bars, daily, nifty, series


class ConstantTests(TestCase):
    def test_the_range_is_twenty_five_minutes(self):
        self.assertEqual(session.OR_MINUTES, 25)
        self.assertEqual(market.OR_MINUTES, 25)

    def test_both_sides_read_the_same_number(self):
        # The per-side split is gone: there is no second constant to drift from.
        self.assertFalse(hasattr(session, "OR_MINUTES_SHORT"))
        self.assertFalse(hasattr(market, "OR_MINUTES_SHORT"))


class GateTests(TestCase):
    """When the range is closed, and that it cannot be used before that."""

    def test_the_gate_opens_on_the_0940_bar(self):
        rng = OpeningRange()
        for mom in (0, 5, 10, 15, 20):
            rng.extend(mom, high=101.0, low=99.0)
            self.assertFalse(rng.ready(mom))       # inside the window
        self.assertTrue(rng.ready(25))             # 09:40

    def test_the_gate_is_a_boundary_and_not_a_count(self):
        rng = OpeningRange(seed=True)
        rng.extend(25, high=101.0, low=99.0)       # a tape that starts at 09:40
        self.assertTrue(rng.ready(25))

    def test_an_unseen_opening_tape_is_never_read_as_a_break(self):
        rng = OpeningRange()                       # sector scoring: no seeding
        rng.extend(45, high=101.0, low=99.0)
        self.assertTrue(np.isnan(rng.hi) and np.isnan(rng.lo))
        self.assertFalse(rng.ready(45))


class RangeExtentTests(TestCase):
    """The five bars 09:15-09:35 build it; the 09:40 bar does not touch it."""

    BARS = [  # (minutes after 09:15, high, low)
        (0, 100.4, 99.6),
        (5, 100.8, 99.4),
        (10, 101.0, 99.2),
        (15, 100.6, 98.9),
        (20, 101.2, 99.1),     # 09:35 — the last bar that counts
        (25, 103.0, 98.0),     # 09:40 — the first bar after it
        (30, 104.0, 97.0),
    ]

    def walk(self):
        rng = OpeningRange()
        for mom, high, low in self.BARS:
            rng.extend(mom, high, low)
        return rng

    def test_the_range_is_the_first_five_bars(self):
        self.assertEqual((self.walk().hi, self.walk().lo), (101.2, 98.9))

    def test_a_bar_after_the_range_moves_neither_edge(self):
        rng = self.walk()
        rng.extend(350, high=110.0, low=95.0)
        self.assertEqual((rng.hi, rng.lo), (101.2, 98.9))

    def test_a_new_session_starts_with_no_range(self):
        rng = self.walk()
        rng.reset()
        self.assertTrue(np.isnan(rng.hi) and np.isnan(rng.lo))
        self.assertFalse(rng.ready(45))


class SeedTests(TestCase):
    """The index engine seeds a range it started late; the sector scorer does not."""

    def test_a_late_starting_index_engine_still_has_a_range(self):
        rng = OpeningRange(seed=True)
        rng.extend(45, high=101.0, low=99.0)
        self.assertEqual((rng.hi, rng.lo), (101.0, 99.0))
        self.assertTrue(rng.ready(45))

    def test_a_late_starting_sector_scorer_does_not(self):
        rng = OpeningRange(seed=False)
        rng.extend(45, high=101.0, low=99.0)
        self.assertFalse(rng.ready(45))


class DirectionConfidenceTests(TestCase):
    """Confidence still follows the side's own break of that one range."""

    def conf(self, dir_state, or_up, or_dn):
        return _dir_conf(2, 1, 4, 2, 25.0, 60.0, dir_state, or_up, or_dn, 0.0)

    def test_a_break_boosts_the_side_it_belongs_to(self):
        self.assertEqual(self.conf(1, True, False) - self.conf(1, False, False), 7)
        self.assertEqual(self.conf(-1, False, True) - self.conf(-1, False, False), 7)
        self.assertEqual(self.conf(-1, True, False), self.conf(-1, False, False))


class BonusArrivesOnThe0940BarTests(TestCase):
    """The rule on a tape: the early-rotation boost starts one bar earlier.

    Bars 09:15-09:35 hold above 99.5 (that is the 25-minute low); 09:40 closes at
    99.0, through it. Under the old 30-minute range nothing was closed at 09:40,
    so the sell side saw no break and earned no boost — and by 09:45 its low was
    the 09:40 wick at 98.9, still below the close, so it never earned one at all.
    """

    FALLING_DAY = [
        (100.2, 100.4, 100.0, 100.2),   # 09:15
        (100.2, 100.3, 99.9, 100.0),    # 09:20
        (100.0, 100.1, 99.7, 99.8),     # 09:25
        (99.8, 99.9, 99.5, 99.6),       # 09:30
        (99.6, 99.7, 99.55, 99.7),      # 09:35  range low = 99.5
        (99.7, 99.8, 98.9, 99.0),       # 09:40  close 99.0 < 99.5
        (99.0, 99.2, 98.9, 99.0),       # 09:45
    ]

    @staticmethod
    def frame(day, rows):
        t = pd.Timestamp(day, tz=IST) + pd.Timedelta(hours=9, minutes=15)
        idx = []
        for _ in rows:
            idx.append(t)
            t += pd.Timedelta(minutes=5)
        return pd.DataFrame({"Open": [r[0] for r in rows], "High": [r[1] for r in rows],
                             "Low": [r[2] for r in rows], "Close": [r[3] for r in rows],
                             "Volume": [1.0] * len(rows)},
                            index=pd.DatetimeIndex(idx))

    def tape(self, today_bars: int):
        out = [bars(d, series(101.0, -0.00002)) for d in DAYS[:-1]]
        out.append(self.frame(DAYS[-1], self.FALLING_DAY[:today_bars]))
        return pd.concat(out)

    def snap(self, today_bars: int):
        df = self.tape(today_bars)
        with patch.dict("os.environ", {"SECTOR_MAX_LAG_BARS": "-1"}):
            return market._sector_snap("TEST", df, daily(df), DAYS[-1], nifty(), 1.0,
                                       df.index[-1])

    def test_the_break_earns_the_boost_from_the_0940_bar(self):
        now = self.snap(7)                                    # last bar 09:45
        with patch.object(market, "OR_MINUTES", 30):          # the old range
            then = self.snap(7)
        self.assertEqual(now.side, -1)
        self.assertGreater(now.bear, then.bear + 2.0)

    def test_the_boost_is_not_there_at_0935(self):
        now = self.snap(5)                                    # last bar 09:35
        with patch.object(market, "OR_MINUTES", 30):
            then = self.snap(5)
        self.assertEqual(now.bear, then.bear)                 # neither rule is ready
