"""Early (pre-close) alerts and the always-on loop — no network access."""
from datetime import timedelta
from unittest import TestCase
from unittest.mock import patch

import pandas as pd

from nse_scanner import data, pipeline, state
import test_live_alerts as base

BAR, bars, quiet_bars, signal = base.BAR, base.bars, base.quiet_bars, base.signal

PREV = (BAR - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")


def minute_bars(*offsets, close=100.0):
    idx = pd.DatetimeIndex([BAR + timedelta(minutes=m) for m in offsets])
    return pd.DataFrame({"Open": close, "High": close + 0.01, "Low": close - 0.01,
                         "Close": close, "Volume": 250.0}, index=idx)


class ProvisionalBarTests(TestCase):
    def setUp(self):
        self.done = quiet_bars(*[BAR - timedelta(minutes=5 * i) for i in range(5, 0, -1)])
        self.now = BAR + timedelta(minutes=4, seconds=5)

    def test_forming_candle_is_rebuilt_from_one_minute_bars(self):
        m1 = minute_bars(0, 1, 2, 3)
        m1.loc[m1.index[1], "High"] = 101.5
        m1.loc[m1.index[2], "Low"] = 99.2
        m1.loc[m1.index[3], "Close"] = 100.7
        # A forming 5m row from Yahoo must be ignored in favour of the 1m build.
        raw5 = pd.concat([self.done, quiet_bars(BAR, close=555.0, high=556, low=554)])
        out = data.provisional_5m(raw5, m1, BAR, self.now)
        self.assertEqual(out.index[-1], BAR)
        self.assertEqual(len(out), len(self.done) + 1)
        row = out.iloc[-1]
        self.assertEqual((row.Open, row.High, row.Low, row.Close, row.Volume),
                         (100.0, 101.5, 99.2, 100.7, 1000.0))

    def test_stale_or_missing_minutes_never_produce_a_guess(self):
        self.assertIsNone(data.provisional_5m(self.done, minute_bars(0), BAR, self.now))  # 245s old
        self.assertIsNone(data.provisional_5m(self.done, minute_bars(-3, -2), BAR, self.now))
        self.assertIsNone(data.provisional_5m(self.done, None, BAR, self.now))
        self.assertIsNone(data.provisional_5m(None, minute_bars(3), BAR, self.now))

    def test_next_candles_minutes_are_excluded(self):
        out = data.provisional_5m(self.done, minute_bars(3, 4, 5, 6), BAR,
                                  BAR + timedelta(minutes=4, seconds=30))
        self.assertEqual(out.iloc[-1].Volume, 500.0)


class DailyCacheTests(TestCase):
    def setUp(self):
        self.addCleanup(data.enable_daily_cache, False)
        today = pd.Timestamp.now(tz=data.IST).normalize()
        self.frame = bars(today - pd.Timedelta(days=2), today - pd.Timedelta(days=1), today)

    def test_off_by_default_and_memoised_when_enabled(self):
        with patch.object(data, "fetch_many", side_effect=lambda t, p, i: {x: self.frame for x in t}) as fm:
            data.fetch_daily(["A"], "90d")
            data.fetch_daily(["A"], "90d")
            self.assertEqual(fm.call_count, 2)          # one-shot runs: unchanged behaviour
            data.enable_daily_cache()
            first = data.fetch_daily(["A", "B"], "90d")
            data.fetch_daily(["A", "B"], "90d")
            data.fetch_daily(["B", "C"], "90d")
            self.assertEqual(fm.call_count, 4)          # A+B once, then only C
            self.assertEqual(fm.call_args.args[0], ["C"])
            self.assertEqual(len(first["A"]), 2)        # today's forming daily row dropped

    def test_failed_download_is_not_cached(self):
        data.enable_daily_cache()
        with patch.object(data, "fetch_many", side_effect=[{"A": pd.DataFrame()}, {"A": self.frame}]) as fm:
            self.assertTrue(data.fetch_daily(["A"], "90d")["A"].empty)
            self.assertFalse(data.fetch_daily(["A"], "90d")["A"].empty)
            self.assertEqual(fm.call_count, 2)


class EarlyAlertTests(TestCase):
    market_at_bar = base.LiveAlertTests.market_at_bar

    def setUp(self):
        base.LiveAlertTests.setUp(self)
        self.settled.stop()                                       # real settle rules here
        self.addCleanup(self.settled.start)                       # keep the base cleanup balanced
        self.early_now = BAR + timedelta(minutes=4, seconds=2)   # 58s before the close
        self.now = self.early_now
        self.hist = self.times[:-2]                               # completed bars before BAR
        self.fetch_many.side_effect = self.frames
        self.m1 = minute_bars(0, 1, 2, 3)
        state.save({"session": str(BAR.date()), "market": {}, "sector": {}, "alerts": {},
                    "sent": {}, "positions": {},
                    "scan": {"bar": PREV, "sector": "IT", "side": 1, "label": "BULL", "conf": 74}})

    def frames(self, tickers, period, interval):
        if interval == "1m":
            return {t: self.m1 for t in tickers}
        if self.now < BAR + timedelta(minutes=5):       # candle still forming
            return {t: quiet_bars(*self.hist, BAR) for t in tickers}
        return {t: (quiet_bars(*self.times) if t.endswith(".NS") else bars(*self.times))
                for t in tickers}

    def texts(self):
        return [c.args[0] for c in self.send.call_args_list]

    def close(self):
        self.now = BAR + timedelta(minutes=5, seconds=10)
        self.m1 = minute_bars(0, 1, 2, 3, 4, 5)                   # feed has passed the close
        return pipeline.run_once()

    def test_early_alert_is_sent_before_the_close_and_is_not_an_entry(self):
        out = pipeline.run_early(BAR)
        self.assertEqual(len(out), 1)
        text = self.texts()[0]
        self.assertIn("EARLY BUY</b> (unconfirmed)", text)
        self.assertIn("Price now <b>100.00</b>", text)
        self.assertIn("5m bar 10:00–10:05 still forming (58s left)", text)
        self.assertIn("Calculated 10:04:02 IST", text)
        scanned = self.scan.call_args.args[1]
        self.assertEqual(scanned.index[-1], BAR)                  # provisional candle
        self.assertEqual(scanned.iloc[-1].Volume, 1000.0)         # 4 x 250 from 1m
        st = state.load()
        self.assertEqual(st["sent"], {})                          # not an entry yet
        self.assertEqual(st["positions"], {})
        self.assertEqual(st["early"]["ABC"]["at"], "10:04:02")

    def test_early_pass_covers_the_extra_follow_sector_and_names_it(self):
        self.uni["sectors"]["ENE"] = {"index": "E", "stocks": ["XYZ"]}
        st = state.load()
        st["scan"].update({"label": "NEUTRAL", "conf": 52, "extra": ["ENE"], "follow": True})
        state.save(st)
        self.scan.side_effect = lambda raw, *a, **k: [signal()] if raw == "XYZ" else []
        self.assertEqual(len(pipeline.run_early(BAR)), 1)
        self.assertEqual([c.args[0] for c in self.scan.call_args_list], ["ABC", "XYZ"])
        self.assertIn("Sector <b>ENE (sector follow)</b>  |  Market NEUTRAL (52%)", self.texts()[0])
        self.assertIn("XYZ", state.load()["early"])

    def test_early_alert_is_never_repeated(self):
        pipeline.run_early(BAR)
        self.assertEqual(pipeline.run_early(BAR), [])
        self.now = BAR + timedelta(minutes=9)
        state_now = state.load()
        state_now["scan"]["bar"] = BAR.strftime("%Y-%m-%d %H:%M")
        state.save(state_now)
        self.assertEqual(pipeline.run_early(BAR + timedelta(minutes=5)), [])  # once per session
        self.assertEqual(self.send.call_count, 1)

    def test_no_early_alert_without_a_current_pick(self):
        for scan in ({"bar": "2026-11-09 09:50", "sector": "IT", "side": 1},     # stale decision
                     {"bar": PREV, "sector": "WAIT", "side": 0},                   # WAIT
                     {}):
            st = state.load()
            st["scan"] = scan
            state.save(st)
            self.assertEqual(pipeline.run_early(BAR), [])
        self.send.assert_not_called()
        self.fetch_many.assert_not_called()

    def test_no_early_alert_on_wrong_side_stale_feed_or_when_disabled(self):
        self.scan.return_value = [signal(side="SELL")]
        self.assertEqual(pipeline.run_early(BAR), [])
        self.scan.return_value = [signal()]
        self.m1 = minute_bars(0)                                   # last print 4 min old
        self.assertEqual(pipeline.run_early(BAR), [])
        self.m1 = minute_bars(0, 1, 2, 3)
        with patch.dict("os.environ", {"EARLY_ALERT_SECONDS": "0"}):
            self.assertEqual(pipeline.run_early(BAR), [])
        self.now = BAR + timedelta(minutes=5, seconds=1)           # candle already closed
        self.assertEqual(pipeline.run_early(BAR), [])
        self.send.assert_not_called()

    def test_names_already_alerted_or_open_are_skipped(self):
        st = state.load()
        st["sent"]["ABC"] = {"code": 101, "time": "09:45"}
        state.save(st)
        self.assertEqual(pipeline.run_early(BAR), [])
        self.fetch_many.assert_not_called()

    def test_close_confirms_the_early_alert_and_opens_the_trade(self):
        pipeline.run_early(BAR)
        out = self.close()
        self.assertEqual(len(out["signals"]), 1)
        confirmed = self.texts()[-1]
        self.assertTrue(confirmed.startswith("✅ <b>CONFIRMED</b> — early alert 10:04:02 @ 100.00"))
        self.assertTrue(confirmed.endswith("stock"))
        st = state.load()
        self.assertEqual(st["early"]["ABC"]["result"], "confirmed")
        self.assertEqual(st["sent"]["ABC"]["time"], "10:00")
        self.assertIn("ABC", st["positions"])
        self.assertEqual(sum("EARLY" in t for t in self.texts()), 1)

    def test_close_cancels_an_early_alert_that_did_not_hold(self):
        pipeline.run_early(BAR)
        self.scan.return_value = []
        out = self.close()
        self.assertEqual(out["signals"], [])
        cancel = self.texts()[-1]
        self.assertIn("EARLY CANCELLED", cancel)
        self.assertIn("BUY  ABC", cancel)
        self.assertIn("(10:05 close)", cancel)
        self.assertNotIn("\n", cancel)
        st = state.load()
        self.assertEqual(st["early"]["ABC"]["result"], "cancelled")
        self.assertEqual(st["sent"], {})                          # still free for a real entry
        before = self.send.call_count
        pipeline.run_once()
        self.assertEqual(self.send.call_count, before)            # cancelled once only

    def test_cancelled_name_can_still_get_a_normal_close_alert_later(self):
        pipeline.run_early(BAR)
        self.scan.return_value = []
        self.close()
        nxt = BAR + timedelta(minutes=5)
        self.times = self.times + [nxt + timedelta(minutes=5)]
        self.scan.return_value = [signal(time="10:05")]
        self.now = nxt + timedelta(minutes=5, seconds=10)
        self.m1 = minute_bars(8, 9, 10)
        out = pipeline.run_once()
        self.assertEqual(len(out["signals"]), 1)
        self.assertEqual(self.texts()[-1], "stock")               # plain entry, no early header

    def test_wait_at_the_close_cancels_the_early_alert(self):
        pipeline.run_early(BAR)
        self.md.pick_sector, self.md.action, self.md.pick_side = "WAIT", "WAIT", 0
        self.close()
        self.assertTrue(any("EARLY CANCELLED" in t and "WAIT" in t for t in self.texts()))
        self.assertEqual(state.load()["early"]["ABC"]["result"], "cancelled")
        self.assertEqual(state.load()["scan"]["sector"], "WAIT")

    def test_unchecked_early_alert_is_reported_as_unverified(self):
        pipeline.run_early(BAR)
        self.now = BAR + timedelta(minutes=5, seconds=10)
        with (patch.object(pipeline, "run_once", side_effect=RuntimeError("yahoo down")),
              patch.object(pipeline.time, "sleep",
                           side_effect=lambda s: setattr(self, "now", self.now + timedelta(seconds=60)))):
            with self.assertRaises(RuntimeError):
                pipeline.run_until_ready(expected_bar=BAR)
        self.assertIn("EARLY UNVERIFIED", self.texts()[-1])
        self.assertEqual(state.load()["early"]["ABC"]["result"], "unverified")

    def test_candle_is_not_judged_until_the_feed_has_passed_the_close(self):
        # +10s: the 5m row exists, but the newest 1m print is still inside the candle.
        self.now = BAR + timedelta(minutes=5, seconds=10)
        self.m1 = minute_bars(0, 1, 2, 3, 4)
        out = pipeline.run_once()
        self.assertEqual(out["signals"], [])
        self.assertEqual(out["pending"], ["ABC"])                 # retried, not dropped
        self.scan.assert_not_called()
        self.m1 = minute_bars(0, 1, 2, 3, 4, 5)                   # first print after the close
        self.assertEqual(len(pipeline.run_once()["signals"]), 1)

    def two_stocks(self, quiet_last, busy_last):
        """ABC is quiet (no print after the close); DEF shows where the feed is."""
        self.uni["sectors"]["IT"]["stocks"] = ["ABC", "DEF"]
        self.scan.side_effect = lambda raw, *a, **k: [signal()] if raw == "ABC" else []
        feeds = {"ABC.NS": minute_bars(*range(quiet_last + 1)), "DEF.NS": minute_bars(*range(busy_last + 1))}
        base_frames = self.frames
        self.fetch_many.side_effect = lambda tickers, period, interval: (
            {t: feeds[t] for t in tickers} if interval == "1m" else base_frames(tickers, period, interval))

    def test_quiet_stock_is_judged_after_the_settle_time(self):
        self.two_stocks(quiet_last=4, busy_last=5)                # the feed itself is past the close
        self.now = BAR + timedelta(minutes=5, seconds=44)
        self.assertEqual(pipeline.run_once()["signals"], [])
        self.now = BAR + timedelta(minutes=5, seconds=45)
        self.assertEqual(len(pipeline.run_once()["signals"]), 1)

    def test_delayed_feed_holds_every_candle_back_instead_of_reading_it_unfinished(self):
        self.two_stocks(quiet_last=3, busy_last=4)                # nobody has printed past the close
        self.now = BAR + timedelta(minutes=5, seconds=100)
        out = pipeline.run_once()
        self.assertEqual(out["signals"], [])
        self.assertEqual(sorted(out["pending"]), ["ABC", "DEF"])
        self.assertFalse(any("delayed" in t for t in self.texts()))
        self.now = BAR + timedelta(minutes=5, seconds=130)        # two minutes on: say so, once
        pipeline.run_once()
        pipeline.run_once()
        self.assertEqual(sum("Yahoo feed is delayed" in t for t in self.texts()), 1)
        self.assertEqual(state.load()["sent"], {})
        self.two_stocks(quiet_last=5, busy_last=5)                # feed catches up in time
        self.assertEqual(len(pipeline.run_once()["signals"]), 1)

    def test_without_any_1m_data_the_settle_time_decides(self):
        self.m1 = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        self.now = BAR + timedelta(minutes=5, seconds=20)
        self.assertEqual(pipeline.run_once()["signals"], [])
        self.now = BAR + timedelta(minutes=5, seconds=50)
        self.assertEqual(len(pipeline.run_once()["signals"]), 1)

    def position(self):
        st = state.load()
        st["positions"]["ABC"] = {"side": "BUY", "entry": 100.0, "sl": 99.0, "entry_ema9": 99.5,
                                  "t1": 101.0, "t2": None, "t3": None,
                                  "entry_bar": (BAR - timedelta(minutes=10)).isoformat(),
                                  "last_bar": (BAR - timedelta(minutes=10)).isoformat()}
        state.save(st)

    def exit_frames(self, last_1m):
        prev = BAR - timedelta(minutes=5)
        five = quiet_bars(*self.times)
        five.loc[prev, "Close"] = 99.5                            # completed candle closes below EMA9
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: (minute_bars(*range(last_1m + 1)) if interval == "1m" else
                five.copy() if t.endswith(".NS") else bars(*self.times)) for t in tickers}

    def test_delayed_feed_postpones_a_hit_alert_by_one_candle_and_never_loses_it(self):
        self.position()
        self.exit_frames(last_1m=4)                               # feed has not passed 10:05
        self.now = BAR + timedelta(minutes=5, seconds=100)
        out = pipeline.run_once()
        self.assertIn("ABC", out["pending"])
        exits = [t for t in self.texts() if "EMA9 EXIT" in t]
        self.assertEqual(len(exits), 1)                           # the older 09:55 candle is final
        self.assertIn("5m 09:55", exits[0])
        self.assertNotIn("ABC", state.load()["positions"])

    def test_newest_candle_is_not_used_for_an_exit_until_the_feed_passes_its_close(self):
        self.position()
        five = quiet_bars(*self.times)
        five.loc[BAR, "Close"] = 99.5                             # looks like an exit, may be unfinished
        feed = {"last": 4}
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: (minute_bars(*range(feed["last"] + 1)) if interval == "1m" else
                five.copy() if t.endswith(".NS") else bars(*self.times)) for t in tickers}
        self.now = BAR + timedelta(minutes=5, seconds=100)
        pipeline.run_once()
        self.assertFalse(any("EMA9 EXIT" in t for t in self.texts()))
        self.assertIn("ABC", state.load()["positions"])
        feed["last"] = 5                                          # first print after the close
        pipeline.run_once()
        self.assertEqual(sum("EMA9 EXIT" in t and "5m 10:00" in t for t in self.texts()), 1)

    def test_last_candle_of_the_day_does_not_wait_for_prints_that_never_come(self):
        close = BAR.replace(hour=15, minute=30)
        frames = {"A": minute_bars(0)}
        self.now = close + timedelta(seconds=50)
        self.assertEqual(pipeline._unsettled(frames, ["A"], close), (set(), False))
        self.now = close + timedelta(seconds=20)
        self.assertEqual(pipeline._unsettled(frames, ["A"], close), ({"A"}, False))

    def test_pending_feed_keeps_the_early_alert_open(self):
        pipeline.run_early(BAR)
        self.now = BAR + timedelta(minutes=5, seconds=10)
        self.m1 = minute_bars(0, 1, 2, 3, 4)
        pipeline.run_once()
        self.assertNotIn("result", state.load()["early"]["ABC"])  # neither confirmed nor cancelled
        self.assertEqual(sum("CANCELLED" in t or "CONFIRMED" in t for t in self.texts()), 0)

    def test_statistics_follow_an_early_alert_through_its_confirmation(self):
        pipeline.run_early(BAR)
        self.close()
        pipeline.run_once()                                       # same candle again: not recounted
        x = state.load()["stats"]
        self.assertEqual((x["candles"], x["picks"], x["early_passes"], x["early_live"],
                          x["early_sent"], x["confirmed"], x["entries"]), (1, 1, 1, 1, 1, 1, 1))
        self.assertEqual(x["lag_max"], 10.0)
        self.assertEqual(x["early_age_sum"] / x["early_age_n"], 62.0)   # 10:03 bar seen at 10:04:02

    def test_close_scan_records_the_pick_for_the_next_early_pass(self):
        self.close()
        scan = state.load()["scan"]
        self.assertEqual((scan["bar"], scan["sector"], scan["side"]),
                         (BAR.strftime("%Y-%m-%d %H:%M"), "IT", 1))

    def test_confirmed_alert_prints_its_delay_after_the_close(self):
        self.now = BAR + timedelta(minutes=5, seconds=9)
        self.assertEqual(pipeline._lag_text("10:00"), " (+9s after close)")


