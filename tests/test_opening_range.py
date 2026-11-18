"""The opening range per side: 30 minutes for a buy, 15 for a sell.

The engine used to run one range for both sides, fifteen minutes shorter than
the one it now gives the sell side. On 18 Nov the misses were all shorts fired
between 09:20 and 09:40, while the 09:45 buy-side range was still completing, so
the two sides no longer share a clock: ``OR_MINUTES`` (30) is the buy side,
``OR_MINUTES_SHORT`` (15) the sell side, and nothing else moved.

``market_open_minutes`` is 0 at 09:15, so 09:30 is 15 and 09:45 is 30.
"""
from unittest import TestCase
from unittest.mock import patch

import numpy as np
import pandas as pd

from nse_scanner import market, session
from nse_scanner.market import OpeningRange, _dir_conf
from nse_scanner.session import IST
from test_sector_pick import DAYS, bars, daily, nifty, series


class ConstantsTests(TestCase):
    def test_the_buy_side_keeps_the_full_thirty_minutes(self):
        self.assertEqual(session.OR_MINUTES, 30)
        self.assertEqual(market.OR_MINUTES, 30)

    def test_the_sell_side_reads_fifteen(self):
        self.assertEqual(session.OR_MINUTES_SHORT, 15)
        self.assertEqual(market.OR_MINUTES_SHORT, 15)

    def test_the_two_sides_are_not_the_same_number(self):
        self.assertNotEqual(session.OR_MINUTES, session.OR_MINUTES_SHORT)


class GateTests(TestCase):
    """When each side is allowed to act, and that neither can act earlier."""

    def test_nothing_is_ready_inside_the_shortest_window(self):
        rng = OpeningRange()
        for mom in (0, 5, 10):
            rng.extend(mom, high=101.0, low=99.0)
            self.assertEqual(rng.ready(mom), (False, False))

    def test_the_sell_side_opens_at_0930_and_the_buy_side_at_0945(self):
        rng = OpeningRange()
        for mom in range(0, 30, 5):          # 09:15 ... 09:40
            rng.extend(mom, high=101.0, low=99.0)
            buy, sell = rng.ready(mom)
            self.assertEqual(buy, mom >= 30)
            self.assertEqual(sell, mom >= 15)

    def test_a_missing_opening_tape_is_never_read_as_a_break(self):
        # The tape starts at 10:00: no range was watched, so neither side gets
        # to claim a break of it. This is what seed=False is for.
        rng = OpeningRange()
        rng.extend(45, high=101.0, low=99.0)
        self.assertTrue(np.isnan(rng.hi) and np.isnan(rng.lo_s))
        self.assertEqual(rng.ready(45), (False, False))


class RangeExtentTests(TestCase):
    """Each range stops on its own clock — that is the whole change."""

    BARS = [  # (minutes after 09:15, high, low)
        (0, 100.4, 99.6),
        (5, 100.8, 99.4),      # both windows open: sell low 99.4
        (10, 101.0, 99.2),     # both windows open: sell low 99.2
        (15, 101.6, 98.9),     # sell window shut: 98.9 must NOT reach the sell low
        (20, 102.0, 98.5),
        (25, 101.2, 98.7),
        (30, 103.0, 98.0),     # buy window shut too
    ]

    def walk(self):
        rng = OpeningRange()
        for mom, high, low in self.BARS:
            rng.extend(mom, high, low)
        return rng

    def test_the_sell_low_is_only_the_first_fifteen_minutes(self):
        self.assertEqual(self.walk().lo_s, 99.2)

    def test_the_buy_range_is_the_first_thirty_minutes(self):
        rng = self.walk()
        self.assertEqual((rng.hi, rng.lo), (102.0, 98.5))

    def test_a_break_of_the_sell_range_can_happen_before_the_buy_range_exists(self):
        rng = OpeningRange()
        for mom, high, low in self.BARS[:4]:     # up to and including 09:30
            rng.extend(mom, high, low)
        buy, sell = rng.ready(15)                # the 09:30 bar
        self.assertFalse(buy)                    # the buy side still waits for 09:45
        self.assertTrue(sell)
        self.assertLess(98.6, rng.lo_s)          # 98.6 under 99.2 is a sell break

    def test_a_bar_after_both_windows_moves_neither(self):
        rng = self.walk()
        rng.extend(350, high=110.0, low=95.0)
        self.assertEqual((rng.hi, rng.lo, rng.lo_s), (102.0, 98.5, 99.2))


