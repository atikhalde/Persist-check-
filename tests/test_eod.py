"""The end-of-day report: the whole day in the alert, and nothing silently lost.

The report is the only place the day is described after the fact, so these tests
are about *content* (nothing the scanner knew is missing, and every number is
read from its own book) and about *delivery* (a report that does not fit in one
Telegram message goes out in numbered parts; if any part is refused the session
is not marked as reported).
"""
from __future__ import annotations

import os
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
import test_live_alerts as base       # the patched world and its fixtures
from nse_scanner import eod, pipeline, scan1m, state


def day(positions=None, **stats) -> dict:
    """A session ledger with the shape the scanners really write."""
    return {
        "session": "2026-11-20",
        "market": {"label": "BULL", "conf": 74, "regime": "TREND"},
        "sector": {"name": "AUTO", "side": 1, "reason": "persist"},
        "scan": {"sector": "AUTO", "side": 1, "label": "BULL", "conf": 74,
                 "log": {"2026-11-20T09:20:00": {"name": "IT", "side": 1},
                         "2026-11-20T09:25:00": {"name": "IT", "side": 1},
                         "2026-11-20T09:30:00": {"name": "AUTO", "side": 1},
                         "2026-11-20T15:25:00": {"name": "AUTO", "side": 1}}},
        "sent": {},
        "positions": positions or {},
        "stats": {
            "candles": 75, "picks": 41, "entries": 3, "early_sent": 6,
            "confirmed": 2, "cancelled": 4,
            "cancel_why": {"the close did not confirm the early signal": 3,
                           "the sector changed": 1},
            **stats,
        },
    }


