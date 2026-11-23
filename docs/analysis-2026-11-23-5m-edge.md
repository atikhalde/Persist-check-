# NSE 5-minute scanner — 23 November 2026: edge investigation

## Executive conclusion

**No profitable edge is proven by the supplied reports.** The highest-priority finding is not a magic stock score: it is the combination of **old triggers promoted into new entries, many correlated bank shorts at the same time, and extremely tight stops relative to modeled transaction costs**. This is a research and execution-risk problem, not proof that the short side itself lacks edge.

On 23 November, the 5m audit shows **29 entries**, 27 closed and two still open. Closed trades returned **−₹5,598.24 net**, **−34.53 summed R**, 3 wins and 24 losses; the two open marks sum to **+₹1,153.09 net**, not realized profit. The same audit counts **23 late-leader entries**, but does not supply a per-entry late/rebased flag or full delivery timestamps. The 20 November comparison shows the opposite outcome: 7 closed, +₹1,757.82 net, but one winner accounted for ₹2,466.08. Neither day alone establishes a durable edge.

**Leading testable hypothesis:** on a change in sector pick, prefer a *freshly strengthening* sector and a stock that is gaining rank and moving in the same direction, but require an executable new setup with an economically meaningful stop and a limit on common sector exposure. Test it symmetrically long and short; don't optimize a rule on this one session.

## Sources and accounting conventions

- User-supplied `audit-2026-11-23-5m.json` and `board-2026-11-23-5m.html`. They describe the same session, not independent experiments. The audit includes trades, modeled costs, open marks, sector timeline, leaderboard subsets and settings. The board is a separate sweep: its score/rank need not match the execution scanner's leadership subset, universe, data window or timing. Board bar labels are candle-open labels, not proof of availability at that time.
- Earlier `audit-2026-11-20-5m.json` and `docs/analysis-2026-11-20-5m.md` give context, not a multi-day statistical validation. The prior note's +5.9R catch-up estimate is a hypothetical, not an observed executed result; this session exposes why assumptions about delayed entries, stops and costs must be replayed.
- Audit `entry_at` is the **entry candle label**, not the time a Telegram message was delivered or an order filled. The `alerts.time` field is often the *original fire* time, not the rebased entry time. A difference between these labels is a signal-age proxy, not measured send or execution latency.
- The reports are simulated scanner-ledger outcomes, not broker executions. Raw 1m/5m prices, bid/ask, order acknowledgments and full event logs are absent. No intrabar ordering, actual fill, stop slippage or exact per-name rebase status can be established from these attachments alone.
- R is measured per position against its original stop. Summed R is **not portfolio return** or a fixed-risk portfolio outcome; quantities are approximately ₹100,000 notional per trade, so the rupee risk differs widely. Tiny stops mechanically magnify net R losses after costs.

## 1. The book and its costs

| Closed book | 20 Nov | 23 Nov |
|---|---:|---:|
| Closed positions | 7 | 27 |
| Wins / losses | 4 / 3 | 3 / 24 |
| Win rate | 57.1% | 11.1% |
| Gross P&L | +₹2,449.47 | −₹2,911.66 |
| Modeled costs | ₹691.65 | ₹2,686.58 |
| Net P&L | +₹1,757.82 | −₹5,598.24 |
| Sum of net R | +1.41R | −34.53R |
| Profit factor | 1.85 | 0.23 |
| Open marks, not realized | none | +₹1,153.09 |

The 23 Nov model uses **5 bps per side, ₹20 minimum per order and ₹100,000 notional per position**, roughly ₹100 modeled round trip per name. Real spreads, fees, taxes, impact, borrow restrictions and slippage may be different. The older 20 Nov audit reports zero `risk_rs` for its trade rows, a known historical reporting defect; do not interpret it as zero market risk. That session's +₹1,757.82 drops to **−₹708.26 without TATAELXSI's +₹2,466.08**; that is a concentration sensitivity, not an estimate of what would actually have happened under a new filter.

### 23 November by group