class LiveLoopTests(TestCase):
    market_at_bar = base.LiveAlertTests.market_at_bar

    def setUp(self):
        base.LiveAlertTests.setUp(self)
        self.addCleanup(data.enable_daily_cache, False)
        self.calls = []
        self.sleep = patch.object(pipeline.time, "sleep", side_effect=self.advance)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)

    def test_outside_session_exits_without_telegram_or_data(self):
        for now in (BAR.replace(hour=3), BAR.replace(hour=15, minute=40),
                    BAR + timedelta(days=5)):                     # night, after close, Saturday
            self.now = now
            self.assertEqual(pipeline.live_loop(exit_at_close=True), "closed")
        self.send.assert_not_called()

    def test_each_candle_gets_an_early_pass_then_a_confirmed_scan(self):
        self.now = BAR + timedelta(minutes=1)                     # 10:01, candle closes 10:05
        early = lambda bar: self.calls.append(("early", bar, self.now))
        ready = lambda expected_bar: (self.calls.append(("close", expected_bar, self.now))
                                      or {"market": "BULL"})
        with (patch.object(pipeline, "run_early", side_effect=early),
              patch.object(pipeline, "run_until_ready", side_effect=ready),
              patch.dict("os.environ", {"EARLY_ALERT_SECONDS": "60", "POLL_GRACE_SECONDS": "10"})):
            self.assertEqual(pipeline.live_loop(max_minutes=0.01, exit_at_close=True), "budget")
        self.assertEqual(self.calls, [
            ("early", BAR, BAR + timedelta(minutes=4)),
            ("close", BAR, BAR + timedelta(minutes=5, seconds=10)),
        ])
        self.assertEqual(sum("started" in c.args[0] for c in self.send.call_args_list), 1)

    def test_late_start_skips_the_early_pass_but_not_the_close(self):
        self.now = BAR + timedelta(minutes=4, seconds=45)
        with (patch.object(pipeline, "run_early") as early,
              patch.object(pipeline, "run_until_ready", return_value={"market": "BULL"}) as ready):
            pipeline.live_loop(max_minutes=0.01, exit_at_close=True)
        early.assert_not_called()
        ready.assert_called_once_with(expected_bar=BAR)

    def test_handover_does_not_announce_a_restart(self):
        state.save({"session": str(BAR.date()), "sent": {}, "positions": {}, "announced": True})
        self.now = BAR + timedelta(minutes=4, seconds=45)
        with patch.object(pipeline, "run_until_ready", return_value={"market": "BULL"}):
            pipeline.live_loop(max_minutes=0.01, exit_at_close=True)
        self.send.assert_not_called()

    def test_start_is_announced_once_per_session_even_across_restarts(self):
        for _ in range(3):                                        # e.g. holiday re-checks
            self.now = BAR + timedelta(minutes=4, seconds=45)
            with patch.object(pipeline, "run_until_ready", return_value={"market": "CLOSED"}):
                pipeline.live_loop(max_minutes=0.01, exit_at_close=True)
        self.assertEqual(sum("started" in c.args[0] for c in self.send.call_args_list), 1)
        self.assertTrue(state.load()["announced"])

    def test_dry_run_loop_uses_a_scratch_ledger_and_leaves_the_real_one_alone(self):
        self.addCleanup(state.use_scratch, False)
        real = {"session": str(BAR.date()), "sent": {"XYZ": {"code": 101, "time": "09:45"}},
                "positions": {}, "announced": True}
        state.save(real)
        self.now = BAR + timedelta(minutes=4, seconds=45)

        def one_cycle(expected_bar):
            return pipeline._run_until_ready(expected_bar=expected_bar)

        with (patch.dict("os.environ", {"DRY_RUN": "1"}),
              patch.object(pipeline, "run_until_ready", side_effect=one_cycle)):
            pipeline.live_loop(max_minutes=0.01, exit_at_close=True)
            dry = state.load()
        self.assertEqual(dry["scan"]["sector"], "IT")             # early pass can now work in a dry run
        self.assertIn("ABC", dry["sent"])
        state.use_scratch(False)
        self.assertEqual(state.load(), real)                      # real ledger untouched

    def test_session_summary_is_sent_once_after_the_close(self):
        state.save({"session": str(BAR.date()), "sent": {}, "positions": {}, "announced": True,
                    "stats": {"candles": 75, "picks": 40, "early_passes": 40, "early_live": 38,
                              "early_sent": 3, "confirmed": 2, "cancelled": 1, "entries": 4,
                              "lag_sum": 60.0, "lag_max": 47.0, "early_age_sum": 400.0,
                              "early_age_n": 10, "close_age_sum": 90.0, "close_age_n": 10}})
        for _ in range(2):
            self.now = BAR.replace(hour=15, minute=31)
            self.assertEqual(pipeline.live_loop(exit_at_close=True), "closed")
        texts = [c.args[0] for c in self.send.call_args_list]
        self.assertEqual(len(texts), 1)
        self.assertIn("Candles scanned 75 of 75 · with a sector pick 40", texts[0])
        self.assertIn("Early passes 40 · with live 1m data 38 · newest 1m bar 40s old on average", texts[0])
        self.assertIn("Early alerts 3: confirmed 2 · cancelled 1 · unverified 0", texts[0])
        self.assertIn("Entries 4 · sent 15s after the close on average, slowest 47s", texts[0])
        self.assertIn("At the confirm scan: newest 1m bar 9s old on average", texts[0])

    def test_no_summary_when_nothing_was_scanned(self):
        self.now = BAR.replace(hour=15, minute=34)
        state.save({"session": str(BAR.date()), "sent": {}, "positions": {}})
        self.assertIsNone(pipeline.format_summary(state.load()))

    def test_holiday_ends_the_loop_instead_of_polling_all_day(self):
        self.now = BAR + timedelta(minutes=4, seconds=45)
        with patch.object(pipeline, "run_until_ready", return_value={"market": "CLOSED"}) as ready:
            self.assertEqual(pipeline.live_loop(exit_at_close=True), "holiday")
        self.assertEqual(ready.call_count, 3)

    def test_holiday_stops_the_per_bar_retry(self):
        self.now = BAR + timedelta(minutes=5, seconds=10)
        with patch.object(pipeline, "run_once", return_value={"bar": "2026-11-06 15:25",
                                                              "market": "CLOSED", "action": "WAIT"}) as once:
            pipeline.run_until_ready(expected_bar=BAR)
        once.assert_called_once()


