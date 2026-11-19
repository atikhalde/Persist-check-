"""Regression tests for late/repeated Telegram entries (no network access)."""
from datetime import date, datetime, timedelta
from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import tempfile
from pathlib import Path

import pandas as pd

from nse_scanner import pipeline, state

# The real market formatter, captured before setUp patches it to "market".
REAL_FORMAT_MARKET = pipeline.format_market
from nse_scanner.session import IST
from nse_scanner.stock import StockSignal
from nse_scanner.trade import PivotBasis, TradePlan

ORIGINAL_FORMAT_STOCK = pipeline.format_stock

BAR = datetime(2026, 11, 9, 10, 0, tzinfo=IST)  # open; closes at 10:05


def bars(*times):
    return pd.DataFrame({"Open": [100] * len(times), "High": [101] * len(times),
                         "Low": [99] * len(times), "Close": [100] * len(times),
                         "Volume": [1000] * len(times)}, index=pd.DatetimeIndex(times))


def quiet_bars(*times, close=100.0, high=100.01, low=99.99):
    """5m candles that touch neither the first pivot target nor the initial SL."""
    return pd.DataFrame({"Open": [close] * len(times), "High": [high] * len(times),
                         "Low": [low] * len(times), "Close": [close] * len(times),
                         "Volume": [1000] * len(times)}, index=pd.DatetimeIndex(times))


def daily_bar(day):
    """A previous session with an 8-point range, so pivots sit a % from 100."""
    return pd.DataFrame({"Open": [100.0], "High": [104.0], "Low": [96.0], "Close": [100.0],
                         "Volume": [1000]}, index=pd.DatetimeIndex([day]))


def signal(time="10:00", code=101, side="BUY"):
    return StockSignal("ABC", code, "BUY-EX", side, time, 100.0, 65,
                       "Strong", 20, 1, "test", 99.0)


class EntryCapTests(TestCase):
    """Six names on one pick is one risk factor, not six trades.

    16 Nov: SUNPHARMA, TORNTPHARM, CIPLA, AUROPHARMA, LUPIN and GLENMARK all
    fired on the 09:35 candle — alerted in 35 seconds, in universe order, with no
    ceiling. All six were out within 45 minutes for -1.6R.
    """

    SCORES = {"AAA": 82, "BBB": 62, "CCC": 77, "DDD": 62}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = patch.object(state, "STATE_PATH", Path(self.tmp.name) / "state.json")
        self.ctx.start()
        self.addCleanup(self.ctx.stop)
        self.ctx = patch.dict("os.environ", {"MAX_ALERT_LAG_SECONDS": "180", "DRY_RUN": "0",
                                             "MAX_ENTRIES_PER_PICK": "3"})
        self.ctx.start()
        self.addCleanup(self.ctx.stop)
        self.now = BAR + timedelta(minutes=6)
        self.times = [BAR - timedelta(minutes=5 * i) for i in range(39, 0, -1)] + [BAR]
        self.uni = {"nifty": "NIFTY", "vix": "VIX",
                    "sectors": {"IT": {"index": "INDEX", "stocks": list(self.SCORES)}}}
        self.md = SimpleNamespace(bar_time=BAR.strftime("%Y-%m-%d %H:%M"),
                                  dir_state=1, dir_label="BULL", dir_conf=74,
                                  pick_sector="IT", pick_side=1,
                                  pick_reason="test", action="SCAN LONG", regime="TREND")
        self.contexts = [
            patch.object(pipeline, "now_ist", side_effect=lambda: self.now),
            patch.object(pipeline.data, "load_universe", return_value=self.uni),
            patch.object(pipeline.data, "fetch_symbol", side_effect=lambda *args: {
                "m5": bars(*self.times), "d1": bars(BAR - timedelta(days=1))}),
            patch.object(pipeline.data, "fetch_many", side_effect=lambda tickers, period, interval: {
                t: (quiet_bars(*self.times) if t.endswith(".NS") else bars(*self.times))
                for t in tickers}),
            patch.object(pipeline.data, "apply_1m_volume", side_effect=lambda df, _: df),
            patch.object(pipeline, "run_market", side_effect=self.market_at_bar),
            patch.object(pipeline, "format_market", return_value="market"),
            patch.object(pipeline, "format_stock", side_effect=lambda sig, *a, **k: f"stock {sig.symbol}"),
            patch.object(pipeline, "scan_stock", side_effect=self.scan),
            patch.object(pipeline.telegram, "send", return_value=True),
            patch.object(pipeline, "_unsettled", return_value=(set(), False)),
            # A real daily frame: ``pivot_basis`` needs a *previous* session, and
            # the stock feed above answers interval="1d" with 5m bars from today,
            # which left every fixture plan with T1/T2/T3 N/A. Since
            # SCAN5M_REQUIRE_TARGET those plans are (correctly) refused. Appended
            # last on purpose: the tests index ``self.mocks``.
            patch.object(pipeline.data, "fetch_daily", side_effect=lambda tickers, period: {
                t: daily_bar(BAR - timedelta(days=1)) for t in tickers}),
        ]
        self.mocks = [p.start() for p in self.contexts]
        for p in self.contexts:
            self.addCleanup(p.stop)
        self.send = self.mocks[9]

    def market_at_bar(self, df, *args, **kwargs):
        self.md.bar_time = df.index[-1].strftime("%Y-%m-%d %H:%M")
        return self.md

    def scan(self, symbol, frame, daily, keep_all=False):
        return [StockSignal(symbol, 101, "BUY-EX", "BUY", BAR.strftime("%H:%M"),
                            100.0, self.SCORES[symbol], "Strong", 20, 1, "test", 99.0)]

    def stock_texts(self):
        return [c.args[0] for c in self.send.call_args_list if c.args[0].startswith("stock")]

    def test_one_pick_alerts_only_three_and_best_score_first(self):
        pipeline.run_once()
        self.assertEqual(self.stock_texts(), ["stock AAA", "stock CCC", "stock BBB"])
        self.assertEqual(list(state.load()["sent"]), ["AAA", "CCC", "BBB"])

    def test_the_dropped_name_is_reported_and_still_free(self):
        pipeline.run_once()
        st = state.load()
        self.assertIn("DDD@10:00", st["stats"]["rejected"]["cap"])
        self.assertNotIn("DDD", st["sent"])          # not consumed by the cap
        summary = pipeline.format_summary(st)
        self.assertIn("not taken — the pick already had its entries", summary)

    def test_the_cap_can_be_switched_off(self):
        with patch.dict("os.environ", {"MAX_ENTRIES_PER_PICK": "0"}):
            pipeline.run_once()
        self.assertEqual(len(self.stock_texts()), 4)

    def test_the_cap_is_counted_per_pick_for_the_session(self):
        pipeline.run_once()
        self.assertEqual(state.load()["pick_entries"], {"IT long": 3})


