"""Dual pick: the leading sector on the *other* side of the market, on the same tape.

19 Nov, 09:45: the pick was IT short for eight candles while ``PERSIST +`` held
AUTO at 98 with its live flow aligned (``AUTO▲ Q (88)``). ``_follow_fallback``
returns ``None`` the moment a pick exists, so no AUTO stock was scanned long at
all — and the one long the bot did take an hour later (BAJAJ-AUTO, +5.7R at T2)
came off exactly those numbers.

The opposite side is taken only when it is a leader in its own right: the same
test the NEUTRAL follow already applies, so a quiet sector can never open a
second book.

The second half of this file is the other half of the same request: a sector is
not its twelve names. ``market._sector_leaders`` keeps the top ``SECTOR_TOP_N``
by how far each name leads its sector index.
"""
from datetime import timedelta
from unittest import TestCase
from unittest.mock import patch

import numpy as np
import pandas as pd

from nse_scanner import pipeline
from nse_scanner.market import _dual_leader, _is_leading_short, run_market
from nse_scanner.session import IST
from test_sector_pick import DAYS, bars, daily, series

LAST = pd.Timestamp(DAYS[-1], tz=IST) + pd.Timedelta(hours=15, minutes=20)


def snap(name, **over):
    base = dict(name=name, side=1, persist_bull=60.0, persist_bear=60.0, day_ret=0.0,
                pos=50.0, rs=0.0, bull=60.0, bear=60.0, recent=0.0, qualified=False,
                lag_bars=0, stale=False)
    return type("S", (), {**base, **over})()


class DualLeaderTests(TestCase):
    """Which sector earns the second book, and which does not."""

    def test_a_leading_long_is_taken_while_the_pick_is_short(self):
        # The 19 Nov shape: AUTO long 98 against an IT short.
        auto = snap("AUTO", side=1, persist_bull=98.0, day_ret=2.60, pos=98.0, rs=1.2,
                    bull=75.0, recent=0.30)
        self.assertEqual(_dual_leader("IT", -1, [auto], []),
                         {"sector": "AUTO", "side": 1})

    def test_a_leading_short_is_taken_while_the_pick_is_long(self):
        it = snap("IT", side=-1, persist_bear=96.0, day_ret=-1.40, pos=8.0, rs=-1.1,
                  bear=78.0, recent=-0.25)
        self.assertEqual(_dual_leader("AUTO", 1, [], [it]),
                         {"sector": "IT", "side": -1})

    def test_a_quiet_sector_does_not_get_a_second_book(self):
        quiet = snap("FMCG", side=1, persist_bull=58.0, day_ret=0.10, pos=55.0, rs=0.02,
                     bull=58.0, recent=0.01)
        self.assertIsNone(_dual_leader("IT", -1, [quiet], []))

    def test_the_pick_itself_is_never_the_alt(self):
        auto = snap("AUTO", side=1, persist_bull=98.0, day_ret=2.60, pos=98.0, rs=1.2,
                    bull=75.0, recent=0.30)
        self.assertIsNone(_dual_leader("AUTO", -1, [auto], []))

    def test_a_wait_has_no_alt(self):
        auto = snap("AUTO", side=1, persist_bull=98.0, day_ret=2.60, pos=98.0, rs=1.2,
                    bull=75.0, recent=0.30)
        self.assertIsNone(_dual_leader(None, 0, [auto], []))

    def test_the_knob_switches_it_off(self):
        auto = snap("AUTO", side=1, persist_bull=98.0, day_ret=2.60, pos=98.0, rs=1.2,
                    bull=75.0, recent=0.30)
        with patch.dict("os.environ", {"DUAL_PICK": "0"}):
            self.assertIsNone(_dual_leader("IT", -1, [auto], []))

    def test_a_stale_sector_cannot_lead_a_dual_pick(self):
        auto = snap("AUTO", side=1, persist_bull=98.0, day_ret=2.60, pos=98.0, rs=1.2,
                    bull=75.0, recent=0.30, stale=True)
        self.assertIsNone(_dual_leader("IT", -1, [auto], []))


