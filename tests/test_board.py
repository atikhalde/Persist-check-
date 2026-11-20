"""The full board: every name in every sector, scored once per candle.

The report could only ever answer "who else was moving" for the sectors a scan
happened to look at, which is the wrong way round — the misses *are* the sectors
nobody scanned. These are the rules the new record has to keep:

* a sweep covers **every** sector in ``config/universe.yaml``, not just the pick,
  and copies the sector's own numbers from the timeline rather than re-deriving
  them (one engine, one set of numbers);
* one sweep per candle, on the candle grid, inside the window — a second call in
  the same five minutes writes nothing;
* a name whose frame has not published the candle is *skipped and counted*, never
  scored on an older close;
* a sweep that fails is recorded, and never takes the day's scan down with it;
* the day's summary says what each name did (first, peak, trough, last, best
  score, candles in the top five) and what we did with it (entered, net R, or the
  rule that refused it).
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from nse_scanner import audit, board, data, eod, scan1m

IST = ZoneInfo("Asia/Kolkata")
START = datetime(2026, 11, 20, 9, 15, tzinfo=IST)


def candle(n: int) -> datetime:
    """The open time of the ``n``-th five-minute candle of the session."""
    return START + timedelta(minutes=5 * n)


def frame(closes, volumes=None, end: int | None = None) -> pd.DataFrame:
    """A 5m frame of ``closes`` starting at 09:15, ending on candle ``end``."""
    idx = [START + timedelta(minutes=5 * i) for i in range(len(closes))]
    vols = volumes or [900] * len(closes)
    out = pd.DataFrame({"Open": closes,
                        "High": [c * 1.006 for c in closes],
                        "Low": [c * 0.994 for c in closes],
                        "Close": closes,
                        "Volume": vols}, index=idx)
    return out if end is None else out.iloc[:end + 1]


def timeline(hhmm: str, *, pick: str = "BANK", cells: dict | None = None) -> dict:
    """One timeline row — the sector numbers the engine wrote on that candle."""
    return {"b": hhmm, "tilt": 1, "conf": 70, "label": "BULL", "pick": pick,
            "side": 1, "sec": cells if cells is not None else
            {"AUTO": [92, 20, 4.29, 0.37, 1], "BANK": [97, 3, 1.48, 2.15, 1],
             "IT": [39, 61, 1.08, 2.35, 0]}}


def frames_closing_at(n: int) -> dict:
    """Four names whose frames end on candle ``n``."""
    return {
        data.yahoo_stock("MARUTI"): frame([100, 101, 102, 103, 104, 105, 106], end=n),
        data.yahoo_stock("TMPV"): frame([100, 100.2, 100.1, 100.3, 100.2, 100.1, 100.1],
                                        end=n),
        data.yahoo_stock("HDFCBANK"): frame([500, 505, 510, 515, 520, 525, 530], end=n),
        data.yahoo_stock("AXISBANK"): frame([1100, 1105, 1110, 1115, 1120, 1126, 1132],
                                            end=n),
    }


class Offline(TestCase):
    """No test may reach Yahoo: a sweep that fetches is a sweep that needs a feed.

    The tests hand the sweep the frames they have; the rest of the universe is
    what a live sweep would download, and here it is simply absent — which is
    also the case the sweep has to survive (a name with no frame is skipped and
    counted, not guessed).
    """

    def setUp(self):
        self._fetch = patch.object(board.data, "fetch_many", return_value={})
        self._fetch.start()
        self.addCleanup(self._fetch.stop)


def state(*rows) -> dict:
    return {"session": "2026-11-20", "stats": {"timeline": list(rows) or [
        timeline("09:30")]}}


class SweepTests(Offline):
    """One candle, every sector, no invention."""

    def test_every_sector_on_the_board_is_recorded_not_only_the_pick(self):
        st = state(timeline("09:30", pick="BANK"))
        row = board.sweep(st, now=datetime(2026, 11, 20, 9, 35, tzinfo=IST),
                          frames=frames_closing_at(3))
        self.assertIsNotNone(row)
        # IT was scored by the engine and appears with its sector numbers even
        # though no IT name published a bar — the gap is visible, not implied.
        self.assertEqual(sorted(row["sectors"]), ["AUTO", "BANK", "IT"])
        self.assertEqual(row["sectors"]["IT"]["n"], [])
        self.assertEqual(row["b"], "09:30")          # the candle, not the clock
        self.assertEqual(row["scan"], "09:35")       # ... and when it was taken
        self.assertTrue(row["sectors"]["BANK"]["pick"])
        self.assertFalse(row["sectors"]["AUTO"]["pick"])

    def test_the_sector_numbers_are_the_timeline_s(self):
        st = state(timeline("09:30"))
        row = board.sweep(st, now=datetime(2026, 11, 20, 9, 35, tzinfo=IST),
                          frames=frames_closing_at(3))
        bank = row["sectors"]["BANK"]
        self.assertEqual((bank["bull"], bank["bear"], bank["persist"]), (97, 3, 97))
        self.assertEqual((bank["day"], bank["intra"]), (1.48, 2.15))

    def test_a_short_sector_is_ranked_on_its_own_side(self):
        st = state(timeline("09:30", cells={"MEDIA": [4, 96, -1.32, -0.95, 1]}))
        frames = frames_closing_at(3)
        frames[data.yahoo_stock("PVRINOX")] = frame([100, 99, 98, 97, 96, 95], end=3)
        row = board.sweep(st, now=datetime(2026, 11, 20, 9, 35, tzinfo=IST),
                          frames=frames)
        media = row["sectors"]["MEDIA"]
        self.assertEqual(media["side"], -1)
        self.assertEqual(media["persist"], 96)       # the bear score, not the bull

    def test_every_name_is_scored_with_its_three_legs_and_its_move(self):
        st = state(timeline("09:30"))
        row = board.sweep(st, now=datetime(2026, 11, 20, 9, 35, tzinfo=IST),
                          frames=frames_closing_at(3))
        auto = row["sectors"]["AUTO"]
        self.assertEqual(auto["n"], ["MARUTI", "TMPV"])      # ranked, best first
        self.assertEqual(auto["rk"], [1, 2])
        self.assertEqual(len(auto["s"]), len(auto["v"]))
        self.assertEqual(len(auto["t"]), len(auto["lead"]))
        self.assertEqual([round(m, 2) for m in auto["mv"]], [3.0, 0.3])
        self.assertGreater(auto["rank"][0], auto["rank"][1])
        self.assertEqual(auto["rank"], sorted(auto["rank"], reverse=True))

    def test_a_name_whose_frame_has_not_published_this_candle_is_skipped(self):
        st = state(timeline("09:30"))
        stale = frames_closing_at(2)                  # the 09:25 frame, not 09:30
        row = board.sweep(st, now=datetime(2026, 11, 20, 9, 35, tzinfo=IST),
                          frames=stale)
        self.assertIsNone(row)                        # nothing current: no board
        self.assertEqual(st["stats"]["board"]["meta"]["stale"], 4)
        self.assertEqual(st["stats"]["board"]["bars"], [])

    def test_a_stale_name_never_reaches_the_board(self):
        st = state(timeline("09:30"))
        frames = frames_closing_at(3)
        frames.pop(data.yahoo_stock("TMPV"))
        frames[data.yahoo_stock("TMPV")] = frame([100, 100.1, 100.2], end=2)
        row = board.sweep(st, now=datetime(2026, 11, 20, 9, 35, tzinfo=IST),
                          frames=frames)
        self.assertEqual(row["sectors"]["AUTO"]["n"], ["MARUTI"])
        self.assertEqual(st["stats"]["board"]["meta"]["stale"], 1)

    def test_one_sweep_per_candle(self):
        st = state(timeline("09:30"))
        frames = frames_closing_at(3)
        first = board.sweep(st, now=datetime(2026, 11, 20, 9, 35, tzinfo=IST),
                            frames=frames)
        again = board.sweep(st, now=datetime(2026, 11, 20, 9, 36, tzinfo=IST),
                            frames=frames)
        self.assertIsNotNone(first)
        self.assertIsNone(again)
        self.assertEqual(len(st["stats"]["board"]["bars"]), 1)
        self.assertEqual(st["stats"]["board"]["meta"]["sweeps"], 1)

    def test_the_window_keeps_the_sweep_off_the_open_and_the_auction(self):
        st = state(timeline("09:15"))
        self.assertIsNone(board.sweep(st, now=datetime(2026, 11, 20, 9, 16, tzinfo=IST),
                                      frames=frames_closing_at(0)))
        st = state(timeline("15:20"))
        self.assertIsNone(board.sweep(st, now=datetime(2026, 11, 20, 15, 25, tzinfo=IST),
                                      frames=frames_closing_at(1)))
        self.assertEqual(st["stats"].get("board", {}).get("bars", []), [])

    def test_the_cadence_knob_is_a_divisor_of_the_candle_grid(self):
        # The grid is fixed to the candles: 09:15 is index 0, so 15 minutes keeps
        # 09:15 · 09:30 · 09:45 and 30 keeps 09:15 · 09:45 whatever the run time.
        for every, now, expected in ((5, (9, 35), True), (15, (9, 35), True),
                                     (15, (9, 40), False), (30, (9, 35), False),
                                     (30, (9, 50), True)):
            with self.subTest(every=every, now=now):
                candle_n = (now[0] * 60 + now[1] - (9 * 60 + 15)) // 5 - 1
                st = state(timeline("%02d:%02d" % (9, now[1] - 5)))
                with patch.dict(os.environ, {"AUDIT_BOARD_EVERY_MIN": str(every)}):
                    row = board.sweep(st, now=datetime(2026, 11, 20, *now, tzinfo=IST),
                                      frames=frames_closing_at(candle_n))
                self.assertEqual(row is not None, expected)

    def test_frames_the_caller_already_holds_are_not_fetched_again(self):
        st = state(timeline("09:30"))
        with patch.object(board.data, "fetch_many", return_value={}) as fetch:
            row = board.sweep(st, now=datetime(2026, 11, 20, 9, 35, tzinfo=IST),
                              frames=frames_closing_at(3))
        self.assertIsNotNone(row)
        asked = list(fetch.call_args[0][0])
        self.assertNotIn(data.yahoo_stock("MARUTI"), asked)     # handed over
        self.assertIn(data.yahoo_stock("TCS"), asked)           # not handed over

    def test_a_candle_the_scan_never_judged_is_reported_not_invented(self):
        st = state(timeline("09:25"))                 # no row for 09:30
        row = board.sweep(st, now=datetime(2026, 11, 20, 9, 35, tzinfo=IST),
                          frames=frames_closing_at(3))
        self.assertIsNone(row)
        self.assertEqual(st["stats"]["board"]["meta"]["skipped"], ["09:30"])
        self.assertEqual(st["stats"]["board"]["bars"], [])

    def test_the_sweep_can_be_switched_off(self):
        st = state(timeline("09:30"))
        with patch.dict(os.environ, {"AUDIT_BOARD": "0"}):
            self.assertIsNone(board.sweep(st, now=datetime(2026, 11, 20, 9, 35,
                                                           tzinfo=IST),
                                          frames=frames_closing_at(3)))
        self.assertEqual(st["stats"].get("board", {}).get("bars", []), [])

    def test_a_failure_is_recorded_and_never_raised(self):
        st = state(timeline("09:30"))
        with patch.object(board.data, "fetch_many", side_effect=RuntimeError("yahoo")):
            self.assertIsNone(board.sweep(st, now=datetime(2026, 11, 20, 9, 35,
                                                           tzinfo=IST)))
        errors = st["stats"]["board"]["meta"]["errors"]
        self.assertTrue(errors)
        self.assertIn("RuntimeError", errors[-1])

    def test_a_slow_sweep_stops_the_next_one_rather_than_the_day(self):
        st = state(timeline("09:30"))
        st["stats"] = {"timeline": [timeline("09:30"), timeline("09:35")],
                       "board": {"meta": {"slow": "09:30 took 61s"}}}
        self.assertIsNone(board.sweep(st, now=datetime(2026, 11, 20, 9, 40,
                                                      tzinfo=IST),
                                      frames=frames_closing_at(4)))


class SummaryTests(Offline):
    """What the board says a name did, and what we did with it."""

    def day(self) -> dict:
        st = state(*[timeline("09:%02d" % (20 + 5 * i)) for i in range(4)])
        for i in range(4):
            board.sweep(st, now=datetime(2026, 11, 20, 9, 25 + 5 * i, tzinfo=IST),
                        frames=frames_closing_at(i + 1))
        return st

    def test_the_summary_follows_each_name_from_first_candle_to_last(self):
        rows = {r["name"]: r for r in board.summary(self.day())}
        maruti = rows["MARUTI"]
        self.assertEqual(maruti["sector"], "AUTO")
        self.assertEqual(maruti["candles"], 4)
        self.assertEqual(maruti["first_bar"], "09:20")
        self.assertEqual(maruti["last_bar"], "09:35")
        self.assertAlmostEqual(maruti["first_mv"], 1.0, places=2)   # 09:20 close
        self.assertAlmostEqual(maruti["last_mv"], 4.0, places=2)    # 09:35 close
        self.assertAlmostEqual(maruti["peak_mv"], 4.0, places=2)
        self.assertAlmostEqual(maruti["trough_mv"], 1.0, places=2)

    def test_the_summary_counts_the_candles_a_name_spent_in_the_top_five(self):
        rows = {r["name"]: r for r in board.summary(self.day())}
        self.assertEqual(rows["MARUTI"]["top5"], 4)
        self.assertEqual(rows["MARUTI"]["best_pos"], 1)
        self.assertEqual(rows["TMPV"]["best_pos"], 2)

    def test_the_summary_joins_the_trade_and_the_refusal(self):
        st = self.day()
        rows = {r["name"]: r for r in board.summary(
            st,
            closed=[{"name": "MARUTI", "net_r": -0.8}],
            blocked=[{"rule": "follower", "key": "TMPV@09:30"},
                     {"rule": "weak", "key": "TMPV@09:35"}])}
        self.assertTrue(rows["MARUTI"]["entered"])
        self.assertEqual(rows["MARUTI"]["net_r"], -0.8)
        self.assertFalse(rows["TMPV"]["entered"])
        self.assertEqual(rows["TMPV"]["blocked"], "follower")   # the first rule wins

    def test_the_summary_names_the_best_score_not_the_best_position(self):
        rows = {r["name"]: r for r in board.summary(self.day())}
        self.assertIsNotNone(rows["MARUTI"]["best_rank"])
        self.assertGreater(rows["MARUTI"]["best_rank"], rows["TMPV"]["best_rank"])
        self.assertLessEqual(rows["MARUTI"]["best_pos"], rows["TMPV"]["best_pos"])


class OutputTests(Offline):
    """The JSONL companion, the printable page and the audit's own copy."""

    def day(self) -> dict:
        st = state(timeline("09:15"), timeline("09:30", pick="BANK"))
        board.sweep(st, now=datetime(2026, 11, 20, 9, 20, tzinfo=IST),
                    frames=frames_closing_at(0))
        board.sweep(st, now=datetime(2026, 11, 20, 9, 35, tzinfo=IST),
                    frames=frames_closing_at(3))
        return st

    def test_the_audit_carries_the_board_and_writes_both_files(self):
        st = self.day()
        with TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AUDIT_DIR": tmp}):
                json_path, xlsx_path, payload = audit.write(st, bot="1m")
            self.assertEqual(payload["board"]["meta"]["sweeps"], 2)
            self.assertEqual(len(payload["board"]["summary"]), 4)
            files = [Path(p) for p in payload["board"]["files"]]
            self.assertEqual([f.suffix for f in files], [".jsonl", ".html"])
            for path in files:
                self.assertTrue(path.exists(), path)
                self.assertEqual(path.parent, json_path.parent)
            rows = [json.loads(line) for line in files[0].read_text().splitlines()]
            self.assertEqual(rows[0]["meta"]["fields"], list(board.FIELDS))
            self.assertEqual([r["b"] for r in rows[1:]], ["09:15", "09:30"])
            self.assertIn("board", json.loads(json_path.read_text()))

    def test_a_day_with_no_sweeps_attaches_nothing_extra(self):
        st = state(timeline("09:30"))
        with TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AUDIT_DIR": tmp}):
                _, _, payload = audit.write(st, bot="1m")
        self.assertEqual(payload["board"]["files"], [])

    def test_a_candle_the_sweep_never_reached_is_named_in_the_file(self):
        st = self.day()                      # 09:15 and 09:30 — 09:20/09:25 absent
        with TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AUDIT_DIR": tmp}):
                _, _, payload = audit.write(st, bot="1m")
            first = json.loads(Path(payload["board"]["files"][0])
                               .read_text().splitlines()[0])["meta"]
        self.assertEqual(first["missing"], ["09:20", "09:25"])
        self.assertEqual(first["last_bar"], "09:30")

    def test_the_page_is_printable_and_says_what_a_cell_means(self):
        st = self.day()
        with TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AUDIT_DIR": tmp}):
                _, _, payload = audit.write(st, bot="1m")
            page = Path(payload["board"]["files"][1]).read_text()
        self.assertIn("@page { size: A4 landscape", page)
        self.assertIn("Sector board", page)
        self.assertIn("Every name, the whole session", page)
        self.assertIn("MARUTI", page)
        self.assertIn("HDFCBANK", page)
        self.assertIn("AUTO", page)                      # the heat map's heading
        self.assertIn("boxed cell is the sector that was picked", page)
        self.assertEqual(page.count("<table"), page.count("</table>"))
        self.assertEqual(page.count("<tr"), page.count("</tr>"))

    def test_the_end_of_day_message_attaches_the_board_as_well(self):
        st = self.day()
        docs = []
        with TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AUDIT_DIR": tmp, "EOD_AUDIT": "1"}):
                eod.send_eod(st, bot="1m",
                             send=lambda text, dry_run=False: True,
                             send_doc=lambda path, caption="": docs.append(path) or True)
        self.assertEqual([Path(p).suffix for p in docs], [".json", ".xlsx", ".jsonl", ".html"])


class SweepBoardTests(TestCase):
    """Bot 3's call site: one call, never a lost candle."""

    def test_a_sweep_that_blows_up_does_not_reach_the_scan(self):
        with patch.object(board, "sweep", side_effect=RuntimeError("boom")):
            self.assertIsNone(scan1m.sweep_board(now=datetime(2026, 11, 20, 9, 35,
                                                              tzinfo=IST)))

    def test_the_loop_calls_it_after_the_alerts(self):
        import inspect

        source = inspect.getsource(scan1m.live_loop_1m)
        self.assertIn("sweep_board(now=now)", source)
        # ... and after the projection of this minute, not before it
        self.assertLess(source.index("run_minute(bar, now=now"),
                        source.index("sweep_board(now=now)"))
