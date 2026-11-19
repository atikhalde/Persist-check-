"""The trader's ledger, the cost model, the workbook and the audit files.

R gives the rule; this is what the day paid. Every number here has to survive a
human checking it: size × price move, minus a stated cost, equals the money the
report prints — and the same day has to come out of the JSON and the workbook.
"""
from __future__ import annotations

import json
import os
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
import test_live_alerts as base
from nse_scanner import audit, costs, ledger, xlsx


def state(**stats) -> dict:
    return {"session": "2026-11-20", "sent": {}, "positions": {}, "stats": stats}


def trade(**kw) -> dict:
    rec = {"raw": "MARUTI", "side": "BUY", "time": "10:15", "entry": 100.0, "exit": 110.0,
           "r": 2.0, "why": "EMA9 EXIT", "t_hit": 1, "sector": "AUTO",
           "entry_bar": "2026-11-20T09:20:00+05:30", "stop0": 95.0,
           "t1": 108.0, "t2": 112.0, "risk": 5.0}
    rec.update(kw)
    return rec


# ── the cost model ─────────────────────────────────────────────────────────
class CostTests(base.TestCase):
    def test_a_round_trip_is_turnover_times_bps_with_a_floor_per_order(self):
        with patch.dict(os.environ, {"COST_BPS": "5", "COST_MIN": "20"}, clear=False):
            money = costs.round_trip("BUY", 100.0, 110.0, 1000)
        self.assertEqual(money["qty"], 1000)
        self.assertEqual(money["gross"], 10000.0)
        self.assertEqual(money["costs"], 105.0)        # 0.05% of ₹1,00,000 + of ₹1,10,000
        self.assertEqual(money["net"], 9895.0)

    def test_a_small_position_pays_the_floor_not_the_percentage(self):
        with patch.dict(os.environ, {"COST_BPS": "5", "COST_MIN": "20"}, clear=False):
            money = costs.round_trip("BUY", 10.0, 11.0, 10)     # ₹100 turnover a side
        self.assertEqual(money["costs"], 40.0)         # 2 × ₹20

    def test_a_short_earns_when_the_price_falls(self):
        money = costs.round_trip("SELL", 100.0, 95.0, 100)
        self.assertGreater(money["gross"], 0)
        self.assertAlmostEqual(money["gross"], 500.0)

    def test_the_size_comes_from_the_capital_and_the_price(self):
        with patch.dict(os.environ, {"TRADE_CAPITAL": "100000"}, clear=False):
            self.assertEqual(costs.quantity(250.0), 400)
            self.assertEqual(costs.quantity(300000.0), 0)      # not affordable
            self.assertEqual(costs.quantity(0.0), 0)

    def test_a_bad_knob_falls_back_to_the_default_instead_of_crashing(self):
        with patch.dict(os.environ, {"COST_BPS": "nonsense", "COST_MIN": "",
                                     "TRADE_CAPITAL": "?"}, clear=False):
            self.assertEqual(costs.cost_bps(), 5.0)
            self.assertEqual(costs.cost_min(), 20.0)
            self.assertEqual(costs.trade_capital(), 100_000.0)