class ShortLeadershipTests(TestCase):
    """The mirror test, pinned: it must reject a sector that is merely quiet."""

    def test_a_falling_sector_with_bearish_flow_leads(self):
        self.assertTrue(_is_leading_short(snap("IT", side=-1, persist_bear=96.0,
                                               day_ret=-1.4, pos=8.0, rs=-1.1,
                                               bear=78.0, recent=-0.25), 75.0))

    def test_a_sector_that_is_up_on_the_day_cannot_lead_a_short(self):
        self.assertFalse(_is_leading_short(snap("IT", side=-1, persist_bear=96.0,
                                                day_ret=0.9, pos=8.0, rs=-1.1,
                                                bear=78.0, recent=-0.25), 75.0))

    def test_a_bearish_sector_in_the_top_of_its_range_cannot_lead_a_short(self):
        self.assertFalse(_is_leading_short(snap("IT", side=-1, persist_bear=96.0,
                                                day_ret=-1.4, pos=72.0, rs=-1.1,
                                                bear=78.0, recent=-0.25), 75.0))


class DualPickOnATapeTests(TestCase):
    """``run_market`` itself: a falling index with IT weak and AUTO running."""

    def decide(self, step: float, **env):
        n5 = self.nifty(step)
        sectors = {"AUTO": sector(self.auto), "IT": sector(self.it)}
        vix = bars(DAYS[-1], series(15.0, 0.0))
        with patch.dict("os.environ", {"SECTOR_FOLLOW": "1", **env}):
            return run_market(n5, daily(n5), vix, daily(vix), sectors,
                              {k: daily(v) for k, v in sectors.items()})

    auto = 0.00035
    it = -0.00045

    @staticmethod
    def nifty(step):
        out, level = [], 25000.0
        for d in DAYS:
            seg = series(level, step)
            out.append(bars(d, seg))
            level = seg[-1]
        return pd.concat(out)

    def test_a_bear_pick_names_the_leading_long_at_the_same_time(self):
        md = self.decide(-0.00006)
        self.assertEqual((md.pick_sector, md.pick_side), ("IT", -1))
        self.assertEqual((md.alt_sector, md.alt_side), ("AUTO", 1))
        self.assertIn("DUAL", md.alt_reason)

    def test_a_neutral_market_follow_stays_one_book(self):
        """The NEUTRAL follow is long-only by design — no mirror short on it.

        On a flat index the follow picks the leading long sector; IT is a
        leading short on the same tape and is deliberately not added.
        """
        md = self.decide(0.0)
        self.assertEqual((md.pick_sector, md.pick_side), ("AUTO", 1))
        self.assertEqual((md.alt_sector, md.alt_side), ("", 0))

    def test_the_knob_falls_back_to_the_single_pick(self):
        md = self.decide(-0.00006, DUAL_PICK="0")
        self.assertEqual((md.pick_sector, md.pick_side), ("IT", -1))
        self.assertEqual((md.alt_sector, md.alt_side), ("", 0))
        self.assertEqual(md.alt_reason, "")


def sector(step: float, n: int = 75):
    out, level = [], 1000.0
    for d in DAYS:
        seg = series(level, step)
        out.append(bars(d, seg[:n]))
        level = seg[-1]
    return pd.concat(out)


# ── the tail of a sector: only the leaders are scanned ───────────────────────
def frame(day, opens, closes):
    t = pd.Timestamp(day, tz=IST) + pd.Timedelta(hours=9, minutes=15)
    idx = []
    for _ in closes:
        idx.append(t)
        t += pd.Timedelta(minutes=5)
    o = np.asarray(opens, dtype=float)
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c), "Low": np.minimum(o, c),
                         "Close": c, "Volume": 1.0}, index=pd.DatetimeIndex(idx))


