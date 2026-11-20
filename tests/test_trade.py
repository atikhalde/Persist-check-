"""Risk maths and close-only EMA9 trail regression tests (no market feed)."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

import pandas as pd

from nse_scanner.session import IST
from nse_scanner.stock import StockSignal
from nse_scanner import pipeline
from nse_scanner.trade import (PivotBasis, build_plan, ema9_on_bar, exit_buffer, pivot_basis, plan_at,
                             pivot_targets,
                             trail_broken)


BAR = datetime(2026, 11, 9, 10, 0, tzinfo=IST)


def candles(close):
    times = pd.date_range(BAR - timedelta(minutes=5 * (len(close) - 1)), BAR, freq="5min")
    return pd.DataFrame({"Open": close, "High": [v + 2 for v in close],
                         "Low": [v - 2 for v in close], "Close": close,
                         "Volume": [1000] * len(close)}, index=times)


def signal(side, price):
    return StockSignal("ABC", 101 if side == "BUY" else 201, "EX", side,
                       "10:00", price, 65, "Strong", 20, 0, "test")


class TargetFallbackTests(TestCase):
    """A broken-out name gets a measured target instead of a refusal.

    20 Nov: COFORGE, PERSISTENT, MPHASIS, LTIM and LTTS were the IT leaders the
    catch-up had just found, and all five were skipped at 09:30 with "no pivot
    target — the trade has no exit" — every one of them trading above its own
    pivots. A name past its levels is the most extended leader on the board.
    """

    def daily(self, high, low, close):
        return pd.DataFrame({"Open": [close], "High": [high], "Low": [low],
                             "Close": [close], "Volume": [1]},
                            index=[BAR - timedelta(days=3)])

    def test_a_name_above_every_pivot_gets_the_measured_floor(self):
        # A clean break above yesterday's high: every pivot is behind a 102 entry.
        prior = self.daily(101.0, 100.0, 100.5)
        df = candles([100.0] * 9 + [102.0])
        plan = build_plan(signal("BUY", 102.0), df, BAR, prior)
        self.assertIsNotNone(plan)
        self.assertIsNotNone(plan.t1)
        self.assertEqual(plan.pivot_names[0], "0.5R")
        self.assertGreaterEqual(plan.t1 - plan.entry, 0.5 * plan.risk - 0.011)
        self.assertIn("no pivot ahead — measured target", plan.skipped)

    def test_the_switch_restores_the_refusal(self):
        prior = self.daily(101.0, 100.0, 100.5)
        df = candles([100.0] * 9 + [102.0])
        with patch.dict("os.environ", {"TARGET_FALLBACK": "0"}):
            plan = build_plan(signal("BUY", 102.0), df, BAR, prior)
        self.assertIsNone(plan.t1)

    def test_a_pivot_too_close_is_still_a_refusal(self):
        # Pivots *ahead* but inside the floor: the GODREJCP case, not a breakout.
        prior = self.daily(102.0, 98.0, 100.0)
        df = candles([100.0] * 9 + [102.0])
        with patch.dict("os.environ", {"MIN_TARGET_R": "999"}):
            plan = build_plan(signal("BUY", 102.0), df, BAR, prior)
        self.assertIsNone(plan.t1)
        self.assertTrue(plan.skipped)


class TradePlanTests(TestCase):
    def test_buy_stop_unchanged_and_targets_are_next_pivots(self):
        df = candles([100.0] * 9 + [102.0])
        plan = build_plan(signal("BUY", 102.0), df, BAR, candles([100.]).set_axis([BAR - timedelta(days=3)]))
        self.assertIsNotNone(plan)
        self.assertEqual(plan.entry, float(df["Close"].iloc[-1]))
        self.assertLessEqual(Decimal(str(plan.sl)), Decimal(str(plan.ema9)) * Decimal("0.9998"))
        self.assertEqual(plan.risk, round(plan.entry - plan.sl, 2))
        self.assertEqual([plan.t1, plan.t2, plan.t3],
                         [104.0, 106.0, None])
        self.assertEqual(plan.pivot_names, ("R2", "R3", None))

    def test_sell_stop_and_targets_mirror_buy(self):
        df = candles([100.0] * 9 + [98.0])
        plan = build_plan(signal("SELL", 98.0), df, BAR, candles([100.]).set_axis([BAR - timedelta(days=3)]))
        self.assertIsNotNone(plan)
        self.assertEqual(plan.entry, float(df["Close"].iloc[-1]))
        self.assertGreaterEqual(Decimal(str(plan.sl)), Decimal(str(plan.ema9)) * Decimal("1.0002"))
        self.assertEqual([plan.t1, plan.t2, plan.t3],
                         [96.0, 94.0, None])
        self.assertEqual(plan.pivot_names, ("S2", "S3", None))

    def test_invalid_stop_or_missing_ema_does_not_create_trade(self):
        self.assertIsNone(build_plan(signal("BUY", 99.0), candles([100.] * 9 + [99.]), BAR))
        self.assertIsNone(build_plan(signal("SELL", 101.0), candles([100.] * 9 + [101.]), BAR))
        self.assertIsNone(build_plan(signal("BUY", 100.0), candles([100.] * 8), BAR))
        self.assertIsNone(build_plan(signal("BUY", 102.0), candles([100.] * 10), BAR))

    def test_trail_requires_close_not_intrabar_low_or_equality(self):
        # Low/High can hit an EMA while the candle ultimately closes safely.
        self.assertFalse(trail_broken("BUY", 100., 100.))
        self.assertFalse(trail_broken("BUY", 101., 100.))
        self.assertTrue(trail_broken("BUY", 99., 100.))
        self.assertFalse(trail_broken("SELL", 99., 100.))
        self.assertTrue(trail_broken("SELL", 101., 100.))

    def test_ema_uses_only_closed_as_of_candles(self):
        df = candles([100.] * 9 + [101.])
        future = pd.DataFrame({"Open": [200.], "High": [201.], "Low": [199.],
                               "Close": [200.], "Volume": [1000]},
                              index=pd.DatetimeIndex([BAR + timedelta(minutes=5)]))
        self.assertEqual(ema9_on_bar(df, BAR), ema9_on_bar(pd.concat([df, future]), BAR))

    def test_pivots_skip_passed_levels_and_ignore_current_and_future_daily_rows(self):
        daily = candles([100., 500., 900.]).set_axis(
            [BAR - timedelta(days=3), BAR, BAR + timedelta(days=1)])
        df = candles([100.] * 10)
        self.assertEqual(pivot_targets("BUY", 99., df, BAR, daily), [100., 102., 104.])
        self.assertEqual(pivot_targets("SELL", 101., df, BAR, daily), [100., 98., 96.])
        self.assertEqual(pivot_targets("BUY", 106., df, BAR, daily), [None] * 3)
        self.assertEqual(pivot_targets("BUY", 100., df, BAR), [None] * 3)

    def test_previous_session_intraday_fallback(self):
        previous = candles([100.] * 10)
        previous.index = previous.index - timedelta(days=3)
        df = pd.concat([previous, candles([100.] * 10)])
        self.assertEqual(pivot_targets("BUY", 100., df, BAR), [102., 104., 106.])


class PlanAtTests(TestCase):
    """``plan_at`` — the levels of a trade that has no signal on its candle.

    20 Nov, BANK: the sector was picked at 12:05 with every bank having fired at
    09:20, so the trade still available had no fire on the candle it was taken
    on. Its entry, stop and targets are that candle's, built by the same rules a
    fresh entry obeys — ``MIN_TARGET_R``, the measured fallback, the EMA9 stop.
    """

    def test_the_levels_are_the_candle_s_own(self):
        df = candles([100.0] * 9 + [102.0])
        plan = plan_at("BUY", df, BAR)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.entry, 102.0)
        self.assertLess(plan.sl, plan.entry)
        self.assertAlmostEqual(plan.ema9, ema9_on_bar(df, BAR), places=6)

    def test_the_short_side_is_mirrored(self):
        df = candles([100.0] * 9 + [98.0])
        plan = plan_at("SELL", df, BAR)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.entry, 98.0)
        self.assertGreater(plan.sl, plan.entry)

    def test_a_bar_that_is_not_the_frames_last_is_refused(self):
        df = candles([100.0] * 10)
        self.assertIsNone(plan_at("BUY", df, df.index[-2]))

    def test_a_side_that_is_not_a_trade_is_refused(self):
        self.assertIsNone(plan_at("HOLD", candles([100.0] * 10), BAR))


class TargetQualityTests(TestCase):
    """A pivot a few paise away is resistance, not a target (16 Nov feed).

    GODREJCP was entered at 877.25 with T1 at 878.00 — 0.2R, +0.09% — and its
    "TARGET HIT +0.2R" was announced as a win. The fixture below is the same
    shape: basis H 102 / L 98 / C 100 gives R1 = 102, and entry 101.99 is 1 paisa
    under it, so R1 is 0.006R away and R2 104.00 is the first real target.
    """

    def _plan(self, *, entry=101.99, last=101.99, env=None):
        df = candles([100.0] * 9 + [last])
        daily = candles([100.0]).set_axis([BAR - timedelta(days=3)])
        with patch.dict("os.environ", env or {}, clear=False):
            return build_plan(signal("BUY", entry), df, BAR, daily)

    def test_a_level_inside_the_floor_is_skipped_and_the_ladder_moves(self):
        plan = self._plan(env={"MIN_TARGET_R": "0.5", "MIN_TARGET_PCT": "0.10"})
        self.assertEqual(plan.t1, 104.0)                     # R2, not the R1 2 paise away
        self.assertEqual(plan.pivot_names, ("R2", "R3", None))
        self.assertEqual(plan.skipped, ("R1 102.00 (0.0R, +0.01%)",))
        text = pipeline._target_text(plan)
        self.assertIn("skipped as too close to entry: R1 102.00 (0.0R, +0.01%)", text)

    def test_the_skipped_line_carries_no_raw_angle_bracket(self):
        # 19 Nov: "(< 0.5R)" made Telegram answer "can't parse entities:
        # Unsupported start tag" and every alert with a skipped pivot fell back
        # to plain text. The message is HTML, so the text must not contain "<".
        plan = self._plan(env={"MIN_TARGET_R": "0.5", "MIN_TARGET_PCT": "0.10"})
        text = pipeline._target_text(plan)
        self.assertIn("(under 0.5R)", text)
        self.assertNotIn("< 0.5R", text)
        self.assertNotIn("<", text.replace("<b>", "").replace("</b>", ""))

    def test_every_announced_target_is_at_least_the_floor_away(self):
        plan = self._plan(env={"MIN_TARGET_R": "0.5", "MIN_TARGET_PCT": "0.10"})
        for level in (plan.t1, plan.t2, plan.t3):
            if level is not None:
                self.assertGreaterEqual((level - plan.entry) / plan.risk, 0.5 - 1e-9)

    def test_the_floor_can_be_switched_off(self):
        plan = self._plan(env={"MIN_TARGET_R": "0", "MIN_TARGET_PCT": "0"})
        self.assertEqual(plan.skipped, ())
        self.assertEqual(plan.t1, 102.0)                     # the old behaviour: R1, 0.006R
        self.assertNotIn("skipped", pipeline._target_text(plan))


class ExitBufferTests(TestCase):
    """A close 4 paise past EMA9 must not end a trade (VBL, 16 Nov)."""

    def test_a_shallow_breach_holds(self):
        with patch.dict("os.environ", {"EXIT_EMA9_BUFFER_PCT": "0.10",
                                       "EXIT_EMA9_BUFFER_R": "0.25"}):
            self.assertFalse(trail_broken("BUY", 433.96, 434.00, risk=1.97))   # 4 paise
            self.assertTrue(trail_broken("BUY", 433.00, 434.00, risk=1.97))    # deep
            self.assertFalse(trail_broken("SELL", 434.04, 434.00, risk=1.97))
            self.assertTrue(trail_broken("SELL", 435.00, 434.00, risk=1.97))

    def test_the_band_is_the_smaller_of_pct_and_risk(self):
        with patch.dict("os.environ", {"EXIT_EMA9_BUFFER_PCT": "0.10",
                                       "EXIT_EMA9_BUFFER_R": "0.25"}):
            self.assertAlmostEqual(exit_buffer(1841.60, 14.81), 1.84160, places=4)   # 0.10%
            self.assertAlmostEqual(exit_buffer(1841.60, 2.0), 0.50, places=4)        # 0.25R

    def test_the_buffer_can_be_switched_off(self):
        with patch.dict("os.environ", {"EXIT_EMA9_BUFFER_PCT": "0",
                                       "EXIT_EMA9_BUFFER_R": "0"}):
            self.assertTrue(trail_broken("BUY", 433.99, 434.00, risk=1.97))


# ---------------------------------------------------------------------------
# Pivot basis: the levels must be traceable to a real completed session.
# 11 Nov 2026: NSE was shut on 10 Nov (Diwali-Balipratipada), so the previous
# session was Monday 09 Nov 2026 with O 125.99 / H 132.48 / L 125.92 / C 132.17.
# That is the session behind the reported "T1 R2 136.75 · T2 R3 141.02" alert.
# ---------------------------------------------------------------------------
PNB_DAY = date(2026, 11, 9)
PNB_BAR = datetime(2026, 11, 11, 10, 25, tzinfo=IST)


def daily_bars(rows):
    idx = pd.DatetimeIndex([datetime.combine(d, time(0, 0), tzinfo=IST) for d, *_ in rows])
    return pd.DataFrame({"Open": [r[3] for r in rows], "High": [r[1] for r in rows],
                         "Low": [r[2] for r in rows], "Close": [r[3] for r in rows],
                         "Volume": [1000] * len(rows)}, index=idx)


def history(*, basis, prior_days=25, prior_range_pct=2.5, start=date(2026, 9, 1)):
    rows, d = [], start
    while len(rows) < prior_days:
        if d.weekday() < 5 and d < basis[0]:
            c = 130.0
            rows.append((d, c * (1 + prior_range_pct / 200), c * (1 - prior_range_pct / 200), c))
        d += timedelta(days=1)
    return daily_bars(rows + [basis])


def pnb_bars(n=12):
    """11 Nov 2026 5m bars up to the reported 10:25 candle (close 135.73)."""
    closes = [134.90 + (135.73 - 134.90) * i / (n - 1) for i in range(n)]
    times = [PNB_BAR - timedelta(minutes=5 * (n - 1 - i)) for i in range(n)]
    return pd.DataFrame({"Open": closes, "High": [c + 0.05 for c in closes],
                         "Low": [c - 0.05 for c in closes], "Close": closes,
                         "Volume": [1000] * n}, index=pd.DatetimeIndex(times))


class PivotBasisTests(TestCase):
    def test_reported_pnb_alert_levels_are_reproduced_exactly(self):
        daily = history(basis=(PNB_DAY, 132.48, 125.92, 132.17))
        self.assertEqual(pivot_targets("BUY", 135.73, pnb_bars(), PNB_BAR, daily),
                         [136.75, 141.02, None])
        basis = pivot_basis(pnb_bars(), PNB_BAR, daily)
        self.assertEqual(basis.day, PNB_DAY)
        self.assertEqual((basis.high, basis.low, basis.close), (132.48, 125.92, 132.17))
        self.assertEqual(basis.source, "1d")
        self.assertAlmostEqual(basis.range_pct, 4.9633, places=3)

    def test_every_level_matches_the_documented_classic_formula(self):
        daily = history(basis=(PNB_DAY, 132.48, 125.92, 132.17))
        basis = pivot_basis(pnb_bars(), PNB_BAR, daily)
        h, l, c = basis.high, basis.low, basis.close
        pp = (h + l + c) / 3
        self.assertAlmostEqual(2 * pp - l, 134.46, places=2)          # R1
        self.assertAlmostEqual(pp + (h - l), 136.75, places=2)        # R2
        self.assertAlmostEqual(h + 2 * (pp - l), 141.02, places=2)    # R3
        # The jump that looks wrong is structural: R3 - R2 == PP - L.
        self.assertAlmostEqual(141.02 - 136.75, pp - l, places=2)

    def test_wide_basis_day_is_flagged_and_normal_day_is_not(self):
        outlier = pivot_basis(pnb_bars(), PNB_BAR,
                              history(basis=(PNB_DAY, 132.48, 125.92, 132.17),
                                      prior_range_pct=1.5))
        self.assertTrue(outlier.wide)                     # 4.96% vs 1.50% median
        self.assertGreater(outlier.range_multiple, 3.0)
        normal = pivot_basis(pnb_bars(), PNB_BAR,
                             history(basis=(PNB_DAY, 131.0, 129.0, 130.0),
                                     prior_range_pct=2.5))
        self.assertFalse(normal.wide)

    def test_no_verdict_without_enough_history(self):
        daily = daily_bars([(PNB_DAY, 132.48, 125.92, 132.17)])
        basis = pivot_basis(pnb_bars(), PNB_BAR, daily)
        self.assertIsNone(basis.median_range_pct)
        self.assertFalse(basis.wide)

    def test_stale_basis_is_refused_instead_of_inventing_levels(self):
        old = daily_bars([(date(2026, 9, 1), 132.48, 125.92, 132.17)])
        self.assertIsNone(pivot_basis(pnb_bars(), PNB_BAR, old))
        self.assertEqual(pivot_targets("BUY", 135.73, pnb_bars(), PNB_BAR, old), [None] * 3)

    def test_daily_feed_wins_over_disagreeing_5m_aggregate(self):
        daily = history(basis=(PNB_DAY, 132.48, 125.92, 132.17))
        stale_5m = pd.concat([
            pd.DataFrame({"Open": [125.99], "High": [140.0], "Low": [119.0], "Close": [132.17],
                          "Volume": [1000]},
                         index=pd.DatetimeIndex([datetime(2026, 11, 9, 15, 25, tzinfo=IST)])),
            pnb_bars(),
        ])
        with self.assertLogs("nse_scanner", level="WARNING") as logs:
            basis = pivot_basis(stale_5m, PNB_BAR, daily)
        self.assertEqual(basis.source, "1d")
        self.assertEqual((basis.high, basis.low), (132.48, 125.92))
        self.assertIn("disagree", "\n".join(logs.output))

    def test_single_row_5m_frame_does_not_break_grouping(self):
        # pandas 3 grouped a one-element key list into a 1-tuple and raised
        # TypeError inside build_plan, killing the whole cycle.
        one = pnb_bars().iloc[-1:]
        self.assertEqual(pivot_targets("BUY", 135.73, one, PNB_BAR,
                                       history(basis=(PNB_DAY, 132.48, 125.92, 132.17))),
                         [136.75, 141.02, None])

    def test_build_plan_carries_the_basis_for_the_alert(self):
        daily = history(basis=(PNB_DAY, 132.48, 125.92, 132.17))
        plan = build_plan(signal("BUY", 135.73), pnb_bars(), PNB_BAR, daily)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.basis.day, PNB_DAY)
        self.assertEqual((plan.t1, plan.t2, plan.t3), (136.75, 141.02, None))

    def test_basis_quality_flags_special_and_compressed_sessions(self):
        # Sun 08 Nov 2026 was a ~1h Muhurat session: a legitimate but crushed basis.
        special = PivotBasis(date(2026, 11, 8), 133.10, 132.60, 132.85, "5m", 0.38, 2.26)
        self.assertTrue(special.special)
        self.assertTrue(special.compressed)
        self.assertFalse(special.wide)
        normal = PivotBasis(date(2026, 11, 9), 132.48, 125.92, 132.17, "1d", 4.9633, 2.26)
        self.assertFalse(normal.special)
        self.assertFalse(normal.compressed)
        self.assertFalse(normal.wide)       # 2.19x — under the 2.5x outlier gate
