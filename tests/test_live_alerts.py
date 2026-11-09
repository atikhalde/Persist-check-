"""Regression tests for late/repeated Telegram entries (no network access)."""
from datetime import datetime, timedelta
from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import tempfile
from pathlib import Path

import pandas as pd

from nse_scanner import pipeline, state
from nse_scanner.session import IST
from nse_scanner.stock import StockSignal
from nse_scanner.trade import TradePlan

ORIGINAL_FORMAT_STOCK = pipeline.format_stock

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
        self.times = [BAR - timedelta(minutes=5 * i) for i in range(39, 0, -1)] + [BAR, BAR + timedelta(minutes=5)]
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

    def test_message_distinguishes_bar_open_close_and_calculation_time(self):
        plan = TradePlan(100.0, 100.0, 99.98, 0.02, 100.02, 100.04, 100.06, ("R1", "R2", "R3"))
        text = ORIGINAL_FORMAT_STOCK(signal(), self.md, plan)
        self.assertIn("Entry (candle close) <b>100.00</b>", text)
        self.assertIn("\nSL 99.98 (0.02% below EMA9)\n", text)
        self.assertNotIn("Entry EMA9", text)
        self.assertIn("<b>BUY</b> | <b>BUY-EX</b>", text)
        self.assertNotIn("Targets are reference levels", text)
        self.assertNotIn("Trail through pivot targets", text)
        self.assertIn("next pivot target: T1 R1 100.02 · T2 R2 100.04 · T3 R3 100.06", text)
        self.assertIn("5m bar 10:00–10:05 IST", text)
        self.assertIn("Signal calculated 10:06:00 IST", text)

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

    def test_mixed_old_and_current_fires_only_send_current(self):
        # Nov 6's KALYANKJIL @09:20 and TITAN @09:30 were both sent at
        # 09:35 in the same run. Only the current bar's fire is an entry.
        self.scan.return_value = [signal("09:20", code=90), signal("10:00", code=101)]
        self.assertEqual([s["time"] for s in pipeline.run_once()["signals"]], ["10:00"])
        self.assertEqual(self.send.call_count, 2)

    def test_backfill_is_diagnostic_only(self):
        with self.assertRaisesRegex(ValueError, "--dry-run"):
            pipeline.run_once(backfill=True)
        self.send.assert_not_called()
        with patch.dict("os.environ", {"BACKFILL": "1"}):
            with self.assertRaisesRegex(ValueError, "--dry-run"):
                pipeline.run_once()
        self.send.assert_not_called()

    def test_corrupt_state_fails_closed_instead_of_repeating(self):
        state.STATE_PATH.write_text('{"session": "2026-11-09", "sent": ', encoding="utf-8")
        with self.assertRaisesRegex(state.InvalidStateError, "Invalid scanner state"):
            pipeline.run_once()
        self.send.assert_not_called()
        with patch.object(pipeline.time, "sleep") as sleep:
            with self.assertRaises(state.InvalidStateError):
                pipeline.run_until_ready(expected_bar=BAR)
        sleep.assert_not_called()

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
        self.assertEqual(self.scan.call_args.args[1].index[-1], self.times[-3])
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

    def test_trailing_exit_on_close_even_if_sector_becomes_wait(self):
        entry = pipeline.run_once()
        self.assertEqual(entry["signals"][0]["price"], 100.0)
        self.assertEqual(entry["signals"][0]["sl"], 99.98)
        self.assertIn("ABC", state.load()["positions"])

        self.now = BAR + timedelta(minutes=11)
        self.md.pick_sector, self.md.action, self.md.pick_side = "WAIT", "WAIT", 0
        trailing = bars(*self.times)
        trailing.loc[self.times[-1], "Close"] = 99.0
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: trailing.copy() if t == "ABC.NS" else bars(*self.times) for t in tickers}
        result = pipeline.run_once()
        self.assertEqual(result["action"], "WAIT")
        exits = [c.args[0] for c in self.send.call_args_list if "EMA9 TRAIL EXIT" in c.args[0]]
        self.assertEqual(len(exits), 1)
        self.assertIn("Close 99.00", exits[0])
        self.assertIn("10:05–10:10 IST", exits[0])
        self.assertNotIn("ABC", state.load()["positions"])
        pipeline.run_once()  # same bar must not send the exit again
        self.assertEqual(sum("EMA9 TRAIL EXIT" in c.args[0] for c in self.send.call_args_list), 1)

    def test_trail_exit_works_when_nifty_feed_is_stale(self):
        pipeline.run_once()
        self.now = BAR + timedelta(minutes=11)
        trailing = bars(*self.times)
        trailing.loc[self.times[-1], "Close"] = 99.0
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: trailing.copy() if t == "ABC.NS" else bars(*self.times) for t in tickers}
        self.mocks[2].side_effect = lambda *args: {
            "m5": bars(*self.times[:-1]), "d1": bars(BAR - timedelta(days=1))}
        result = pipeline.run_once()
        self.assertEqual(result["market"], "STALE")
        self.assertNotIn("ABC", state.load()["positions"])
        self.assertEqual(sum("EMA9 TRAIL EXIT" in c.args[0] for c in self.send.call_args_list), 1)

    def test_trail_exit_survives_nifty_download_failure(self):
        pipeline.run_once()
        self.now = BAR + timedelta(minutes=11)
        trailing = bars(*self.times)
        trailing.loc[self.times[-1], "Close"] = 99.0
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: trailing.copy() if t == "ABC.NS" else bars(*self.times) for t in tickers}
        self.mocks[2].side_effect = lambda *args: {"m5": bars(), "d1": bars(BAR - timedelta(days=1))}
        with self.assertRaisesRegex(RuntimeError, "No Nifty"):
            pipeline.run_once()
        self.assertNotIn("ABC", state.load()["positions"])
        self.assertEqual(sum("EMA9 TRAIL EXIT" in c.args[0] for c in self.send.call_args_list), 1)

    def test_sell_mirrors_stop_targets_and_ema_exit(self):
        self.md.pick_side, self.md.dir_state, self.md.dir_label = -1, -1, "BEAR"
        self.scan.return_value = [signal(side="SELL", code=201)]
        entry = pipeline.run_once()["signals"][0]
        self.assertEqual((entry["price"], entry["sl"], entry["t1"], entry["t2"], entry["t3"]),
                         (100.0, 100.02, None, None, None))
        self.now = BAR + timedelta(minutes=11)
        self.md.pick_sector, self.md.action, self.md.pick_side = "WAIT", "WAIT", 0
        trailing = bars(*self.times)
        trailing.loc[self.times[-1], "Close"] = 101.0
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: trailing.copy() if t == "ABC.NS" else bars(*self.times) for t in tickers}
        pipeline.run_once()
        self.assertNotIn("ABC", state.load()["positions"])
        self.assertTrue(any("closed above EMA9" in c.args[0] for c in self.send.call_args_list))

    def test_passing_all_pivot_targets_does_not_exit_before_ema_close(self):
        pipeline.run_once()
        st = state.load()
        st["positions"]["ABC"].update(t1=100.1, t2=100.2, t3=100.3)
        state.save(st)
        self.now = BAR + timedelta(minutes=11)
        trailing = bars(*self.times)
        trailing.loc[self.times[-1], "Close"] = 110.0
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: trailing.copy() if t == "ABC.NS" else bars(*self.times) for t in tickers}
        pipeline.run_once()
        self.assertIn("ABC", state.load()["positions"])
        self.assertFalse(any("TRAIL EXIT" in c.args[0] for c in self.send.call_args_list))

    def test_trail_ignores_wick_and_waits_for_close(self):
        pipeline.run_once()
        self.now = BAR + timedelta(minutes=11)
        self.md.pick_sector, self.md.action, self.md.pick_side = "WAIT", "WAIT", 0
        trailing = bars(*self.times)
        trailing.loc[self.times[-1], ["Low", "Close"]] = [90.0, 101.0]
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: trailing.copy() if t == "ABC.NS" else bars(*self.times) for t in tickers}
        pipeline.run_once()
        self.assertIn("ABC", state.load()["positions"])
        self.assertEqual(state.load()["positions"]["ABC"]["last_bar"],
                         self.times[-1].isoformat())
        self.assertFalse(any("EMA9 TRAIL EXIT" in c.args[0] for c in self.send.call_args_list))

    def test_failed_exit_remains_open_for_retry(self):
        pipeline.run_once()
        self.now = BAR + timedelta(minutes=11)
        self.md.pick_sector, self.md.action, self.md.pick_side = "WAIT", "WAIT", 0
        trailing = bars(*self.times)
        trailing.loc[self.times[-1], "Close"] = 99.0
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: trailing.copy() if t == "ABC.NS" else bars(*self.times) for t in tickers}
        self.send.side_effect = [False, True, True]  # exit fails; market succeeds; exit retries
        self.assertIn("ABC", state.load()["positions"])
        pipeline.run_once()
        self.assertIn("ABC", state.load()["positions"])
        pipeline.run_once()
        self.assertNotIn("ABC", state.load()["positions"])

    def test_new_ist_session_resets_dedupe(self):
        previous = state.reset_if_new_day({}, "2026-11-06")
        previous["sent"]["ABC"] = {"code": 101}
        state.save(previous)
        self.assertEqual(len(pipeline.run_once()["signals"]), 1)
        self.assertEqual(state.load()["session"], "2026-11-09")

    def test_new_session_keeps_open_trail_but_resets_entry_dedupe(self):
        pipeline.run_once()
        before = state.load()
        after = state.reset_if_new_day(before, "2026-11-10")
        self.assertEqual(after["sent"], {})
        self.assertEqual(after["positions"], before["positions"])

    def test_trail_continues_next_ist_session(self):
        pipeline.run_once()
        next_bar = datetime(2026, 11, 10, 9, 15, tzinfo=IST)
        self.now = next_bar + timedelta(minutes=6)
        self.md.pick_sector, self.md.action, self.md.pick_side = "WAIT", "WAIT", 0
        index = bars(*self.times, next_bar)
        stock = index.copy()
        stock.loc[next_bar, "Close"] = 99.0
        self.mocks[2].side_effect = lambda *args: {"m5": index, "d1": bars(BAR - timedelta(days=1))}
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: stock.copy() if t == "ABC.NS" else index.copy() for t in tickers}
        pipeline.run_once()
        self.assertNotIn("ABC", state.load()["positions"])
        self.assertEqual(state.load()["session"], "2026-11-10")
        self.assertEqual(state.load()["sent"]["ABC"]["code"], "EXIT")
        self.assertEqual(sum("EMA9 TRAIL EXIT" in c.args[0] for c in self.send.call_args_list), 1)

    def test_workflow_retries_until_target_bar_is_available(self):
        # External workflow_dispatch fires seconds after a close. Yahoo may
        # still have only the preceding candle on the first fetch.
        self.now = BAR + timedelta(minutes=5, seconds=10)
        target = BAR.strftime("%Y-%m-%d %H:%M")
        with (patch.object(pipeline, "run_once", side_effect=[
                {"bar": "2026-11-09 09:55", "action": "WAIT"},
                {"bar": target, "action": "SCAN LONG"}]) as once,
              patch.object(pipeline.time, "sleep") as sleep):
            out = pipeline.run_until_ready(expected_bar=BAR)
        self.assertEqual(out["bar"], target)
        self.assertEqual(once.call_count, 2)
        sleep.assert_called_once_with(15)

    def test_workflow_retries_when_no_stock_bar_was_published(self):
        self.now = BAR + timedelta(minutes=5, seconds=10)
        target = BAR.strftime("%Y-%m-%d %H:%M")
        with (patch.object(pipeline, "run_once", side_effect=[
                {"bar": target, "action": "PENDING"},
                {"bar": target, "action": "SCAN LONG"}]) as once,
              patch.object(pipeline.time, "sleep") as sleep):
            self.assertEqual(pipeline.run_until_ready(expected_bar=BAR)["action"], "SCAN LONG")
        self.assertEqual(once.call_count, 2)
        sleep.assert_called_once()

    def test_partial_stock_publication_retries_without_duplicate_alerts(self):
        self.now = BAR + timedelta(minutes=5, seconds=10)
        self.uni["sectors"]["IT"]["stocks"] = ["ABC", "DEF"]
        self.scan.side_effect = lambda raw, *args, **kw: [replace(signal(), symbol=raw)]
        calls = 0

        def stock_data(tickers, period, interval):
            nonlocal calls
            if interval == "5m" and "DEF.NS" in tickers:
                calls += 1
                return {t: bars(*(self.times[:1] if t == "DEF.NS" and calls == 1 else self.times))
                        for t in tickers}
            return {t: bars(*self.times) for t in tickers}

        self.fetch_many.side_effect = stock_data
        with patch.object(pipeline.time, "sleep") as sleep:
            result = pipeline.run_until_ready(expected_bar=BAR)
        self.assertEqual(calls, 2)
        self.assertEqual([s["symbol"] for s in result["signals"]], ["ABC", "DEF"])
        self.assertEqual(self.send.call_count, 3)  # market, ABC, DEF (no replay)
        self.assertEqual(set(state.load()["sent"]), {"ABC", "DEF"})
        sleep.assert_called_once()

    def test_expired_workflow_does_not_wait_or_backfill(self):
        self.now = BAR + timedelta(minutes=9)
        with (patch.object(pipeline, "run_once", return_value={"market": "STALE"}) as once,
              patch.object(pipeline.time, "sleep") as sleep):
            self.assertEqual(pipeline.run_until_ready(expected_bar=BAR)["market"], "STALE")
        once.assert_called_once_with(force_sector=None)
        sleep.assert_not_called()
