"""Direction label vs committed side (the "Market NEUTRAL ... BUY" contradiction)."""
from unittest import TestCase

import pandas as pd

from types import SimpleNamespace
from unittest.mock import patch

from nse_scanner.market import _complementary_pick, _direction_label, lapsed_stickiness


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
        base = dict(name=name, side=1, persist_bull=90.0, day_ret=0.9, pos=85.0, rs=0.6,
                    bull=70.0, recent=0.02, qualified=False, lag_bars=0, stale=False)
        return SimpleNamespace(**{**base, **over})

    def pick(self, plus, **env):
        from unittest.mock import patch
        from nse_scanner.market import _follow_pick
        with patch.dict("os.environ", env):
            return _follow_pick(plus)

    def test_persistent_leader_is_followed_even_between_fresh_pushes(self):
        name, reason = self.pick([self.snap(), self.snap("ENE", persist_bull=88)])
        self.assertEqual(name, "PHA")
        self.assertTrue(reason.startswith("SECTOR FOLLOW"))

    def leaders(self, plus, **env):
        from unittest.mock import patch
        from nse_scanner.market import _follow_leaders
        with patch.dict("os.environ", env):
            return [x.name for x in _follow_leaders(plus)]

    def test_next_leader_is_scanned_too_when_the_first_is_weak_or_quiet(self):
        plus = [self.snap(), self.snap("ENE", persist_bull=88), self.snap("IT", persist_bull=80)]
        self.assertEqual(self.leaders(plus), ["PHA", "ENE"])                 # two by default
        self.assertEqual(self.leaders(plus, SECTOR_FOLLOW_MAX="1"), ["PHA"])
        self.assertEqual(self.leaders(plus, SECTOR_FOLLOW_MAX="3"), ["PHA", "ENE", "IT"])
        plus[0] = self.snap(pos=40.0)                                        # #1 not leading
        self.assertEqual(self.leaders(plus), ["ENE", "IT"])

    def test_only_the_persist_top_three_can_lead(self):
        plus = [self.snap(pos=40.0), self.snap("B", pos=40.0), self.snap("C", pos=40.0), self.snap("D")]
        self.assertEqual(self.leaders(plus), [])

    def test_chart_aligned_sectors_are_scanned_first(self):
        plus = [self.snap(), self.snap("ENE", persist_bull=88)]
        self.assertEqual(self.leaders(plus, SF_ALIGNED_SECTORS="ene"), ["ENE", "PHA"])
        name, reason = self.pick(plus, SF_ALIGNED_SECTORS="ENE")
        self.assertEqual(name, "ENE")
        self.assertIn("chart aligned", reason)
        self.assertIn("also scanning PHA 90", reason)

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
        self.assertIsNone(self.pick([self.snap()], SECTOR_FOLLOW_MIN_PERSIST="95")[0])

    def test_the_floor_sits_where_17_nov_split_winners_from_losers(self):
        # 17 Nov: follow picks at persist 63 (CDUR) and 73 (FMCG) lost -2.8R
        # between them; 80 (REALTY) and 97 (PSUBK) paid +6.2R.
        self.assertIsNone(self.pick([self.snap(persist_bull=73.0)])[0])
        self.assertEqual(self.pick([self.snap(persist_bull=76.0)])[0], "PHA")

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
                self.snap("ENE", persist_bull=80)]
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


