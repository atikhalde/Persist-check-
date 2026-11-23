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

    def test_payload_uses_persistence_not_direction_scores(self):
        md = read(sectors={"AUTO": {"side": 1, "bull": 60, "bear": 40,
                                    "persist_bull": 91, "persist_bear": 10},
                           "BANK": {"side": -1, "bull": 20, "bear": 70,
                                    "persist_bull": 5, "persist_bear": 86}})
        pl = live_screen.payload_for(UNI, md, {}, base.candle(11))
        for code, side, persist in (("AUTO", 1, 91), ("BANK", -1, 86)):
            self.assertEqual(pl["sectors"][code]["side"], side)
            self.assertEqual(pl["sectors"][code]["persist"], persist)
            self.assertEqual(pl["sectors"][code]["persist"],
                             live_screen._cell(md, code, UNI["sectors"][code])["persist"])

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

    def test_result_counts_qualified_candidates_not_only_cards(self):
        st = screen.fresh("2026-11-20")
        frames = {base.data.yahoo_stock("MARUTI"): base.rising(10_000, 8, 12)}
        with mock.patch.dict(os.environ, {"SCREEN_LAST_ENTRY": "09:30"}), \
                mock.patch.object(screen, "load", return_value=st), \
                mock.patch.object(screen, "save"), \
                mock.patch.object(live_screen.telegram, "send_bot4", return_value=True):
            out = live_screen.close_pass(uni=UNI, md=read(), frames=frames,
                                         last=base.candle(11))
        self.assertEqual(out["candidates"], 1)
        self.assertEqual(out["surfaced"], [])

    def test_a_dry_run_does_not_send_its_update_lines(self):
        """DRY_RUN=1 posted CONFIRMED/CANCELLED to bot 4 before this test existed."""
        frames = {base.data.yahoo_stock("MARUTI"): base.rising(10_000, 8, 12)}
        st = screen.fresh("2026-11-20")
        posted = []
        with mock.patch.dict(os.environ, {"DRY_RUN": "1"}), \
                mock.patch.object(screen, "load", return_value=st), \
                mock.patch.object(screen, "save"), \
                mock.patch.object(screen, "updates", return_value=["CONFIRMED #1"]), \
                mock.patch.object(live_screen.telegram, "send_bot4",
                                  side_effect=lambda t, **k: posted.append((t, k)) or True):
            live_screen.close_pass(uni=UNI, md=read(), frames=frames,
                                   last=base.candle(11))
        self.assertTrue(posted, "the update line should still be attempted")
        for _text, kwargs in posted:
            self.assertIs(kwargs.get("dry_run"), True, "a dry run must not post")

    def test_the_fetch_budget_is_the_strongest_books(self):
        fresh = {"AUTO": {"side": 1, "bull": 92, "bear": 8, "persist_bull": 92,
                          "persist_bear": 8, "day_ret": 3.1, "intra_ret": 0.4,
                          "qualified": True}}
        uni = {"nifty": "^NSEI", "vix": "^INDIAVIX", "sectors": {}}
        for i, persist in enumerate((99, 95, 90, 88, 85, 82, 80)):
            code = f"S{i}"
            fresh[code] = {"side": 1, "bull": persist, "bear": 100 - persist,
                           "persist_bull": persist, "persist_bear": 100 - persist,
                           "day_ret": 2.0, "intra_ret": 0.3, "qualified": True}
            uni["sectors"][code] = {"index": f"^X{i}", "stocks": [f"N{i}"]}
        got = live_screen.sectors_to_fetch(uni, read(sectors=fresh), {},
                                           limit=live_screen.EARLY_SECTORS)
        self.assertEqual(len(got), 3)
        self.assertEqual(got[0], "S0")          # strongest persistence first

    def test_a_failed_pass_never_takes_the_loop_down(self):
        with mock.patch.object(screen, "load", side_effect=RuntimeError("corrupt ledger")):
            out = live_screen.close_pass(uni=UNI, md=read(), frames={},
                                         last=base.candle(11))
        self.assertEqual(out["bar"], None)
        self.assertEqual(out["candidates"], 0)


