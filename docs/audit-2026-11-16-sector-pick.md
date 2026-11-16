# Audit — does the scan pick the persistent mover from its sector?

**Question asked:** *"pick persistent moving stock from its sector."* Everything
below is judged against that one sentence: after the engine has chosen a sector,
does the stock it alerts belong to that sector's move, and is the sector it chose
being described on a tape that still exists?

**Method.** Every step of the path was read — `market.run_market` →
`_sector_snap` → `_complementary_pick` / `_follow_leaders` → `_scan_list` →
`pipeline.run_once` candidate loop → `stock.scan_stock` score → `trade.build_plan`
→ `format_stock`. Each hypothesis was then replayed through the shipped functions
on synthetic 5m frames (four sessions × 75 bars, IST): the sandbox has no route to
Yahoo, so nothing here comes from live data. Three of the four defects below were
reproduced this way; the fourth is a one-line reading of the message formatter.

The path, in the order it decides:

```
Nifty 5m ──► commit_side (dir_state / concrete lock)
              │
              ▼
   sector indices (15) ──► SectorSnap × 15  (day_ret, rs, pos, bull/bear, persist)
              │                    │
              │                    └──► PERSIST + / PERSIST − top 3        ← defect 1, 2
              ▼
   _complementary_pick  (Nifty committed)     _follow_leaders (Nifty NEUTRAL)
              │                                        │
              └───────────────► pick sector ───────────┘
                                   │
                                   ▼
                    _scan_list: EVERY stock in that sector
                                   │
                                   ▼
              scan_stock → 0–100 score → candidates.sort(...)  ← defect 3
                                   │
                                   ▼
                       MAX_ENTRIES_PER_PICK (3) → alert
```

---

## Findings

| # | finding | severity | status |
|---|---|---|---|
| 1 | a sector index that stopped printing is still scored, listed in PERSIST + and **picked** — `_sector_snap`'s `last_ts` argument was never read | **high** — the whole stock list hangs off a pick made on an old tape | **fixed** (`SECTOR_MAX_LAG_BARS`) |
| 2 | the pick can drop the most persistent sector for a **hair-thin RS reading**, and the block never said so (`PERSIST + FMCG …` with the leader silently gone) | medium-high — this is the churn that moved 16 Nov `PHA → FMCG` one candle later | **fixed** (`PICK_RS_MARGIN` + `passed over …`) |
| 3 | the entry cap ranks by the **absolute** 0–100 `score`, whose "rs" leg is `close > previous close` (one bar, 15 of 100) — never the stock against its own sector | **high for the stated goal** — "best score" ≈ "most extended chart", not "the mover" | **fixed** (`RANK_BY_SECTOR_LEAD`, `lead` on the alert) |
| 4 | `Live flow` never printed its side: 16 Nov showed `REALTY Q (85)` in a BULL block while REALTY was the **top of PERSIST −** | low, but it reads as the opposite of what it means | **fixed** (`REALTY▼`) |
| 5 | the path that *trades* (`_complementary_pick`) is **laxer** than the fallback (`_follow_leaders`): no persistence minimum, no range position, no live-flow minimum | medium | **fixed** (`PICK_MIN_PERSIST`) — see the fifth pass in [`live-defects`](live-defects-2026-11-16.md) |
| 6 | no hysteresis on the pick: a 2-point persistence margin is enough to switch sector mid-trend | medium | **fixed** (`PICK_STICKY_MARGIN`) — see the fifth pass in [`live-defects`](live-defects-2026-11-16.md) |
| 7 | `_complementary_pick` only looks at `pool[:3]`; a leader ranked 4th is unpickable | low (the top 3 is the design) | documented, no change |
| 8 | 14 stocks sit in 2–3 sectors (`SBIN` in FIN/PSUBK/BANK, `AXISBANK` in FIN/BANK …), so an alert's sector label depends on which pick was live | low | documented, no change |

---

## 1. A stale sector index was scored, listed and picked

`_sector_snap(name, df5, d1, sid, nifty_df, vol_mult, last_ts)` took the decision
bar and **never used it**. Whatever the sector's last available bar was, it was
scored — and the sector decides which stocks are scanned at all.

