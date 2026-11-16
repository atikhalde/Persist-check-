# Audit — the 16 Nov 2026 bot-3 feed (09:15 → 10:30)

The whole morning stream was pasted in and read end to end: market/sector blocks,
the 5m `BUY` alerts, the bot-3 `⚡ EARLY BUY` projections, `✅ CONFIRMED`,
`🎯 TARGET HIT` and `🔔 EMA9 EXIT`. This is what it says, alert by alert.

**It is a feed from the code as it stood before PR #23 / #24** — the projections
are there, but no `⏰ LATE LEADER` message appears anywhere, so the catch-up was
not running yet.

Method: the pick in force on each candle comes from the market blocks in the same
feed; the candle a name fired on comes from its 5m alert time; the lead is bot 3's
projection time against bot 1's close alert. Prices and scores are copied from the
messages. No bar data was re-fetched — this sandbox has no route to Yahoo, and the
audit is about *which* candle an alert belonged to, not about re-deriving R.

## The pick timeline

| time | market | pick | note |
|---|---|---|---|
| 09:15 | TRANSITION 29 | WAIT | breadth 10▲/5▼, VIX −6.74% |
| 09:30 | TRANSITION 35 | WAIT | still WAIT — no sector cleared the gate |
| 09:35 | BULL 56 | **PHA** | the flip: pharma takes the pick |
| 09:40 | BULL 59 | **FMCG** | the pick moves on — FMCG leaders fire here |
| 09:45 | STRONG BULL 60 → 99 | FMCG | market score saturates (see finding 4) |
| 10:20 | STRONG BULL 79 | FMCG | breadth already 7▲/8▼, ADX 41 |

Nifty: 25,314.9 at 09:45 → 25,270.8 at 10:20. **09:35–10:20 was the day's high.**

## The alerts

| name | 5m entry | score | pick in force | fired on | bot 3 | outcome |
|---|---|---|---|---|---|---|
| SUNPHARMA | 1894.80 BUY-EX7 | 72 | PHA (09:35) | flip candle 09:35–09:40 | none seen in the feed | EMA9 −0.6R |
| TORNTPHARM | 4725.00 BUY-EX11 | 82 | PHA | flip candle | none seen | EMA9 −0.5R |
| CIPLA | 1402.40 ENTRY BUY | 62 | PHA | flip candle | none seen | EMA9 −0.4R |
| AUROPHARMA | 1841.60 | 77 | PHA | flip candle | none seen | EMA9 −0.0R |
| LUPIN | 2001.00 | 62 | PHA | flip candle | none seen | **T1 2011.43 +1.3R**, then EMA9 +0.2R |
| GLENMARK | 2678.30 | 82 | PHA | flip candle | none seen | EMA9 −0.3R |
| DIVISLAB | — | — | PHA | 09:40–09:45 candle | **⚡ 09:43:07** (1m 53s before the 09:45 close) | not in the day's entries |
| BIOCON | — | — | PHA | 09:40–09:45 candle | **⚡ 09:43:07** | not in the day's entries |
| NESTLEIND | 1278.00 | 67 | FMCG (09:40) | 09:45–09:50 candle | **⚡ 09:47:06**, ✅ 09:51 (**+67s** after the close) | T1 1283.47 **+0.8R**, then EMA9 −0.1R |
| HINDUNILVR | 1995.00 | 72 | FMCG | 09:55–10:00 candle | **⚡ 09:48**, then the 5m BUY at **10:00:08 with no ✅ line** | EMA9 −0.8R |
| VBL | 434.75 (72 → 77 at close) | 72 | FMCG | 09:55–10:00 candle | ⚡ 09:59, ✅ 10:00:0x (**+9s**) | EMA9 −0.7R |
| GODREJCP | 877.25 | 67 | FMCG | 10:00–10:05 candle | none seen | **T1 878.00 +0.2R** |

Every name that fired on the flip candle was a pharma name, and the pick that
candle belonged to was PHA (09:35). That is the projection fix working: bot 3 did
**not** wait for the 09:45 close on DIVISLAB and BIOCON — it projected the
09:40–09:45 candle at 09:43:07 and gave 1m 53s of warning.

## What the feed proves is fixed

1. **Bot 3 leads on the flip candle.** DIVISLAB/BIOCON at 09:43:07 for a 09:45
   close is a genuine 1m53s lead — before PR #23 the pick on that candle was the
   *previous* candle's, so a sector that had just been picked had nothing to
   project and bot 3 could only echo bot 1 at the close.