class SectorLeadRankingTests(TestCase):
    """The cap must keep the name that is *moving inside its sector*.

    ``score`` is absolute — its "rs" leg is ``close > previous close``, one bar
    worth 15 of 100 — so ranking by it keeps the most extended chart, which on
    16 Nov was how a pick could alert a follower ahead of the sector's leader.
    """

    SCORES = {"AAA": 82, "BBB": 62, "CCC": 77, "DDD": 62}
    LAST = {"AAA": 100.4, "BBB": 103.0, "CCC": 100.6, "DDD": 100.2}  # +.4 / +3.0 / +.6 / +.2 %
    SECTOR_INTRA = 1.0                                               # index +1.0% from its open

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = patch.object(state, "STATE_PATH", Path(self.tmp.name) / "state.json")
        self.ctx.start()
        self.addCleanup(self.ctx.stop)
        self.ctx = patch.dict("os.environ", {"MAX_ALERT_LAG_SECONDS": "180", "DRY_RUN": "0"})
        self.ctx.start()
        self.addCleanup(self.ctx.stop)
        self.now = BAR + timedelta(minutes=6)
        self.times = [BAR - timedelta(minutes=5 * i) for i in range(39, 0, -1)] + [BAR]
        self.uni = {"nifty": "NIFTY", "vix": "VIX",
                    "sectors": {"IT": {"index": "INDEX", "stocks": list(self.SCORES)}}}
        self.md = SimpleNamespace(bar_time=BAR.strftime("%Y-%m-%d %H:%M"),
                                  dir_state=1, dir_label="BULL", dir_conf=74,
                                  pick_sector="IT", pick_side=1, nifty_intra=0.5,
                                  sectors={"IT": {"intra_ret": self.SECTOR_INTRA, "rs": 0.5}},
                                  pick_reason="test", action="SCAN LONG", regime="TREND")
        self.contexts = [
            patch.object(pipeline, "now_ist", side_effect=lambda: self.now),
            patch.object(pipeline.data, "load_universe", return_value=self.uni),
            patch.object(pipeline.data, "fetch_symbol", side_effect=lambda *args: {
                "m5": bars(*self.times), "d1": bars(BAR - timedelta(days=1))}),
            patch.object(pipeline.data, "fetch_many", side_effect=self.fetch),
            patch.object(pipeline.data, "apply_1m_volume", side_effect=lambda df, _: df),
            patch.object(pipeline, "run_market", side_effect=self.market_at_bar),
            patch.object(pipeline, "format_market", return_value="market"),
            patch.object(pipeline, "format_stock", side_effect=lambda sig, *a, **k: f"stock {sig.symbol}"),
            patch.object(pipeline, "scan_stock", side_effect=self.scan),
            patch.object(pipeline.telegram, "send", return_value=True),
            patch.object(pipeline, "_unsettled", return_value=(set(), False)),
            # A real daily frame: ``pivot_basis`` needs a *previous* session, and
            # the stock feed above answers interval="1d" with 5m bars from today,
            # which left every fixture plan with T1/T2/T3 N/A. Since
            # SCAN5M_REQUIRE_TARGET those plans are (correctly) refused. Appended
            # last on purpose: the tests index ``self.mocks``.
            patch.object(pipeline.data, "fetch_daily", side_effect=lambda tickers, period: {
                t: daily_bar(BAR - timedelta(days=1)) for t in tickers}),
        ]
        self.mocks = [p.start() for p in self.contexts]
        for p in self.contexts:
            self.addCleanup(p.stop)
        self.send = self.mocks[9]

    def fetch(self, tickers, period, interval):
        out = {}
        for t in tickers:
            if t.endswith(".NS"):
                c = self.LAST[t[:-3]]
                out[t] = pd.DataFrame({"Open": [100.0] * len(self.times),
                                       "High": [max(100.0, c)] * len(self.times),
                                       "Low": [min(100.0, c)] * len(self.times),
                                       "Close": [c] * len(self.times),
                                       "Volume": [1000] * len(self.times)},
                                      index=pd.DatetimeIndex(self.times))
            else:
                out[t] = bars(*self.times)
        return out

    def market_at_bar(self, df, *args, **kwargs):
        self.md.bar_time = df.index[-1].strftime("%Y-%m-%d %H:%M")
        return self.md

    def scan(self, symbol, frame, daily, keep_all=False):
        c = self.LAST[symbol]
        return [StockSignal(symbol, 101, "BUY-EX", "BUY", BAR.strftime("%H:%M"),
                            c, self.SCORES[symbol], "Strong", 20, 1, "test", c - 1.0)]

    def stock_texts(self):
        return [c.args[0] for c in self.send.call_args_list if c.args[0].startswith("stock")]

    def test_only_the_sector_s_runners_are_entered(self):
        """The count is open now; the momentum band is what keeps a name out.

        BBB is +2.0pp ahead of its sector, CCC +(-0.4), AAA +(-0.6), DDD +(-0.8):
        the other three are the same move at a worse price, so they are not
        entered however good their absolute score is (AAA 82 is the top score).
        """
        pipeline.run_once()
        self.assertEqual(self.stock_texts(), ["stock BBB"])
        self.assertIn("AAA@10:00", state.load()["stats"]["rejected"]["weak"])

    def test_the_band_can_be_opened(self):
        with patch.dict("os.environ", {"ENTRY_RANK_BAND": "0"}):
            pipeline.run_once()
        self.assertEqual(self.stock_texts(),
                         ["stock BBB", "stock CCC", "stock AAA", "stock DDD"])

    def test_the_old_score_order_can_be_restored(self):
        with patch.dict("os.environ", {"RANK_BY_SECTOR_LEAD": "0", "ENTRY_RANK_BAND": "0"}):
            pipeline.run_once()
        self.assertEqual(self.stock_texts(),
                         ["stock AAA", "stock CCC", "stock BBB", "stock DDD"])

    def test_the_gate_calls_a_name_that_fell_over_reversing(self):
        """A name rolling over is not entered, however good its day has been.

        The frame ran to 103.3 and faded to 101.5: under its EMA9, low in its
        range, the last candles against it — the structure leg sees it even
        before the day's move does.
        """
        n = 20
        closes = ([100.4] * 5 + [101.0, 101.6, 102.2, 102.8, 103.3, 102.6, 102.2, 101.9,
                                 101.8, 101.75, 101.7, 101.65, 101.6, 101.55, 101.5])
        idx = [BAR - timedelta(minutes=5 * i) for i in range(n, 0, -1)]
        frame = pd.DataFrame({"Open": closes, "High": [c + 0.02 for c in closes],
                              "Low": [c - 0.02 for c in closes], "Close": closes,
                              "Volume": [1000] * n}, index=pd.DatetimeIndex(idx))
        early = frame.index[7]
        ok, why, rank = pipeline.momentum_verdict(frame, early, 102.2, True, 0.0)
        self.assertTrue(ok, why)                      # running: up on heavy trend
        ok, why, rank = pipeline.momentum_verdict(frame, frame.index[-1], 101.5, True, 0.0)
        self.assertFalse(ok)
        self.assertIn("reversing", why)

    def test_the_floor_refuses_a_name_with_no_strength_left(self):
        with patch.dict("os.environ", {"ENTRY_MIN_RANK": "95"}):
            ok, why, _ = pipeline.momentum_verdict(
                pd.DataFrame({"Open": [100.0], "High": [100.1], "Low": [99.9],
                              "Close": [100.0], "Volume": [1000]},
                             index=pd.DatetimeIndex([BAR])), BAR, 100.0, True, 0.0)
        self.assertFalse(ok)
        self.assertIn("weak momentum", why)

    def test_no_cap_by_default(self):
        self.assertEqual(pipeline.max_entries_per_pick(), 0)

    def test_the_alert_shows_how_far_ahead_of_its_sector_it_is(self):
        text = ORIGINAL_FORMAT_STOCK(
            signal(), self.md, TradePlan(100.0, 99.5, 99.0, 1.0, 102.0, 104.0, 106.0),
            sector="IT", lead=2.4)
        self.assertIn("Market BULL (74%)  ·  lead +2.4pp vs sector", text)

    def test_no_lead_means_no_claim(self):
        text = ORIGINAL_FORMAT_STOCK(
            signal(), self.md, TradePlan(100.0, 99.5, 99.0, 1.0, 102.0, 104.0, 106.0), sector="IT")
        self.assertNotIn("lead", text)


def market_decision(**over):
    """A complete MarketDecision, for the block formatter (see market.MarketDecision)."""
    base = dict(session="2026-11-16", bar_time="2026-11-16 10:20", nifty=25270.8,
                nifty_day_ret=0.50, nifty_intra=0.26, vix=15.68, vix_chg=-6.74,
                adx=41, mtf_bull=3, dir_state=1, dir_label="BULL", dir_conf=94,
                dir_tier="BUILDING", breadth_adv=7, breadth_dec=8, regime="TRANSITION",
                persist_plus=[("FMCG", 79), ("BANK", 56)], persist_minus=[("REALTY", 91)],
                live_flow="REALTY", live_flow_side=-1, live_flow_score=85,
                live_flow_qualified=True,
                pick_sector="FMCG", pick_side=1, pick_reason="PERSIST + FMCG qualified",
                action="SCAN LONG")
    base.update(over)
    return SimpleNamespace(**base)


class MarketBlockTests(TestCase):
    """`conf 99% -> 67%` in 35 minutes used to be silent: only labels are news."""

    def test_a_slide_is_disclosed_with_what_it_was(self):
        text = REAL_FORMAT_MARKET(market_decision(dir_conf=67), 94)
        self.assertIn("conf 67% BUILDING", text)
        self.assertIn("(was 94%)", text)

    def test_a_small_move_carries_no_note(self):
        text = REAL_FORMAT_MARKET(market_decision(dir_conf=94), 99)
        self.assertIn("conf 94%", text)
        self.assertNotIn("(was", text)

    def test_the_note_can_be_switched_off(self):
        with patch.dict("os.environ", {"MARKET_CONF_DELTA": "0"}):
            text = REAL_FORMAT_MARKET(market_decision(dir_conf=10), 99)
        self.assertNotIn("(was", text)

    def test_the_flow_score_shows_its_side(self):
        # 16 Nov printed "REALTY Q (85)" in a BULL block while REALTY was the
        # top of PERSIST minus: the number read as bullish flow and was not.
        down = REAL_FORMAT_MARKET(market_decision(live_flow="REALTY", live_flow_side=-1))
        up = REAL_FORMAT_MARKET(market_decision(live_flow="PHA", live_flow_side=1))
        self.assertIn("Live flow  REALTY▼ Q (85)", down)
        self.assertIn("Live flow  PHA▲ Q (85)", up)
        self.assertIn("PERSIST −  REALTY 91", down)      # the contradiction, now visible

    def test_a_block_that_trades_nothing_does_not_announce_a_direction(self):
        # 16 Nov 11:55 read "MARKET BULL conf 41% WEAK" over "WAIT — market
        # TRANSITION conf 41": a bullish call the engine then refused to act on.
        text = REAL_FORMAT_MARKET(market_decision(
            dir_conf=41, dir_tier="WEAK", regime="TRANSITION", breadth_adv=5, breadth_dec=10,
            pick_sector="WAIT", pick_side=0, action="WAIT",
            pick_reason="WAIT — market TRANSITION conf 41 < 55"))
        self.assertIn("MARKET STAND ASIDE", text)
        self.assertIn("BULL lean", text)                 # the lean is still shown
        self.assertNotIn("<b>MARKET BULL", text)
        # what it is waiting for — escaped, or Telegram refuses the whole block
        # ("can't parse entities: Unsupported start tag", 18 Nov, nine messages)
        self.assertIn("conf 41 &lt; 55", text)
        self.assertNotIn("conf 41 < 55", text)

    def test_the_day_s_movers_are_named_when_nothing_is_traded(self):
        text = REAL_FORMAT_MARKET(market_decision(
            pick_sector="WAIT", pick_side=0, action="WAIT",
            persist_plus=[("FMCG", 93), ("BANK", 44)],
            persist_minus=[("REALTY", 91), ("AUTO", 86)]))
        self.assertIn("No pick · persistent movers  FMCG long 93 · REALTY short 91", text)

    def test_a_traded_block_keeps_its_label_and_has_no_extra_line(self):
        text = REAL_FORMAT_MARKET(market_decision())
        self.assertIn("<b>MARKET BULL</b>  conf 94%", text)
        self.assertNotIn("STAND ASIDE", text)
        self.assertNotIn("persistent movers", text)

    def test_a_neutral_stand_aside_has_no_lean(self):
        text = REAL_FORMAT_MARKET(market_decision(
            dir_label="NEUTRAL", dir_state=0, dir_conf=41, dir_tier="WEAK",
            pick_sector="WAIT", pick_side=0, action="WAIT"))
        self.assertIn("MARKET STAND ASIDE</b>  conf 41% WEAK", text)
        self.assertNotIn("lean", text.splitlines()[0])

    def test_a_stale_index_is_named_on_the_block(self):
        text = REAL_FORMAT_MARKET(market_decision(stale_sectors=[("FMCG", 6)]))
        self.assertIn("⚠ stale index  FMCG 6 bars behind", text)

    def test_a_fresh_scan_has_no_stale_line(self):
        self.assertNotIn("stale index", REAL_FORMAT_MARKET(market_decision()))


class LiveAlertTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = patch.object(state, "STATE_PATH", Path(self.tmp.name) / "state.json")
        self.ctx.start()
        self.addCleanup(self.ctx.stop)
        self.ctx = patch.dict("os.environ", {"MAX_ALERT_LAG_SECONDS": "180", "DRY_RUN": "0"})
        self.ctx.start()
        self.addCleanup(self.ctx.stop)
        self.now = BAR + timedelta(minutes=6)
        self.times = [BAR - timedelta(minutes=5 * i) for i in range(39, 0, -1)] + [BAR, BAR + timedelta(minutes=5)]
        self.uni = {"nifty": "NIFTY", "vix": "VIX",
                    "sectors": {"IT": {"index": "INDEX", "stocks": ["ABC"]}}}
        self.md = SimpleNamespace(bar_time=BAR.strftime("%Y-%m-%d %H:%M"),
                                  dir_state=1, dir_label="BULL", dir_conf=74,
                                  pick_sector="IT", pick_side=1,
                                  pick_reason="test", action="SCAN LONG", regime="TREND")
        self.contexts = [
            patch.object(pipeline, "now_ist", side_effect=lambda: self.now),
            patch.object(pipeline.data, "load_universe", return_value=self.uni),
            patch.object(pipeline.data, "fetch_symbol", side_effect=lambda *args: {
                "m5": bars(*self.times), "d1": bars(BAR - timedelta(days=1))}),
            patch.object(pipeline.data, "fetch_many", side_effect=lambda tickers, period, interval: {
                t: (quiet_bars(*self.times) if t.endswith(".NS") else bars(*self.times))
                for t in tickers}),
            patch.object(pipeline.data, "apply_1m_volume", side_effect=lambda df, _: df),
            patch.object(pipeline, "run_market", side_effect=self.market_at_bar),
            patch.object(pipeline, "format_market", return_value="market"),
            patch.object(pipeline, "format_stock", return_value="stock"),
            patch.object(pipeline, "scan_stock", return_value=[signal()]),
            patch.object(pipeline.telegram, "send", return_value=True),
            # These tests feed the same frames for every interval, so there is no
            # realistic 1m stream: treat the feed as settled (see test_early_alerts
            # for the half-published-candle rules themselves).
            patch.object(pipeline, "_unsettled", return_value=(set(), False)),
            # A real daily frame: ``pivot_basis`` needs a *previous* session, and
            # the stock feed above answers interval="1d" with 5m bars from today,
            # which left every fixture plan with T1/T2/T3 N/A. Since
            # SCAN5M_REQUIRE_TARGET those plans are (correctly) refused. Appended
            # last on purpose: the tests index ``self.mocks``.
            patch.object(pipeline.data, "fetch_daily", side_effect=lambda tickers, period: {
                t: daily_bar(BAR - timedelta(days=1)) for t in tickers}),
        ]
        self.mocks = [p.start() for p in self.contexts]
        for p in self.contexts:
            self.addCleanup(p.stop)
        self.fetch_many = self.mocks[3]
        self.scan = self.mocks[8]
        self.send = self.mocks[9]
        self.settled = self.contexts[10]

    def market_at_bar(self, df, *args, **kwargs):
        self.md.bar_time = df.index[-1].strftime("%Y-%m-%d %H:%M")
        return self.md

    def test_message_distinguishes_bar_open_close_and_calculation_time(self):
        plan = TradePlan(100.0, 100.0, 99.98, 0.02, 100.02, 100.04, 100.06, ("R1", "R2", "R3"))
        text = ORIGINAL_FORMAT_STOCK(signal(), self.md, plan)
        self.assertIn("Entry (candle close) <b>100.00</b>", text)
        self.assertIn("\nSL 99.98 (0.02% below EMA9)\n", text)
        self.assertNotIn("Entry EMA9", text)
        self.assertIn("<b>BUY</b> | <b>BUY-EX</b>", text)
        self.assertNotIn("Targets are reference levels", text)
        self.assertNotIn("Trail through pivot targets", text)
        self.assertIn("next pivot target: T1 R1 100.02 (1.0R, +0.02%) · T2 R2 100.04 (2.0R, +0.04%) · "
                      "T3 R3 100.06 (3.0R, +0.06%)", text)
        self.assertIn("Pivot basis N/A (no completed previous session)", text)
        self.assertIn("5m bar 10:00–10:05 IST", text)
        self.assertIn("Signal calculated 10:06:00 IST", text)

    def test_only_closed_candle_used_and_only_once_per_day(self):
        first = pipeline.run_once()
        self.assertEqual(len(first["signals"]), 1)
        self.assertEqual(self.scan.call_args.args[1].index[-1], BAR)
        self.assertTrue(self.scan.call_args.kwargs["keep_all"])
        self.assertEqual([call.args[0] for call in self.send.call_args_list], ["market", "stock"])
        second = pipeline.run_once()
        self.assertEqual(second["signals"], [])
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(state.load()["sent"]["ABC"]["time"], "10:00")

    def test_rank_upgrade_is_not_a_second_entry(self):
        pipeline.run_once()
        self.scan.return_value = [signal(code=120)]
        self.assertEqual(pipeline.run_once()["signals"], [])
        self.assertEqual(self.send.call_count, 2)

    def test_historical_table_row_is_never_sent(self):
        self.scan.return_value = [signal("09:50")]
        self.assertEqual(pipeline.run_once()["signals"], [])
        self.assertEqual([call.args[0] for call in self.send.call_args_list], ["market"])

    def test_mixed_old_and_current_fires_only_send_current(self):
        # Nov 6's KALYANKJIL @09:20 and TITAN @09:30 were both sent at
        # 09:35 in the same run. Only the current bar's fire is an entry.
        self.scan.return_value = [signal("09:20", code=90), signal("10:00", code=101)]
        self.assertEqual([s["time"] for s in pipeline.run_once()["signals"]], ["10:00"])
        self.assertEqual(self.send.call_count, 2)

    def test_backfill_is_diagnostic_only(self):
        with self.assertRaisesRegex(ValueError, "--dry-run"):
            pipeline.run_once(backfill=True)
        self.send.assert_not_called()
        with patch.dict("os.environ", {"BACKFILL": "1"}):
            with self.assertRaisesRegex(ValueError, "--dry-run"):
                pipeline.run_once()
        self.send.assert_not_called()

    def test_corrupt_state_fails_closed_instead_of_repeating(self):
        state.STATE_PATH.write_text('{"session": "2026-11-09", "sent": ', encoding="utf-8")
        with self.assertRaisesRegex(state.InvalidStateError, "Invalid scanner state"):
            pipeline.run_once()
        self.send.assert_not_called()
        with patch.object(pipeline.time, "sleep") as sleep:
            with self.assertRaises(state.InvalidStateError):
                pipeline.run_until_ready(expected_bar=BAR)
        sleep.assert_not_called()

    def test_late_run_does_not_download_or_send(self):
        self.now = BAR + timedelta(minutes=9)
        result = pipeline.run_once()
        self.assertEqual(result["market"], "STALE")
        self.fetch_many.assert_not_called()
        self.send.assert_not_called()

    def test_current_bar_unpublished_is_not_processed(self):
        self.now = BAR + timedelta(minutes=1)
        result = pipeline.run_once()
        self.assertEqual(result["bar"], "2026-11-09 09:55")
        self.assertEqual(self.scan.call_args.args[1].index[-1], self.times[-3])
        self.assertNotIn("stock", [c.args[0] for c in self.send.call_args_list])

    def test_stock_bar_missing_is_not_replayed(self):
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: bars(*(self.times[:-2] if t == "ABC.NS" else self.times)) for t in tickers}
        result = pipeline.run_once()
        self.assertEqual(result["signals"], [])
        self.assertEqual(result["action"], "PENDING")
        self.scan.assert_not_called()

    def test_failed_send_does_not_mark_stock_sent(self):
        self.send.side_effect = [True, False, True]
        self.assertEqual(pipeline.run_once()["signals"], [])
        self.assertEqual(pipeline.run_once()["signals"][0]["symbol"], "ABC")
        self.assertEqual(self.send.call_count, 3)

    def test_dry_run_does_not_consume_real_state(self):
        with patch.dict("os.environ", {"DRY_RUN": "1"}):
            pipeline.run_once()
        self.assertEqual(state.load(), {})

    def test_expired_during_fetch_does_not_send(self):
        def slow_fetch(tickers, period, interval):
            self.now = BAR + timedelta(minutes=9)
            return {t: bars(*self.times) for t in tickers}
        self.fetch_many.side_effect = slow_fetch
        self.assertEqual(pipeline.run_once()["signals"], [])
        self.send.assert_not_called()

    def test_trailing_exit_on_close_even_if_sector_becomes_wait(self):
        entry = pipeline.run_once()
        self.assertEqual(entry["signals"][0]["price"], 100.0)
        self.assertEqual(entry["signals"][0]["sl"], 99.98)
        self.assertIn("ABC", state.load()["positions"])

        self.now = BAR + timedelta(minutes=11)
        self.md.pick_sector, self.md.action, self.md.pick_side = "WAIT", "WAIT", 0
        trailing = quiet_bars(*self.times)
        trailing.loc[self.times[-1], "Close"] = 99.0
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: trailing.copy() if t == "ABC.NS" else bars(*self.times) for t in tickers}
        result = pipeline.run_once()
        self.assertEqual(result["action"], "WAIT")
        exits = [c.args[0] for c in self.send.call_args_list if "EMA9 EXIT" in c.args[0]]
        self.assertEqual(len(exits), 1)
        self.assertIn("close 99.00", exits[0])
        self.assertIn("5m 10:05", exits[0])
        self.assertNotIn("ABC", state.load()["positions"])
        pipeline.run_once()  # same bar must not send the exit again
        self.assertEqual(sum("EMA9 EXIT" in c.args[0] for c in self.send.call_args_list), 1)

    def test_trail_exit_works_when_nifty_feed_is_stale(self):
        pipeline.run_once()
        self.now = BAR + timedelta(minutes=11)
        trailing = quiet_bars(*self.times)
        trailing.loc[self.times[-1], "Close"] = 99.0
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: trailing.copy() if t == "ABC.NS" else bars(*self.times) for t in tickers}
        self.mocks[2].side_effect = lambda *args: {
            "m5": bars(*self.times[:-1]), "d1": bars(BAR - timedelta(days=1))}
        result = pipeline.run_once()
        self.assertEqual(result["market"], "STALE")
        self.assertNotIn("ABC", state.load()["positions"])
        self.assertEqual(sum("EMA9 EXIT" in c.args[0] for c in self.send.call_args_list), 1)

    def test_trail_exit_survives_nifty_download_failure(self):
        pipeline.run_once()
        self.now = BAR + timedelta(minutes=11)
        trailing = quiet_bars(*self.times)
        trailing.loc[self.times[-1], "Close"] = 99.0
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: trailing.copy() if t == "ABC.NS" else bars(*self.times) for t in tickers}
        self.mocks[2].side_effect = lambda *args: {"m5": bars(), "d1": bars(BAR - timedelta(days=1))}
        with self.assertRaisesRegex(RuntimeError, "No Nifty"):
            pipeline.run_once()
        self.assertNotIn("ABC", state.load()["positions"])
        self.assertEqual(sum("EMA9 EXIT" in c.args[0] for c in self.send.call_args_list), 1)

    def test_sell_mirrors_stop_targets_and_ema_exit(self):
        self.md.pick_side, self.md.dir_state, self.md.dir_label = -1, -1, "BEAR"
        self.scan.return_value = [signal(side="SELL", code=201)]
        entry = pipeline.run_once()["signals"][0]
        # A real daily frame is now in the fixture (SCAN5M_REQUIRE_TARGET needs a
        # pivot basis to exist for the entry to be allowed at all), so the short
        # has its three levels instead of three N/As.
        self.assertEqual((entry["price"], entry["sl"], entry["t1"], entry["t2"], entry["t3"]),
                         (100.0, 100.02, 96.0, 92.0, 88.0))
        self.now = BAR + timedelta(minutes=11)
        self.md.pick_sector, self.md.action, self.md.pick_side = "WAIT", "WAIT", 0
        trailing = quiet_bars(*self.times, high=100.0, low=99.5)
        trailing.loc[self.times[-1], "Close"] = 101.0
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: trailing.copy() if t == "ABC.NS" else bars(*self.times) for t in tickers}
        pipeline.run_once()
        self.assertNotIn("ABC", state.load()["positions"])
        self.assertTrue(any("above EMA9" in c.args[0] and "EMA9 EXIT" in c.args[0] for c in self.send.call_args_list))

    def test_passing_all_pivot_targets_does_not_exit_before_ema_close(self):
        pipeline.run_once()
        st = state.load()
        st["positions"]["ABC"].update(t1=100.1, t2=100.2, t3=100.3,
                                      t_names=[None, None, None])
        state.save(st)
        self.now = BAR + timedelta(minutes=11)
        trailing = quiet_bars(*self.times)
        trailing.loc[self.times[-1], ["High", "Close"]] = [110.0, 110.0]
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: trailing.copy() if t == "ABC.NS" else bars(*self.times) for t in tickers}
        pipeline.run_once()
        self.assertIn("ABC", state.load()["positions"])
        self.assertEqual(state.load()["positions"]["ABC"]["hit_targets"], [1, 2, 3])
        hits = [c.args[0] for c in self.send.call_args_list if "TARGET HIT" in c.args[0]]
        self.assertEqual(len(hits), 1)                     # one line, not three
        for level in ("T1 100.10", "T2 100.20", "T3 100.30"):
            self.assertIn(level, hits[0])
        self.assertFalse(any("EMA9 EXIT" in c.args[0] for c in self.send.call_args_list))

    def test_trail_ignores_wick_and_waits_for_close(self):
        pipeline.run_once()
        self.now = BAR + timedelta(minutes=11)
        self.md.pick_sector, self.md.action, self.md.pick_side = "WAIT", "WAIT", 0
        st = state.load()
        st["positions"]["ABC"]["sl"] = 85.0          # keep the wick clear of the stop
        state.save(st)
        trailing = quiet_bars(*self.times)
        trailing.loc[self.times[-1], ["Low", "Close"]] = [90.0, 100.5]
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: trailing.copy() if t == "ABC.NS" else bars(*self.times) for t in tickers}
        pipeline.run_once()
        self.assertIn("ABC", state.load()["positions"])
        self.assertEqual(state.load()["positions"]["ABC"]["last_bar"],
                         self.times[-1].isoformat())
        self.assertFalse(any("EMA9 EXIT" in c.args[0] for c in self.send.call_args_list))

    def test_failed_exit_remains_open_for_retry(self):
        pipeline.run_once()
        self.now = BAR + timedelta(minutes=11)
        self.md.pick_sector, self.md.action, self.md.pick_side = "WAIT", "WAIT", 0
        trailing = quiet_bars(*self.times)
        trailing.loc[self.times[-1], "Close"] = 99.0
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: trailing.copy() if t == "ABC.NS" else bars(*self.times) for t in tickers}
        self.send.side_effect = [False, True, True]  # exit fails; market succeeds; exit retries
        self.assertIn("ABC", state.load()["positions"])
        pipeline.run_once()
        self.assertIn("ABC", state.load()["positions"])
        pipeline.run_once()
        self.assertNotIn("ABC", state.load()["positions"])

    # --- one-line hit alerts -------------------------------------------------
    def _trade(self, *, t1=101.0, t2=102.0, t3=103.0, names=("R1", "R2", "R3")):
        pipeline.run_once()
        st = state.load()
        st["positions"]["ABC"].update(t1=t1, t2=t2, t3=t3, t_names=list(names))
        state.save(st)
        self.now = BAR + timedelta(minutes=11)
        self.md.pick_sector, self.md.action, self.md.pick_side = "WAIT", "WAIT", 0

    def _frames(self, stock):
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: stock.copy() if t == "ABC.NS" else bars(*self.times) for t in tickers}

    def test_target_hit_alert_is_one_line_and_keeps_the_trade_open(self):
        self._trade()
        trailing = quiet_bars(*self.times)
        trailing.loc[self.times[-1], "High"] = 101.0        # touches T1 only
        self._frames(trailing)
        pipeline.run_once()
        hits = [c.args[0] for c in self.send.call_args_list if "TARGET HIT" in c.args[0]]
        self.assertEqual(len(hits), 1)
        self.assertNotIn("\n", hits[0])                     # one line
        self.assertIn("ABC", hits[0])
        self.assertIn("T1 R1 101.00 (+1.00%, +50.0R)", hits[0])   # 0.02 stop in this fixture
        self.assertIn("5m 10:05", hits[0])
        self.assertIn("entry 100.00", hits[0])
        self.assertEqual(state.load()["positions"]["ABC"]["hit_targets"], [1])
        self.assertIn("ABC", state.load()["positions"])
        pipeline.run_once()                                  # same bar, no repeat
        self.assertEqual(sum("TARGET HIT" in c.args[0] for c in self.send.call_args_list), 1)

    def test_sl_hit_alert_is_one_line_and_closes_the_trade(self):
        self._trade()
        trailing = quiet_bars(*self.times)
        trailing.loc[self.times[-1], "Low"] = 99.90          # through SL 99.98
        self._frames(trailing)
        pipeline.run_once()
        sl = [c.args[0] for c in self.send.call_args_list if "SL HIT" in c.args[0]]
        self.assertEqual(len(sl), 1)
        self.assertNotIn("\n", sl[0])
        self.assertIn("SL 99.98 (-0.02%, -1.0R)", sl[0])
        self.assertIn("5m 10:05", sl[0])
        self.assertNotIn("ABC", state.load()["positions"])
        self.assertIn("ABC", state.load()["sent"])           # same-day re-entry stays blocked
        pipeline.run_once()                                  # closed: no repeat
        self.assertEqual(sum("SL HIT" in c.args[0] for c in self.send.call_args_list), 1)
        self.assertEqual(pipeline.run_once()["signals"], [])  # no re-entry same day

    def test_ema9_exit_is_one_line(self):
        self._trade()
        trailing = quiet_bars(*self.times)
        trailing.loc[self.times[-1], "Close"] = 99.0
        self._frames(trailing)
        pipeline.run_once()
        exits = [c.args[0] for c in self.send.call_args_list if "EMA9 EXIT" in c.args[0]]
        self.assertEqual(len(exits), 1)
        self.assertNotIn("\n", exits[0])
        self.assertIn("close 99.00 below EMA9", exits[0])
        self.assertIn("5m 10:05", exits[0])

    def test_hits_are_replayed_bar_by_bar_after_a_missed_cycle(self):
        # Positions are tracked from the last processed candle, so a late or
        # skipped run still reports the hit on the candle where it happened.
        self._trade()
        self.now = BAR + timedelta(minutes=16)               # closed bar 10:10
        trailing = quiet_bars(*self.times, BAR + timedelta(minutes=10))
        trailing.loc[self.times[-1], "High"] = 101.0         # T1 on 10:05
        trailing.loc[BAR + timedelta(minutes=10), "Low"] = 99.90   # SL on 10:10
        self._frames(trailing)
        pipeline.run_once()
        hits = [c.args[0] for c in self.send.call_args_list if "TARGET HIT" in c.args[0]]
        sl = [c.args[0] for c in self.send.call_args_list if "SL HIT" in c.args[0]]
        self.assertEqual(len(hits), 1)
        self.assertIn("5m 10:05", hits[0])                   # not the run time
        self.assertEqual(len(sl), 1)
        self.assertIn("5m 10:10", sl[0])
        self.assertNotIn("ABC", state.load()["positions"])

    def test_entry_bar_never_produces_hit_alerts(self):
        pipeline.run_once()
        self.md.pick_sector, self.md.action, self.md.pick_side = "WAIT", "WAIT", 0
        self.now = BAR + timedelta(minutes=7)                # still the entry bar
        trailing = quiet_bars(*self.times)
        # the ENTRY candle itself would breach the stop and every target
        trailing.loc[self.times[-2], ["Low", "High"]] = [90.0, 120.0]
        self._frames(trailing)
        pipeline.run_once()
        self.assertFalse(any("HIT" in c.args[0] for c in self.send.call_args_list))
        self.assertIn("ABC", state.load()["positions"])

    # --- never twice (same guarantee as the entry alert) ---------------------
    def test_target_alert_is_not_repeated_when_the_position_record_is_rebuilt(self):
        self._trade()
        # Above entry throughout: a dip back to entry would now take the trade
        # out at the breakeven stop (test_breakeven_after_t1 pins that).
        trailing = quiet_bars(*self.times, low=100.005)
        trailing.loc[self.times[-1], "High"] = 101.0
        self._frames(trailing)
        pipeline.run_once()
        st = state.load()
        self.assertEqual(list(st["alerts"]), ["ABC:2026-11-09T10:00:00+05:30:T1"])
        # a rebuilt/older position record forwards the cursor and forgets the hit
        st["positions"]["ABC"].pop("hit_targets", None)
        st["positions"]["ABC"]["last_bar"] = st["positions"]["ABC"]["entry_bar"]
        state.save(st)
        pipeline.run_once()
        pipeline.run_once()
        self.assertEqual(sum("TARGET HIT" in c.args[0] for c in self.send.call_args_list), 1)
        self.assertIn("ABC", state.load()["positions"])

    def test_sl_alert_is_not_repeated_when_the_position_is_reinserted(self):
        self._trade()
        before = state.load()["positions"]["ABC"]
        trailing = quiet_bars(*self.times)
        trailing.loc[self.times[-1], "Low"] = 99.90
        self._frames(trailing)
        pipeline.run_once()
        self.assertEqual(sum("SL HIT" in c.args[0] for c in self.send.call_args_list), 1)
        st = state.load()
        st["positions"]["ABC"] = dict(before)          # stale record reappears
        state.save(st)
        pipeline.run_once()
        self.assertEqual(sum("SL HIT" in c.args[0] for c in self.send.call_args_list), 1)
        self.assertNotIn("ABC", state.load()["positions"])

    def test_ema9_exit_is_not_repeated_when_the_position_is_reinserted(self):
        self._trade()
        before = state.load()["positions"]["ABC"]
        trailing = quiet_bars(*self.times)
        trailing.loc[self.times[-1], "Close"] = 99.0
        self._frames(trailing)
        pipeline.run_once()
        self.assertEqual(sum("EMA9 EXIT" in c.args[0] for c in self.send.call_args_list), 1)
        st = state.load()
        st["positions"]["ABC"] = dict(before)
        state.save(st)
        pipeline.run_once()
        self.assertEqual(sum("EMA9 EXIT" in c.args[0] for c in self.send.call_args_list), 1)
        self.assertNotIn("ABC", state.load()["positions"])

    def test_the_same_level_is_never_announced_twice_across_many_cycles(self):
        self._trade()
        trailing = quiet_bars(*self.times)
        trailing.loc[self.times[-1], "High"] = 101.0
        self._frames(trailing)
        for _ in range(5):
            pipeline.run_once()
        self.assertEqual(sum("TARGET HIT" in c.args[0] for c in self.send.call_args_list), 1)
        self.assertEqual(len(state.load()["alerts"]), 1)

    def test_a_new_trade_on_the_same_symbol_gets_its_own_alerts(self):
        self._trade()
        trailing = quiet_bars(*self.times)
        trailing.loc[self.times[-1], "High"] = 101.0
        self._frames(trailing)
        pipeline.run_once()
        st = state.load()
        # a fresh trade (different entry candle) must not inherit the old dedupe
        st["positions"]["ABC"].update(entry_bar="2026-11-09T09:55:00+05:30",
                                      last_bar="2026-11-09T10:00:00+05:30",
                                      hit_targets=[])
        state.save(st)
        self.now = BAR + timedelta(minutes=11)
        pipeline.run_once()
        self.assertEqual(sum("TARGET HIT" in c.args[0] for c in self.send.call_args_list), 2)

    def test_entry_alert_is_still_sent_exactly_once_per_session(self):
        self._trade()
        trailing = quiet_bars(*self.times)
        trailing.loc[self.times[-1], "High"] = 101.0
        self._frames(trailing)
        for _ in range(3):
            pipeline.run_once()
        entries = [c.args[0] for c in self.send.call_args_list
                   if "TARGET HIT" not in c.args[0] and "SL HIT" not in c.args[0]
                   and "EMA9 EXIT" not in c.args[0] and c.args[0] != "market"]
        self.assertEqual(len(entries), 1)               # the untouched entry alert
        self.assertEqual(state.load()["sent"]["ABC"]["code"], 101)

    def test_new_ist_session_resets_dedupe(self):
        previous = state.reset_if_new_day({}, "2026-11-06")
        previous["sent"]["ABC"] = {"code": 101}
        state.save(previous)
        self.assertEqual(len(pipeline.run_once()["signals"]), 1)
        self.assertEqual(state.load()["session"], "2026-11-09")

    def test_new_session_resets_dedupe_and_drops_yesterdays_trade(self):
        """A trade is intraday: the rollover drops it instead of trailing it."""
        pipeline.run_once()
        before = state.load()
        self.assertTrue(before["positions"])                  # opened today
        after = state.reset_if_new_day(before, "2026-11-10")
        self.assertEqual(after["sent"], {})
        self.assertEqual(after["positions"], {})              # not carried over
        same_day = state.reset_if_new_day(before, "2026-11-09")   # a restart, same session
        self.assertEqual(same_day["positions"], before["positions"])

    def test_a_trade_from_an_earlier_session_is_dropped_not_trailed(self):
        """16 Nov: "TARGET HIT — BUY PNB · T1 136.75 · T2 141.02 · 5m 09:15".

        The PNB trade was signalled on 11 Nov. Its position was carried into the
        next session and trailed against *that* day's candles, so the 09:15
        candle "hit" two targets of an eleven-day-old entry. A trade is
        intraday: yesterday's position is dropped silently, no hit / stop /
        EMA9-exit alert can be produced for it, and it does not block today's
        entry on the same name either.
        """
        pipeline.run_once()
        self.assertIn("ABC", state.load()["positions"])
        next_bar = datetime(2026, 11, 10, 9, 15, tzinfo=IST)
        self.now = next_bar + timedelta(minutes=6)
        self.md.pick_sector, self.md.action, self.md.pick_side = "WAIT", "WAIT", 0
        index = bars(*self.times, next_bar)
        stock = quiet_bars(*self.times, next_bar)
        stock.loc[next_bar, "Close"] = 99.0                 # would close on EMA9 yesterday
        self.mocks[2].side_effect = lambda *args: {"m5": index, "d1": bars(BAR - timedelta(days=1))}
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: stock.copy() if t == "ABC.NS" else index.copy() for t in tickers}
        pipeline.run_once()
        self.assertNotIn("ABC", state.load()["positions"])   # dropped at the rollover
        self.assertEqual(state.load()["session"], "2026-11-10")
        self.assertFalse(any(("EMA9 EXIT" in c.args[0] or "TARGET HIT" in c.args[0]
                              or "SL HIT" in c.args[0]) for c in self.send.call_args_list))
        self.assertNotIn("ABC", state.load()["sent"])

    def test_a_ledger_left_over_from_a_previous_day_never_produces_a_hit_alert(self):
        """The live case: today's session stamp, yesterday's trade inside it.

        16 Nov's ledger was already rolled over to the 16th (so ``session``
        matched) while the PNB position inside it still pointed at 11 Nov. The
        rollover cannot see that; ``_track_positions`` must.
        """
        stale = {"session": str(BAR.date()),
                 "alerts": {},
                 "sent": {},
                 "positions": {"PNB": {
                     "side": "BUY", "entry": 135.73, "sl": 134.94,
                     "entry_ema9": 135.5, "t1": 136.75, "t2": 141.02, "t3": None,
                     "entry_bar": "2026-11-11T09:15:00+05:30",
                     "last_bar": "2026-11-11T09:15:00+05:30"}}}
        state.save(stale)
        self.now = BAR + timedelta(minutes=6)
        index = bars(*self.times, BAR)
        stock = quiet_bars(*self.times, BAR, high=142.0, low=134.0)   # trades through both
        self.mocks[2].side_effect = lambda *args: {"m5": index, "d1": bars(BAR - timedelta(days=1))}
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: stock.copy() if t == "PNB.NS" else index.copy() for t in tickers}
        with self.assertLogs("nse_scanner", level="WARNING") as logs:
            pipeline._track_positions(state.load(), BAR, "7d", send=self.send, save=state.save)
        self.assertTrue(any("is not today's" in line for line in logs.output))
        self.assertEqual(state.load()["positions"], {})
        # It never gets a hit alert — but it is still the user's position, so the
        # drop is announced once instead of happening in a log file.
        self.assertEqual(self.send.call_count, 1)
        text = self.send.call_args.args[0]
        self.assertIn("TRADE ABANDONED", text)
        self.assertIn("PNB", text)
        self.assertIn("2026-11-11", text)
        self.assertNotIn("TARGET HIT", text)

    # --- the stop protects a hit target (16 Nov: all three gave it back) -----
    def _extra_bar(self, stock, *, minutes=10):
        """Publish one more closed candle and point the scan at it."""
        bar = BAR + timedelta(minutes=minutes)
        self.now = bar + timedelta(minutes=6)
        self.times = self.times + [bar]
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: (stock.copy() if t.endswith(".NS") else bars(*self.times)) for t in tickers}
        self.mocks[2].side_effect = lambda *args: {"m5": bars(*self.times),
                                                   "d1": bars(BAR - timedelta(days=1))}
        return bar

    def test_breakeven_stop_closes_the_trade_after_t1(self):
        self._trade()
        stock = quiet_bars(*self.times, BAR + timedelta(minutes=10))
        stock.loc[BAR + timedelta(minutes=5), "High"] = 101.0      # T1 on the 10:05 candle
        self._extra_bar(stock)                                     # ... dips back at 10:10
        pipeline.run_once()
        texts = [c.args[0] for c in self.send.call_args_list]
        self.assertTrue(any("TARGET HIT" in t and "stop → breakeven 100.00" in t for t in texts))
        self.assertTrue(any("SL HIT" in t and "stop moved to breakeven after T1" in t
                            for t in texts))
        self.assertNotIn("ABC", state.load()["positions"])
        sl_line = next(t for t in texts if "SL HIT" in t)
        self.assertIn("+0.0R", sl_line)                 # R still uses the original stop

    def test_t1_without_the_breakeven_rule_keeps_the_original_stop(self):
        with patch.dict("os.environ", {"BREAKEVEN_AFTER_T1": "0"}):
            self._trade()
            stock = quiet_bars(*self.times, BAR + timedelta(minutes=10))
            stock.loc[BAR + timedelta(minutes=5), "High"] = 101.0
            self._extra_bar(stock)
            pipeline.run_once()
        texts = [c.args[0] for c in self.send.call_args_list]
        self.assertTrue(any("TARGET HIT" in t for t in texts))
        self.assertFalse(any("SL HIT" in t for t in texts))
        self.assertIn("ABC", state.load()["positions"])

    # --- the market block is not blind between label changes ----------------
    def _market_texts(self) -> list[str]:
        return [c.args[0] for c in self.send.call_args_list if c.args[0] == "market"]

    def test_a_confidence_slide_is_announced_without_a_label_change(self):
        pipeline.run_once()                                # first block, 74%
        self.send.reset_mock()
        self.md.dir_conf = 60                              # 74 -> 60, same BULL label
        self._extra_bar(quiet_bars(*self.times, BAR + timedelta(minutes=10)))
        pipeline.run_once()
        self.assertEqual(len(self._market_texts()), 1, "a 14-point confidence drop is news")

    def test_a_small_confidence_move_stays_quiet(self):
        pipeline.run_once()
        self.send.reset_mock()
        self.md.dir_conf = 70                              # 4 points, under the 10 default
        self._extra_bar(quiet_bars(*self.times, BAR + timedelta(minutes=10)))
        pipeline.run_once()
        self.assertEqual(self._market_texts(), [])

    def test_the_confidence_delta_can_be_switched_off(self):
        pipeline.run_once()
        self.send.reset_mock()
        self.md.dir_conf = 10                              # a 64-point drop, knob off
        self._extra_bar(quiet_bars(*self.times, BAR + timedelta(minutes=10)))
        with patch.dict("os.environ", {"MARKET_CONF_DELTA": "0"}):
            pipeline.run_once()
        self.assertEqual(self._market_texts(), [])

    def test_workflow_retries_until_target_bar_is_available(self):
        # External workflow_dispatch fires seconds after a close. Yahoo may
        # still have only the preceding candle on the first fetch.
        self.now = BAR + timedelta(minutes=5, seconds=10)
        target = BAR.strftime("%Y-%m-%d %H:%M")
        with (patch.object(pipeline, "run_once", side_effect=[
                {"bar": "2026-11-09 09:55", "action": "WAIT"},
                {"bar": target, "action": "SCAN LONG"}]) as once,
              patch.object(pipeline.time, "sleep") as sleep):
            out = pipeline.run_until_ready(expected_bar=BAR)
        self.assertEqual(out["bar"], target)
        self.assertEqual(once.call_count, 2)
        sleep.assert_called_once_with(5)  # fast poll right after the close

    def test_workflow_retries_when_no_stock_bar_was_published(self):
        self.now = BAR + timedelta(minutes=5, seconds=10)
        target = BAR.strftime("%Y-%m-%d %H:%M")
        with (patch.object(pipeline, "run_once", side_effect=[
                {"bar": target, "action": "PENDING"},
                {"bar": target, "action": "SCAN LONG"}]) as once,
              patch.object(pipeline.time, "sleep") as sleep):
            self.assertEqual(pipeline.run_until_ready(expected_bar=BAR)["action"], "SCAN LONG")
        self.assertEqual(once.call_count, 2)
        sleep.assert_called_once()

    def test_partial_stock_publication_retries_without_duplicate_alerts(self):
        self.now = BAR + timedelta(minutes=5, seconds=10)
        self.uni["sectors"]["IT"]["stocks"] = ["ABC", "DEF"]
        self.scan.side_effect = lambda raw, *args, **kw: [replace(signal(), symbol=raw)]
        calls = 0

        def stock_data(tickers, period, interval):
            nonlocal calls
            if interval == "5m" and "DEF.NS" in tickers:
                calls += 1
                return {t: (quiet_bars(*ts) if t.endswith(".NS") else bars(*ts))
                        for t in tickers
                        for ts in [(self.times[:1] if t == "DEF.NS" and calls == 1 else self.times)]}
            return {t: (quiet_bars(*self.times) if t.endswith(".NS") else bars(*self.times))
                    for t in tickers}

        self.fetch_many.side_effect = stock_data
        with patch.object(pipeline.time, "sleep") as sleep:
            result = pipeline.run_until_ready(expected_bar=BAR)
        self.assertEqual(calls, 2)
        self.assertEqual([s["symbol"] for s in result["signals"]], ["ABC", "DEF"])
        self.assertEqual(self.send.call_count, 3)  # market, ABC, DEF (no replay)
        self.assertEqual(set(state.load()["sent"]), {"ABC", "DEF"})
        sleep.assert_called_once()

    def test_expired_workflow_does_not_wait_or_backfill(self):
        self.now = BAR + timedelta(minutes=9)
        with (patch.object(pipeline, "run_once", return_value={"market": "STALE"}) as once,
              patch.object(pipeline.time, "sleep") as sleep):
            self.assertEqual(pipeline.run_until_ready(expected_bar=BAR)["market"], "STALE")
        once.assert_called_once_with(force_sector=None)
        sleep.assert_not_called()

    def test_far_target_and_wide_basis_are_disclosed_not_silently_shipped(self):
        # 11 Nov 2026 PNB: entry 135.73, risk 0.79, pivots from 09 Nov
        # (H 132.48 / L 125.92 / C 132.17 -> R2 136.75, R3 141.02).
        basis = PivotBasis(date(2026, 11, 9), 132.48, 125.92, 132.17, "1d", 4.9633, 1.70)
        plan = TradePlan(135.73, 134.9670, 134.94, 0.79, 136.75, 141.02, None,
                         ("R2", "R3", None), basis)
        text = ORIGINAL_FORMAT_STOCK(signal(time="10:25", code=17), self.md, plan)
        self.assertIn("T1 R2 136.75 (1.3R, +0.75%)", text)
        self.assertIn("T2 R3 141.02 (6.7R, +3.90% ⚠)", text)   # 6.7R / 3.9% is not a 5m target
        self.assertIn("T3 N/A", text)
        self.assertIn("Pivot basis 09 Nov  H 132.48  L 125.92  C 132.17  range 4.96%", text)
        self.assertIn("2.9× median", text)
        self.assertIn("wide basis", text)

    def test_normal_basis_shows_levels_without_the_wide_warning(self):
        basis = PivotBasis(date(2026, 11, 9), 135.40, 133.10, 135.10, "1d", 1.70, 1.65)
        plan = TradePlan(135.73, 134.9670, 134.94, 0.79, 135.95, 136.80, 138.20,
                         ("R1", "R2", "R3"), basis)
        text = ORIGINAL_FORMAT_STOCK(signal(time="10:25", code=17), self.md, plan)
        self.assertNotIn("wide basis day", text)
        self.assertNotIn("⚠", text)
        self.assertIn("Pivot basis 09 Nov", text)