class OnePassPerCandleTests(unittest.TestCase):
    """The screen runs at the top of the 5m cycle and on a retry — never twice."""

    def test_the_same_candle_is_not_judged_twice(self):
        frames = {base.data.yahoo_stock("MARUTI"): base.rising(10_000, 8, 12)}
        st = screen.fresh("2026-11-20")
        sent = []
        with mock.patch.object(screen, "load", return_value=st), \
                mock.patch.object(screen, "save"), \
                mock.patch.object(live_screen.telegram, "send_bot4",
                                  side_effect=lambda t, **k: sent.append(t) or True):
            first = live_screen.close_pass(uni=UNI, md=read(), frames=frames,
                                           last=base.candle(11))
            second = live_screen.close_pass(uni=UNI, md=read(), frames=frames,
                                            last=base.candle(11))
        self.assertEqual(len(first["surfaced"]), 1)
        self.assertTrue(second.get("skipped"))
        self.assertEqual(second["surfaced"], [])
        self.assertEqual(len(sent), 1, "the retry carded the same name again")

    def test_previous_session_same_label_does_not_skip_new_day(self):
        st = screen.fresh("2026-11-19")
        st["scan"] = [{"b": "10:10"}]
        frames = {base.data.yahoo_stock("MARUTI"): base.rising(10_000, 8, 12)}
        with mock.patch.object(screen, "load", return_value=st), \
                mock.patch.object(screen, "save"), \
                mock.patch.object(live_screen.telegram, "send_bot4", return_value=True):
            out = live_screen.close_pass(uni=UNI, md=read(), frames=frames,
                                         last=base.candle(11))
        self.assertFalse(out.get("skipped"))
        self.assertEqual(st["session"], "2026-11-20")
        self.assertEqual(len(out["surfaced"]), 1)

    def test_retry_of_earlier_recorded_bar_is_not_judged(self):
        st = screen.fresh("2026-11-20")
        st["scan"] = [{"b": "10:10"}, {"b": "10:15"}]
        with mock.patch.object(screen, "load", return_value=st):
            out = live_screen.close_pass(uni=UNI, md=read(), frames={},
                                         last=base.candle(11))
        self.assertTrue(out["skipped"])

    def test_the_next_candle_is_judged_again(self):
        st = screen.fresh("2026-11-20")
        frames = {base.data.yahoo_stock("MARUTI"): base.rising(10_000, 8, 13)}
        with mock.patch.object(screen, "load", return_value=st), \
                mock.patch.object(screen, "save"), \
                mock.patch.object(live_screen.telegram, "send_bot4", return_value=True):
            live_screen.close_pass(uni=UNI, md=read(), frames=frames, last=base.candle(11))
            second = live_screen.close_pass(uni=UNI, md=read(), frames=frames,
                                            last=base.candle(12))
        self.assertFalse(second.get("skipped"))


class NameWaitTests(unittest.TestCase):
    """The index bar and the stock bars publish at different moments."""

    def test_it_polls_until_the_names_carry_the_closed_bar(self):
        stale, fresh = base.rising(10_000, 8, 11), base.rising(10_000, 8, 12)
        calls = []

        def fetch(uni, sectors, *, last=None):
            calls.append(1)
            return {base.data.yahoo_stock("MARUTI"): (fresh if len(calls) >= 3 else stale)}

        with mock.patch.object(live_screen, "name_frames", side_effect=fetch), \
                mock.patch.object(live_screen.time, "sleep"):
            frames = live_screen.name_frames_when_ready(UNI, ["AUTO"], base.candle(11))
        self.assertEqual(len(calls), 3)
        self.assertEqual(live_screen._fresh_names(frames, base.candle(11)), 1)

    def test_the_wait_is_bounded(self):
        stale = base.rising(10_000, 8, 11)
        calls = []
        with mock.patch.object(live_screen, "name_frames",
                               side_effect=lambda *a, **k: calls.append(1) or
                               {base.data.yahoo_stock("MARUTI"): stale}), \
                mock.patch.object(live_screen.time, "sleep"):
            live_screen.name_frames_when_ready(UNI, ["AUTO"], base.candle(11), budget_s=0)
        self.assertEqual(len(calls), 1, "a zero budget must not poll at all")

    def test_a_stale_name_is_counted_not_ignored(self):
        st = screen.fresh("2026-11-20")
        last = base.candle(11)
        seen = to_ist(last) + timedelta(minutes=5, seconds=1.4)
        live_screen.note_latency(st, last, seen, seen + timedelta(milliseconds=250),
                                 15, 14, save=None)
        self.assertEqual(st["stats"]["feed_s"][-1], 1.4)
        self.assertAlmostEqual(st["stats"]["work_s"][-1], 0.25, places=2)
        self.assertEqual(st["stats"]["names_stale"], 1)


