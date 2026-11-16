# Audit — the 16 Nov 2026 **5m** feed (bots 1/2, 09:15 → 10:30)

The bot-3 stream was audited in [`audit-2026-11-16-alerts.md`](audit-2026-11-16-alerts.md).
This is the other half: every 5m alert in the same morning — market/sector blocks,
`🟢 BUY` entries, `⚡ EARLY … (unconfirmed)` heads-ups, `❌ EARLY CANCELLED`,
`🎯 TARGET HIT`, `🔔 EMA9 EXIT` — checked against the pick that was in force, the
rules in `main`, and against each other.

**The feed predates the session gate in `_track_positions`** (it still contains the
PNB hit that gate was written for), so it is a record of what the code did, not of
what it does now. Where today's `main` already answers a finding, it says so and
names the test.

## Timeline — and where the pick actually changed

`md.bar_time` is the **decision bar's open time**, so the block footed `09:35` is
the scan of the 09:30–09:35 candle, published at 09:35:xx. That matters: it makes
the whole morning consistent, including the two cancellations.

| block footer | pick | signal / conf | breadth | note |
|---|---|---|---|---|
| 09:15 | WAIT | NEUTRAL 29 | 10▲/5▼ | `PERSIST + PSUBK 59` but market TRANSITION 29 → no scan |
| 09:30 | WAIT | BULL 35 | 9▲/6▼ | TREND UP, but conf 35 → still no scan |
| 09:35 | **PHA** | BULL 56 | 8▲/7▼ | `PHA 56 · FMCG 54` — **a 2-point race**; six PHA entries follow |
| 09:40 | **FMCG** | BULL 63 | 8▲/7▼ | `FMCG 59 · PHA 54` — the flip, on the next candle |
| 09:45 | FMCG | STRONG BULL 99 | 9▲/6▼ | TREND-LOCK, MTF 3/3 |
| 10:20 | FMCG | BULL 67 | 7▲/8▼ | `FMCG 79`, `REALTY 91 · MET 90 · DEF 84` on the bearish side |

**A 35-minute hole.** Between the 09:45 and 10:20 footers no block was sent,
because a block only goes out when the label or the pick *changes*. In those 35
minutes market confidence went **99 → 100 → 94 → 67** while three entries were
taken (HINDUNILVR and VBL at 100%, GODREJCP at 94%) — see finding 5.

## The alerts

| time | name | alert | pick in force | plan (entry / SL / risk) | outcome |
|---|---|---|---|---|---|
| 09:40:20 | SUNPHARMA | BUY-EX7 · 72 | PHA 56 | 1894.80 / 1883.58 / 0.59% | 🔔 10:15 @1888.30 **−0.6R** |
| 09:40:27 | TORNTPHARM | BUY-EX11 · 82 | PHA | 4725.00 / 4694.74 / 0.64% | 🔔 10:20 @4710.00 **−0.5R** |
| 09:40:34 | CIPLA | ENTRY BUY · 62 | PHA | 1402.40 / 1397.95 / 0.32% | 🔔 10:25 @1400.50 **−0.4R** |
| 09:40:41 | AUROPHARMA | BUY-EX17 · 77 | PHA | 1841.60 / 1826.79 / 0.80% | 🔔 10:15 @1841.00 **−0.0R** |
| 09:40:48 | LUPIN | BUY-EX17 · 62 | PHA | 2001.00 / 1992.72 / 0.41% | 🎯 T1 2011.43 **+1.3R** · 🔔 10:20 **+0.2R** |
| 09:40:55 | GLENMARK | BUY-EX11 · 82 | PHA | 2678.30 / 2658.31 / 0.75% | 🔔 10:15 @2672.00 **−0.3R** |
| 09:44:01 | DIVISLAB | ⚡ 58s early | PHA 56 | 11534.00 / 11477.97 | ❌ cancelled 09:45 |
| 09:44:08 | BIOCON | ⚡ 51s early | PHA 56 | 367.55 / 366.26 | ❌ cancelled 09:45 |
| 09:49:01 | NESTLEIND | ⚡ 58s early → ✅ 09:50:20 | FMCG 63 | 1279.00 → confirmed 1278.00 / 1271.32 / 0.52% | 🎯 T1 1283.47 **+0.8R** · 🔔 10:30 **−0.1R** |
| 09:59:01 | HINDUNILVR | ⚡ 58s early → ✅ 10:00:13 | FMCG (100%) | 1995.00 / 1988.82 / 0.31% | 🔔 10:20 @1990.00 **−0.8R** |
| 09:59:08 | VBL | ⚡ 51s early → ✅ 10:00:20 | FMCG (100%) | 434.90 → 434.75 / 432.78 / 0.45% | 🔔 10:20 @433.40 **−0.7R** |
| 10:20:34 | GODREJCP | BUY-EX4 · 67 | FMCG 94% | 877.25 / 873.02 / 0.48% | 🎯 T1 878.00 +0.2R, still open |
| 09:15 | **PNB** | 🎯 T1 136.75 · T2 141.02 | — | entry 135.73 **from 11 Nov** | see finding 1 |
| 10:20 | **PNB** | 🔔 EMA9 EXIT @145.60 **+12.5R** | — | entry 135.73, 11 Nov | see finding 1 |

