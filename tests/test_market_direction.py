"""Direction label vs committed side (the "Market NEUTRAL ... BUY" contradiction)."""
from unittest import TestCase

from nse_scanner.market import _complementary_pick, _direction_label


class DirectionLabelTests(TestCase):
    def test_a_traded_side_is_never_labelled_neutral(self):
        for side in (1, -1):
            for strong in (False, True):
                self.assertNotEqual(_direction_label(side, strong, strong), "NEUTRAL")

    def test_labels_follow_the_committed_side(self):
        self.assertEqual(_direction_label(1), "BULL")
        self.assertEqual(_direction_label(1, strong_bull=True), "STRONG BULL")
        self.assertEqual(_direction_label(-1), "BEAR")
        self.assertEqual(_direction_label(-1, strong_bear=True), "STRONG BEAR")
        self.assertEqual(_direction_label(0), "NEUTRAL")

    def test_neutral_market_never_picks_a_sector(self):
        # The locked rule: NEUTRAL / CHOP / conf < 55 -> WAIT, no stock scan.
        pick, side, reason = _complementary_pick(0, 88, "TRANSITION", [], [], 0.0, 0.0)
        self.assertIsNone(pick)
        self.assertEqual(side, 0)
        self.assertIn("WAIT", reason)


class SectorFollowTests(TestCase):
    """Nifty NEUTRAL + one sector plainly leading -> scan that sector LONG."""

    @staticmethod
    def snap(name="PHA", **over):
        from types import SimpleNamespace
        base = dict(name=name, side=1, persist_bull=72.0, day_ret=0.9, pos=85.0, rs=0.6,
                    bull=70.0, recent=0.02, qualified=False, lag_bars=0, stale=False)
        return SimpleNamespace(**{**base, **over})

    def pick(self, plus, **env):
        from unittest.mock import patch
        from nse_scanner.market import _follow_pick
        with patch.dict("os.environ", env):
            return _follow_pick(plus)

    def test_persistent_leader_is_followed_even_between_fresh_pushes(self):
        name, reason = self.pick([self.snap(), self.snap("ENE", persist_bull=60)])
        self.assertEqual(name, "PHA")
        self.assertTrue(reason.startswith("SECTOR FOLLOW"))

    def leaders(self, plus, **env):
        from unittest.mock import patch
        from nse_scanner.market import _follow_leaders
        with patch.dict("os.environ", env):
            return [x.name for x in _follow_leaders(plus)]

    def test_next_leader_is_scanned_too_when_the_first_is_weak_or_quiet(self):
        plus = [self.snap(), self.snap("ENE", persist_bull=60), self.snap("IT", persist_bull=58)]
        self.assertEqual(self.leaders(plus), ["PHA", "ENE"])                 # two by default
        self.assertEqual(self.leaders(plus, SECTOR_FOLLOW_MAX="1"), ["PHA"])
        self.assertEqual(self.leaders(plus, SECTOR_FOLLOW_MAX="3"), ["PHA", "ENE", "IT"])
        plus[0] = self.snap(pos=40.0)                                        # #1 not leading
        self.assertEqual(self.leaders(plus), ["ENE", "IT"])

    def test_only_the_persist_top_three_can_lead(self):
        plus = [self.snap(pos=40.0), self.snap("B", pos=40.0), self.snap("C", pos=40.0), self.snap("D")]
        self.assertEqual(self.leaders(plus), [])

    def test_chart_aligned_sectors_are_scanned_first(self):
        plus = [self.snap(), self.snap("ENE", persist_bull=60)]
        self.assertEqual(self.leaders(plus, SF_ALIGNED_SECTORS="ene"), ["ENE", "PHA"])
        name, reason = self.pick(plus, SF_ALIGNED_SECTORS="ENE")
        self.assertEqual(name, "ENE")
        self.assertIn("chart aligned", reason)
        self.assertIn("also scanning PHA 72", reason)

    def test_flow_agreement_is_reported_not_required(self):
        self.assertIn("flow aligned", self.pick([self.snap(qualified=True)])[1])
        self.assertNotIn("flow aligned", self.pick([self.snap(qualified=False)])[1])

    def test_a_leader_that_is_not_really_leading_is_not_followed(self):
        for weak in (dict(persist_bull=50.0),      # 09:15 on 13-Nov: PHA 50
                     dict(side=-1), dict(day_ret=0.1), dict(pos=55.0), dict(rs=-0.1),
                     dict(bull=50.0), dict(recent=-0.2)):
            self.assertIsNone(self.pick([self.snap(**weak)])[0], weak)
        self.assertIsNone(self.pick([])[0])

    def test_can_be_switched_off_and_tuned(self):
        self.assertIsNone(self.pick([self.snap()], SECTOR_FOLLOW="0")[0])
        self.assertIsNone(self.pick([self.snap()], SECTOR_FOLLOW_MIN_PERSIST="80")[0])

    def test_alert_says_it_is_a_sector_follow_trade(self):
        from types import SimpleNamespace
        from nse_scanner.pipeline import _follow_tag
        md = SimpleNamespace(pick_reason="SECTOR FOLLOW — Nifty NEUTRAL", pick_sector="PHA",
                             flow_aligned=["ENE"])
        self.assertEqual(_follow_tag(md), " (sector follow)")
        self.assertEqual(_follow_tag(md, "ENE"), " (sector follow, flow aligned)")
        self.assertEqual(_follow_tag(SimpleNamespace(pick_reason="PERSIST + IT qualified",
                                                     pick_sector="IT")), "")