| Group | Closed | Wins | Gross ₹ | Modeled costs ₹ | Net ₹ | Sum R |
|---|---:|---:|---:|---:|---:|---:|
| IT BUY | 8 | 3 | +1,665.13 | 791.30 | +873.83 | +0.67 |
| MEDIA BUY | 1 | 0 | −603.20 | 99.54 | −702.74 | −0.92 |
| BANK SELL | 9 | 0 | −1,931.09 | 897.16 | −2,828.25 | −14.94 |
| PSUBK SELL | 6 | 0 | −1,892.65 | 600.58 | −2,493.23 | −8.36 |
| FIN SELL | 3 | 0 | −149.85 | 298.00 | −447.85 | −10.98 |
| **All SELL** | **18** | **0** | **−3,973.59** | **1,795.74** | **−5,769.33** | **−34.28** |

These trades cluster in the same day, sectors, entry windows and market moves. Side, sector, time and entry mechanism are confounded. The two FIN shorts still open at report time, especially CHOLAFIN marked +₹1,008.03, are excluded from the closed table. It would be wrong to conclude that all shorts lose, or that 18 sells are 18 independent pieces of evidence.

## 2. The 10:20 bank cluster: high persistence was not a fresh entry

The audit's timeline reports:

| Candle label | Market / pick | BANK bearish persistence / intra | PSUBK bearish persistence / intra |
|---|---|---|---|
| 09:30 | neutral / WAIT | 80 / −2.70% | 92 / −3.09% |
| 09:35 | neutral / WAIT | 72 / −1.96% | 91 / −2.51% |
| 10:15 | neutral / IT | 94 / −2.49% | 96 / −3.01% |
| 10:20 | bear / PSUBK | 95 / −2.54% | 98 / −3.17% |

At the **10:20 entry-candle label**, 10 BANK/PSUBK sells were entered in the modeled book. All 10 closed negative: **−₹3,928.96 net**, versus total initial risk ₹3,259.80 and modeled transaction costs approximately ₹1,000. There was also one IT buy on that candle, PERSISTENT, which closed +₹259.88. These are not simultaneous verified fills; the candle labels do not prove send times or prices available to a user.

PSUBK persistence was already **92 at 09:30**, and had fallen intraday 3.09%; at 10:20 it was 98 and −3.17%. This suggests that a high persistent reading did not itself imply a fresh, attractive new short. It does not prove precisely when or why the subsequent stops happened.

The audit's effective `MAX_ENTRIES_PER_PICK` was **0**, meaning no per-sector entry ceiling in the implementation, and `LATE_LEADER_ENTRY`, `LATE_LEADER_REBASE`, and `PICK_CATCHUP` were enabled. BANK and PSUBK are highly correlated and have overlapping constituents; separate sector caps alone would not fully control the shared risk. This is a strong reason to **test a cluster-level risk budget**, measured in actual ₹ risk and not merely number of names. It is not valid to claim how much a cap would have saved until a deterministic selection and execution replay accounts for skipped winners and changes in later capital availability.

## 3. Old fires became fresh positions: a concrete audit limitation

23 Nov's report counts **23 late entries out of 29**. In the code examined for this report, a fresh pick could open a catch-up window and a fire older than `SCAN5M_LEADER_MAX_MINUTES=40` can be re-based onto current-candle levels when `LATE_LEADER_REBASE=1`. This explains how *such* old signals can be considered; the JSON doesn't preserve the per-trade `rebase` flag, so it cannot alone prove the exact path taken by each entry or the precise deployed code version.

Matching each **closed trade by name** with the audit's stored alert fire label and comparing candle labels:

| Recorded fire-label age | Closed trades | Wins | Net ₹ | Sum net R |
|---|---:|---:|---:|---:|
| More than 40 minutes | 19 | 1 | −5,684.36 | −33.36 |
| 40 minutes or less | 8 | 2 | +86.12 | −1.17 |

This is a **post-hoc, closed-trades-only association**, not a rebase-vs-fresh causal experiment; it is confounded by short side, sectors, entry time and exposure. Alert fields are last recorded per name, not a full event history. The two open entries are excluded because their per-trade entry timestamps are not present in `open_trades`. Four closed shorts have original fire labels 09:20–09:30 and entry candle label 14:20: SBIN (300 minutes apart), BAJFINANCE (295), HDFCLIFE (290) and SBILIFE (290). Those four closed **−₹612.14**, with sum **−13.50R**, largely amplified by tiny initial risk denominators.

