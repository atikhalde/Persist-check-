# NSE Live Sector + Stock Scanner

Python port of two TradingView engines, chained the way you specified:

1. **Market + sector brain** (`NIFTY Institutional Core + Flow v4.0`)  
   India VIX, Nifty 5m direction, 15-sector breadth, live RS flow, **PERSIST + / PERSIST −**.  
   **Only the strongest persistent bull/bear sector that complements that day’s Nifty direction is kept.**  
   CHOP / NEUTRAL / VIX-spike conflict → `WAIT` (no stock scan).

2. **Stock 5m scanner** (`MASTER SECTOR BATCH SCANNER`)  
   Runs **only on that sector’s full batch** (both TV batches — Python has no 12-symbol cap).  
   Aggressive window entries + 22-condition exception codes.  
   **Every new/upgraded entry is pushed to Telegram immediately** (one message per fire, no digest delay).

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
Price 1842.50   SL 1831.20
Score 68 Strong   base 20/21  cond22=0  score=68
Time 10:25 IST
```

Deduped per symbol per session. A later **exception** upgrades an earlier ENTRY/NORMAL (same rank rules as the TV table).

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

yfinance NSE 5m is **delayed** (often ~1 minute, sometimes more) and rate-limited.  
**GitHub Actions cron is ± a few minutes and cold-starts.** It cannot be “zero delay”.

Lowest practical delay:

```bash
python -m nse_scanner live
```

on a small VPS (Mumbai / any always-on box). The loop sleeps until the next 5m close + 12s grace, then fetches, computes, and POSTs Telegram in the same cycle.

Actions is shipped as a **backup heartbeat**, not the primary live path.

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

1. BotFather → `/newbot` → copy token  
2. Message the bot, then `https://api.telegram.org/bot<TOKEN>/getUpdates` → copy `chat.id`  
3. Put both in `.env` **or** GitHub Actions secrets:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

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
| `python -m nse_scanner once --dry-run` | print, don’t send |
| `python -m nse_scanner once --sector IT` | skip picker, scan IT |
| `python -m nse_scanner once --backfill` | scan last session (holiday/weekend) |
| `python -m nse_scanner live` | always-on 09:14–15:40 IST |

State (dedupe) is `state/scan_state.json`.

---

## GitHub Actions

Repo **Settings → Secrets and variables → Actions**:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Workflow: `.github/workflows/live-scan.yml`  
Cron: every 5 minutes **09:22–15:32 IST, Mon–Fri** (2 minutes after each 5m close, so Yahoo has the bar and we skip GitHub’s `:00` queue), plus `workflow_dispatch`. Overlapping late jobs are cancelled so the newest bar is not stuck behind a stale run.

GitHub’s scheduler **cannot** fire faster than 5 minutes and may still slip under load. Actions is a heartbeat. For bar-close alerts run `python -m nse_scanner live`.

Actions cache holds `scan_state.json` so the same INFY BUY-EX is not re-sent every run. The **first** successful run of each IST day has empty state, so it catch-up-sends every complementary fire already on the table (then later crons only send new/upgraded codes).

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