class PassedOverTests(TestCase):
    """A sector the pick walked past must be named, with the number that did it."""

    snap = staticmethod(SectorFollowTests.snap)

    def pick(self, plus, **env):
        from unittest.mock import patch
        with patch.dict("os.environ", env):
            return _complementary_pick(1, 80, "TREND UP", plus, [], 0.0, 0.5)

    def test_a_leader_is_not_dropped_for_a_hair_thin_rs(self):
        # persist 91 skipped because rs read -0.01 (1/100th of a percentage point)
        # and the pick fell to the persist-60 sector, silently. This is the churn
        # that moved the 16 Nov pick PHA -> FMCG one candle later.
        plus = [self.snap("AUTO", persist_bull=91.0, rs=-0.01),
                self.snap("FMCG", persist_bull=60.0, rs=0.40)]
        name, side, reason = self.pick(plus)
        self.assertEqual(name, "AUTO")
        self.assertEqual(side, 1)
        self.assertIn("score 91", reason)

    def test_without_the_margin_the_leader_is_skipped_and_named(self):
        plus = [self.snap("AUTO", persist_bull=91.0, rs=-0.01),
                self.snap("FMCG", persist_bull=60.0, rs=0.40)]
        name, _, reason = self.pick(plus, PICK_RS_MARGIN="0")
        self.assertEqual(name, "FMCG")
        self.assertIn("passed over AUTO (rs -0.01)", reason)

    def test_a_genuinely_underperforming_leader_is_still_skipped(self):
        plus = [self.snap("AUTO", persist_bull=91.0, rs=-0.60),
                self.snap("FMCG", persist_bull=60.0, rs=0.40)]
        name, _, reason = self.pick(plus)
        self.assertEqual(name, "FMCG")
        self.assertIn("passed over AUTO (rs -0.60)", reason)

    def test_a_stale_sector_is_passed_over_and_named(self):
        plus = [self.snap("AUTO", persist_bull=91.0, stale=True, lag_bars=6),
                self.snap("FMCG", persist_bull=60.0, rs=0.40)]
        name, _, reason = self.pick(plus)
        self.assertEqual(name, "FMCG")
        self.assertIn("passed over AUTO (index 6 bars behind)", reason)

    def test_the_wait_reason_names_what_it_refused(self):
        plus = [self.snap("AUTO", persist_bull=91.0, side=-1)]
        name, side, reason = self.pick(plus)
        self.assertIsNone(name)
        self.assertEqual(side, 0)
        self.assertIn("passed over AUTO (flow bearish)", reason)

    def test_a_stale_sector_never_leads_a_follow_scan(self):
        from nse_scanner.market import _follow_leaders
        from unittest.mock import patch
        plus = [self.snap("AUTO", stale=True, lag_bars=6),
                self.snap("ENE", persist_bull=60)]
        with patch.dict("os.environ", {}):
            self.assertEqual([x.name for x in _follow_leaders(plus)], ["ENE"])

    def test_lag_bars_counts_five_minute_candles(self):
        import pandas as pd
        from nse_scanner.market import _lag_bars
        from nse_scanner.session import IST
        bar = pd.Timestamp("2026-11-16 10:20", tz=IST)      # the decision bar
        self.assertEqual(_lag_bars(bar, bar), 0)
        self.assertEqual(_lag_bars(bar - pd.Timedelta(minutes=30), bar), 6)
        self.assertEqual(_lag_bars(bar + pd.Timedelta(minutes=10), bar), 0)   # never negative
        self.assertEqual(_lag_bars(None, bar), 0)
        self.assertEqual(_lag_bars(bar, None), 0)


class PersistFloorTests(TestCase):
    """A sector has to be persistent before it may be picked at all."""

    snap = staticmethod(SectorFollowTests.snap)

    def pick(self, plus, **env):
        from unittest.mock import patch
        with patch.dict("os.environ", env):
            return _complementary_pick(1, 80, "TREND UP", plus, [], 0.0, 0.5)

    def test_a_shallow_sector_is_not_picked(self):
        # The complementary path used to accept anything that was merely not
        # fighting the market: no persistence floor at all, while SECTOR FOLLOW
        # (used only when Nifty is NEUTRAL) demands 55.
        plus = [self.snap("AUTO", persist_bull=42.0, rs=0.40),
                self.snap("FMCG", persist_bull=61.0, rs=0.40)]
        name, _, reason = self.pick(plus)
        self.assertEqual(name, "FMCG")
        self.assertIn("passed over AUTO (persist 42 < 50)", reason)

    def test_the_floor_can_be_switched_off(self):
        plus = [self.snap("AUTO", persist_bull=42.0, rs=0.40)]
        name, _, reason = self.pick(plus, PICK_MIN_PERSIST="0")
        self.assertEqual(name, "AUTO")
        self.assertIn("score 42", reason)

    def test_the_floor_is_between_the_old_behaviour_and_sector_follow(self):
        from nse_scanner.market import pick_min_persist
        from unittest.mock import patch
        with patch.dict("os.environ", {}):
            self.assertEqual(pick_min_persist(), 50.0)
        with patch.dict("os.environ", {"PICK_MIN_PERSIST": "55"}):
            self.assertEqual(pick_min_persist(), 55.0)
        with patch.dict("os.environ", {"PICK_MIN_PERSIST": "junk"}):
            self.assertEqual(pick_min_persist(), 50.0)