# ── content: nothing the scanner knew is left out ───────────────────────────
class ContentTests(base.TestCase):
    def test_every_blocked_signal_is_listed_by_the_rule_that_blocked_it(self):
        st = day(rejected={
            "follower": {"XYZ": "XYZ AUTO", "PQR": "PQR AUTO"},
            "weak": {f"N{i}": f"N{i} 10:35 — rank 12 is behind its sector's runners"
                     for i in range(9)},
            "earlier": {"AAA": "AAA fired at 09:35, AUTO picked at 10:10"},
        }, weak={"behind its sector's runners": 9, "reversing": 2},
            trim={"AUTO": {"kept": 15, "dropped": 24, "candles": 9}})
        text = eod.format_eod(st)
        self.assertIn("<b>Blocked signals — by rule</b>", text)
        self.assertIn("12 signals blocked in all", text)
        # the count per rule, which the short summary never had
        self.assertIn("2 — ", text)
        self.assertIn("9 — ", text)
        self.assertIn("outside the sector's top", text)   # the follower rule, in words
        self.assertIn("N0 10:35 — rank 12", text)     # a name *and* its reason
        self.assertIn("Momentum gate: behind its sector's runners 9 · reversing 2", text)
        self.assertIn("Alerts on record: none", text)
        self.assertIn("Sector trim (SECTOR_TOP_N): kept 15 name-checks, dropped 24", text)
        self.assertIn("AUTO: kept 15 · dropped 24", text)

    def test_what_went_out_is_counted_by_kind(self):
        st = day()
        st["sent"] = {"A": {"code": "ENTRY"}, "B": {"code": "ENTRY"}, "C": {"code": "T1"},
                      "D": {"code": "SL"}, "E": {"code": "EXIT"}}
        st["undelivered"] = {"F": 1}
        text = eod.format_eod(st)
        self.assertIn("Alerts on record 5 (last code per name): ENTRY 2 · EXIT 1 · SL 1 · T1 1", text)
        self.assertIn("<b>1 undelivered</b>", text)

    def test_one_blocked_signal_reads_as_one(self):
        text = eod.format_eod(day(rejected={"weak": {"AAA": "AAA rank 4 is behind its sector's runners"}}))
        self.assertIn("1 signal blocked in all", text)

    def test_a_reason_that_runs_long_is_clipped_not_dropped(self):
        why = ("N0 10:35 — " + "x" * 200)
        text = eod.format_eod(day(rejected={"weak": {"N0": why}}))
        self.assertIn("N0 10:35 — xxx", text)
        self.assertIn("…", text)
        self.assertNotIn("x" * 100, text)

    def test_a_long_reject_list_is_truncated_but_counted(self):
        names = {f"N{i:02d}": f"N{i:02d} 10:35 — rank {i} is behind its sector's runners"
                 for i in range(40)}
        text = eod.format_eod(day(rejected={"weak": names}))
        self.assertIn("40 — ", text)
        self.assertIn("…", text)
        self.assertNotIn("N39", text.split("Momentum gate")[0].split("40 — ")[1][:200])

    def test_the_sector_table_draws_the_day_and_who_it_picked(self):
        st = day(sector_hist={
            "AUTO": {"first": "2026-11-20 09:20", "last": "2026-11-20 15:25",
                     "max": 98, "min": 40, "n": 75, "picks": 41, "persist": 91,
                     "day_ret": 1.84, "intra": 0.62, "side": 1,
                     "series": [["09:20", 40], ["10:20", 66], ["11:20", 76],
                                ["12:20", 80], ["13:20", 88], ["14:20", 95],
                                ["15:20", 91]]},
            "IT": {"first": "2026-11-20 09:20", "last": "2026-11-20 15:25",
                   "max": 71, "min": 20, "n": 75, "picks": 0, "persist": 44,
                   "day_ret": -0.9, "intra": -0.3, "side": -1,
                   "series": [["09:20", 71], ["15:20", 44]]},
        })
        text = eod.format_eod(st)
        self.assertIn("<b>Sector persistence (the day)</b>", text)
        self.assertIn("AUTO 98max/91now ↑", text)
        self.assertIn("95 91]", text)                   # the last samples
        self.assertIn("day +1.84%", text)
        self.assertIn("41 candles as pick", text)
        self.assertIn("IT 71max/44now ↓", text)

    def test_the_pick_history_says_how_long_each_sector_held(self):
        st = day(sector_hist={
            "AUTO": {"n": 75, "picks": 41, "persist": 91, "max": 98, "series": [], "side": 1},
            "BANK": {"n": 75, "picks": 20, "persist": 88, "max": 90, "series": [], "side": 1},
        }, extra={"IT": 30, "PHARMA": 12})
        text = eod.format_eod(st)
        self.assertIn("Pick (candles): AUTO 41 · BANK 20", text)
        self.assertIn("Longest run AUTO 41 candles (205 min)", text)
        self.assertIn("2 sector(s) picked in all", text)
        self.assertIn("first pick 09:20 · last 15:25 · 4 candles on record · "
                      "changed 1 time", text)
        self.assertIn("Scanned beside the pick: IT 30 · PHARMA 12", text)

    def test_a_trade_that_closed_is_scored_in_r(self):
        st = day(closed=[
            {"raw": "MARUTI", "side": "BUY", "time": "10:15", "entry": 12000.0,
             "exit": 12180.0, "r": 1.8, "why": "EMA9 EXIT", "t_hit": 2, "sector": "AUTO"},
            {"raw": "TATAMOTORS", "side": "BUY", "time": "11:05", "entry": 700.0,
             "exit": 693.0, "r": -1.0, "why": "SL HIT", "t_hit": 0, "sector": "AUTO"},
        ])
        text = eod.format_eod(st)
        self.assertIn("MARUTI BUY 10:15 12000.0 → 12180.0 <b>+1.8R</b> ", text)
        self.assertIn("EMA9 EXIT, T2 hit", text)
        self.assertIn("TATAMOTORS BUY 11:05 700.0 → 693.0 <b>-1.0R</b> (SL HIT)", text)
        self.assertIn("Closed 2: 1 up / 1 down · +0.8R in all · "
                      "best +1.8R · worst -1.0R", text)

    def test_a_trade_still_open_is_marked_at_the_close(self):
        st = day(positions={"MARUTI": {
            "side": "BUY", "entry": 12000.0, "risk": 100.0, "sl": 11900.0,
            "entry_bar": "2026-11-20T10:15:00+05:30", "last_close": 12150.0,
            "hit_targets": [1]}})
        text = eod.format_eod(st)
        self.assertIn("MARUTI BUY open from 10:15 @ 12000.0 · stop 11900.0", text)
        self.assertIn("<b>+1.5R</b> at the close", text)
        self.assertIn("T1 hit", text)

    def test_an_open_trade_with_no_bar_says_unmarked_instead_of_zero(self):
        st = day(positions={"MARUTI": {"side": "BUY", "entry": 12000.0, "risk": 100.0,
                                       "sl": 11900.0, "entry_bar": "2026-11-20T10:15:00",
                                       "last_close": None}})
        text = eod.format_eod(st)
        self.assertIn("unmarked", text)
        self.assertNotIn("+0.0R at the close", text)

    def test_the_health_section_carries_the_silent_failures(self):
        st = day(pending=["PNB"], checked=["PNB"], unjudged=["15:20", "15:25"],
                 feed_behind=3)
        text = eod.format_eod(st)
        self.assertIn("<b>Health</b>", text)
        self.assertIn("not checked", text)
        self.assertIn("candles never judged: 2 — 15:20, 15:25", text)
        self.assertIn("feed lagged past the close on 3 candle(s)", text)

    def test_a_clean_day_says_so_instead_of_printing_nothing(self):
        text = eod.format_eod(day())
        self.assertIn("No feed, scan or delivery problems recorded.", text)
        self.assertIn("None opened.", text)

    def test_the_rules_in_force_are_printed_with_the_day(self):
        with patch.dict(os.environ, {"ENTRY_RANK_BAND": "0.6", "MAX_ENTRIES_PER_PICK": "0"},
                        clear=False):
            text = eod.format_eod(day())
        self.assertIn("<b>Rules in force</b>", text)
        self.assertIn("cap none per pick", text)
        self.assertIn("runners within 60% of the sector's best", text)
        self.assertIn("ENTRY_RANK_BAND=0.6", text)

    def test_the_observations_are_computed_from_the_day(self):
        st = day(entries=1, weak={"behind its sector's runners": 40},
                 sector_hist={"AUTO": {"n": 75, "picks": 41, "persist": 91, "max": 98,
                                       "series": [], "side": 1}})
        text = eod.format_eod(st)
        self.assertIn("<b>What to look at tomorrow</b>", text)
        self.assertIn("40 names refused by the momentum gate against 1 entry", text)
        self.assertIn("ENTRY_RANK_BAND may be too tight", text)

    def test_an_early_alert_that_died_is_named_with_its_reason(self):
        st = day(early_sent=6, confirmed=1, cancel_why={
            "the close did not confirm the early signal": 5})
        text = eod.format_eod(st)
        self.assertIn("5 of 6 early alerts died before their close", text)
        self.assertIn("most often: the close did not confirm the early signal", text)

    def test_the_early_alerts_that_died_are_counted_with_their_reason(self):
        st = day(early_sent=6, confirmed=2, cancelled=4,
                 cancel_why={"the close did not confirm the early signal": 3,
                             "the sector changed": 1},
                 alert_capped={"10:00": 8})
        text = eod.format_eod(st)
        self.assertIn("Early alerts cancelled: the close did not confirm the early "
                      "signal 3 · the sector changed 1", text)
        self.assertIn("Held back by the per-candle alert cap: 10:00 8", text)

    def test_a_closed_trade_says_how_it_ended_by_kind(self):
        st = day(closed=[
            {"raw": "A", "side": "BUY", "time": "10:00", "entry": 100.0, "exit": 110.0,
             "r": 2.0, "why": "EMA9 EXIT", "t_hit": 2},
            {"raw": "B", "side": "BUY", "time": "10:05", "entry": 100.0, "exit": 96.0,
             "r": -1.0, "why": "SL HIT", "t_hit": 0},
            {"raw": "C", "side": "BUY", "time": "10:10", "entry": 100.0, "exit": 101.0,
             "r": 0.5, "why": "EMAIL9 EXIT".replace("EMAIL", "EMA"), "t_hit": 0},
        ])
        self.assertIn("Closed by: EMA9 EXIT 2 · SL HIT 1", eod.format_eod(st))

    def test_the_late_leaders_and_the_early_skips_are_in_health(self):
        st = day(catchup=3, catchup_skip={"old": 2}, early_skip={"no 1m bar": 5})
        text = eod.format_eod(st)
        self.assertIn("LATE LEADER catch-ups 3", text)
        self.assertIn("Early pass skipped: no 1m bar 5", text)

    def test_a_signal_that_fired_before_its_pick_is_not_hidden(self):
        st = day(rejected={"earlier": {"AAA": "AAA fired at 09:35, AUTO picked at 10:10"}},
                 catchup=0, late_entries=0)
        text = eod.format_eod(st)
        self.assertIn("1 signal fired before their sector was picked", text)
        self.assertIn("the pick is late, not the signal", text)