class WeakFollowTests(TestCase):
    """16 Nov 11:55: BULL at conf 41, FMCG 93 / REALTY 91 — and no path at all.

    `_complementary_pick` stands aside below conf 55, and `_follow_leaders` only
    ran with no committed side. A weak-but-directional day therefore had no way
    to reach the sector that was actually moving.
    """

    snap = staticmethod(SectorFollowTests.snap)

    def fallback(self, pick, commit_side, conf, plus, **env):
        from unittest.mock import patch
        from nse_scanner.market import _follow_fallback
        with patch.dict("os.environ", env):
            return _follow_fallback(pick, commit_side, conf, plus)

    plus = [("FMCG", 93)]

    def leaders(self):
        return [self.snap("FMCG", persist_bull=93.0, pos=88.0, rs=0.6, bull=70.0)]

    def test_the_locked_rule_is_the_default(self):
        self.assertIsNone(self.fallback(None, 1, 41, self.leaders()))

    def test_the_knob_opens_the_weak_bull_case(self):
        out = self.fallback(None, 1, 41, self.leaders(), SECTOR_FOLLOW_ON_WEAK="1")
        self.assertEqual((out["pick"], out["side"], out["action"]), ("FMCG", 1, "SCAN LONG"))
        self.assertIn("Nifty BULL weak (41%)", out["reason"])

    def test_the_neutral_case_is_unchanged(self):
        out = self.fallback(None, 0, 41, self.leaders())
        self.assertEqual(out["pick"], "FMCG")
        self.assertIn("Nifty NEUTRAL", out["reason"])

    def test_a_weak_bear_is_not_followed(self):
        # The bear mirror is not written: a weak BEAR still waits.
        self.assertIsNone(self.fallback(None, -1, 41, self.leaders(), SECTOR_FOLLOW_ON_WEAK="1"))

    def test_a_pick_is_never_overridden(self):
        self.assertIsNone(self.fallback("PHA", 1, 41, self.leaders(), SECTOR_FOLLOW_ON_WEAK="1"))

    def test_a_weak_day_demands_more_persistence_than_a_flat_one(self):
        # NEUTRAL follows at persist 75; a weak BULL only follows the exception (85).
        mid = [self.snap("PHA", persist_bull=78.0, pos=70.0, rs=0.4)]
        self.assertEqual(self.fallback(None, 0, 41, mid)["pick"], "PHA")
        self.assertIsNone(self.fallback(None, 1, 41, mid, SECTOR_FOLLOW_ON_WEAK="1"))
        strong = [self.snap("FMCG", persist_bull=93.0, pos=88.0, rs=0.6)]
        self.assertEqual(self.fallback(None, 1, 41, strong, SECTOR_FOLLOW_ON_WEAK="1")["pick"],
                         "FMCG")

    def test_the_weak_floor_can_be_moved(self):
        mid = [self.snap("PHA", persist_bull=58.0, pos=70.0, rs=0.4)]
        out = self.fallback(None, 1, 41, mid, SECTOR_FOLLOW_ON_WEAK="1",
                            SECTOR_FOLLOW_WEAK_MIN_PERSIST="55")
        self.assertEqual(out["pick"], "PHA")

    def test_the_pick_is_one_sided(self):
        # Decided 16 Nov: no two-sided pick. On a committed BULL bar the top of
        # PERSIST − (REALTY 91) is a reading, never a pick — even when the weak
        # path is on and REALTY is the more persistent of the two.
        plus = self.snap("FMCG", persist_bull=93.0, pos=88.0, rs=0.6)
        bear = self.snap("REALTY", side=-1, persist_bear=95.0, persist_bull=5.0,
                         pos=10.0, rs=-0.9, day_ret=-1.2)
        from nse_scanner.market import _follow_leaders
        self.assertEqual([x.name for x in _follow_leaders([bear, plus], 65)], ["FMCG"])
        with patch.dict("os.environ", {"SECTOR_FOLLOW_ON_WEAK": "1"}):
            out = self.fallback(None, 1, 41, [bear, plus])
        self.assertEqual((out["pick"], out["side"]), ("FMCG", 1))

    def test_a_weak_day_still_needs_a_leading_sector(self):
        dull = [self.snap("FMCG", persist_bull=52.0, pos=45.0, rs=-0.2)]
        self.assertIsNone(self.fallback(None, 1, 41, dull, SECTOR_FOLLOW_ON_WEAK="1"))

    def test_a_strong_day_is_not_affected(self):
        # conf 74 with a good sector never reaches the fallback: the picker picks.
        self.assertIsNone(self.fallback(None, 1, 74, self.leaders(), SECTOR_FOLLOW_ON_WEAK="1"))

    def test_follow_switch_still_turns_everything_off(self):
        self.assertIsNone(self.fallback(None, 0, 41, self.leaders(), SECTOR_FOLLOW="0",
                                        SECTOR_FOLLOW_ON_WEAK="1"))


