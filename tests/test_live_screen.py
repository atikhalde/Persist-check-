"""Bot 4's own loop: the failover decision, and a pass that needs no network.

The point of these tests is the property the loop exists for — that the screen keeps
working when the 5m job does not — and the property it must not break — that on a
normal day it does *not* screen, so there is only one set of numbers.
"""
from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import pandas as pd

import test_screen as base
from nse_scanner import live_screen, screen
from nse_scanner.session import to_ist

DAY = base.START


def read(*, dir_state=1, pick="AUTO", sectors=None) -> SimpleNamespace:
    """A ``MarketDecision``-shaped market read."""
    return SimpleNamespace(dir_state=dir_state, dir_label="BULL" if dir_state > 0 else "BEAR",
                           dir_conf=70, regime="TREND", pick_sector=pick,
                           bar_time="10:10", sectors=sectors if sectors is not None
                           else {"AUTO": {"side": 1, "bull": 92, "bear": 8,
                                          "persist_bull": 92, "persist_bear": 8,
                                          "day_ret": 3.1, "intra_ret": 0.4,
                                          "qualified": True}})


UNI = {"nifty": "^NSEI", "vix": "^INDIAVIX",
       "sectors": {"AUTO": {"index": "^CNXAUTO", "stocks": ["MARUTI"]},
                   "BANK": {"index": "^NSEBANK", "stocks": ["HDFCBANK"]}}}


class PayloadTests(unittest.TestCase):
    def test_it_builds_the_board_shape_from_this_process_read(self):
        frames = {base.data.yahoo_stock("MARUTI"): base.rising(10_000, 8, 12)}
        two = {"AUTO": {"side": 1, "bull": 92, "bear": 8, "persist_bull": 92,
                        "persist_bear": 8, "day_ret": 3.1, "intra_ret": 0.4,
                        "qualified": True},
               "BANK": {"side": 1, "bull": 80, "bear": 20, "persist_bull": 80,
                        "persist_bear": 20, "day_ret": 1.1, "intra_ret": 0.2,
                        "qualified": True}}
        pl = live_screen.payload_for(UNI, read(sectors=two), frames, base.candle(11))
        self.assertEqual(pl["b"], "10:10")
        self.assertEqual(pl["tilt"], 1)
        self.assertEqual(pl["sectors"]["AUTO"]["n"], ["MARUTI"])
        self.assertEqual(pl["sectors"]["AUTO"]["persist"], 92)
        self.assertEqual(len(pl["sectors"]["AUTO"]["lead"]), 1)  # one leg per name
        self.assertLess(abs(pl["sectors"]["AUTO"]["lead"][0]), 2.0)
        self.assertEqual(pl["sectors"]["BANK"]["n"], [])         # nothing fetched

    def test_the_screen_judges_it_both_ways(self):
        frames = {base.data.yahoo_stock("MARUTI"): base.rising(10_000, 8, 12),
                  base.data.yahoo_stock("HDFCBANK"): base.falling(1_600, 2, 12)}
        md = read(sectors={"AUTO": {"side": 1, "bull": 92, "bear": 8, "persist_bull": 92,
                                    "persist_bear": 8, "day_ret": 3.1, "intra_ret": 0.4,
                                    "qualified": True},
                           "BANK": {"side": -1, "bull": 9, "bear": 88, "persist_bull": 9,
                                    "persist_bear": 88, "day_ret": -1.2, "intra_ret": -0.5,
                                    "qualified": True}})
        pl = live_screen.payload_for(UNI, md, frames, base.candle(11))
        st = screen.fresh("2026-11-20")
        with mock.patch.dict(os.environ, {"SCREEN_COUNTER_PERSIST": "85"}):
            out = screen.consider(st, pl, frames, md, base.candle(11),
                                  send=lambda *_a, **_k: True,
                                  save_state=lambda *_a, **_k: None)
        self.assertEqual({s["side"] for s in out}, {"BUY", "SELL"})

    def test_a_counter_trend_sector_needs_its_own_floor(self):
        md = read(sectors={"BANK": {"side": -1, "bull": 9, "bear": 88, "persist_bull": 9,
                                    "persist_bear": 88, "day_ret": -1.2, "intra_ret": -0.5,
                                    "qualified": True}})
        # market BULL, sector BEAR at 88 < SCREEN_COUNTER_PERSIST (90): not fetched
        self.assertNotIn("BANK", live_screen.sectors_to_fetch(UNI, md, {}))
        with mock.patch.dict(os.environ, {"SCREEN_COUNTER_PERSIST": "85"}):
            self.assertIn("BANK", live_screen.sectors_to_fetch(UNI, md, {}))

    def test_a_weak_sector_is_never_fetched(self):
        md = read(sectors={"AUTO": {"side": 1, "bull": 60, "bear": 40, "persist_bull": 60,
                                    "persist_bear": 40, "day_ret": 0.2, "intra_ret": 0.1,
                                    "qualified": False}})
        self.assertEqual(live_screen.sectors_to_fetch(UNI, md, {}), [])