The stock level has always refused this: `pipeline.run_once` builds
`current = [sym for sym in tickers if to_ist(sm5[sym].index[-1]) == last]`, and a
stock whose frame does not reach the decision bar is dropped and reported. Yahoo's
*cash indices* gap far more often than stocks do, so the level that needed the
rule most was the one without it.

Replay — Nifty + three sectors, all four sessions, FMCG's index chopped 30
minutes early (decision bar 15:20, so five bars):

```
sector   last bar   age  close    day%   pos     rs   pBull  side  qual
MET         15:25     0  956.6  -1.10     8  -1.218    2.3    -1 False
FMCG        14:55    30 1059.7  +1.37    94  +1.197   91.8    +1  True
PHA         15:25     0 1014.9  +0.37    82  +0.214   50.5    +1 False

before:  PERSIST +  [('FMCG', 91.8), ('PHA', 50.5), ('MET', 2.3)]
         pick: FMCG | PERSIST + FMCG qualified (score 92)
after:   PERSIST +  [('PHA', 50.5), ('MET', 2.3)]
         pick: PHA  | PERSIST + PHA persistent (score 51)
         stale_sectors: [('FMCG', 5)]      →  ⚠ stale index  FMCG 5 bars behind
```

**Fix.** `lag_bars` is measured against the decision bar; a sector trailing by more
than `SECTOR_MAX_LAG_BARS` (2 bars = 10 min, `-1` = off) is marked `stale`, taken
out of PERSIST + / PERSIST − and out of both pickers, and named on the market
block. It keeps its row in the sector table, so a dead feed is visible rather than
silent. Tests: `tests/test_sector_pick.py` (5), including the counter-test that
`SECTOR_MAX_LAG_BARS=-1` puts the stale sector back in front — the guard is what
changes the outcome, nothing else.

## 2. A leader is dropped for a hair, and the drop is invisible

The complementary pick takes the first of `pool[:3]` that clears

```python
rs >= 0  and  day_ret >= -0.05  and  side != -1
```

`rs` is in **percentage points of index move**, so `rs = -0.01` is one hundredth of
a point — noise. It outranks nothing, yet it outranked a 31-point persistence gap:

```
leader persist 91 (rs -0.01) vs #2 persist 60 (rs +0.40)
  PICK_RS_MARGIN=0    -> pick MIDDLE (persist 60)
  default (0.05)      -> pick LEADER (persist 91)
```

That is also the mechanism behind the 16 Nov churn: the pick moved `PHA 56 →
FMCG 59` one candle later, and every block only ever describes the sector it
*chose*, never the one it walked past — so the reader cannot tell "the leader faded"
from "the leader was passed over on a rounding error".

**Fix.** `PICK_RS_MARGIN` (0.05 pp, `0` = old rule) and a `passed over` clause that
names what was skipped and why:

```
PERSIST + FMCG persistent (score 60) · passed over PHA (rs -0.01)
WAIT — persist #AUTO does not complement BULL market · passed over AUTO (flow bearish)
```

The same clause reports a stale sector (`index 6 bars behind`), so the two new
gates explain themselves in the alert rather than in the log.

## 3. The cap ranked by the wrong number — the one that matters for this goal

`MAX_ENTRIES_PER_PICK` keeps the top 3 of the names that fired on the pick:

```python
candidates.sort(key=lambda c: -float(getattr(c["best"], "score", 0) or 0))
```

`StockSignal.score` is the 0–100 technical composite:

```
trend_base 20/10/0 + vwap 10 + rsi 12/6 + macd 13 + adx 15/10/5 + vol 15/10 + rs 15
                                                                                ^^^^^^
                                    rs = 15 if close > previous close   (one bar)
```

So 15 of 100 points — as much as `ADX >= 25` — are spent on **one 5m bar's
direction**, and *nothing at all* compares the stock with the sector it was picked
from. "Best score first" is therefore "the most extended chart first", which is not
the same stock as "the one that is moving in this sector". With the cap in place
(16 Nov: 10 candidate entries → 6 taken) that ordering decides which three of six
pharma names you own.

**Fix.** Each candidate now carries a `lead`:

```
lead = (stock move from its session open) − (its sector index's move from its own
        session open)        in percentage points, × −1 for a SELL
```

