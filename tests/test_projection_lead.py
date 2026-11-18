"""Bot 3 must lead bot 1 — on the right candle and the right name.

Three live defects from 15-16 Nov 2026 are pinned here:

* both scanners sent the **same** alert in the same minute (no 4-minute lead);
* the scan **missed the sector leader** (ITC in FMCG, the pharma leader) while
  sending late alerts for the followers that fired afterwards;
* a **TARGET HIT** alert fired for a trade from a previous session.

The first two share one root cause: the projection judged the forming candle
with the *previous* candle's sector pick, so on the candle the market flips —
the candle a sector's leader fires on — bot 3 had nothing to project and only
echoed bot 1's entry at the close, while followers firing on the *next* candles
did get an early alert. ``scan1m.projected_market`` fixes that by projecting the
pick itself; ``data.project_volume`` fixes the other half (a partial candle's
volume reads a fifth of itself, which pushed ``total > 60`` past the whole
candle).

No network access: every fetch and the market engine are mocked.
"""
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import tempfile

import numpy as np
import pandas as pd

from nse_scanner import data, scan1m, state, telegram
from nse_scanner.session import IST
from nse_scanner.stock import StockSignal
from nse_scanner.trade import TradePlan

BAR = datetime(2026, 11, 16, 10, 0, tzinfo=IST)      # 10:00–10:05, the flip candle
PREV = BAR - timedelta(minutes=5)
MIN1 = BAR + timedelta(minutes=1, seconds=6)         # minute 1 of the forming candle
MIN3 = BAR + timedelta(minutes=3, seconds=6)
PLAN = TradePlan(100.0, 100.0, 99.98, 0.02, 100.02, 100.04, 100.06, ("R1", "R2", "R3"))


def frame_5m(*times, close=100.0):
    return pd.DataFrame({"Open": [close] * len(times), "High": [close + 0.05] * len(times),
                         "Low": [close - 0.05] * len(times), "Close": [close] * len(times),
                         "Volume": [1000.0] * len(times)},
                        index=pd.DatetimeIndex(times))


def frame_1m(*minutes, price=100.0, volume=250.0):
    idx = pd.DatetimeIndex([BAR + timedelta(minutes=m) for m in minutes])
    return pd.DataFrame({"Open": price, "High": price + 0.02, "Low": price - 0.02,
                         "Close": price, "Volume": volume}, index=idx)


def md(sector, *, bar=BAR, side=1, action="SCAN LONG", extra=(), follow=False):
    return SimpleNamespace(bar_time=bar.strftime("%Y-%m-%d %H:%M"), dir_state=side,
                           dir_label="BULL" if side == 1 else "BEAR", dir_conf=74,
                           pick_sector=sector, pick_side=side, action=action,
                           pick_reason="SECTOR FOLLOW test" if follow else "test",
                           regime="TREND", extra_sectors=list(extra))


def signal(time="10:00", side="BUY", code=101, symbol="ITC"):
    return StockSignal(symbol, code, "BUY-EX", side, time, 100.0, 65, "Strong",
                       20, 1, "test", 99.0)


