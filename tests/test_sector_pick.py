"""Which sector gets picked, and on how fresh a tape (no network access).

``market._sector_snap`` took a ``last_ts`` argument and never read it, so a
sector index that stopped printing was scored on its last available bar and
could still win the pick. The stock level has always refused a stale frame
(``to_ist(sm5[sym].index[-1]) == last``); the sector level did not, and the
sector decides which stocks are scanned at all.
"""
from datetime import date
from unittest import TestCase
from unittest.mock import patch

import numpy as np
import pandas as pd

from nse_scanner import market
from nse_scanner.market import run_market
from nse_scanner.session import IST

DAYS = [date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11), date(2026, 11, 12)]


def bars(day, closes):
    t = pd.Timestamp(day, tz=IST) + pd.Timedelta(hours=9, minutes=15)
    idx = []
    for _ in closes:
        idx.append(t)
        t += pd.Timedelta(minutes=5)
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"Open": c, "High": c * 1.001, "Low": c * 0.999, "Close": c,
                         "Volume": np.ones(len(c))}, index=pd.DatetimeIndex(idx))


def series(start, step, n=75):
    return [start * (1 + step) ** i for i in range(n)]


def daily(df):
    rows = {}
    for ts, g in df.groupby([t for t in df.index.date]):
        rows[ts] = {"Open": g["Open"].iloc[0], "High": g["High"].max(),
                    "Low": g["Low"].min(), "Close": g["Close"].iloc[-1],
                    "Volume": g["Volume"].sum()}
    return pd.DataFrame(list(rows.values()),
                        index=pd.DatetimeIndex([pd.Timestamp(d, tz=IST) for d in rows]))


def sector(step: float, tail_bars: int = 0):
    """Four sessions of 5m bars, optionally chopped at the end (a lagging feed)."""
    out, level = [], 1000.0
    for d in DAYS:
        seg = series(level, step)
        out.append(bars(d, seg))
        level = seg[-1]
    df = pd.concat(out)
    return df.iloc[:len(df) - tail_bars] if tail_bars else df


def nifty():
    out, level = [], 25000.0
    for d in DAYS:
        seg = series(level, 0.00002)
        out.append(bars(d, seg))
        level = seg[-1]
    return pd.concat(out)


def crashy(step: float, drop: float, tail: int = 6):
    """A sector that led all morning and then gave it all back in ``tail`` bars."""
    df = sector(step)
    last = df.iloc[-tail:].copy()
    for col in ("Open", "High", "Low", "Close"):
        last[col] = last[col] * (1 - drop)
    return pd.concat([df.iloc[:-tail], last])


class StaleSectorTests(TestCase):
    """A sector index that stopped printing is scored, but never picked."""

    def world(self):
        n5 = nifty()
        sectors = {
            "PHA": sector(0.00005),                 # fresh, modest
            "FMCG": sector(0.00020, tail_bars=6),   # strongest, but 30 minutes stale
            "MET": sector(-0.00015),                # falling
        }
        vix = bars(DAYS[-1], series(15.0, 0.0))
        return n5, vix, sectors

    def decide(self, **env):
        n5, vix, sectors = self.world()
        with patch.dict("os.environ", {"SECTOR_FOLLOW": "1", **env}):
            return run_market(n5, daily(n5), vix, daily(vix), sectors,
                              {k: daily(v) for k, v in sectors.items()})

    def test_the_stale_leader_is_not_picked(self):
        # FMCG's index ends at 14:55 while the decision bar is 15:20 (the 15:25
        # print is dropped by sanitize_index_5m), so it is 5 bars behind.
        md = self.decide()
        self.assertEqual(md.pick_sector, "PHA")
        self.assertEqual(md.stale_sectors, [("FMCG", 5)])

    def test_the_stale_leader_is_not_listed_as_moving_now(self):
        md = self.decide()
        self.assertNotIn("FMCG", [name for name, _ in md.persist_plus])
        self.assertNotIn("FMCG", [name for name, _ in md.persist_minus])

    def test_the_guard_is_what_changed_the_pick(self):
        # With the freshness check off the 30-minute-old tape still wins the pick.
        md = self.decide(SECTOR_MAX_LAG_BARS="-1")
        self.assertEqual(md.pick_sector, "FMCG")
        self.assertEqual(md.stale_sectors, [])

    def test_a_bar_behind_is_tolerated_and_five_is_not(self):
        # One bar is normal jitter on Yahoo's cash indices; five is a dead feed.
        self.assertEqual(self.decide(SECTOR_MAX_LAG_BARS="0").pick_sector, "PHA")
        md = self.decide(SECTOR_MAX_LAG_BARS="7")
        self.assertEqual(md.pick_sector, "FMCG")
        self.assertEqual(md.stale_sectors, [])

    def test_a_stale_sector_keeps_its_display_row(self):
        md = self.decide()
        # it is not hidden from the sector table, only from the pick
        self.assertIn("FMCG", md.sectors)
        self.assertTrue(md.sectors["FMCG"]["stale"])
        self.assertEqual(md.sectors["FMCG"]["lag_bars"], 5)
        self.assertFalse(md.sectors["PHA"]["stale"])