A crucial limitation in the present rebase logic: the current `catchup_candidate` path returns the rebase candidate for an over-age fire **before** the original fire's stop-touch and original move-from-fire checks. The subsequent rebase plan and momentum rules test the *new* setup, not proof the original signal survived or remained unextended. Rebuilding a stop does not itself create an independent fresh trigger or guarantee a non-chased trade. Verify production version and event logs before attributing any individual result to that exact path.

**Candidate tests, not live instructions:** (a) no entry solely from an old crossover; require a new, timestamped continuation or pullback/re-break trigger after the sector qualifies; (b) separate strict-age versus rebased entries; (c) compare both with labelled informational-only catch-ups; and (d) bound age/extension using prices known at decision time. Measure missed winners as well as avoided losses. 20 Nov's bank trend cautions against simply suppressing every late leader.

## 4. Tight risk and costs: a concrete defect in economic quality

Six of the 27 closed trades had a stop distance below **0.10% of entry**, and all six had modeled round-trip cost larger than their initial rupee risk. They are AXISBANK, RBLBANK, BAJFINANCE, SBIN, HDFCLIFE and SBILIFE. Together they produced **−₹923.45** and **−19.17R**. That is ~55.5% of the day's summed −34.53R, but only ~16.5% of the rupee loss; the R denominator makes the same rupee cost appear much larger.

| Name | Entry | Original stop | Approx stop distance | Initial risk ₹ | Modeled cost ₹ | Closed net R |
|---|---:|---:|---:|---:|---:|---:|
| AXISBANK | 1,412.50 | 1,413.45 | 0.067% | 66.50 | 98.91 | −2.49 |
| RBLBANK | 392.05 | 392.23 | 0.046% | 45.90 | 100.00 | −3.18 |
| BAJFINANCE | 1,102.00 | 1,102.94 | 0.085% | 84.60 | 99.22 | −2.17 |
| SBIN | 1,138.05 | 1,138.80 | 0.066% | 65.25 | 99.04 | −2.52 |
| HDFCLIFE | 576.40 | 576.65 | 0.043% | 43.25 | 99.74 | −3.31 |
| SBILIFE | 1,800.60 | 1,801.00 | 0.022% | 22.00 | 99.04 | −5.50 |

Numbers rounded from the audit; check stop and cost at source when implementing. Example: SBILIFE lost ₹22 gross plus ₹99.04 modeled costs, giving −₹121.04 / ₹22 ≈ **−5.50R**. A stop order need not fill at the stop in reality, so this model can still understate a fast adverse move.

**Test** an economic viability rule based on estimated spread + costs as a fraction of risk and anticipated target, with a volatility/tick-aware minimum stop. Do not merely widen the stop and retain ₹100,000 notional: that changes rupee risk and outcomes. Size from an explicit risk budget, respect notional/lot/liquidity constraints, and replay realistic stop fills. The threshold must be frozen on training data, not fitted to these six trades.

## 5. Stock quality: find acceleration, not just today's top rank

The board's last complete snapshot before the 10:20 candle label shows several shorts that were not top-five by the **separate audit board**: HDFCBANK #7, FEDERALBNK #8, ICICIBANK #10, CANBK #7, CENTRALBK #6 and UCOBANK #8. UNIONBANK was #1 and BANKBARODA #3, yet both lost. Thus a top-five filter alone would neither retain every loser nor eliminate all losers; board position also cannot be equated with what the live scan knew when sending. The audit board is independently fetched after alerts and scores all names, while the scan may rank a different subset using different data.

At 14:15, the board listed SBIN #1 (score 71.3), yet its next candle-labeled trade lost −2.52R with a 0.066% stop. This is a useful counterexample: **leader rank is not a trade plan**, and its cost-adjusted risk and fresh trigger matter.

For TCS, the 5m board starts at 09:40; it cannot establish TCS's 09:30/09:35 full-board ranks. Its recorded 09:40 trade made +0.95R; PERSISTENT entered on 10:20 made +0.61R. Don't transfer scores from the separate 1m board into a claim about 5m execution without synchronizing source, data coverage and timestamps.