# ── the books the report reads, written as the scanners go ─────────────────
class BookTests(base.TestCase):
    """The report can only describe what the scanner kept: keep it correctly."""

    def md(self, **sectors):
        from types import SimpleNamespace
        snaps = {name: {"side": 1, "persist_bull": p, "persist_bear": 10.0,
                        "day_ret": d, "intra_ret": 0.4, "rs": 0.5, "pos": 70.0,
                        "qualified": True}
                 for name, (p, d) in sectors.items()}
        return SimpleNamespace(pick_sector="AUTO", pick_side=1, sectors=snaps)

    def test_the_sector_day_keeps_first_last_max_and_the_sampled_series(self):
        st = {"stats": {}}
        for i, (bar, p) in enumerate([("09:20", 40), ("09:25", 55), ("09:30", 66),
                                      ("09:35", 98), ("09:40", 91)]):
            pipeline._note_sector_day(st, self.md(AUTO=(p, 1.5)), f"2026-11-20 {bar}")
        h = st["stats"]["sector_hist"]["AUTO"]
        self.assertEqual((h["first"], h["last"]), ("2026-11-20 09:20", "2026-11-20 09:40"))
        self.assertEqual((h["min"], h["max"], h["persist"]), (40.0, 98.0, 91))
        self.assertEqual(h["day_ret"], 1.5)
        self.assertEqual(h["picks"], 5)                 # it was the pick on every candle
        # first candle, then every third: the table prints "now" from ``persist``
        self.assertEqual([v for _, v in h["series"]], [40, 66])

    def test_the_weak_book_says_which_test_refused_the_name(self):
        st = {"stats": {}}
        for why in ("reversing — under its EMA9 with the last candles against it",
                    "sideways — no net move over the last three candles",
                    "weak momentum rank 12",
                    "rank 12 is behind its sector's runners"):
            pipeline._note_weak(st, why)
        self.assertEqual(st["stats"]["weak"],
                         {"reversing": 1, "sideways": 1, "weak momentum": 1,
                          "behind its sector's runners": 1})

    def test_the_trim_book_keeps_what_the_ceiling_dropped(self):
        st = {"stats": {}}
        pipeline._note_trim(st, "AUTO", kept=5, total=13)
        pipeline._note_trim(st, "AUTO", kept=5, total=13)
        pipeline._note_trim(st, "IT", kept=5, total=5)      # nothing trimmed: no record
        self.assertEqual(st["stats"]["trim"]["AUTO"],
                         {"kept": 10, "dropped": 16, "candles": 2})
        self.assertNotIn("IT", st["stats"]["trim"])

    def test_a_closed_trade_is_filed_with_its_r_on_both_sides(self):
        st = {"stats": {}, "scan": {"sector": "AUTO"}}
        pipeline._close_trade(st, "LONG", {"side": "BUY", "entry": 100.0, "risk": 2.0,
                                           "hit_targets": [1]}, 105.0, "EMA9 EXIT", "11:00")
        pipeline._close_trade(st, "SHORT", {"side": "SELL", "entry": 100.0, "risk": 2.0},
                              96.0, "SL HIT", "11:30")
        long, short = st["stats"]["closed"]
        self.assertEqual((long["r"], long["t_hit"], long["why"]), (2.5, 1, "EMA9 EXIT"))
        self.assertEqual((short["r"], short["why"]), (2.0, "SL HIT"))
        self.assertEqual(long["sector"], "AUTO")

    def test_a_tracked_stop_lands_in_the_report_with_its_r(self):
        """The exit deletes the position, so the report reads the closed book."""
        st = {"stats": {}, "scan": {"sector": "AUTO"}, "positions": {}, "sent": {}}
        pos = {"side": "BUY", "entry": 100.0, "sl": 98.0, "risk": 2.0, "t_names": ["S1"],
               "entry_bar": base.BAR.isoformat(), "last_bar": base.BAR.isoformat(),
               "session": str(base.BAR.date()), "sector": "AUTO"}
        st["positions"]["ABC"] = pos
        row = {"Open": 100.0, "High": 100.5, "Low": 97.5, "Close": 97.5, "Volume": 10}
        later = base.BAR + timedelta(minutes=5)
        frame = pd.DataFrame([row], index=pd.DatetimeIndex([later]))
        sent: list[str] = []
        with patch.object(pipeline.data, "fetch_many",
                          return_value={pipeline.data.yahoo_stock("ABC"): frame}), \
                patch.object(pipeline, "_unsettled", return_value=(set(), False)), \
                patch.dict(os.environ, {"DRY_RUN": "0"}, clear=False):
            pipeline._track_positions(
                st, later, str(base.BAR.date()),
                send=lambda text, dry_run=False: sent.append(text) or True)
        self.assertTrue(sent, "no stop alert")
        closed = st["stats"]["closed"]
        self.assertEqual([(c["raw"], c["r"], c["why"]) for c in closed],
                         [("ABC", -1.0, "SL HIT")])
        self.assertNotIn("ABC", st["positions"])
        self.assertIn("(SL HIT)", eod.format_eod(st))

    def test_a_trade_with_no_risk_cannot_divide_by_zero(self):
        st = {"stats": {}}
        pipeline._close_trade(st, "X", {"side": "BUY", "entry": 100.0, "risk": 0.0},
                              101.0, "SL HIT", "11:00")
        self.assertEqual(st["stats"]["closed"][0]["r"], 0.0)


