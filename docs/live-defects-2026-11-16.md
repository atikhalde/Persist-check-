# 16 Nov 2026 — the three live defects: cause, fix, and how to prove it

Reported from the live feed:

1. **Both scanners fired the same alert at the same second.** The 1-minute scan
   exists to deliver the signal up to four minutes before the 5m scan; an
   identical simultaneous alert is the one outcome that makes it pointless.
2. **The scan missed the sector leader** and sent late alerts for the followers
   — "many FMCG alerts but ITC missed", the same in pharma.
3. **A TARGET HIT alert belonged to another day**:
   `🎯 TARGET HIT — BUY PNB · T1 136.75 (+0.75%, +1.3R) · T2 141.02 (+3.90%, +6.7R) · 5m 09:15 · entry 135.73`
   — PNB was signalled on **11 Nov**; the alert fired on **16 Nov**.

Each has a single, code-level cause. None of them is a rule difference between
the two scans, and none of them needed a rule change to fix.

---

## 1 + 2. One root cause: the projection used the *previous* candle's pick

`scan1m.pick_for(bar, projection=True)` — the pick used to decide *which names*
a forming candle may alert — returned the pick of `bar − 5m`:

```python
allowed = [(bar - timedelta(minutes=BAR_MINUTES)).strftime("%Y-%m-%d %H:%M")]
```

That is the pick the 5m scan decided at the **last close**. But the 5m scan
alerts an entry on the candle whose close establishes the pick
(`pipeline.run_once` decides `md` from `last` and then scans `md.pick_sector`
for signals **on that same bar**). So the two scans are structurally
misaligned on exactly one kind of candle: **the candle where the market flips
to a new sector.**

What that does, in the order it hurts:

| on the flip candle (say 10:00, FMCG becoming the pick) | bot 1 (5m) | bot 3 (1m, before this fix) |
|---|---|---|
| 10:01–10:04 | nothing (waits for the close) | scans the **old** sector. FMCG is not on the list, so ITC — the leader that moves first when FMCG flips — is never even looked at |
| 10:05 close | pick = FMCG → ITC's entry alert | pick = FMCG (confirm pass) → the **same** ITC entry alert, same second |
| 10:10, 10:15 … | follows the sector | now scans FMCG, and the *followers* (which fire on the later candles — persistent NORMAL/base conditions) get genuine `⚡ EARLY` alerts |

That is defect 1 and defect 2 in one mechanism: the **leader** is the name that
fires on the flip candle (no lead, or missed), the **followers** are the names
that fire afterwards (early — so the feed reads "late leader, early followers").

**Fix — `scan1m.projected_market`.** The pick is now projected the same way a
stock is: Nifty, VIX and every sector index are rebuilt as
`[completed 5m bars] + [forming candle from 1m prints]` and the **unchanged**
`run_market` runs on them (`_prepare_index`, `sanitize_index_5m`,
`_complementary_pick`, `_follow_leaders`, `SECTOR_ORDER` — all of them, no
fork). `pick_for(projection=True)` now prefers, in order:

1. a pick the 5m scan has already written for `bar` itself;
2. **the forming candle's own verdict** (`projected_market`);
3. the previous candle's pick — the old behaviour, used only when (2) cannot be
   computed (no 1m prints for the indices, a lagging feed, sanitation moving
   the bar, or `SCAN1M_MARKET_SOURCE=ledger`).

Consequences worth knowing:

* a **projected** pick never sends a MARKET/SECTOR message — that statement
  belongs to a finished candle, so it waits for the close, exactly as on bot 1;
* `SCAN1M_MARKET_SOURCE=ledger` can never project (the 5m ledger holds closed
  candles only); `own`/`auto` is what buys the lead. The workflow already runs
  `own`;
* the projection is recomputed every minute of the candle, so a pick that flips
  at minute 3 alerts at minute 3 (2 minutes early), not minute 1.

### Measured: the flip candle is real, and it is the one bot 3 could not see

`tests/test_projection_lead.py::RealMarketEngineTests` drives the **real**
`run_market` (no mock) over a synthetic session where one sector index runs up
and the rest drift down. Sweeping the candles from 10:55:

```
10:55  FORMING candle says AUTO 1 (SCAN LONG) | last CLOSED candle said WAIT 0
       → bot 1 learns this at 11:00; bot 3 can alert from 10:56
```

One flip in the window, and it is exactly the reported shape: the 5m scan has
*nothing* to scan on the candle the pick appears on (it still holds WAIT), while
the forming candle already says `AUTO, long`. Before this change bot 3 also held
WAIT — it scanned nothing for four minutes and then echoed bot 1's entry at
11:00. The leader of AUTO fires on the 10:55–11:00 candle; the followers fire on
the candles after it. Same seed, same result on every run.