class StickyPickTests(TestCase):
    """Two sectors a couple of points apart must not trade the pick back and forth."""

    snap = staticmethod(SectorFollowTests.snap)

    def pick(self, plus, prev, **env):
        from nse_scanner.market import _sticky_pick
        from unittest.mock import patch
        with patch.dict("os.environ", env):
            return _sticky_pick("FMCG", 1, "PERSIST + FMCG qualified (score 59)",
                                plus, [], prev)

    def test_the_incumbent_is_held_when_the_challenger_is_within_the_margin(self):
        plus = [self.snap("FMCG", persist_bull=59.0, rs=0.40),
                self.snap("PHA", persist_bull=56.0, rs=0.30)]
        name, side, reason = self.pick(plus, {"name": "PHA", "side": 1})
        self.assertEqual((name, side), ("PHA", 1))
        self.assertIn("held", reason)
        self.assertIn("FMCG 59 is +3.0, needs 5", reason)

    def test_a_clear_challenger_takes_the_pick(self):
        plus = [self.snap("FMCG", persist_bull=66.0, rs=0.40),
                self.snap("PHA", persist_bull=56.0, rs=0.30)]
        name, _, reason = self.pick(plus, {"name": "PHA", "side": 1})
        self.assertEqual(name, "FMCG")
        self.assertNotIn("held", reason)

    def test_the_margin_can_be_switched_off(self):
        plus = [self.snap("FMCG", persist_bull=57.0, rs=0.40),
                self.snap("PHA", persist_bull=56.0, rs=0.30)]
        name, _, _ = self.pick(plus, {"name": "PHA", "side": 1}, PICK_STICKY_MARGIN="0")
        self.assertEqual(name, "FMCG")

    def test_a_faded_incumbent_is_not_held(self):
        # The incumbent must still pass the gate it passed when it was chosen.
        plus = [self.snap("FMCG", persist_bull=59.0, rs=0.40),
                self.snap("PHA", persist_bull=56.0, rs=-0.40)]      # rs now negative
        name, _, _ = self.pick(plus, {"name": "PHA", "side": 1})
        self.assertEqual(name, "FMCG")

    def test_a_stale_incumbent_is_not_held(self):
        plus = [self.snap("FMCG", persist_bull=59.0, rs=0.40),
                self.snap("PHA", persist_bull=56.0, rs=0.30, stale=True, lag_bars=6)]
        name, _, _ = self.pick(plus, {"name": "PHA", "side": 1})
        self.assertEqual(name, "FMCG")

    def test_an_incumbent_below_the_floor_is_not_held(self):
        plus = [self.snap("FMCG", persist_bull=59.0, rs=0.40),
                self.snap("PHA", persist_bull=30.0, rs=0.30)]
        name, _, _ = self.pick(plus, {"name": "PHA", "side": 1})
        self.assertEqual(name, "FMCG")

    def test_a_side_change_or_a_wait_gives_no_incumbent(self):
        plus = [self.snap("FMCG", persist_bull=59.0, rs=0.40)]
        self.assertEqual(self.pick(plus, {"name": "PHA", "side": -1})[0], "FMCG")
        self.assertEqual(self.pick(plus, {"name": "WAIT", "side": 0})[0], "FMCG")
        self.assertEqual(self.pick(plus, None)[0], "FMCG")


class PreviousPickTests(TestCase):
    """The incumbent is the previous candle's pick — never an older one."""

    def test_only_the_adjacent_candle_counts(self):
        import pandas as pd
        from nse_scanner.market import previous_pick
        from nse_scanner.session import IST
        bar = pd.Timestamp("2026-11-16 10:20", tz=IST)
        self.assertEqual(previous_pick({"sector": "PHA", "side": 1,
                                        "bar": "2026-11-16 10:15"}, bar),
                         {"name": "PHA", "side": 1})
        self.assertIsNone(previous_pick({"sector": "PHA", "side": 1,
                                         "bar": "2026-11-16 09:00"}, bar))
        self.assertIsNone(previous_pick({"sector": "WAIT", "side": 0,
                                         "bar": "2026-11-16 10:15"}, bar))
        self.assertIsNone(previous_pick(None, bar))
        self.assertIsNone(previous_pick({"sector": "PHA", "side": 1}, bar))
