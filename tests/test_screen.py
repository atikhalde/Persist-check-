"""Bot 4, the name-first screen: the rules that make its short list trustworthy.

The screen exists because three sessions of logs show the 5m engine refusing whole
sector books for a mechanical reason ("not fired on this candle") while the *state*
of those names was exactly what the day's best trades were made of. So the rules
these tests pin down are the ones that keep the replacement honest:

* **both sides** every candle — a short sector produces short setups with mirrored
  levels, not silence;
* every gate is a **number**, and the number that refused a name is counted, so
  tomorrow's PDF can say why a candle surfaced nothing;
* **the cap and the cooldown** — at most SCREEN_MAX_DAY setups, at most
  SCREEN_CAP_PER_SECTOR per sector, no name twice;
* **nothing is entered**: the card says "Not an entry alert", the ledger holds no
  position, and a bot-4 refusal leaves the setup unsent rather than falling back
  to bots 1/2;
* each surfaced setup is **tracked from its own bar** (not the day's high) and the
  stop wins when one candle touches both;
* the EOD audit is a real PDF, and the caption that carries it is valid Telegram
  HTML (no bare ``<``).
"""

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from nse_scanner import data, screen, screen_pdf, telegram

IST = ZoneInfo("Asia/Kolkata")
START = datetime(2026, 11, 20, 9, 15, tzinfo=IST)


def candle(n: int) -> datetime:
    """The open time of the ``n``-th five-minute candle of the session."""
    return START + timedelta(minutes=5 * n)


def frame(closes, *, wick: float = 0.001, end: int | None = None) -> pd.DataFrame:
    """A 5m frame of ``closes`` starting at 09:15, one row per candle.

    The wick is deliberately 0.1%: ``SCREEN_NEAR_HIGH_PCT`` is 0.20%, so the last
    close is *at* its session extreme on this tape unless a test says otherwise.
    """
    idx = [candle(i) for i in range(len(closes))]
    out = pd.DataFrame({"Open": closes,
                        "High": [c * (1 + wick) for c in closes],
                        "Low": [c * (1 - wick) for c in closes],
                        "Close": closes,
                        "Volume": [1000] * len(closes)}, index=idx)
    return out if end is None else out.iloc[:end + 1]


def rising(start: float, step: float, n: int, end: int | None = None) -> pd.DataFrame:
    """A frame that trends steadily up — close above EMA9, at its own high."""
    return frame([start + step * i for i in range(n)], end=end)


def falling(start: float, step: float, n: int, end: int | None = None) -> pd.DataFrame:
    return frame([start - step * i for i in range(n)], end=end)


def payload(hhmm="10:00", *, sectors=None, tilt=1) -> dict:
    """One board candle, in the shape ``board.sweep`` returns."""
    return {"b": hhmm, "scan": hhmm, "tilt": tilt, "pick": "AUTO",
            "sectors": sectors if sectors is not None else {
                "AUTO": {"side": 1, "bull": 92, "bear": 8, "persist": 92,
                         "day": 4.29, "intra": 0.37, "gate": True, "pick": True,
                         "n": ["MARUTI"], "rank": [88.0], "s": [78], "v": [80],
                         "t": [85], "lead": [0.4], "mv": [3.1], "rk": [1]}}}


def md(label="BULL", conf=70, side=1, regime="TREND"):
    return SimpleNamespace(dir_label=label, dir_conf=conf, dir_state=side,
                           pick_sector="AUTO", regime=regime)


