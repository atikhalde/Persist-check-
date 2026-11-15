"""Golden parity: the 1m scan and the 5m scan must be indistinguishable.

One synthetic session, both scanners, same inputs. Then compare

1. the frame handed to the engine (``stock.scan_stock``) — same rows, same
   values, same ``keep_all``;
2. the plan built from the signal — same entry / stop / targets;
3. the messages, character for character (``telegram.send`` vs ``send_bot3``).

If any of those drift apart, one of the two scans has changed a rule and this
file fails. It is the mechanical form of "there is no gap between the two
scanners".
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import tempfile
from pathlib import Path

import pandas as pd
from pandas.testing import assert_frame_equal

from nse_scanner import pipeline, scan1m, state
from nse_scanner.trade import TradePlan

import test_live_alerts as base

BAR = base.BAR                                         # 10:00 open, 10:05 close
NOW = BAR + timedelta(minutes=6)                       # 10:06, one minute after the close
PLAN = TradePlan(100.0, 100.0, 99.98, 0.02, 100.02, 100.04, 100.06, ("R1", "R2", "R3"))


def five_minute_bars():
    times = [BAR - timedelta(minutes=5 * i) for i in range(39, -1, -1)]
    return base.quiet_bars(*times)


def one_minute_bars():
    """Prints up to 10:05, so the feed has reached the close (as Yahoo's does)."""
    idx = pd.DatetimeIndex([BAR + timedelta(minutes=m) for m in range(6)])
    return pd.DataFrame({"Open": [100.0] * 6, "High": [100.01] * 6, "Low": [99.99] * 6,
                         "Close": [100.0] * 6, "Volume": [250.0] * 6}, index=idx)


class GoldenParityTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        scan1m._pick_cache.clear()
        scan1m._one_cache.clear()
        scan1m._five_cache.clear()
        scan1m._md_cache.clear()

        self.env = patch.dict("os.environ", {
            "DRY_RUN": "0", "SCAN1M_MARKET_SOURCE": "own", "SCAN1M_FIRST_MINUTE": "1",
            "SCAN1M_CANCEL_MESSAGE": "0", "SCAN1M_MAX_STALE_SECONDS": "100",
            "MAX_ALERT_LAG_SECONDS": "180", "BAR_SETTLE_SECONDS": "45", "OI_FILTER": "off",
            "YF_PERIOD_5M": "7d", "YF_PERIOD_1D": "90d",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

        self.five = five_minute_bars()
        self.minutes = one_minute_bars()
        self.uni = {"nifty": "NIFTY", "vix": "VIX",
                    "sectors": {"IT": {"index": "NIFTY IT", "stocks": ["ABC"]}}}
        self.md = SimpleNamespace(
            bar_time=BAR.strftime("%Y-%m-%d %H:%M"), dir_state=1, dir_label="BULL",
            dir_conf=74, dir_tier="Strong", pick_sector="IT", pick_side=1,
            pick_reason="PERSIST + IT", action="SCAN LONG", regime="TREND",
            persist_plus=(("IT", 82.0),), persist_minus=(("PSU", 11.0),),
            extra_sectors=(), nifty=25000.0, nifty_day_ret=0.6, nifty_intra=0.2,
            vix=13.2, vix_chg=-1.5, adx=27.0, mtf_bull=3, breadth_adv=28, breadth_dec=12,
            live_flow="BUY", live_flow_qualified=True, live_flow_score=71.0)

        self.patches = [
            patch.object(state, "STATE_PATH", self.dir / "scan_state.json"),
            patch.object(scan1m, "LEDGER", self.dir / "scan1m_state.json"),
            patch.object(pipeline, "now_ist", side_effect=lambda: NOW),
            patch.object(scan1m, "now_ist", side_effect=lambda: NOW),
            patch.object(pipeline.data, "load_universe", return_value=self.uni),
            patch.object(pipeline.data, "fetch_symbol", side_effect=lambda *a: {
                "m5": self.five, "d1": base.quiet_bars(BAR - timedelta(days=1))}),
            patch.object(pipeline.data, "fetch_many", side_effect=self.fetch),
            patch.object(pipeline.data, "fetch_daily", return_value={}),
            patch.object(pipeline, "run_market", side_effect=self.market_at_bar),
            patch.object(scan1m, "run_market", side_effect=self.market_at_bar),
            patch.object(pipeline, "format_market", return_value="MARKET (same text)"),
            patch.object(pipeline, "scan_stock", side_effect=self.scan),
            patch.object(scan1m, "scan_stock", side_effect=self.scan),
            patch.object(pipeline, "build_plan", side_effect=self.plan),
            patch.object(scan1m, "build_plan", side_effect=self.plan),
            patch.object(pipeline, "_oi_reader", return_value=None),
            patch.object(scan1m, "_oi_reader", return_value=None),
            patch.object(pipeline.telegram, "send", return_value=True),
            patch.object(pipeline.telegram, "send_bot3", return_value=True),
        ]
        self.mocks = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)
        self.bot1 = self.mocks[17]
        self.bot3 = self.mocks[18]

        self.engine_calls = []
        self.plan_calls = []
        self.signal = None
        self.plan_value = PLAN

    def market_at_bar(self, df, *args):
        self.md.bar_time = df.index[-1].strftime("%Y-%m-%d %H:%M")
        return self.md

    def fetch(self, tickers, period, interval):
        if interval == "5m":
            return {t: self.five for t in tickers}
        return {t: self.minutes for t in tickers}

    def scan(self, raw, frame, daily, keep_all=False):
        self.engine_calls.append((raw, frame, keep_all))
        return [self.signal or base.signal(time=BAR.strftime("%H:%M"))]

    def plan(self, sig, frame, bar, daily):
        self.plan_calls.append((sig, frame, bar))
        return self.plan_value

    def texts(self, mock):
        return [c.args[0] for c in mock.call_args_list]

    # ── the comparison ──────────────────────────────────────────────────────
    def test_both_scans_scan_the_same_frame_and_send_the_same_message(self):
        self.assertEqual(pipeline.run_once()["signals"] != [], True)
        five_m = self.engine_calls[-1]
        self.assertEqual(scan1m.confirm_pass(BAR, now=NOW) != [], True)
        one_m = self.engine_calls[-1]

        # 1. the engine sees exactly the same candle
        assert_frame_equal(one_m[1], five_m[1])
        self.assertEqual(one_m[1].index[-1], BAR)
        self.assertEqual((five_m[0], five_m[2]), ("ABC", True))
        self.assertEqual((one_m[0], one_m[2]), ("ABC", True))

        # 2. the plan is built from the same bar
        self.assertEqual(self.plan_calls[-1][2], BAR)
        assert_frame_equal(self.plan_calls[-1][1], self.plan_calls[0][1])

        # 3. the messages are the same, in the same order
        self.assertEqual(self.texts(self.bot1), ["MARKET (same text)", self.texts(self.bot1)[1]])
        self.assertEqual(self.texts(self.bot3), self.texts(self.bot1))
        self.assertEqual(len(self.texts(self.bot3)), 2)          # market + entry

    def test_a_second_candle_is_the_same_on_both(self):
        pipeline.run_once()
        scan1m.confirm_pass(BAR, now=NOW)
        self.bot1.reset_mock()
        self.bot3.reset_mock()
        pipeline.run_once()                       # nothing new: one entry per session
        scan1m.confirm_pass(BAR, now=NOW)
        self.assertEqual(self.texts(self.bot1), [])
        self.assertEqual(self.texts(self.bot3), [])

    def test_a_projection_only_adds_the_confirmation_header(self):
        scan1m.run_minute(BAR, now=BAR + timedelta(minutes=1, seconds=5))
        self.bot3.reset_mock()
        scan1m.confirm_pass(BAR, now=NOW)
        pipeline.run_once()
        entry1, entry3 = self.texts(self.bot1)[-1], self.texts(self.bot3)[-1]
        self.assertTrue(entry3.startswith("✅ <b>CONFIRMED</b> — projected 10:01:05 @ 100.00"))
        self.assertTrue(entry3.endswith(entry1))     # the entry body is bot 1's text

    def test_the_shared_helpers_still_default_to_bot1_and_the_5m_ledger(self):
        """The 1m scan passes bot 3 explicitly; the defaults must stay bot 1."""
        st = {"session": str(BAR.date()), "sent": {},
              "positions": {"ABC": {"side": "BUY", "entry": 100.0, "sl": 99.0,
                                    "entry_ema9": 100.0, "t1": 100.008, "t2": None, "t3": None,
                                    "entry_bar": (BAR - timedelta(minutes=10)).isoformat(),
                                    "last_bar": (BAR - timedelta(minutes=10)).isoformat()}}}
        pipeline._track_positions(st, BAR, "7d")               # no kwargs
        self.assertEqual(self.bot3.call_count, 0)
        self.assertIn("TARGET HIT", self.bot1.call_args.args[0])
        self.bot1.reset_mock()
        with patch.object(pipeline, "now_ist", return_value=BAR + timedelta(minutes=7, seconds=30)):
            pipeline._warn_feed_behind({"session": "2026-11-09", "sent": {}, "stats": {}},
                                       BAR + timedelta(minutes=5),
                                       {"ABC.NS": one_minute_bars().iloc[:3]})
        self.assertEqual(self.bot3.call_count, 0)
        self.assertIn("Yahoo feed is delayed", self.bot1.call_args.args[0])

    # ── the paths where a gap would be invisible ────────────────────────────
    def rejected(self):
        five = state.load(self.dir / "scan_state.json").get("stats", {}).get("rejected") or {}
        one = state.load(self.dir / "scan1m_state.json").get("stats", {}).get("rejected") or {}
        return five, one

    def test_the_same_reject_book_for_a_wrong_side_signal(self):
        self.signal = base.signal(time=BAR.strftime("%H:%M"), side="SELL")
        pipeline.run_once()
        scan1m.confirm_pass(BAR, now=NOW)
        five, one = self.rejected()
        self.assertEqual(five, one)
        self.assertEqual(list(one["opposite"]), ["ABC@10:00"])
        self.assertEqual(self.texts(self.bot3), self.texts(self.bot1))   # market only
        self.assertEqual(len(self.texts(self.bot3)), 1)

    def test_the_same_reject_book_without_a_valid_stop(self):
        self.plan_value = None
        pipeline.run_once()
        scan1m.confirm_pass(BAR, now=NOW)
        five, one = self.rejected()
        self.assertEqual(five, one)
        self.assertEqual(list(one["no_stop"]), ["ABC@10:00"])
        self.assertEqual(self.texts(self.bot3), self.texts(self.bot1))

    def test_a_wait_pick_sends_only_the_market_message_on_both(self):
        self.md.pick_sector, self.md.action, self.md.pick_side = "WAIT", "WAIT", 0
        pipeline.run_once()
        scan1m.confirm_pass(BAR, now=NOW)
        self.assertEqual(len(self.texts(self.bot1)), 1)
        self.assertEqual(self.texts(self.bot3), self.texts(self.bot1))

    def test_a_delayed_feed_is_announced_with_the_same_words(self):
        # 150 s after the close: past the 120 s "say something" mark, still inside
        # the 180 s alert deadline (so the run is not simply too late).
        self.minutes = one_minute_bars().iloc[:3]          # newest print 10:02
        late = BAR + timedelta(minutes=7, seconds=30)
        with patch.object(pipeline, "now_ist", return_value=late), \
             patch.object(scan1m, "now_ist", return_value=late):
            pipeline.run_once()
            scan1m.confirm_pass(BAR, now=late)
        self.assertEqual(self.texts(self.bot3), self.texts(self.bot1))
        self.assertTrue(any("Yahoo feed is delayed" in t for t in self.texts(self.bot3)))

    def test_a_late_run_sends_nothing_on_both(self):
        late = BAR + timedelta(minutes=10)                 # 5 min past the close
        with patch.object(pipeline, "now_ist", return_value=late), \
             patch.object(scan1m, "now_ist", return_value=late):
            pipeline.run_once()
            scan1m.confirm_pass(BAR, now=late)
        self.assertEqual(self.texts(self.bot1), [])
        self.assertEqual(self.texts(self.bot3), [])

    def test_the_entry_text_carries_the_same_numbers(self):
        pipeline.run_once()
        scan1m.confirm_pass(BAR, now=NOW)
        entry1, entry3 = self.texts(self.bot1)[-1], self.texts(self.bot3)[-1]
        self.assertEqual(entry3, entry1)
        for needle in ("Entry (candle close) <b>100.00</b>", "SL 99.98", "5m bar 10:00–10:05 IST"):
            self.assertIn(needle, entry3)
