# NSE Live Sector + Stock Scanner

Python port of two TradingView engines, chained the way you specified:

1. **Market + sector brain** (`NIFTY Institutional Core + Flow v4.0`)  
   India VIX, Nifty 5m direction, 15-sector breadth, live RS flow, **PERSIST + / PERSIST −**.  
   **Only the strongest persistent bull/bear sector that complements that day’s Nifty direction is kept.**  
   CHOP / NEUTRAL / VIX-spike conflict → `WAIT` (no stock scan).

2. **Stock 5m scanner** (`MASTER SECTOR BATCH SCANNER`)  
   Runs **only on that sector’s full batch** (both TV batches — Python has no 12-symbol cap).  
   Aggressive window entries + 22-condition exception codes.  
   **Only newly fired entries from the latest completed 5m candle** are recorded as entries (an optional, clearly labelled *early* heads-up is sent ~60s before that close — see *Entry timing*) and eligible for Telegram (no historical table replay or digest). Each stock can alert once per IST trading day.

L2 matches the TV master-scanner table: daily/15m `lookahead_on` on completed days, dual VWAP, first-hit + rank upgrade, 90/290 cleared after 09:45 (and 90/290 do not lock later exceptions). Interior Yahoo 5m gaps are filled; trailing 15:15–15:25 drops are not invented. Yahoo’s 5m **09:15 bar often has Volume=0** (the auction OHLC is real; volume lives on 1m). That zero volume broke VWAP / relVol / W1 `entry_sell_today` and printed false 09:30 EX1 instead of EX8. When 1m history exists (last ~7d) those zero-volume 5m slots are filled from 1m sums.

Verify with `python -m nse_scanner backtest --date YYYY-MM-DD` — compare the **TV TABLE** block to TradingView, not the plotshape dump.

Data: **yfinance** NSE 5-minute bars. Session clock: **Asia/Kolkata**.

---

## What you get on Telegram

**Market / sector change**

```
MARKET BULL  conf 74% STRONG
Nifty 24810.4  day +0.42%  intra +0.31%
VIX 12.80 ↓ -3.10%   ADX 26   MTF 3▲
Breadth 11▲/4▼  TREND UP
PERSIST +  IT 78 · PHA 71 · FMCG 64
PERSIST −  AUTO 70 · MET 66 · MEDIA 61
SECTOR PICK: IT  BULL
PERSIST + IT qualified (score 78)
Action: SCAN LONG
```

`MTF` shows **both counts** — `MTF 2▲/1▼` is two timeframes bullish, one bearish,
`MTF 3▲` is all three. It used to print `MTF 2/3`, i.e. only the bullish count,
which is ambiguous on a bear block: your chart's `MTF 2/3` beside `DIR BEAR` reads
as *two of three agree with the bear*, while ours printed the complement (`MTF 1/3`)
for the same tape. 16 Nov 14:41 was exactly that case. Both numbers at once is
readable under either convention, and the direction decision never used the sign
(`_dir_conf` takes `abs(bull − bear)`).

**Stock entry** (only same side as the pick)

```
🟢 BUY | BUY-EX17  PNB
Sector PSUBK  |  Market BULL (88%)  ·  lead +1.9pp vs sector
Entry (candle close) 135.73
SL 134.94 (0.02% below EMA9)
Risk 0.79  |  next pivot target: T1 R2 136.75 (1.3R, +0.75%) · T2 R3 141.02 (6.7R, +3.90% ⚠) · T3 N/A
Pivot basis 09 Nov  H 132.48  L 125.92  C 132.17  range 4.96% (2.2× median)
Score 82 Extreme Strong   base 21/21 cond22=0  score=82
5m bar 10:25–10:30 IST (signal confirmed at close)
Signal calculated 10:31:34 IST
```

