"""The end-of-day report: the whole day in the alert, and nothing silently lost.

The report is the only place the day is described after the fact, so these tests
are about *content* (nothing the scanner knew is missing, and every number is
read from its own book) and about *delivery* (a report that does not fit in one
Telegram message goes out in numbered parts with both audit attachments; if any
part is refused the session is not marked as reported).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
import test_live_alerts as base
from nse_scanner import eod, pipeline, scan1m, state


def _cell(pick="AUTO", side=1, auto=(60, 10, 1.2, 0.4, 0), it=(40, 20, -0.3, -0.1, 0)):
    return {"b": pick, "tilt": 1, "conf": 74, "label": "BULL", "regime": "TREND",
            "pick": pick, "side": side,
            "sec": {"AUTO": list(auto), "IT": list(it)}}


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
            "timeline": [
                {"b": "09:20", "tilt": 1, "conf": 74, "label": "BULL", "regime": "TREND",
                 "pick": "WAIT", "side": 0,
                 "sec": {"AUTO": [60, 10, 1.2, 0.4, 0], "IT": [40, 20, -0.3, -0.1, 0]}},
                {"b": "09:25", "tilt": 1, "conf": 74, "label": "BULL", "regime": "TREND",
                 "pick": "AUTO", "side": 1,
                 "sec": {"AUTO": [78, 8, 1.4, 0.6, 1], "IT": [38, 22, -0.4, -0.2, 0]}},
                {"b": "10:00", "tilt": 1, "conf": 70, "label": "BULL", "regime": "TREND",
                 "pick": "AUTO", "side": 1,
                 "sec": {"AUTO": [91, 6, 2.0, 0.9, 1], "IT": [30, 30, -0.9, -0.4, 0]}},
                {"b": "15:25", "tilt": 0, "conf": 60, "label": "NEUTRAL", "regime": "CHOP",
                 "pick": "AUTO", "side": 1,
                 "sec": {"AUTO": [88, 9, 1.8, 0.2, 1], "IT": [44, 26, -1.1, -0.5, 0]}},
            ],
            "leadership": [
                {"b": "09:25", "sec": {"AUTO": [
                    {"name": "MARUTI", "rank": 78, "strength": 71, "volume": 92,
                     "structure": 78, "kept": True},
                    {"name": "M&M", "rank": 61, "strength": 55, "volume": 70,
                     "structure": 60, "kept": True}]}},
                {"b": "10:00", "sec": {"AUTO": [
                    {"name": "MARUTI", "rank": 80, "strength": 73, "volume": 90,
                     "structure": 80, "kept": True},
                    {"name": "M&M", "rank": 58, "strength": 52, "volume": 60,
                     "structure": 64, "kept": True}]}},
            ],
            **stats,
        },
    }


def trade(**kw):
    rec = {"raw": "TATAMOTORS", "side": "BUY", "time": "10:15", "entry": 700.0,
           "exit": 712.0, "r": 1.2, "qty": 142, "why": "EMA9 EXIT", "t_hit": 1,
           "sector": "AUTO", "entry_bar": "2026-11-20T09:20:00+05:30", "stop0": 694.0,
           "risk": 6.0, "t1": 712.0}
    rec.update(kw)
    return rec


# ── content: nothing the scanner knew is left out ───────────────────────────
class ContentTests(base.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"AUDIT_DIR": self.tmp.name}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_every_blocked_signal_is_listed_one_by_one_with_its_reason(self):
        names = {f"N{i:02d} {10 + i // 60:02d}:{(i % 60):02d}":
                 f"N{i:02d} {10 + i // 60:02d}:{(i % 60):02d} — rank {i} is behind "
                 f"its sector's runners" for i in range(12)}
        text = eod.format_eod(day(rejected={"weak": names,
                                            "follower": {"XYZ": "XYZ AUTO"}},
                                 weak={"behind its sector's runners": 12},
                                 trim={"AUTO": {"kept": 15, "dropped": 24}}))
        self.assertIn("<b>Every blocked signal</b>", text)
        self.assertIn("13 signals blocked", text)
        self.assertIn("<b>12 — not running — sideways, reversing or weak momentum</b>", text)
        for i in range(12):                     # not a sample of six: all of them
            self.assertIn(f"N{i:02d}", text)
        self.assertIn("N11", text)
        self.assertIn("• XYZ AUTO", text)
        self.assertIn("Sector trim (SECTOR_TOP_N): kept 15 name-checks, dropped 24", text)
        self.assertIn("Momentum gate: behind its sector's runners 12", text)

    def test_a_reason_that_runs_long_is_clipped_but_the_name_survives(self):
        text = eod.format_eod(day(rejected={"weak": {"N0": "N0 10:35 — " + "x" * 200}}))
        self.assertIn("N0 10:35 — xxx", text)
        self.assertIn("…", text)
        self.assertNotIn("x" * 120, text)

    def test_the_timeline_shows_the_tilt_the_pick_and_its_flow(self):
        text = eod.format_eod(day())
        self.assertIn("<b>Sector momentum timeline</b>", text)
        self.assertIn("09:20 BULL 74", text)
        self.assertIn("AUTO (leader) · flow 60▲/10▼ · day +1.20% · intra +0.40%", text)
        self.assertIn("15:25 NEUTRAL 60 · AUTO · flow 88▲/9▼", text)   # the last candle
        self.assertIn("Pick moved 1 time(s): 09:25 WAIT→AUTO", text)
        self.assertIn("the full matrix is in the audit", text)

    def test_with_no_pick_the_timeline_names_the_leading_sector(self):
        text = eod.format_eod(day(timeline=[{
            "b": "09:20", "tilt": 1, "conf": 70, "label": "BULL", "regime": "TREND",
            "pick": "WAIT", "side": 0,
            "sec": {"AUTO": [80, 10, 1.0, 0.3, 1], "IT": [40, 20, 0, 0, 0]}}]))
        self.assertIn("AUTO (leader)", text)

    def test_the_persistence_lines_carry_the_range_the_gate_and_the_crossings(self):
        text = eod.format_eod(day())
        self.assertIn("<b>Persistent-sector performance</b>", text)
        self.assertIn("AUTO P+ 60–91→88", text)
        self.assertIn("3/4 candles ≥70", text)
        self.assertIn("gate open 1 spell 09:25–15:25", text)
        self.assertIn("crossed 70↑@09:25", text)
        self.assertIn("IT P+ 30–44→44 · 0/4 candles ≥70", text)

    def test_the_leadership_line_carries_the_three_ranking_legs(self):
        text = eod.format_eod(day(closed=[trade(raw="MARUTI", entry=12000.0,
                                                exit=12150.0, qty=8, risk=50.0)]))
        self.assertIn("<b>Top-5 leadership activity</b>", text)
        self.assertIn("MARUTI 79 (s72/v91/t79) ×2", text)
        self.assertIn("M&M 60 (s54/v65/t62) ×2", text)
        self.assertIn("✚ entered", text)                 # it was taken
        self.assertIn("every candle's table is in the audit", text)

    def test_a_led_name_that_was_blocked_says_which_rule_blocked_it(self):
        text = eod.format_eod(day(rejected={"weak": {"M&M 10:00": "M&M — weak"}}))
        self.assertIn("⊘ weak", text)

    def test_the_ledger_row_has_size_prices_costs_and_both_r_multiples(self):
        text = eod.format_eod(day(closed=[trade()]))
        self.assertIn("<b>Trader ledger</b>", text)
        self.assertIn("1. TATAMOTORS BUY 142q @ 700.00 (09:20) → 712.00 (10:15)", text)
        self.assertIn("gross ₹1,704 · costs ₹100 · <b>net +1,604 ₹</b>", text)
        self.assertIn("+1.2R gross / +1.9R net", text)
        self.assertIn("55 min", text)

    def test_the_ledger_totals_name_the_money_the_hit_rate_and_the_drawdown(self):
        text = eod.format_eod(day(closed=[
            trade(), trade(raw="M&M", entry=3400.0, exit=3370.0, r=-1.0, qty=29,
                           why="SL HIT", t_hit=0)]))
        self.assertIn("2 closed · 1W/1L (50%)", text)
        self.assertIn("gross ₹", text)
        self.assertIn("net ₹", text)
        self.assertIn("worst drawdown ₹", text)
        self.assertIn("avg win ₹", text)
        self.assertIn("Cost model: 5 bps/side (min ₹20 per order) on ₹100,000 a trade", text)

    def test_an_open_trade_is_marked_at_the_close(self):
        text = eod.format_eod(day(positions={"MARUTI": {
            "side": "BUY", "entry": 12000.0, "risk": 100.0, "sl": 12000.0, "qty": 8,
            "entry_bar": "2026-11-20T12:40:00+05:30", "last_close": 12150.0,
            "hit_targets": [1], "sector": "AUTO"}}))
        self.assertIn("MARUTI BUY 8q @ 12000.00 → 12150.00 marked", text)
        self.assertIn("+1.5R gross / +1.4R net · still open", text)

    def test_an_open_trade_with_no_bar_says_unmarked_instead_of_zero(self):
        text = eod.format_eod(day(positions={"MARUTI": {
            "side": "BUY", "entry": 12000.0, "risk": 100.0, "qty": 8,
            "entry_bar": "2026-11-20T12:40:00", "last_close": None}}))
        self.assertIn("still open, unmarked", text)
        self.assertNotIn("+0.0R at the close", text)

    def test_the_diagnostics_carry_the_feed_and_the_latency(self):
        text = eod.format_eod(day(pending=["PNB"], checked=["PNB"],
                                  unjudged=["15:20", "15:25"], feed_behind=3,
                                  lag_sum=90.0, lag_n=6, lag_max=47.0))
        self.assertIn("<b>Data diagnostics</b>", text)
        self.assertIn("not checked", text)
        self.assertIn("candles never judged: 2 — 15:20, 15:25", text)
        self.assertIn("feed lagged past the close on 3 candle(s)", text)
        self.assertIn("alerts went out 15s after the close on average", text)
        self.assertIn("slowest 47s", text)

    def test_a_clean_day_says_so_instead_of_printing_nothing(self):
        text = eod.format_eod(day())
        self.assertIn("No feed, scan or delivery problems recorded.", text)
        self.assertIn("No position was opened.", text)

    def test_the_rules_in_force_are_printed_with_the_day(self):
        with patch.dict(os.environ, {"ENTRY_RANK_BAND": "0.6", "MAX_ENTRIES_PER_PICK": "0"},
                        clear=False):
            text = eod.format_eod(day())
        self.assertIn("<b>Rules in force</b>", text)
        self.assertIn("cap none per pick", text)
        self.assertIn("runners within 60% of the sector's best", text)
        self.assertIn("ENTRY_RANK_BAND=0.6", text)

    def test_the_observations_are_computed_from_the_day(self):
        text = eod.format_eod(day(entries=1, weak={"behind its sector's runners": 40}))
        self.assertIn("<b>What to look at tomorrow</b>", text)
        self.assertIn("40 names refused by the momentum gate against 1 entry", text)
        self.assertIn("ENTRY_RANK_BAND may be too tight", text)

    def test_costs_that_ate_the_day_are_called_out(self):
        text = eod.format_eod(day(closed=[
            trade(entry=100.0, exit=100.05, r=0.02, qty=1000),
            trade(raw="B", entry=100.0, exit=100.04, r=0.02, qty=1000)]))
        self.assertIn("costs ate ₹", text)

    def test_a_ledger_with_no_sector_histogram_still_names_the_picks(self):
        st = day()
        st["stats"].pop("sector_hist", None)
        text = eod.format_eod(st, bot="1m")
        self.assertIn("<b>How the day was picked</b>", text)
        self.assertIn("Pick (candles): AUTO", text)

    def test_the_day_shape_names_the_pick_history(self):
        text = eod.format_eod(day())
        self.assertIn("first pick 09:20 · last 15:25", text)
        self.assertIn("changed 1 time", text)


# ── delivery: a long report arrives in full, and only then counts as sent ───
class ChunkTests(base.TestCase):
    def test_a_short_report_is_one_message(self):
        self.assertEqual(eod.chunks("a\n\nb", limit=100), ["a\n\nb"])

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
                self.assertGreaterEqual(eod.max_chars(), 1000)
                self.assertLessEqual(eod.max_chars(), 4000)

    def test_the_report_and_its_attachments_can_be_switched_off(self):
        self.assertTrue(eod.report_enabled())
        self.assertTrue(eod.audit_enabled())
        with patch.dict(os.environ, {"EOD_REPORT": "0", "EOD_AUDIT": "0"}, clear=False):
            self.assertFalse(eod.report_enabled())
            self.assertFalse(eod.audit_enabled())


class SendTests(base.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"AUDIT_DIR": self.tmp.name}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def big_day(self) -> dict:
        return day(rejected={"weak": {f"N{i:03d} {9 + i // 60:02d}:{i % 60:02d}":
                                      f"N{i:03d} {9 + i // 60:02d}:{i % 60:02d} — rank {i} "
                                      f"is behind its sector's runners"
                                      for i in range(90)}},
                   closed=[trade(raw=f"X{i}", entry=100.0, exit=101.0, r=1.0, qty=1000)
                           for i in range(15)])

    def test_a_long_report_is_sent_as_several_messages_and_all_of_them_arrive(self):
        sent: list[str] = []
        with patch.dict(os.environ, {"EOD_MAX_CHARS": "1000"}, clear=False):
            ok = eod.send_eod(self.big_day(),
                              send=lambda text, dry_run=False: sent.append(text) or True,
                              send_doc=lambda *a, **k: True)
        self.assertTrue(ok)
        self.assertGreater(len(sent), 3)
        self.assertTrue(all(len(s) <= 1020 for s in sent), [len(s) for s in sent])
        self.assertTrue(sent[0].startswith("(1/"))
        self.assertIn("Every blocked signal", "".join(sent))

    def test_both_attachments_are_uploaded_with_the_messages(self):
        docs: list[tuple[str, str]] = []
        sent: list[str] = []
        eod.send_eod(self.big_day(),
                     send=lambda text, dry_run=False: sent.append(text) or True,
                     send_doc=lambda path, caption="": docs.append((path, caption)) or True)
        self.assertEqual(len(docs), 2)
        self.assertTrue(docs[0][0].endswith(".json"))
        self.assertTrue(docs[1][0].endswith(".xlsx"))
        for path, caption in docs:
            self.assertTrue(Path(path).exists(), path)
            self.assertIn("Audit 2026-11-20", caption)

    def test_a_refused_part_leaves_the_report_unsent(self):
        calls = []

        def send(text, dry_run=False):
            calls.append(text)
            return len(calls) != 2                  # the second part is refused

        with patch.dict(os.environ, {"EOD_MAX_CHARS": "1000"}, clear=False):
            self.assertFalse(eod.send_eod(self.big_day(), send=send,
                                          send_doc=lambda *a, **k: True))
        self.assertGreaterEqual(len(calls), 2)

    def test_a_refused_attachment_leaves_the_report_unsent(self):
        self.assertFalse(eod.send_eod(day(), send=lambda *a, **k: True,
                                      send_doc=lambda *a, **k: False))

    def test_the_audit_can_be_switched_off_without_losing_the_messages(self):
        docs = []
        with patch.dict(os.environ, {"EOD_AUDIT": "0"}, clear=False):
            self.assertTrue(eod.send_eod(day(), send=lambda *a, **k: True,
                                         send_doc=lambda path, caption="": docs.append(path)
                                         or True))
        self.assertEqual(docs, [])

    def test_the_bot_is_named_in_the_report(self):
        self.assertIn("bot 1m", eod.format_eod(day(), bot="1m"))


# ── the books the report reads, written as the scanners go ─────────────────
class BookTests(base.TestCase):
    """The report can only describe what the scanner kept: keep it correctly."""

    def md(self, **sectors):
        from types import SimpleNamespace
        snaps = {name: {"side": 1, "persist_bull": p, "persist_bear": 10.0,
                        "day_ret": d, "intra_ret": 0.4, "rs": 0.5, "pos": 70.0,
                        "qualified": True}
                 for name, (p, d) in sectors.items()}
        return SimpleNamespace(pick_sector="AUTO", pick_side=1, dir_state=1,
                               dir_conf=74, dir_label="BULL", regime="TREND",
                               sectors=snaps)

    def test_the_sector_day_keeps_first_last_max_and_the_sampled_series(self):
        st = {"stats": {}}
        for bar, p in [("09:20", 40), ("09:25", 55), ("09:30", 66),
                       ("09:35", 98), ("09:40", 91)]:
            pipeline._note_sector_day(st, self.md(AUTO=(p, 1.5)), f"2026-11-20 {bar}")
        h = st["stats"]["sector_hist"]["AUTO"]
        self.assertEqual((h["first"], h["last"]), ("2026-11-20 09:20", "2026-11-20 09:40"))
        self.assertEqual((h["min"], h["max"], h["persist"]), (40.0, 98.0, 91))
        self.assertEqual(h["day_ret"], 1.5)
        self.assertEqual(h["picks"], 5)
        self.assertEqual([v for _, v in h["series"]], [40, 66])

    def test_the_timeline_keeps_one_row_per_candle_with_the_whole_board(self):
        st = {"stats": {}}
        pipeline._note_timeline(st, self.md(AUTO=(80, 1.2), IT=(40, -0.3)),
                                "2026-11-20 09:25", "AUTO", 1)
        pipeline._note_timeline(st, self.md(AUTO=(84, 1.4), IT=(38, -0.4)),
                                "2026-11-20 09:25", "AUTO", 1)   # the same candle again
        pipeline._note_timeline(st, self.md(AUTO=(90, 1.6), IT=(36, -0.5)),
                                "2026-11-20 09:30", "AUTO", 1)
        rows = st["stats"]["timeline"]
        self.assertEqual([r["b"] for r in rows], ["09:25", "09:30"])   # not duplicated
        self.assertEqual(rows[0]["label"], "BULL")
        self.assertEqual(rows[0]["sec"]["AUTO"], [84, 10, 1.4, 0.4, 1])
        self.assertEqual(rows[1]["pick"], "AUTO")

    def test_the_leadership_book_keeps_the_top_names_with_their_legs(self):
        st = {"stats": {}}
        record = {"AUTO": [{"name": "MARUTI", "rank": 78.0, "strength": 71,
                            "volume": 92, "structure": 78, "lead": 0.9, "kept": True}]}
        pipeline._note_leadership(st, "2026-11-20 09:25", record)
        pipeline._note_leadership(st, "2026-11-20 09:25", record)      # same candle
        pipeline._note_leadership(st, "2026-11-20 09:30", record)
        rows = st["stats"]["leadership"]
        self.assertEqual([r["b"] for r in rows], ["09:25", "09:30"])
        self.assertEqual(rows[0]["sec"]["AUTO"][0]["volume"], 92)

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
        pipeline._note_trim(st, "AUTO", 5, 13)
        pipeline._note_trim(st, "AUTO", 5, 13)
        pipeline._note_trim(st, "IT", 5, 5)      # nothing trimmed: no record
        self.assertEqual(st["stats"]["trim"]["AUTO"],
                         {"kept": 10, "dropped": 16, "candles": 2})
        self.assertNotIn("IT", st["stats"]["trim"])

    def test_a_closed_trade_is_filed_with_its_r_money_and_size(self):
        st = {"stats": {}, "scan": {"sector": "AUTO"}}
        pipeline._close_trade(st, "LONG", {"side": "BUY", "entry": 100.0, "risk": 2.0,
                                           "qty": 1000, "hit_targets": [1]}, 105.0,
                              "EMA9 EXIT", "11:00")
        pipeline._close_trade(st, "SHORT", {"side": "SELL", "entry": 100.0, "risk": 2.0,
                                            "qty": 1000}, 96.0, "SL HIT", "11:30")
        long, short = st["stats"]["closed"]
        self.assertEqual((long["r"], long["t_hit"], long["why"]), (2.5, 1, "EMA9 EXIT"))
        self.assertEqual((short["r"], short["why"]), (2.0, "SL HIT"))
        self.assertEqual(long["qty"], 1000)
        self.assertEqual(long["gross"], 5000.0)
        self.assertGreater(long["costs"], 0)
        self.assertEqual(long["net"], long["gross"] - long["costs"])
        self.assertEqual(long["sector"], "AUTO")

    def test_a_trade_with_no_risk_cannot_divide_by_zero(self):
        st = {"stats": {}}
        pipeline._close_trade(st, "X", {"side": "BUY", "entry": 100.0, "risk": 0.0,
                                        "qty": 1}, 101.0, "SL HIT", "11:00")
        self.assertEqual(st["stats"]["closed"][0]["r"], 0.0)

    def test_a_tracked_stop_lands_in_the_report_with_its_r_and_money(self):
        """The exit deletes the position, so the report reads the closed book."""
        st = {"stats": {}, "scan": {"sector": "AUTO"}, "positions": {}, "sent": {}}
        st["positions"]["ABC"] = {
            "side": "BUY", "entry": 100.0, "sl": 98.0, "risk": 2.0, "qty": 900,
            "t_names": ["S1"], "entry_bar": base.BAR.isoformat(),
            "last_bar": base.BAR.isoformat(), "session": str(base.BAR.date()),
            "sector": "AUTO"}
        later = base.BAR + timedelta(minutes=5)
        frame = pd.DataFrame([{"Open": 100.0, "High": 100.5, "Low": 97.5, "Close": 97.5,
                               "Volume": 10}], index=pd.DatetimeIndex([later]))
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
        self.assertEqual(closed[0]["qty"], 900)
        self.assertLess(closed[0]["net"], closed[0]["gross"])       # costs were paid
        self.assertNotIn("ABC", st["positions"])
        text = eod.format_eod(st)
        self.assertIn("ABC BUY 900q @ 100.00", text)
        self.assertIn("SL HIT", text)


class FiveMinuteDayBookTests(base.TestCase):
    """One real 5m pass must leave every book the report reads behind it."""

    def setUp(self):
        base.LiveAlertTests.setUp(self)
        self.env = patch.dict(os.environ, {"AUDIT_DIR": self.tmp.name}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def market_at_bar(self, df, *args, **kwargs):
        md = base.LiveAlertTests.market_at_bar(self, df, *args, **kwargs)
        md.sectors = {"IT": {"side": 1, "persist_bull": 88, "persist_bear": 9,
                             "day_ret": 1.2, "intra_ret": 0.4, "rs": 0.6, "pos": 72.0,
                             "qualified": True}}
        return md

    def test_a_real_pass_writes_the_timeline_the_board_and_the_ranking(self):
        pipeline.run_once()
        st = state.load()
        self.assertTrue(st["stats"]["timeline"], "no timeline written")
        row = st["stats"]["timeline"][-1]
        self.assertEqual(row["pick"], "IT")
        self.assertEqual(row["sec"]["IT"], [88, 9, 1.2, 0.4, 1])
        self.assertIn("IT", st["stats"]["sector_hist"])
        self.assertTrue(st["stats"]["leadership"], "no leadership rows")
        self.assertEqual(st["stats"]["leadership"][-1]["b"], row["b"])
        self.assertEqual(st["scan"]["sector"], "IT")

    def test_the_report_renders_from_what_the_pass_wrote(self):
        pipeline.run_once()
        text = eod.format_eod(state.load())
        self.assertIn("<b>Sector momentum timeline</b>", text)
        self.assertIn("<b>Persistent-sector performance</b>", text)
        self.assertIn("IT P+ 88–88→88", text)
        self.assertIn("<b>Top-5 leadership activity</b>", text)

    def test_the_audit_is_written_from_a_real_pass(self):
        from nse_scanner import audit

        pipeline.run_once()
        json_path, xlsx_path, payload = audit.write(state.load(), bot="5m")
        data = json.loads(json_path.read_text())
        self.assertTrue(data["timeline"])
        self.assertTrue(data["sectors"])
        self.assertEqual(data["timeline"][-1]["sectors"]["IT"]["persist_bull"], 88)
        self.assertTrue(data["leadership"])
        self.assertTrue(xlsx_path.exists())


# ── the two scanners actually send it, by default ───────────────────────────
class PipelineWiringTests(base.TestCase):
    """15:33 is the only moment the day is described: it must be the report."""

    market_at_bar = base.LiveAlertTests.market_at_bar

    def setUp(self):
        base.LiveAlertTests.setUp(self)
        self.sleep = patch.object(pipeline.time, "sleep", side_effect=self._advance)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.env = patch.dict(os.environ, {"AUDIT_DIR": self.tmp.name}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

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
        self.assertIn("Every blocked signal", texts[0])
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
        docs: list[str] = []
        self.sent, self.docs = sent, docs
        self.patches = [
            patch.object(scan1m, "LEDGER", self.dir / "scan1m.json"),
            patch.object(scan1m, "now_ist", return_value=base.BAR),   # the ledger's own day
            patch.object(scan1m.telegram, "send_bot3",
                         side_effect=lambda text, dry_run=False: sent.append(text) or True),
            patch.object(scan1m.telegram, "send_document",
                         side_effect=lambda path, caption="", **kw: docs.append(str(path))
                         or True),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.env = patch.dict(os.environ, {"AUDIT_DIR": str(self.dir)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

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
        self.assertEqual(len(self.docs), 2)             # the audit and the workbook
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