class PoolSideTests(TestCase):
    """A sector that has rolled over is a short, however persistent it was.

    The two pools used to be sorted by persistence and nothing else, so a sector
    that spent the morning leading and then reversed sat at the top of PERSIST +
    with a bearish side, and a BULL market could buy the reversal.
    """

    def world(self, drop: float):
        n5 = nifty()
        sectors = {"PHA": crashy(0.00020, drop),       # led all morning, then fell over
                   "FMCG": sector(0.00005),            # steady
                   "MET": sector(-0.00015)}            # falling all day
        vix = bars(DAYS[-1], series(15.0, 0.0))
        return n5, vix, sectors

    def decide(self, drop: float, **env):
        n5, vix, sectors = self.world(drop)
        with patch.dict("os.environ", {"SECTOR_FOLLOW": "1", "PICK_RS_MARGIN": "0", **env}):
            return run_market(n5, daily(n5), vix, daily(vix), sectors,
                              {k: daily(v) for k, v in sectors.items()})

    def test_a_rolled_over_sector_is_a_minus_however_persistent(self):
        md = self.decide(0.03)
        plus = dict(md.persist_plus)
        minus = dict(md.persist_minus)
        self.assertNotIn("PHA", plus)
        self.assertGreater(minus["PHA"], 90.0)     # never dropped for weakness
        self.assertEqual(md.pick_side, 1)
        self.assertNotEqual(md.pick_sector, "PHA")

    def test_the_pools_never_share_a_name(self):
        md = self.decide(0.03)
        self.assertEqual(set(dict(md.persist_plus)) & set(dict(md.persist_minus)), set())

    def test_a_steady_leader_is_still_a_plus(self):
        md = self.decide(0.0)
        self.assertEqual(md.persist_plus[0][0], "PHA")
        self.assertEqual(md.pick_sector, "PHA")


class DirectionFirstTests(TestCase):
    """The pick is chosen by the Nifty lock first, the persistence number second.

    In the 16 Nov feed REALTY sat at the top of PERSIST − with 91 while FMCG took
    the pick with 93. That is not a ranking between the two numbers — they are
    different metrics on different sides — and it is the question the block keeps
    raising, so it is pinned here: the same two sectors, the same numbers, and the
    pick flips only because the index does.
    """

    @staticmethod
    def nifty(step: float):
        out, level = [], 25000.0
        for d in DAYS:
            seg = series(level, step)
            out.append(bars(d, seg))
            level = seg[-1]
        return pd.concat(out)

    def decide(self, step: float):
        n5 = self.nifty(step)
        sectors = {"FMCG": sector(0.00030), "REALTY": sector(-0.00045)}
        vix = bars(DAYS[-1], series(15.0, 0.0))
        with patch.dict("os.environ", {"SECTOR_FOLLOW": "1"}):
            return run_market(n5, daily(n5), vix, daily(vix), sectors,
                              {k: daily(v) for k, v in sectors.items()})

    def test_a_bull_index_picks_the_persistent_riser(self):
        md = self.decide(0.00004)
        self.assertEqual((md.pick_sector, md.pick_side), ("FMCG", 1))
        self.assertTrue(md.pick_reason.startswith("PERSIST +"))

    def test_a_bear_index_picks_the_persistent_faller(self):
        md = self.decide(-0.00005)
        self.assertEqual((md.pick_sector, md.pick_side), ("REALTY", -1))
        self.assertTrue(md.pick_reason.startswith("PERSIST −"))

    def test_the_two_numbers_are_not_comparable(self):
        # REALTY is the more persistent of the two on its own side (97.3 vs FMCG's
        # 96.8) and scores 0.8 on the bull metric: "top persistent" is per-side.
        md = self.decide(0.00004)
        fmcg, realty = md.sectors["FMCG"], md.sectors["REALTY"]
        self.assertGreater(fmcg["persist_bull"], 90.0)
        self.assertLess(realty["persist_bull"], 5.0)
        self.assertGreater(realty["persist_bear"], fmcg["persist_bull"])   # 97.3 > 96.8
        # each pool carries its own side's number, and the block shows both
        self.assertAlmostEqual(dict(md.persist_plus)["FMCG"], fmcg["persist_bull"], delta=0.05)
        self.assertAlmostEqual(dict(md.persist_minus)["REALTY"], realty["persist_bear"], delta=0.05)

    def test_a_flat_index_follows_the_long_leader_only(self):
        md = self.decide(0.0)
        self.assertEqual((md.pick_sector, md.pick_side), ("FMCG", 1))
        self.assertTrue(md.pick_reason.startswith("SECTOR FOLLOW"))
        self.assertIn("Nifty NEUTRAL", md.pick_reason)