Each target carries its distance in **R** (multiples of that trade's risk) *and*
in **% of price**. R alone is unstable — it moves whenever the stop moves — so a
level gets a `⚠` marker when it is past `FAR_TARGET_R` (4R) **or** past
`FAR_TARGET_PCT` (2.0 % of price). The price is always kept: levels are never
hidden or re-invented. The `Pivot basis` line names the session the levels
were built from, so every number in the alert can be re-derived by hand.

Entry is the **signal candle's close** (two-decimal price precision). EMA9 includes that completed candle. For BUY, initial SL = EMA9 × (1 − 0.0002); for SELL, SL = EMA9 × (1 + 0.0002). Stops are rounded outward to 0.01, targets are the next three classic daily pivot levels strictly above entry for BUY or below entry for SELL (nearest first), using the previous trading session’s high/low/close. The levels are S3, S2, S1, PP, R1, R2, R3, where PP = (H + L + C) / 3; R1 = 2PP − L, S1 = 2PP − H; R2/S2 = PP ± (H − L); R3 = H + 2(PP − L), S3 = L − 2(H − PP). Current/future daily candles are excluded.

### Why a pivot target can look wrong (verified case: PNB, 11 Nov 2026)

The levels above are real. NSE was shut on **Tue 10 Nov 2026** (Diwali-Balipratipada),
so the previous session was **Mon 09 Nov 2026**: H 132.48, L 125.92, C 132.17.

| | value | |
|---|---|---|
| PP = (H+L+C)/3 | 130.19 | |
| R1 = 2PP − L | 134.46 | already passed by the 135.73 entry |
| R2 = PP + (H−L) | **136.75** | first level above entry → T1 |
| R3 = H + 2(PP−L) | **141.02** | → T2, 6.7R away |

`R3 − R2 = PP − L = 4.27`, so the 4.27-point jump between T1 and T2 is the
formula, not a rounding slip: 09 Nov had a **4.96% range** (a 125.95→132.48
reversal day), and `R3` grows with that range while `R2` does not. Outcome that
day: R2 (136.75) was tagged within ~10 minutes of the 10:31 alert, the session
high was **₹139.00** (+5.1%, NSE via Upstox), and R3 at 141.02 stayed 1.5% above
that high all session — a real level, but never a reachable 5m target. That is
why a far level is labelled as far instead of being shown as a plain "T2".

So the scanner does **not** silently trust one session:

* **Basis disclosure** — every entry alert prints the previous session's date,
  H/L/C and range, so the levels are auditable in the message itself.
* **Basis quality** — the basis range is compared with the median range of the
  last 20 completed sessions (`PIVOT_RANGE_LOOKBACK`) and the multiple is printed
  on every alert. Above 2.5× (`PIVOT_RANGE_OUTLIER`) the alert adds a wide-basis
  warning; below 0.4× (`PIVOT_RANGE_COMPRESSED`) it warns that the levels are
  compressed; a Sat/Sun basis (Muhurat session) is called out as a special
  session. A wider basis day mechanically stretches R3/S3.
* **Stale-basis refusal** — if the newest completed session is more than
  `MAX_BASIS_GAP_DAYS` (7) calendar days before the signal, no pivots are
  published (`N/A`) instead of building levels from an old day.
* **Feed reconciliation** — 5m-aggregated and daily OHLC for the basis day are
  compared; a >0.5% disagreement (split, bad print, stale feed) is logged and the
  daily bar wins.
* **Single-row safety** — a 5m frame holding only the signal candle no longer
  breaks grouping (pandas 3 turned a one-element key list into a 1-tuple, which
  raised `TypeError` inside the trade plan).

Known conventions differ on `R3/S3`: `PP + 2(H − L)` gives 143.31 for the PNB
example and Fibonacci pivots give 140.80. This repo stays on the TradingView
"Traditional" formula above; if your chart shows another number, check its
pivot method before assuming the alert is wrong. Missing or exhausted levels show N/A rather than invented risk-multiple targets, without suppressing entry or EMA9 trailing; verify your symbol's exchange/broker tick size before placing an order. An entry with no EMA9 or with a stop on the wrong side of its entry is **skipped** instead of advertising invalid risk. SELL stop/targets and EMA9 exit are the mirror image of BUY.

Every open trade is watched on each completed 5m candle, and **every level that is
actually touched produces exactly one one-line alert**. Three events, nothing else:

**Target hit** — the first candle that trades through a pivot target. Informational
only: the **stop moves to breakeven** at T1, the trail keeps running, and each of
`T1/T2/T3` fires at most once (candles that blow through several levels report them
on one line). A pivot closer than `MIN_TARGET_R` (0.5) / `MIN_TARGET_PCT` (0.10%) to
the entry is not a target — it is the resistance the entry was made into — so the
ladder starts at the first level that is far enough away and every skipped level is
shown on the entry alert.

```
🎯 TARGET HIT — BUY  INFY · T1 R1 1850.20 (+0.42%, +1.8R) · stop → breakeven 1842.50 · 5m 11:05 · entry 1842.50
```

**Stop hit** — the first candle whose low (BUY) reaches the stop. Before T1 that is
the initial EMA9 stop; after T1 it is the breakeven stop, and the line says so. This
closes the position in the ledger and blocks a fresh entry on that symbol for the day.

```
🛑 SL HIT — BUY  INFY · SL 1838.28 (-0.23%, -1.0R) · 5m 11:20 · entry 1842.50
🛑 SL HIT — BUY  INFY · SL 1842.50 (+0.00%, +0.0R) · stop moved to breakeven after T1 · 5m 11:20 · entry 1842.50
```

**EMA9 trail exit** — the first *completed* candle closing beyond EMA9 by more than
the noise band: the smaller of `EXIT_EMA9_BUFFER_PCT` (0.10%) of price and
`EXIT_EMA9_BUFFER_R` (0.25) × risk, SELL mirrored. A wick alone does not trigger it,
and neither does a 4-paise poke: on 16 Nov nine of ten exits were breaches of
0.009–0.13%, and five of them are held by the band. Also closes the position.

```
🔔 EMA9 EXIT — BUY  INFY · close 1849.00 below EMA9 1850.20 (+0.35%, +1.5R) · 5m 11:05 · entry 1842.50
```

**Never twice.** Each alert has one identity — symbol + the candle the trade was entered
on + the level — and that identity is written to the state file *after the POST returns
and before the next one is attempted*. Re-driving the same candle, a retried run, a
rewound or rebuilt position record, or a trade that carries overnight therefore cannot
produce the same message twice; a later trade on the same symbol still gets its own
alerts because its entry candle differs. This is the same once-only mechanism that
guards entry alerts, and entry alerts are unchanged by it. Deleting the ledger entry by
hand is the only way to see a repeat.

Every candle since the last check is replayed, so a slow queue or a skipped run still
reports the hit on the candle where it happened, stamped with *that* candle's time
rather than the delivery time. A failed Telegram POST stops the replay at that candle
so the next cycle retries it without resending what already went out.

The targets are **reference levels**, not auto-sell orders: passing them never exits by
itself, and the scanner keeps monitoring the symbol even when the sector switches or the
market returns WAIT. Neither the SL nor the targets are **executed** by this bot — the
alerts tell you what happened; use broker-side orders for actual stop execution. Positions
carry into later sessions if the state cache survives; a missing Yahoo bar or an expired
freshness window can delay an alert until the next cycle sees the candle.

One stock entry per symbol per IST session. Later rank upgrades or re-discovered table rows are **not** pushed again. The message labels the 5m candle **open and close** and the calculation time separately; a 10:25 candle cannot be confirmed before its 10:30 close. Telegram's receipt time is the delivery time.

---

## Complementary sector rule (locked)

| Nifty lock | Sector used | Stocks scanned |
|---|---|---|
| BULL / STRONG BULL | strongest **PERSIST +** that still has RS ≥ −`PICK_RS_MARGIN`, is up on the day and is not a qualified short | that sector, **BUY** entries only |
| BEAR / STRONG BEAR | strongest **PERSIST −** that still has RS ≤ `PICK_RS_MARGIN`, is down on the day and is not a qualified long | that sector, **SELL** entries only |
| NEUTRAL / CHOP / conf < 55 / VIX spike against the day | **WAIT** | none |

The label describes the side the engine actually trades (`commit_side`, the concrete
lock when one exists), so an alert can no longer read *"Market NEUTRAL"* on a BUY.
`NEUTRAL` now always means no pick and no scan.

No pair-trades. No “rel-long the strongest bull while index is bear”.

The pick reads the sector list, never a single sector in isolation:

* **A sector index that stopped printing is not picked.** Its own 5m feed is
  allowed to trail the Nifty decision bar by `SECTOR_MAX_LAG_BARS` (2) — Yahoo's
  cash indices gap more than stocks do — and a sector further behind is dropped
  from PERSIST + / PERSIST − and from the pick, and named on the block
  (`⚠ stale index  FMCG 5 bars behind`). The stock level has always required a
  current frame (`index[-1] == last`); the sector decides which stocks are
  scanned at all, so it needs the same rule.
* **A leader is not dropped for a hair.** `rs` is in percentage points, so
  `PICK_RS_MARGIN` (0.05) is five basis points of noise. With `0` a sector
  leading PERSIST + by 30 points was passed over for reading `rs -0.01`.
* **A sector has to be persistent to be picked at all.** `PICK_MIN_PERSIST` (50)
  is the floor the complementary path never had: it used to accept any sector
  that was merely not fighting the market, while SECTOR FOLLOW demanded 55.
  `PICK_CONF_SCALED_FLOOR=1` moves that floor with the confidence (50 → 65 as
  conf falls from 70 to 55), so a marginal call needs an exceptional sector.
* **The pick does not flicker.** A challenger must beat the incumbent by
  `PICK_STICKY_MARGIN` (5) points to take the pick, so two sectors a couple of
  points apart stop trading it every bar (and the same stock stops being
  scanned in two sectors). The incumbent is only ever held, never restored: if
  it goes stale, drops below the floor or rolls over, it is dropped at once.
  The line carries the arithmetic:
  `PERSIST + PHA qualified (score 56) · held — FMCG 59 is +3.0, needs 5 to take the pick`.
* **Whatever is passed over is named**, with the number that did it:
  `PERSIST + FMCG persistent (score 60) · passed over PHA (rs -0.01)`.
* **A block that trades nothing does not announce a direction.** `MARKET BULL
  conf 41% WEAK` sat directly above `WAIT — market TRANSITION conf 41` on 16 Nov
  11:55: a bullish call on a 5▲/10▼ tape that the engine then refused to act on.
  Such a block now opens `MARKET STAND ASIDE  BULL lean · conf 41% WEAK`, names
  the confidence it needs (`conf 41 < 55`) and still lists the day's movers on
  both sides — `No pick · persistent movers  FMCG long 93 · REALTY short 91` —
  because "which sectors are moving" is the question a WAIT used to leave open.

---

## Entry timing: early alerts + always-on loop

A close-confirmed 5m signal has three delays stacked on top of each other
(measured on this repo, see `docs/latency-analysis-2026-11-12.md`):

| stage | before | now |
|---|---|---|
| the candle itself — the breakout happens *inside* it | up to 5 min | **early alert ~60s before the close** |
| trigger after the close (external dispatcher) | +65 s | 0 — the loop is already running |
| runner boot + `pip install` | +20 s | 0 |
| Yahoo fetch + scan | +9 s | not measured yet — every alert now prints it (daily candles cached, 5 s re-poll) |
| candles actually scanned (10–12 Nov) | **7 of 75** (hourly trigger) | all 75 |

**Early alert.** `EARLY_ALERT_SECONDS` (default 60) before each 5m close the loop
rebuilds the *forming* candle from Yahoo 1m bars and runs the unchanged stock
engine on it, for the sector/side picked on the previous completed candle:

```
⚡ 🟢 EARLY BUY (unconfirmed) | BUY-EX17  PNB
Sector PSUBK  |  Market BULL (88%)
Price now 135.52  ·  5m bar 10:25–10:30 still forming (55s left)
SL ≈ 134.90 (EMA9 so far)  |  Risk ≈ 0.62
next pivot target: T1 R2 136.75 (2.0R, +0.91%) · …
Holds only if the 10:30 close keeps the signal — ✅ confirm or ❌ cancel follows
Calculated 10:29:05 IST
```

Seconds after the close exactly one follow-up arrives:

* `✅ CONFIRMED — early alert 10:29:05 @ 135.52 (close is +0.15% beyond it)` on top
  of the normal entry alert. Only now is the trade recorded (entry = candle close,
  SL, targets, hit alerts) — exactly as before.
* `❌ EARLY CANCELLED — BUY PNB · … did not hold (10:30 close) · close 135.20 vs early 135.52`
  when the completed candle no longer fires, the sector pick changed or the market
  went to WAIT. No trade is recorded and the stock stays eligible for a later signal.
* `⚠ EARLY UNVERIFIED` if Yahoo never published the closed candle in time.

Rules: an early alert is **not** a confirmed signal — the last minute can undo it,
which is the price of being early. It never touches the entry ledger; one early
alert per stock per session (no flicker spam); it is skipped when the newest 1m
print is older than `EARLY_MAX_STALE_SECONDS` (100) so a frozen feed is never shown
as a live price; `EARLY_ALERT_SECONDS=0` restores close-only behaviour. A larger
value (e.g. 120) is earlier but cancels more often.

**Measure it before trusting it** (Yahoo 1m history, last ~7 days):

```bash
python -m nse_scanner early-check --date 2026-11-12 --sector PSUBK --lead 60
```

prints every early alert next to its close (confirmed / cancelled / close-only),
the confirmation rate and how far the close already was from the early price.
Omit `--sector` for all sectors (≈30 s per stock).

### 1-minute projected scan (second bot, up to 4 minutes earlier)

The indicator can also run on a **1-minute chart**: the forming 5m candle is
rebuilt from the 1m prints as they arrive, so a signal shows up to four minutes
before that candle closes. That mode is a *separate* scan with its own bot:

```bash
python -m nse_scanner live-1m            # projected alerts + confirmed entries, bot 3
python -m nse_scanner once-1m --dry-run  # one projected pass, prints instead of sending
python -m nse_scanner once-1m --sector IT --dry-run   # force a sector, as `once` does
```

The 5m flags carry over: `--sector` forces the sector exactly like `once --sector`
(same override block, same `FORCED …` reason), `--max-minutes` / `--exit-at-close`
drive the handover, and `--backfill` is refused — replaying a past session is a 5m
diagnostic (`once --backfill --dry-run`), never a 1m live loop.

```
⚡ 🟢 EARLY BUY (1m projection) | BUY-EX17  PNB
Sector PSUBK  |  Market BULL (74%)  ·  pick projected from the forming candle
Price now 135.70  ·  5m bar 10:30–10:35 still forming
3m 55s left — projected from 1m prints up to 10:31
SL ≈ 135.02 (EMA9 so far)  |  Risk ≈ 0.68
next pivot target: T1 R2 136.75 (2.0R, +0.77%) · …
Score 82 Extreme Strong   base 21/21 cond22=0 score=82
Rules: identical to the 5m scan · judged on minute 1/4 of the candle
Confirmed entry follows at the 10:35 close if the candle holds
```

* **Rules are not forked.** Every minute of the candle the 1m prints rebuild the
  forming 5m bar and the *unchanged* `stock.scan_stock` runs on
  `[completed 5m bars] + [projected bar]` — the same 22 conditions, the same 32
  codes, the same windows/locks/gap/wick/liquidity/score gates, the same market
  pick, side, EMA9 stop and pivot targets. Only the timing differs: minute
  `SCAN1M_FIRST_MINUTE` (1) instead of the close.
* **Early and confirmed.** The projection is sent as soon as it appears; at the
  candle's close the completed candle is judged again and the normal entry alert
  follows on the same bot (with `✅ CONFIRMED — projected 10:31:05 @ 135.70` in
  front when the projection held). A projection that does not hold is dropped
  **silently** (`SCAN1M_CANCEL_MESSAGE=1` to report it instead).
* **The pick is projected too (2026-11-16).** The forming candle is not judged
  on the *previous* candle's sector pick any more: `projected_market` rebuilds
  Nifty, VIX and every sector index the same way and runs the unchanged
  `run_market` on them, so the pick can move *during* a candle. Without this,
  bot 3 was blind on exactly the candle that matters — the one where the market
  flips to a new sector, which is the candle that sector's **leader** fires on.
  Its only message was then the same entry as bot 1 at the same second, while
  the followers that fired on the next candles did get the early alert. A
  projected pick never announces a MARKET/SECTOR change: that message waits for
  the close, exactly as on bot 1.
* **The forming bar's volume is finished from its prints.** `rel_vol` and the
  score's `vol_sc` read the *full* candle, so at minute 1 a partial candle
  showed a fifth of itself and a name scoring 65 on the completed candle showed
  60 — no projection until the candle was nearly over. `data.project_volume`
  scales the forming bar by `5 / minutes covered`, which is exactly 1.0 at the
  close: the confirmed frame is the one bot 1 judged, unchanged.
* **The leader that fired too early is announced, labelled.** A sector's leader
  usually fires *before* its sector is picked, so the scan rejects it as
  `earlier` — correct (it is not a new signal on the candle being scanned) but
  it used to mean silence, while the followers that fired later got their
  alerts: the reported "missed the leader, late on the followers". Bot 3 now
  sends `⏰ LATE LEADER` for them when the sector is picked and the trade is
  still valid — **one message per pick window** by default, listing the names
  best-first (17 of them in 35 minutes on 17 Nov was the false-alert problem,
  not the rule; `SCAN1M_CATCHUP_DIGEST=0` sends one per name): the fire's own stop untouched since, no
  older than `SCAN1M_LEADER_MAX_MINUTES` (30), move within
  `SCAN1M_LEADER_MAX_R` (2.0R). It is **not** an entry — `sent`/`positions` are
  untouched, and the message says so. `SCAN1M_LEADER_CATCHUP=0` turns it off;
  bot 1 never sends it.
* **Own bot, own ledger.** `TELEGRAM_BOT_TOKEN_3` / `TELEGRAM_CHAT_ID_3` and
  `state/scan1m_state.json`. It never calls the bots 1/2 sender and never reads
  or writes `state/scan_state.json` (the 5m pick is only read when
  `SCAN1M_MARKET_SOURCE=ledger`). The 5m scan keeps running exactly as before;
  stop this command and nothing about it changes.
* **The same honesty rules apply**: a frozen 1m feed (`SCAN1M_MAX_STALE_SECONDS`,
  100) never produces a projection, a candle the feed has not reached the end of
  is never confirmed, one projected alert per stock per session, and the entry is
  still the candle close — the last minute can undo a projection, which is
  exactly the 4 minutes you are buying.

**Checked step by step against `pipeline.run_once` (2026-11-15).** The close pass of
the 1m scan does what the 5m pass does, in the same order:

| step | 5m scan (`pipeline.run_once`) | 1m scan (`scan1m.confirm_pass`) |
|---|---|---|
| name list | `_scan_list` — sector, follows, `SECTOR_EXTRA` | the same function |
| frame | a stock is judged only if its 5m row is at the decision bar (`current`), zero-volume bars repaired from 1m | the same two tests and the same repair (prior days' 1m come from one 7d fetch per session) |
| late candle | `_unsettled` + `BAR_SETTLE_SECONDS` settle, `_warn_feed_behind` | **the same two functions**, pointed at bot 3 and the 1m ledger |
| unfinished candle | `_run_until_ready` retries until the 180 s deadline | the same (`tick()` → `retry`, `retry_bar` in the ledger) |
| deadline | `_max_lag()` (180 s) after the close | the same `_within_lag` |
| engine | `stock.scan_stock(raw, frame, daily, keep_all=True)` | the same call |
| side filter | signals of the pick's side at that candle's time | the same |
| rejects | opposite / earlier / no_stop / oi book | the same four, same keys, reported in the summary |
| stop & OI | `build_plan` (entry, EMA9 stop, targets), `_oi_mode` gate | the same |
| entry text | `format_stock(..., oi_line=...)` + `_target_text` | **the same text, character for character**; `✅ CONFIRMED — projected …` is prepended only when this candle *was* projected, so a close-only entry reads exactly like bot 1's |
| once per name/session | `sent` + `positions` | the same two maps |
| delivery | a failed send is retried while the candle is judgeable | the same — a failed bot-3 send is retried the next minute |
| summary | candles, picks, sectors, rejects | the same, plus projected / confirmed / cancelled |

**The same alert set, not just entries.** Every message the 5m scan sends has a
bot-3 twin, produced by the same code:

| message | 5m scan (bot 1/2) | 1m scan (bot 3) |
|---|---|---|
| start of session | `NSE live scanner started (5m …)` once | `1m projected scan started (bot 3 …)` once |
| MARKET / SECTOR change | `format_market(md)` when direction or pick moves | the same text, when this loop decided the market (`own`/`auto`) |
| projected entry | `EARLY` heads-up 60 s before the close | `⚡ EARLY … (1m projection)`, up to 4 min before |
| entry | `format_stock` + OI line, at most `MAX_ENTRIES_PER_PICK` per pick, leader first | the same text, with `✅ CONFIRMED` in front — same ceiling, same ranking: a name that does not clear it is told so (`not taken — IT long already has its entries`), never projected and then dropped |
| 🎯 TARGET HIT / 🛑 SL HIT / 🔔 EMA9 EXIT | `_track_positions` | **the same function** — `pipeline._track_positions(..., send=bot 3, save=1m ledger)` |
| ⚠ feed delayed | one notice per session on bot 1 | the same notice on bot 3 |
| ⏰ LATE LEADER | never | the sector's leader fired before its sector was picked: once, labelled, bounded (`SCAN1M_LEADER_CATCHUP`) |
| ❌ early cancelled | only when `EARLY_…` reports it | only when `SCAN1M_CANCEL_MESSAGE=1` |
| 15:33 summary | `format_summary` | same counters, plus projected / confirmed / cancelled |

Positions are opened by the confirm pass through the same `new_position` call
`run_once` uses (`side, entry, sl, sl0, risk, t_names, entry_ema9, t1–t3,
entry_bar, last_bar, session`), so a trade confirmed on bot 3 is followed to its
targets, its stop and its EMA9 exit — and an SL/EXIT also blocks a re-entry for
the rest of the session, just like bot 1. Bot 3 used to write the record itself,
which had silently lost `sl0`, `risk` and `t_names`: its target alerts read
`T1 103.20` where bot 1 read `T1 R1 103.20`, and the R in every later alert was
measured against a stop that had moved.

**A trade is intraday.** Only candles of the session a trade was entered in are
replayed, on both scanners: a position left in the ledger by an earlier session
is dropped before anything is fetched (`state.position_session`), not trailed
against today's bars. Before 2026-11-16 a PNB trade signalled on 11 Nov was still
open on the 16th, and the 16th's 09:15 candle — not the trade's candle — "hit"
`T1 136.75` and `T2 141.02` and produced the alert
`🎯 TARGET HIT — BUY PNB · T1 136.75 (+0.75%, +1.3R) · T2 141.02 (+3.90%, +6.7R) · 5m 09:15 · entry 135.73`
(see `docs/live-defects-2026-11-16.md`). Restarting inside the same session still
keeps an open trade open.

**Prove it, don't claim it:** `tests/test_workflow_parity.py` also reads both workflow
files and fails if either loses a rule input (`SECTOR_FOLLOW`, `SF_ALIGNED_SECTORS`,
`OI_FILTER`, `MAX_ALERT_LAG_SECONDS`, `BAR_SETTLE_SECONDS`), the guard, the 170-minute
budget, the handover step, the schema validation, the per-run cache or the separate
concurrency group; and `tests/test_scan1m_parity.py` runs one synthetic
session through *both* scanners and asserts (1) the frame handed to
`stock.scan_stock` is identical (`assert_frame_equal`, same rows, same volume
repair, `keep_all=True`), (2) the plan is built from the same bar, and (3) the
messages are identical character for character — on the happy path, on a wrong-side
signal, on a missing EMA9 stop, on a WAIT pick, on a delayed feed and on a late run.
The reject books (`stats.rejected`) are compared as dicts. If either scanner's rules
drift, that file fails.

The only additions this mode needs are the forming (projected) candle, the EARLY
and cancellation messages, and bot 3 itself. `state/scan_state.json`,
`stock.py`, `telegram.send()` and `live-scan.yml` are untouched; `pipeline.py`
gained two optional `send`/`save` arguments on `_track_positions` (defaults are
the 5m behaviour, so bots 1/2 are unchanged) purely so the 1m loop can run the
same tracker instead of a copy.

**The loop itself, too:** both park until 09:14, announce once per session (handovers stay
silent), send the 15:33 summary once, treat three candles without an NSE bar as a holiday,
stop at 15:33, hand over after 170 minutes, and retry an unfinished candle until the
`MAX_ALERT_LAG_SECONDS` deadline instead of writing it off. The same candles are judged
(09:15 … 15:25, so the 15:25 bar that closes at 15:30 is not lost), and the close always
comes first: the minute that judges a closed candle still projects the forming one
afterwards, so an entry that only appears at the close is never traded for a projection.

**The workflow is the same loop, too** (`live-scan-1m.yml` vs `live-scan.yml`):
same guard (one live loop, older-run handshake, dry runs beside it), same 170-minute
budget and `--exit-at-close` handover with `actions: write`, same one-key-per-run
immutable state cache (own prefix, so neither loop can restore the other's
snapshot), same `state.load` schema validation before a snapshot may be saved, same
`concurrency` belt-and-braces (its own group, `cancel-in-progress: false`) and the
same `SECTOR_FOLLOW`, `SF_ALIGNED_SECTORS`, `OI_FILTER`, `MAX_ALERT_LAG_SECONDS`
and `BAR_SETTLE_SECONDS` rule inputs.

**The knobs an operator turns** (all optional, all read from the environment — in
the workflow set them as repository *Variables*, no commit needed):

| variable | default | what it does |
|---|---|---|
| `SCAN1M_FIRST_MINUTE` | `1` | first minute of the 5m candle that may alert: `1` = up to 4 min early, `4` = 1 min early. Later minutes project fewer, better candles (clamped to 1–4) |
| `SCAN1M_CANCEL_MESSAGE` | `0` | `1` = send `❌ EARLY CANCELLED — BUY ABC · projected 10:31:05 @ 135.70 · close 135.20 (−0.37%)` instead of dropping the projection silently |
| `SCAN1M_MARKET_SOURCE` | `auto` | `ledger` = reuse the 5m scan's pick from `state/scan_state.json` (same sector and side, no market fetch — needs that file on this machine); `own` = decide the market here with the same engine (what the workflow uses); `auto` = ledger first, own as fallback. In `ledger` mode a missing or stale 5m ledger says so in the log, once per candle, instead of doing nothing |
| `SCAN1M_EARLY_PER_DAY` | `1` | projected alerts per stock per session. A projection that did **not** survive its close is not spent: it may be retried on a later candle of the same session |
| `SCAN1M_EARLY_RETRIES` | `2` | how many of those re-projections a name gets after a cancellation. `0` restores the old one-shot rule (one bad candle silenced the name for the day, and its later alerts were same-second duplicates of bot 1) |
| `SCAN1M_MAX_STALE_SECONDS` | `100` | skip a stock whose newest 1m print is older than this |
| `SCAN1M_GRACE_SECONDS` | `6` | seconds after a minute boundary before asking Yahoo |
| `SCAN1M_SUMMARY` | `1` | `0` disables the 15:33 summary |
| `SCAN1M_ALERT_CAP` | `3` | projections allowed per 5m candle, ranked by sector lead exactly as the entries are ranked (`0` = one message per firing name). 17 Nov put **11 of the 24 names it scanned** into one candle (six FMCG, five PSU banks) and no name could be taken that the cap would not have ranked first; the held-back names are counted in the summary (`· 8 held back by the per-candle cap`) |
| `SCAN1M_CATCHUP_DIGEST` | `1` | one `LATE LEADER` message per pick window, best-first, instead of one per name (`0` = per name) |
| `SCAN1M_REQUIRE_TARGET` | `1` | refuse an entry that has **no pivot target at all** — no exit level, no trade. `MIN_TARGET_R` refuses a target that is too close; this refuses the absence of one. DABUR on 17 Nov was entered with `T1 N/A · T2 N/A · T3 N/A` and lost −0.8R (`0` = old rule) |
| `SCAN1M_1M_PERIOD` | `1d` | 1m history window fetched every minute (prior sessions come from one 7d fetch per day) |

A few more decide what a 5m alert is allowed to say. The exit, target and
sector-pick rules apply to **both** loops — bot 3 trails the same positions,
renders the same plan, confirms entries on the same ceiling and must not scan a
sector bot 1 would have refused — so they are shared, not forked.

| variable | default | what it does |
|---|---|---|
| `MAX_ENTRIES_PER_PICK` | `3` | entries per pick (sector + side) per session, `0` = no cap, enforced by **both** loops. Six names can fire on one candle (16 Nov: six pharma entries in 35 seconds, all out within 45 minutes for −1.6R); one pick is one risk factor. Bot 3 used to confirm all six |
| `RANK_BY_SECTOR_LEAD` | `1` | which of the names that clear the cap is alerted: ranked by **how far the stock leads its own sector today** (stock move minus sector-index move, both from the session open, in pp), score breaking ties. `0` = rank by the absolute 0–100 `score`, whose "rs" leg is only `close > previous close` — one bar, 15 of 100 points. The alert shows the number: `lead +1.9pp vs sector` |
| `SECTOR_MAX_LAG_BARS` | `2` | how many 5m bars a sector index may trail the decision bar and still be picked (`-1` = no check). A sector further behind is taken out of PERSIST + / PERSIST − and out of the pick, and shown as `⚠ stale index` |
| `MIN_TARGET_R` / `MIN_TARGET_PCT` | `0.5` / `0.10` | a pivot closer than this to the entry is not a target (`0` = old behaviour). GODREJCP on 16 Nov: entered 877.25 with T1 at 878.00 — 0.2R, +0.09% — announced as "TARGET HIT +0.2R" |
| `EXIT_EMA9_BUFFER_PCT` / `EXIT_EMA9_BUFFER_R` | `0.10` / `0.25` | a close must be beyond EMA9 by the *smaller* of these before the trail counts as broken (`0`/`0` = the old bare comparison). The band is what stops a 4-paise poke from ending a trade |
| `BREAKEVEN_AFTER_T1` | `1` | once T1 is touched the stop moves to entry, so a hit target cannot turn into a loss (16 Nov: LUPIN +1.3R → +0.2R, NESTLEIND +0.8R → −0.1R). The R in every later alert is still measured against the **original** stop |
| `MARKET_CONF_DELTA` | `10` | send the market block when confidence moves this many points, not only when the label or the pick changes — 16 Nov slid 99% → 94% → 67% without a message while three entries were taken (`0` = off) |
| `PICK_RS_MARGIN` | `0.05` | pp of RS a sector may give up and still complement the pick; `0` restores the bare `rs >= 0` test that skipped a persist-91 leader for reading `rs -0.01` |
| `PICK_MIN_PERSIST` | `50` | persistence a sector needs before it can be picked at all (`0` = no floor). The complementary path used to accept anything not fighting the market, while SECTOR FOLLOW demanded 55: a sector that had been drifting all day could be picked over one that had actually held |
| `PICK_STICKY_MARGIN` | `5` | points a challenger must beat the incumbent sector by to take the pick (`0` = switch on any margin). Two sectors 2–3 points apart used to trade the pick back and forth every bar, so the same stock was scanned in two sectors. The incumbent is only ever *held*, never *restored*: stale, below-floor or rolled-over and it is dropped at once — and the line says so: `held — FMCG 59 is +3.0, needs 5 to take the pick` |
| `PICK_CONF_SCALED_FLOOR` | `0` | `1` makes the persistence floor rise as the confidence falls: `PICK_MIN_PERSIST` (50) at `conf ≥ 70`, `PICK_LOWCONF_MIN_PERSIST` (65) at the conf-55 gate, linear between. On 16 Nov 09:35 a sector at **56** was bought on a **conf 56** bar — the best of a weak bunch, while the morning's persistent sector was REALTY (66/76/69 on PERSIST −) and the long side never got above 59. The three paths already sat on that gradient (50 complementary / 55 follow / 65 weak day); only the first was fixed. The alert says the effective floor: `passed over PHA (persist 56 < 64)` |
| `PICK_LOWCONF_MIN_PERSIST` | `65` | the floor at the confidence gate when the floor is scaled |
| `DIVERGENCE_SHORT` | `0` | `1` lets the **persistent faller** be shorted while the index itself is up. `commit_side == BULL` only says the index is on the up side — it does not say the up-move is confirmed, so the short needs the index up **and** one of: `MTF < 3`, `conf < PICK_CONF_GOOD` (70), or breadth against it (`dec − adv ≥ 2`). 16 Nov was `MTF 2/3` and `conf 29–56` all morning (REALTY 66 → 76 → 69 on `PERSIST −`, nothing tradeable) and `5▲/10▼` by 11:55 (REALTY 91). Mirrored gates on the sector: `persist_bear ≥ DIVERGE_MIN_PERSIST` (65), not stale, `day ≤ −0.20%`, `range pos ≤ 40`, `rs < 0`, bearish flow, not bouncing. **One pick per bar, long side first** — the short only replaces a `WAIT`; the block marks it `· short on divergence`. `0` keeps the pick aligned with Nifty |
| `DIVERGE_MIN_PERSIST` | `65` | `persist_bear` a sector needs for that short (see `DIVERGENCE_SHORT`) |
| `SECTOR_FOLLOW_WEAK_MIN_PERSIST` | `85` | persistence the weak-day path needs (see below); it must stay above the NEUTRAL floor (`SECTOR_FOLLOW_MIN_PERSIST` 75), because on a weak day only the exception is worth following |
| `SECTOR_FOLLOW_ON_WEAK` | `0` | `1` lets a **weak but directional** market follow a plainly leading sector. Two gates used to leave a gap between them on 16 Nov 11:55 — `MARKET BULL conf 41% WEAK`, `PERSIST + FMCG 93`, `PERSIST − REALTY 91`, `WAIT`: the picker stands aside below conf 55 and the sector-follow path only ran with *no* committed side, so a day with a direction it did not trust had no path to a sector at all. `1` falls back to the strict `SECTOR_FOLLOW` gates (up on the day, upper half of the range, positive RS, bullish flow, not rolling over) on a weak **BULL** only, with a higher persistence floor than the NEUTRAL case — `SECTOR_FOLLOW_WEAK_MIN_PERSIST` (85) against 75 — because on a weak day only the exception is worth following. `0` is the locked rule: conf < 55 stands aside. The pick stays one-sided: on a committed BULL bar the top of `PERSIST −` is a reading, never a pick |

`python -m nse_scanner doctor` prints the ones that change behaviour most
(`first minute → up to N min early`, `market source`, `re-projections after a
cancel`, `cancel messages on/off`, `leader catch-up`) next to the bot-3
credentials, so a typo is visible before the session starts.

**Did the variable actually arrive?** This is the failure that leaves no trace: a
repository variable created as a *secret* (or in an environment the job does not
attach, or in the other repository) resolves to an empty `vars.X`, the workflow
supplies its default, and the session looks exactly like one where the knob had
no effect. So every run now says what it is running with — `doctor` and both
loops, once at startup:

```
pick knobs in force: PICK_CONF_SCALED_FLOOR=1 · PICK_MIN_PERSIST=50 · … · DIVERGENCE_SHORT=0
pick knobs differing from default: PICK_CONF_SCALED_FLOOR=1
```

and each workflow prints the repository's own variables before the loop starts
(`repository variables: {"PICK_CONF_SCALED_FLOOR":"1"}`, `{}` when none are set) —
the one thing the process cannot know, because the workflow materialises the
defaults. A knob whose recommendation differs from its default appears on the
second line exactly when it arrived; `SECTOR_FOLLOW_ON_WEAK=0` and
`DIVERGENCE_SHORT=0` need no proof, because `0` *is* the recommendation either way.
An empty value is treated as absent, in the log and in the code, so `""` and
"unset" can never be two different configurations.

**Measure it before trusting it** — the same early-check harness works for the
projection, and `--lead` is exactly "how early":

```bash
python -m nse_scanner early-check --date 2026-11-12 --sector PSUBK --lead 240
python -m nse_scanner early-check --date 2026-11-12 --sector PSUBK --lead 60
```

The difference between the two rows is what four extra minutes of projection buy
you (and what they cost in cancelled signals).

Cost: one extra 1m batch download per minute for the picked sector, and the 5m
history once per candle. Workflow: `.github/workflows/live-scan-1m.yml` (its own
guard, cache prefix and secrets; it decides the market itself with
`SCAN1M_MARKET_SOURCE=own`, so the two loops share nothing).

**Always-on loop.** `python -m nse_scanner live` sleeps to `close − 60s` (early
pass), then to `close + POLL_GRACE_SECONDS` (10) and polls Yahoo every 5 s for the
closed candle. Every confirmed alert prints its own delay
(`Signal calculated 10:30:12 IST (+12s after close)`).

**Unfinished candles are never judged.** Yahoo's 5m row exists while the candle is
still forming, so "the row is there" does not mean the last trades have arrived. A
stock's candle is used only once its 1m feed shows a print at or after the close.
If other stocks already print past the close, a quiet stock is accepted after
`BAR_SETTLE_SECONDS` (45). If *no* stock prints past the close the whole feed is
behind, and every entry and EMA9-exit decision on that candle waits — up to the
alert deadline for entries, and to the next cycle for exits (older candles are
still replayed, so a hit alert is postponed by a candle, never lost). When that
happens you get one `⚠ Yahoo feed is delayed` message per session.

**Session summary.** After the close one message reports what actually happened:

```
📊 Scanner summary 2026-11-13
Candles scanned 75 of 75 · with a sector pick 54
Sectors picked (candles): PHA long, follow 31 · ENE long, follow 18 · IT long 23
Early passes 53 · with live 1m data 53 · newest 1m bar 62s old on average
Early alerts 3: confirmed 2 · cancelled 1 · unverified 0
Entries 4 · sent 14s after the close on average, slowest 47s
Signals not alerted: 3 fired before their sector was picked (LAURUSLABS 09:30, DRREDDY 09:30, DIVISLAB 09:30) · 1 on the opposite side to the pick (CIPLA 11:05)
At the confirm scan: newest 1m bar 19s old on average
```

`Sectors picked` counts the candles on which each sector was scanned (`follow` =
picked while Nifty was NEUTRAL). `Signals not alerted` lists stock signals the
scanner saw in those sectors but did not send, and why: the stock had already fired
before its sector was picked (the scanner only alerts a signal on the candle it
fires), it fired against the pick's side, it had no valid EMA9 stop, open interest
did not confirm it, the scan finished too late, or the pick already had
`MAX_ENTRIES_PER_PICK` entries. Stocks in sectors that were never
picked are not scanned and cannot appear here.

`with live 1m data 0` or a very old "newest 1m bar" means Yahoo's feed is too
delayed for early alerts; `Candles scanned` well below 75 means the loop started
late or died. The per-candle log line `early 10:25 PSUBK BUY: 12 names, 9 with live
1m data, …` shows the same thing live.

**Dry run.** `python -m nse_scanner live --dry-run` (or *Run workflow* → `dry_run`)
runs the complete loop — early pass, confirm/cancel, hit alerts, summary — printing
instead of sending. It keeps its own throw-away ledger
(`state/scan_state.dry.json`), so the real alert ledger is never touched.

**Sector follow (Nifty NEUTRAL).** The normal pick needs a committed Nifty
direction, so on a flat index it used to say WAIT all day even when a sector was
clearly leading. Now, when Nifty is NEUTRAL, any `PERSIST +` top-3 sector that is a
real leader (persist score at least `SECTOR_FOLLOW_MIN_PERSIST` = 75, up on the day,
in the upper part of its range, positive RS, bullish flow, not rolling over) is
scanned LONG — up to `SECTOR_FOLLOW_MAX` (2) sectors, so a quiet leader does not
hide an entry in the next one. The market message reads
`SECTOR FOLLOW — Nifty NEUTRAL, PHA leads (…); also scanning ENE 60` and stock
alerts show `Sector PHA (sector follow) | Market NEUTRAL`, plus `flow aligned` when
live flow agrees with persistence. Early alerts, confirmation and target / SL /
EMA9-exit alerts work exactly as for a normal pick. Longs only; a committed
BULL/BEAR market is unaffected.

| Setting | Default | Meaning |
|---|---|---|
| `SECTOR_FOLLOW` | `1` | `0` = old behaviour (NEUTRAL → WAIT) |
| `SECTOR_FOLLOW_MIN_PERSIST` | `75` | persist score a sector needs. **Raised from 55 on 17 Nov:** the follow path's picks at persist 63 (CDUR) and 73 (FMCG) lost −2.8R between them, while 80 (REALTY) and 97 (PSUBK) paid +6.2R — and the 09:21 burst of eleven projections came from exactly those two weak picks. `55` restores the old rule |
| `SECTOR_FOLLOW_MAX` | `2` | how many leading sectors are scanned |
| `SF_ALIGNED_SECTORS` | empty | e.g. `PHA,ENE` — sectors your chart shows as SMART$/MARKET/SECTOR/FLOW aligned; scanned first. The scanner cannot read the chart, so this is manual. |

**Open interest (optional).** Yahoo has no futures OI, so this only does something
when you fill `data/oi_history.csv` (`symbol,timestamp,oi,close`; timestamp = the
5m bar's start in IST, e.g. `2026-11-13 10:20`; one row per stock per 5m bar).

| `OI_FILTER` | Effect |
|---|---|
| `annotate` (default) | entry alerts get a line such as `OI Long Build-up · OI +1.80% · price +0.35% (5m)` when a row exists |
| `require` | an entry is sent only when OI confirms it (BUY = long build-up, SELL = short build-up) |
| `off` | OI ignored |

With no rows for today the filter is unavailable and alerts go out unchanged — an
empty file can never silence the scanner.

The older separate commands `sector-follow`, `live-sector-follow`, `dual` and
`live-dual` still exist but now simply run the main scanner (`once` / `live`).
Do not run them next to `live`.

What this cannot fix: yfinance is not an exchange feed (it can lag or rate-limit),
the entry price on a confirmed alert is still the candle close, and you still have
to place the order. For exchange-grade timing use a broker WebSocket feed.

---

## Setup

```bash
git clone https://github.com/atikhalde/Persist-check-.git
cd Persist-check-
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

### Telegram

1. BotFather → `/newbot` → copy the primary bot token.
2. Message the bot, then `https://api.telegram.org/bot<TOKEN>/getUpdates` → copy `chat.id`.
3. Put these required values in `.env` **or** GitHub Actions secrets:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

To mirror every alert through a second bot, create it with BotFather, start a chat with it (or add it to the target group), and optionally add:

```
TELEGRAM_BOT_TOKEN_2=...
TELEGRAM_CHAT_ID_2=...
```

The second pair is optional but must be configured together. The existing primary bot remains unchanged and authoritative for alert deduplication; secondary delivery failures are logged and do not trigger a repeat to the primary bot.

Load env (local):

```bash
export $(grep -v '^#' .env | xargs)
python -m nse_scanner doctor
python -m nse_scanner once --dry-run
python -m nse_scanner live
```

| Command | Meaning |
|---|---|
| `python -m nse_scanner doctor` | yfinance + Telegram ping |
| `python -m nse_scanner once` | one 5m cycle, send new alerts |
| `python -m nse_scanner once --wait-for-bar` | retry the just-closed bar (up to 3 min after close) if Yahoo has not published it yet |
| `python -m nse_scanner once --dry-run` | print, don’t send |
| `python -m nse_scanner once --sector IT` | skip picker, scan IT |
| `python -m nse_scanner once --backfill --dry-run` | diagnose last session without sending old entries |
| `python -m nse_scanner live` | always-on loop: early pass before each close, confirmed scan after it |
| `python -m nse_scanner live --exit-at-close --max-minutes 170` | same, for hosts with a job time limit (GitHub Actions) |
| `python -m nse_scanner early-check --date D --sector S` | measure early alerts against their closes |

State (dedupe) is `state/scan_state.json`.

---

## GitHub Actions

Repo **Settings → Secrets and variables → Actions**:

- Required: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- Optional secondary mirror: `TELEGRAM_BOT_TOKEN_2`, `TELEGRAM_CHAT_ID_2`

Workflow: `.github/workflows/live-scan.yml` runs the **live loop inside one
long job** instead of one cold-started job per candle.

* Every trigger (`workflow_dispatch` from your external scheduler, or the backup
  crons) only means "make sure the loop is alive". A `guard` job checks for an older
  active run: if the loop is running it exits in seconds and sends nothing; if not,
  that run becomes the loop. So the existing hourly dispatcher now acts as a
  watchdog — a crashed loop is restarted at the next trigger.
* A job cannot run longer than 6 h, so after 170 minutes the loop saves its state
  and dispatches its own successor (≈1 minute gap, right after a finished candle).
* Outside 08:30–15:33 IST Mon–Fri, and on exchange holidays, runs exit immediately.
* **Add one trigger at 09:05 IST (03:35 UTC)** to the external scheduler. Its first
  call today is 09:31 IST and GitHub's own cron has been hours late on this repo, so
  without it the first candles of the day are not scanned.
* Manual test without Telegram: *Run workflow* → tick `dry_run`.

State is written to the Actions cache when a run ends (handover, session close,
failure or cancel). If a runner is killed outright mid-session, alerts sent since
the last handover are not remembered by the next run.

Actions restores the **latest same-day snapshot**, validates the state file, then saves a new uniquely keyed snapshot after each run. If state is corrupt the scanner fails closed instead of forgetting earlier deliveries and repeating alerts. (A fixed date-only cache key is immutable and would restore the first snapshot forever, causing repeated signals.) The first run has empty state, but **does not catch up** old table fires: only a fire on the current completed candle within the freshness window can be sent. Entry dedupe resets at the next IST session while open EMA9-trailing positions carry over via the previous cache snapshot. Cache may be evicted; fresh-only filtering still prevents historical replay, but if cache is lost an entry from a later bar for the same stock could be resent. For strict durable once-per-day behavior use an external durable state store; Telegram and cache saves are not an atomic transaction.

---

## Universe

Sector codes follow the flow indicator. Stocks follow the master scanner lists (all batches).

| Pick | Yahoo index | Stocks |
|---|---|---|
| AUTO | `^CNXAUTO` | BAJAJ-AUTO, M&M, MARUTI, TMPV, … |
| FIN | `NIFTY_FIN_SERVICE.NS` | BAJFINANCE, HDFCBANK, … |
| IT | `^CNXIT` | INFY, TCS, … |
| MET | `^CNXMETAL` | TATASTEEL, HINDALCO, … |
| DEF | `NIFTY_IND_DEFENCE.NS` | HAL, BEL, MAZDOCK, … |
| ENE | `^CNXENERGY` | NTPC, POWERGRID, … |
| FMCG | `^CNXFMCG` | HINDUNILVR, ITC, … |
| OIL | `NIFTY_OIL_AND_GAS.NS` | RELIANCE, ONGC, … |
| PHA | `^CNXPHARMA` | SUNPHARMA, CIPLA, … |
| PSE | `^CNXPSE` | BHEL, IRCTC, … |
| PSUBK | `^CNXPSUBANK` | SBIN, BANKBARODA, … |
| BANK | `^NSEBANK` | HDFCBANK, ICICIBANK, … |
| REALTY | `^CNXREALTY` | DLF, GODREJPROP, … |
| CDUR | `NIFTY_CONSR_DURBL.NS` | DIXON, HAVELLS, TITAN, … |
| MEDIA | `^CNXMEDIA` | SUNTV, ZEEL, … |

Edit `config/universe.yaml` to add/remove names. Yahoo suffix is `.NS` (LTIM → `LTM.NS` after the 2026 ticker rename).

Nifty 50 / VIX: `^NSEI`, `^INDIAVIX`.

---

## What was **not** ported (no yfinance feed)

- Futures OI buildup, PCR, Max Pain  
- True options OPEX (the Pine “OPEX” block is a volume-CVD proxy, not F&O)  
- Earnings highlight, TV dashboard cosmetics  

Direction uses Nifty EMA/VWAP/ADX/MTF + opening range. Institutional vote is **VIX-only**. That is enough for the complementary-sector lock you asked for.

---

## Layout

```
nifty-live-scanner/
  nse_scanner/
    market.py      # VIX + Nifty dir + persist +/- + complementary pick
    stock.py       # 5m strength + windows + exception codes
    scan1m.py      # 1m ➜ 5m projection, projected + confirmed alerts on bot 3
    pipeline.py    # one cycle + live loop
    data.py        # yfinance
    telegram.py    # one POST per alert
  config/universe.yaml
  .github/workflows/live-scan.yml
```

Push this folder to GitHub, add the two secrets, enable Actions. Run the live loop in ONE place only (Actions *or* your own box): two loops have separate dedupe state and would double-send.
