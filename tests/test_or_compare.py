"""Opening-range comparison — the measuring tool, not the scanner (no network)."""

from unittest import TestCase
from unittest.mock import patch

import pandas as pd

from nse_scanner import market, or_compare
from nse_scanner.session import IST

from datetime import date, datetime


def bars(rows, day=date(2026, 11, 18)):
    idx = [datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time(), tzinfo=IST)
           for hhmm, *_ in rows]
    return pd.DataFrame({"Open": [r[1] for r in rows], "High": [r[2] for r in rows],
                         "Low": [r[3] for r in rows], "Close": [r[4] for r in rows]}, index=idx)


class OpeningRangePatchTests(TestCase):
    def test_the_engine_is_patched_and_always_restored(self):
        before = market.OR_MINUTES
        with or_compare.opening_range(15):
            self.assertEqual(market.OR_MINUTES, 15)
        self.assertEqual(market.OR_MINUTES, before)

    def test_the_patch_is_restored_even_when_the_replay_raises(self):
        before = market.OR_MINUTES
        with self.assertRaises(RuntimeError):
            with or_compare.opening_range(15):
                raise RuntimeError("boom")
        self.assertEqual(market.OR_MINUTES, before)


class OutcomeTests(TestCase):
    """The fire's own EMA9 stop decides the risk; the close decides the score."""

    def test_a_short_that_never_trades_up_scores_its_close(self):
        df = bars([("09:25", 100, 100, 100, 100), ("09:30", 100, 100.2, 99.0, 99.2),
                   ("09:35", 99.2, 99.4, 98.0, 98.2)])
        res = or_compare.outcome(df, df.index[0], "SELL", 100.0, 101.0)
        self.assertFalse(res["stopped"])
        self.assertAlmostEqual(res["r_close"], 1.8)
        self.assertAlmostEqual(res["mfe"], 2.0)

    def test_a_stop_taken_first_is_minus_one_r_even_if_the_close_is_better(self):
        df = bars([("09:25", 100, 100, 100, 100), ("09:30", 100, 101.5, 100, 99.0),
                   ("09:35", 99.0, 99.1, 96.0, 96.1)])
        res = or_compare.outcome(df, df.index[0], "SELL", 100.0, 101.0)
        self.assertTrue(res["stopped"])
        self.assertEqual(res["stop_at"], "09:30")
        self.assertAlmostEqual(res["r_close"], -1.0)

    def test_a_long_mirrors_the_short(self):
        df = bars([("09:25", 100, 100, 100, 100), ("09:30", 100, 102.0, 99.6, 101.5)])
        res = or_compare.outcome(df, df.index[0], "BUY", 100.0, 99.0)
        self.assertAlmostEqual(res["r_close"], 1.5)
        self.assertAlmostEqual(res["mfe"], 2.0)

    def test_a_zero_risk_entry_is_not_scored(self):
        df = bars([("09:25", 100, 100, 100, 100)])
        self.assertIsNone(or_compare.outcome(df, df.index[0], "SELL", 100.0, 100.0)["r_close"])


class LiveEntryTests(TestCase):
    """An entry only exists if its fire happened on a candle whose pick was live."""

    def tape(self):
        tape = or_compare.Tape.__new__(or_compare.Tape)      # no fetching
        tape.target = date(2026, 11, 18)
        tape.uni = {"sectors": {"CDUR": {"index": "CDUR-I", "stocks": ["DIXON", "TITAN"]}}}
        tape.stocks = {raw: bars([("09:25", 100, 100, 100, 100),
                                  ("09:30", 100, 100.1, 100, 100),
                                  ("09:35", 100, 100.2, 99.9, 99.0)]) for raw in ("DIXON", "TITAN")}
        tape.daily = {}
        return tape

    def run_entries(self, path, fires):
        def scan(raw, frame, daily, keep_all=False, as_of=None):
            return [fires[raw]]

        with patch.object(or_compare, "scan_stock", side_effect=scan):
            return or_compare.live_entries(self.tape(), path, "CDUR", -1)

    def fire(self, raw, time, price=100.0, sl=101.0):
        from types import SimpleNamespace
        return SimpleNamespace(symbol=raw, name=raw, side="SELL", time=time,
                               price=price, sl=sl, code=101, score=70, strength="Strong",
                               reason="test")

    def test_a_fire_inside_the_opening_range_is_not_an_entry(self):
        # The pick only exists from 09:45: the 09:25 fire is Historical, not live.
        path = [{"time": "09:25", "pick": "WAIT", "side": 0},
                {"time": "09:45", "pick": "CDUR", "side": -1}]
        fires = {"DIXON": self.fire("DIXON", "09:25"), "TITAN": self.fire("TITAN", "09:25")}
        self.assertEqual([], self.run_entries(path, fires))

    def test_the_same_fires_are_entries_once_the_pick_arrives_earlier(self):
        # OR 15: the pick exists from 09:30, so a 09:30 fire is live and taken.
        path = [{"time": "09:30", "pick": "CDUR", "side": -1}]
        fires = {"DIXON": self.fire("DIXON", "09:30"), "TITAN": self.fire("TITAN", "09:35")}
        rows = self.run_entries(path, fires)
        self.assertEqual([r["raw"] for r in rows], ["DIXON"])
        self.assertEqual(rows[0]["time"], "09:30")
        self.assertAlmostEqual(rows[0]["r_close"], 1.0)     # 100 -> 99.0 on a 1.0 stop


class ReportTests(TestCase):
    def day(self):
        return {"date": "2026-11-18", "results": [
            {"or": 30, "first_pick": "09:45 CDUR short", "dominant": "CDUR short",
             "wait_bars": 3, "net_r": 0.4, "stops": 0,
             "entries": [{"raw": "LGEINDIA", "time": "10:15", "r_close": 0.4, "stopped": False}]},
            {"or": 15, "first_pick": "09:30 CDUR short", "dominant": "CDUR short",
             "wait_bars": 0, "net_r": -0.6, "stops": 1,
             "entries": [{"raw": "DIXON", "time": "09:30", "r_close": 0.4, "stopped": False},
                         {"raw": "TITAN", "time": "09:30", "r_close": -1.0, "stopped": True}]},
        ]}

    def test_the_table_names_both_opening_ranges_and_their_entries(self):
        text = or_compare.render(self.day())
        self.assertIn("OR 30 min", text)
        self.assertIn("OR 15 min", text)
        self.assertIn("09:45 CDUR short", text)
        self.assertIn("09:30 CDUR short", text)
        self.assertIn("DIXON@09:30 (+0.4R)", text)
        self.assertIn("-1.0R", text)

    def test_the_totals_add_the_sessions_up(self):
        text = or_compare.render_totals([self.day(), self.day()])
        self.assertIn("| OR 30 min (sessions 2) | 2 | +0.8R | +0.40R | 2/2 | 0 |", text)
        self.assertIn("| OR 15 min (sessions 2) | 4 | -1.2R | -0.30R | 2/4 | 2 |", text)

    def test_the_report_says_what_it_does_not_replay(self):
        text = or_compare.render_report([self.day()], (30, 15))
        self.assertIn("The entry cap", text)
        self.assertIn("not what the bots would have sent", text)