class ScanSidesTests(TestCase):
    """The close scan carries a side per stock, not one for the whole scan."""

    UNI = {"sectors": {"IT": {"stocks": ["HCLTECH", "WIPRO"]},
                       "AUTO": {"stocks": ["MARUTI", "BAJAJ-AUTO"]}}}

    def test_the_pick_and_the_dual_pick_keep_their_own_sides(self):
        sector_of, side_of = pipeline._scan_sides(self.UNI, "IT", -1, (), "AUTO", 1)
        self.assertEqual(sector_of["HCLTECH"], "IT")
        self.assertEqual(side_of["HCLTECH"], -1)
        self.assertEqual(sector_of["MARUTI"], "AUTO")
        self.assertEqual(side_of["MARUTI"], 1)

    def test_a_name_in_both_books_keeps_the_pick_side(self):
        uni = {"sectors": {"IT": {"stocks": ["TCS"]}, "AUTO": {"stocks": ["TCS"]}}}
        _, side_of = pipeline._scan_sides(uni, "IT", -1, (), "AUTO", 1)
        self.assertEqual(side_of["TCS"], -1)

    def test_no_alt_means_one_side_for_everything(self):
        _, side_of = pipeline._scan_sides(self.UNI, "IT", -1)
        self.assertEqual(set(side_of.values()), {-1})


class LeaderSelectionTests(TestCase):
    """A sector's leaders are ranked by how far each name leads the index."""

    def world(self, n_names: int = 8):
        names = [f"S{i}" for i in range(n_names)]
        frames = {}
        for i, name in enumerate(names):
            # Each name opens at 100 and closes 0.1% higher than the one before:
            # S7 leads the sector, S0 trails it.
            closes = [100.0 * (1 + 0.001 * i)] * 5
            frames[name] = frame(DAYS[-1], [100.0] * 5, closes)
        md = type("MD", (), {"sectors": {"IT": {"intra_ret": 0.20}}})()
        return names, frames, md

    def leaders(self, n_names: int = 8, **env):
        names, frames, md = self.world(n_names)
        tickers = [f"{x}.NS" for x in names]
        sector_of = {x: "IT" for x in names}
        side_of = dict.fromkeys(names, 1)
        with patch.dict("os.environ", env):
            return pipeline._sector_leaders(names, tickers, sector_of, side_of,
                                            dict(zip(tickers, [frames[x] for x in names])),
                                            tickers, md, LAST)

    def test_only_the_top_names_are_kept(self):
        keep = self.leaders()
        self.assertEqual(len(keep), 5)
        self.assertIn("S7", keep)                 # the biggest mover
        self.assertNotIn("S0", keep)              # the follower

    def test_the_number_can_be_moved(self):
        self.assertEqual(len(self.leaders(SECTOR_TOP_N="3")), 3)
        self.assertEqual(len(self.leaders(SECTOR_TOP_N="2")), 2)

    def test_zero_keeps_every_name(self):
        self.assertEqual(len(self.leaders(SECTOR_TOP_N="0")), 8)

    def test_a_name_without_a_candle_sinks_and_is_not_kept(self):
        names, frames, md = self.world(8)
        tickers = [f"{x}.NS" for x in names]
        sector_of = {x: "IT" for x in names}
        side_of = dict.fromkeys(names, 1)
        by_symbol = dict(zip(tickers, [frames[x] for x in names]))
        with patch.dict("os.environ", {"SECTOR_TOP_N": "2"}):
            keep = pipeline._sector_leaders(names, tickers, sector_of, side_of, by_symbol,
                                            tickers[:-1], md, LAST)   # S7 has no candle
        self.assertEqual(keep, {"S6", "S5"})


class AlertTextTests(TestCase):
    """A dual entry says which side of the market it is going against."""

    def test_the_note_is_printed_under_the_market_line(self):
        from nse_scanner.stock import StockSignal
        sig = StockSignal(symbol="AUTO.NS", code=101, name="BUY", side="BUY",
                          time="10:00", price=10608.0, score=57, strength="Medium",
                          base_active=21, cond22=0, reason="x", sl=10570.0)
        md = type("MD", (), {"pick_sector": "IT", "pick_side": -1, "dir_label": "BEAR",
                             "dir_conf": 62, "pick_reason": "", "alt_reason": ""})()
        plan = type("P", (), {"entry": 10608.0, "sl": 10570.0, "risk": 37.96, "ema9": 10570.0,
                              "t1": None, "t2": None, "t3": None, "pivot_names": (None,) * 3,
                              "basis": None, "skipped": ()})()
        text = pipeline.format_stock(sig, md, plan, sector="AUTO", dual="DUAL — AUTO long 98")
        self.assertIn("DUAL — AUTO long 98", text)
        self.assertNotIn("<i></i>", text)


