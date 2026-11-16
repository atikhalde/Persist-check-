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
VIX 12.80 ↓ -3.10%   ADX 26   MTF 3/3
Breadth 11▲/4▼  TREND UP
PERSIST +  IT 78 · PHA 71 · FMCG 64
PERSIST −  AUTO 70 · MET 66 · MEDIA 61
SECTOR PICK: IT  BULL
PERSIST + IT qualified (score 78)
Action: SCAN LONG
```

**Stock entry** (only same side as the pick)

```
🟢 BUY | BUY-EX17  PNB
Sector PSUBK  |  Market BULL (88%)
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
only: the position stays open, the trail keeps running, and each of `T1/T2/T3` fires at
most once (candles that blow through several levels report them on one line).

```
🎯 TARGET HIT — BUY  INFY · T1 R1 1850.20 (+0.42%, +1.8R) · 5m 11:05 · entry 1842.50
```

**Initial stop hit** — the first candle whose low (BUY) reaches the initial SL. This
closes the position in the ledger and blocks a fresh entry on that symbol for the day.

```
🛑 SL HIT — BUY  INFY · SL 1838.28 (-0.23%, -1.0R) · 5m 11:20 · entry 1842.50
```

**EMA9 trail exit** — the first *completed* candle closing strictly below EMA9 (SELL
strictly above). A wick alone does not trigger it. Also closes the position.

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
| BULL / STRONG BULL | strongest **PERSIST +** that still has RS ≥ 0 and is not a qualified short | that sector, **BUY** entries only |
| BEAR / STRONG BEAR | strongest **PERSIST −** that still has RS ≤ 0 and is not a qualified long | that sector, **SELL** entries only |
| NEUTRAL / CHOP / conf < 55 / VIX spike against the day | **WAIT** | none |

The label describes the side the engine actually trades (`commit_side`, the concrete
lock when one exists), so an alert can no longer read *"Market NEUTRAL"* on a BUY.
`NEUTRAL` now always means no pick and no scan.

No pair-trades. No “rel-long the strongest bull while index is bear”.

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
Sector PSUBK  |  Market BULL (74%)
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
| entry | `format_stock` + OI line | the same text, with `✅ CONFIRMED` in front |
| 🎯 TARGET HIT / 🛑 SL HIT / 🔔 EMA9 EXIT | `_track_positions` | **the same function** — `pipeline._track_positions(..., send=bot 3, save=1m ledger)` |
| ⚠ feed delayed | one notice per session on bot 1 | the same notice on bot 3 |
| ❌ early cancelled | only when `EARLY_…` reports it | only when `SCAN1M_CANCEL_MESSAGE=1` |
| 15:33 summary | `format_summary` | same counters, plus projected / confirmed / cancelled |

Positions are opened by the confirm pass exactly as `run_once` opens them
(`side, entry, sl, entry_ema9, t1–t3, entry_bar, last_bar, session`), so a trade
confirmed on bot 3 is followed to its targets, its stop and its EMA9 exit — and
an SL/EXIT also blocks a re-entry for the rest of the session, just like bot 1.

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
| `SCAN1M_EARLY_PER_DAY` | `1` | projected alerts per stock per session (a projection that failed its close may be retried on a later candle) |
| `SCAN1M_MAX_STALE_SECONDS` | `100` | skip a stock whose newest 1m print is older than this |
| `SCAN1M_GRACE_SECONDS` | `6` | seconds after a minute boundary before asking Yahoo |
| `SCAN1M_SUMMARY` | `1` | `0` disables the 15:33 summary |
| `SCAN1M_1M_PERIOD` | `1d` | 1m history window fetched every minute (prior sessions come from one 7d fetch per day) |

`python -m nse_scanner doctor` prints the three that change behaviour most
(`first minute → up to N min early`, `market source`, `cancel messages on/off`)
next to the bot-3 credentials, so a typo is visible before the session starts.

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
did not confirm it, or the scan finished too late. Stocks in sectors that were never
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
real leader (persist score at least `SECTOR_FOLLOW_MIN_PERSIST` = 55, up on the day,
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
| `SECTOR_FOLLOW_MIN_PERSIST` | `55` | persist score a sector needs |
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