class MtfLineTests(TestCase):
    """`MTF 2/3` means two things; the alert must not print an ambiguous number.

    The chart indicator prints it next to its direction row, where it reads as
    "two of three agree with DIR"; this engine only ever stored the bullish
    count, so a BEAR block printed the complement — 16 Nov 14:41 was
    `DIR BEAR 44% Weak · MTF 2/3` on the indicator and `MTF 1/3` on the block.
    """

    def test_both_counts_are_shown_when_the_timeframes_split(self):
        from nse_scanner.market import mtf_text
        self.assertEqual(mtf_text(2), "MTF 2▲/1▼")
        self.assertEqual(mtf_text(1), "MTF 1▲/2▼")     # the same tape as 2/3 on a BEAR row

    def test_a_unanimous_mtf_needs_one_number(self):
        from nse_scanner.market import mtf_text
        self.assertEqual(mtf_text(3), "MTF 3▲")
        self.assertEqual(mtf_text(0), "MTF 3▼")

    def test_the_bear_block_shows_the_two_bearish_timeframes(self):
        # The 14:41 block: DIR BEAR 44% WEAK, one timeframe bullish.
        from nse_scanner.pipeline import format_market
        md = SimpleNamespace(
            pick_side=0, dir_label="BEAR", dir_conf=44, dir_tier="WEAK", commit_side=-1,
            nifty=25217.0, nifty_day_ret=0.29, nifty_intra=0.10, vix=15.63, vix_chg=-6.75,
            adx=15, mtf_bull=1, regime="TRANSITION", breadth_adv=2, breadth_dec=13,
            persist_plus=[("FMCG", 93.0)],
            persist_minus=[("PHA", 95.0), ("MET", 94.0), ("AUTO", 90.0)],
            live_flow="", live_flow_side=0, live_flow_score=0, live_flow_qualified=False,
            stale_sectors=[], pick_sector="WAIT", pick_reason="WAIT — test", action="WAIT",
            bar_time="14:41")
        text = format_market(md)
        self.assertIn("MTF 1▲/2▼", text)             # both conventions readable
        self.assertNotIn("MTF 1/3", text)            # never the bare, sign-blind form
        self.assertIn("Breadth 2▲/13▼  TRANSITION", text)


class FlowAgainstTests(TestCase):
    """A qualified live flow on the other side is named on the pick line.

    17 Nov 12:50 picked BANK long with `Live flow DEF▼ Q (72)` three lines below
    it, and nothing said the two disagreed.
    """

    def md(self, **over):
        base = dict(pick_side=1, dir_label="STRONG BULL", dir_conf=100, dir_tier="TREND-LOCK",
                    commit_side=1, nifty=25458.1, nifty_day_ret=0.66, nifty_intra=0.71,
                    vix=15.49, vix_chg=1.16, adx=39, mtf_bull=3, regime="TREND UP",
                    breadth_adv=10, breadth_dec=5, persist_plus=[("BANK", 71.0)],
                    persist_minus=[("PSE", 66.0)], live_flow="DEF", live_flow_side=-1,
                    live_flow_score=72, live_flow_qualified=True, stale_sectors=[],
                    pick_sector="BANK", pick_reason="PERSIST + BANK qualified (score 71)",
                    action="SCAN LONG", bar_time="12:50")
        return SimpleNamespace(**{**base, **over})

    def test_the_disagreement_is_named(self):
        from nse_scanner.pipeline import format_market
        self.assertIn("live flow against (DEF)", format_market(self.md()))

    def test_an_unqualified_flow_is_only_reported(self):
        from nse_scanner.pipeline import format_market
        self.assertNotIn("live flow against", format_market(self.md(live_flow_qualified=False)))

    def test_a_flow_with_the_pick_is_not_flagged(self):
        from nse_scanner.pipeline import format_market
        self.assertNotIn("live flow against", format_market(self.md(live_flow_side=1)))