class ProjectVolumeTests(TestCase):
    """The forming candle's volume is finished from the prints, exactly at close."""

    def test_a_partial_candle_is_scaled_to_the_full_five_minutes(self):
        history = frame_5m(PREV - timedelta(minutes=5), PREV)
        forming = data.provisional_5m(history, frame_1m(0), BAR, MIN1, 100)
        self.assertEqual(float(forming["Volume"].iloc[-1]), 250.0)
        one = data.project_volume(forming, frame_1m(0), BAR)
        three = data.project_volume(data.provisional_5m(history, frame_1m(0, 1, 2), BAR, MIN3, 100),
                                    frame_1m(0, 1, 2), BAR)
        self.assertEqual(float(one["Volume"].iloc[-1]), 1250.0)      # 250 × 5/1
        self.assertAlmostEqual(float(three["Volume"].iloc[-1]), 250 * 3 * 5 / 3)

    def test_the_completed_candle_is_untouched(self):
        """Factor 1.0 at the close, so the confirmed frame cannot drift."""
        history = frame_5m(*[BAR - timedelta(minutes=5 * i) for i in range(5, 0, -1)])
        forming = data.provisional_5m(history, frame_1m(0, 1, 2, 3, 4), BAR, BAR + timedelta(minutes=4, seconds=50), 100)
        out = data.project_volume(forming, frame_1m(0, 1, 2, 3, 4), BAR)
        self.assertEqual(out["Volume"].iloc[-1], forming["Volume"].iloc[-1])
        self.assertEqual(out["Volume"].iloc[:-1].tolist(), forming["Volume"].iloc[:-1].tolist())

    def test_a_frozen_feed_is_scaled_by_the_prints_it_has_not_by_the_clock(self):
        """A lagging 1m feed keeps the volume it actually saw."""
        forming = frame_5m(PREV, BAR)
        forming.loc[BAR, "Volume"] = 300.0
        out = data.project_volume(forming, frame_1m(0, 1), BAR)      # 2 minutes covered
        self.assertEqual(float(out["Volume"].iloc[-1]), 300.0 * 5 / 2)

    def test_no_forming_row_means_no_change(self):
        closes = frame_5m(PREV)
        self.assertIs(data.project_volume(closes, frame_1m(0), BAR), closes)

    def test_only_the_projection_scales_the_volume(self):
        history = frame_5m(*[BAR - timedelta(minutes=5 * i) for i in range(5, 0, -1)])
        one = frame_1m(0)
        raw = scan1m.projected_frame(history, one, BAR, MIN1, project_volume=False)
        scaled = scan1m.projected_frame(history, one, BAR, MIN1)
        self.assertEqual(float(raw["Volume"].iloc[-1]), 250.0)
        self.assertEqual(float(scaled["Volume"].iloc[-1]), 250.0 * 5)

    def test_the_confirm_pass_never_projects_the_volume(self):
        """The candle both scans confirm at the close is bot 1's frame verbatim."""
        history = frame_5m(*[BAR - timedelta(minutes=5 * i) for i in range(5, -1, -1)])
        settled = frame_1m(0, 1, 2, 3, 4, 5)        # a print past the close settles it
        with patch.object(scan1m.data, "project_volume",
                          side_effect=AssertionError("the close must not be projected")):
            frame = scan1m.closed_frame(history, settled, BAR)
        self.assertIsNotNone(frame)
        self.assertEqual(float(frame["Volume"].iloc[-1]), 1000.0)