Session arithmetic, same-day trades only: **10 entries, 9 closed, 0 stop-outs,
3 T1 hits, −3.2R.** The three T1 hits were all given back (LUPIN +1.3R → +0.2R,
NESTLEIND +0.8R → −0.1R, GODREJCP +0.2R still open).

## Finding 1 — the PNB hit and exit: defect 3, still live in this feed

`🎯 TARGET HIT — BUY PNB · T1 136.75 · T2 141.02 · 5m 09:15` and
`🔔 EMA9 EXIT — BUY PNB · close 145.60 · entry 135.73 · +12.5R`.

The entry is **11 Nov's** (135.73, T1 136.75 — the trade in
`docs/hit-alerts-2026-11-11-pnb.md`). On 16 Nov the position was still in the
ledger, so the 09:15 candle's high "hit" two targets of a trade that had already
travelled 7.3% past its entry: the hit is not news, it is arithmetic on a stale
record. The exit that follows is meanwhile *correct* — somebody holding that trade
should be told to leave — which is exactly why the hit is dangerous: it reads like
a fresh target being reached.

**Fixed in `main`.** `_track_positions` drops any position whose
`state.position_session` is not today's *before* fetching or replaying anything,
and logs `dropping PNB: trade from 2026-11-11 is not today's (2026-11-16)`.
Pinned by `test_a_trade_from_an_earlier_session_is_dropped_not_trailed` and
`test_a_ledger_left_over_from_a_previous_day_never_produces_a_hit_alert`, the
second written from this very record.

**But note the trade-off**: a real position carried overnight is now dropped
**silently** — no hit, no stop, no exit, only a WARNING in the log. An operator who
was actually long PNB from 11 Nov stopped getting exit alerts on 16 Nov. If that is
not wanted, the gate needs a one-line "position from 11 Nov is no longer tracked"
notice; that is a policy decision, not a bug.

## Finding 2 — the 09:44 heads-ups were dead on arrival

`⚡ DIVISLAB` at 09:44:01 and `⚡ BIOCON` at 09:44:08, then
`❌ EARLY CANCELLED … is no longer in the scanned sector/side (09:45 close)` for
both. Four messages, zero actionable outcome.

They were **not** wrong when sent: the pick in force was PHA (the 09:35 block,
`run_early` reads `st["scan"]`, i.e. the most recent completed scan), and PHA was
still the pick at 09:44. The pick changed *at that very close* — the 09:40 block
(`FMCG 59 · PHA 54`) is produced by the 09:45:xx run. So the heads-up was correct
and its validating close was the one that killed it.

What makes it a finding is the **margin**: PHA beat FMCG by **2 points** (56 vs 54)
on the candle that produced six entries. A pick that close to a flip should not
generate heads-ups that a single close can invalidate.