# ── the ledger rows ────────────────────────────────────────────────────────
class LedgerTests(base.TestCase):
    def test_a_closed_trade_is_a_row_with_size_prices_costs_and_a_running_total(self):
        rows = ledger.closed_rows(state(closed=[trade(), trade(raw="M&M", exit=95.0,
                                                               r=-1.0, why="SL HIT")]))
        first, second = rows
        self.assertEqual((first["name"], first["qty"]), ("MARUTI", 1000))
        self.assertEqual((first["entry"], first["exit"]), (100.0, 110.0))
        self.assertEqual(first["gross_rs"], 10000.0)
        self.assertEqual(first["costs_rs"], 105.0)       # 0.05% of ₹1,00,000 + ₹1,10,000
        self.assertEqual(first["net_rs"], 9895.0)
        self.assertEqual(first["cum_rs"], 9895.0)
        self.assertLess(second["cum_rs"], first["cum_rs"])
        self.assertEqual(first["hold_min"], 55.0)

    def test_the_net_r_is_the_money_relative_to_the_money_risked(self):
        rows = ledger.closed_rows(state(closed=[trade()]))
        row = rows[0]
        self.assertEqual(row["risk_rs"], 5000.0)          # 5.0 × 1000 shares
        self.assertEqual(row["gross_r"], 2.0)
        self.assertAlmostEqual(row["net_r"], round(9895.0 / 5000.0, 2))

    def test_an_open_trade_is_marked_and_costed_as_if_it_closed_there(self):
        opened = ledger.open_rows({"session": "2026-11-20", "stats": {}, "positions": {
            "MARUTI": {"side": "BUY", "entry": 100.0, "risk": 5.0, "sl": 95.0, "qty": 1000,
                       "last_close": 105.0, "hit_targets": [1], "sector": "AUTO"}}})
        self.assertEqual(opened[0]["mark"], 105.0)
        self.assertEqual(opened[0]["gross_rs"], 5000.0)
        self.assertGreater(opened[0]["net_rs"], 0)
        self.assertEqual(opened[0]["targets_hit"], 1)

    def test_an_open_trade_with_no_candle_is_named_not_zeroed(self):
        opened = ledger.open_rows({"session": "2026-11-20", "stats": {}, "positions": {
            "PNB": {"side": "BUY", "entry": 100.0, "risk": 5.0, "last_close": None}}})
        self.assertIsNone(opened[0]["mark"])
        self.assertIsNone(opened[0]["net_rs"])
        self.assertIn("no candle", opened[0]["why"])

    def test_the_day_totals_carry_the_hit_rate_expectancy_and_drawdown(self):
        rows = ledger.closed_rows(state(closed=[
            trade(exit=110.0), trade(raw="B", exit=95.0, r=-1.0),
            trade(raw="C", exit=120.0, r=4.0), trade(raw="D", exit=98.0, r=-0.4)]))
        books = ledger.totals(rows)
        self.assertEqual((books["trades"], books["wins"], books["losses"]), (4, 2, 2))
        self.assertEqual(books["win_rate"], 50.0)
        self.assertGreater(books["gross_rs"], books["net_rs"])
        self.assertGreater(books["costs_rs"], 0)
        self.assertGreater(books["max_dd_rs"], 0)
        self.assertEqual(len(books["equity"]), 4)
        self.assertIsNotNone(books["profit_factor"])
        self.assertEqual(books["avg_hold_min"], 55)

    def test_a_day_with_no_trade_says_so_instead_of_printing_zeros(self):
        books = ledger.totals([])
        self.assertEqual(books["trades"], 0)
        self.assertIn("No trade was closed today", ledger.summary_line(books))

    def test_the_one_line_summary_names_the_money_and_the_drawdown(self):
        books = ledger.totals(ledger.closed_rows(state(closed=[trade()])))
        line = ledger.summary_line(books)
        self.assertIn("1 closed · 1W/0L", line)
        self.assertIn("net ₹", line)