class FactoryWiringTests(unittest.TestCase):
    """Where in the 5m cycle the screen runs, and that it does not need a pick."""

    def test_it_runs_before_the_engine_plans_anything(self):
        from nse_scanner import pipeline
        import inspect

        src = inspect.getsource(pipeline.run_once)
        call = src.index("_screen_pass(md, last, uni)")
        self.assertLess(call, src.index("format_market(md"), "the market alert is sent first")
        self.assertLess(call, src.index('md.pick_sector == "WAIT"'),
                        "a WAIT day returns before the screen runs")
        self.assertLess(call, src.index("sm1 = data.fetch_many"),
                        "the screen waits for the 1m fetch")

    def test_a_wait_day_still_screens(self):
        from nse_scanner import pipeline

        md = SimpleNamespace(dir_state=0, dir_label="WAIT", dir_conf=0, regime="",
                             pick_sector="WAIT", sectors={})
        with mock.patch.object(live_screen, "close_pass",
                               return_value={"bar": "10:10", "candidates": 0,
                                             "surfaced": [], "feed_s": 1.0,
                                             "screen_s": 0.1}) as cp:
            pipeline._screen_pass(md, base.candle(11), UNI)
        cp.assert_called_once()

    def test_a_screen_failure_never_reaches_the_scan(self):
        from nse_scanner import pipeline

        with mock.patch.object(live_screen, "close_pass", side_effect=RuntimeError("boom")):
            pipeline._screen_pass(read(), base.candle(11), UNI)   # must not raise


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
    def test_a_published_bar_is_judged_at_once(self):
        last = base.candle(11)
        with mock.patch.object(live_screen, "_nifty_last", return_value=to_ist(last)) as poll:
            self.assertTrue(live_screen.wait_for_bar(UNI, last, deadline_s=0))
        poll.assert_called_once()

    def test_an_unpublished_bar_is_skipped_not_guessed(self):
        last = base.candle(11)
        with mock.patch.object(live_screen, "_nifty_last",
                               return_value=to_ist(base.candle(10))), \
                mock.patch.object(live_screen.time, "sleep"):
            self.assertFalse(live_screen.wait_for_bar(UNI, last, deadline_s=0))

    def test_polling_asks_for_one_symbol_not_the_whole_tape(self):
        """Thirteen symbols per poll, every 5s, for two minutes is not a poll."""
        with mock.patch.object(live_screen.data, "fetch_many",
                               return_value={UNI["nifty"]: base.rising(20_000, 2, 12)}) as f:
            live_screen._nifty_last(UNI)
        self.assertEqual(f.call_args[0][0], [UNI["nifty"]])


class _Stop(Exception):
    """End the scripted loop from inside its own sleep."""