class FailoverTests(unittest.TestCase):
    """``five_minute_alive`` — the one thing that decides whether bot 4 speaks."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r",
                                                "GITHUB_TOKEN": "t"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _api(self, runs):
        body = json.dumps({"workflow_runs": runs}).encode()
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = body
        return mock.patch("urllib.request.urlopen", return_value=resp)

    def _run(self, status, minutes_ago=1.0, run_id=7):
        when = (datetime.now().astimezone() - timedelta(minutes=minutes_ago))
        return {"id": run_id, "status": status, "created_at": when.isoformat()}

    def test_a_running_scan_means_stand_down(self):
        with self._api([self._run("in_progress")]):
            alive, why = live_screen.five_minute_alive()
        self.assertTrue(alive)
        self.assertIn("in progress", why)

    def test_a_scan_queued_a_moment_ago_is_still_the_scan(self):
        with self._api([self._run("queued", 3)]):
            self.assertTrue(live_screen.five_minute_alive()[0])

    def test_a_run_stuck_in_the_queue_is_not(self):
        with self._api([self._run("queued", 40)]):
            alive, why = live_screen.five_minute_alive()
        self.assertFalse(alive)
        self.assertIn("no 5m scan run is active", why)

    def test_no_run_at_all_means_bot_4_screens(self):
        with self._api([]):
            self.assertFalse(live_screen.five_minute_alive()[0])

    def test_without_a_token_it_screens_rather_than_going_silent(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "", "GH_TOKEN": ""}):
            alive, why = live_screen.five_minute_alive()
        self.assertFalse(alive)
        self.assertIn("so screening", why)
        self.assertTrue(live_screen.should_screen()[0])

    def test_an_api_that_does_not_answer_also_screens(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            alive, why = live_screen.five_minute_alive()
        self.assertFalse(alive)
        self.assertIn("so screening", why)

    def test_the_switch_off_skips_the_question_entirely(self):
        with mock.patch.dict(os.environ, {"SCREEN_FAILOVER": "0"}), \
                mock.patch("urllib.request.urlopen") as api:
            do, why = live_screen.should_screen()
        self.assertTrue(do)
        self.assertIn("always screens", why)
        api.assert_not_called()

    def test_screen_bot_off_stands_down_whatever_the_api_says(self):
        with mock.patch.dict(os.environ, {"SCREEN_BOT": "0"}):
            do, why = live_screen.should_screen()
        self.assertFalse(do)
        self.assertIn("switched off", why)


class ClosePassTests(unittest.TestCase):
    def test_it_sends_only_on_bot_4(self):
        sent = []
        frames = {base.data.yahoo_stock("MARUTI"): base.rising(10_000, 8, 12)}
        st = screen.fresh("2026-11-20")
        with mock.patch.object(screen, "load", return_value=st), \
                mock.patch.object(screen, "save"), \
                mock.patch.object(live_screen.telegram, "send_bot4",
                                  side_effect=lambda t, **_k: sent.append(t) or True), \
                mock.patch.object(live_screen.telegram, "send",
                                  side_effect=AssertionError("bot 1 was used")) as bot1:
            out = live_screen.close_pass(uni=UNI, md=read(), frames=frames,
                                         last=base.candle(11))
        self.assertEqual(out["bar"], "10:10")
        self.assertEqual(len(out["surfaced"]), 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("MARUTI", sent[0])
        bot1.assert_not_called()

    def test_a_failed_pass_never_takes_the_loop_down(self):
        with mock.patch.object(screen, "load", side_effect=RuntimeError("corrupt ledger")):
            out = live_screen.close_pass(uni=UNI, md=read(), frames={},
                                         last=base.candle(11))
        self.assertEqual(out["bar"], None)
        self.assertEqual(out["candidates"], 0)


class EndOfSessionTests(unittest.TestCase):
    def test_a_running_scan_reports_the_audit_itself(self):
        with mock.patch.object(live_screen, "should_screen",
                               return_value=(False, "5m run in progress")), \
                mock.patch.object(live_screen, "end_of_day") as eod:
            self.assertFalse(live_screen.end_of_session())
        eod.assert_not_called()

    def test_a_dead_scan_leaves_the_audit_to_bot_4(self):
        with mock.patch.object(live_screen, "should_screen",
                               return_value=(True, "no 5m scan run is active")), \
                mock.patch.object(live_screen, "end_of_day", return_value=True) as eod:
            self.assertTrue(live_screen.end_of_session())
        eod.assert_called_once()


class WaitForBarTests(unittest.TestCase):
    def _frame(self, last):
        return {UNI["nifty"]: pd.DataFrame(
            {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [0]},
            index=[to_ist(last)])}

    def test_a_published_bar_is_judged_at_once(self):
        last = base.candle(11)
        with mock.patch.object(live_screen, "index_frames",
                               return_value=(self._frame(last), {})):
            self.assertTrue(live_screen.wait_for_bar(UNI, last, deadline_s=0))

    def test_an_unpublished_bar_is_skipped_not_guessed(self):
        last = base.candle(11)
        stale = self._frame(base.candle(10))
        with mock.patch.object(live_screen, "index_frames", return_value=(stale, {})), \
                mock.patch.object(live_screen.time, "sleep"):
            self.assertFalse(live_screen.wait_for_bar(UNI, last, deadline_s=0))


class LoopContractTests(unittest.TestCase):
    """The handover contract, without sleeping through a session."""

    def test_the_budget_returns_before_any_pass(self):
        with mock.patch.object(screen, "load", return_value={}), \
                mock.patch.object(live_screen, "early_pass") as early, \
                mock.patch.object(live_screen, "close_pass") as close:
            with mock.patch.object(live_screen, "now_ist",
                                   return_value=DAY.replace(hour=10, minute=2)):
                out = live_screen.live_loop_screen(max_minutes=0, exit_at_close=True)
        self.assertEqual(out, "budget")
        early.assert_not_called()
        close.assert_not_called()

    def test_outside_the_session_it_does_nothing(self):
        with mock.patch.object(live_screen, "now_ist",
                               return_value=DAY.replace(hour=18, minute=0)):
            self.assertEqual(live_screen.live_loop_screen(exit_at_close=True), "closed")

    def test_a_weekend_does_not_start_a_loop(self):
        with mock.patch.object(live_screen, "now_ist",
                               return_value=DAY.replace(day=21, hour=10)):   # Saturday
            self.assertEqual(live_screen.live_loop_screen(exit_at_close=True), "closed")


if __name__ == "__main__":
    unittest.main()
