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