class ProjectedPickTests(TestCase):
    """``pick_for(projection=True)`` uses the forming candle's own verdict."""

    def setUp(self):
        scan1m._pick_cache.clear()
        scan1m._ledger_warned.clear()
        self.env = patch.dict("os.environ", {"DRY_RUN": "0", "SCAN1M_MARKET_SOURCE": "own"},
                              clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_path = Path(tmp.name) / "scan_state.json"

    def _run(self, projected, previous, *, source="own"):
        with patch.dict("os.environ", {"SCAN1M_MARKET_SOURCE": source}, clear=False), \
             patch.object(state, "STATE_PATH", self.state_path), \
             patch.object(scan1m, "projected_market", return_value=projected) as proj, \
             patch.object(scan1m, "_own_pick", return_value=previous) as own:
            pick = scan1m.pick_for(BAR, projection=True, now=MIN1)
        return pick, proj, own

    def test_the_flip_candle_is_decided_by_the_forming_candle(self):
        """The 5m scan still holds IT; the forming candle has already flipped."""
        pick, proj, own = self._run(md("FMCG"), {"sector": "IT", "side": 1, "projected": False,
                                                 "bar": PREV.strftime("%Y-%m-%d %H:%M")})
        self.assertEqual(pick["sector"], "FMCG")
        self.assertTrue(pick["projected"])
        self.assertEqual(pick["bar"], BAR.strftime("%Y-%m-%d %H:%M"))
        proj.assert_called_once()
        own.assert_not_called()                      # no need for the old pick at all

    def test_without_a_projection_the_previous_candle_is_used(self):
        pick, _, own = self._run(None, {"sector": "IT", "side": 1, "projected": False,
                                       "bar": PREV.strftime("%Y-%m-%d %H:%M")})
        self.assertEqual(pick["sector"], "IT")
        self.assertFalse(pick["projected"])
        own.assert_called_once()

    def test_a_projected_wait_is_an_answer_not_a_fallback(self):
        """The forming candle says WAIT: nothing is projected on the old sector."""
        pick, _, own = self._run(md("WAIT", action="WAIT", side=1),
                                 {"sector": "IT", "side": 1, "projected": False,
                                  "bar": PREV.strftime("%Y-%m-%d %H:%M")})
        self.assertEqual(pick["sector"], "WAIT")
        own.assert_not_called()

    def test_ledger_mode_never_projects(self):
        """``SCAN1M_MARKET_SOURCE=ledger`` reads closed candles: no projection."""
        with patch.object(scan1m, "projected_market",
                          side_effect=AssertionError("must not project in ledger mode")), \
             patch.object(state, "STATE_PATH", self.state_path), \
             patch.dict("os.environ", {"SCAN1M_MARKET_SOURCE": "ledger"}, clear=False):
            state.save({"session": str(BAR.date()), "sent": {}, "scan": {}}, self.state_path)
            self.assertIsNone(scan1m.pick_for(BAR, projection=True, now=MIN1))

    def test_the_projection_is_recomputed_each_minute(self):
        """Minute 1 and minute 3 must not share one cached answer."""
        with patch.object(scan1m, "projected_market", side_effect=[md("IT"), md("FMCG")]) as proj, \
             patch.object(scan1m, "_own_pick", return_value=None), \
             patch.object(state, "STATE_PATH", self.state_path):
            first = scan1m.pick_for(BAR, projection=True, now=MIN1)
            second = scan1m.pick_for(BAR, projection=True, now=MIN3)
        self.assertEqual((first["sector"], second["sector"]), ("IT", "FMCG"))
        self.assertEqual(proj.call_count, 2)


class ProjectedMarketTests(TestCase):
    """``projected_market`` rebuilds Nifty/VIX/sectors like a stock."""

    def setUp(self):
        self.universe = {"nifty": "NIFTY", "vix": "VIX",
                         "sectors": {"FMCG": {"index": "CNXFMCG", "stocks": ["ITC"]},
                                     "IT": {"index": "CNXIT", "stocks": ["INFY"]}}}
        self.env = patch.dict("os.environ", {"SCAN1M_MAX_STALE_SECONDS": "100"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        scan1m._index_daily.clear()
        scan1m._five_cache.clear()
        scan1m._one_cache.clear()

    def _patches(self, frames_5m, frames_1m, market):
        return [patch.object(scan1m.data, "load_universe", return_value=self.universe),
                patch.object(scan1m, "five_minute_history", return_value=frames_5m),
                patch.object(scan1m, "one_minute_frames", return_value=frames_1m),
                patch.object(scan1m.data, "fetch_daily", return_value={}),
                patch.object(scan1m, "run_market", return_value=market)]

    def test_the_forming_bar_reaches_the_market_engine(self):
        history = {s: frame_5m(*[BAR - timedelta(minutes=5 * i) for i in range(5, 0, -1)])
                   for s in ("NIFTY", "VIX", "CNXFMCG", "CNXIT")}
        prints = {s: frame_1m(0, 1) for s in ("NIFTY", "VIX", "CNXFMCG", "CNXIT")}
        seen = {}

        def capture(nifty_5, nifty_d, vix_5, vix_d, sector_5, sector_d, **kwargs):
            seen["last"] = nifty_5.index[-1]
            seen["nifty_bars"] = len(nifty_5)
            return md("FMCG")

        patches = self._patches(history, prints, None)
        patches[-1] = patch.object(scan1m, "run_market", side_effect=capture)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        out = scan1m.projected_market(BAR, MIN1)
        self.assertEqual(out.pick_sector, "FMCG")
        self.assertEqual(seen["last"], BAR)          # the forming candle, not PREV
        self.assertEqual(seen["nifty_bars"], 5 + 1)  # 5 completed + the projection

    def test_sanitation_moving_the_bar_is_refused(self):
        history = {s: frame_5m(PREV) for s in ("NIFTY", "VIX", "CNXFMCG", "CNXIT")}
        prints = {s: frame_1m(0) for s in ("NIFTY", "VIX", "CNXFMCG", "CNXIT")}
        for p in self._patches(history, prints, md("FMCG", bar=PREV)):
            p.start()
            self.addCleanup(p.stop)
        self.assertIsNone(scan1m.projected_market(BAR, MIN1))

    def test_no_nifty_frame_means_no_projection(self):
        for p in self._patches({}, {}, md("FMCG")):
            p.start()
            self.addCleanup(p.stop)
        self.assertIsNone(scan1m.projected_market(BAR, MIN1))


class LeaderFixture(TestCase):
    """One sector flipping to FMCG with ITC inside it — the shared world."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        scan1m._pick_cache.clear()
        scan1m._five_cache.clear()
        scan1m._one_cache.clear()
        scan1m._index_daily.clear()
        self.bot3: list[str] = []
        self.env = patch.dict("os.environ", {
            "DRY_RUN": "0", "SCAN1M_FIRST_MINUTE": "1", "SCAN1M_MAX_STALE_SECONDS": "100",
            "SCAN1M_EARLY_PER_DAY": "1", "SCAN1M_CANCEL_MESSAGE": "0",
            "SCAN1M_MARKET_SOURCE": "own", "TELEGRAM_BOT_TOKEN_3": "",
            "TELEGRAM_CHAT_ID_3": ""}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.universe = {"nifty": "NIFTY", "vix": "VIX",
                         "sectors": {"FMCG": {"index": "CNXFMCG",
                                              "stocks": ["ITC", "DABUR"]},
                                     "IT": {"index": "CNXIT", "stocks": ["INFY"]}}}
        self.history = {s: frame_5m(*[BAR - timedelta(minutes=5 * i) for i in range(25, 0, -1)])
                        for s in ("NIFTY", "VIX", "CNXFMCG", "CNXIT")}
        self.prints = {s: frame_1m(0, 1, 2, 3) for s in ("NIFTY", "VIX", "CNXFMCG", "CNXIT")}
        self.scanned: list[str] = []
        self.fires = {"ITC"}
        self.fire_time = BAR.strftime("%H:%M")
        self.patches = [
            patch.object(scan1m, "LEDGER", self.dir / "scan1m.json"),
            patch.object(state, "STATE_PATH", self.dir / "scan_state.json"),
            patch.object(scan1m, "now_ist", side_effect=lambda: MIN1),
            patch.object(scan1m.data, "load_universe", return_value=self.universe),
            patch.object(scan1m.data, "yahoo_stock", side_effect=lambda raw: raw + ".NS"),
            patch.object(scan1m.data, "fetch_many", side_effect=self._fetch),
            patch.object(scan1m.data, "fetch_daily", return_value={}),
            patch.object(scan1m.data, "apply_1m_volume", side_effect=lambda df, _1m: df),
            patch.object(scan1m, "projected_market", return_value=md("FMCG")),
            patch.object(scan1m, "_scan_list", side_effect=self._scan_list),
            patch.object(scan1m, "scan_stock", side_effect=self._scan),
            patch.object(scan1m, "build_plan", return_value=PLAN),
            patch.object(scan1m, "_oi_reader", return_value=None),
            patch.object(scan1m, "market_alert",
                         side_effect=AssertionError("a projected pick must not announce a "
                                                    "MARKET/SECTOR change")),
            patch.object(scan1m.telegram, "send_bot3", side_effect=self._send_bot3),
            patch.object(telegram, "send"),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    # --- fixtures ------------------------------------------------------------
    def _fetch(self, tickers, period, interval):
        out = {}
        for t in tickers:
            if interval == "1m":
                out[t] = self.prints.get(t, self.prints["NIFTY"])
            else:
                out[t] = self.history.get(t, self.history["NIFTY"])
        return out

    def _scan_list(self, uni, pick):
        self.scanned.append(pick["sector"])
        return {raw: pick["sector"] for raw in uni["sectors"][pick["sector"]]["stocks"]}

    def _scan(self, symbol, frame, daily, keep_all=False):
        if symbol in self.fires:                # default: the leader, on the flip candle
            return [signal(symbol=symbol, time=self.fire_time)]
        return []

    def _send_bot3(self, text, dry_run=False):
        self.bot3.append(text)
        return True

    def _send_texts(self) -> list[str]:
        return self.bot3


class LeaderAlertTests(LeaderFixture):
    """A leader firing on the flip candle is projected; the followers are not."""
    def test_the_leader_of_the_new_sector_is_projected_before_the_close(self):
        out = scan1m.run_minute(BAR, now=MIN1)
        self.assertEqual(self.scanned, ["FMCG"])          # the projected pick, not IT
        self.assertEqual(len(self.bot3), 1)
        self.assertIn("EARLY BUY", self.bot3[0])
        self.assertIn("ITC", self.bot3[0])
        self.assertIn("Sector <b>FMCG</b>", self.bot3[0])
        self.assertIn("3m 54s left", self.bot3[0])        # minute 1 of 4 → four minutes early
        self.assertIn("judged on minute 1/4", self.bot3[0])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["minute"], 1)
        ledger = scan1m.load_ledger()
        self.assertEqual(ledger["stats"]["lead_min"], 4)
        self.assertEqual(ledger["stats"]["early"], 1)
        self.assertNotIn("ITC", ledger["sent"])           # a projection is not an entry

    def test_the_same_candle_is_projected_once(self):
        scan1m.run_minute(BAR, now=MIN1)
        scan1m.run_minute(BAR, now=MIN3)                  # the loop's next minute
        self.assertEqual(len(self.bot3), 1)

    def test_a_follower_that_fires_later_still_gets_its_own_early_alert(self):
        """The projection is per name and per candle — not one alert per candle."""
        scan1m.run_minute(BAR, now=MIN1)                  # ITC fires at minute 1
        self.fires = {"DABUR"}                            # the follower fires at minute 3
        scan1m.run_minute(BAR, now=MIN3)
        self.assertEqual(len(self.bot3), 2)
        self.assertIn("DABUR", self.bot3[1])
        self.assertIn("1m 54s left", self.bot3[1])        # minute 3 → two minutes early

    def test_the_summary_reports_the_lead_and_why_names_were_skipped(self):
        scan1m.run_minute(BAR, now=MIN1)
        st = scan1m.load_ledger()
        text = scan1m.format_summary(st)
        self.assertIn("Average lead 4.0 min", text)
        self.assertIn("no signal on the forming candle", text)
        self.assertIn("fired before their sector was picked", scan1m.format_summary({
            **st, "stats": {**st["stats"], "rejected": {"earlier": {"ITC": "ITC 09:20"}}}}))


class RepeatProjectionTests(LeaderFixture):
    """Defect 1 in the live feed: a name that was cancelled never led again.

    HINDUNILVR, 16 Nov: projected 09:48, cancelled at the 09:50 close, then
    entered at 10:00 — the same second bot 1 sent it, with no lead at all. The
    day's cap (``SCAN1M_EARLY_PER_DAY=1``) was tested *before* the retry rule, so
    one cancellation silenced the name until the next session.
    """

    def _second_candle(self):
        """Rebase the fixture on 10:05–10:10 and let ITC fire again."""
        bar = BAR + timedelta(minutes=5)
        now = bar + timedelta(minutes=1, seconds=6)
        for cache in (scan1m._five_cache, scan1m._one_cache, scan1m._pick_cache):
            cache.clear()
        self.prints = {s: frame_1m(5, 6, 7, 8) for s in self.prints}
        self.history = {s: frame_5m(*[bar - timedelta(minutes=5 * i) for i in range(25, 0, -1)])
                        for s in self.history}
        self.fires = {"ITC"}
        self.fire_time = "10:05"
        return bar, now

    def _cancel_on_close(self, bar=BAR):
        st = scan1m.load_ledger()
        scan1m._settle(st, bar, set(), checked={"ITC", "DABUR"},
                       scanned={"ITC", "DABUR"})
        scan1m.save_ledger(st)
        return st

    def test_the_name_still_gets_an_early_alert_after_a_cancelled_projection(self):
        scan1m.run_minute(BAR, now=MIN1)
        self.assertEqual(len(self.bot3), 1)
        self._cancel_on_close()
        bar, now = self._second_candle()
        with patch.object(scan1m, "now_ist", return_value=now):
            out = scan1m.run_minute(bar, now=now)
        self.assertEqual(len(self.bot3), 2, "a cancelled projection must not silence the name")
        self.assertIn("EARLY BUY", self.bot3[1])
        self.assertIn("ITC", self.bot3[1])
        self.assertEqual(out[0]["minute"], 1)
        self.assertEqual(scan1m.load_ledger()["early"]["ITC"]["n"], 2)

    def test_a_confirmed_projection_still_spends_the_name_for_the_day(self):
        scan1m.run_minute(BAR, now=MIN1)
        st = scan1m.load_ledger()
        st["early"]["ITC"]["result"] = "confirmed"
        scan1m.save_ledger(st)
        bar, now = self._second_candle()
        with patch.object(scan1m, "now_ist", return_value=now):
            scan1m.run_minute(bar, now=now)
        self.assertEqual(len(self.bot3), 1)               # no second projection for ITC


class LeaderCatchupTests(LeaderFixture):
    """Turned on by request: the leader that fired BEFORE its sector was picked.

    The scan rejects such a signal as ``earlier`` — correctly, it is not a new
    signal on the candle being scanned — and before this it stayed silent while
    the followers, which fire on the later candles, got their alerts. Bot 3 now
    announces it once, labelled, bounded and only while the trade is still worth
    taking. It is never an entry: ``sent`` and ``positions`` stay untouched.
    """

    def setUp(self):
        super().setUp()
        self.fire_time = "09:40"                  # fired before the 10:00 pick
        self.five = {s: frame_5m(*[BAR - timedelta(minutes=5 * i) for i in range(25, 0, -1)],
                                 close=100.0) for s in ("NIFTY", "VIX", "CNXFMCG", "CNXIT")}
        for df in self.five.values():
            df["High"] = 100.02
            df["Low"] = 99.99                     # the stop (99.98) is never touched
        self.history = self.five
        self.prints = {s: frame_1m(0, 1) for s in ("NIFTY", "VIX", "CNXFMCG", "CNXIT")}
        for df in self.prints.values():
            df["Low"] = 99.99                     # the 1m prints must not touch the stop

    def test_the_missed_leader_is_announced_once_when_its_sector_is_picked(self):
        out = scan1m.run_minute(BAR, now=MIN1)
        self.assertEqual(len(self.bot3), 1)
        text = self.bot3[0]
        self.assertIn("LATE LEADER BUY", text)
        self.assertIn("fired before its sector was picked", text)
        self.assertIn("ITC", text)
        self.assertIn("09:40", text)
        self.assertIn("Not an entry alert", text)
        self.assertEqual([m["symbol"] for m in out], ["ITC"])
        self.assertTrue(out[0]["catchup"])
        ledger = scan1m.load_ledger()
        self.assertEqual(ledger["stats"]["catchup"], 1)
        self.assertNotIn("ITC", ledger["sent"])        # not an entry
        self.assertNotIn("ITC", ledger["positions"])   # no trade opened
        scan1m.run_minute(BAR, now=MIN3)               # the loop's next minute
        self.assertEqual(len(self.bot3), 1)            # once per name per session

    def test_the_knob_turns_it_off(self):
        with patch.dict("os.environ", {"SCAN1M_LEADER_CATCHUP": "0"}, clear=False):
            self.assertEqual(scan1m.run_minute(BAR, now=MIN1), [])
        self.assertEqual(self.bot3, [])

    def test_a_name_projected_on_this_candle_is_not_also_caught_up(self):
        """One message per name per minute: the projection wins."""
        self.fire_time = BAR.strftime("%H:%M")         # ITC also fires on the 10:00 candle
        self.history = {s: frame_5m(*[BAR - timedelta(minutes=5 * i) for i in range(25, 0, -1)])
                        for s in self.five}
        out = scan1m.run_minute(BAR, now=MIN1)
        self.assertEqual(len(self.bot3), 1)
        self.assertIn("EARLY BUY", self.bot3[0])
        self.assertNotIn("LATE LEADER", self.bot3[0])
        self.assertTrue(out[0]["early"])

    def test_the_summary_reports_the_catch_up(self):
        scan1m.run_minute(BAR, now=MIN1)
        self.assertIn("LATE LEADER catch-ups 1", scan1m.format_summary(scan1m.load_ledger()))

    # --- what "still valid" means -------------------------------------------
    def _candidate(self, *, fire_time="09:40", lows=99.99, highs=100.02, now=MIN1,
                   price_now=100.0, env=None):
        times = [BAR - timedelta(minutes=5 * i) for i in range(25, 0, -1)]
        frame = frame_5m(*times, close=100.0)
        frame["Low"], frame["High"] = lows, highs
        forming = frame_1m(0, price=price_now)         # the 10:00 candle so far
        forming["Low"] = min(99.99, price_now - 0.001)   # stays above the 99.98 stop
        projected = data.provisional_5m(frame, forming, BAR, now, 100)
        fires = [signal(time=fire_time, symbol="ITC")]
        with patch.dict("os.environ", env or {}, clear=False):
            return scan1m.catchup_candidate(projected, fires, "BUY", BAR, now)

    def test_a_fire_from_this_candle_is_not_a_catch_up(self):
        self.assertIsNone(self._candidate(fire_time=BAR.strftime("%H:%M")))

    def test_the_reported_stop_cancels_the_catch_up(self):
        self.assertIsNone(self._candidate(lows=99.50))          # traded through 99.98

    def test_a_fire_older_than_the_window_is_dropped(self):
        self.assertIsNone(self._candidate(env={"SCAN1M_LEADER_MAX_MINUTES": "10"}))
        hit = self._candidate(env={"SCAN1M_LEADER_MAX_MINUTES": "60"})
        self.assertIsNotNone(hit)
        self.assertAlmostEqual(hit["age"], 16.0, places=0)      # 09:40 close 09:45 → 10:01

    def test_a_leader_that_already_ran_is_not_chased(self):
        moved = {"price_now": 100.03}                  # +1.5R from the fire (risk 0.02)
        self.assertIsNone(self._candidate(**moved, env={"SCAN1M_LEADER_MAX_R": "0.5"}))
        self.assertIsNotNone(self._candidate(**moved, env={"SCAN1M_LEADER_MAX_R": "2.0"}))

    def test_the_catch_up_is_evaluated_around_the_pick_change_only(self):
        st: dict = {}
        pick = {"sector": "FMCG", "side": 1}
        self.assertTrue(scan1m.catchup_due(st, pick, BAR))               # first sight
        self.assertTrue(scan1m.catchup_due(st, pick, BAR + timedelta(minutes=3)))
        self.assertFalse(scan1m.catchup_due(st, pick, BAR + timedelta(minutes=15)))
        self.assertTrue(scan1m.catchup_due(st, {"sector": "IT", "side": 1}, BAR))  # new pick


class RetryAndSettleTests(TestCase):
    """What the 16 Nov feed exposed, in units.

    * a projection that did not hold used to silence the name for the rest of the
      day, because the per-day cap was tested before the retry rule — so every
      later alert for that name was a same-second duplicate of bot 1
      (HINDUNILVR: projected 09:48, cancelled 09:50, entered 10:00 with no lead);
    * a projection whose candle picked a different sector was never resolved:
      not counted as cancelled, and its ``n`` still at the cap.
    """

    # --- a cancelled projection gets its own retry budget -------------------
    def _prev(self, *, n=1, result="cancelled", bar="2026-11-16T09:45:00+05:30"):
        return {"bar": bar, "session": "2026-11-16", "n": n, "result": result}

    def test_a_cancelled_projection_may_be_retried(self):
        with patch.dict("os.environ", {"SCAN1M_EARLY_PER_DAY": "1",
                                       "SCAN1M_EARLY_RETRIES": "2"}, clear=False):
            self.assertTrue(scan1m._early_allowed(self._prev(n=1), "2026-11-16", BAR))
            self.assertTrue(scan1m._early_allowed(self._prev(n=2), "2026-11-16", BAR))
            self.assertFalse(scan1m._early_allowed(self._prev(n=3), "2026-11-16", BAR))

    def test_the_retry_budget_can_be_switched_off(self):
        with patch.dict("os.environ", {"SCAN1M_EARLY_PER_DAY": "1",
                                       "SCAN1M_EARLY_RETRIES": "0"}, clear=False):
            self.assertFalse(scan1m._early_allowed(self._prev(n=1), "2026-11-16", BAR))

    def test_a_confirmed_projection_still_spends_the_day(self):
        with patch.dict("os.environ", {"SCAN1M_EARLY_PER_DAY": "1",
                                       "SCAN1M_EARLY_RETRIES": "2"}, clear=False):
            self.assertFalse(scan1m._early_allowed(self._prev(n=1, result=None),
                                                   "2026-11-16", BAR))
            self.assertFalse(scan1m._early_allowed(self._prev(n=1, result="confirmed"),
                                                   "2026-11-16", BAR))

    def test_one_projection_per_candle_even_after_a_cancel(self):
        with patch.dict("os.environ", {"SCAN1M_EARLY_RETRIES": "2"}, clear=False):
            prev = self._prev(bar=BAR.isoformat())
            self.assertFalse(scan1m._early_allowed(prev, "2026-11-16", BAR))

    # --- a projection whose sector moved is resolved, not left dangling -----
    def _st(self):
        return {"session": "2026-11-16", "sent": {}, "positions": {}, "stats": {},
                "early": {"DIVISLAB": {"bar": BAR.isoformat(), "side": "BUY", "price": 100.0,
                                       "at": "09:43:00", "name": "BUY-EX6", "n": 1,
                                       "result": None},
                          "SUNPHARMA": {"bar": BAR.isoformat(), "side": "BUY", "price": 100.0,
                                        "at": "09:36:00", "name": "BUY-EX7", "n": 1,
                                        "result": None}}}

    def test_a_name_the_close_no_longer_scans_is_written_off(self):
        st = self._st()
        scan1m._settle(st, BAR, set(), checked={"ITC"}, scanned={"ITC"})
        self.assertEqual(st["early"]["DIVISLAB"]["result"], "cancelled")
        self.assertEqual(st["early"]["SUNPHARMA"]["result"], "cancelled")
        self.assertEqual(st["stats"]["cancelled"], 2)

    def test_a_name_whose_feed_had_not_published_is_still_retried(self):
        st = self._st()
        scan1m._settle(st, BAR, set(), checked=set(),
                       scanned={"DIVISLAB", "SUNPHARMA"})
        self.assertIsNone(st["early"]["DIVISLAB"]["result"])
        self.assertNotIn("cancelled", st["stats"])

    # --- the alert says which pick it judged -------------------------------
    def test_the_early_alert_names_the_pick_it_used(self):
        text = scan1m.format_projected(signal(), {"sector": "FMCG", "label": "BULL",
                                                  "conf": 63, "projected": True},
                                       PLAN, BAR, MIN1, minute=1)
        self.assertIn("pick projected from the forming candle", text)
        fallback = scan1m.format_projected(signal(), {"sector": "FMCG", "label": "BULL",
                                                      "conf": 63, "projected": False},
                                           PLAN, BAR, MIN1, minute=1)
        self.assertIn("pick: last close (no projection this minute)", fallback)

    # --- the alert names the stock's own sector ----------------------------
    def test_a_name_from_the_follow_sector_is_not_called_the_pick(self):
        # 17 Nov: SUNTV (MEDIA) was announced as "Sector REALTY" because REALTY
        # was the pick and MEDIA was one of the `also scanning` sectors.
        pick = {"sector": "REALTY", "label": "NEUTRAL", "conf": 63, "projected": True}
        text = scan1m.format_projected(signal(), pick, PLAN, BAR, MIN1, minute=1,
                                       sector="MEDIA")
        self.assertIn("Sector <b>MEDIA</b>  (scanned with REALTY)", text)
        self.assertNotIn("Sector <b>REALTY</b>", text)

    def test_a_name_in_the_picked_sector_says_it_once(self):
        pick = {"sector": "REALTY", "label": "NEUTRAL", "conf": 63, "projected": True}
        text = scan1m.format_projected(signal(), pick, PLAN, BAR, MIN1, minute=1,
                                       sector="REALTY")
        self.assertIn("Sector <b>REALTY</b>  |", text)
        self.assertNotIn("scanned with", text)

    def test_the_late_leader_does_the_same(self):
        pick = {"sector": "REALTY", "label": "NEUTRAL", "conf": 61}
        hit = {"sig": signal(), "plan": PLAN, "price": PLAN.entry, "r": 0.2, "age": 4.0}
        text = scan1m.format_catchup("BANKBARODA", hit, pick, BAR, MIN1, sector="PSUBK")
        self.assertIn("Sector <b>PSUBK</b>  (scanned with REALTY)", text)

    def test_the_fallback_is_logged_once_per_candle(self):
        scan1m._pick_notes.clear()
        with self.assertLogs("nse_scanner.scan1m", level="INFO") as logs:
            scan1m._note_pick_fallback(BAR, "no forming bar for ^NSEI")
        self.assertEqual(len(logs.output), 1)
        self.assertIn("no forming bar for ^NSEI", logs.output[0])
        with self.assertNoLogs("nse_scanner.scan1m", level="INFO"):
            scan1m._note_pick_fallback(BAR, "no forming bar for ^NSEI")   # same candle


class RealMarketEngineTests(TestCase):
    """The seam no mock covers: ``run_market`` on a projected Nifty/sector frame.

    A synthetic session where one sector index runs up hard and the rest drift
    down. The pick first becomes available *inside* a candle: the forming candle
    already says "AUTO, long" while the last closed candle still says WAIT — the
    flip candle, the one the sector's leader fires on. Before the projection the
    1m scan scanned nothing on that candle and echoed bot 1 at its close.
    Measured flip in this synthetic session: the 10:55 candle.
    """

    def setUp(self):
        self.uni = data.load_universe()                       # the real config
        idx = pd.date_range(pd.Timestamp("2026-11-13 09:15", tz=IST), periods=75, freq="5min")
        self.bars = idx
        syms = [self.uni["nifty"], self.uni["vix"]] + [s["index"] for s in self.uni["sectors"].values()]
        auto = self.uni["sectors"]["AUTO"]["index"]
        drift = {s: -0.01 for s in syms}
        drift[auto], drift[self.uni["nifty"]], drift[self.uni["vix"]] = 0.25, 0.06, -0.5
        self.fives = {s: self._frame5(drift[s]) for s in syms}
        self.ones = {s: self._to_1m(f) for s, f in self.fives.items()}

    def _frame5(self, drift):
        rng = np.random.default_rng(abs(int(round(drift * 1000))) + 1)
        close = 100 + drift * np.arange(len(self.bars)) + np.cumsum(rng.normal(0, 0.02, len(self.bars)))
        open_ = np.r_[close[0], close[:-1]]
        return pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) + 0.03,
                             "Low": np.minimum(open_, close) - 0.03, "Close": close,
                             "Volume": np.full(len(self.bars), 1e6)}, index=self.bars)

    def _to_1m(self, df5):
        rows = []
        for ts, r in df5.iterrows():
            for i in range(5):
                c = r["Open"] + (r["Close"] - r["Open"]) * (i + 1) / 5
                rows.append((ts + timedelta(minutes=i), r["Open"] if i == 0 else c,
                             max(c, r["High"]), min(c, r["Low"]),
                             r["Close"] if i == 4 else c, 1e6 / 5))
        return pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"]
                            ).set_index("ts")

    def _pick_at(self, bar, now):
        fives = {s: f.loc[f.index <= bar] for s, f in self.fives.items()}
        ones = {s: o.loc[o.index < bar + timedelta(minutes=5)] for s, o in self.ones.items()}
        with patch.object(scan1m, "five_minute_history", return_value=fives), \
             patch.object(scan1m, "one_minute_frames", return_value=ones), \
             patch.object(scan1m.data, "fetch_daily", return_value={}):
            scan1m._index_daily.clear()
            return scan1m.projected_market(bar, now)

    def test_the_pick_moves_inside_the_candle_bot_1_can_only_see_at_its_close(self):
        leads = []
        for bar in self.bars[20:32]:                          # 10:55 … 11:50
            prev = bar - timedelta(minutes=5)
            forming = self._pick_at(bar, bar + timedelta(minutes=2))
            closed = self._pick_at(prev, prev + timedelta(minutes=5))
            self.assertIsNotNone(forming)
            self.assertIsNotNone(closed)
            self.assertEqual(forming.bar_time, bar.strftime("%Y-%m-%d %H:%M"))
            if forming.pick_sector != closed.pick_sector and forming.pick_sector != "WAIT":
                leads.append((bar, forming.pick_sector, closed.pick_sector))
        self.assertEqual(len(leads), 1, f"expected exactly one flip in the window, got {leads}")
        bar, sector, was = leads[0]
        self.assertEqual(sector, "AUTO")
        self.assertEqual(was, "WAIT")        # bot 1 had nothing to scan on that candle
        self.assertEqual(str(bar.time()), "10:55:00")
