# Parity audit — Pine `MASTER SECTOR BATCH SCANNER` v6.3 vs the Python 5m live scan

Date: 2026-11-15
Pine file audited: `New Text Document.txt` (4,285 lines, v6.3 "1m ➜ 5m Projection Engine",
conditions 8/11/14/15/17/20 removed → **21 base + cond22**)
Python audited: `nse_scanner/stock.py` (engine), `nse_scanner/session.py` (windows),
`nse_scanner/trade.py` (stop/targets), `nse_scanner/pipeline.py` + `early_check.py` (live loop),
`nse_scanner/market.py` (regime gate), `nse_scanner/data.py` (feed).

---

## Verdict in one paragraph

The Python engine is a **faithful port of the Pine *entry engine*, not of the whole indicator**.
All 22 BUY conditions, all 22 SELL conditions, the 100-point strength meter, the three
separate VWAPs, the daily/15m `lookahead_on` semantics, the first-5m-candle sizing with gap,
the retest/BOS machinery, the window times, and all 12 BUY + 19 SELL exception codes are
reproduced line-for-line, including several Pine quirks. The **differences that change what
you see on Telegram** are not in the conditions — they are (a) the data feed (Yahoo vs
TradingView), (b) the extra regime gate + one-side/one-sector scope that Pine does not have,
(c) close-only alerting vs Pine's intrabar table preview, and (d) a completely different
risk model (EMA9 stop + pivot targets instead of Pine's 0.5 % candle stop). Everything Pine
does for *display* (PDH/PDL breakout alerts, ORB labels, first-strong markers, earnings tint,
12-symbol table cosmetics) is not in Python.

So: **do not expect the same alert list.** Expect the same *entry families* on the same bars
when the feeds agree and the gate is open.

---

## 1. What each side actually is

| | Pine v6.3 | Python live scan |
|---|---|---|
| Runs on | chart of one symbol; optionally 12 `request.security` slots for the chosen sector/batch | server loop, whole sector batch from `config/universe.yaml` (no 12-name cap) |
| Bar resolution | 1m chart: 5m bar rebuilt from 1m (developing bar updates **every minute**); 5m chart: `sameTF5` short-circuit | 5m only; one optional *early pass* at T−60 s that rebuilds the forming bar from 1m |
| Signals shown | every historical bar, both sides, plus a "today" table per symbol | only the **latest completed 5m bar**, and only the side the market pick allows |
| Entry conditions | 22 BUY + 22 SELL + 12 BUY / 19 SELL exception codes | identical, `stock.py:711` (BUY) and `stock.py:738` (SELL) |
| Table state | first hit of the day, upgrade only on higher rank, `90/290` cleared after 09:45 | identical, `_table_view()` `stock.py:317` |
| Delivery | `alertcondition` / `alert()` | Telegram, one alert per stock per IST day |
| Risk | `stop_buy = candleLow × 0.995`, `stop_sell = candleHigh × 1.005` (labels commented out) | EMA9 ± 0.02 % stop, S3…R3 daily-pivot targets, trailing on EMA9 (`trade.py:253`) |
| Market context | **none** — sector/batch inputs only pick which 12 symbols to table | VIX + Nifty direction + 15-sector breadth + PERSIST ± → `WAIT` blocks the whole scan (`market.py`) |

---

## 2. Strength meter — component by component

| Component | Pine | Python | Verdict |
|---|---|---|---|
| `trendBase` | `close>ema20 && ema20>ema50 ? 20 : close>ema20 ? 10 : 0` | same, `stock.py:509` | ✅ |
| `trendVwap` | `close > vwap_val`, `vwap_val = f5_vwap_hlc3_p` | `v_typ` = hlc3 session VWAP | ✅ |
| `momRsi` | `rsi>55 ? 12 : rsi>50 ? 6 : 0` | same | ✅ |
| `momMacd` | `hist>0 ? 13 : 0`, 12/26/9 | same | ✅ |
| `adxScore` | `>=25:15 / >=20:10 / >=15:5 / else 0` | same | ✅ |
| `volumeScore` | `obvSlope>0 && relVol>1.5 ? 15 : obvSlope>0 ? 10 : 0` | `obv_slope = SMA5(obv) − SMA20(obv)`, `relVol = vol / SMA20(vol)` | ✅ (Pine's `obvSlope_p` is also SMA5−SMA20; the *linreg* OBV slope is only used by the unused `breakout5m`) |
| `rsScore` | `(close − close[1])/close[1] > 0 ? 15 : 0` | same | ✅ |
| Zones | `>=80 Extreme Strong / >=60 Strong / >=40 Medium / else Low` | same, `stock.py:517` | ✅ |
| `firstStrong / firstWeak / firstRed / firstGreen` | score-transition flags, reset on new day | same, `stock.py:519-524` | ✅ |

No divergence in the score. Note the score is display-only in both: it never gates an exception
code, only `entry_buy = agg_buy and total > 60` / `entry_sell = agg_sell and total < 40`.

## 3. The condition stacks

Verified expression-by-expression against `stock.py:711-735` (BUY) and `stock.py:738-762` (SELL).

| # | BUY condition | SELL mirror | Verdict |
|---|---|---|---|
| 01 | `rsi > rsi[1]` | `rsi < rsi[1]` | ✅ (`rsi5_prev` = committed bar) |
| 02 | `rsi > 55` | `rsi < 45` | ✅ |
| 03 | `close > vwap5` (hlc3 VWAP) | `<` | ✅ |
| 04 | `close > close[1]` | `<` | ✅ |
| 05 | `atr14 > 0.5 × SMA20(atr)` | same | ✅ |
| 06 | `close > ema20(5m)` | `<` | ✅ |
| 07 | `close > open` | `<` | ✅ |
| 08 | `vol × close > 5,000,000` | same | ✅ |
| 09 | `close > high[1]` | `close < low[1]` | ✅ |
| 10 | `vwap5 > vwap5[1]` | `<` | ✅ (Pine's `_c` variant = committed VWAP = `[1]`) |
| 11 | `close > high[2]` | `close < low[2]` | ✅ |
| 12 | `(h−l)/close < 1.5 %` | same | ✅ |
| 13 | 15m close > 15m open | `<` | ✅ |
| 14 | `close > open` (duplicate of 07 — Pine keeps it) | `<` | ✅ duplicate kept |
| 15 | daily open > 50 | same | ✅ (Pine quirk kept) |
| 16 | daily close > EMA39(daily closes) | `<` | ✅ EMA39 built on the 5m series of daily closes, as Pine does |
| 17 | 15m close > EMA39(15m closes) | `<` | ✅ |
| 18 | `high > highest(7)[1]` | `low < lowest(7)[1]` | ✅ (NA until 7 bars → false, matched) |
| 19 | first-5m size `< 4 %` | `< 5 %` | ✅ including the gap: `max(f5h, prevDayClose) − min(f5l, prevDayClose)` |
| 20 | max first-6 body `< 1.5 %` | same | ✅ *including the Pine bug*: body is **signed** `(c−o)/c`, so a big red candle never raises the max |
| 21 | `close < daily R2` | `close > daily S2` | ✅ today's daily pivot from lookahead daily H/L/C |
| 22 | pre–10:15: retest only; after: `originalBreakout or retest` | mirror | ✅ `originalBreakout = close > max(dHigh, highest(20)[1])`, retest band ±0.3 %, both BOS/CHoCH (`b2`/`s2`) and 20-bar/today-high (`b1`/`s1`) paths, 15-bar expiry |

`baseActive` = sum of 01–21, `useExceptionEntry = baseActive >= 20`, `normalTriggerScore` /
`exceptionTriggerScore` are transcribed identically (`stock.py:764-777`).

## 4. Entry layer (windows, filters, locks)

| Element | Pine | Python | Verdict |
|---|---|---|---|
| Windows (bar **open** time) | BUY/SELL W1 09:16–09:59, W2 10:00–11:44, W3 11:45–15:15 | `session.py:21-27` identical | ✅ |
| Final windows | BUY 09:26–11:15, SELL 09:26–12:00 | `FIN_BUY`, `FIN_SELL` | ✅ |
| Aggressive gate | `agg_entry_buy = (window and high > consHigh7[1]) or firstGreen or firstStrong` **and** VWAP-distance ≤ 1 % **and** (upper wick ≤ 25 % or close > close-VWAP) **and** close > close-VWAP **and** `rangeBreak` (20-bar hh/ll break) | identical, `stock.py:539-568` | ✅ |
| Score gate | `entry_buy = agg_buy and total > 60`, `entry_sell = … and total < 40` | identical | ✅ |
| Per-window locks | three booleans, reset daily | `buy_w[0..2]`, `sell_w[0..2]` | ✅ |
| Gap filter | `firstWindowBigGap` (±1.5 % on the first 5m bar) blocks W1 only; gap-up blocks BUY W1, gap-down blocks SELL W1 | identical, `stock.py:445-453` | ✅ |
| Liquidity | `relVol >= 0.2` | `rel_vol >= 0.2` | ✅ |
| Dead Pine paths **not** ported | `breakout5m`, `validBreakout`, `breakdown5m`, `vol_ok = true`, `issideways`, `shasFullCandleAboveVwap`/`isNoFullCandleAboveVwap` | not in Python | ✅ correct to omit — Pine computes them and never uses them in an entry |

## 5. Exception codes

BUY — `chain_b` (`stock.py:802`) is the same priority order Pine uses in
`f_scannerExceptionCode()`: `101 EX → 102 EX17 → 103 EX4 → 104 EX5 → 105 EX6 → 106 EX7 →
107 EX8 → 108 EX9 → 109 EX10 → 110 EX11 → 111 EX12 → 112 EX13 → 80 NORMAL → 90 ENTRY BUY`.

SELL — `chain_s` = `201 EX1 … 220 EX19 → 280 NORMAL → 290 ENTRY SELL`.

Spot-checked condition by condition (Pine lines 1490-1760 for BUY, 2130-2720 for SELL) against
`stock.py:775-960`. Each code's extra gates are reproduced: `baseActive` thresholds
(=21 / >=20 / 18-20 / ==20), which of `isNewDayHigh`, `isCloseAboveDayHigh`, `first5mIsDayLow`,
`entry_buy_today`, `brokePDH/PDL`, `vwapnotfar`, `abovePP/belowPP`, `closeAbove/Below15mORB`,
`bmaxCandleSizeOk`, `body_ok (≤ first5 full or body)`, `isNoRelativeSpike`, `first5size`,
`firstsellsize`, `isVwapBullish/Bearish`, `s_max_body < 1.25 / 1.5 / 2` — all present with the
right value. **No missing or invented exception.**

The `90/290` rule also matches: emitted only before 09:45 and only when no exception/normal code
fired, and **not** locking the day (`BUY_LOCK` excludes 90, `SELL_LOCK` excludes 290).

---

## 6. Fidelity details that were easy to get wrong — and are right

- **Three different VWAPs**, as in Pine: hlc3 session VWAP for the score/`cond03/10`, close
  session VWAP for `longAllowed`/wick filters, and VWAP as of the previous bar for `vwap5_prev`.
- **`lookahead_on` on daily and 15m for completed days**, developing values for today
  (`_daily_lookahead` `stock.py:189`, `_htf15_lookahead` `stock.py:275`) — exactly TradingView's
  behaviour, so a historical replay is comparable to the TV chart and live is not repainting.
- **`ema39_D` / `ema39_15`** are EMA(…,39) applied to the *5m series of HTF closes*, not a 39-bar
  EMA on the HTF itself. Most ports get this wrong.
- **First-5m block** is frozen on the first bar of the day and `first5mIsDayLow/High` compare
  against the daily extreme with a paise-rounded `==` (`_eq`, because Yahoo returns 1554.800049).
- **09:15 exclusion** from the "candles beyond 15m VWAP" counters (`isVwapBullish/Bearish`),
  the 2..6 body trackers, and the range/spike trackers.
- **`isVwapBullish = below < 4`** — the Pine comment says "less than 2", the code says 4, and
  Python matches the **code**.
- **Table semantics**: first hit kept, upgrade only on higher rank (exceptions 3 → normal 2 →
  ENTRY 1), `90/290` wiped once the clock passes 09:45 — including the "wipe on *every* bar after
  09:45, even with no new signal" behaviour (`_table_view` with the last bar's time).
- **ORB** built 09:15–09:25 (Pine session `0915-0930` is end-exclusive), finalised on 09:30 and
  usable by the same 09:30 bar — the exact `orbSessionJustEnded` / same-bar-compare order.
- **Previous-day pivot** `correctPivot` (not today's pivot) is what `abovePP/belowPP` use, and
  PVOTS3 is `prevLow − 2×(prevHigh − PP)`.

---

## 7. Real divergences (ranked by how much they change the alert list)

### D1 — Alert timing: close-only vs intrabar preview *(by design, largest visible difference)*
Pine's scanner rows come from `request.security(sym, "5", …)` and its own chart engine runs on the
**developing** 5m bar, so a row/label can appear up to 4 minutes before the candle closes and then
change or vanish. Python evaluates **only the completed candle** (`pipeline.py:934-960`), plus one
labelled *early* heads-up at T−60 s that is cancelled if the close disagrees
(`early_check.py`, `pipeline.py:356-425`).
→ TVs will show entries Python never sends, and Python will send a "confirmed" flag on ones TV
only settled at the close. `docs/latency-analysis-2026-11-12.md` is the measurement of this.

**Implemented 2026-11-15 (`nse_scanner/scan1m.py`, bot 3).** The missing mode now exists as its
own scan: every minute of a forming 5m candle the candle is rebuilt from the 1m prints and the
*unchanged* `stock.scan_stock` runs on `[completed 5m bars] + [projected bar]`, so the alert can
fire up to four minutes early, and the confirmed entry follows at the close on the same bot.
Rules are not forked (§3–§5 still apply unchanged); only the timing differs. Run it with
`python -m nse_scanner live-1m` (workflow `.github/workflows/live-scan-1m.yml`); the 5m scan on
bots 1/2 is untouched and its ledger is never written by the 1m scan.
Its close pass was audited line by line against `pipeline.run_once` on the same day — name list,
frame repair, late-candle settle, 180 s deadline, engine call, side filter, the four reject
books, EMA9 stop, OI gate, entry text, one-entry-per-name and delivery retry all match; the only
additions are the projected candle, the EARLY message, silent cancellation and bot 3 (table in
the README). The alert set matches too: the market-change message, the once-per-session
feed-delayed notice and the T1–T3 / SL / EMA9-exit tracker are the 5m scan's own code, with the
sender and the ledger swapped for bot 3 and `state/scan1m_state.json`
(`pipeline._track_positions` and `_warn_feed_behind` gained optional `send`/`save` arguments whose
defaults keep bots 1/2 byte-for-byte unchanged; the settle gate, the "is the row published"
test, the reject book and the retry-until-deadline behaviour are the same functions, not copies).
`tests/test_scan1m_parity.py` holds that claim to account mechanically: one synthetic session
through both scanners, comparing the frame given to the engine, the plan and every message
character for character. The scheduler follows the same obligation as `_run_until_ready`: a
closed candle stays "owed" until its close pass has run (`stats.judged_bar`), so a candle with
no projection is still judged — the bug the loop test caught before release.

### D2 — Feed: Yahoo 5m vs TV NSE *(unavoidable)*
Zero-volume 09:15 bars, missing 15:15–15:25 slots, delayed/unsettled prints, interior holes filled
by `fill_nse_grid` (`stock.py:104`) with ffill + 1m volume repair (`data.apply_1m_volume`).
Conditions that read volume or exact OHLC are the sensitive ones: **08** (`vol×close > 5 M`),
**19/20** (first-5m and body %), **05** (ATR), plus every VWAP-based condition (03/10/22, wick
filter, `longAllowed`). Two feeds disagreeing by one tick on the first 5m candle flip 19/20 and
therefore the whole exception that fires.

### D3 — Scope: one gate, one sector, one side *(by design)*
Pine scans whatever symbols you select, both directions. Python scans one sector (the day's
PERSIST pick) and only `md.pick_side` (`pipeline.py:920-925`), so on a `WAIT` day it sends
nothing at all while TV is full of rows. This is the single largest source of "the indicator says
X, the bot said nothing".

### D4 — Extra Python filters that silently drop a signal TV shows
- `WAIT` regime / no complementary sector (`market.py`, `pipeline.py:863`).
- Feed behind the close, or the bar older than the freshness deadline → `late` reject
  (`pipeline.py:905-917`, `pipeline.py:959-962`).
- `build_plan` returns `None` when the EMA9 stop would be on the wrong side → entry dropped
  (`trade.py:278`).
- Optional OI filter (`should_take_entry`), also not in Pine.
- One entry per stock per IST day, even if a *higher-rank* exception fires later
  (`pipeline.py:926`); Pine's table would upgrade the row.

### D5 — Different risk model
Pine: `stop_buy = candleLow × 0.995` / `stop_sell = candleHigh × 1.005`, no targets, no trail.
Python: EMA9 × (1 ∓ 0.0002), pivot T1/T2/T3, EMA9-close trailing exit. None of the Pine SL
numbers exist in Python; compare entries only, not exits.

### D6 — `90/290` vs a SELL exception on the same bar *(small, real bug)*
Pine's chain is one `if/else if`: BUY exceptions → 80 → SELL exceptions → 280 → 90 → 290, so a
SELL exception wins over `90`. Python emits `code = 90` at `stock.py:811` **before** the sell chain
runs (guarded by `if code == 0` at `stock.py:815`), so on a bar where `entry_buy_today` is true
before 09:45 *and* a SELL exception fires, Python reports `ENTRY BUY` and TV reports `SELL-EX…`.
Pre-09:45 + `finSellWindow` makes this rare, and only the label/side of that one bar is wrong.

### D7 — `EX` vs `EX17` when both are true *(deliberate, documented)*
`stock.py:789-790` forces `ex = False` when `ex` and `ex17` are both true, so the stored code
becomes `102 BUY-EX17`. Pine's `f_scannerExceptionCode()` would return `101 BUY-EX` (and TV plots
both labels). Entry is identical; only the code name in the alert differs. The comment says this
was done to surface TV's EX17 rows; it is a knowing deviation, not a bug.

### D8 — Sell-side candle tracking stops after the first entry
Pine sets `sstopTrackingCandles := true` on any sell entry, freezing `isNoRelativeSpike` and the
body/range trackers; `bstopTrackingCandles` is declared for BUY and **never** set. Python keeps
accumulating. Because both sides lock after the first entry (`sell_fired` / `sellWindowFired`),
no alert can be affected.

### D9 — Zero-volume VWAP weighting
`ta.session_vwap(..., fill_zero_vol=True)` (default, used for the hlc3 VWAP at `stock.py:381`)
treats a zero-volume bar as weight 1, so the VWAP moves on such a bar; Pine's sums would leave it
unchanged. Only matters on the 09:15 bar before the 1m repair, and on names Yahoo reports with
zero volume.

### D10 — `isGapSmall` when the previous close is unknown
Pine: `na(gapPct) → false`, so it falls through to the VWAP-distance test. Python: `gap_base == 0`
→ `gap_pct = 0.0` → `is_gap_small = True` → `vwapnotfar` passes unconditionally
(`stock.py:600-604`). Only reachable with a missing previous session.

### D11 — Very first bar of a loaded series
`nz(todayHigh, nz(prevDayHigh5, tf5_high))`: at index 0 Python's `prev_th` is NaN so
`isNewDayHigh/isCloseAboveDayHigh` are False, while Pine compares against the bar's own high.
Unreachable in practice (final BUY window starts 09:26).

### D12 — History rendering
Python only emits rows for the **last** session (`if not is_today: continue`, `stock.py:768`) and
then applies the table view to the last bar. Pine renders every bar. That is why the README says to
compare the **TV TABLE** block, not the plotshape dump — the plotshapes are a per-bar history,
the table is the state.

---

## 8. Pine features with no Python equivalent

| Pine feature | Status |
|---|---|
| `bullish5mBreakout` / `bearish5mBreakout` — PDH/PDL break with `alert()`, yellow/purple bars | **not ported** (Python computes `broke_pdh/pdl` only as an exception filter). If you want those alerts, they are a 15-line addition in `stock.py`. |
| ORB high/low plots + "ORB break" labels | not ported as signals; the ORB *level* is used by EX11/EX12/EX19. |
| `firstStrong`/`firstWeak`/`firstRed`/`firstGreen` plotshapes | computed, used only inside `agg_entry_*` (as in Pine, where the shapes are commented out). |
| Earnings-day background (`request.earnings`) | not ported (README: no feed). |
| 12-symbol table, colours, debug tables, EMA9/15 cross labels, PDH/PDL lines, vertical day lines | not ported (Telegram replaced them). |
| `sectorInput`/`batchInput` (AUTO / BANK ALL / … / Batch 1-2) | replaced by `config/universe.yaml` + the automatic sector pick — better, since Pine caps at 12 names per batch. |

## 9. What Python adds that Pine has none of

Market/sector regime gate with PERSIST ± and `WAIT`; sector RS flow; EMA9 stop + pivot targets +
R multiples; target/SL/trailing-hit alerts and a position ledger; Telegram formatting and dedupe
state; `backtest`, `early-check`, `hit-alerts`, pivot audit tooling; 1m volume repair; stale-feed
and settled-close guards; optional OI confirmation.

---

## 10. Recommended next steps

1. **A/B one session end-to-end.** Run `python -m nse_scanner backtest --date <d>` for the picked
   sector and diff the *TV TABLE* block column-by-column (code, time, base/cond22). Expect the
   disagreements to cluster in D1/D3/D4, not in §3–§5.
2. **Fix D6** (2 lines): move the `90` assignment to *after* the sell chain, i.e. evaluate
   `chain_s` first and only then `if code == 0 and entry_buy_today …: code = 90`, then
   `if code == 0 and entry_sell_today …: code = 290`. Add a test with a bar that satisfies both.
3. **Decide on D7.** If you want TV-identical labels, delete the `ex = False` override and let
   `101` win; if you want the EX17 visibility, keep it and note it in the alert footer.
4. **Optional: port the PDH/PDL breakout alerts** — they are the one *signal* class in Pine with
   no Python counterpart, and the plumbing (`p_high`, `p_low`, `IGNORE_PDH` window, per-day lock)
   already exists.
5. **Document D5 in the alert text** if you ever compare numbers with TV: Pine's SL is 0.5 % off
   the entry candle, Python's is EMA9 — the stop in the Telegram message is not the stop the
   indicator would draw.

---

### Files read for this audit

`/tmp/indicator.pine` (working copy of the attachment, 4,285 lines) ·
`nse_scanner/stock.py` (988) · `nse_scanner/session.py` (135) · `nse_scanner/ta.py` (203) ·
`nse_scanner/data.py` (308) · `nse_scanner/trade.py` (290) · `nse_scanner/early_check.py` (136) ·
`nse_scanner/pipeline.py` (1,223) · `nse_scanner/market.py` (828) · `README.md` ·
`docs/latency-analysis-2026-11-12.md`