class DivergenceWiringTests(TestCase):
    """The divergence short only replaces a WAIT; it never sits next to a long."""

    # the real SECTOR_ORDER codes: a made-up name is dropped by run_market
    NAMES = ["AUTO", "FIN", "IT", "MET", "DEF", "ENE", "FMCG", "OIL", "PHA", "PSE",
             "PSUBK", "BANK", "REALTY", "CDUR", "MEDIA"]

    def world(self):
        """Index rising, five sectors up and the rest falling: 16 Nov, in miniature."""
        sectors = {n: (sector(0.00030) if i < 5 else sector(-0.00010 * (1 + i % 3)))
                   for i, n in enumerate(self.NAMES)}      # 5 up, 10 down
        out, level = [], 25000.0
        for d in DAYS:
            seg = series(level, 0.00004)
            out.append(bars(d, seg))
            level = seg[-1]
        n5 = pd.concat(out)
        vix = bars(DAYS[-1], series(15.0, 0.0))
        return n5, vix, sectors

    def decide(self, **env):
        """A thunk: the env has to be patched while ``run_market`` runs, not before."""
        def go():
            n5, vix, sectors = self.world()
            with patch.dict("os.environ", {"SECTOR_FOLLOW": "1", **env}):
                return run_market(n5, daily(n5), vix, daily(vix), sectors,
                                  {k: daily(v) for k, v in sectors.items()})
        return go

    def test_a_divergence_day_is_still_waited_out_by_default(self):
        with patch.object(market, "_complementary_pick", return_value=(None, 0, "WAIT — test")):
            md = self.decide()()
        self.assertEqual((md.pick_sector, md.pick_side, md.action), ("WAIT", 0, "WAIT"))

    def test_the_short_fills_the_wait_when_asked(self):
        with patch.object(market, "_complementary_pick", return_value=(None, 0, "WAIT — test")):
            md = self.decide(DIVERGENCE_SHORT="1")()
        self.assertEqual((md.pick_side, md.action), (-1, "SCAN SHORT"))
        self.assertIn("DIVERGENCE SHORT", md.pick_reason)
        self.assertEqual(md.pick_sector, md.persist_minus[0][0])     # the persistent faller
        self.assertEqual(md.commit_side, 1)                          # against a BULL lock

    def test_a_long_pick_still_wins_the_bar(self):
        # One side only: with a sector to buy, the divergence short stays off the tape.
        md = self.decide(DIVERGENCE_SHORT="1")()
        self.assertEqual((md.pick_side, md.action), (1, "SCAN LONG"))
        self.assertNotIn("DIVERGENCE", md.pick_reason)

    def test_the_market_state_reaches_the_rule(self):
        # The 16 Nov morning was MTF 2/3 at every bar: the unconfirmed-uptrend
        # pattern has to see the real MTF and confidence, not defaults.
        seen = {}
        real = market._divergence_short

        def spy(*args, **kwargs):
            seen.update(kwargs)
            return real(*args, **kwargs)

        with patch.object(market, "_complementary_pick", return_value=(None, 0, "WAIT — test")), \
                patch.object(market, "_divergence_short", side_effect=spy):
            md = self.decide(DIVERGENCE_SHORT="1")()
        self.assertEqual(seen["mtf_bull"], md.mtf_bull)
        self.assertEqual(seen["dir_conf"], md.dir_conf)

    def test_the_short_names_the_divergence_on_the_block(self):
        with patch.object(market, "_complementary_pick", return_value=(None, 0, "WAIT — test")):
            md = self.decide(DIVERGENCE_SHORT="1")()
        from nse_scanner.pipeline import format_market
        text = format_market(md)
        self.assertIn("short on divergence", text)
        self.assertIn("SECTOR PICK:", text)
        self.assertIn("BEAR", text)
        from nse_scanner.market import mtf_text
        self.assertIn(mtf_text(md.mtf_bull), text)      # the block shows both MTF counts