Research combination: completed and available sector persistence change + directional intraday progression, plus a current top-N stock with positive directional stock move and improving combined rank, plus an actual fresh trade trigger and cost-aware stop. A strict strengthening requirement can also exclude a sustained high-persistence trend with flat scores; compare continuous rank, flat-but-strong, and no-filter baselines on held-out days. Track both blocked and taken candidates and rank sectors independently where feasible.

## 6. Exits and MFE: do not treat the bar high/low as an executable profit

The audit records **8 of 27** closed positions with MFE ≥1R, **6 of those 8 net negative**: AXISBANK, RBLBANK, MPHASIS, CENTRALBK, CANBK and HDFCBANK; TCS and PERSISTENT closed net positive. Only 4 closed records report a target hit. The MFE measure is excursion relative to original risk, not an entry-time feature.

At the time of analysis the position tracker measures the entire OHLC of a replayed bar before evaluating the stop. If a stop and a favorable excursion happen within the same bar, the report does **not** establish which came first; the bar's favorable high/low can occur after the stop. For a 5m close entry, a signal candle's price is also not proof a fill was available at its close after publication and messaging. Therefore, it is incorrect to claim all six could have banked +1R or that a breakeven rule would have improved the ledger. Replay raw, finer-resolution data and quote-aware fills, using adverse ordering for ambiguous bars; test partial exits, costs on the extra order, and stop gaps separately.

## 7. Misses, feed and delivery integrity

- The audit lists **114 blocked records**: 66 `follower`, 45 `earlier`, 2 `no_data`, 1 `opposite`. These are rule records, not necessarily unique candidates or actual missed profitable trades. Names such as TATASTEEL appear in more than one category. Use timestamps and full event history before deciding whether the refusal was correct.
- 72 timeline candles are listed, while 67 were judged and the 5m full-name board has **67 sweeps**, first bar 09:40, last 15:10. The first five five-minute labels from 09:15 to 09:35 are absent from that board; the metadata lists no `skipped` or `errors`. Do not infer exact cause from the audit alone. `no_bar` and `no_index` are empty, which does not prove all name data or alerts were healthy.
- `early_sent=3`, `confirmed=2`, `cancelled=1` are audit counters, not a trade win rate. `undelivered=0` is an internal primary retry counter, not proof of receipt.
- `secondary_failures=18` is reported. In the inspected Telegram implementation it counts missing/incomplete secondary configuration or failed secondary sends after the primary send succeeded. The audit does not identify which of those causes happened or how many distinct messages/users were affected. Inspect the **actual 23 Nov 5m run logs** and bot-2 membership/configuration; do not expose tokens.
- `scan_lag_max=27.821003` and `lag_max=31.043087` are logged counters, but `lag_n` is null. They do not independently give a validated median or a per-alert close-to-delivery latency. The 20 retained board sweep durations (4.06–6.62 seconds) measure the sweep, not alert receipt or exchange-feed publication delay.

## 8. All 27 closed trades, 23 November

Values are the audit ledger, after modeled costs. Entry is a candle label; exits are reported by their exit label, not confirmed fill times.

