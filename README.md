# NSE Live Sector + Stock Scanner

Python port of two TradingView engines, chained the way you specified:

1. **Market + sector brain** (`NIFTY Institutional Core + Flow v4.0`)  
   India VIX, Nifty 5m direction, 15-sector breadth, live RS flow, **PERSIST + / PERSIST −**.  
   **Only the strongest persistent bull/bear sector that complements that day’s Nifty direction is kept.**  
   CHOP / NEUTRAL / VIX-spike conflict → `WAIT` (no stock scan).

2. **Stock 5m scanner** (`MASTER SECTOR BATCH SCANNER`)  
   Runs **only on that sector’s full batch** (both TV batches — Python has no 12-symbol cap).  
   Aggressive window entries + 22-condition exception codes.  
   **Only newly fired entries from the latest completed 5m candle** are eligible for Telegram (no historical table replay or digest). Each stock can alert once per IST trading day.

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

## Honest latency (yfinance)

A five-minute entry is known only **after the candle closes**; the bar's printed time is its OPEN, so even perfect delivery is at least five minutes later than that printed time. yfinance NSE bars can arrive late or be rate-limited. GitHub Actions cron/dispatch can queue or cold-start unpredictably. **Neither service can guarantee an exactly-on-time trade alert.**

For example, the November 6 workflow started at 09:35:13 IST, sent KALYANKJIL's 09:20 candle at 09:35:38 (10m38s after its 09:25 close), and sent TITAN's 09:30 candle at 09:35:39 (39s after close). That contrast confirms the old KALYANKJIL alert was a **historical table replay**, not a 15-minute Telegram POST. The old run also labelled a still-forming 09:35 market candle as current; completed-bar filtering prevents that. The scanner now ignores forming candles, old Yahoo candles, and historical signals retained by the TV table. By default it suppresses all messages if more than **180 seconds after the 5m close** (configurable via `MAX_ALERT_LAG_SECONDS`); this prevents a 15–20-minute-old entry from being sent, but may mean **missed** entries. Each stock is notified at most once per IST day. Logs show skipped stale candles and actual send times.

Lowest practical delay:

```bash
python -m nse_scanner live
```

on a small VPS (Mumbai / any always-on box), with one instance and persistent `state/scan_state.json`. The loop sleeps until each 5m close + 12s grace, polls briefly for a late Yahoo bar, then computes and POSTs Telegram if still fresh. For genuinely low-latency, guaranteed delivery you need a licensed real-time NSE feed, a continuously running host and monitoring; a GitHub cron/yfinance scanner cannot promise this.

Actions is a **backup heartbeat**, not the primary live path. Do not run both modes simultaneously without sharing the same state store: their local/cache dedupe stores are independent.

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
| `python -m nse_scanner live` | always-on 09:14–15:40 IST |

State (dedupe) is `state/scan_state.json`.

---

## GitHub Actions

Repo **Settings → Secrets and variables → Actions**:

- Required: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- Optional secondary mirror: `TELEGRAM_BOT_TOKEN_2`, `TELEGRAM_CHAT_ID_2`

Workflow: `.github/workflows/live-scan.yml`  
Cron: every 5 minutes **09:22–15:32 IST, Mon–Fri** (2 minutes after each 5m close), plus `workflow_dispatch`. The November 6 log shows an **additional `workflow_dispatch` roughly every five minutes** (for example 04:05:10 UTC = 09:35:10 IST); that event is triggered by an external automation, not the cron lines above. If it is still running, disable it in its own scheduler to avoid unnecessary overlapping runs. Jobs here are serialized; queued runs with expired candles are skipped, not sent late.

GitHub’s scheduler **cannot** fire faster than 5 minutes and may still slip under load. Actions now polls until Yahoo publishes the *specific* just-closed bar (at most 180 seconds after close); if it remains unavailable, the alert is missed rather than sent stale. A workflow run starting immediately at a candle boundary no longer silently misses the bar because of a single early Yahoo fetch; stocks whose bars appear later are retried within the same deadline without resending the ones already delivered. Logs show target bar and deadline, with explicit skipped historical fires. Actions is a heartbeat. For bar-close alerts run `python -m nse_scanner live`.

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
    pipeline.py    # one cycle + live loop
    data.py        # yfinance
    telegram.py    # one POST per alert
  config/universe.yaml
  .github/workflows/live-scan.yml
```

Push this folder to GitHub, add the two secrets, enable Actions. For trading hours keep `python -m nse_scanner live` running.