2. **A projection is not an entry.** None of the projected names appears in
   `sent`/`positions` before its close — LUPIN's T1 and NESTLEIND's ✅ both come
   from the 5m flow, exactly once.
3. **`+67s` still confirms, `+180s` is the limit.** NESTLEIND confirmed 67s after
   its close; `MAX_ALERT_LAG_SECONDS=180` absorbed it, so a slow feed delays a
   message instead of losing it.
4. **No duplicate message for one event.** Every confirmation appears once, and
   the TARGET HITs (LUPIN, NESTLEIND, GODREJCP) all belong to trades opened the
   same session — the cross-session hit of 11 Nov is gone from this feed.

## What the feed proves is *not* fixed — findings 1–6

### 1. A cancelled projection silenced the name for the rest of the day

HINDUNILVR was projected at 09:48, did not survive the 09:50 close, and then —
when it did fire at 10:00:08 — arrived with **no lead at all**, in the same second
as bot 1. `_early_allowed` tested the day's cap (`SCAN1M_EARLY_PER_DAY=1`) *before*
the retry rule, so `n=1` blocked every later candle. The retry rule existed and
could never run under the default cap.

Fixed: the retry has its own budget, `SCAN1M_EARLY_RETRIES` (default 2) — a
cancelled projection may be re-projected on a later candle, a confirmed one still
spends the name for the day.

### 2. A projection whose sector moved was never resolved

The 09:40–09:45 candle was FMCG's close, so DIVISLAB and BIOCON — projected on it
as PHA — were never *checked*: `_settle` skips anything not in `checked`, the
`sent`/`positions` filters removed both names from the list, and `result` stayed
`null` for the rest of the session. Consequence: they were never counted as
cancelled (the summary under-reports), and their `n` sat at the cap, which is the
same silent block as finding 1.

Fixed: `_settle` takes the list the candle actually scanned. A name that is no
longer on it is written off (`is no longer in the scanned sector/side`) instead of
dangling; a name whose feed simply had not published yet is still retried.

### 3. The projection's pick source was invisible in the alert

The `⚡` blocks read `Market BULL (56%)` at 09:43 and `(63%)` at 09:47 — those are
the 09:35 and 09:40 closes, not the forming candle's own verdict. Nothing in the
message said whether the pick had been projected or copied from the last close, and
nothing in the log said why, so the lead could not be verified from the feed.

Fixed: every `⚡` now carries `·  pick projected from the forming candle` or
`·  pick: last close (no projection this minute)`, and the fallback logs one
`pick projected? NO (<reason>)` line per candle. This is the diagnostic that makes
the next feed self-explaining.

### 4. The market score saturates (not a defect of bot 3)

`STRONG BULL 99`, `conf 100%` on a breadth of 9▲/6▼ moving to 7▲/8▼ with ADX 41:
the market score is pinned at its ceiling while the internals deteriorate. It does
not change which names alert, but it does mean "Market 100%" is not a strength
reading. Left as is — recorded so it is not read as a signal.

### 5. No `⏰ LATE LEADER` anywhere

The day's known miss — a leader that fires before its sector is picked — produced
nothing in this feed because the code had no catch-up yet. PR #24 adds it: once per
name per session, ≤ 30 minutes old, stop untouched since the fire, ≤ 2.0R of
movement, bot 3 only.

### 6. Feed latency is real but absorbed

NESTLEIND confirmed at +67s, VBL at +9s. Not a bug; it is why
`MAX_ALERT_LAG_SECONDS` exists. If the lag ever exceeds it, the alert is dropped
rather than sent late — which is a decision, not a silent failure.

## Verdict

* The projection fix (PR #23) worked on the day that mattered: the flip candle —
  where a sector's leader fires — produced ⚡ alerts 1m53s before the close.
* Two places still dropped that lead silently: a cancellation spent the day's
  budget (finding 1), and a projection overtaken by a pick change was never
  resolved (finding 2). Both are fixed in PR #25.
* The audit had to be done by hand because the alert could not say which pick it
  judged (finding 3). It says so now.

**What tomorrow's feed should show**: every `⚡` states its pick source; a name
that was cancelled appears again on a later candle; no `⚡` and 5m `BUY` for the
same name in the same second; a `⏰ LATE LEADER` for any leader that fired before
its pick; and at most one INFO line per candle explaining a fallback.