class TargetGateTests(LiveAlertTests):
    """No pivot target at all means no entry (SCAN5M_REQUIRE_TARGET).

    17 Nov: HINDUNILVR was entered ``T2 N/A · T3 N/A`` (−0.6R) and DABUR
    ``T1 N/A · T2 N/A · T3 N/A`` (−0.8R). Both had no exit level; bot 3 has
    refused that since the 1m round, bot 1 did not.
    """

    def settings(self, **env):
        return patch.dict("os.environ", {"MIN_TARGET_R": "999", **env})

    def test_an_entry_without_a_target_is_refused_and_reported(self):
        with self.settings():
            self.assertEqual(pipeline.run_once()["signals"], [])
        self.assertNotIn("ABC", state.load()["positions"])
        self.assertIn("ABC@10:00", state.load()["stats"]["rejected"]["no_target"])
        self.assertEqual([c for c in self.send.call_args_list if c.args[0].startswith("stock")], [])

    def test_the_old_rule_can_be_restored(self):
        with self.settings(SCAN5M_REQUIRE_TARGET="0"):
            self.assertEqual(len(pipeline.run_once()["signals"]), 1)
        self.assertIn("ABC", state.load()["positions"])

    def test_a_plan_with_a_target_is_untouched(self):
        self.assertEqual(len(pipeline.run_once()["signals"]), 1)