Also note what "cancelled" hides: DIVISLAB and BIOCON were never re-checked. Their
signals may have held at the 09:45 close; nobody looked, because the sector was no
longer scanned. The message says the alert is dead — which is true — but it reads
like the signal failed.

## Finding 3 — the two "winners" that were never checked, and the test that says so

Covered in the bot-3 audit as well: the same `_settle_early` path resolves anything
the close scan did not look at. That is the correct behaviour, and the wording
should say it: `not checked — the 09:45 close picked FMCG` beats
`is no longer in the scanned sector/side`, and
`BUY DIVISLAB · BUY-EX6 is no longer in the scanned sector/side` parses as if
`BUY-EX6` were the thing that moved. Cosmetic, but this is the message a user reads
when they hold the stock.

## Finding 4 — six entries on one pick, entered within 35 seconds

09:40:20 → 09:40:55, six pharma names, same pick, same side, same 5m candle. There
is **no cap** on entries per pick, per sector, or per session (`grep` finds no
`MAX_ENTRIES`/`MAX_POSITIONS`). All six exited between 10:15 and 10:25 — five of
them within five minutes of each other, for −1.6R combined. Six positions, one risk
factor, one outcome.

Ranking would not have saved it: the two highest scores (TORNTPHARM 82, GLENMARK 82)
both lost. A cap would have: three entries instead of six would have halved the
cluster. Suggested: `MAX_ENTRIES_PER_PICK` (default 3), ordered by score, since the
burst is emitted in symbol-list order today (TORNTPHARM, 82, arrives second;
AUROPHARMA, 77, fourth — the operator sees rank 82, 62, 77, 62, 82).

## Finding 5 — the market collapse was invisible, and the entries kept coming

| when | market | conf |
|---|---|---|
| 09:45 block | STRONG BULL | 99% |
| 09:50 NESTLEIND confirm | STRONG BULL | 99% |
| 09:59 ⚡ HINDUNILVR / VBL | STRONG BULL | **100%** |
| 10:20 GODREJCP entry | STRONG BULL | **94%** |
| 10:20 block (sent 10:25) | BULL | **67%** |

The label changed only once — at the end — so exactly one block was sent. Between
99% and 67% the operator saw nothing, while three entries were taken. Breadth had
already gone 9▲/6▼ → 7▲/8▼ and `REALTY 91 · MET 90 · DEF 84` were climbing on the
bearish side.

Suggested: send the block on a **confidence move** as well as a label/pick change
(e.g. |Δconf| ≥ 10), or add one line to every entry alert when conf has fallen
since the pick (`Market STRONG BULL (94%) · pick conf 100% → 94%`).

## Finding 6 — T1 can be 0.2R, and the EMA9 exit fires on 0.01%

`_next_levels` takes the next daily pivot above entry with no minimum distance, and
`trail_broken` is a bare `close < ema` with no buffer. Both show up here:

| name | T1 distance | exit breach past EMA9 | as % of 1R |
|---|---|---|---|
| GODREJCP | **0.2R (+0.09%)** | — | — |
| AUROPHARMA | 0.6R | 1841.00 vs 1841.24 = **−0.013%** | 1.6% |
| NESTLEIND | 0.6R (plan) / 0.8R (hit) | 1277.00 vs 1278.47 = −0.12% | 22% |
| VBL | 1.8R | 433.40 vs 433.44 = **−0.009%** | 2% |
| LUPIN | 1.3R | 2002.80 vs 2004.02 = −0.06% | 15% |

* **GODREJCP was entered at 877.25 with T1 at 878.00** — the 13 Nov pivot high, 75
  paise away. Its "🎯 TARGET HIT +0.2R" is noise, and the entry was into immediate
  resistance.
* **Nine of ten exits came from the EMA9 rule, none from the stop.** The stop
  published on every alert (0.3–0.8% away) was never reached; the trail exited at
  0.009–0.13% past EMA9. AUROPHARMA was let go 24 paise past its EMA9 — **1.6% of
  its own stop distance** — and marked −0.0R.