## 2b. The second half of the miss: a partial candle's volume

`data.provisional_5m` sums the 1m prints seen so far. At minute 1 that is about
a fifth of the candle, and the engine reads volume in three places:

* `rel_vol >= 0.2` gates `entry_buy_today` / `entry_sell_today`;
* `vol_sc` gives 15 score points only when `rel_vol > 1.5` (10 otherwise);
* `entry_buy = agg_buy and total > 60`.

So a name whose completed candle scores 65 (with `rel_vol > 1.5`) scored **60**
at minute 1 — not an entry — and only crossed 60 once the candle was nearly
over. The projection then appeared at minute 3–4, i.e. 1–2 minutes before bot 1,
or not at all when the score sat exactly on the line.

**Fix — `data.project_volume`.** The forming bar's volume is finished from its
own prints (`volume so far × 5 / minutes covered`). The factor is exactly 1.0
at the close, so `closed_frame` — the frame *both* scanners confirm at the
close — is untouched: this can only make a projection appear earlier, never
change what the close confirms. It is applied by `scan1m.projected_frame` only;
`pipeline.run_early` (bot 1's own 60-second heads-up) is unchanged.

## 3. A trade is intraday — the stale TARGET HIT

`pipeline._track_positions` replayed "every candle since the last check" *within
the current session*:

```python
replay = [ts for ts in grid.index
          if (cursor is None or ts > cursor) and session_date(to_ist(ts)) == session]
```

and `state.reset_if_new_day` deliberately carried positions across days
("trail until an exit, even across sessions"). The two together are the bug: a
position opened on 11 Nov is still open on 16 Nov, its `hit_targets` is stale,
and the **16th's** 09:15 candle is checked against the **11th's** entry. A gap
up through both levels in one candle produces exactly the reported line — two
targets of an eleven-day-old trade "hit" at 09:15, an entry price from a
different week.

**Fix — the position dies with its session.**

* `state.position_session(pos)` — the session a trade belongs to (its
  `entry_bar`, or its stored `session`);
* `state.reset_if_new_day` keeps only positions of the new session;
* `pipeline._track_positions` **also** drops, before any fetch, a position whose
  session is not today (the live ledger had already been rolled over to the
  16th, so the rollover alone cannot see it). It logs
  `dropping PNB: trade from 2026-11-11 is not today's (2026-11-16)` and sends
  nothing. Both `_note_reject`-free and message-free by design: an alert about
  a trade that is not today's is noise, not news;
* new positions store `"session"` (both scans).

A restart **inside** the same session still keeps an open trade open — that is
the only reason the carry-over existed.

---

## Proving it tomorrow, from the log

The runner's log is the audit trail. Per alert, these lines are enough:

```
grep -E "1m EARLY|1m CONFIRMED|ALERT |SKIP historical|1m no projection|1m: last .* fire|dropping " live.log
```

What to look for:

| check | pass condition |
|---|---|
| lead | every `1m EARLY … (minute k of 4, n min before the close)` has `n ≥ 2`; `minute 1` is the ideal |
| not a duplicate | no `1m CONFIRMED` whose text repeats bot 1's `ALERT` **without** a preceding `1m EARLY` for the same name and candle |
| leader first | on the candle the pick flips (the `market … pick=FMCG` line), the first alert of that sector is on its **strongest** name |
| pick source | `1m EARLY … (projected pick, …)` — `ledger pick` means the projection could not be built and the lead was lost |
| missed names | `SKIP historical <NAME> fires=ENTRY BUY@09:20` = the signal fired **before** its sector was picked (no catch-up rule); `no signal on the forming candle` = the engine did not fire on this candle (see the score arithmetic above) |
| stale trades | no `TARGET HIT` / `SL HIT` line whose `5m hh:mm` is on a different date than the entry's |

The 15:33 summary now carries two extra lines so the day is self-documenting:

```
Average lead 3.2 min before the 5m close (earliest 4 min)
Checked, not projected: no signal on the forming candle 96 · no valid EMA9 stop yet 7 · 1m feed missing/stale 2
Signals not alerted: 1 fired before their sector was picked (ITC 09:20)
```

That last line is the one that answers "was the leader missed, or did it never
fire?" — and it is the only remaining way a leader can be absent, because the
`earlier` reject is deliberate: a signal calculated from a candle the scan was
not watching for that sector is not an entry alert (no catch-up trades). If the
log shows `earlier ITC@09:20` for a leader you wanted, that is a **policy**
question, not a bug — see the open question at the end of this note.

## What this did not change

* `stock.py`, `market.py`, `trade.py`, `telegram.send()`, `live-scan.yml`: no
  edits. Bot 1 and bot 2 behave exactly as they did.
* The confirmed stream on bot 3 is still `pipeline.format_stock`, character for
  character, and still uses the *closed* candle's pick — the `projection=False`
  path is untouched.
* Silence on a failed projection is still the default
  (`SCAN1M_CANCEL_MESSAGE=1` to report it).

## The alert-by-alert analysis you asked for

I could not do it from this workspace: there is no alert log, ledger or state
file in the repository (`data/` holds only `oi_history.csv`, `state/` is empty),
and this sandbox has no route to Yahoo/Google, so the day's candles cannot be
re-fetched here either. What is needed, either way:

* the day's bot-1 **and** bot-3 messages (Telegram export, or the Actions log
  lines above) — timestamps to the second;