the sort is `(-lead, -score)`, and the number is printed on the alert:

```
Sector PSUBK  |  Market BULL (88%)  ·  lead +1.9pp vs sector
```

`RANK_BY_SECTOR_LEAD=0` restores score ordering. Fixture (one sector, cap 3):

| stock | score | move from open | sector | lead | rank (lead) | rank (score) |
|---|---|---|---|---|---|---|
| BBB | 62 | +3.0% | +1.0% | **+2.0** | **1** | 3 |
| CCC | 77 | +0.6% | +1.0% | −0.4 | 2 | 2 |
| AAA | 82 | +0.4% | +1.0% | −0.6 | 3 | **1** |
| DDD | 62 | +0.2% | +1.0% | −0.8 | dropped | dropped |

Tests: `tests/test_live_alerts.py::SectorLeadRankingTests` — the leader takes the
first slot with the lowest score, takes the only slot when
`MAX_ENTRIES_PER_PICK=1`, and `RANK_BY_SECTOR_LEAD=0` reproduces the old order.

## 4. `Live flow` hid its side

```
Live flow  {md.live_flow} {'Q' if qualified else 'uQ'} ({score})
```

`MarketDecision.live_flow_side` existed and was never printed. On 16 Nov the block
read `Live flow REALTY Q (85)` in a BULL market while `PERSIST − REALTY 91` sat two
lines above — the same name, described as strong flow in the bullish-looking line
and as the strongest bearish persistence in the other. Now `REALTY▼ Q (85)` /
`PHA▲ Q (85)`.

---

## Proposals — A and B are now implemented

> A and B shipped behind knobs in the fifth pass of
> [`live-defects-2026-11-16.md`](live-defects-2026-11-16.md#fifth-pass--the-whole-repo-and-the-two-proposals):
> `PICK_MIN_PERSIST=50`, `PICK_STICKY_MARGIN=5`, both `0` = the behaviour described
> below. C is still a note.

**A. The path that trades is the laxer one.** `_complementary_pick` accepts any of
the top 3 that is `rs >= -0.05`, `day_ret >= -0.05` and not the opposite side: no
persistence floor, no range position, no live-flow floor. `_follow_leaders` — the
fallback used only when Nifty is NEUTRAL — demands `persist >= 55`, `day >= 0.20`,
`pos >= 60`, `rs > 0`, `bull >= 55`, `recent > -0.07`. So the engine is strictest
exactly when it is least sure. Proposal: `PICK_MIN_PERSIST` (default `0` = today,
recommended `50`) and reuse `BULL_MIN_POS` on the complementary path. It is a
knob rather than a fix because a bad value turns a whole day into `WAIT` — cheap to
test live, expensive to guess.

**B. No hysteresis on the pick.** Two candles with different margins switch the
sector (16 Nov: `PHA 56` at 09:35 → `FMCG 59` at 09:50). Since the cap and the early
alerts are now keyed per pick, a switch also resets the cap and cancels the pending
early alert, so the cost of churn went *up* this week. Proposal: the incumbent keeps
the pick until a challenger beats it by `PICK_STICKY_MARGIN` (5 points of
persistence), implemented by passing the previous pick + score into `run_market`.
Default `0` so a day can be compared with and without.

**C. Duplicate sector membership.** 14 names are in two or three sectors. The alert
names the sector of the pick that was live; `_scan_list` keeps the first. Harmless
today (one entry per symbol per session), worth remembering when a stock's sector
label is disputed.

## How to check the fixes live

`python -m nse_scanner doctor` now prints

```
5m alert rules: max 3 entries per pick · ranked by sector lead · …
sector pick: rs margin 0.05pp · index freshness 2 bars
```

and every block carries the reasons it needs to be auditable: `passed over …` when
a sector was skipped, `⚠ stale index …` when an index stopped printing, `lead ±X.Xpp
vs sector` on each entry. If a live day shows `⚠ stale index` on a sector that is
plainly printing (Yahoo timestamp skew rather than a dead feed), raise
`SECTOR_MAX_LAG_BARS`; if the ranking keeps choosing names you would not have,
`RANK_BY_SECTOR_LEAD=0` restores the old ordering for a direct comparison.