# ── the workbook ───────────────────────────────────────────────────────────
class XlsxTests(base.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_the_file_is_a_zip_with_one_sheet_per_name(self):
        path = xlsx.write(self.dir / "a.xlsx", {"Summary": [["a", "b"], [1, 2]],
                                                "Trades": [["x"]]})
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            self.assertIn("xl/workbook.xml", names)
            self.assertIn("xl/worksheets/sheet2.xml", names)
            sheet1 = z.read("xl/worksheets/sheet1.xml").decode()
            self.assertIn("<t xml:space=\"preserve\">a</t>", sheet1)
            self.assertIn("<v>1</v>", sheet1)

    def test_a_sheet_name_excel_would_refuse_is_cleaned(self):
        path = xlsx.write(self.dir / "b.xlsx", {"a/b:c*d?" * 8: [["x"]]})
        with zipfile.ZipFile(path) as z:
            workbook = z.read("xl/workbook.xml").decode()
        self.assertNotIn("a/b", workbook)
        self.assertLess(len(workbook), 2000)

    def test_special_characters_are_escaped_not_dropped(self):
        path = xlsx.write(self.dir / "c.xlsx", {"S": [["<a & b>"]]})
        with zipfile.ZipFile(path) as z:
            sheet = z.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("&lt;a &amp; b&gt;", sheet)

    def test_nan_and_infinity_do_not_produce_an_unreadable_file(self):
        path = xlsx.write(self.dir / "d.xlsx", {"S": [[float("nan"), float("inf"), 3.5]]})
        with zipfile.ZipFile(path) as z:
            sheet = z.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("—", sheet)
        self.assertIn("<v>3.5</v>", sheet)


# ── the audit document ─────────────────────────────────────────────────────
class AuditTests(base.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"AUDIT_DIR": str(self.dir)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def day(self) -> dict:
        st = state(
            candles=4, picks=3, entries=2,
            timeline=[{"b": "09:20", "tilt": 1, "conf": 74, "label": "BULL",
                       "regime": "TREND", "pick": "AUTO", "side": 1,
                       "sec": {"AUTO": [60, 10, 1.2, 0.4, 0],
                               "IT": [40, 20, -0.3, -0.1, 0]}},
                      {"b": "09:25", "tilt": 1, "conf": 74, "label": "BULL",
                       "regime": "TREND", "pick": "AUTO", "side": 1,
                       "sec": {"AUTO": [78, 8, 1.4, 0.6, 1],
                               "IT": [38, 22, -0.4, -0.2, 0]}}],
            leadership=[{"b": "09:25", "sec": {"AUTO": [
                {"name": "MARUTI", "rank": 78, "strength": 71, "volume": 92,
                 "structure": 78, "kept": True}]}}],
            rejected={"weak": {"MARUTI 10:35": "MARUTI 10:35 — rank 12 is behind "
                                                "its sector's runners"}},
            closed=[trade()])
        st["market"] = {"label": "BULL", "conf": 74, "regime": "TREND"}
        st["sector"] = {"name": "AUTO", "side": 1}
        st["positions"] = {"PNB": {"side": "BUY", "entry": 100.0, "risk": 5.0, "qty": 1000,
                                   "last_close": 105.0, "sector": "AUTO"}}
        return st

    def test_the_json_carries_the_whole_day(self):
        json_path, xlsx_path, payload = audit.write(self.day(), bot="5m")
        data = json.loads(json_path.read_text())
        self.assertEqual(data["session"], "2026-11-20")
        self.assertEqual(len(data["timeline"]), 2)
        self.assertEqual(data["timeline"][1]["sectors"]["AUTO"]["qualified"], True)
        self.assertEqual(len(data["blocked"]), 1)
        self.assertTrue(data["blocked"][0]["meaning"])
        self.assertEqual(data["books"]["trades"], 1)
        self.assertEqual(data["sectors"]["AUTO"]["max"], 78)
        self.assertEqual(data["leadership"][0]["strength"], 71)
        self.assertEqual(data["costs"]["bps_per_side"], 5.0)
        self.assertIn("MAX_ENTRIES_PER_PICK", data["rules"])
        self.assertTrue(xlsx_path.exists())

    def test_the_workbook_has_one_sheet_per_section(self):
        _, xlsx_path, _ = audit.write(self.day())
        with zipfile.ZipFile(xlsx_path) as z:
            workbook = z.read("xl/workbook.xml").decode()
        for name in ("Summary", "Trades", "Blocked", "Timeline", "Sectors",
                     "SectorSeries", "Leadership", "Diagnostics"):
            self.assertIn(f'name="{name}"', workbook)

    def test_a_sector_day_counts_candles_above_the_gate_and_the_spells(self):
        stats = audit.sector_stats(self.day())
        auto = stats["AUTO"]
        self.assertEqual((auto["min"], auto["max"], auto["now"]), (60.0, 78.0, 78.0))
        self.assertEqual(auto["above_70"], 1)
        self.assertEqual(auto["gate_candles"], 1)
        self.assertEqual([(p["from"], p["to"]) for p in auto["gate_periods"]],
                         [("09:25", "09:25")])
        self.assertEqual(auto["series"], [["09:20", 60], ["09:25", 78]])

    def test_a_crossing_is_recorded_once_per_level(self):
        st = state(timeline=[
            {"b": "09:20", "tilt": 1, "conf": 70, "label": "BULL", "pick": "AUTO",
             "side": 1, "sec": {"AUTO": [60, 10, 0, 0, 0]}},
            {"b": "09:25", "tilt": 1, "conf": 70, "label": "BULL", "pick": "AUTO",
             "side": 1, "sec": {"AUTO": [75, 10, 0, 0, 1]}},
            {"b": "09:30", "tilt": 1, "conf": 70, "label": "BULL", "pick": "AUTO",
             "side": 1, "sec": {"AUTO": [90, 10, 0, 0, 1]}},
            {"b": "09:35", "tilt": 1, "conf": 70, "label": "BULL", "pick": "AUTO",
             "side": 1, "sec": {"AUTO": [80, 10, 0, 0, 1]}}])
        crosses = audit.sector_stats(st)["AUTO"]["transitions"]
        self.assertEqual([(c["bar"], c["level"], c["dir"]) for c in crosses],
                         [("09:25", 70, "up"), ("09:30", 85, "up"), ("09:35", 85, "down")])

    def test_a_sector_that_never_moved_has_no_crossing(self):
        st = state(timeline=[
            {"b": "09:20", "tilt": 1, "conf": 70, "label": "BULL", "pick": "IT",
             "side": 1, "sec": {"IT": [40, 20, 0, 0, 0]}},
            {"b": "09:25", "tilt": 1, "conf": 70, "label": "BULL", "pick": "IT",
             "side": 1, "sec": {"IT": [38, 22, 0, 0, 0]}}])
        self.assertEqual(audit.sector_stats(st)["IT"]["transitions"], [])

    def test_the_entries_of_the_day_land_on_their_sector(self):
        stats = audit.sector_stats(self.day())
        self.assertEqual(stats["AUTO"]["entries"], 2)      # one closed, one open

    def test_the_two_bots_write_different_files(self):
        json_5m, _ = audit.paths("2026-11-20", "5m")
        json_1m, _ = audit.paths("2026-11-20", "1m")
        self.assertNotEqual(json_5m, json_1m)
        self.assertIn("5m", json_5m.name)

    def test_an_empty_session_still_writes_valid_files(self):
        json_path, xlsx_path, payload = audit.write({"session": "2026-11-20", "stats": {}})
        self.assertEqual(payload["books"]["trades"], 0)
        self.assertEqual(json.loads(json_path.read_text())["timeline"], [])
        self.assertTrue(xlsx_path.stat().st_size > 1000)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