# ── bot 3 (the 1m scan) picks up the same two books ─────────────────────────
class BotThreeTargetTests(TestCase):
    """The 1m scan reads the dual pick off the 5m ledger, sides and all."""

    UNI = {"sectors": {"IT": {"stocks": ["HCLTECH", "WIPRO"]},
                       "AUTO": {"stocks": ["MARUTI", "BAJAJ-AUTO"]}}}

    PICK = {"sector": "IT", "side": -1, "label": "BEAR", "extra": [],
            "alt": "AUTO", "alt_side": 1,
            "alt_reason": "DUAL — AUTO long 98 against the IT SHORT pick"}

    def test_the_alt_sector_is_scanned_with_its_own_side(self):
        from nse_scanner import scan1m
        sector_of, side_of, dual = scan1m._scan_targets(self.UNI, self.PICK)
        self.assertEqual(sector_of["MARUTI"], "AUTO")
        self.assertEqual(side_of["MARUTI"], 1)
        self.assertEqual(side_of["HCLTECH"], -1)
        self.assertEqual(scan1m._want(side_of, "MARUTI", self.PICK), "BUY")
        self.assertEqual(scan1m._want(side_of, "HCLTECH", self.PICK), "SELL")
        self.assertIn("DUAL", dual)

    def test_without_a_dual_every_name_is_judged_on_the_picks_side(self):
        from nse_scanner import scan1m
        _, side_of, dual = scan1m._scan_targets(self.UNI, {**self.PICK, "alt": "", "alt_side": 0})
        self.assertEqual(set(side_of.values()), {-1})
        self.assertEqual(dual, "")

    def test_the_ledger_carries_the_dual_pick(self):
        from nse_scanner import scan1m
        scan = {"bar": "2026-11-19 09:45", "sector": "IT", "side": -1, "label": "BEAR",
                "conf": 62, "extra": [], "follow": False, "intra": -0.8,
                "alt": {"sector": "AUTO", "side": 1}, "alt_intra": 1.2,
                "alt_reason": "DUAL — AUTO long 98 against the IT SHORT pick"}
        with patch.object(scan1m, "_five_minute_scan", return_value=scan):
            pick = scan1m._ledger_pick(["2026-11-19 09:45"])
        self.assertEqual((pick["alt"], pick["alt_side"], pick["alt_intra"]),
                         ("AUTO", 1, 1.2))
        self.assertIn("DUAL", pick["alt_reason"])

    def test_a_market_decision_hands_over_its_dual_pick(self):
        from nse_scanner import scan1m
        md = type("MD", (), {
            "bar_time": "2026-11-19 09:45", "pick_sector": "IT", "pick_side": -1,
            "dir_label": "BEAR", "dir_conf": 62, "action": "SCAN SHORT",
            "extra_sectors": (), "pick_reason": "",
            "alt_sector": "AUTO", "alt_side": 1,
            "alt_reason": "DUAL — AUTO long 98 against the IT SHORT pick",
            "sectors": {"IT": {"intra_ret": -0.8}, "AUTO": {"intra_ret": 1.2}}})()
        pick = scan1m._pick_dict(md)
        self.assertEqual((pick["alt"], pick["alt_side"], pick["alt_intra"]),
                         ("AUTO", 1, 1.2))
        self.assertEqual(pick["intra"], -0.8)

    def test_a_wait_pick_still_has_no_dual(self):
        from nse_scanner import scan1m
        md = type("MD", (), {
            "bar_time": "2026-11-19 10:00", "pick_sector": "WAIT", "pick_side": 0,
            "dir_label": "NEUTRAL", "dir_conf": 49, "action": "WAIT",
            "extra_sectors": (), "pick_reason": "",
            "alt_sector": "AUTO", "alt_side": 1, "alt_reason": "DUAL",
            "sectors": {"AUTO": {"intra_ret": 1.2}}})()
        pick = scan1m._pick_dict(md)
        self.assertEqual((pick["alt"], pick["alt_side"]), ("", 0))