class DivergenceShortTests(TestCase):
    """Index up, breadth down — the one tape the one-sided rule cannot trade.

    16 Nov 11:55: Nifty +0.44%, breadth 5▲/10▼, PERSIST − REALTY 91. A BULL bar
    has no short pool, so nothing was picked. This is the rule that would.
    """

    snap = staticmethod(SectorFollowTests.snap)

    def short(self, commit=1, day=0.44, adv=5, dec=10, minus=None, prev=None,
              mtf=3, conf=99, **env):
        from nse_scanner.market import _divergence_short
        pool = minus if minus is not None else [self.faller()]
        with patch.dict("os.environ", {"DIVERGENCE_SHORT": "1", **env}):
            return _divergence_short(commit, day, adv, dec, pool, prev,
                                     mtf_bull=mtf, dir_conf=conf)

    def faller(self, name="REALTY", persist=91.0, **over):
        base = dict(side=-1, persist_bear=persist, day_ret=-1.20, pos=8.0, rs=-0.9,
                    bear=85.0, recent=-0.02, stale=False)
        return self.snap(name, **{**base, **over})

    def test_it_fires_on_the_16_nov_tape(self):
        out = self.short(mtf=3, conf=41)
        self.assertEqual((out["pick"], out["side"], out["action"]), ("REALTY", -1, "SCAN SHORT"))
        self.assertIn("DIVERGENCE SHORT — index up (Nifty +0.44%)", out["reason"])
        self.assertIn("breadth 5▲/10▼", out["reason"])
        self.assertIn("persist 91", out["reason"])

    def test_it_is_off_by_default(self):
        with patch.dict("os.environ", {"DIVERGENCE_SHORT": "0"}):
            from nse_scanner.market import _divergence_short
            self.assertIsNone(_divergence_short(1, 0.44, 5, 10, [self.faller()]))

    def test_the_index_has_to_be_up(self):
        self.assertIsNone(self.short(day=-0.30))          # a falling index has a short pool
        self.assertIsNone(self.short(commit=-1, day=-0.30))

    def test_a_confirmed_uptrend_is_never_shorted(self):
        # MTF 3/3, conf 85, breadth positive: the index is a real uptrend.
        self.assertIsNone(self.short(adv=8, dec=7, mtf=3, conf=85))

    def test_an_unconfirmed_uptrend_is_enough(self):
        # 16 Nov 09:30: MTF 2/3, conf 35, breadth 9▲/6▼ -> REALTY 76 was the
        # persistent faller and nothing could be shorted. This is that bar.
        out = self.short(adv=9, dec=6, mtf=2, conf=35, minus=[self.faller(persist=76.0)])
        self.assertEqual((out["pick"], out["side"]), ("REALTY", -1))
        self.assertIn("MTF 2▲/1▼", out["reason"])        # both counts, never "2/3"
        self.assertIn("conf 35%", out["reason"])
        self.assertNotIn("breadth", out["reason"])        # breadth was not the reason

    def test_a_marginal_confidence_alone_is_enough(self):
        # 16 Nov 09:35: MTF 2/3, conf 56, breadth 8▲/7▼.
        self.assertIsNotNone(self.short(adv=8, dec=7, mtf=3, conf=56))
        self.assertIsNone(self.short(adv=8, dec=7, mtf=3, conf=70))
        self.assertIsNone(self.short(adv=8, dec=7, mtf=3, conf=85))

    def test_narrow_breadth_alone_is_enough(self):
        # 16 Nov 11:55: MTF 3/3, conf 41, breadth 5▲/10▼.
        out = self.short(adv=5, dec=10, mtf=3, conf=85)
        self.assertIsNotNone(out)
        self.assertIn("breadth 5▲/10▼", out["reason"])

    def test_a_small_negative_spread_is_not_narrow_enough(self):
        self.assertIsNone(self.short(adv=7, dec=8, mtf=3, conf=85))     # 1 is noise
        self.assertIsNotNone(self.short(adv=6, dec=9, mtf=3, conf=85))  # 2 is the bar

    def test_a_marginal_faller_is_not_shorted(self):
        self.assertIsNone(self.short(minus=[self.faller(persist=58.0)]))
        self.assertIsNotNone(self.short(minus=[self.faller(persist=65.0)]))

    def test_every_mirrored_gate_matters(self):
        for bad in (dict(side=1),                  # not a short sector
                    dict(day_ret=+0.10),           # not down enough
                    dict(pos=55.0),                # middle of its range
                    dict(rs=+0.10),                # ahead of Nifty
                    dict(bear=40.0),               # flow not bearish
                    dict(recent=+0.12),            # bouncing
                    dict(stale=True)):             # index not printing
            self.assertIsNone(self.short(minus=[self.faller(**bad)]), bad)

    def test_the_floor_can_be_moved(self):
        self.assertIsNone(self.short(minus=[self.faller(persist=58.0)]))
        self.assertIsNotNone(self.short(minus=[self.faller(persist=58.0)],
                                        DIVERGE_MIN_PERSIST="55"))

    def test_the_incumbent_faller_is_held_within_the_margin(self):
        # Two persistent fallers must not trade the short back and forth.
        best = self.faller("REALTY", persist=91.0)
        near = self.faller("DEF", persist=88.0)
        out = self.short(minus=[best, near], prev={"name": "DEF", "side": -1})
        self.assertEqual(out["pick"], "DEF")
        self.assertIn("held over REALTY", out["reason"])
        far = self.faller("DEF", persist=80.0)
        self.assertEqual(self.short(minus=[best, far], prev={"name": "DEF", "side": -1})["pick"],
                         "REALTY")

    def test_a_recovered_incumbent_is_not_held(self):
        best = self.faller("REALTY", persist=91.0)
        back = self.faller("DEF", persist=88.0, recent=+0.20)     # bouncing: not a short
        self.assertEqual(self.short(minus=[best, back], prev={"name": "DEF", "side": -1})["pick"],
                         "REALTY")

    def test_no_bear_pool_is_no_trade(self):
        self.assertIsNone(self.short(minus=[]))