class ScreenCase(TestCase):
    """A screen test with its own ledger, so no test can see another's day."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "screen_state.json"
        p = patch.object(screen, "STATE_PATH", self.state)
        p.start()
        self.addCleanup(p.stop)
        self.sent: list[str] = []
        self.env = patch.dict(os.environ, {"SCREEN_BOT": "1", "DRY_RUN": "0"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def send(self, text, **kw):
        self.sent.append(text)
        return True

    def st(self):
        return screen.load()

    def run_candle(self, pl, frames, *, at=10, market=None):
        return screen.consider(self.st(), pl, frames, market or md(),
                               candle(at) if not isinstance(at, datetime) else at,
                               send=self.send, save_state=screen.save,
                               now=datetime(2026, 11, 20, 10, 0,
                                            tzinfo=IST) + timedelta(minutes=5 * at))


class SideTests(ScreenCase):
    def test_a_long_setup_carries_long_levels(self):
        frames = {data.yahoo_stock("MARUTI"): rising(10_000, 40, 12)}
        out = self.run_candle(payload(), frames, at=11)
        self.assertEqual(len(out), 1)
        setup = out[0]
        self.assertEqual(setup["side"], "BUY")
        self.assertLess(setup["sl"], setup["entry"])          # stop below entry
        if setup["t1"] is not None:
            self.assertGreater(setup["t1"], setup["entry"])

    def test_the_same_tape_screens_short_with_mirrored_levels(self):
        sec = {"BANK": {"side": -1, "bull": 6, "bear": 94, "persist": 94,
                        "day": -2.10, "intra": -0.55, "gate": True, "pick": True,
                        "n": ["HDFCBANK"], "rank": [80.0], "s": [72], "v": [77],
                        "t": [81], "lead": [-0.9], "mv": [-1.8], "rk": [1]}}
        frames = {data.yahoo_stock("HDFCBANK"): falling(1_600, 9, 12)}
        out = self.run_candle(payload(sectors=sec, tilt=-1), frames, at=11)
        self.assertEqual(len(out), 1)
        setup = out[0]
        self.assertEqual(setup["side"], "SELL")
        self.assertGreater(setup["sl"], setup["entry"])       # stop above entry
        if setup["t1"] is not None:
            self.assertLess(setup["t1"], setup["entry"])

    def test_both_sides_can_be_surfaced_on_one_candle(self):
        sectors = payload()["sectors"]
        sectors["BANK"] = {"side": -1, "bull": 6, "bear": 94, "persist": 94,
                           "day": -2.1, "intra": -0.55, "gate": True, "pick": False,
                           "n": ["HDFCBANK"], "rank": [80.0], "s": [72], "v": [77],
                           "t": [81], "lead": [-0.9], "mv": [-1.8], "rk": [1]}
        frames = {data.yahoo_stock("MARUTI"): rising(10_000, 40, 12),
                  data.yahoo_stock("HDFCBANK"): falling(1_600, 9, 12)}
        with patch.dict(os.environ, {"SCREEN_MAX_DAY": "6"}):
            out = self.run_candle(payload(sectors=sectors), frames, at=11)
        self.assertEqual({s["side"] for s in out}, {"BUY", "SELL"})
        self.assertEqual(len({s["name"] for s in out}), 2)


class GateTests(ScreenCase):
    """Each gate is a number, and the refusal is counted against that number."""

    def refuses(self, *, sectors=None, frames=None, gate, market=None, at=11):
        frames = ({data.yahoo_stock("MARUTI"): rising(10_000, 40, 12)}
                  if frames is None else frames)
        out = self.run_candle(payload(sectors=sectors), frames, at=at, market=market)
        self.assertEqual(out, [], "the name should have been refused")
        refused = self.st()["stats"]["refused"]
        self.assertIn(gate, refused, f"refused {refused}, expected {gate}")
        return refused[gate]

    def test_low_sector_persistence_is_refused_and_counted(self):
        sec = payload()["sectors"]
        sec["AUTO"]["persist"] = 55
        self.refuses(sectors=sec, gate="sector persist below floor")

    def test_a_falling_sector_is_refused(self):
        """Persistence 92 -> 92 -> 85: still above the floor, but falling."""
        sec = payload()["sectors"]
        frames = {data.yahoo_stock("MARUTI"): rising(10_000, 40, 12)}
        with patch.dict(os.environ, {"SCREEN_SLOPE_BARS": "1", "SCREEN_SLOPE_MIN": "1"}):
            for hhmm, at in (("09:55", 8), ("10:00", 9)):
                screen.consider(self.st(), payload(hhmm, sectors=dict(sec)), frames, md(),
                                candle(at), send=self.send, save_state=screen.save)
            sec2 = {k: dict(v) for k, v in sec.items()}
            sec2["AUTO"]["persist"] = 85
            out = screen.consider(self.st(), payload("10:05", sectors=sec2), frames, md(),
                                  candle(10), send=self.send, save_state=screen.save)
        self.assertEqual(out, [])
        self.assertIn("sector persistence falling", self.st()["stats"]["refused"])

    def test_a_sector_moving_against_its_side_is_refused(self):
        sec = payload()["sectors"]
        sec["AUTO"]["intra"] = -0.40          # side says up, the tape says down
        self.refuses(sectors=sec, gate="sector moving against its side")

    def test_a_name_not_moving_its_sector_way_is_refused(self):
        """Still above EMA9 (so it has levels) but it has left its high and turned down.

        A name *below* its EMA9 for the long side never reaches this gate: the plan
        itself refuses it first, which is why the falling-frame case counts as
        "no levels" and not as a tape refusal.
        """
        closes = [10_000 + 40 * i for i in range(11)] + [10_360]
        self.refuses(frames={data.yahoo_stock("MARUTI"): frame(closes)},
                     gate="name not moving its sector's way")

    def test_low_structure_is_refused_with_the_floor_named(self):
        sec = payload()["sectors"]
        sec["AUTO"]["t"] = [58]
        self.refuses(sectors=sec, gate="structure below the floor")

    def test_low_strength_is_refused(self):
        sec = payload()["sectors"]
        sec["AUTO"]["s"] = [51]
        self.refuses(sectors=sec, gate="strength below the floor")

    def test_a_lead_outside_the_band_is_refused(self):
        sec = payload()["sectors"]
        sec["AUTO"]["lead"] = [4.2]
        self.refuses(sectors=sec, gate="lead outside the band")

    def test_a_name_that_already_ran_is_refused(self):
        # Ten candles of +2% each: the name is far past SCREEN_RUN_MAX_R by now.
        frames = {data.yahoo_stock("MARUTI"): rising(10_000, 200, 12)}
        with patch.dict(os.environ, {"SCREEN_RUN_MAX_R": "0.5"}):
            self.refuses(frames=frames, gate="already run too far")

    def test_a_name_without_a_frame_is_refused_not_scored(self):
        self.refuses(frames={}, gate="no candle for the name")

    def test_a_setup_against_a_committed_market_needs_high_persistence(self):
        sec = payload()["sectors"]
        sec["AUTO"]["persist"] = 85           # above the floor, below COUNTER_PERSIST
        self.refuses(sectors=sec, gate="sector against the market", market=md("BEAR", 70, -1))

    def test_the_same_setup_is_taken_against_the_tape_at_90(self):
        sec = payload()["sectors"]
        sec["AUTO"]["persist"] = 95
        out = self.run_candle(payload(sectors=sec),
                              {data.yahoo_stock("MARUTI"): rising(10_000, 40, 12)},
                              at=11, market=md("BEAR", 70, -1))
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["aligned"])
        self.assertIn("against the tape", self.sent[0])


class CapTests(ScreenCase):
    def book(self, names):
        sec = payload()["sectors"]
        sec["AUTO"]["n"] = names
        sec["AUTO"]["rank"] = [90 - 2 * i for i in range(len(names))]
        sec["AUTO"]["s"] = [80] * len(names)
        sec["AUTO"]["v"] = [80] * len(names)
        sec["AUTO"]["t"] = [80] * len(names)
        sec["AUTO"]["lead"] = [0.3] * len(names)
        sec["AUTO"]["mv"] = [3.0] * len(names)
        sec["AUTO"]["rk"] = list(range(1, len(names) + 1))
        return sec

    def frames_for(self, names, n=12):
        return {data.yahoo_stock(x): rising(10_000 + 10 * i, 40, n)
                for i, x in enumerate(names)}

    def test_the_daily_cap_holds(self):
        names = ["MARUTI", "M&M", "TATAMOTORS"]
        with patch.dict(os.environ, {"SCREEN_MAX_DAY": "2", "SCREEN_CAP_PER_SECTOR": "3"}):
            out = self.run_candle(payload(sectors=self.book(names)), self.frames_for(names),
                                  at=11)
        self.assertEqual(len(out), 2)
        self.assertEqual(len(self.st()["setups"]), 2)

    def test_the_per_sector_cap_holds(self):
        names = ["MARUTI", "M&M", "TATAMOTORS"]
        with patch.dict(os.environ, {"SCREEN_MAX_DAY": "6", "SCREEN_CAP_PER_SECTOR": "1"}):
            out = self.run_candle(payload(sectors=self.book(names)), self.frames_for(names),
                                  at=11)
        self.assertEqual(len(out), 1)

    def test_a_name_is_never_surfaced_twice_in_a_day(self):
        names = ["MARUTI"]
        frames = self.frames_for(names)
        self.run_candle(payload("10:00", sectors=self.book(names)), frames, at=9)
        self.run_candle(payload("10:30", sectors=self.book(names)), frames, at=15)
        self.assertEqual(len(self.st()["setups"]), 1)

    def test_nothing_is_surfaced_after_the_last_entry_time(self):
        with patch.dict(os.environ, {"SCREEN_LAST_ENTRY": "11:00"}):
            out = self.run_candle(payload("12:15", sectors=self.book(["MARUTI"])),
                                  self.frames_for(["MARUTI"]), at=36)
        self.assertEqual(out, [])
        self.assertEqual(self.st()["setups"], [])

    def test_a_score_below_the_floor_is_skipped(self):
        names = ["MARUTI"]
        with patch.dict(os.environ, {"SCREEN_MIN_SCORE": "99"}):
            out = self.run_candle(payload(sectors=self.book(names)),
                                  self.frames_for(names), at=11)
        self.assertEqual(out, [])

    def test_a_bot_4_refusal_leaves_the_setup_unsent(self):
        """No fallback to bots 1/2 — a missing bot 4 is a missing channel, not a leak."""
        screen.consider(self.st(), payload(), {data.yahoo_stock("MARUTI"): rising(10_000, 40, 12)},
                        md(), candle(11), send=lambda *a, **k: False, save_state=screen.save)
        self.assertEqual(self.st()["setups"], [])
        self.assertEqual(self.st()["stats"].get("surfaced", 0), 0)


class TrackingTests(ScreenCase):
    def track_and_save(self, frames, last):
        """``track`` mutates the ledger it is given; save it so the assertions read it."""
        st = self.st()
        screen.track(st, frames, last)
        screen.save(st)
        return st

    def surface(self):
        names = ["MARUTI"]
        frames = {data.yahoo_stock("MARUTI"): rising(10_000, 40, 12)}
        out = self.run_candle(payload("10:00", sectors=CapTests.book(self, names)),
                              frames, at=9)
        self.assertEqual(len(out), 1)
        return out[0]

    def test_a_setup_that_runs_one_r_is_worked(self):
        setup = self.surface()
        risk = float(setup["risk"])
        entry = float(setup["entry"])
        # The same name, later: a close far above 1R and a high above entry+risk.
        closes = [10_000 + 40 * i for i in range(12)] + [entry + 6 * risk]
        frames = {data.yahoo_stock("MARUTI"): frame(closes)}
        got = self.track_and_save(frames, candle(12))["setups"][0]
        self.assertTrue(got["hit_1r"])
        self.assertGreaterEqual(got["mfe_r"], 1.0)

    def test_a_setup_that_breaks_the_stop_is_stopped(self):
        setup = self.surface()
        sl = float(setup["sl"])
        closes = [10_000 + 40 * i for i in range(12)] + [sl * 0.98]
        got = self.track_and_save({data.yahoo_stock("MARUTI"): frame(closes)},
                                  candle(12))["setups"][0]
        self.assertTrue(got["stopped"])

    def test_tracking_starts_at_the_setup_bar_not_the_day_high(self):
        """The 09:15-09:55 range must never be credited to a setup surfaced at 10:00."""
        st = screen.fresh("2026-11-20")
        st["setups"].append({"id": 1, "raw": "MARUTI", "sector": "AUTO", "side": "BUY",
                             "entry": 100.0, "sl": 99.0, "risk": 1.0, "bar": "10:00",
                             "status": "surfaced"})
        closes = [110.0] * 9 + [100.2, 100.4]     # a 110 spike before the setup
        screen.track(st, {data.yahoo_stock("MARUTI"): frame(closes)}, candle(10))
        got = st["setups"][0]
        # Only the two candles at/after 10:00 count: high 100.5, low 100.1 -> 0.5R
        # favourable, and nothing adverse (the 110 spike is before the setup, so it
        # cannot be credited to it — that is the whole point of the test).
        self.assertAlmostEqual(got["mfe_r"], 0.5, places=2)
        self.assertEqual(got["mae_r"], 0.0)

    def test_the_stop_wins_when_one_candle_touches_both(self):
        st = screen.fresh("2026-11-20")
        st["setups"].append({"id": 1, "raw": "MARUTI", "sector": "AUTO", "side": "BUY",
                             "entry": 100.0, "sl": 99.0, "risk": 1.0, "bar": "10:00",
                             "status": "surfaced"})
        df = frame([100.0] * 9 + [101.5])
        df.loc[df.index[-1], ["High", "Low"]] = [102.0, 98.5]   # both in one candle
        screen.track(st, {data.yahoo_stock("MARUTI"): df}, candle(10))
        got = st["setups"][0]
        self.assertTrue(got["stopped"])
        self.assertIsNone(got.get("hit_1r"))

    def test_an_update_is_announced_once(self):
        self.surface()
        sl = float(self.st()["setups"][0]["sl"])
        closes = [10_000 + 40 * i for i in range(12)] + [sl * 0.98]
        st = self.track_and_save({data.yahoo_stock("MARUTI"): frame(closes)}, candle(12))
        first = screen.updates(st)
        second = screen.updates(st)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertIn("stop traded", first[0])


class OnTimeTests(ScreenCase):
    """The signal has to arrive *with* its candle, not after it.

    A 5m bar closes at 10:15; Yahoo publishes tens of seconds later, so a
    close-only card is late by construction. These tests pin the fix: a
    PROVISIONAL card on the forming candle (``EARLY_ALERT_SECONDS`` before its
    close), one short line at the close — CONFIRMED with the closed candle's own
    levels, or CANCELLED with its slot freed — and a visible record of any candle
    the loop never judged at all.
    """

    def forming(self, closes=None, n=12):
        """A frame whose newest row IS the forming candle (same shape as ``provisional_5m``)."""
        return {data.yahoo_stock("MARUTI"): rising(10_000, 40, n)} if closes is None else \
            {data.yahoo_stock("MARUTI"): frame(closes)}

    def test_a_provisional_card_goes_out_before_the_close(self):
        with patch.dict(os.environ, {"SCREEN_EARLY": "1"}):
            out = screen.provisional(self.st(), payload(), self.forming(), md(), candle(11),
                                     send=self.send,
                                     now=candle(11) + timedelta(minutes=4))
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["provisional"])
        self.assertEqual(out[0]["seconds_left"], 60)
        self.assertIn("PROVISIONAL", self.sent[0])
        self.assertIn("closes in <b>60s</b>", self.sent[0])
        self.assertIn("forming close", self.sent[0])
        self.assertIn("Not an entry alert", self.sent[0])

    def test_the_same_name_is_not_surfaced_again_at_the_close(self):
        with patch.dict(os.environ, {"SCREEN_EARLY": "1"}):
            screen.provisional(self.st(), payload(), self.forming(), md(), candle(11),
                               send=self.send, now=candle(11) + timedelta(minutes=4))
        out = self.run_candle(payload("10:10"), self.forming(), at=11)
        self.assertEqual(out, [])                       # settled, not re-admitted
        self.assertEqual(len(self.st()["setups"]), 1)

    def test_a_card_that_survives_the_close_is_confirmed_with_closed_levels(self):
        with patch.dict(os.environ, {"SCREEN_EARLY": "1"}):
            screen.provisional(self.st(), payload(), self.forming(), md(), candle(11),
                               send=self.send, now=candle(11) + timedelta(minutes=4))
        self.sent.clear()
        self.run_candle(payload("10:10"), self.forming(), at=11)
        st = self.st()
        self.assertEqual(len(st["setups"]), 1)
        got = st["setups"][0]
        self.assertFalse(got["provisional"])
        self.assertEqual(got["confirmed"], "10:10")
        self.assertEqual(got["status"], "surfaced")
        self.assertIsNotNone(got["age_s"])
        self.assertTrue(any("CONFIRMED" in t for t in self.sent))
        self.assertEqual(st["stats"].get("confirmed"), 1)

    def test_a_card_the_close_drops_is_cancelled_and_its_slot_freed(self):
        with patch.dict(os.environ, {"SCREEN_EARLY": "1", "SCREEN_MAX_DAY": "1"}):
            screen.provisional(self.st(), payload(), self.forming(), md(), candle(11),
                               send=self.send, now=candle(11) + timedelta(minutes=4))
            self.assertEqual(len(self.st()["setups"]), 1)
            self.sent.clear()
            # The close: the name has lost its high, so it no longer qualifies.
            closes = [10_000 + 40 * i for i in range(11)] + [10_300]
            self.run_candle(payload("10:10"), self.forming(closes), at=11)
            st = self.st()
        self.assertEqual(st["setups"], [])              # slot freed
        self.assertEqual(len(st["cancelled"]), 1)
        self.assertEqual(st["cancelled"][0]["status"], "cancelled")
        self.assertTrue(any("CANCELLED" in t for t in self.sent))
        # ...and the freed slot can be used by another name on the next candle.
        with patch.dict(os.environ, {"SCREEN_EARLY": "1", "SCREEN_MAX_DAY": "1"}):
            sec = payload()["sectors"]
            sec["AUTO"]["n"] = ["M&M"]
            out = self.run_candle(payload("10:15", sectors=sec),
                                  {data.yahoo_stock("M&M"): rising(5_000, 20, 13)}, at=12)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["raw"], "M&M")

    def test_a_provisional_whose_candle_was_never_judged_says_so(self):
        """The one case that hides a real failure: the loop skipped its close pass."""
        with patch.dict(os.environ, {"SCREEN_EARLY": "1"}):
            screen.provisional(self.st(), payload(), self.forming(), md(), candle(11),
                               send=self.send, now=candle(11) + timedelta(minutes=4))
            self.sent.clear()
            self.run_candle(payload("10:25"), self.forming(), at=14)   # 10:10 skipped
        st = self.st()
        self.assertEqual(st["setups"], [])
        self.assertEqual(len(st["cancelled"]), 1)
        self.assertIn("no close pass for its 10:10 candle", st["cancelled"][0]["why_close"])
        self.assertTrue(any("CANCELLED" in t for t in self.sent))

    def test_the_early_pass_can_be_switched_off(self):
        with patch.dict(os.environ, {"SCREEN_EARLY": "0"}):
            out = screen.provisional(self.st(), payload(), self.forming(), md(), candle(11),
                                     send=self.send, now=candle(11) + timedelta(minutes=4))
        self.assertEqual(out, [])
        self.assertEqual(self.sent, [])

    def test_the_early_pass_respects_the_cap_and_the_last_entry_time(self):
        with patch.dict(os.environ, {"SCREEN_EARLY": "1", "SCREEN_LAST_ENTRY": "10:00"}):
            out = screen.provisional(self.st(), payload(), self.forming(), md(), candle(11),
                                     send=self.send, now=candle(11) + timedelta(minutes=4))
        self.assertEqual(out, [])

    def test_the_age_after_the_close_is_recorded_and_late_cards_are_flagged(self):
        # A close pass that reaches the candle 140s after it closed.
        with self.assertLogs("nse_scanner", level="WARNING") as logs:
            out = screen.consider(self.st(), payload("10:10"), self.forming(), md(),
                                  candle(11), send=self.send, save_state=screen.save,
                                  now=candle(12) + timedelta(seconds=140))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["age_s"], 140)
        self.assertEqual(self.st()["stats"].get("late"), 1)
        self.assertIn("140s after the 10:10 candle closed", " ".join(logs.output))
        self.assertIn("140s after the close", self.sent[0])

    def test_an_on_time_card_is_not_flagged(self):
        out = screen.consider(self.st(), payload("10:10"), self.forming(), md(), candle(11),
                              send=self.send, save_state=screen.save,
                              now=candle(12) + timedelta(seconds=12))
        self.assertEqual(out[0]["age_s"], 12)
        self.assertEqual(self.st()["stats"].get("late", 0), 0)

    def test_the_pipeline_runs_the_screen_on_the_early_pass(self):
        import inspect

        from nse_scanner import pipeline

        source = inspect.getsource(pipeline.live_loop)
        self.assertIn("_screen_early(bar)", source)
        self.assertIn("run_early(bar)", source)
        self.assertLess(source.index("run_early(bar)"), source.index("_screen_early(bar)"))


class MessageTests(ScreenCase):
    def test_the_card_says_it_is_not_an_entry(self):
        frames = {data.yahoo_stock("MARUTI"): rising(10_000, 40, 12)}
        out = self.run_candle(payload(), frames, at=11)
        text = self.sent[0]
        self.assertIn("Not an entry alert", text)
        self.assertIn(f"SETUP #{out[0]['id']}", text)
        self.assertIn("SETUP", text)

    def test_the_card_has_no_bare_less_than(self):
        """Telegram HTML: every ``<`` must open one of the tags we actually send."""
        frames = {data.yahoo_stock("MARUTI"): rising(10_000, 40, 12)}
        self.run_candle(payload(), frames, at=11)
        for text in self.sent:
            for tag in re.findall(r"<[^>]*>", text):
                self.assertIn(tag, {"<b>", "</b>", "<i>", "</i>", "<code>", "</code>",
                                    "<pre>", "</pre>"}, f"unexpected tag {tag}")
            self.assertNotIn("< ", text)

    def test_the_ledger_records_no_position_of_any_kind(self):
        frames = {data.yahoo_stock("MARUTI"): rising(10_000, 40, 12)}
        self.run_candle(payload(), frames, at=11)
        st = self.st()
        self.assertEqual(st["setups"][0]["status"], "surfaced")
        for forbidden in ("positions", "entries", "sent"):
            self.assertNotIn(forbidden, st)


class StateTests(ScreenCase):
    def test_a_new_session_drops_yesterday(self):
        st = screen.fresh("2026-11-19")
        st["setups"].append({"id": 1, "raw": "OLD"})
        same = screen.reset_if_new_day(st, "2026-11-20")
        self.assertEqual(same["setups"], [])
        self.assertEqual(same["session"], "2026-11-20")

    def test_the_knobs_are_frozen_on_the_session_they_ran(self):
        st = screen.fresh("2026-11-20")
        with patch.dict(os.environ, {"SCREEN_MAX_DAY": "3"}):
            self.assertEqual(st["knobs"]["SCREEN_MAX_DAY"], 6)   # frozen at the open
        self.assertEqual(screen_pdf._knobs_locked(st)["SCREEN_MAX_DAY"], 6)

    def test_an_unreadable_ledger_starts_a_fresh_one(self):
        self.state.write_text("{not json", encoding="utf-8")
        self.assertEqual(screen.load(), {})
        screen.save(screen.fresh("2026-11-20"))
        self.assertEqual(screen.load()["session"], "2026-11-20")

    def test_the_state_file_is_the_screens_own(self):
        self.assertTrue(str(screen.STATE_PATH).endswith("screen_state.json"))
        from nse_scanner import state

        self.assertNotEqual(screen.STATE_PATH, state.STATE_PATH)


class TelegramBot4Tests(TestCase):
    def test_bot_4_has_its_own_credentials_and_never_falls_back(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN_4": "", "TELEGRAM_CHAT_ID_4": "",
                                     "TELEGRAM_BOT_TOKEN": "one", "TELEGRAM_CHAT_ID": "one"},
                        clear=True):
            with patch.object(telegram, "_send_to", return_value=True) as sent:
                self.assertFalse(telegram.send_bot4("hello"))
                sent.assert_not_called()

    def test_bot_4_sends_to_its_own_chat(self):
        env = {"TELEGRAM_BOT_TOKEN_4": "t4", "TELEGRAM_CHAT_ID_4": "c4",
               "TELEGRAM_BOT_TOKEN": "t1", "TELEGRAM_CHAT_ID": "c1"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(telegram, "_send_to", return_value=True) as sent:
                self.assertTrue(telegram.send_bot4("hello"))
                self.assertEqual(sent.call_args.args[:2], ("t4", "c4"))
                self.assertEqual(sent.call_args.kwargs.get("label"), "bot4")

    def test_the_document_upload_knows_bot_4(self):
        from tempfile import NamedTemporaryFile

        with NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(b"%PDF-1.4 fake")
            name = fh.name
        self.addCleanup(lambda: Path(name).unlink(missing_ok=True))
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN_4": "t4", "TELEGRAM_CHAT_ID_4": "c4"},
                        clear=True):
            with patch("nse_scanner.telegram.requests.post") as post:
                post.return_value.status_code = 200
                post.return_value.text = "{}"
                telegram.send_document(name, "caption", bot=4)
                url = post.call_args.args[0]
                self.assertIn("t4", url)


class PdfTests(ScreenCase):
    def report_state(self) -> dict:
        frames = {data.yahoo_stock("MARUTI"): rising(10_000, 40, 12)}
        self.run_candle(payload(), frames, at=11)
        st = self.st()
        st["setups"][0]["mfe_r"] = 1.4
        st["setups"][0]["mae_r"] = -0.2
        st["setups"][0]["eod_r"] = 1.1
        st["setups"][0]["mark"] = 10_500
        return st

    def test_the_pdf_is_written_and_is_a_pdf(self):
        st = self.report_state()
        out = screen_pdf.build(st, Path(self.tmp.name) / "audit.pdf")
        self.assertTrue(out.exists())
        self.assertEqual(out.suffix, ".pdf")
        head = out.read_bytes()[:5]
        self.assertEqual(head, b"%PDF-")

    def test_the_pdf_carries_the_knobs_the_gates_and_the_short_list(self):
        # The text fallback is the same audit, so it is the honest thing to assert on.
        st = self.report_state()
        text = screen_pdf._plain_text(st)
        for needle in ("KNOBS IN FORCE", "SCREEN_PERSIST_MIN", "THE SHORT LIST",
                       "CANDLE-BY-CANDLE", "GATES", "FROZEN 20 NOV HOLDOUT", "MARUTI"):
            self.assertIn(needle, text)
        self.assertIn(screen_pdf.config_hash(st), text)

    def test_the_pdf_degrades_to_text_when_reportlab_is_missing(self):
        st = self.report_state()
        with patch.object(screen_pdf, "_write_pdf", side_effect=ImportError("no reportlab")):
            out = screen_pdf.build(st, Path(self.tmp.name) / "audit.pdf")
        self.assertEqual(out.suffix, ".txt")
        self.assertIn("SCREEN_PERSIST_MIN", out.read_text())

    def test_a_failing_build_never_raises(self):
        with patch.object(screen_pdf, "_write_pdf", side_effect=RuntimeError("boom")):
            out = screen_pdf.build({"session": "2026-11-20"}, Path(self.tmp.name) / "x.pdf")
        self.assertTrue(out.exists())

    def test_the_caption_is_valid_telegram_html(self):
        st = self.report_state()
        text = screen_pdf.text_summary(st)
        for tag in re.findall(r"<[^>]*>", text):
            self.assertIn(tag, {"<b>", "</b>", "<code>", "</code>"})
        self.assertNotIn("< ", text)
        self.assertIn("SCREEN AUDIT", text)

    def test_the_report_uploads_the_pdf_to_bot_4(self):
        st = self.report_state()
        calls = {}

        def fake_doc(path, caption, **kw):
            calls["path"], calls["caption"], calls["bot"] = path, caption, kw.get("bot")
            return True

        out = screen_pdf.report(st, send_doc=fake_doc)
        self.assertTrue(out["sent"])
        self.assertEqual(calls["bot"], 4)
        self.assertTrue(str(calls["path"]).endswith(".pdf"))
        self.assertIn("SCREEN AUDIT", calls["caption"])

    def test_a_refused_upload_says_so_and_keeps_the_file(self):
        st = self.report_state()
        with self.assertLogs("nse_scanner", level="ERROR") as logs:
            out = screen_pdf.report(st, send_doc=lambda *a, **k: False)
        self.assertFalse(out["sent"])
        self.assertTrue(Path(out["path"]).exists())
        self.assertIn("NOT delivered", " ".join(logs.output))