* for each alert: the name, the 5m bar, the sector pick at that candle, and the
  lead (or its absence).

With those, each alert gets the same three questions — (i) which candle's pick
was in force, (ii) did the name fire on that candle or an earlier one, (iii) how
many minutes separated bot 3 from bot 1 — and the result is a table like the
one above, per alert, with the price move added from the runner (the bot has
the 5m bars; this sandbox does not).

## The catch-up (decided: bot 3 only, bounded)

A leader whose signal fired **before** its sector was picked is still not a new
signal on the candle being scanned — the `earlier` reject is correct — so it
gets its own labelled message instead of being squeezed into the entry stream:

```
⏰ 🟢 LATE LEADER BUY — fired before its sector was picked
ITC · sector FMCG  |  Market BULL (74%)
Fired 09:40 as ENTRY BUY @ 100.00 · sector picked 10:00 · 16 min ago
Price now 100.03 (+1.5R from the fire) · SL 99.98 (the fire candle's EMA9 stop, untouched since)
Risk if taken now ≈ 0.05 (0.05%)  |  next pivot target: T1 R1 100.02 (1.0R, +0.02%) …
Score 65 Strong   base 20/21 cond22=1 score=65
5m bar 10:00–10:05 forming · 3m 54s left
Not an entry alert — no signal fired on this candle; bot 1 has none for this name
either. Check the stop yourself before acting.
```

Rules, all of them explicit:

| gate | value |
|---|---|
| where | bot 3 only; bot 1 never sends it, and `sent` / `positions` are **not** written (it is not an entry, so the normal entry flow still applies to that name) |
| how often | once per name per session (`st["catchup"]`); the scan runs only around a pick change (`catchup_due`, 10 minutes) so the extra fetch is bounded |
| age | the fire must be within `SCAN1M_LEADER_MAX_MINUTES` (default **30**) of the current minute |
| still valid | no candle since the fire has touched the stop that fire defined (`plan.sl` from the fire candle's EMA9) |
| not a chase | the move from the fire entry must be ≤ `SCAN1M_LEADER_MAX_R` (default **2.0R**) |
| off | `SCAN1M_LEADER_CATCHUP=0` |

The 15:33 summary counts them: `LATE LEADER catch-ups 1 (signal fired before its
sector was picked)`.

This is why it was not silent-by-default: a missed leader is the one case where
*no* message is worse than a labelled one — you cannot act on a signal you never
saw, and you cannot tell "the leader never fired" from "the leader fired before
its sector was picked" without the log. The message says exactly which of those
happened, carries the fire's own stop and R, and states in the text that bot 1
has no alert for it.

## Second pass — after the feed was read end to end

The alert-by-alert audit of the 16 Nov feed is in
[`docs/audit-2026-11-16-alerts.md`](audit-2026-11-16-alerts.md). It confirmed the
projection fix on the candle that matters — DIVISLAB and BIOCON were projected at
09:43:07 for a 09:45 close, a real 1m53s lead — and found three further causes of
"bot 3 has nothing to say, so bot 1 says it":

| # | what the feed showed | cause | fix |
|---|---|---|---|
| 1 | HINDUNILVR projected 09:48, cancelled at the 09:50 close, then entered at 10:00:08 with **no lead** | `_early_allowed` tested the day's cap (`SCAN1M_EARLY_PER_DAY=1`) *before* the retry rule, so `n=1` blocked every later candle — the retry could never run under the default cap | the retry has its own budget, `SCAN1M_EARLY_RETRIES` (default **2**); a confirmed projection still spends the name for the day |
| 2 | DIVISLAB / BIOCON projected on the 09:40 candle, never confirmed, never cancelled, never counted | `_settle` only resolved names the close scan could judge; the next candle picked FMCG, so both left `sent`/`positions` and their projections dangled with `n` at the cap | `_settle(..., scanned=names)` writes off a projection that is no longer on the scanned list (`is no longer in the scanned sector/side`) and still retries one whose feed had not published |
| 3 | `Market BULL (56%)` at 09:43 and `(63%)` at 09:47 — the 09:35 and 09:40 closes, not the forming candle's verdict, with nothing saying so | the projection fell back to the last close's pick silently, so neither the alert nor the log could show whether the lead was real | every `⚡` carries `·  pick projected from the forming candle` or `·  pick: last close (no projection this minute)`, and `_projection_pick` logs one `pick projected? NO (<reason>)` line per candle |

Findings 1 and 2 are the same failure in two places: **a projection could be spent
without ever being resolved**, and an unresolved projection is a name that is
silent for the rest of the session. Both now end in `cancelled`, which is counted
in the summary and does not consume the budget.

Two things in the feed are reported and deliberately **not** changed:

* **the market score saturates** — `STRONG BULL 99` / `conf 100%` on a breadth that
  went 10▲/5▼ → 7▲/8▼ with ADX 41. It changes nothing about which names alert, but
  "Market 100%" is not a strength reading and should not be read as one;
* **the pharma leaders were all given up within 40 minutes** by their 5m EMA9
  exits while 09:35–10:20 was the day's high. That is the strategy, not the
  plumbing — the projection did its job and the entries still lost.

## Third pass — the 5m stream, and what it costs

[`docs/audit-2026-11-16-5m-alerts.md`](audit-2026-11-16-5m-alerts.md) reads the
bot-1/2 feed the same way: the alerts are sound (no duplicate event, no look-ahead,
the pick on every alert was the pick in force) and the morning still lost 3.2R on the
day's high. Five gates came out of it, all of them knobs with the old behaviour one
value away:

| knob | default | what it fixes |
|---|---|---|
| `MAX_ENTRIES_PER_PICK` | 3 | six entries on one pick in 35 seconds → three, most ahead of its sector first |
| `MIN_TARGET_R` / `MIN_TARGET_PCT` | 0.5 / 0.10% | a "TARGET HIT +0.2R" 75 paise from entry |
| `EXIT_EMA9_BUFFER_PCT` / `_R` | 0.10% / 0.25 | nine exits on breaches of 0.009–0.13%; five of them are held |
| `BREAKEVEN_AFTER_T1` | on | all three T1 hits were given back; the stop now moves to entry |
| `MARKET_CONF_DELTA` | 10 | 99% → 67% in 35 minutes with no message while entries were taken |

None of them changes *which* signal fires. They change what the same signal is
allowed to cost.

## Fourth pass — the sector pick, and the stock inside it

[`docs/audit-2026-11-16-sector-pick.md`](audit-2026-11-16-sector-pick.md) walks
the other half of the path: which sector gets picked, and which stock inside it.
The goal is one sentence — *pick the persistent moving stock from its sector* — and
three of the four defects were against it:

| # | what was wrong | why it mattered | fix |
|---|---|---|---|
| 1 | `_sector_snap` took the decision bar as `last_ts` and **never read it** | a sector index that stopped printing 30 minutes ago was scored, listed in PERSIST + and *picked* — and the pick decides which stocks are scanned at all | `SECTOR_MAX_LAG_BARS` (2) + `⚠ stale index` on the block |
| 2 | a leader was dropped for `rs -0.01` (five basis points) and the block never said so | this is the churn that moved the 16 Nov pick `PHA → FMCG` one candle later, with no way to tell it from "the leader faded" | `PICK_RS_MARGIN` (0.05 pp) + `· passed over PHA (rs -0.01)` |
| 3 | the entry cap ranked by the absolute 0–100 `score`, whose "rs" leg is `close > previous close` — one bar, 15 points | "best score first" was "most extended chart first": the follower with the prettier chart outranked the sector's mover | `RANK_BY_SECTOR_LEAD`, ranked by stock-minus-sector move from each session open, printed as `lead +1.9pp vs sector` |
| 4 | `Live flow` never printed its side | 16 Nov read `REALTY Q (85)` in a BULL block while REALTY was the **top of PERSIST −** | `REALTY▼ Q (85)` |

Two findings are proposals rather than changes, both because the wrong value turns
a day into `WAIT`: the complementary pick has **no persistence floor** (laxer than
the `SECTOR_FOLLOW` fallback that needs 55), and there is **no hysteresis** — a
2-point margin switches the sector, which now also resets the per-pick cap and
cancels the pending early alert.


## Fifth pass — the whole repo, and the two proposals

The first four passes audited the alert streams. This one reads the codebase
top to bottom — every module, both workflows, the CLI and its `doctor` line —
against the same sentence: **pick the persistent moving stock from its sector.**
Eight defects came out of it; the two proposals from the fourth pass are now
implemented behind knobs.

### Fixed

| # | what was wrong | why it mattered | fix |
|---|---|---|---|
| 1 | `run_market` built PERSIST + / PERSIST − by **sorting on persistence alone** | a sector that led all morning and then rolled over stayed at the top of PERSIST + with a bearish side, and a BULL market could buy the reversal | the pools are filtered to `side == +1` / `−1` first, then ordered; the same sector still shows its persistence — in the other pool |
| 2 | the complementary pick had **no persistence floor** | it accepted anything that was merely not fighting the market, while the `SECTOR_FOLLOW` fallback demanded 55: a drifting sector could outrank one that had held | `PICK_MIN_PERSIST` (50); the sector passed over is named (`passed over AUTO (persist 42 < 50)`) |
| 3 | there was **no hysteresis** — a 2-point margin switched the sector | 16 Nov: `PHA → FMCG` one candle later, which also reset the per-pick cap, dropped the pending early alert and rescanned the same stock in another sector | `PICK_STICKY_MARGIN` (5). The incumbent is only ever *held*: stale, below-floor or rolled over and it is dropped at once, so the rule can delay a switch but never prevent one |
| 4 | bot 3 confirmed **every** name that fired on the close | 16 Nov: six pharma entries in 35 seconds where bot 1 announced three — the same pick was one risk factor on one bot and six on the other | `confirm_pass` collects all candidates, ranks them with the same `rank_candidates`, and enforces the same `entry_cap_room` / `take_entry_slot` on `pick_cap_key`. A capped name is resolved with its reason (`not taken — PHA long already has its entries`), not left to read "did not hold" |
| 5 | bot 3 wrote its own position record | it had lost `sl0`, `risk` and `t_names`: target alerts read `T1 103.20` where bot 1 read `T1 R1 103.20`, and every later R was measured against a stop that had moved | both loops call `new_position` |
| 6 | the catch-up window lived only in memory | each minute started a fresh 10-minute window, so a name whose feed lagged was retried until the pick changed | `catchup_due` writes its anchor to the ledger (and to `state`), so the window survives the minute — and the process |
| 7 | `session.py` carried **dead entry guards** — `is_open_blocked`, `is_late_blocked`, `is_freeze_time`, `is_market_hours` — with `OPEN_BLOCK_MIN` / `NO_ENTRY_AFTER_MIN` / `FREEZE_AFTER_MIN` | nothing called them, so they read like a safeguard that does not exist; the master scanner's own windows (`BUY_W1 09:16–09:59` … `FIN_BUY 09:26–11:15`) are the rules that actually bound an entry | removed, with a note saying so. `OR_MINUTES` (used) stays |
| 8 | `oi_backtest` fetched a 1-minute frame per bar and **threw it away**; three legacy modules carried unused imports and locals | a network round-trip per bar in a replay, for nothing — and the kind of residue that hides a real bug | the fetch is gone; `pyflakes` is now clean on every module |

### The two fourth-pass proposals, now knobs

| knob | default | old behaviour |
|---|---|---|
| `PICK_MIN_PERSIST` | `50` | `0` — no floor |
| `PICK_STICKY_MARGIN` | `5` pp | `0` — switch on any margin |

Both are settable in the workflow (`vars.*`), printed by `doctor`
(`persist floor 50 · hold the incumbent by 5`) and shared by both loops. A day
can be replayed with `PICK_MIN_PERSIST=0 PICK_STICKY_MARGIN=0` to compare it
against the old pick.

Full-correctness set for the sector pick:

```
SECTOR_MAX_LAG_BARS=2  PICK_RS_MARGIN=0.05  PICK_MIN_PERSIST=50  PICK_STICKY_MARGIN=5  RANK_BY_SECTOR_LEAD=1
```

### Not changed, and why

* **`STRONG BULL 99` saturation** and **`rsi` returning NaN instead of Pine's 100
  on a zero-loss window** are reported, not fixed: neither changes which name
  alerts, and the second would change the score of a chart the parity harness
  reproduces bar for bar.
* **the six-entry morning itself** is the strategy's outcome, not plumbing. The
  cap and the lead ranking change what the same signals cost; they do not
  promise a better day.

### Proof

`pytest -q` → **313 passed**; `pyflakes` clean on `nse_scanner/` and `tests/`.
New tests cover every row above: the pool side filter (a sector that gives back
3% is a minus with persistence 93), the floor and its `0`, the sticky hold /
clear switch / faded / stale / below-floor incumbent, `previous_pick` adjacency
(only the candle 5 minutes earlier, never `WAIT`), bot 3's cap + lead ranking +
pivot names + "not taken" cancel reason, the CatchupWindow window anchor, and
the `new_position` record both bots now share.

## Follow-up — the 11:55 WAIT block, and what "the day's sector" means when it is two

The block from the 11:55 feed:

```
MARKET BULL  conf 41% WEAK  (was 67%)
Nifty 25,255.2  day +0.44%  intra +0.19%
VIX 15.69 ↓ -6.74%   ADX 15   MTF 3/3
Breadth 5▲/10▼  TRANSITION
PERSIST +  FMCG 93 · BANK 44 · FIN 32
PERSIST −  REALTY 91 · AUTO 86 · DEF 83
Live flow  AUTO Q (79)

SECTOR PICK: WAIT  FLAT
WAIT — market TRANSITION conf 41
```

Read from the feed: *"today's sector could be REALTY and FMCG."* That reading is
right, and the block had already measured it: **FMCG 93 is the most persistent
bullish sector, REALTY 91 the most persistent bearish one.** They are the two
ends of the same rotation — a defensive bid (FMCG) with property offered
(REALTY) — on a tape where the index is held up by a few names while 5 stocks
advance and 10 decline. That is what `PERSIST +` / `PERSIST −` are for.

Three things were wrong with the block, and only the third changes behaviour.

### 1. It opened with a direction it had refused to act on

`MARKET BULL conf 41% WEAK` sat directly above `WAIT — market TRANSITION conf
41`. A bullish header on a 5▲/10▼ tape, followed by an action of `WAIT`: the
engine names a side and then declines to use it, and the reader is left to guess
which half to believe. A block that trades nothing now says so, and keeps the
lean as a lean:

```
MARKET STAND ASIDE  BULL lean · conf 41% WEAK  (was 67%)
...
WAIT — market TRANSITION conf 41 < 55
```

`dir_label` itself is untouched (it is the *commit* side, and the summaries and
the "did the label change" test depend on it) — only the rendered header
distinguishes "trading this side" from "leaning that way".

### 2. It never answered "which sectors are moving?"

The two pools answer it, but only if you know that `PERSIST +` is long and
`PERSIST −` is short. A WAIT block now says it in one line, on both sides:

```
No pick · persistent movers  FMCG long 93 · REALTY short 91
```

Display only. Nothing is scanned, no entry is opened.

The `Live flow` line in the pasted block also reads `AUTO Q (79)` with **no
arrow**, while `AUTO` is the #2 of `PERSIST −`: that block predates the
`REALTY▼` fix (PR #28) and reads `AUTO▼ Q (79)` on this tree.

### 3. The two gates left a gap, and the day fell in it

```python
# _complementary_pick
if commit_side == 0 or regime == "CHOP" or dir_conf < PICK_MIN_CONF:   # 41 < 55 -> WAIT
    return None, 0, "WAIT ..."

# run_market, before this change
if not pick and commit_side == 0:        # <- and the fallback needs NO side at all
    leaders = _follow_leaders(persist_plus)
```

The engine could pick a sector with a confident direction, or follow one with no
direction at all. A direction it did not trust — `commit_side == 1` at conf 41 —
was the one case with **no path to a sector**, and FMCG 93 was sitting in that
gap.

`SECTOR_FOLLOW_ON_WEAK` (default **0**) closes it: a weak committed BULL falls
back to the same strict gates the NEUTRAL case uses — persist ≥ 55, up on the
day, upper half of the range, positive RS, bullish live flow, not rolling over —
which is much stricter than the complementary path it replaces. The reason line
says which market it is: `SECTOR FOLLOW — Nifty BULL weak (41%), FMCG leads
(persist 93, day +0.90%, range pos 88, flow aligned)`.

It is **off by default** because the locked rule is `conf < 55 → WAIT`, and
because 16 Nov is the day that rule was written for: six entries on a weak tape
in 35 seconds. Turning it on trades the *strict* path, not the lax one.

Two decisions were taken with it:

* **the weak path is stricter than the flat one** — `SECTOR_FOLLOW_WEAK_MIN_PERSIST`
  (65) against the NEUTRAL case's 55. On a day where the index offers no help,
  only the exception is worth following: FMCG 93 qualifies, a sector at 58 does
  not;
* **the pick stays one-sided.** Asked on 16 Nov, the answer was one side only: on
  a `commit_side == 1` bar the top of `PERSIST −` is a reading on the block,
  never a pick. `_follow_leaders` is long-only and a test now pins it —
  REALTY 91 with `persist_bear 95` is refused even with the weak path on.

A weak BEAR still waits: the mirrored gates are not written, and a
half-mirrored rule is worse than none.

### "REALTY was top persistent — why was FMCG picked?"

Because the two numbers are not on the same scale, and because the pick is
direction-first. From a replay of the same two sectors, the only thing that
changes between the two runs is the Nifty series:

| sector | side | persist **+** | persist **−** | day | range pos |
|---|---|---|---|---|---|
| FMCG | +1 | **96.8** | 1.2 | +2.24% | 96 |
| REALTY | −1 | 0.8 | **97.3** | −3.28% | 3 |

* Nifty rising → `pick FMCG` (`PERSIST + FMCG qualified (score 97)`);
* Nifty falling → `pick REALTY` (`PERSIST − REALTY qualified (score 97)`);
* Nifty flat → `pick FMCG` (`SECTOR FOLLOW — Nifty NEUTRAL, FMCG leads`).

REALTY's 91 is **bearish** persistence — its score on the bull metric is 0.8.
Putting 91 next to FMCG's 93 compares a fall with a rise. The pools never rank
against each other: `commit_side` chooses the pool, and only then does
persistence order it. On a BULL bar REALTY is not second, it is **not a
candidate**, and in the morning — before the concrete lock and conf ≥ 55 — the
lane that runs is `SECTOR FOLLOW`, which is long-only by construction.

On 16 Nov the index was `BULL` in both blocks, so **no version of the current
rule picks REALTY that day**, at 11:55 or in the morning. What would is a
divergence rule (index up, breadth 5▲/10▼ → allow a `PERSIST −` short with the
mirrored gates) — a new rule, not a bug. Pinned by `DirectionFirstTests`.

### The divergence rule — `DIVERGENCE_SHORT` (off, for testing)

The one tape the one-sided rule cannot trade: **index up, breadth against it.** On
16 Nov 11:55 that was `Nifty +0.44%`, breadth `5▲/10▼`, `PERSIST − REALTY 91` —
and no pick, because a BULL bar has no short pool. The rule, when
`DIVERGENCE_SHORT=1`:

| condition | value |
|---|---|
| index side | committed **BULL** and `nifty day > 0` |
| breadth | `dec − adv ≥ 2` (`5▲/10▼` = 5) |
| sector | top of `PERSIST −`, `side == −1`, not stale |
| persistence | `persist_bear ≥ DIVERGE_MIN_PERSIST` (65) |
| trend | `day ≤ −0.20%`, `range pos ≤ 40`, `rs < 0` |
| flow | bear flow ≥ 55, and `recent < +0.07` — **not bouncing** |
| precedence | **one pick per bar, long side first**: the short only replaces a `WAIT`, never a long |
| hysteresis | the incumbent faller is held unless a challenger is `PICK_STICKY_MARGIN` more persistent |

It is a fallback, not a second pick: on a bar where a sector can be bought, the
short does not exist. The block says which one it is —
`MARKET BULL conf 41% WEAK  ·  short on divergence` over `SECTOR PICK: REALTY
BEAR`, with the reason spelled out:

```
DIVERGENCE SHORT — index up (Nifty +0.44%) but breadth 5▲/10▼; REALTY is breaking
down (persist 91, day -1.20%, range pos 3, flow 85 bear)
```

Downstream it is an ordinary short: `pick_cap_key` is `REALTY short`, the cap and
the lead ranking apply, bot 3 scans the same sector, and the stock ranked first is
the one falling *fastest relative to its sector* (the short-side `lead`).

**Why it is off.** It shorts into a rising index. The tape it targets — index held
up by two or three names, everything else offered — can grind up for hours while
the breadth stays red, and then the fade never comes. It is a rule to test, not a
rule to switch on and forget. To A/B it: set `DIVERGENCE_SHORT=1` for a session
(the block and the log then name every divergence short it would take), and on a
day it is off, the same information is still on the block as
`No pick · persistent movers  FMCG long 93 · REALTY short 91`.

### What it does not fix

**REALTY 91 could not have been traded on this block either way.** It is the most
persistent *bearish* sector, and the rule is that the pick must agree with the
committed side: on a `commit_side == 1` bar the only usable pool is `PERSIST +`.
Shorting the loser of a 5▲/10▼ tape is defensible, but it is a counter-index
trade and it needs a BEAR lock, which this day never gave. If that is a trade you
want, it is a new rule (a two-sided pick on a weak tape), not a bug fix — say so
and it can be specified.

### Proof

`pytest -q` → **353 passed**. New: the stand-aside header (and that a traded
block keeps `MARKET BULL conf …`), the movers line, the `conf 41 < 55` gate text,
and seven cases on `_follow_fallback` — default-off, the knob on, NEUTRAL
unchanged, weak BEAR refused, no override of a real pick, a dull sector still
refused, `SECTOR_FOLLOW=0` still master.

### The "persist test" — the three morning blocks, read against each other

The 09:15 / 09:30 / 09:35 feed, same morning as the 11:55 block:

| bar | label / conf | breadth | PERSIST + | PERSIST − | pick |
|---|---|---|---|---|---|
| 09:15 | NEUTRAL 29 WEAK | 10▲/5▼ | PSUBK 59 · PHA 43 · CDUR 42 | **REALTY 66** · MET 54 · PSE 50 | WAIT |
| 09:30 | BULL 35 WEAK | 9▲/6▼ | PSUBK 50 · PHA 50 · BANK 42 | **REALTY 76** · MET 65 · DEF 59 | WAIT |
| 09:35 | BULL 56 BUILDING | 8▲/7▼ | **PHA 56** · FMCG 54 · BANK 48 | **REALTY 69** · DEF 66 · PSE 63 | **PHA** |
| 11:55 | BULL 41 WEAK | 5▲/10▼ | **FMCG 93** · BANK 44 · FIN 32 | **REALTY 91** · AUTO 86 · DEF 83 | WAIT |

Three things the test shows, and only one of them is a defect.

**1. REALTY was the one persistent sector of the day, and the engine was right
not to trade it.** Top of `PERSIST −` at every bar (66 → 76 → 69 → 91), live flow
on it at 09:30 and 09:35, and it kept falling. It is the cleanest reading in the
whole feed. It was also untradeable: the index was `BULL` at every one of those
bars, and the pick has to agree with the index. The divergence rule
(`DIVERGENCE_SHORT`) would not have fired either — breadth was **positive** all
morning (10▲/5▼, 9▲/6▼, 8▲/7▼); it only looks at the 5▲/10▼ tape that arrived
by 11:55. So on this morning nothing in the current design, on or off, shorts
REALTY. That is the one-sided rule doing exactly what it was asked to do.

**2. The long side was never persistent, and 09:35 bought the best of a weak
bunch.** The `PERSIST +` top went 59 → 50 → 56 while the floor was 50. At 09:35
the pick was `PERSIST + PHA persistent (score 56)` — the label says *persistent*,
not *qualified*, which is the engine being honest: PHA was not flow-aligned, and
56 is six points over a 50 floor. The same pharma names were given up within 40
minutes at the day's high (third pass). This is the floor, not the measurement.

**3. The three paths sat on a gradient and only one number knew it.** 50 for the
complementary pick, 55 for `SECTOR FOLLOW`, 65 for a weak day reads as "the less
index confirmation, the more the sector has to prove" — coherent. But the
complementary floor was flat, so a **conf 56** bar was treated exactly like a
**conf 96** bar. `PICK_CONF_SCALED_FLOOR` (default **0**) fixes the gradient:
`PICK_MIN_PERSIST` (50) at conf ≥ 70, `PICK_LOWCONF_MIN_PERSIST` (65) at the 55
gate, linear between. On the 09:35 bar the floor becomes 64, and:

```
WAIT — persist #PHA does not complement BULL market · passed over PHA (persist 56 < 64)
```

The morning's only long entry disappears; FMCG 93 still passes on any bar with
conf ≥ 55. The incumbent is validated against the same floor, so a pick made at
conf 74 is dropped — not held — once the confidence falls to the gate.

**Not changed:** the 09:30 block reads `Breadth 9▲/6▼ TREND UP` while the header
says `WEAK` and the action is `WAIT`. `regime` is a breadth/ADX description, not a
trade signal, and the header no longer announces a direction it will not use
(`MARKET STAND ASIDE  BULL lean · conf 35% WEAK`). Considered, left alone.

**Also noted:** at 09:15 `persist` is built on a single bar (30% of it is the day
move, 25% the position in a one-bar range), so `PSUBK 59` at 09:15 carries the same
authority on the block as `FMCG 93` at 11:55. The 50/64 floor and the conf-55 gate
are what keep the first bar from being traded; a "few bars so far" marker on the
block would be a display nicety, not a fix.
