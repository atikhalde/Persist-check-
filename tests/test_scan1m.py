"""The 1-minute projected scan (Telegram bot #3) — no network access."""
import contextlib
import io
import inspect
from datetime import timedelta
from pathlib import Path
from unittest import TestCase
from types import SimpleNamespace
from unittest.mock import patch
import tempfile

import pandas as pd
from pandas.testing import assert_frame_equal

from nse_scanner import data, pipeline, scan1m, state, stock, telegram
from datetime import time as clock_time
from nse_scanner.trade import TradePlan

import test_live_alerts as base

BAR = base.BAR                                     # 10:00 open; closes 10:05
MIN1 = BAR + timedelta(minutes=1, seconds=5)       # minute 1 of the candle
MIN3 = BAR + timedelta(minutes=3, seconds=5)
CLOSE = BAR + timedelta(minutes=5, seconds=6)      # the confirm moment

PLAN = TradePlan(100.0, 100.0, 99.98, 0.02, 100.02, 100.04, 100.06, ("R1", "R2", "R3"))


def five_minute_history():
    """Completed 5m bars up to and including BAR (so the close has a real row)."""
    times = [BAR - timedelta(minutes=5 * i) for i in range(25, -1, -1)]
    return base.quiet_bars(*times)


def one_minute(*minutes, price=100.0):
    idx = pd.DatetimeIndex([BAR + timedelta(minutes=m) for m in minutes])
    return pd.DataFrame({"Open": price, "High": price + 0.02, "Low": price - 0.02,
                         "Close": price, "Volume": 250.0}, index=idx)