class ConfScaledFloorTests(TestCase):
    """The floor should rise as the confidence falls.

    16 Nov 09:35: `MARKET BULL conf 56% BUILDING`, `PERSIST + PHA 56 · FMCG 54 ·
    BANK 48` -> buy PHA at 56. The morning's persistent sector was REALTY
    (66/76/69 on PERSIST −); the long side never got above 59 and the pharma
    entries were given up within 40 minutes.
    """

    snap = staticmethod(SectorFollowTests.snap)

    def floor(self, conf, **env):
        from nse_scanner.market import persist_floor
        with patch.dict("os.environ", {"PICK_CONF_SCALED_FLOOR": "1", **env}):
            return persist_floor(conf)

    def pick(self, conf, plus, **env):
        with patch.dict("os.environ", {"PICK_CONF_SCALED_FLOOR": "1", **env}):
            return _complementary_pick(1, conf, "TRANSITION", plus, [], 0.0, 0.5)

    def test_the_flat_floor_is_the_default(self):
        from nse_scanner.market import persist_floor
        with patch.dict("os.environ", {}):
            self.assertEqual(persist_floor(56), 50.0)
        with patch.dict("os.environ", {"PICK_CONF_SCALED_FLOOR": "0"}):
            self.assertEqual(persist_floor(56), 50.0)

    def test_the_floor_scales_between_the_gate_and_a_good_confidence(self):
        self.assertEqual(self.floor(55), 65.0)          # at the gate
        self.assertEqual(self.floor(70), 50.0)          # a good day: no premium
        self.assertEqual(self.floor(90), 50.0)          # never below the base
        self.assertAlmostEqual(self.floor(56), 64.0, places=1)
        self.assertAlmostEqual(self.floor(62.5), 57.5, places=1)

    def test_a_marginal_confidence_call_needs_a_persistent_sector(self):
        # the 09:35 bar: conf 56, best long 56 -> now refused
        plus = [self.snap("PHA", persist_bull=56.0), self.snap("FMCG", persist_bull=54.0)]
        name, side, reason = self.pick(56, plus)
        self.assertIsNone(name)
        self.assertEqual(side, 0)
        self.assertIn("persist 56 < 64", reason)

    def test_the_same_sector_is_taken_on_a_confident_bar(self):
        plus = [self.snap("PHA", persist_bull=56.0)]
        name, side, _ = self.pick(74, plus)
        self.assertEqual((name, side), ("PHA", 1))

    def test_a_strong_sector_still_passes_on_a_marginal_bar(self):
        plus = [self.snap("FMCG", persist_bull=93.0)]
        self.assertEqual(self.pick(56, plus)[0], "FMCG")

    def test_the_premium_can_be_tuned(self):
        plus = [self.snap("PHA", persist_bull=56.0)]
        self.assertIsNone(self.pick(56, plus)[0])
        self.assertEqual(self.pick(56, plus, PICK_LOWCONF_MIN_PERSIST="55")[0], "PHA")

    def test_the_incumbent_is_held_against_the_same_floor(self):
        from nse_scanner.market import _sticky_pick
        inc = self.snap("PHA", persist_bull=56.0)
        near = self.snap("FMCG", persist_bull=58.0)
        with patch.dict("os.environ", {"PICK_CONF_SCALED_FLOOR": "1"}):
            held = _sticky_pick("FMCG", 1, "PERSIST + FMCG", [near, inc], [], 
                                {"name": "PHA", "side": 1}, floor=64.0)
        self.assertEqual(held[0], "FMCG")      # the incumbent is below the floor: not held