class DivergenceStudyTests(TestCase):
    """`divergence_rows` answers "would the REALTY short have paid?" from a replay."""

    @staticmethod
    def bar(hhmm, *, label="BULL", conf=35, mtf=2, day=0.42, adv=9, dec=6,
            pool=(("REALTY", 76),), pick="WAIT", realty=-1.40, nifty=25250.0):
        return {"time": hhmm, "dir": label, "conf": conf, "mtf_bull": mtf,
                "nifty_day_ret": day, "breadth": f"{adv}/{dec}",
                "persist_minus": list(pool), "pick": pick,
                "sectors": {"REALTY": {"day_ret": realty, "persist_bear": 76.0}}}

    def rows(self, *bars):
        from nse_scanner.backtest import divergence_rows
        return divergence_rows(list(bars))

    def test_the_16_nov_morning_is_a_divergence_bar(self):
        # MTF 2/3, conf 35, breadth 9▲/6▼: not a breadth divergence at all.
        out = self.rows(self.bar("09:30"), self.bar("15:25", day=0.40, realty=-3.10))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["sector"], "REALTY")
        self.assertAlmostEqual(out[0]["day_at"], -1.40)
        self.assertAlmostEqual(out[0]["day_close"], -3.10)
        self.assertAlmostEqual(out[0]["move"], -1.70)        # kept falling
        self.assertFalse(out[0]["blocked"])

    def test_a_confirmed_uptrend_has_no_rows(self):
        out = self.rows(self.bar("10:00", mtf=3, conf=85, adv=9, dec=6),
                        self.bar("15:25", mtf=3, conf=85, realty=-3.0))
        self.assertEqual(out, [])

    def test_a_flat_or_falling_index_has_no_rows(self):
        # no index up-day at all: every bar fails the first gate
        self.assertEqual(self.rows(self.bar("10:00", day=-0.2), self.bar("15:25", day=-0.3)), [])
        self.assertEqual(self.rows(self.bar("10:00", label="BEAR", day=0.3),
                                   self.bar("15:25", label="BEAR", day=0.3)), [])

    def test_narrow_breadth_alone_qualifies(self):
        out = self.rows(self.bar("11:55", mtf=3, conf=41, adv=5, dec=10),
                        self.bar("15:25", mtf=3, conf=41, adv=5, dec=10, realty=-3.1))
        self.assertEqual(len(out), 2)

    def test_a_long_pick_marks_the_bar_as_blocked(self):
        out = self.rows(self.bar("09:35", pick="PHA"), self.bar("15:25"))
        self.assertTrue(out[0]["blocked"])

    def test_a_recovering_sector_shows_a_positive_move(self):
        out = self.rows(self.bar("10:00"), self.bar("15:25", realty=-0.4))
        self.assertGreater(out[0]["move"], 0)        # a short would have lost

    def test_the_table_renders_and_says_when_there_is_nothing(self):
        from nse_scanner.backtest import render_divergence
        rows = self.rows(self.bar("09:30"), self.bar("15:25", realty=-3.10))
        text = render_divergence(rows, "2026-11-16")
        self.assertIn("Divergence study", text)
        self.assertIn("REALTY", text)
        self.assertIn("kept falling", text)
        self.assertIn("no divergence bar", render_divergence([], "2026-11-16"))


class StickyLapseOnThePickLineTests(TestCase):
    """The note reaches ``pick_reason`` — and only when the incumbent was lost."""

    def world(self):
        n5 = nifty()
        sectors = {"PHA": sector(0.00005), "FMCG": sector(0.00020, tail_bars=6),
                   "MET": sector(-0.00015)}
        vix = bars(DAYS[-1], series(15.0, 0.0))
        return n5, vix, sectors

    def decide(self, **kw):
        n5, vix, sectors = self.world()
        with patch.dict("os.environ", {"SECTOR_FOLLOW": "1"}):
            return run_market(n5, daily(n5), vix, daily(vix), sectors,
                              {k: daily(v) for k, v in sectors.items()}, **kw)

    def test_the_note_is_appended_when_no_incumbent_was_readable(self):
        md = self.decide(prev_pick=None, sticky_note=" · REALTY not held — gone")
        self.assertIn("REALTY not held", md.pick_reason)

    def test_the_note_is_not_appended_when_an_incumbent_existed(self):
        md = self.decide(prev_pick={"name": "PHA", "side": 1},
                         sticky_note=" · REALTY not held — gone")
        self.assertNotIn("REALTY not held", md.pick_reason)