# ── delivery: a long report arrives in full, and only then counts as sent ───
class ChunkTests(base.TestCase):
    def test_a_short_report_is_one_message(self):
        parts = eod.chunks("a\n\nb", limit=100)
        self.assertEqual(parts, ["a\n\nb"])

    def test_a_long_report_is_split_on_block_boundaries_and_numbered(self):
        text = "\n\n".join(f"block {i} " + "x" * 80 for i in range(10))
        parts = eod.chunks(text, limit=300)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 320 for p in parts), [len(p) for p in parts])
        self.assertTrue(parts[0].startswith("(1/"), parts[0][:12])
        self.assertIn(f"({len(parts)}/{len(parts)})", parts[-1])
        self.assertEqual("".join(p.split("\n", 1)[1] for p in parts).replace("\n", ""),
                         text.replace("\n", ""))

    def test_one_block_longer_than_a_message_is_cut_on_a_line(self):
        text = "\n".join(f"line {i} " + "y" * 40 for i in range(20))
        parts = eod.chunks(text, limit=200)
        self.assertTrue(all(len(p) <= 220 for p in parts))
        for part in parts:
            for line in part.split("\n"):
                self.assertTrue(line.startswith("(") or line.startswith("line "), line)

    def test_the_limit_comes_from_the_environment_and_is_clamped(self):
        with patch.dict(os.environ, {"EOD_MAX_CHARS": "1500"}, clear=False):
            self.assertEqual(eod.max_chars(), 1500)
        for bad in ("0", "999999", "not a number"):
            with patch.dict(os.environ, {"EOD_MAX_CHARS": bad}, clear=False):
                self.assertIn(eod.max_chars(), (1000, 4000, eod.DEFAULT_LIMIT))
                self.assertGreaterEqual(eod.max_chars(), 1000)
                self.assertLessEqual(eod.max_chars(), 4000)

    def test_the_report_can_be_switched_off(self):
        self.assertTrue(eod.report_enabled())
        with patch.dict(os.environ, {"EOD_REPORT": "0"}, clear=False):
            self.assertFalse(eod.report_enabled())


