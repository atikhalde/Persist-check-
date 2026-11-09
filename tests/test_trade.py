"""Risk maths and close-only EMA9 trail regression tests (no market feed)."""
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import TestCase

import pandas as pd

from nse_scanner.session import IST
from nse_scanner.stock import StockSignal
from nse_scanner.trade import build_plan, ema9_on_bar, trail_broken, pivot_targets


BAR = datetime(2026, 11, 9, 10, 0, tzinfo=IST)


def candles(close):
    times = pd.date_range(BAR - timedelta(minutes=5 * (len(close) - 1)), BAR, freq="5min")
    return pd.DataFrame({"Open": close, "High": [v + 2 for v in close],
                         "Low": [v - 2 for v in close], "Close": close,
                         "Volume": [1000] * len(close)}, index=times)


def signal(side, price):
    return StockSignal("ABC", 101 if side == "BUY" else 201, "EX", side,
                       "10:00", price, 65, "Strong", 20, 0, "test")


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