class AlertInventoryTests(TestCase):
    """Every state a trade can end a session in has a line that says so.

    The audit that produced this class went looking for the messages nobody
    sends: a trade still open at 15:30, a trade dropped the next morning, a name
    the pick named but never scanned, an open trade whose candles never arrived.
    """

    def summary(self, **stats):
        base = {"candles": 75, "picks": 10, "rejected": {}}
        return pipeline.format_summary({"session": "2026-11-17",
                                        "stats": {**base, **stats}, "positions": {}})

    def test_the_summary_names_the_trades_with_no_exit_alert(self):
        text = pipeline.format_summary({
            "session": "2026-11-17", "stats": {"candles": 75, "picks": 10},
            "positions": {"LODHA": {"side": "BUY", "entry": 1208.2, "sl": 1208.2}}})
        self.assertIn("Open at the close — no exit alert yet: LODHA 1208.20 (stop 1208.20)", text)

    def test_a_flat_book_says_so_instead_of_saying_nothing(self):
        self.assertIn("Open at the close: none", self.summary())

    def test_an_unchecked_trade_is_named_with_the_reason(self):
        self.assertIn("not checked this session (no 5m candle): PNB",
                      self.summary(pending=["PNB"]))

    def test_a_trade_checked_all_day_is_not_called_never_evaluated(self):
        # 19 Nov: BAJAJ-AUTO's last candle was missing from the feed, and the
        # summary said its stops, targets and EMA9 exit "were never evaluated" on
        # the same page as its T1 and T2 hits.
        text = self.summary(pending=["BAJAJ-AUTO"], checked=["BAJAJ-AUTO"])
        self.assertIn("not checked on the last candle (no 5m candle): BAJAJ-AUTO", text)
        self.assertNotIn("never evaluated", text)

    def test_a_name_never_checked_still_says_so(self):
        text = self.summary(pending=["PNB", "BAJAJ-AUTO"], checked=["BAJAJ-AUTO"])
        self.assertIn("not checked this session (no 5m candle): PNB", text)
        self.assertIn("never evaluated", text)
        self.assertIn("not checked on the last candle (no 5m candle): BAJAJ-AUTO", text)

    def test_a_name_the_pick_named_but_could_not_be_scanned_is_reported(self):
        text = self.summary(rejected={
            "no_data": {"X@09:20": "X 09:20"},
            "scan_error": {"Y@09:25": "Y 09:25"}})
        # 18 Nov counted these as names: "147 had no 5m candle to scan" was 147
        # name-checks across 5 candles, and nothing said the bar was the feed's.
        self.assertIn("1 name-check had no 5m bar from the feed (X 09:20)", text)
        self.assertIn("1 could not be scanned (Y 09:25)", text)

    def test_the_1m_summary_carries_the_same_open_line(self):
        from nse_scanner import scan1m
        text = scan1m.format_summary({
            "session": "2026-11-17", "stats": {"candles": 10, "picks": 4},
            "positions": {"BANKINDIA": {"side": "BUY", "entry": 155.12, "sl": 155.12}}})
        self.assertIn("Open at the close — no exit alert yet: BANKINDIA 155.12", text)