class SendTests(base.TestCase):
    def big_day(self) -> dict:
        return day(rejected={"weak": {f"N{i:03d}": f"N{i:03d} 10:35 — rank {i} is behind "
                                               f"its sector's runners" for i in range(90)}},
                   sector_hist={f"S{i}": {"n": 75, "picks": i, "persist": 50 + i, "max": 90,
                                          "min": 20, "day_ret": 1.0, "series": [],
                                          "side": 1} for i in range(12)},
                   closed=[{"raw": f"X{i}", "side": "BUY", "time": "10:00", "entry": 100.0,
                            "exit": 101.0, "r": 1.0, "why": "EMA9 EXIT", "t_hit": 1}
                           for i in range(15)])

    def test_a_long_report_is_sent_as_several_messages_and_all_of_them_arrive(self):
        sent: list[str] = []
        with patch.dict(os.environ, {"EOD_MAX_CHARS": "1000"}, clear=False):
            ok = eod.send_eod(self.big_day(), send=lambda text, dry_run=False: sent.append(text) or True)
        self.assertTrue(ok)
        self.assertGreater(len(sent), 3)
        self.assertTrue(all(len(s) <= 1020 for s in sent), [len(s) for s in sent])
        self.assertTrue(sent[0].startswith("(1/"))
        self.assertIn("What to look at tomorrow", sent[-1])

    def test_a_refused_part_leaves_the_report_unsent(self):
        calls = []

        def send(text, dry_run=False):
            calls.append(text)
            return len(calls) != 2                  # the second part is refused

        with patch.dict(os.environ, {"EOD_MAX_CHARS": "1000"}, clear=False):
            self.assertFalse(eod.send_eod(self.big_day(), send=send))
        self.assertGreaterEqual(len(calls), 2)

    def test_the_bot_is_named_in_the_report(self):
        self.assertIn("bot 1m", eod.format_eod(day(), bot="1m"))


