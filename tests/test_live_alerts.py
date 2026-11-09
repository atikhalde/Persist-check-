"""Regression tests for late/repeated Telegram entries (no network access)."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import tempfile
from pathlib import Path

import pandas as pd

from nse_scanner import pipeline, state
from nse_scanner.session import IST
from nse_scanner.stock import StockSignal


BAR = datetime(2026, 11, 9, 10, 0, tzinfo=IST)  # open; closes at 10:05


def bars(*times):
    return pd.DataFrame({"Open": [100] * len(times), "High": [101] * len(times),
                         "Low": [99] * len(times), "Close": [100] * len(times),
                         "Volume": [1000] * len(times)}, index=pd.DatetimeIndex(times))


def signal(time="10:00", code=101, side="BUY"):
    return StockSignal("ABC", code, "BUY-EX", side, time, 100.0, 65,
                       "Strong", 20, 1, "test", 99.0)


class LiveAlertTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = patch.object(state, "STATE_PATH", Path(self.tmp.name) / "state.json")
        self.ctx.start()
        self.addCleanup(self.ctx.stop)
        self.ctx = patch.dict("os.environ", {"MAX_ALERT_LAG_SECONDS": "180", "DRY_RUN": "0"})
        self.ctx.start()
        self.addCleanup(self.ctx.stop)
        self.now = BAR + timedelta(minutes=6)
        self.times = [BAR - timedelta(minutes=5), BAR, BAR + timedelta(minutes=5)]
        self.uni = {"nifty": "NIFTY", "vix": "VIX",
                    "sectors": {"IT": {"index": "INDEX", "stocks": ["ABC"]}}}
        self.md = SimpleNamespace(bar_time=BAR.strftime("%Y-%m-%d %H:%M"),
                                  dir_state=1, dir_label="BULL", dir_conf=74,
                                  pick_sector="IT", pick_side=1,
                                  pick_reason="test", action="SCAN LONG", regime="TREND")
        self.contexts = [
            patch.object(pipeline, "now_ist", side_effect=lambda: self.now),
            patch.object(pipeline.data, "load_universe", return_value=self.uni),
            patch.object(pipeline.data, "fetch_symbol", side_effect=lambda *args: {
                "m5": bars(*self.times), "d1": bars(BAR - timedelta(days=1))}),
            patch.object(pipeline.data, "fetch_many", side_effect=lambda tickers, period, interval: {
                t: bars(*self.times) for t in tickers}),
            patch.object(pipeline.data, "apply_1m_volume", side_effect=lambda df, _: df),
            patch.object(pipeline, "run_market", side_effect=self.market_at_bar),
            patch.object(pipeline, "format_market", return_value="market"),
            patch.object(pipeline, "format_stock", return_value="stock"),
            patch.object(pipeline, "scan_stock", return_value=[signal()]),
            patch.object(pipeline.telegram, "send", return_value=True),
        ]
        self.mocks = [p.start() for p in self.contexts]
        for p in self.contexts:
            self.addCleanup(p.stop)
        self.fetch_many = self.mocks[3]
        self.scan = self.mocks[8]
        self.send = self.mocks[9]

    def market_at_bar(self, df, *args):
        self.md.bar_time = df.index[-1].strftime("%Y-%m-%d %H:%M")
        return self.md

    def test_only_closed_candle_used_and_only_once_per_day(self):
        first = pipeline.run_once()
        self.assertEqual(len(first["signals"]), 1)
        self.assertEqual(self.scan.call_args.args[1].index[-1], BAR)
        self.assertTrue(self.scan.call_args.kwargs["keep_all"])
        self.assertEqual([call.args[0] for call in self.send.call_args_list], ["market", "stock"])
        second = pipeline.run_once()
        self.assertEqual(second["signals"], [])
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(state.load()["sent"]["ABC"]["time"], "10:00")

    def test_rank_upgrade_is_not_a_second_entry(self):
        pipeline.run_once()
        self.scan.return_value = [signal(code=120)]
        self.assertEqual(pipeline.run_once()["signals"], [])
        self.assertEqual(self.send.call_count, 2)

    def test_historical_table_row_is_never_sent(self):
        self.scan.return_value = [signal("09:50")]
        self.assertEqual(pipeline.run_once()["signals"], [])
        self.assertEqual([call.args[0] for call in self.send.call_args_list], ["market"])

    def test_late_run_does_not_download_or_send(self):
        self.now = BAR + timedelta(minutes=9)
        result = pipeline.run_once()
        self.assertEqual(result["market"], "STALE")
        self.fetch_many.assert_not_called()
        self.send.assert_not_called()

    def test_current_bar_unpublished_is_not_processed(self):
        self.now = BAR + timedelta(minutes=1)
        result = pipeline.run_once()
        self.assertEqual(result["bar"], "2026-11-09 09:55")
        self.assertEqual(self.scan.call_args.args[1].index[-1], self.times[0])
        self.assertNotIn("stock", [c.args[0] for c in self.send.call_args_list])

    def test_stock_bar_missing_is_not_replayed(self):
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: bars(*(self.times[:-2] if t == "ABC.NS" else self.times)) for t in tickers}
        result = pipeline.run_once()
        self.assertEqual(result["signals"], [])
        self.assertEqual(result["action"], "PENDING")
        self.scan.assert_not_called()

    def test_failed_send_does_not_mark_stock_sent(self):
        self.send.side_effect = [True, False, True]
        self.assertEqual(pipeline.run_once()["signals"], [])
        self.assertEqual(pipeline.run_once()["signals"][0]["symbol"], "ABC")
        self.assertEqual(self.send.call_count, 3)

    def test_dry_run_does_not_consume_real_state(self):
        with patch.dict("os.environ", {"DRY_RUN": "1"}):
            pipeline.run_once()
        self.assertEqual(state.load(), {})

    def test_expired_during_fetch_does_not_send(self):
        def slow_fetch(tickers, period, interval):
            self.now = BAR + timedelta(minutes=9)
            return {t: bars(*self.times) for t in tickers}
        self.fetch_many.side_effect = slow_fetch
        self.assertEqual(pipeline.run_once()["signals"], [])
        self.send.assert_not_called()

    def test_new_ist_session_resets_dedupe(self):
        previous = state.reset_if_new_day({}, "2026-11-06")
        previous["sent"]["ABC"] = {"code": 101}
        state.save(previous)
        self.assertEqual(len(pipeline.run_once()["signals"]), 1)
        self.assertEqual(state.load()["session"], "2026-11-09")