class EarlyCheckReplayTests(TestCase):
    """The measuring tool must judge the forming candle only on what was published."""

    def setUp(self):
        from nse_scanner import early_check
        self.ec = early_check
        hist = [BAR - timedelta(minutes=5 * i) for i in range(3, 0, -1)]
        self.m5 = quiet_bars(*hist, BAR, BAR + timedelta(minutes=5))
        self.m1 = pd.concat([minute_bars(0, 1, 2, 3, close=100.0), minute_bars(4, close=101.0),
                             minute_bars(5, 6, 7, 8, 9, close=102.0)])
        self.seen = []

    def fires(self, early=(), final=()):
        def fake(raw, df5, d1, hhmm, clock):
            is_early = clock.second == 0 and clock.minute % 5 == 4
            self.seen.append((hhmm, is_early, float(df5["Close"].iloc[-1]), float(df5["Volume"].iloc[-1])))
            return [signal(time=hhmm)] if hhmm in (early if is_early else final) else []
        return patch.object(self.ec, "_fires_on", side_effect=fake)

    def run_day(self):
        return self.ec.replay_symbol("ABC", self.m5, self.m1, None, BAR.date(), 60)

    def test_confirmed_early_alert_reports_the_price_given_up_by_waiting(self):
        with self.fires(early=("10:00",), final=("10:00",)):
            rows = self.run_day()
        self.assertEqual([(r["bar"], r["kind"]) for r in rows], [("10:00", "confirmed")])
        self.assertEqual(rows[0]["early_px"], 100.0)              # minute 4 (101.0) not yet seen
        early_view = [s for s in self.seen if s[0] == "10:00" and s[1]][0]
        self.assertEqual(early_view[2:], (100.0, 1000.0))         # 4 completed minutes only
        self.assertAlmostEqual(rows[0]["gain_pct"], 0.0)          # 5m close fixture is 100.00

    def test_cancelled_and_close_only_are_counted_and_early_fires_once(self):
        with self.fires(early=("10:00", "10:05"), final=("10:05",)):
            rows = self.run_day()
        self.assertEqual([(r["bar"], r["kind"]) for r in rows],
                         [("10:00", "cancelled"), ("10:05", "close-only")])
        text = self.ec.render(rows, BAR.date(), 60)
        self.assertIn("cancelled at close   : 1 (100%)", text)
        self.assertIn("close signals          : 1, of which 0 (0%) had an early alert", text)
