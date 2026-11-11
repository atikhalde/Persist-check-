# Pivot-target audit — PNB, Buy-EX17, 11 Nov 2026 10:25 bar

**Alert under review**

```
🟢 BUY | BUY-EX17  PNB
Sector PSUBK  |  Market NEUTRAL (88%)
Entry (candle close) 135.73
SL 134.94 (0.02% below EMA9)
Risk 0.79  |  next pivot target: T1 R2 136.75 · T2 R3 141.02 · T3 N/A
```

## Verdict

| Question | Answer |
|---|---|
| Is `T1 R2 136.75` wrong? | **No.** Correct classic R2 for the previous session. |
| Is `T2 R3 141.02` wrong? | **No.** Correct classic R3 — but it was never a reachable 5m target (6.7R, 1.5% above the day's high). |
| Was the arithmetic broken? | **No.** Reproduced exactly from real OHLC. |
| Was the alert broken? | **Yes, twice** — see [Bugs fixed](#bugs-fixed). |

## The inputs behind the numbers

NSE was closed on **Tue 10 Nov 2026** (Diwali-Balipratipada), so the previous
session for 11 Nov was **Mon 09 Nov 2026** — and that is the session the scanner
used:

| 09 Nov 2026 | Open | High | Low | Close | Range |
|---|---|---|---|---|---|
| PNB | 125.99 | 132.48 | 125.92 | 132.17 | 6.56 = **4.96 %** |

Classic (TradingView "Traditional") pivots:

| Level | Formula | Value | vs entry 135.73 |
|---|---|---|---|
| PP | (H+L+C)/3 | 130.19 | −5.54 |
| R1 | 2PP − L | 134.46 | −1.27 (already passed) |
| **R2** | PP + (H−L) | **136.75** | +1.02 → **T1** |
| **R3** | H + 2(PP−L) | **141.02** | +5.29 → **T2** |
| S1 | 2PP − H | 127.90 | −7.83 |

`python -m pytest tests/test_trade.py::PivotBasisTests` asserts all three levels
against these exact OHLC values.

## Why it *looked* wrong

Two identities explain everything:

```
R2 − R1 = H − PP          R3 − R2 = PP − L          R3 − R1 = H − L
```

So the 4.27 jump from T1 to T2 is literally `PP − L = 4.27`. On a normal day
(`PP − L ≈ 1`) T1 and T2 sit close together; on 09 Nov the range was 4.96 % of
price (a 125.92 → 132.48 reversal day), so the *upper* pivot stretches while R2
stays reasonable. Nothing is mis-computed — the basis session was simply an
outlier, and the alert gave no hint of that.

What actually happened on 11 Nov: R2 136.75 traded about ten minutes after the
10:31 alert, the session high was ₹139.00 (+5.1 %), and R3 141.02 stayed ~1.5 %
above that high all day. T1 was good; T2 was decoration.

If your chart shows a different R3, check its method — the same day gives
`PP + 2(H−L)` = 143.31, Fibonacci (1.618) = 140.80, Woodie R2 = 137.25.

## Bugs fixed

1. **`Market NEUTRAL (88 %)` on a BUY alert** (`nse_scanner/market.py`).
   The label was derived from `dir_state`, but the picker and the action use
   `commit_side = concrete or dir_state`. A surviving concrete lock with a reset
   `dir_state` produced `NEUTRAL` next to `SCAN LONG`, which the locked rule
   ("NEUTRAL → WAIT") says can never happen. Label and strong-tier flags now
   follow `commit_side`, and `NEUTRAL` always means no pick.
2. **Pivots were published with zero provenance** (`nse_scanner/trade.py`,
   `nse_scanner/pipeline.py`). Now every plan carries a `PivotBasis`:
   * the alert prints `Pivot basis 09 Nov  H 132.48  L 125.92  C 132.17  range 4.96%`;
   * the basis range is compared with the median of the last 20 sessions and
     flagged when > 2.5× (a wide day is what pushes R3/S3 away);
   * each target shows its distance in **R**, with `⚠` beyond 4R so a far pivot
     cannot be mistaken for a peer of T1 (levels are never hidden or replaced);
   * a basis older than 7 calendar days is refused (`N/A`) instead of building
     levels from a stale day;
   * 5m-aggregated vs daily OHLC for the basis day are reconciled; a >0.5 %
     disagreement is logged and the daily bar wins.
3. **Latent crash**: with pandas 3, `groupby([single_key])` yields a 1-tuple key,
   so a 5m frame holding only the signal candle raised `TypeError` inside
   `build_plan` and killed the cycle. Grouping now uses a real column.

The fixed alert for the same signal:

```
🟢 BUY | BUY-EX17  PNB
Sector PSUBK  |  Market BULL (88%)
Entry (candle close) 135.73
SL 134.94 (0.02% below EMA9)
Risk 0.79  |  next pivot target: T1 R2 136.75 (1.3R, +0.75%) · T2 R3 141.02 (6.7R, +3.90% ⚠) · T3 N/A
Pivot basis 09 Nov  H 132.48  L 125.92  C 132.17  range 4.96% (2.2× median)
Score 82 Extreme Strong   base 21/21 cond22=0  score=82
```

## Verifying

```bash
python -m pytest tests -q          # 52 tests
python -m pytest tests/test_trade.py -q -k PivotBasis -v
```

Sources for the market data: NSE/ETMoney session OHLC for 09 Nov 2026 (open
125.99, high 132.48, low 125.92, close 132.17), exchange holiday calendar for
10 Nov 2026, and 11 Nov intraday high ₹139.00 (NSE via Upstox).