class StickyLapseNoteTests(TestCase):
    """A skipped sticky hold says so, instead of flipping the pick in silence.

    17 Nov 09:45: the early pass held REALTY 96 on the forming 09:45 candle, the
    close pass took PSUBK 97 on the same candle, and two market blocks printed
    five seconds apart with no word about why the margin had stopped applying.
    ``previous_pick`` needs the candle before ``bar``; the early pass had already
    written ``bar`` into that slot.
    """

    def test_a_scan_for_this_candle_loses_the_incumbent(self):
        scan = {"bar": "2026-11-17 09:45", "sector": "REALTY", "side": 1}
        note = lapsed_stickiness(scan, pd.Timestamp("2026-11-17 09:45"))
        self.assertIn("REALTY not held", note)
        self.assertIn("already", note)

    def test_the_candle_before_it_is_a_usable_incumbent(self):
        scan = {"bar": "2026-11-17 09:40", "sector": "REALTY", "side": 1}
        self.assertEqual(lapsed_stickiness(scan, pd.Timestamp("2026-11-17 09:45")), "")

    def test_a_wait_bar_has_nothing_to_hold(self):
        scan = {"bar": "2026-11-17 09:45", "sector": "WAIT", "side": 0}
        self.assertEqual(lapsed_stickiness(scan, pd.Timestamp("2026-11-17 09:45")), "")

    def test_an_empty_ledger_has_nothing_to_hold(self):
        self.assertEqual(lapsed_stickiness(None, pd.Timestamp("2026-11-17 09:45")), "")