class BotThreeWorld(TestCase):
    """The whole 1m pass on the 19 Nov shape: IT short booked, AUTO long too."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from nse_scanner import scan1m
        self.scan1m = scan1m
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        scan1m._five_cache.clear()
        scan1m._pick_cache.clear()
        self.env = patch.dict("os.environ", {
            "DRY_RUN": "0", "SCAN1M_FIRST_MINUTE": "1", "SCAN1M_MAX_STALE_SECONDS": "100",
            "SCAN1M_ALERT_CAP": "0", "SCAN1M_EARLY_PER_DAY": "3",
            "SCAN1M_CANCEL_MESSAGE": "0", "SCAN1M_MARKET_SOURCE": "ledger",
            "TELEGRAM_BOT_TOKEN_3": "", "TELEGRAM_CHAT_ID_3": "",
            "SECTOR_TOP_N": "5"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

        from test_scan1m import BAR, CLOSE, MIN1, five_minute_history, one_minute
        self.BAR, self.CLOSE, self.MIN1 = BAR, CLOSE, MIN1
        self.pick = {"bar": (BAR - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M"),
                     "sector": "IT", "side": -1, "label": "BEAR", "conf": 62,
                     "extra": [], "follow": False, "intra": -0.8,
                     "alt": "AUTO", "alt_side": 1, "alt_intra": 1.2,
                     "alt_reason": "DUAL — AUTO long 98 against the IT SHORT pick"}
        self.universe = {"nifty": "NIFTY", "vix": "VIX", "sectors": {
            "IT": {"index": "IT-I", "stocks": ["AAA"]},
            "AUTO": {"index": "AUTO-I", "stocks": ["EEE"]}}}
        self.five = five_minute_history()
        self.minutes = one_minute(0, 1, 2, 3, 4, 5)
        self.closes = {"AAA": 99.2, "EEE": 103.0}
        self.plan_for = lambda raw, px: type("P", (), {
            "entry": px, "sl": px - 1.0, "risk": 1.0, "ema9": px - 1.0,
            "t1": px + 1.0, "t2": px + 2.0, "t3": px + 3.0,
            "pivot_names": ("R1", "R2", "R3"), "basis": None, "skipped": ()})()
        self.engine = None

        from nse_scanner import state, stock, telegram, pipeline
        self.patches = [
            patch.object(scan1m, "LEDGER", self.dir / "scan1m.json"),
            patch.object(state, "STATE_PATH", self.dir / "scan_state.json"),
            patch.object(scan1m, "now_ist", side_effect=lambda: self.MIN1),
            patch.object(scan1m, "pick_for",
                         side_effect=lambda bar, projection, force_sector=None, now=None:
                         self.pick),
            patch.object(scan1m.data, "load_universe", return_value=self.universe),
            patch.object(scan1m.data, "yahoo_stock", side_effect=lambda raw: raw + ".NS"),
            patch.object(scan1m.data, "fetch_many", side_effect=self.fetch),
            patch.object(scan1m.data, "fetch_daily", return_value={}),
            patch.object(scan1m, "scan_stock", side_effect=self.scan),
            patch.object(scan1m, "build_plan",
                         side_effect=lambda sig, frame, bar, daily:
                         self.plan_for(sig.symbol, sig.price)),
            patch.object(scan1m, "_oi_reader", return_value=None),
            patch.object(scan1m.telegram, "send_bot3", return_value=True),
            patch.object(telegram, "send"),
            patch.object(stock, "now_ist", side_effect=lambda: self.MIN1),
            patch.object(pipeline, "now_ist", side_effect=lambda: self.MIN1),
        ]
        self.mocks = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)
        self.bot3 = self.mocks[11]

    def fetch(self, tickers, period, interval):
        if interval != "5m":
            return {t: self.minutes for t in tickers}
        out = {}
        for t in tickers:
            raw = t[:-3]
            c = self.closes.get(raw, 100.0)
            idx = self.five.index
            out[t] = pd.DataFrame({"Open": [100.0] * len(idx),
                                   "High": [max(100.0, c)] * len(idx),
                                   "Low": [min(100.0, c)] * len(idx),
                                   "Close": [c] * len(idx),
                                   "Volume": [100000.0] * len(idx)}, index=idx)
        return out

    def scan(self, raw, frame, daily, keep_all=False):
        if frame.index[-1] != self.BAR:
            return []
        side = "SELL" if raw == "AAA" else "BUY"          # IT falls, AUTO rises
        px = self.closes[raw]
        from test_live_alerts import StockSignal
        return [StockSignal(raw, 101, side + "-EX", side, self.BAR.strftime("%H:%M"),
                            px, 70, "Strong", 20, 1, "test", px - 1.0)]

    def ledger(self):
        from nse_scanner import state
        return state.load(self.dir / "scan1m.json")

class BotThreeDualOnACandleTests(BotThreeWorld):
    """IT short and AUTO long, booked side by side on the 1m close pass."""

    def test_each_book_is_confirmed_on_its_own_side(self):
        out = self.scan1m.confirm_pass(self.BAR, now=self.CLOSE)
        sides = {s["symbol"]: s["side"] for s in out}
        self.assertEqual(sides, {"AAA": "SELL", "EEE": "BUY"})
        self.assertEqual(self.ledger()["pick_entries"],
                         {"IT short": 1, "AUTO long": 1})

    def test_the_dual_entry_says_what_it_is_going_against(self):
        self.scan1m.confirm_pass(self.BAR, now=self.CLOSE)
        texts = [c.args[0] for c in self.bot3.call_args_list]
        dual = [t for t in texts if "DUAL — AUTO long 98" in t]
        self.assertEqual(len(dual), 1)          # one dual name in this world
        self.assertIn("EEE", dual[0])

    def test_the_dual_is_projected_early_as_a_buy(self):
        self.scan1m.run_minute(self.BAR, now=self.MIN1)
        texts = [c.args[0] for c in self.bot3.call_args_list]
        early = [t for t in texts if "EARLY BUY" in t]
        self.assertTrue(early, texts)
        self.assertIn("DUAL — AUTO long 98", early[0])
        self.assertIn("EEE", early[0])

    def test_the_pick_side_is_unaffected(self):
        self.scan1m.run_minute(self.BAR, now=self.MIN1)
        texts = [c.args[0] for c in self.bot3.call_args_list]
        sell = [t for t in texts if "EARLY SELL" in t]
        self.assertTrue(sell, texts)
        self.assertNotIn("DUAL", sell[0])
        self.assertIn("AAA", sell[0])

    def test_the_knob_leaves_the_1m_scan_one_sided(self):
        self.pick = {k: v for k, v in self.pick.items() if not k.startswith("alt")}
        out = self.scan1m.confirm_pass(self.BAR, now=self.CLOSE)
        self.assertEqual([s["symbol"] for s in out], ["AAA"])


class BotThreeFollowerTrimTests(BotThreeWorld):
    """Followers are not projected, confirmed or alerted on the 1m pass either."""

    def setUp(self):
        super().setUp()
        # AAA is the IT pick; AUTO's three movers at 104 / 101 / 100 close.
        self.universe["sectors"]["AUTO"]["stocks"] = ["BBB", "CCC", "EEE"]
        self.closes = {"AAA": 99.2, "BBB": 101.0, "CCC": 100.0, "EEE": 104.0}
        self.pick = {**self.pick, "alt_intra": 1.0}

    def test_only_the_top_names_of_the_sector_are_judged(self):
        with patch.dict("os.environ", {"SECTOR_TOP_N": "2", "MAX_ENTRIES_PER_PICK": "9"}):
            out = self.scan1m.confirm_pass(self.BAR, now=self.CLOSE)
        # EEE leads AUTO, BBB follows it, CCC is the tail the trim drops.
        self.assertEqual(sorted(s["symbol"] for s in out), ["AAA", "BBB", "EEE"])
        rejected = self.ledger()["stats"]["rejected"]["follower"]
        self.assertIn("CCC", ",".join(rejected))

    def test_a_follower_is_projected_nowhere(self):
        with patch.dict("os.environ", {"SECTOR_TOP_N": "2", "SCAN1M_ALERT_CAP": "0"}):
            self.scan1m.run_minute(self.BAR, now=self.MIN1)
        texts = [c.args[0] for c in self.bot3.call_args_list]
        self.assertTrue(texts)
        self.assertFalse(any("CCC" in t for t in texts), texts)

    def test_zero_scans_the_whole_sector(self):
        with patch.dict("os.environ", {"SECTOR_TOP_N": "0", "MAX_ENTRIES_PER_PICK": "9"}):
            out = self.scan1m.confirm_pass(self.BAR, now=self.CLOSE)
        self.assertEqual(sorted(s["symbol"] for s in out), ["AAA", "BBB", "CCC", "EEE"])


# ── the 5m close scan trims the sector the same way ─────────────────────────
class FiveMinuteTrimTests(TestCase):
    """A picked sector alerts its leaders, not its tail."""

    market_at_bar = __import__("test_live_alerts").LiveAlertTests.market_at_bar

    def setUp(self):
        from test_live_alerts import LiveAlertTests
        LiveAlertTests.setUp(self)
        self.uni["sectors"]["IT"]["stocks"] = ["ABC", "QQQ", "XYZ"]
        self.scan.side_effect = self.any_name

    def any_name(self, raw, frame, daily, keep_all=False):
        from test_live_alerts import StockSignal
        return [StockSignal(raw, 101, "BUY-EX", "BUY", "10:00", 100.0, 65,
                            "Strong", 20, 1, "test", 99.0)]

    def test_only_the_top_name_is_alerted_and_the_rest_are_counted(self):
        from nse_scanner import state
        with patch.dict("os.environ", {"SECTOR_TOP_N": "1", "MAX_ENTRIES_PER_PICK": "9"}):
            pipeline.run_once()
        self.assertEqual(sorted(state.load()["sent"]), ["ABC"])
        followers = state.load()["stats"]["rejected"]["follower"]
        self.assertEqual(sorted(followers), ["QQQ", "XYZ"])

    def test_zero_restores_the_whole_sector(self):
        from nse_scanner import state
        with patch.dict("os.environ", {"SECTOR_TOP_N": "0", "MAX_ENTRIES_PER_PICK": "9"}):
            pipeline.run_once()
        self.assertEqual(sorted(state.load()["sent"]), ["ABC", "QQQ", "XYZ"])


class FiveMinuteDualEntryTests(TestCase):
    """The 5m close scan books the dual sector on its own side, its own cap."""

    market_at_bar = __import__("test_live_alerts").LiveAlertTests.market_at_bar

    def setUp(self):
        from test_live_alerts import LiveAlertTests
        LiveAlertTests.setUp(self)
        self.uni["sectors"]["IT"]["stocks"] = ["ABC"]
        self.uni["sectors"]["AUTO"] = {"index": "AUTO-I", "stocks": ["EEE"]}
        self.md.pick_side = self.md.dir_state = -1
        self.md.dir_label = "BEAR"
        self.md.pick_reason = "test"
        self.md.alt_sector = "AUTO"
        self.md.alt_side = 1
        self.md.alt_reason = "DUAL — AUTO long 98 against the IT SHORT pick"

    def test_the_dual_name_is_taken_long_while_the_pick_is_short(self):
        from nse_scanner import state
        pipeline.run_once()
        st = state.load()
        self.assertEqual(sorted(st["sent"]), ["EEE"])
        self.assertEqual(st["pick_entries"], {"AUTO long": 1})
        self.assertEqual(st["positions"]["EEE"]["side"], "BUY")

    def test_a_pick_without_a_dual_scans_one_side(self):
        """``DUAL_PICK=0`` (or no opposite leader) leaves ``md.alt_sector`` empty.

        The close scan takes the dual from the decision, so this is the state the
        knob produces: the short is the only book, and nothing long is taken.
        """
        from nse_scanner import state
        self.md.alt_sector, self.md.alt_side, self.md.alt_reason = "", 0, ""
        pipeline.run_once()
        self.assertEqual(state.load()["sent"], {})