# ── the two scanners actually send it, by default ───────────────────────────
class PipelineWiringTests(base.TestCase):
    """15:33 is the only moment the day is described: it must be the report.

    The world is ``LiveAlertTests``'s (borrowed, not inherited: the report is
    what is under test here, not the alert rules).
    """

    market_at_bar = base.LiveAlertTests.market_at_bar

    def setUp(self):
        base.LiveAlertTests.setUp(self)
        # Walk the clock instead of waiting for it (see test_early_alerts).
        self.sleep = patch.object(pipeline.time, "sleep", side_effect=self._advance)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def _advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)

    def test_the_close_sends_the_report_not_the_short_summary(self):
        state.save({"session": str(base.BAR.date()), "sent": {}, "positions": {},
                    "stats": {"candles": 75, "picks": 40, "entries": 4,
                              "rejected": {"weak": {"ABC": "ABC 10:35 — rank 9 is behind "
                                                           "its sector's runners"}}}})
        self.now = base.BAR.replace(hour=15, minute=31)
        with patch.dict(os.environ, {"EOD_REPORT": "1"}, clear=False):
            self.assertEqual(pipeline.live_loop(exit_at_close=True), "closed")
        texts = [c.args[0] for c in self.send.call_args_list
                 if isinstance(c.args[0], str) and "END OF DAY" in c.args[0]]
        self.assertEqual(len(texts), 1, [c.args[0][:60] for c in self.send.call_args_list])
        self.assertIn("END OF DAY", texts[0])
        self.assertIn("Blocked signals — by rule", texts[0])
        self.assertIn("rank 9 is behind its sector's runners", texts[0])
        self.assertTrue(state.load()["stats"]["reported"])

    def test_a_report_telegram_refuses_leaves_the_session_unreported(self):
        state.save({"session": str(base.BAR.date()), "sent": {}, "positions": {},
                    "stats": {"candles": 3}})
        self.send.return_value = False
        self.now = base.BAR.replace(hour=15, minute=32)
        with patch.dict(os.environ, {"EOD_REPORT": "1"}, clear=False):
            with self.assertLogs("nse_scanner", level="ERROR") as logs:
                pipeline.live_loop(exit_at_close=True)
        self.assertTrue(any("NOT delivered" in line for line in logs.output), logs.output)
        self.assertFalse(state.load()["stats"].get("reported"))

    def test_the_report_is_sent_once(self):
        state.save({"session": str(base.BAR.date()), "sent": {}, "positions": {},
                    "stats": {"candles": 3}})
        with patch.dict(os.environ, {"EOD_REPORT": "1"}, clear=False):
            for _ in range(2):
                self.now = base.BAR.replace(hour=15, minute=31)
                pipeline.live_loop(exit_at_close=True)
        texts = [c.args[0] for c in self.send.call_args_list if "END OF DAY" in str(c.args[0])]
        self.assertEqual(len(texts), 1)


