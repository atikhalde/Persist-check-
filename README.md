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
🟢 BUY-EX  INFY
Sector IT  |  Market BULL (74%)
Entry (candle close) 1842.50
Entry EMA9 1838.6500  |  SL 1838.28 (0.02% below EMA9)
Risk 4.22  |  T1 1:1 1846.72  T2 1:2 1850.94  T3 1:3 1855.16
Trail: alert on first completed 5m close below EMA9 (even after T3).
Score 68 Strong   base 20/21  cond22=0  score=68
5m bar 10:25–10:30 IST (signal confirmed at close)
Signal calculated 10:31:12 IST
```

Entry is the **signal candle's close** (two-decimal price precision). EMA9 includes that completed candle. For BUY, initial SL = EMA9 × (1 − 0.0002); for SELL, SL = EMA9 × (1 + 0.0002). Stops are rounded outward to 0.01, then 1R/2R/3R targets are calculated from the rounded entry-to-stop distance; verify your symbol's exchange/broker tick size before placing an order. An entry with no EMA9 or with a stop on the wrong side of its entry is **skipped** instead of advertising invalid risk. SELL stop/targets and EMA9 exit are the mirror image of BUY.

The targets are **reference levels**, not auto-sell orders: the scanner continues monitoring the symbol, even when the sector switches or the market returns WAIT, and sends one separate **EMA9 TRAIL EXIT** Telegram alert on the first *completed* BUY candle closing strictly **below** EMA9 (SELL strictly **above**). A wick alone does not trigger a trail exit. The initial SL is a reference, **not an automatically executed broker stop**; neither SL nor targets are executed by this bot. Positions carry into later sessions if the state cache survives; a missing Yahoo bar or expired freshness window can cause a missed exit alert. For actual stop execution use broker-side orders.

**Trailing exit** (only after a qualifying 5m candle closes)

```
🔔 EMA9 TRAIL EXIT — BUY  INFY
5m bar 2026-11-09 11:00–11:05 IST closed below EMA9
Close 1849.00  |  EMA9 1850.2000
Entry 1842.50  |  Initial SL 1838.28
```

One stock entry per symbol per IST session. Later rank upgrades or re-discovered table rows are **not** pushed again. The message labels the 5m candle **open and close** and the calculation time separately; a 10:25 candle cannot be confirmed before its 10:30 close. Telegram's receipt time is the delivery time.

---

## Complementary sector rule (locked)

| Nifty lock | Sector used | Stocks scanned |
|---|---|---|
| BULL / STRONG BULL | strongest **PERSIST +** that still has RS ≥ 0 and is not a qualified short | that sector, **BUY** entries only |
| BEAR / STRONG BEAR | strongest **PERSIST −** that still has RS ≤ 0 and is not a qualified long | that sector, **SELL** entries only |
| NEUTRAL / CHOP / conf < 55 / VIX spike against the day | **WAIT** | none |

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