class SeedTests(TestCase):
    """The index engine seeds a range it started late; the sector scorer does not."""

    def test_a_late_starting_index_engine_still_has_a_range(self):
        rng = OpeningRange(seed=True)
        rng.extend(45, high=101.0, low=99.0)
        self.assertEqual((rng.hi, rng.lo, rng.lo_s), (101.0, 99.0, 99.0))
        self.assertEqual(rng.ready(45), (True, True))

    def test_a_late_starting_sector_scorer_does_not(self):
        rng = OpeningRange(seed=False)
        rng.extend(45, high=101.0, low=99.0)
        self.assertEqual(rng.ready(45), (False, False))

    def test_a_new_session_starts_with_no_range(self):
        rng = OpeningRange()
        rng.extend(5, high=101.0, low=99.0)
        rng.reset()
        self.assertTrue(np.isnan(rng.hi) and np.isnan(rng.lo) and np.isnan(rng.lo_s))
        self.assertEqual(rng.ready(45), (False, False))


class DirectionConfidenceTests(TestCase):
    """The confidence boost follows the side's own break, not a shared one."""

    def conf(self, dir_state, or_up, or_dn):
        return _dir_conf(2, 1, 4, 2, 25.0, 60.0, dir_state, or_up, or_dn, 0.0)

    def test_a_sell_break_boosts_only_a_bear(self):
        self.assertEqual(self.conf(-1, False, True) - self.conf(-1, False, False), 7)
        self.assertEqual(self.conf(1, False, True), self.conf(1, False, False))

    def test_a_buy_break_boosts_only_a_bull(self):
        self.assertEqual(self.conf(1, True, False) - self.conf(1, False, False), 7)
        self.assertEqual(self.conf(-1, True, False), self.conf(-1, False, False))


class SellSideOnATapeTests(TestCase):
    """The rule, on a tape: a dip through the 15-minute low but not the 30-minute one.

    Bars 09:15-09:30 hold above 99.5, so the sell-side range has a low of 99.5 and
    a close under it from 09:35. Split falls to 98.4 and back, so the *thirty*
    minute low is 98.4 — a close of 99.0 is nowhere near it. Under the old shared
    range the sell side therefore saw no break at all until 11:30, and earned no
    early-rotation boost; now it earns it from 09:35.
    """

    FALLING_DAY = [
        (100.2, 100.4, 100.0, 100.2),   # 09:15
        (100.2, 100.3, 99.9, 100.0),    # 09:20
        (100.0, 100.1, 99.7, 99.8),     # 09:25
        (99.8, 99.9, 99.5, 99.6),       # 09:30  sell range low = 99.5
        (99.6, 99.7, 98.4, 99.2),       # 09:35  the dip that only the 30-min range sees
        (99.2, 99.4, 98.9, 99.0),       # 09:40  close 99.0 < 99.5
        (99.0, 99.2, 98.9, 99.0),       # 09:45  buy range completes here
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

    def snap(self, today_bars: int, **env):
        df = self.tape(today_bars)
        with patch.dict("os.environ", {"SECTOR_MAX_LAG_BARS": "-1", **env}):
            return market._sector_snap("TEST", df, daily(df), DAYS[-1], nifty(), 1.0,
                                       df.index[-1])

    def test_the_sell_side_earns_the_rotation_boost_before_0945(self):
        now = self.snap(6)                     # last bar 09:40
        with patch.object(market, "OR_MINUTES_SHORT", 30):   # the old shared range
            then = self.snap(6)
        self.assertEqual(now.side, -1)
        self.assertGreater(now.bear, then.bear + 2.0)

    def test_the_buy_side_is_where_it_was(self):
        now = self.snap(6)
        with patch.object(market, "OR_MINUTES_SHORT", 30):
            then = self.snap(6)
        self.assertEqual(now.bull, then.bull)

    def test_before_0930_the_two_rules_score_the_same(self):
        # 09:30 is the first bar either rule can act on for a sell: nothing at
        # 09:25 differs, which is what "only the sell side's clock moved" means.
        now = self.snap(3)                     # last bar 09:25
        with patch.object(market, "OR_MINUTES_SHORT", 30):
            then = self.snap(3)
        self.assertEqual((now.bear, now.side), (then.bear, then.side))
        self.assertEqual(self.snap(4).side, -1)    # 09:30 still scored and sideable
