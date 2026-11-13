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
                    bull=70.0, recent=0.02, qualified=False)
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

    def test_only_the_top_persistent_sector_counts(self):
        self.assertIsNone(self.pick([self.snap(pos=40.0), self.snap("ENE")])[0])

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
        self.assertEqual(_follow_tag(SimpleNamespace(pick_reason="SECTOR FOLLOW — Nifty NEUTRAL")),
                         " (sector follow)")
        self.assertEqual(_follow_tag(SimpleNamespace(pick_reason="PERSIST + IT qualified")), "")