class PendingBookKeepingTests(TestCase):
    """'not checked' describes the open book — it cannot outlive it.

    The list was set when a trade's candles were missing and never cleared, so a
    session whose last position had already closed still printed
    ``not checked this session (no 5m candle): PNB`` in the 15:33 summary.
    """

    def st(self, positions):
        return {"session": "2026-11-17", "alerts": {}, "sent": {},
                "positions": positions, "stats": {"candles": 3, "picks": 1}}

    def trade(self):
        return {"side": "BUY", "entry": 100.0, "sl": 99.0,
                "entry_bar": "2026-11-17T10:00:00+05:30",
                "last_bar": "2026-11-17T10:00:00+05:30"}

    def test_a_closed_book_forgets_what_was_unchecked(self):
        st = self.st({"PNB": self.trade()})
        st["stats"]["pending"] = ["PNB"]
        pipeline._track_positions(st, BAR, "7d", send=self.send_none, save=lambda _: None)
        self.assertNotIn("pending", st["stats"])
        self.assertNotIn("not checked", pipeline.format_summary(st))

    def test_an_empty_book_needs_no_save_to_forget_it(self):
        st = self.st({})
        st["stats"]["pending"] = ["PNB"]
        pipeline._track_positions(st, BAR, "7d", send=self.send_none, save=lambda _: None)
        self.assertNotIn("pending", st["stats"])
        self.assertIn("Open at the close: none", pipeline.format_summary(st))

    @staticmethod
    def send_none(text, dry_run=False):
        return True


