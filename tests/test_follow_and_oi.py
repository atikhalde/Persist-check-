"""Sector follow (extra sectors) and the open-interest line/filter inside the main scan."""
from datetime import timedelta
from unittest import TestCase
from pathlib import Path
from unittest.mock import patch

from nse_scanner import oi_live, pipeline, sector_follow, state
import test_live_alerts as base

BAR, signal = base.BAR, base.signal


class FollowScanTests(TestCase):
    market_at_bar = base.LiveAlertTests.market_at_bar

    def setUp(self):
        base.LiveAlertTests.setUp(self)
        self.uni["sectors"]["PHA"] = {"index": "P", "stocks": ["ABC", "PQR"]}
        self.uni["sectors"]["ENE"] = {"index": "E", "stocks": ["XYZ", "ABC"]}
        self.md.dir_state, self.md.dir_label, self.md.dir_conf = 0, "NEUTRAL", 52
        self.md.pick_sector = "PHA"
        self.md.pick_reason = "SECTOR FOLLOW — Nifty NEUTRAL, PHA leads; also scanning ENE 60"
        self.md.extra_sectors = ["ENE"]
        self.md.flow_aligned = ["ENE"]
        self.format = self.mocks[7]

    def test_extra_sector_is_scanned_and_each_alert_names_its_own_sector(self):
        self.scan.side_effect = lambda raw, *a, **k: [signal()] if raw in ("ABC", "XYZ") else []
        out = pipeline.run_once()
        self.assertEqual({s["sector"] for s in out["signals"]}, {"PHA", "ENE"})
        self.assertEqual([c.args[0] for c in self.scan.call_args_list], ["ABC", "PQR", "XYZ"])
        self.assertEqual([c.kwargs["sector"] for c in self.format.call_args_list], ["PHA", "ENE"])
        scan = state.load()["scan"]
        self.assertEqual((scan["sector"], scan["extra"], scan["follow"]), ("PHA", ["ENE"], True))

    def test_real_alert_text_is_tagged(self):
        self.format.side_effect = base.ORIGINAL_FORMAT_STOCK
        self.scan.side_effect = lambda raw, *a, **k: [signal()] if raw == "XYZ" else []
        with patch.object(pipeline, "build_plan", return_value=base.TradePlan(
                100.0, 100.0, 99.98, 0.02, 100.02, 100.04, 100.06, ("R1", "R2", "R3"))):
            pipeline.run_once()
        text = self.send.call_args_list[-1].args[0]
        self.assertIn("Sector <b>ENE</b> (sector follow, flow aligned)  |  Market NEUTRAL (52%)", text)

    def test_normal_pick_scans_one_sector_only(self):
        self.md.pick_reason, self.md.extra_sectors = "PERSIST + PHA qualified", []
        pipeline.run_once()
        self.assertEqual([c.args[0] for c in self.scan.call_args_list], ["ABC", "PQR"])
        self.assertFalse(state.load()["scan"]["follow"])

    def test_old_commands_run_the_main_scanner(self):
        with patch.object(pipeline, "run_once", return_value={"signals": [{"oi_regime": "LONG_BUILDUP"}, {}]}) as once:
            self.assertEqual(len(sector_follow.run_once_sector_follow(force_sector="IT")["signals"]), 2)
            out = oi_live.run_once_dual()
            self.assertEqual((len(out["original"]["signals"]), len(out["oi"]["signals"])), (2, 1))
            self.assertEqual(once.call_count, 2)
        with patch.object(pipeline, "live_loop", return_value="closed") as loop:
            self.assertEqual(sector_follow.live_loop_sector_follow(), "closed")
            self.assertEqual(oi_live.live_loop_dual(), "closed")
            self.assertEqual(loop.call_count, 2)


class OpenInterestTests(TestCase):
    market_at_bar = base.LiveAlertTests.market_at_bar

    def setUp(self):
        base.LiveAlertTests.setUp(self)
        self.format = self.mocks[7]
        self.csv = Path(self.tmp.name) / "oi.csv"
        env = patch.dict("os.environ", {"OI_FILE": str(self.csv), "OI_SOURCE": "csv"})
        env.start()
        self.addCleanup(env.stop)

    def rows(self, *rows):
        self.csv.write_text("symbol,timestamp,oi,close\n" + "".join(
            f"{sym},{(BAR + timedelta(minutes=off)):%Y-%m-%d %H:%M},{oi},{close}\n"
            for sym, off, oi, close in rows))

    def run_mode(self, mode):
        with patch.dict("os.environ", {"OI_FILTER": mode}):
            return pipeline.run_once()

    def oi_line(self):
        return self.format.call_args.kwargs["oi_line"]

    def test_no_oi_data_never_blocks_or_decorates_an_alert(self):
        for mode in ("annotate", "require"):
            self.setUp()
            self.assertEqual(len(self.run_mode(mode)["signals"]), 1, mode)   # no file at all
            self.assertEqual(self.oi_line(), "")
        self.setUp()
        self.rows(("ABC", -24 * 60, 1000, 100.0))                             # only yesterday's rows
        self.assertEqual(len(self.run_mode("require")["signals"]), 1)

    def test_annotate_adds_the_reading_and_never_filters(self):
        self.rows(("ABC", -5, 100000, 100.0), ("ABC", 0, 98000, 100.4))       # short covering
        out = self.run_mode("annotate")
        self.assertEqual(out["signals"][0]["oi_regime"], "SHORT_COVERING")
        self.assertEqual(self.oi_line(), "OI <b>Short covering</b> · OI -2.00% · price +0.40% (5m)\n")

    def test_require_sends_only_oi_confirmed_entries(self):
        self.rows(("ABC", -5, 100000, 100.0), ("ABC", 0, 98000, 100.4))       # price up, OI down
        self.assertEqual(self.run_mode("require")["signals"], [])
        self.assertNotIn("ABC", state.load()["sent"])
        self.rows(("ABC", -5, 100000, 100.0), ("ABC", 0, 102000, 100.4))      # long build-up
        self.assertEqual(len(self.run_mode("require")["signals"]), 1)
        self.assertIn("Long Build-up", self.oi_line())

    def test_require_rejects_a_stock_without_a_row_when_others_have_data(self):
        self.rows(("OTHER", -5, 100000, 100.0), ("OTHER", 0, 102000, 100.4))
        self.assertEqual(self.run_mode("require")["signals"], [])
        self.assertEqual(len(self.run_mode("annotate")["signals"]), 1)

    def test_off_ignores_the_file_and_a_broken_file_does_not_stop_the_scan(self):
        self.rows(("ABC", -5, 100000, 100.0), ("ABC", 0, 98000, 100.4))
        self.assertEqual(len(self.run_mode("off")["signals"]), 1)
        self.assertEqual(self.oi_line(), "")
        self.setUp()
        self.csv.write_text("symbol,timestamp,oi,close\nABC,2026-11-09 10:00,not-a-number,1\n")
        self.assertEqual(len(self.run_mode("require")["signals"]), 1)