class Scan1mFixture(TestCase):
    """Patched world: one stock, one 5m candle, both bots recorded.

    Only the fixture lives here, so the test classes below can share it without
    running each other's tests again.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        scan1m._five_cache.clear()
        scan1m._pick_cache.clear()

        self.env = patch.dict("os.environ", {
            "DRY_RUN": "0", "SCAN1M_FIRST_MINUTE": "1", "SCAN1M_MAX_STALE_SECONDS": "100",
            # this world is the old one: the per-candle alert cap has its own tests
            "SCAN1M_ALERT_CAP": "0",
            "SCAN1M_EARLY_PER_DAY": "1", "SCAN1M_CANCEL_MESSAGE": "0",
            "SCAN1M_MARKET_SOURCE": "ledger", "TELEGRAM_BOT_TOKEN_3": "",
            "TELEGRAM_CHAT_ID_3": "",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

        # fixtures first: several patches capture them eagerly
        self.now = MIN1
        self.pick = {"bar": (BAR - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M"),
                     "sector": "IT", "side": 1, "label": "BULL", "conf": 74,
                     "extra": [], "follow": False}
        self.universe = {"nifty": "NIFTY", "vix": "VIX",
                         "sectors": {"IT": {"index": "INDEX", "stocks": ["ABC"]}}}
        self.fires = True
        self.five = five_minute_history()
        self.minutes = one_minute(0, 1)
        self.one_history = None
        scan1m._one_cache.clear()
        scan1m._five_cache.clear()

        self.patches = [
            patch.object(scan1m, "LEDGER", self.dir / "scan1m.json"),
            patch.object(state, "STATE_PATH", self.dir / "scan_state.json"),
            patch.object(scan1m, "now_ist", side_effect=lambda: self.now),
            patch.object(scan1m, "pick_for",
                         side_effect=lambda bar, projection, force_sector=None, now=None: self.pick),
            patch.object(scan1m.data, "load_universe", return_value=self.universe),
            patch.object(scan1m.data, "yahoo_stock", side_effect=lambda raw: raw + ".NS"),
            patch.object(scan1m.data, "fetch_many", side_effect=self.fetch),
            patch.object(scan1m.data, "fetch_daily", return_value={}),
            patch.object(scan1m, "scan_stock", side_effect=self.scan),
            patch.object(scan1m, "build_plan", return_value=PLAN),
            patch.object(scan1m, "_oi_reader", return_value=None),
            patch.object(scan1m.telegram, "send_bot3", return_value=True),
            patch.object(telegram, "send"),
            patch.object(stock, "now_ist", side_effect=lambda: self.now),
        ]
        from nse_scanner import pipeline
        self.pipeline_now = patch.object(pipeline, "now_ist", side_effect=lambda: self.now)
        self.patches.append(self.pipeline_now)

        self.mocks = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)
        self.bot3 = self.mocks[11]
        self.bot1 = self.mocks[12]
        self.engine = self.mocks[8]

    # ── fixtures ────────────────────────────────────────────────────────────
    def fetch(self, tickers, period, interval):
        if interval == "5m":
            return {t: self.five for t in tickers}
        return {t: (self.one_history if period == "7d" and self.one_history is not None
                    else self.minutes) for t in tickers}

    def scan(self, raw, frame, daily, keep_all=False):
        if not self.fires or frame.index[-1] != BAR:
            return []
        return [base.signal(time=BAR.strftime("%H:%M"))]

    def ledger(self):
        return state.load(self.dir / "scan1m.json")

# ── the entry rules bot 3 shares with bot 1 ─────────────────────────────────
class Bot3EntryRuleTests(Scan1mFixture):
    """One pick is one risk factor on both bots, and the leader goes first.

    Bot 3 used to confirm every name that fired on the close — on 16 Nov that
    was six pharma entries where bot 1 announced three — and it wrote its own
    trade record, which had silently lost the pivot names.
    """

    LAST = {"AAA": 100.4, "BBB": 103.0, "CCC": 100.6, "DDD": 100.2}
    SECTOR_INTRA = 1.0

    def setUp(self):
        super().setUp()
        self.universe["sectors"]["IT"]["stocks"] = ["AAA", "BBB", "CCC", "DDD"]
        self.pick = {**self.pick, "intra": self.SECTOR_INTRA}
        self.minutes = one_minute(0, 1, 2, 3, 4, 5)     # the whole 10:00 candle
        self.engine.side_effect = self.scan_all
        self.plan_for = lambda raw: base.TradePlan(
            self.LAST[raw], self.LAST[raw], self.LAST[raw] - 1.0, 1.0,
            self.LAST[raw] + 1.0, self.LAST[raw] + 2.0, self.LAST[raw] + 3.0,
            ("R1", "R2", "R3"))
        self.patches.append(patch.object(scan1m, "build_plan",
                                         side_effect=lambda sig, frame, bar, daily: self.plan_for(sig.symbol)))
        self.patches[-1].start()
        self.addCleanup(self.patches[-1].stop)

    def fetch(self, tickers, period, interval):
        if interval != "5m":
            return {t: self.minutes for t in tickers}
        out = {}
        for t in tickers:
            c = self.LAST[t[:-3]]
            idx = self.five.index
            out[t] = pd.DataFrame({"Open": [100.0] * len(idx),
                                   "High": [max(100.0, c)] * len(idx),
                                   "Low": [min(100.0, c)] * len(idx),
                                   "Close": [c] * len(idx),
                                   "Volume": [100000.0] * len(idx)}, index=idx)
        return out

    def scan_all(self, raw, frame, daily, keep_all=False):
        if frame.index[-1] != BAR:
            return []
        return [base.StockSignal(raw, 101, "BUY-EX", "BUY", BAR.strftime("%H:%M"),
                                 self.LAST[raw], 70, "Strong", 20, 1, "test",
                                 self.LAST[raw] - 1.0)]

    def test_bot3_confirms_three_of_four_on_the_same_ceiling(self):
        out = scan1m.confirm_pass(BAR, now=CLOSE)
        self.assertEqual(len(out), 3)
        self.assertEqual(self.ledger()["pick_entries"], {"IT long": 3})
        self.assertIn("DDD@10:00", self.ledger()["stats"]["rejected"]["cap"])

    def test_the_leader_is_confirmed_first(self):
        # BBB is the furthest ahead of its sector (+2.0pp) at the lowest score.
        with patch.dict("os.environ", {"MAX_ENTRIES_PER_PICK": "1"}):
            out = scan1m.confirm_pass(BAR, now=CLOSE)
        self.assertEqual([s["symbol"] for s in out], ["BBB"])
        self.assertIn("AAA@10:00", self.ledger()["stats"]["rejected"]["cap"])

    def test_the_trade_record_is_the_5m_one(self):
        scan1m.confirm_pass(BAR, now=CLOSE)
        pos = self.ledger()["positions"]["BBB"]
        self.assertEqual(pos["t_names"], ["R1", "R2", "R3"])
        self.assertEqual(pos["sl0"], self.LAST["BBB"] - 1.0)
        self.assertEqual(pos["risk"], 1.0)

    def test_the_confirmed_text_carries_the_sector_lead(self):
        scan1m.confirm_pass(BAR, now=CLOSE)
        texts = [c.args[0] for c in self.bot3.call_args_list]
        self.assertTrue(any("lead +2.0pp vs sector" in t for t in texts), texts)

    def test_the_lead_is_printed_only_when_the_ranking_is_on(self):
        with patch.dict("os.environ", {"RANK_BY_SECTOR_LEAD": "0"}):
            scan1m.confirm_pass(BAR, now=CLOSE)
        texts = [c.args[0] for c in self.bot3.call_args_list]
        self.assertEqual(len(texts), 3)
        self.assertFalse(any("vs sector" in t for t in texts))     # exactly as bot 1

    def test_the_catch_up_window_is_saved_with_the_ledger(self):
        # It used to live only in memory: every minute started a fresh window, so
        # a name whose feed lagged was retried until the pick changed or 15:30.
        scan1m.run_minute(BAR, now=MIN1)
        self.assertEqual(self.ledger()["catchup_pick"]["since"], BAR.isoformat())

    def test_a_capped_out_projection_is_resolved_as_not_taken(self):
        scan1m.run_minute(BAR, now=MIN1)          # one projection per name
        self.assertEqual(len(self.ledger()["early"]), 4)
        with patch.dict("os.environ", {"MAX_ENTRIES_PER_PICK": "1"}):
            scan1m.confirm_pass(BAR, now=CLOSE)
        book = self.ledger()["early"]
        taken = [raw for raw, e in book.items() if e.get("result") == "confirmed"]
        self.assertEqual(taken, ["BBB"])
        self.assertTrue(all(e.get("result") == "cancelled"
                            for raw, e in book.items() if raw != "BBB"))


# ── the projection ──────────────────────────────────────────────────────────
class Scan1mTests(Scan1mFixture):
    def test_projects_on_minute_one_and_only_to_bot3(self):
        out = scan1m.run_minute(BAR, now=self.now)
        self.assertEqual(len(out), 1)
        self.assertEqual(self.bot3.call_count, 1)
        self.bot1.assert_not_called()
        text = self.bot3.call_args.args[0]
        self.assertIn("<b>EARLY BUY</b> (1m projection)", text)
        self.assertIn("5m bar 10:00–10:05 still forming", text)
        self.assertIn("3m 55s left", text)
        self.assertIn("identical to the 5m scan", text)
        entry = self.ledger()["early"]["ABC"]
        self.assertEqual((entry["minute"], entry["side"], entry["result"]), (1, "BUY", None))
        self.assertEqual(self.ledger()["sent"], {})          # not an entry yet

    def test_projection_frame_is_history_plus_the_forming_candle(self):
        scan1m.run_minute(BAR, now=self.now)
        frame = self.engine.call_args.args[1]
        self.assertEqual(frame.index[-1], BAR)               # projected candle is last
        self.assertEqual(len(frame), len(self.five))         # history + the projection
        self.assertTrue(self.engine.call_args.kwargs["keep_all"])

    def test_first_minute_gate(self):
        with patch.dict("os.environ", {"SCAN1M_FIRST_MINUTE": "2"}):
            self.assertEqual(scan1m.run_minute(BAR, now=MIN1), [])
            self.bot3.assert_not_called()
            self.assertEqual(len(scan1m.run_minute(BAR, now=BAR + timedelta(minutes=2, seconds=5))), 1)

    def test_same_candle_is_projected_once(self):
        scan1m.run_minute(BAR, now=MIN1)
        scan1m.run_minute(BAR, now=MIN3)
        self.assertEqual(self.bot3.call_count, 1)

    def test_stale_one_minute_feed_never_guesses(self):
        # only the first minute published; at minute 3 the newest print is 185s old
        self.minutes = one_minute(0)
        self.assertEqual(scan1m.run_minute(BAR, now=MIN3), [])
        self.bot3.assert_not_called()

    def test_no_pick_no_projection(self):
        self.pick = None
        self.assertEqual(scan1m.run_minute(BAR, now=self.now), [])
        self.bot3.assert_not_called()

    def test_opposite_side_is_not_projected(self):
        self.pick = {**self.pick, "side": -1}
        self.assertEqual(scan1m.run_minute(BAR, now=self.now), [])
        self.bot3.assert_not_called()

    def test_wait_pick_is_not_a_pick(self):
        self.pick = {**self.pick, "sector": "WAIT", "side": 0}
        self.assertEqual(scan1m.run_minute(BAR, now=self.now), [])
        self.bot3.assert_not_called()

    # ── the close ───────────────────────────────────────────────────────────
    def test_close_confirms_the_projection(self):
        scan1m.run_minute(BAR, now=MIN1)
        self.minutes = one_minute(0, 1, 2, 3, 4, 5)
        out = scan1m.confirm_pass(BAR, now=CLOSE)
        self.assertEqual(len(out), 1)
        self.assertEqual(self.bot3.call_count, 2)
        text = self.bot3.call_args.args[0]
        self.assertIn("<b>CONFIRMED</b> — projected 10:01:05 @ 100.00", text)
        self.assertIn("Entry (candle close) <b>100.00</b>", text)   # the 5m entry text
        led = self.ledger()
        self.assertEqual(led["early"]["ABC"]["result"], "confirmed")
        self.assertEqual(led["sent"]["ABC"]["time"], "10:00")
        self.bot1.assert_not_called()

    def test_projection_that_does_not_hold_is_silent(self):
        scan1m.run_minute(BAR, now=MIN1)
        self.minutes = one_minute(0, 1, 2, 3, 4, 5)
        self.fires = False
        self.assertEqual(scan1m.confirm_pass(BAR, now=CLOSE), [])
        self.assertEqual(self.bot3.call_count, 1)                  # the early alert only
        led = self.ledger()
        self.assertEqual(led["early"]["ABC"]["result"], "cancelled")
        self.assertEqual(led["sent"], {})

    def test_cancel_can_be_reported_when_enabled(self):
        scan1m.run_minute(BAR, now=MIN1)
        self.minutes = one_minute(0, 1, 2, 3, 4, 5)
        self.fires = False
        with patch.dict("os.environ", {"SCAN1M_CANCEL_MESSAGE": "1"}):
            scan1m.confirm_pass(BAR, now=CLOSE)
        self.assertEqual(self.bot3.call_count, 2)
        self.assertIn("EARLY CANCELLED", self.bot3.call_args.args[0])

    def test_close_only_signal_is_still_sent(self):
        # No projection for this candle: the message is exactly the 5m entry text
        # (the parity harness compares it character for character).
        self.minutes = one_minute(0, 1, 2, 3, 4, 5)
        out = scan1m.confirm_pass(BAR, now=CLOSE)
        self.assertEqual(len(out), 1)
        text = self.bot3.call_args.args[0]
        self.assertTrue(text.startswith("🟢 <b>BUY</b> | <b>BUY-EX</b>  ABC"))
        self.assertNotIn("CONFIRMED", text)
        self.assertEqual(self.ledger()["sent"]["ABC"]["time"], "10:00")

    def test_a_feed_that_has_not_reached_the_close_is_not_judged(self):
        # 1m prints stop at minute 2: the 5m row may still be forming
        self.minutes = one_minute(0, 1, 2)
        self.assertIsNone(scan1m.closed_frame(self.five, self.minutes, BAR))
        self.assertEqual(scan1m.confirm_pass(BAR, now=CLOSE), [])
        self.bot3.assert_not_called()

    def test_close_waits_for_the_published_5m_row_like_the_5m_scan(self):
        # run_once only judges a stock whose Yahoo 5m row is at the decision bar
        # (its ``current`` test). The 1m scan does not invent that row from 1m
        # prints: it waits, so both scans alert on the same candles.
        self.five = five_minute_history().iloc[:-1]        # the closed row is absent
        self.minutes = one_minute(0, 1, 2, 3, 4, 5)
        self.assertEqual(scan1m.confirm_pass(BAR, now=CLOSE), [])
        self.engine.assert_not_called()
        self.bot3.assert_not_called()

    def test_unpublished_candle_is_not_written_off(self):
        scan1m.run_minute(BAR, now=MIN1)
        with patch.object(scan1m, "closed_frame", return_value=None):
            self.assertEqual(scan1m.confirm_pass(BAR, now=CLOSE), [])
        self.assertEqual(self.ledger()["early"]["ABC"]["result"], None)
        self.assertEqual(scan1m.unresolved(BAR), {"ABC"})

    def test_one_entry_per_stock_per_session(self):
        self.minutes = one_minute(0, 1, 2, 3, 4, 5)
        scan1m.confirm_pass(BAR, now=CLOSE)
        later = BAR + timedelta(minutes=5)
        self.fires = True
        self.now = later + timedelta(minutes=6)
        self.assertEqual(scan1m.confirm_pass(later, now=self.now), [])
        self.assertEqual(self.bot3.call_count, 1)

    # ── isolation from the 5m scan ──────────────────────────────────────────
    def test_own_ledger_never_touches_the_5m_one(self):
        scan1m.run_minute(BAR, now=MIN1)
        self.assertTrue((self.dir / "scan1m.json").exists())
        self.assertFalse((self.dir / "scan_state.json").exists())

    def test_dry_run_uses_its_own_scratch_ledger(self):
        with patch.dict("os.environ", {"DRY_RUN": "1"}):
            self.assertEqual(scan1m.ledger_path().name, "scan1m.dry.json")


class FiveMinuteParityTests(Scan1mFixture):
    """Everything the 5m scan does around the engine, the 1m scan does too."""

    def test_prior_day_volume_is_repaired_exactly_like_the_5m_scan(self):
        # Yesterday's 5m row has Volume=0 (the Yahoo 09:15 auction bar); the 1m
        # history knows the real volume. The 5m scan repairs it — so must we.
        yesterday = BAR - timedelta(days=1)
        five = pd.concat([
            base.quiet_bars(yesterday, close=100.0).assign(Volume=[0.0]),
            five_minute_history(),
        ])
        self.five = five
        prior = pd.DataFrame({"Open": [100.0] * 5, "High": [100.01] * 5,
                              "Low": [99.99] * 5, "Close": [100.0] * 5,
                              "Volume": [321.0] * 5},
                             index=pd.DatetimeIndex([yesterday + timedelta(minutes=m)
                                                     for m in range(5)]))
        self.one_history = prior
        frames = scan1m.one_minute_frames({"ABC": "ABC.NS"}, BAR)
        self.assertEqual(frames["ABC.NS"].loc[yesterday, "Volume"], 321.0)    # one minute
        frame = scan1m.projected_frame(five, frames["ABC.NS"], BAR, self.now)
        self.assertEqual(frame.loc[yesterday, "Volume"], 1605.0)              # repaired
        # ... and the same frame is what the 5m scan would scan
        self.assertEqual(frame.iloc[:-1]["Volume"].tolist(),
                         data.apply_1m_volume(five, frames["ABC.NS"]).loc[frame.index[:-1], "Volume"].tolist())

    def test_confirmed_alert_carries_the_oi_line(self):
        self.minutes = one_minute(0, 1, 2, 3, 4, 5)
        reading = SimpleNamespace(oi_chg_pct=1.8, price_chg_pct=0.4, regime=SimpleNamespace(value="LONG_BUILDUP"))
        reader = SimpleNamespace(snapshot=lambda raw, bar: reading)
        with patch.object(scan1m, "_oi_reader", return_value=reader), \
             patch.object(scan1m, "_oi_mode", return_value="annotate"):
            out = scan1m.confirm_pass(BAR, now=CLOSE)
        self.assertEqual(len(out), 1)
        self.assertIn("OI <b>Long Build-up</b> · OI +1.80%", self.bot3.call_args.args[0])

    def test_opposite_side_and_missing_stop_are_recorded(self):
        self.minutes = one_minute(0, 1, 2, 3, 4, 5)
        with patch.object(scan1m, "scan_stock", side_effect=lambda r, f, d, keep_all=False: [base.signal()]), \
             patch.object(scan1m, "build_plan", return_value=None):
            self.assertEqual(scan1m.confirm_pass(BAR, now=CLOSE), [])          # EMA9 stop missing
        self.assertEqual(list(self.ledger()["stats"]["rejected"]["no_stop"]), ["ABC@10:00"])
        with patch.object(scan1m, "scan_stock",
                          side_effect=lambda r, f, d, keep_all=False: [base.signal(side="SELL")]):
            self.assertEqual(scan1m.confirm_pass(BAR, now=CLOSE), [])          # pick is BUY
        self.assertEqual(list(self.ledger()["stats"]["rejected"]["opposite"]), ["ABC@10:00"])
        self.bot3.assert_not_called()

    def test_a_candle_past_the_alert_deadline_is_never_confirmed(self):
        self.minutes = one_minute(0, 1, 2, 3, 4, 5)
        late = CLOSE + timedelta(seconds=400)          # MAX_ALERT_LAG_SECONDS = 180
        self.assertEqual(scan1m.confirm_pass(BAR, now=late), [])
        self.bot3.assert_not_called()

    def test_each_candle_is_counted_once(self):
        self.minutes = one_minute(0, 1, 2, 3, 4, 5)
        scan1m.confirm_pass(BAR, now=CLOSE)
        scan1m.confirm_pass(BAR, now=CLOSE + timedelta(seconds=20))
        stats = self.ledger()["stats"]
        self.assertEqual((stats["candles"], stats["picks"]), (1, 1))
        self.assertEqual(stats["sectors"], {"IT long": 1})

    def test_an_entry_without_a_projection_is_not_a_confirmed_projection(self):
        # 16 Nov: "Projected alerts 5 · of which confirmed at the close 11".
        # ``confirmed`` was bumped per entry, so a subset outgrew its parent set.
        self.minutes = one_minute(0, 1, 2, 3, 4, 5)
        self.assertEqual(len(scan1m.confirm_pass(BAR, now=CLOSE)), 1)   # an entry, no early alert
        stats = self.ledger()["stats"]
        self.assertEqual((stats.get("entries"), stats.get("confirmed", 0)), (1, 0))

    def test_a_feed_that_lags_the_close_is_counted_not_guessed(self):
        self.minutes = one_minute(0, 1, 2)                     # stops at minute 2
        later = BAR + timedelta(minutes=7)                     # 2 min past the close
        self.now = later                                       # both clocks agree
        self.assertEqual(scan1m.confirm_pass(BAR, now=later), [])
        text = self.bot3.call_args.args[0]                     # the 5m scan's notice
        self.assertIn("Yahoo feed is delayed", text)
        self.assertIn("two minutes after the 10:05 close", text)
        self.assertNotIn("BUY", text)
        self.assertEqual(self.bot3.call_count, 1)              # once, not per stock
        stats = self.ledger()["stats"]
        self.assertEqual((stats["feed_behind"], stats["feed_warned"]), (1, True))

    def test_summary_reports_rejects_and_feed(self):
        st = {"session": "2026-11-09",
              "stats": {"candles": 3, "picks": 2, "early": 2, "confirmed": 1, "cancelled": 1,
                        "sectors": {"IT long": 2}, "feed_behind": 1,
                        "rejected": {"opposite": {"X@10:00": "X 10:00"},
                                     "no_stop": {"Y@10:05": "Y 10:05"}}},
              "sent": {}}
        text = scan1m.format_summary(st)
        self.assertIn("Candles confirmed 3", text)
        self.assertIn("Projected alerts 2", text)
        self.assertIn("1 on the opposite side to the pick", text)
        self.assertIn("1 without a valid EMA9 stop", text)
        self.assertIn("feed lagged past the close on 1 candle", text)


class AlertCapTests(Bot3EntryRuleTests):
    """Four names can fire on one candle; the alert layer now ranks like the cap.

    17 Nov 09:21: 24 names scanned (FMCG + the `also scanning` PSUBK), 11
    projected, 3 ever taken. The entry cap existed; nothing capped the messages.
    """

    def test_the_default_keeps_the_three_best_of_a_four_name_candle(self):
        with patch.dict("os.environ", {"SCAN1M_ALERT_CAP": "3"}):
            scan1m.run_minute(BAR, now=MIN1)
        texts = [c.args[0] for c in self.bot3.call_args_list]
        self.assertEqual(len(texts), 3)
        self.assertEqual(len(self.ledger()["early"]), 3)
        self.assertIn("DDD@10:00", self.ledger()["stats"]["rejected"]["alert_cap"])
        self.assertEqual(self.ledger()["stats"]["alert_capped"], 1)

    def test_the_ranking_is_the_one_the_entries_use(self):
        # BBB is furthest ahead of its sector (+2pp); the rest are at +0pp, so
        # the tie falls to the score the way `rank_candidates` always decides it.
        from nse_scanner import pipeline
        leads = {self.LAST["BBB"]: 2.0}
        with patch.dict("os.environ", {"SCAN1M_ALERT_CAP": "2"}), \
                patch.object(pipeline, "sector_lead",
                             side_effect=lambda f, b, close, intra, long_side:
                             leads.get(close, 0.0)):
            scan1m.run_minute(BAR, now=MIN1)
        sent = [c.args[0] for c in self.bot3.call_args_list]
        self.assertEqual(len(sent), 2)
        self.assertTrue(any("BBB" in t for t in sent))
        self.assertFalse(any("DDD" in t for t in sent))

    def test_zero_restores_one_message_per_name(self):
        with patch.dict("os.environ", {"SCAN1M_ALERT_CAP": "0"}):
            scan1m.run_minute(BAR, now=MIN1)
        self.assertEqual(len(self.ledger()["early"]), 4)

    def test_the_summary_says_how_many_were_held_back(self):
        st = {"session": "2026-11-17",
              "stats": {"candles": 2, "picks": 1, "early": 3, "confirmed": 2,
                        "cancelled": 1, "alert_capped": 8},
              "sent": {}}
        self.assertIn("8 held back by the per-candle cap", scan1m.format_summary(st))


class EntryTargetTests(Bot3EntryRuleTests):
    """A trade with no pivot target at all has no exit — DABUR, 17 Nov, -0.8R."""

    def test_an_entry_without_any_target_is_refused(self):
        self.plan_for = lambda raw: base.TradePlan(
            self.LAST[raw], self.LAST[raw], self.LAST[raw] - 1.0, 1.0,
            None, None, None, ())
        out = scan1m.confirm_pass(BAR, now=CLOSE)
        self.assertEqual(out, [])
        self.assertIn("AAA@10:00", self.ledger()["stats"]["rejected"]["no_target"])
        self.bot3.assert_not_called()

    def test_the_old_rule_can_be_restored(self):
        self.plan_for = lambda raw: base.TradePlan(
            self.LAST[raw], self.LAST[raw], self.LAST[raw] - 1.0, 1.0,
            None, None, None, ())
        with patch.dict("os.environ", {"SCAN1M_REQUIRE_TARGET": "0"}):
            out = scan1m.confirm_pass(BAR, now=CLOSE)
        self.assertEqual(len(out), 3)                     # only the entry cap stops them


class CatchupDigestTests(TestCase):
    """A burst of late leaders is one message; a single one keeps its detail."""

    def test_a_burst_becomes_one_message(self):
        items = []
        for i, (raw, r) in enumerate((("BANKINDIA", -0.1), ("CANBK", 0.1),
                                      ("PSB", 0.0), ("BLUESTARCO", 0.4))):
            plan = base.TradePlan(100.0, 100.0, 99.0, 1.0, 101.0, 102.0, None, ("R1", "R2"))
            sig = base.StockSignal(raw, 1, "ENTRY", "BUY", f"09:2{i}", 100.0, 80,
                                   "Strong", 20, 1, "test", 99.0)
            items.append({"raw": raw, "sector": "PSUBK",
                          "hit": {"sig": sig, "plan": plan, "price": 100.0, "r": r, "age": 3.0}})
        text = scan1m.format_catchup_digest(items, {"sector": "PSUBK", "label": "BULL",
                                                    "conf": 56}, BAR, MIN1)
        self.assertIn("LATE LEADER BUY ×4", text)
        self.assertIn("BLUESTARCO", text.split("\n")[2])      # best first
        self.assertIn("Not entry alerts", text)
        self.assertEqual(len([ln for ln in text.split("\n") if ln.startswith("•")]), 4)
        self.assertNotIn("PSUBK", text.split("\n")[2])        # its own sector, no tag

    def test_a_different_sector_is_tagged(self):
        plan = base.TradePlan(100.0, 100.0, 99.0, 1.0, 101.0, None, None, ("R1",))
        sig = base.StockSignal("SUNTV", 1, "ENTRY", "BUY", "09:30", 100.0, 80, "Strong",
                               20, 1, "test", 99.0)
        item = {"raw": "SUNTV", "sector": "MEDIA",
                "hit": {"sig": sig, "plan": plan, "price": 100.0, "r": 0.2, "age": 4.0}}
        text = scan1m.format_catchup_digest([item, {**item, "raw": "ZEEL"}],
                                            {"sector": "REALTY", "label": "BULL", "conf": 61},
                                            BAR, MIN1)
        self.assertIn("SUNTV</b> · MEDIA", text)


class SummaryCounterTests(TestCase):
    """The summary block is the audit surface: its counters have to add up."""

    def test_confirmed_is_never_larger_than_the_projections_it_comes_from(self):
        line = scan1m._projected_line({"early": 5, "confirmed": 3, "cancelled": 1})
        self.assertEqual(line, "Projected alerts 5 · of which confirmed at the close 3"
                               " · did not hold 1 · still open 1")

    def test_the_residue_is_named_only_when_there_is_one(self):
        line = scan1m._projected_line({"early": 5, "confirmed": 3, "cancelled": 2})
        self.assertNotIn("still open", line)                # 5 = 3 + 2, nothing left over

    def test_entries_split_into_carried_and_close_only(self):
        line = scan1m._entries_line({"entries": 11, "confirmed": 3})
        self.assertEqual(line, "Entries (close-confirmed, sent to this bot) 11"
                               " · with an early alert 3 · close-only 8")

    def test_a_fully_carried_day_has_no_close_only_tail(self):
        line = scan1m._entries_line({"entries": 2, "confirmed": 2})
        self.assertEqual(line, "Entries (close-confirmed, sent to this bot) 2"
                               " · with an early alert 2")

    def test_a_zero_catch_up_line_says_why_it_was_zero(self):
        st = {"session": "2026-11-16",
              "stats": {"candles": 4, "picks": 1, "catchup": 0,
                        "catchup_skip": {"stop": 3, "none": 9, "chased": 2}},
              "sent": {}}
        text = scan1m.format_summary(st)
        self.assertIn("LATE LEADER catch-ups 0", text)
        self.assertIn("not sent: stop taken since the fire 3 · already ran (chase) 2"
                      " · no earlier fire 9", text)

    def test_skips_alone_still_print_the_late_leader_line(self):
        st = {"session": "2026-11-16", "stats": {"candles": 1, "picks": 1,
                                                "catchup_skip": {"stop": 1}}, "sent": {}}
        self.assertIn("LATE LEADER catch-ups 0 (signal fired before its sector was picked)"
                      " · not sent: stop taken since the fire 1", scan1m.format_summary(st))

    def test_a_refusal_names_its_reason(self):
        signal = base.signal(time="09:30")
        for frame, fires, want in ((None, [signal], "bar"),
                                   (five_minute_history(), [], "bar"),
                                   (five_minute_history(), [base.signal(side="SELL", time="09:30")],
                                    "none")):
            report = {}
            self.assertIsNone(scan1m.catchup_candidate(frame, fires, "BUY", BAR, CLOSE,
                                                       None, report=report))
            self.assertEqual(report["why"], want)

    def test_a_refusal_without_a_report_dict_still_refuses_quietly(self):
        self.assertIsNone(scan1m.catchup_candidate(None, [base.signal(time="09:30")],
                                                   "BUY", BAR, CLOSE, None))


class AlertParityTests(Scan1mFixture):
    """Bot 3 gets the whole 5m alert set, not just entries."""

    def open_trade(self, **over):
        st = self.ledger() or {"session": str(BAR.date()), "sent": {}, "positions": {}}
        pos = {"side": "BUY", "entry": 100.0, "sl": 99.0, "entry_ema9": 100.0,
               "t1": None, "t2": None, "t3": None,
               "entry_bar": (BAR - timedelta(minutes=10)).isoformat(),
               "last_bar": (BAR - timedelta(minutes=10)).isoformat()}
        pos.update(over)
        st.setdefault("positions", {})["ABC"] = pos
        st.setdefault("sent", {})
        state.save(st, self.dir / "scan1m.json")
        return st

    def test_target_hit_goes_to_bot3_only(self):
        self.open_trade(t1=100.008)                      # quiet bars touch 100.01
        self.minutes = one_minute(*range(0, 6))          # feed is settled past the close
        scan1m.track_positions(BAR)
        texts = [c.args[0] for c in self.bot3.call_args_list]
        hit = next(t for t in texts if "TARGET HIT" in t)
        self.assertIn("T1 100.01", hit)
        # T1 moves the stop to breakeven; the same fixture then trades back to
        # entry and the trade closes there, on bot 3, in the same pass.
        self.assertTrue(any("SL HIT" in t and "stop moved to breakeven" in t for t in texts))
        self.bot1.assert_not_called()

    def test_stop_hit_closes_the_trade_on_bot3(self):
        self.open_trade(sl=99.995)                       # quiet bars dip to 99.99
        self.minutes = one_minute(*range(0, 6))
        scan1m.track_positions(BAR)
        self.assertIn("SL", self.bot3.call_args.args[0])
        st = self.ledger()
        self.assertEqual(st["positions"], {})
        self.assertEqual(st["sent"]["ABC"]["code"], "SL")     # no re-entry, as on bot 1
        self.bot1.assert_not_called()

    def test_no_positions_no_message(self):
        self.assertEqual(scan1m.track_positions(BAR), [])
        self.bot3.assert_not_called()

    def test_hits_are_tracked_once_per_candle(self):
        self.minutes = one_minute(0, 1, 2, 3, 4, 5)
        with patch.object(scan1m, "track_positions", return_value=[]) as track:
            scan1m.confirm_pass(BAR, now=CLOSE)
            scan1m.confirm_pass(BAR, now=CLOSE + timedelta(seconds=20))
            scan1m.run_minute(BAR + timedelta(minutes=5), now=BAR + timedelta(minutes=6, seconds=5))
        self.assertEqual([c.args[0] for c in track.call_args_list], [BAR])

    def test_market_change_message_is_the_5m_one(self):
        md = SimpleNamespace(
            dir_state=1, dir_label="BULL", dir_conf=74, dir_tier="Strong", nifty=25000.0,
            nifty_day_ret=0.6, nifty_intra=0.2, vix=13.2, vix_chg=-1.5, adx=27.0, mtf_bull=3,
            breadth_adv=28, breadth_dec=12, regime="TREND", persist_plus=[("IT", 82.0)],
            persist_minus=[("PSU", 12.0)], live_flow="BUY", live_flow_qualified=True,
            live_flow_score=71.0, pick_sector="IT", pick_side=1, pick_reason="PERSIST + IT",
            action="SCAN LONG", bar_time="2026-11-09 10:00")
        scan1m._md_cache.clear()
        scan1m._md_cache[md.bar_time] = md
        st = {"session": str(BAR.date()), "sent": {}, "stats": {}}
        self.assertTrue(scan1m.market_alert(st, {"bar": md.bar_time, "sector": "IT", "side": 1}))
        text = self.bot3.call_args.args[0]
        self.assertIn("MARKET BULL", text)
        self.assertIn("SECTOR PICK: IT", text)
        self.assertEqual(st["sector"]["name"], "IT")
        self.bot3.reset_mock()
        self.assertFalse(scan1m.market_alert(st, {"bar": md.bar_time}))   # unchanged: silent
        self.bot3.assert_not_called()


class KnobTests(Scan1mFixture):
    """The three knobs an operator actually turns."""

    def test_first_minute_4_moves_the_projection_to_the_last_minute(self):
        self.minutes = one_minute(0, 1, 2, 3, 4, 5)
        settled = patch.object(scan1m, "_judged_bar",
                               return_value=(BAR - timedelta(minutes=5)).isoformat())
        settled.start()
        self.addCleanup(settled.stop)
        with patch.dict("os.environ", {"SCAN1M_FIRST_MINUTE": "4"}):
            self.assertEqual(scan1m.tick(MIN1), "idle")           # too early to project
            self.assertEqual(scan1m.run_minute(BAR, now=MIN1), [])
            self.bot3.assert_not_called()
            late = BAR + timedelta(minutes=4, seconds=5)
            self.assertEqual(scan1m.tick(late), "project")
            self.assertEqual(len(scan1m.run_minute(BAR, now=late)), 1)
        self.assertIn("0m 55s left", self.bot3.call_args.args[0])

    def test_first_minute_is_clamped_to_the_candle(self):
        with patch.dict("os.environ", {"SCAN1M_FIRST_MINUTE": "0"}):
            self.assertEqual(scan1m.first_minute(), 1)
        with patch.dict("os.environ", {"SCAN1M_FIRST_MINUTE": "9"}):
            self.assertEqual(scan1m.first_minute(), 4)
        with patch.dict("os.environ", {"SCAN1M_FIRST_MINUTE": "junk"}):
            self.assertEqual(scan1m.first_minute(), 1)

    def test_cancel_message_reports_the_close_instead_of_silence(self):
        with patch.dict("os.environ", {"SCAN1M_CANCEL_MESSAGE": "1"}):
            self.assertEqual(len(scan1m.run_minute(BAR, now=MIN1)), 1)     # projected
            self.minutes = one_minute(0, 1, 2, 3, 4, 5)
            self.fires = False                                             # close fails
            self.assertEqual(scan1m.confirm_pass(BAR, now=CLOSE), [])
        text = self.bot3.call_args.args[0]
        self.assertIn("EARLY CANCELLED", text)
        self.assertIn("projected 10:01:05 @ 100.00", text)
        self.assertIn("close 100.00", text)
        self.assertIn("no entry recorded", text)

    def test_cancel_message_off_stays_silent(self):
        self.assertEqual(len(scan1m.run_minute(BAR, now=MIN1)), 1)
        self.minutes = one_minute(0, 1, 2, 3, 4, 5)
        self.fires = False
        self.bot3.reset_mock()
        self.assertEqual(scan1m.confirm_pass(BAR, now=CLOSE), [])
        self.bot3.assert_not_called()
        self.assertEqual(self.ledger()["early"]["ABC"]["result"], "cancelled")


class RetryTests(Scan1mFixture):
    """A candle the feed had not finished is retried until the deadline."""

    def test_an_unfinished_candle_is_retried_even_without_a_projection(self):
        self.minutes = one_minute(0, 1, 2)                     # newest print 10:02
        self.assertEqual(scan1m.confirm_pass(BAR, now=CLOSE), [])
        self.assertEqual(self.ledger()["retry_bar"], BAR.isoformat())
        self.assertEqual(scan1m.tick(BAR + timedelta(minutes=6)), "retry")   # tries again
        self.minutes = one_minute(0, 1, 2, 3, 4, 5)            # feed caught up
        later = BAR + timedelta(minutes=6, seconds=20)
        self.now = later
        self.assertEqual(len(scan1m.confirm_pass(BAR, now=later)), 1)
        self.assertNotIn("retry_bar", self.ledger())

    def test_the_retry_stops_at_the_alert_deadline(self):
        self.minutes = one_minute(0, 1, 2)
        self.assertEqual(scan1m.confirm_pass(BAR, now=CLOSE), [])
        too_late = BAR + timedelta(minutes=4)                  # > 180 s after the close
        self.assertNotEqual(scan1m.tick(too_late), "retry")


class ForceSectorTests(TestCase):
    """``--sector IT`` behaves like ``run_once(force_sector=...)``."""

    def setUp(self):
        scan1m._pick_cache.clear()
        scan1m._md_cache.clear()
        self.now = BAR + timedelta(minutes=6)
        md = SimpleNamespace(dir_state=-1, dir_label="BEAR", dir_conf=60, pick_sector="PSU",
                             pick_side=-1, pick_reason="PERSIST + PSU", action="SCAN SHORT",
                             regime="TREND", bar_time=BAR.strftime("%Y-%m-%d %H:%M"),
                             extra_sectors=())
        self.uni = {"nifty": "NIFTY", "vix": "VIX",
                    "sectors": {"IT": {"index": "IDX"}, "PSU": {"index": "IDX2"}}}
        self.patches = [
            patch.object(pipeline, "now_ist", side_effect=lambda: self.now),
            patch.object(scan1m, "now_ist", side_effect=lambda: self.now),
            patch.object(scan1m.data, "load_universe", return_value=self.uni),
            patch.object(scan1m.data, "fetch_symbol", side_effect=lambda *a: {
                "m5": five_minute_history(), "d1": None}),
            patch.object(scan1m.data, "fetch_many", side_effect=lambda t, p, i: {x: None for x in t}),
            patch.object(scan1m.data, "fetch_daily", return_value={}),
            patch.object(scan1m, "run_market", return_value=md),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_the_market_pick_is_used_without_a_forced_sector(self):
        pick = scan1m._own_pick(BAR)
        self.assertEqual((pick["sector"], pick["side"]), ("PSU", -1))

    def test_a_forced_sector_overrides_it_like_run_once(self):
        pick = scan1m._own_pick(BAR, "IT")
        self.assertEqual((pick["sector"], pick["side"]), ("IT", -1))   # dir_state is BEAR
        self.assertEqual(scan1m._md_cache[pick["bar"]].pick_reason, "FORCED IT")

    def test_an_unknown_forced_sector_is_ignored(self):
        self.assertEqual(scan1m._own_pick(BAR, "NOPE")["sector"], "PSU")


class CliTests(TestCase):
    def test_once_1m_passes_the_forced_sector(self):
        import contextlib
        import io

        from nse_scanner import __main__ as cli, scan1m, session

        now = BAR + timedelta(seconds=6)                 # minute 0: the close pass
        with patch.object(session, "now_ist", return_value=now), \
             patch.object(scan1m, "_bar_open", return_value=BAR), \
             patch.object(scan1m, "confirm_pass", return_value=[]) as confirm, \
             patch.object(scan1m, "run_minute") as project:
            out = io.StringIO()
            with patch.dict("os.environ"):        # main(--dry-run) sets DRY_RUN for the run
                with contextlib.redirect_stdout(out):
                    self.assertEqual(cli.main(["once-1m", "--dry-run", "--sector", "IT"]), 0)
        confirm.assert_called_once_with(BAR - timedelta(minutes=5), now=now, force_sector="IT")
        project.assert_not_called()
        self.assertIn("minute 0", out.getvalue())

    def test_backfill_is_refused_for_the_1m_scan(self):
        from nse_scanner import __main__ as cli

        with patch.dict("os.environ"):
            with self.assertRaises(SystemExit):
                cli.main(["once-1m", "--backfill", "--dry-run"])
            with self.assertRaises(SystemExit):
                cli.main(["live-1m", "--backfill", "--dry-run"])


class _Stop(Exception):
    """Ends a fake loop run once the scripted iterations are done."""


class LoopTests(TestCase):
    """``live_loop_1m`` itself — the entry point production actually runs."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        scan1m._pick_cache.clear()
        scan1m._one_cache.clear()
        scan1m._five_cache.clear()
        scan1m._md_cache.clear()
        scan1m._last_nifty_bar = None
        self.env = patch.dict("os.environ", {"DRY_RUN": "0", "SCAN1M_SUMMARY": "1",
                                             "SCAN1M_FIRST_MINUTE": "1"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.patches = [
            patch.object(scan1m, "LEDGER", self.dir / "scan1m.json"),
            patch.object(state, "STATE_PATH", self.dir / "scan_state.json"),
            patch.object(scan1m.data, "enable_daily_cache"),
            patch.object(telegram, "send_bot3", return_value=True),
        ]
        self.mocks = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)
        self.bot3 = self.mocks[3]

    def run_loop(self, start, *, minutes=4, step_minutes=1, exit_at_close=False,
                 max_minutes=None, force_sector=None):
        """Run the real loop with a moving clock; stop it after ``minutes`` cycles."""
        clock = SimpleNamespace(t=start)
        ticks = {"n": 0}

        def sleep_until(_target):
            ticks["n"] += 1
            if ticks["n"] > minutes:
                raise _Stop
            clock.t = clock.t + timedelta(minutes=step_minutes)

        def fake_confirm(bar, *, now=None, force_sector=None):
            st = scan1m.load_ledger()
            scan1m._mark_judged(st, bar)        # what the real pass does when complete
            scan1m.save_ledger(st)
            return []

        with patch.object(scan1m, "now_ist", side_effect=lambda: clock.t), \
             patch.object(scan1m, "_sleep_until", side_effect=sleep_until), \
             patch("nse_scanner.session.is_weekday", return_value=True), \
             patch.object(scan1m, "_announce") as announce, \
             patch.object(scan1m, "confirm_pass", side_effect=fake_confirm) as confirm, \
             patch.object(scan1m, "run_minute", return_value=[]) as project:
            try:
                reason = scan1m.live_loop_1m(max_minutes=max_minutes,
                                             exit_at_close=exit_at_close,
                                             force_sector=force_sector)
            except _Stop:
                reason = "scripted"
        return SimpleNamespace(reason=reason, announce=announce, confirm=confirm,
                               project=project, clock=clock)

    def test_the_loop_judges_the_close_then_projects_the_forming_candle(self):
        run = self.run_loop(BAR + timedelta(minutes=0, seconds=6))      # 10:00:06
        self.assertEqual(run.announce.call_count, 1)
        # the 09:55 candle is owed a close pass before anything is projected
        self.assertEqual(run.confirm.call_count, 1)
        self.assertEqual(run.confirm.call_args.args[0], BAR - timedelta(minutes=5))
        self.assertEqual([c.args[0] for c in run.project.call_args_list], [BAR] * 4)

    def test_a_close_with_no_projection_still_runs_in_the_loop(self):
        with patch.object(scan1m, "unresolved", return_value=set()):
            run = self.run_loop(BAR + timedelta(minutes=5, seconds=6))  # 10:05:06
        self.assertEqual(run.confirm.call_count, 1)
        self.assertEqual(run.confirm.call_args.args[0], BAR)            # the 10:00 candle

    def test_the_projection_survives_a_confirm_in_the_same_minute(self):
        run = self.run_loop(BAR + timedelta(minutes=5, seconds=6))      # 10:05:06
        self.assertEqual(run.confirm.call_count, 1)
        self.assertEqual(run.project.call_count, 4)                     # not lost to the close
        self.assertEqual(run.project.call_args_list[0].args[0], BAR + timedelta(minutes=5))

    def test_the_forced_sector_reaches_both_passes(self):
        run = self.run_loop(BAR + timedelta(minutes=5, seconds=6), minutes=1, force_sector="IT")
        self.assertEqual(run.confirm.call_args.kwargs["force_sector"], "IT")
        self.assertEqual(run.project.call_args.kwargs["force_sector"], "IT")

    def test_the_session_summary_is_sent_once_and_remembered(self):
        state.save({"session": str(BAR.date()), "sent": {}, "positions": {},
                    "stats": {"candles": 3, "picks": 2}}, self.dir / "scan1m.json")
        start = BAR + timedelta(hours=5, minutes=31, seconds=50)        # 15:31:50
        run = self.run_loop(start, minutes=3, step_minutes=1, exit_at_close=True,
                            max_minutes=None)
        # Every cycle before 15:33 does nothing; the first one after it summarises
        # and hands back "closed".
        self.assertIn(run.reason, ("closed", "scripted"))
        texts = [c.args[0] for c in self.bot3.call_args_list]
        self.assertEqual(sum("1m projected scan" in t for t in texts), 1)
        self.assertIn("Candles confirmed 3", texts[-1])
        self.assertTrue(state.load(self.dir / "scan1m.json")["stats"]["reported"])

    def test_the_budget_hands_over(self):
        run = self.run_loop(BAR, minutes=3, step_minutes=1, max_minutes=1.0)
        self.assertEqual(run.reason, "budget")


class CliHandoffTests(TestCase):
    def test_a_budget_exit_writes_the_handoff_marker(self):
        import contextlib
        import io

        from nse_scanner import __main__ as cli, scan1m

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh_output"
            with patch.object(scan1m, "live_loop_1m", return_value="budget"), \
                 patch.dict("os.environ", {"GITHUB_OUTPUT": str(out)}), \
                 patch.dict("os.environ"):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["live-1m", "--dry-run"]), 0)
            self.assertIn("handoff=true", out.read_text())
            out.unlink()
            with patch.object(scan1m, "live_loop_1m", return_value="closed"), \
                 patch.dict("os.environ", {"GITHUB_OUTPUT": str(out)}), \
                 patch.dict("os.environ"):
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.main(["live-1m", "--dry-run"])
            self.assertIn("handoff=false", out.read_text())