class SummaryInvariantTests(TestCase):
    """The counters a reader can check with a pencil must balance.

    The 17 Nov pair failed this: ``with a sector pick 62`` against a sector line
    summing to 66, because the follow sectors were counted in the pick's book.
    """

    def test_the_sector_book_sums_to_the_picks(self):
        from nse_scanner import scan1m
        stats = {}
        for i, pick in enumerate([
                {"sector": "PSUBK", "side": 1, "follow": False, "extra": ["FMCG"],
                 "bar": "2026-11-17 09:20"},
                {"sector": "PSUBK", "side": 1, "follow": False, "extra": [],
                 "bar": "2026-11-17 09:25"},
                {"sector": "WAIT", "side": 0, "extra": [], "bar": "2026-11-17 09:30"}]):
            scan1m._count_candle(stats, pick)
        self.assertEqual(stats["candles"], 3)
        self.assertEqual(stats["picks"], 2)
        self.assertEqual(sum(stats["sectors"].values()), stats["picks"])
        self.assertEqual(stats["extra"], {"FMCG": 1})

    def test_one_candle_is_counted_once_however_often_it_is_judged(self):
        from nse_scanner import scan1m
        stats = {}
        pick = {"sector": "PSUBK", "side": 1, "follow": False, "extra": [],
                "bar": "2026-11-17 09:20"}
        for _ in range(4):                    # projection pass, retries, confirm pass
            scan1m._count_candle(stats, pick)
        self.assertEqual((stats["candles"], stats["picks"]), (1, 1))

    def test_entries_never_exceed_what_the_cap_allows_per_pick(self):
        st = {"pick_entries": {}}
        key = pipeline.pick_cap_key("IT", 1)
        with patch.dict("os.environ", {"MAX_ENTRIES_PER_PICK": "3"}):
            for _ in range(3):
                self.assertTrue(pipeline.entry_cap_room(st, key))
                pipeline.take_entry_slot(st, key)
            self.assertFalse(pipeline.entry_cap_room(st, key))
            self.assertTrue(pipeline.entry_cap_room(st, pipeline.pick_cap_key("PHA", 1)))

    def test_the_two_loops_charge_the_cap_to_the_same_keys(self):
        # One sector on one side is one risk factor, and the key is built from the
        # sector the name lives in — never from the pick it was scanned under.
        self.assertEqual(pipeline.pick_cap_key("CDUR", 1), "CDUR long")
        self.assertEqual(pipeline.pick_cap_key("FMCG", -1), "FMCG short")
        self.assertNotEqual(pipeline.pick_cap_key("CDUR", 1), pipeline.pick_cap_key("FMCG", 1))