class BotThreeWiringTests(base.TestCase):
    """Bot 3 closes at the same time and must report the same way."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        sent: list[str] = []
        self.sent = sent
        self.patches = [
            patch.object(scan1m, "LEDGER", self.dir / "scan1m.json"),
            patch.object(scan1m, "now_ist", return_value=base.BAR),   # the ledger's own day
            patch.object(scan1m.telegram, "send_bot3",
                         side_effect=lambda text, dry_run=False: sent.append(text) or True),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def ledger_with(self, **stats):
        st = {"session": str(base.BAR.date()), "sent": {}, "positions": {},
              "market": {"label": "BULL", "conf": 70, "regime": "TREND"},
              "scan": {"sector": "IT", "side": 1},
              "stats": {"candles": 71, **stats}}
        state.save(st, self.dir / "scan1m.json")
        return st

    def test_the_1m_close_sends_the_report_and_remembers_it(self):
        self.ledger_with(rejected={"weak": {"AAA": "AAA rank 4 is behind its sector's runners"}})
        with patch.dict(os.environ, {"EOD_REPORT": "1", "SCAN1M_SUMMARY": "1"}, clear=False):
            with self.assertLogs("nse_scanner", level="INFO"):
                scan1m._end_of_session(base.BAR.replace(hour=15, minute=33))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("END OF DAY", self.sent[0])
        self.assertIn("bot 1m", self.sent[0])
        self.assertTrue(state.load(self.dir / "scan1m.json")["stats"]["reported"])

    def test_a_refused_1m_report_is_not_marked_as_sent(self):
        self.ledger_with()
        with patch.object(scan1m.telegram, "send_bot3", return_value=False), \
                patch.dict(os.environ, {"EOD_REPORT": "1", "SCAN1M_SUMMARY": "1"},
                           clear=False):
            with self.assertLogs("nse_scanner", level="ERROR"):
                scan1m._end_of_session(base.BAR.replace(hour=15, minute=33))
        self.assertFalse(state.load(self.dir / "scan1m.json")["stats"].get("reported"))


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