class LoopScheduleTests(unittest.TestCase):
    """The clock: what runs, when, and what must not run while bot 1 is alive."""

    def run_loop(self, *, alive, start, sleeps=6):
        clock = SimpleNamespace(t=start)
        calls = {"early": [], "close": [], "eod": 0, "announce": []}

        def sleep_until(target):
            clock.t = max(clock.t, target)
            calls.setdefault("sleeps", []).append(target.strftime("%H:%M:%S"))
            if len(calls["sleeps"]) >= sleeps:
                raise _Stop

        # should_screen returns (do_it, why): a live 5m scan means "stand down".
        with mock.patch.object(live_screen, "now_ist", side_effect=lambda: clock.t), \
                mock.patch.object(live_screen, "_sleep_until", side_effect=sleep_until), \
                mock.patch.object(live_screen, "should_screen",
                                  side_effect=lambda **_k: (not alive,
                                                            "5m run is alive" if alive
                                                            else "no 5m run")), \
                mock.patch.object(live_screen, "early_pass",
                                  side_effect=lambda f, **_k: calls["early"].append(
                                      f.strftime("%H:%M")) or []), \
                mock.patch.object(live_screen, "close_pass",
                                  side_effect=lambda **_k: calls["close"].append(
                                      _k.get("last").strftime("%H:%M"))
                                  or {"bar": None, "candidates": 0, "surfaced": [], "setups": 0}), \
                mock.patch.object(live_screen, "wait_for_bar", return_value=True), \
                mock.patch.object(live_screen, "end_of_session",
                                  side_effect=lambda **_k: calls.__setitem__("eod", calls["eod"] + 1)), \
                mock.patch.object(live_screen, "_announce",
                                  side_effect=lambda why: calls["announce"].append(why)), \
                mock.patch.object(live_screen.data, "load_universe", return_value=UNI), \
                mock.patch.object(live_screen.data, "enable_daily_cache"), \
                mock.patch("nse_scanner.knobs.describe", return_value=[]):
            try:
                reason = live_screen.live_loop_screen(exit_at_close=False)
            except _Stop:
                reason = "scripted"
        return SimpleNamespace(reason=reason, calls=calls, clock=clock)

    def test_a_live_5m_scan_gets_every_candle_and_bot_4_does_nothing(self):
        run = self.run_loop(alive=True, start=DAY.replace(hour=9, minute=9, second=30))
        self.assertEqual(run.calls["early"], [])
        self.assertEqual(run.calls["close"], [])
        self.assertEqual(run.calls["announce"], [])
        self.assertGreaterEqual(len(run.calls["sleeps"]), 3)

    def test_a_dead_5m_scan_gets_the_provisional_card_a_minute_early(self):
        run = self.run_loop(alive=False, start=DAY.replace(hour=9, minute=9, second=30))
        # the 09:15 candle: provisional pass at 09:19 for 09:15, close pass at 09:20
        self.assertEqual(run.calls["early"][:1], ["09:15"])
        self.assertEqual(run.calls["close"][:1], ["09:15"])
        self.assertIn("09:19:00", run.calls["sleeps"])
        # close + POLL_GRACE_SECONDS, and POLL_GRACE_SECONDS is 1: the loop asks at
        # :20:01 and polls, instead of sleeping ten seconds and finding the bar had
        # been there for nine of them.
        self.assertIn("09:20:01", run.calls["sleeps"])
        self.assertNotIn("09:20:10", run.calls["sleeps"])
        self.assertTrue(run.calls["announce"], "the takeover is announced")

    def test_a_dead_scan_keeps_screening_candle_after_candle(self):
        run = self.run_loop(alive=False, start=DAY.replace(hour=9, minute=9, second=30),
                            sleeps=10)
        self.assertGreater(len(run.calls["close"]), 1)
        self.assertEqual(run.calls["close"], sorted(run.calls["close"]))

    def test_the_hello_line_is_sent_once_per_ledger(self):
        """Restarting the loop must not greet bot 4 on every run of the day."""
        with mock.patch.object(live_screen.telegram, "send_bot4", return_value=True) as send:
            live_screen._announce("no 5m scan run is active")
            live_screen._announce("no 5m scan run is active")
        self.assertEqual(send.call_count, 1)
        self.assertTrue(send.call_args[0][0].startswith("🎯 Bot 4 screen is live"))


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