| Entry label | Name | Sec | Side | Exit / reason | Net ₹ | Net R | MFE R |
|---|---|---|---|---|---:|---:|---:|
| 09:40 | TCS | IT | BUY | 11:00 EMA9 | 1,209.60 | 0.95 | 1.81 |
| 09:40 | TECHM | IT | BUY | 11:00 EMA9 | 247.85 | 0.12 | 0.50 |
| 09:40 | WIPRO | IT | BUY | 10:50 EMA9 | −62.83 | −0.07 | 0.95 |
| 09:45 | COFORGE | IT | BUY | 10:25 EMA9 | −484.53 | −0.63 | 0.45 |
| 09:45 | INFY | IT | BUY | 10:35 EMA9 | −99.95 | −0.10 | 0.85 |
| 09:50 | LTIM | IT | BUY | 11:00 SL | −97.76 | −0.08 | 0.74 |
| 09:50 | MPHASIS | IT | BUY | 10:50 SL | −98.43 | −0.13 | 1.05 |
| 10:20 | AXISBANK | BANK | SELL | 10:25 SL | −165.41 | −2.49 | 1.26 |
| 10:20 | BANKBARODA | PSUBK | SELL | 11:10 SL | −583.94 | −1.21 | 0.07 |
| 10:20 | CANBK | PSUBK | SELL | 11:10 SL | −352.29 | −1.40 | 1.32 |
| 10:20 | CENTRALBK | PSUBK | SELL | 11:00 SL | −338.02 | −1.42 | 1.87 |
| 10:20 | FEDERALBNK | BANK | SELL | 10:50 SL | −460.27 | −1.28 | 0.11 |
| 10:20 | HDFCBANK | BANK | SELL | 11:10 EMA9 | −81.76 | −0.26 | 2.06 |
| 10:20 | ICICIBANK | BANK | SELL | 11:10 SL | −478.86 | −1.26 | 0.28 |
| 10:20 | KOTAKBANK | BANK | SELL | 11:10 SL | −486.13 | −1.26 | 0.77 |
| 10:20 | PERSISTENT | IT | BUY | 11:50 EMA9 | 259.88 | 0.61 | 1.85 |
| 10:20 | UCOBANK | PSUBK | SELL | 10:35 SL | −342.03 | −1.41 | 0.50 |
| 10:20 | UNIONBANK | PSUBK | SELL | 11:00 SL | −640.25 | −1.19 | 0.11 |
| 10:25 | RBLBANK | BANK | SELL | 10:30 SL | −145.90 | −3.18 | 1.39 |
| 10:30 | BANDHANBNK | BANK | SELL | 10:35 SL | −317.80 | −1.46 | 0.34 |
| 10:30 | PSB | PSUBK | SELL | 10:35 SL | −236.70 | −1.73 | 0.00 |
| 10:30 | PVRINOX | MEDIA | BUY | 10:55 EMA9 | −702.74 | −0.92 | 0.77 |
| 12:00 | INDUSINDBK | BANK | SELL | 12:20 SL | −527.83 | −1.23 | 0.18 |
| 14:20 | BAJFINANCE | FIN | SELL | 14:25 SL | −183.82 | −2.17 | 0.11 |
| 14:20 | HDFCLIFE | FIN | SELL | 14:25 SL | −142.99 | −3.31 | 0.40 |
| 14:20 | SBILIFE | FIN | SELL | 14:25 SL | −121.04 | −5.50 | 0.50 |
| 14:20 | SBIN | BANK | SELL | 14:25 SL | −164.29 | −2.52 | 0.20 |

Open: CHOLAFIN short +₹1,008.03 and LICHSGFIN short +₹145.06, both marked net at report time; neither is included in the closed-trade results above.

## 9. Verification plan before changing live rules

1. **First fix observability and reconcile delivery:** obtain full actual scanner run logs, Telegram delivery records, same-session state and board JSONL. Record signal bar, original fire, pick change, candidate decision time, feed arrival, send result/time, rebase flag and age, entry/stop/targets, actual fills, all gate decisions and configuration/version. Diagnose the 18 secondary failures.
2. Obtain 30–60+ independent sessions of market and name 1m OHLC, ideally bid/ask, sector and universe snapshots and real event times. Include both trend days and reversals. Tape rows or correlated names aren't independent days.
3. Predeclare baseline vs tests: old-fire-to-new-entry alternatives; directional sector and stock momentum ranking; cost/risk viability; a shared BANK/PSUBK/FIN correlated-risk limit; and separately exit rules. Model entries no earlier than information arrival and executable quote, costs and slippage, gaps, non-fills, ambiguous-bar stop/target order, sizing and open positions consistently. Validate long and short separately.
4. Use chronological train/validation windows and freeze thresholds before an untouched later holdout. Bootstrap by **day**, report session count, sample size, cost-adjusted expectancy R/trade, rupee return under defined sizing, drawdown, precision, turnover and missed opportunities. Do not select features or thresholds using final leaderboard values, favorable excursions or later day prices.
5. Consider the prior project hurdle of net ≥+0.30R per trade and precision ≥45% as a proposed go/no-go, subject to adequate independent days and uncertainty. It is not met or tested by these reports.

**Recommendation:** investigate late/rebased entry eligibility, economic stop viability and correlated exposure first; test fresh sector and stock ranking in parallel. Do not disable sells or change live settings solely on this one bad session. No live scanner configuration was changed in this analysis.