* **All three T1 hits were given back** (LUPIN +1.3R → +0.2R inside 40 minutes,
  NESTLEIND +0.8R → −0.1R). Nothing moves the stop to breakeven after T1.

Suggested, in order of expected value: a **breakeven stop after T1**; a **buffer on
the EMA9 exit** (0.15% or 0.25R, whichever is smaller) or two consecutive closes; a
**minimum T1 distance** (skip or relabel a target closer than 0.5R).

## Finding 7 — delivery lag: the price on the alert is not a price you can get

`Signal calculated … +21s / +28s / +34s / +42s / +49s / +56s after close`. The
six-entry burst takes 35 seconds because each name is fetched and scanned in
sequence (and the order is the universe's, not the score's). The entry price on the
alert is the candle **close**, and R is computed from it, so a fill 20–56 seconds
later is worse by an unknown fraction of a stop that is only 0.3–0.8% wide.
AUROPHARMA's stop was 0.80% away; a third of a stop is 0.27%.

Not a bug — the loop is deliberately sequential and `MAX_ALERT_LAG_SECONDS=180`
bounds it — but it means the R in these messages is an upper bound. And at ~37
messages in 75 minutes (6 blocks, 5 ⚡, 2 ❌, 10 entries, 4 hit lines, 10 exits) the
channel is one message every two minutes.

## Finding 8 — two fields in the feed are internally inconsistent

* **VIX change frozen.** `VIX 15.56 ↓ -6.74%`, then `15.75 … -6.74%`, `15.68 …
  -6.74%`, `15.72 … -6.74%`. The level moves, the percentage never does. The code
  computes `vix_chg = (vix_today - vix_prev)/vix_prev` from the same fetch as
  `vix` (`market.py`), so the two cannot disagree — one of them was stale in
  whatever produced this feed.
* **Block vs alert confidence.** `94%` on the GODREJCP entry and `67%` on the
  10:20 block are *different runs*, not a contradiction: the block is only sent on
  a label change, so the 10:20 footer is the 10:25:xx run. Verified against
  `run_once`: `format_market` and `format_stock` print the same `md.dir_conf`, so
  any pair of numbers that disagree is proof the two messages came from different
  scans — worth knowing before treating a block and an alert as simultaneous.

## What is already answered in `main`

| finding | status |
|---|---|
| 1 — cross-session hit/exit | **fixed** — session gate in `_track_positions`, 2 tests written from this record |
| 2 — heads-up invalidated by the very close it waits for | open — pick margin / projected pick |
| 3 — cancellation wording | open — cosmetic |
| 4 — no cap on entries per pick | open |
| 5 — confidence collapse unreported | open |
| 6 — sub-0.5R targets, unbuffered EMA9 exit, no breakeven after T1 | open |
| 7 — sequential burst, R computed on the close | open — informational |
| 8 — stale VIX %, block-vs-alert reads | open — data integrity |

Defect 1 (duplicate/echo), defect 2 (missed leader) and defect 3 (cross-session hit)
are all covered: the projection lead on the flip candle, the bot-3 catch-up, and the
session gate. Everything above is a *quality* finding about the 5m engine itself —
the alerts are correct, and the strategy they describe still lost 3.2R on the
morning's high.

## Verified, positively, on this feed

* **No look-ahead on targets.** Every hit is stamped with the bar *after* the entry
  bar (`cursor = last_bar`, `replay = ts > cursor`), and the entry bar is excluded
  by construction — `test_entry_bar_never_produces_hit_alerts`.
* **No duplicate message** for any event in 75 minutes, including the two names
  whose head-ups were confirmed (`✅` sent once, then the entry).
* **The pick on every alert matched the pick in force** — six PHA entries under
  PHA, four FMCG entries under FMCG, no IT-sector or stale-sector alert.
* **The cancellations were honest** — both names were cancelled once, with the
  close time, and never re-announced.
* **`sent` blocked nothing wrongly**: 10 names, 10 entries, each once; GODREJCP at
  10:15 was a different name from every earlier one.