class LateLeaderTests(LiveAlertTests):
    """A crossover that fired before its sector was picked is not lost (18 Nov).

    CDUR was the pick from 09:45 with PERSIST − 93→99 and a qualified live flow,
    and no CDUR stock was alerted all morning: the names had fired at 09:20–09:40,
    while the 5m entry loop only accepts a fire on the decision candle. Bot 3 has
    had this alert since the 1m round; bot 1 filed the names as "earlier" and said
    nothing until the 15:33 summary.
    """

    #: These tests pin the heads-up wording, so they switch the catch-up *entry*
    #: off; ``LateLeaderEntryTests`` below turns it on.
    LATE_ENTRY = "0"

    def setUp(self):
        super().setUp()
        self.ctx = patch.dict("os.environ", {"LATE_LEADER_ENTRY": self.LATE_ENTRY})
        self.ctx.start()
        self.addCleanup(self.ctx.stop)
        # The fire candle needs 9 in-session 5m slots before it for its EMA9 (the
        # stop of the catch-up plan) to exist at all: 09:15..09:55.
        self.times = ([BAR - timedelta(minutes=5 * i) for i in range(9, 0, -1)]
                      + [BAR, BAR + timedelta(minutes=5)])

    def test_a_stock_that_fired_before_the_pick_is_reported(self):
        self.scan.side_effect = lambda raw, *a, **k: [signal(time="09:55")]
        pipeline.run_once()                       # BAR is 10:00, 11 minutes later
        texts = [c.args[0] for c in self.send.call_args_list]
        late = [t for t in texts if "LATE LEADER" in t]
        self.assertEqual(len(late), 1, texts)
        self.assertIn("fired before its sector was picked", late[0])
        self.assertIn("Fired <b>09:55</b>", late[0])
        self.assertIn("Not an entry alert", late[0])
        self.assertIn("no position", late[0].replace("no position was opened for it", "no position"))
        self.assertEqual(state.load()["stats"]["catchup"], 1)
        self.assertNotIn("ABC", state.load()["positions"])       # never a trade
        self.assertIn("ABC", state.load()["catchup"])

    def test_it_is_not_sent_twice_on_the_next_candle(self):
        self.scan.side_effect = lambda raw, *a, **k: [signal(time="09:55")]
        pipeline.run_once()
        self.now = BAR + timedelta(minutes=11)
        self.times = self.times + [BAR + timedelta(minutes=5)]
        self.scan.side_effect = lambda raw, *a, **k: [signal(time="09:50")]
        pipeline.run_once()
        self.assertEqual(sum("LATE LEADER" in c.args[0] for c in self.send.call_args_list), 1)

    def test_a_burst_of_late_leaders_is_one_digest(self):
        self.uni["sectors"]["IT"]["stocks"] = ["ABC", "XYZ"]
        self.scan.side_effect = lambda raw, *a, **k: [signal(time="09:55")]
        pipeline.run_once()
        late = [c.args[0] for c in self.send.call_args_list if "LATE LEADER" in c.args[0]]
        self.assertEqual(len(late), 1, late)      # one message, not one per name
        self.assertIn("×2", late[0])
        self.assertIn("ABC", late[0])
        self.assertIn("XYZ", late[0])
        self.assertIn("Not entry alerts", late[0])
        self.assertEqual(state.load()["stats"]["catchup"], 2)

    def test_a_stop_taken_since_the_fire_is_refused_and_counted(self):
        self.scan.side_effect = lambda raw, *a, **k: [signal(time="09:55")]
        taken = quiet_bars(*self.times, low=98.0)          # traded through the SL since
        self.fetch_many.side_effect = lambda tickers, period, interval: {
            t: (taken if t == "ABC.NS" else bars(*self.times)) for t in tickers}
        pipeline.run_once()
        self.assertEqual([c for c in self.send.call_args_list if "LATE LEADER" in c.args[0]], [])
        self.assertEqual(state.load()["stats"]["catchup_skip"]["stop"], 1)
        self.assertIn("stop taken since the fire 1", pipeline.format_summary(state.load()))

    def test_the_knob_switches_the_alert_off_but_not_the_reject_book(self):
        self.scan.side_effect = lambda raw, *a, **k: [signal(time="09:55")]
        with patch.dict("os.environ", {"SCAN5M_LEADER_CATCHUP": "0"}):
            pipeline.run_once()
        self.assertEqual([c for c in self.send.call_args_list if "LATE LEADER" in c.args[0]], [])
        self.assertIn("ABC 09:55", state.load()["stats"]["rejected"]["earlier"]["ABC"])


class LateLeaderEntryTests(LiveAlertTests):
    """The late leader as an *entry*, not only a heads-up (19 Nov, AMBER/VOLTAS).

    The heads-up is a message about a trade nobody took: bot 3's 10:01 list named
    nine CDUR names at 0.3R-1.8R, labelled "not an entry", and the day's five
    entries were all elsewhere. A leader that is still before its first target is
    still a trade, so it is taken at the price now with the fire candle's stop.
    """

    def setUp(self):
        super().setUp()
        self.ctx = patch.dict("os.environ", {"LATE_LEADER_ENTRY": "1"})
        self.ctx.start()
        self.addCleanup(self.ctx.stop)
        self.times = ([BAR - timedelta(minutes=5 * i) for i in range(9, 0, -1)]
                      + [BAR, BAR + timedelta(minutes=5)])

    def test_a_late_leader_before_its_target_is_entered(self):
        self.scan.side_effect = lambda raw, *a, **k: [signal(time="09:55")]
        pipeline.run_once()
        texts = [c.args[0] for c in self.send.call_args_list]
        entry = [t for t in texts if "LATE LEADER ENTRY" in t]
        self.assertEqual(len(entry), 1, texts)
        self.assertIn("Catch-up entry", entry[0])
        self.assertIn("Fired <b>09:55</b>", entry[0])
        self.assertNotIn("Not an entry alert", entry[0])
        st = state.load()
        self.assertEqual(st["stats"]["late_entries"], 1)
        self.assertEqual(st["stats"]["entries"], 1)
        self.assertEqual(st["positions"]["ABC"]["entry"], 100.0)
        self.assertLess(st["positions"]["ABC"]["sl"], 100.0)   # the fire candle's EMA9 stop
        self.assertTrue(st["catchup"]["ABC"]["entry"])

    def test_the_entry_fills_the_pick_cap(self):
        self.scan.side_effect = lambda raw, *a, **k: [signal(time="09:55")]
        with patch.dict("os.environ", {"MAX_ENTRIES_PER_PICK": "1"}):
            pipeline.run_once()
            self.now = BAR + timedelta(minutes=11)
            self.times = self.times + [BAR + timedelta(minutes=10)]
            pipeline.run_once()
        st = state.load()
        self.assertEqual(st["stats"]["late_entries"], 1)
        self.assertEqual(st["pick_entries"], {"IT long": 1})

    def test_a_trade_that_has_already_paid_is_refused_by_the_plan(self):
        plan = TradePlan(100.0, 99.0, 99.0, 1.0, 103.0, 104.0, 105.0)
        hit = {"sig": signal(), "plan": plan, "price": 100.5, "r": 0.5, "age": 5.0}
        got = pipeline.late_plan(hit)
        self.assertIsNotNone(got)
        self.assertEqual((got.entry, got.sl, got.risk), (100.5, 99.0, 1.5))
        self.assertEqual((got.t1, got.pivot_names), (103.0, (None, None, None)))
        self.assertIsNone(pipeline.late_plan({**hit, "price": 103.0}))   # at T1: paid
        self.assertIsNone(pipeline.late_plan({**hit, "price": 98.5}))    # through the stop

    def test_the_nearest_target_must_still_be_worth_the_risk_taken_now(self):
        """19 Nov M&M: +2.0R from its fire, T1 1.77 points away, 44-point stop.

        "Price is still before T1" would have entered it at 0.04R of room. The
        catch-up obeys the same ``MIN_TARGET_R`` as a fresh entry instead.
        """
        plan = TradePlan(100.0, 99.0, 99.0, 1.0, 100.6, 106.0, 108.0)   # M&M's shape
        hit = {"sig": signal(), "plan": plan, "price": 102.0, "r": 2.0, "age": 25.0}
        self.assertIsNone(pipeline.late_plan(hit))          # 1.4 of room, 3.0 of risk
        self.assertIsNone(pipeline.late_plan({**hit, "price": 100.3}))   # 0.23R of room
        far = TradePlan(100.0, 99.0, 99.0, 1.0, 105.0, 106.0, 108.0)    # target worth it
        self.assertIsNotNone(pipeline.late_plan({**hit, "plan": far, "price": 100.5}))

    def test_the_knob_restores_the_plain_heads_up(self):
        self.scan.side_effect = lambda raw, *a, **k: [signal(time="09:55")]
        with patch.dict("os.environ", {"LATE_LEADER_ENTRY": "0"}):
            pipeline.run_once()
        texts = [c.args[0] for c in self.send.call_args_list]
        self.assertEqual([t for t in texts if "LATE LEADER ENTRY" in t], [])
        self.assertEqual([t for t in texts if "Not an entry alert" in t].__len__(), 1)
        self.assertNotIn("ABC", state.load()["positions"])


class TradeRecordTests(TestCase):
    """Both bots write one trade record, so both name the pivots they hit."""

    plan = staticmethod(lambda: TradePlan(100.0, 99.8, 99.0, 1.0, 102.0, 104.0, 106.0,
                                          ("R1", "R2", "R3")))

    def test_the_record_keeps_the_risk_and_the_pivot_names(self):
        pos = pipeline.new_position("BUY", self.plan(), "2026-11-09T10:00:00+05:30",
                                    "2026-11-09")
        self.assertEqual(pos["t_names"], ["R1", "R2", "R3"])
        self.assertEqual((pos["sl0"], pos["sl"], pos["risk"]), (99.0, 99.0, 1.0))
        self.assertEqual((pos["t1"], pos["t2"], pos["t3"]), (102.0, 104.0, 106.0))
        self.assertEqual((pos["entry"], pos["side"], pos["session"]),
                         (100.0, "BUY", "2026-11-09"))

    def test_a_target_line_names_the_pivot(self):
        pos = pipeline.new_position("BUY", self.plan(), "2026-11-09T10:00:00+05:30",
                                    "2026-11-09")
        text = pipeline.format_target_hit("ABC", pos, BAR, [(1, 102.0)])
        self.assertIn("T1 R1 102.00", text)
        self.assertIn("+2.00%", text)
        self.assertIn("+2.0R", text)          # 2 points against a 1-point risk

    def test_bot3_and_bot1_build_the_record_the_same_way(self):
        import inspect
        from nse_scanner import scan1m
        self.assertIn("new_position", inspect.getsource(scan1m.confirm_pass))
        self.assertNotIn('"risk":', inspect.getsource(scan1m.confirm_pass))