class HolidayTests(TestCase):
    def tearDown(self):
        scan1m._last_nifty_bar = None

    def test_a_day_without_an_nse_bar_is_a_holiday(self):
        scan1m._last_nifty_bar = BAR - timedelta(days=4)        # last bar was last week
        self.assertTrue(scan1m._session_closed(BAR))
        scan1m._last_nifty_bar = BAR                            # today's bar exists
        self.assertFalse(scan1m._session_closed(BAR + timedelta(minutes=5)))
        scan1m._last_nifty_bar = None                           # nothing seen yet
        self.assertFalse(scan1m._session_closed(BAR))


class MarketSourceTests(TestCase):
    """SCAN1M_MARKET_SOURCE=ledger vs own — with no market fetch in ledger mode."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        scan1m._pick_cache.clear()
        scan1m._ledger_warned.clear()
        self.env = patch.dict("os.environ", {"DRY_RUN": "0", "SCAN1M_MARKET_SOURCE": "ledger"},
                              clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        for p in (patch.object(state, "STATE_PATH", self.dir / "scan_state.json"),
                  patch.object(scan1m, "_own_pick",
                               side_effect=AssertionError("own pick must not run in ledger mode"))):
            p.start()
            self.addCleanup(p.stop)
        self.scan5 = {"bar": (BAR - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M"),
                      "sector": "IT", "side": 1, "label": "BULL", "conf": 74,
                      "extra": [], "follow": False}

    def test_ledger_pick_is_reused_for_the_projection(self):
        state.save({"session": str(BAR.date()), "scan": self.scan5, "sent": {}}, state.STATE_PATH)
        pick = scan1m.pick_for(BAR, projection=True)
        self.assertEqual(pick["sector"], "IT")
        self.assertEqual(pick["side"], 1)
        self.assertEqual(pick["bar"], self.scan5["bar"])

    def test_ledger_pick_of_another_candle_is_not_reused(self):
        stale = {**self.scan5, "bar": (BAR - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")}
        state.save({"session": str(BAR.date()), "scan": stale, "sent": {}}, state.STATE_PATH)
        with self.assertLogs("nse_scanner.scan1m", level="WARNING"):
            self.assertIsNone(scan1m.pick_for(BAR, projection=True))

    def test_missing_ledger_says_why_and_only_once_per_candle(self):
        with self.assertLogs("nse_scanner.scan1m", level="WARNING") as logs:
            self.assertIsNone(scan1m.pick_for(BAR, projection=True))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("does not exist", logs.output[0])
        self.assertIn("SCAN1M_MARKET_SOURCE=own", logs.output[0])
        with self.assertNoLogs("nse_scanner.scan1m", level="WARNING"):
            scan1m._warn_ledger_once(BAR - timedelta(minutes=5))   # same candle: already said
        with self.assertLogs("nse_scanner.scan1m", level="WARNING"):
            scan1m._warn_ledger_once(BAR + timedelta(minutes=5))


class DoctorTests(TestCase):
    def test_doctor_shows_the_knobs_before_the_session_starts(self):
        from nse_scanner import __main__ as cli

        uni = {"nifty": "NIFTY", "vix": "VIX", "sectors": {}}
        with patch.dict("os.environ", {"SCAN1M_FIRST_MINUTE": "2", "SCAN1M_MARKET_SOURCE": "ledger",
                                       "SCAN1M_CANCEL_MESSAGE": "1"}, clear=False), \
             patch.object(data, "load_universe", return_value=uni), \
             patch.object(data, "fetch_symbol", return_value={"m5": pd.DataFrame()}), \
             patch.object(telegram, "config_from_env", return_value=("", "")), \
             patch.object(telegram, "secondary_config_from_env", return_value=("", "")), \
             patch.object(telegram, "third_config_from_env", return_value=("", "")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli._doctor()
        text = out.getvalue()
        self.assertIn("first minute 2 (up to 3 min early)", text)
        self.assertIn("market source ledger", text)
        self.assertIn("cancel messages on", text)


class SchedulerTests(TestCase):
    """The minute router: the close first, then the projection."""

    def setUp(self):
        self.env = patch.dict("os.environ", {"SCAN1M_FIRST_MINUTE": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for p in (patch.object(scan1m, "LEDGER", self.dir / "scan1m.json"),
                  patch.object(scan1m, "now_ist", return_value=BAR + timedelta(minutes=1))):
            p.start()
            self.addCleanup(p.stop)

    def at(self, minutes, seconds=6):
        return BAR + timedelta(minutes=minutes, seconds=seconds)

    def judge(self, bar, retry=False):
        """Pretend ``bar`` has been through the close pass (or is still owed)."""
        st = {"session": str(bar.date()), "sent": {}, "stats": {}}
        if retry:
            st["retry_bar"] = bar.isoformat()      # tried, feed had not published
        else:
            st["stats"]["judged_bar"] = bar.isoformat()
        state.save(st, self.dir / "scan1m.json")

    def test_the_close_is_judged_before_anything_is_projected(self):
        # 10:01:06: the 09:55 candle closed 66 s ago and has not been judged.
        self.assertEqual(scan1m.tick(self.at(1)), "confirm")
        self.judge(BAR - timedelta(minutes=5))
        self.assertEqual(scan1m.tick(self.at(1)), "project")      # T−4
        self.assertEqual(scan1m.tick(self.at(4)), "project")      # T−1

    def test_a_close_with_no_projection_is_still_judged(self):
        """The bug this scheduler had: no projection ⇒ no close pass at all."""
        self.assertEqual(scan1m.unresolved(BAR - timedelta(minutes=5)), set())
        self.assertEqual(scan1m.tick(self.at(1)), "confirm")

    def test_a_judged_close_is_not_judged_again(self):
        self.judge(BAR - timedelta(minutes=5))
        self.assertNotIn(scan1m.tick(self.at(1)), ("confirm", "retry"))

    def test_an_unfinished_close_is_retried(self):
        self.judge(BAR - timedelta(minutes=5), retry=True)
        self.assertEqual(scan1m.tick(self.at(1)), "retry")
        self.assertEqual(scan1m.tick(self.at(2)), "retry")

    def test_the_close_stops_being_owed_at_the_alert_deadline(self):
        # 10:08:06 is 186 s after the 10:00 close: too late to judge it.
        self.assertNotIn(scan1m.tick(self.at(3, seconds=6)), ("confirm", "retry"))

    def test_first_minute_is_respected_once_the_close_is_settled(self):
        self.judge(BAR - timedelta(minutes=5))
        with patch.dict("os.environ", {"SCAN1M_FIRST_MINUTE": "3"}):
            self.assertEqual(scan1m.tick(self.at(1)), "idle")
            self.assertEqual(scan1m.tick(self.at(3)), "project")

    def test_nothing_to_do_after_the_session(self):
        late = BAR.replace(hour=15, minute=40, second=6)
        self.assertEqual(scan1m.tick(late), "idle")


class SessionWindowTests(TestCase):
    """The candles the 1m scan judges are the candles the 5m scan judges."""

    def test_the_last_candle_of_the_day_is_not_dropped(self):
        for minute, expected in ((14, False), (15, True), (30, True), (55, True)):
            bar = BAR.replace(hour=9, minute=minute)
            self.assertIs(scan1m._in_session(bar), expected, f"09:{minute:02d}")
        for minute, expected in ((15, True), (20, True), (25, True), (30, False)):
            bar = BAR.replace(hour=15, minute=minute)
            self.assertIs(scan1m._in_session(bar), expected, f"15:{minute:02d}")

    def test_the_same_window_as_the_5m_scan(self):
        from nse_scanner import pipeline
        for hh, mm in ((9, 15), (15, 20), (15, 25)):
            bar = BAR.replace(hour=hh, minute=mm)
            self.assertTrue(scan1m._in_session(bar))
            self.assertTrue(clock_time(hh, mm) >= clock_time(9, 15)
                            and clock_time(hh, mm) <= clock_time(15, 25))
        self.assertFalse(scan1m._in_session(BAR.replace(hour=15, minute=30)))
        # pipeline judges closed bars up to 15:25 (its ``_run_until_ready`` guard)
        self.assertIn("clock_time(15, 25)", inspect.getsource(pipeline._run_until_ready))


class ProjectionInvariantTests(TestCase):
    def test_a_complete_projection_is_identical_to_the_closed_candle(self):
        """When the 1m prints cover the candle, projecting changes nothing.

        That is the whole promise of the mode: the same numbers, earlier. Any
        drift here would mean the projection is not the same engine state.
        """
        five = five_minute_history()
        minutes = pd.DataFrame(
            {"Open": [100.0] * 6, "High": [100.01] * 6, "Low": [99.99] * 6,
             "Close": [99.995, 100.0, 100.005, 100.0, 100.0, 100.0], "Volume": [200.0] * 6},
            index=pd.DatetimeIndex([BAR + timedelta(minutes=m) for m in range(6)]),
        )
        projected = scan1m.projected_frame(five, minutes, BAR, BAR + timedelta(minutes=4, seconds=5))
        closed = scan1m.closed_frame(five, minutes, BAR)
        assert_frame_equal(projected, closed)
        self.assertEqual(projected.iloc[-1]["Volume"], 1000.0)


class EngineWiringTests(TestCase):
    def test_the_projected_scan_runs_the_unmodified_5m_engine(self):
        from nse_scanner.trade import build_plan

        self.assertIs(scan1m.scan_stock, stock.scan_stock)
        self.assertIs(scan1m.build_plan, build_plan)


class Bot3TelegramTests(TestCase):
    def test_send_bot3_uses_only_the_third_bot(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN_3": "333:three",
                                       "TELEGRAM_CHAT_ID_3": "999", "DRY_RUN": "0"}), \
             patch.object(telegram, "_send_to", return_value=True) as send_to:
            self.assertTrue(telegram.send_bot3("hello"))
        send_to.assert_called_once()
        args, kwargs = send_to.call_args
        self.assertEqual(args[0], "333:three")
        self.assertEqual(args[1], "999")
        self.assertEqual(kwargs["label"], "bot3")

    def test_missing_bot3_config_does_not_fall_back_to_bot1(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN_3": "", "TELEGRAM_CHAT_ID_3": "",
                                       "TELEGRAM_BOT_TOKEN": "111:one", "TELEGRAM_CHAT_ID": "1",
                                       "DRY_RUN": "0"}), \
             patch.object(telegram, "_send_to") as send_to:
            self.assertFalse(telegram.send_bot3("hello"))
        send_to.assert_not_called()

    def test_primary_send_is_unchanged(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "111:one", "TELEGRAM_CHAT_ID": "1",
                                       "TELEGRAM_BOT_TOKEN_2": "", "TELEGRAM_CHAT_ID_2": "",
                                       "DRY_RUN": "0"}), \
             patch.object(telegram, "_send_to", return_value=True) as send_to:
            self.assertTrue(telegram.send("hi"))
        self.assertEqual(send_to.call_count, 1)
        self.assertEqual(send_to.call_args.kwargs["label"], "primary")


class CatchupWindowTests(TestCase):
    """Catch-ups run around a pick change, for ten minutes — and only then."""

    pick = {"sector": "IT", "side": 1}

    def bar(self, minutes):
        return BAR + timedelta(minutes=minutes)

    def test_the_window_is_anchored_to_the_first_minute_of_the_pick(self):
        st = {}
        self.assertTrue(scan1m.catchup_due(st, self.pick, self.bar(0)))
        since = st["catchup_pick"]["since"]
        self.assertTrue(scan1m.catchup_due(st, self.pick, self.bar(5)))
        self.assertEqual(st["catchup_pick"]["since"], since)       # never re-anchored
        self.assertTrue(scan1m.catchup_due(st, self.pick, self.bar(10)))
        self.assertFalse(scan1m.catchup_due(st, self.pick, self.bar(11)))

    def test_a_new_pick_opens_a_new_window(self):
        st = {}
        scan1m.catchup_due(st, self.pick, self.bar(0))
        self.assertTrue(scan1m.catchup_due(st, {**self.pick, "sector": "FMCG"}, self.bar(30)))
        self.assertEqual(st["catchup_pick"]["key"], "FMCG|1")

    def test_the_switch_can_turn_it_off(self):
        with patch.dict("os.environ", {"SCAN1M_LEADER_CATCHUP": "0"}):
            self.assertFalse(scan1m.catchup_due({}, self.pick, self.bar(0)))
