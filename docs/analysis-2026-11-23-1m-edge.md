# 1-minute projected scanner: edge investigation, 23 November 2026

## Verdict

**No statistically established tradable edge can be claimed from these reports.** The strongest research candidate is **early, fresh leadership in a sector that is accelerating now**, rather than chasing a sector merely because its persistence remains high after a large move. A second candidate is better exit management. Both are hypotheses, not proven improvements.

On 23 November, the recorded closed book made ₹645.13 gross but lost **₹1,642.94 net**, or **−3.35R**, on 23 closed trades. Costs were ₹2,288.07. There was one additional open trade marked +₹145.20 net; it is not a realized close. Results are the scanner's modeled ledger, not verified broker fills.

## Sources and what they can establish

- User-provided `audit-2026-11-23-1m.json` and `board-2026-11-23-1m.html`: same session, **not two independent samples**. The JSON supplies trades, recorded blocks, market/sector timeline, leaderboard subset, aggregate stats and board summaries. The HTML shows the full sector/name board. Board snapshots are five minutes apart even though this is the 1m projected scanner.
- Context only: `audit-2026-11-20-1m.json` and repository notes `docs/analysis-2026-11-20-1m.md` and `docs/analysis-2026-11-19-1m.md`. The 19th is partial logs plus an end-of-day summary, not an independent full replay here.
- The board summary's first/last/peak moves and best ranks summarize the *whole session*. These are **hindsight**, not features available at the beginning. A sector's final/summary side should not be applied retrospectively to every earlier bar. A bar's label is not necessarily its live availability time.
- The attachments do not supply a complete timestamped order/alert delivery record, actual executions, or the underlying 1-minute OHLC and quote stream. Consequently they cannot establish executable entry times/prices, event ordering within candles, realized slippage, or simulate alternative exits reliably.

## Scorecard and costs

| Closed ledger | 20 Nov | 23 Nov |
|---|---:|---:|
| Closed trades | 5 | 23 |
| Winners / losses | 2 / 3 | 6 / 17 |
| Win rate | 40.0% | 26.1% |
| Gross | −₹513.22 | +₹645.13 |
| Modeled costs | ₹494.93 | ₹2,288.07 |
| Net | −₹1,008.15 | −₹1,642.94 |
| Total net R | −1.31R | −3.35R |
| Open marked net (not realized) | +₹708.52 | +₹145.20 |

23 Nov cost model: ₹100,000 capital per trade, 5 bps per side, ₹20 minimum per order. Typical modeled round trip is around ₹100, or 0.10% of notional. Real spread, market impact, slippage, borrow/shorting constraints and applicable taxes/charges may differ. It is incorrect to assume these outcomes are the P&L of a live account. Fixed notional also means individual risk in ₹ and contribution to summed R vary substantially; the sum of Rs is not a return on a single fixed-sized portfolio.

At the observed 23 Nov 26.1% win rate, average winning trade ₹623.64 and average losing trade ₹316.75 give a naive break-even win rate of about **33.7%** (316.75 / (623.64 + 316.75)). This is descriptive, not a forecast: the sample is small, correlated, and includes a small number of outsized winners.

## Where the money came from

Partitioning the 23 closed trades by recorded sector and side:

| Group | Closed n | Wins | Gross ₹ | Costs ₹ | Net ₹ | Sum net R |
|---|---:|---:|---:|---:|---:|---:|
| IT, buy | 10 | 5 | 3,740.15 | 990.28 | **+2,749.86** | +2.83 |
| BANK, sell | 6 | 0 | −2,382.21 | 598.66 | **−2,980.88** | −3.90 |
| PSUBK, sell | 3 | 0 | −505.94 | 300.19 | −806.12 | −0.56 |
| MET, sell | 2 | 1 | +104.62 | 199.53 | −94.90 | −1.28 |
| FIN, sell | 2 | 0 | −311.49 | 199.41 | −510.90 | −0.44 |
| **All sells** | **13** | **1** | **−3,095.02** | **1,297.79** | **−4,392.80** | **−6.18** |

The ₹0.01 discrepancy in BANK gross minus costs is rounding in the individual ledger entries. Every buy happened in IT before 10:00, while all recorded sells began at 10:20 or later. **Side, sector and time are completely confounded**: the data cannot tell us whether IT, longs, early entries, or the particular price paths caused the difference. Do not turn off shorts based on this day.

The first five BANK/PSUBK shorts, all with recorded entry time 10:20, returned −₹2,438.07 net in total (−1.53R): RBLBANK −₹393.29, AXISBANK −₹574.91, FEDERALBNK −₹1,032.05, IDFCFIRSTB −₹99.96 and CENTRALBK −₹337.86. These are closely related exposures, not five independent tests. A portfolio cap or correlated-sector risk budget is a candidate **risk control**, not a demonstrated alpha improvement. Model rejected opportunities and position sizes before claiming a P&L benefit.

## Market timing: the useful distinction

Timeline values below are the audit's recorded bar labels and sector snapshots; entries attributed to a timestamp are not proof an order could have been filled at that timestamp.

| Board bar | Pick / market | IT bull persistence; IT intraday | BANK bear persistence; BANK intraday | PSUBK bear persistence; PSUBK intraday |
|---|---|---|---|---|
| 09:30 | WAIT / neutral | 49; −0.16% | 80; −2.70% | 92; −3.09% |
| 09:35 | WAIT / neutral | 84; +0.55% | 72; −1.96% | 91; −2.51% |
| 09:40 | IT / neutral | 93; +0.80% | 75; −2.03% | 94; −2.70% |
| 10:20 | PSUBK / bear | 96; +1.66% | 95; −2.54% | 98; −3.17% |
| 15:10 | BANK / strong bear | 76; +0.75% | 98; −3.68% | 97; −4.04% |

IT's recorded persistence rose **49 → 84 → 93** from 09:30 to 09:40 as its intraday move improved. The 10 IT buys are recorded 09:40–09:50. In contrast PSUBK was already at 92 and −3.09% at 09:30, well before the 10:20 pick, when it was 98 and −3.17%. These observations suggest **incremental leadership and freshness**, not high persistence alone, might matter. They do not establish whether early sell signals were executable or whether the subsequent recorded entries were late, nor do they explain the losses causally.

Candidate, symmetric on both sides, to test **without changing live rules yet**:
1. Use only a completed, fresh snapshot known at decision time. Define aligned persistence and intraday move in the proposed trade direction.
2. Compare two predeclared features: (a) change in aligned persistence over the last two completed 5-minute snapshots; (b) move already completed since the session open, measured at decision time. Test a continuous score or fixed buckets, rather than selecting the best number on this session.
3. Compare fresh same-bar triggers with catch-up/rebased triggers separately using their **actual decision/send timestamp and then-executable quote**. Do not treat signal candle open, historical fire price or end-of-day rank as the fill.
4. Test with and without a risk budget across correlated BANK/PSUBK/FIN positions; preserve long and short examples and measure opportunity loss as well as drawdown.

This is a research proposal, not an instruction to loosen current stale-signal protections. Catch-up alerts and entry paths have distinct semantics; never treat an informational 'not an entry' message as an executable trade.

## Exit management: a lead, not a backtest

Eight of 23 closed trades recorded MFE of at least +1R, and **three of those eight closed net negative**: MPHASIS (MFE 1.05R, net −0.13R), HCLTECH (1.71R, −0.05R), LTIM (1.21R, −0.11R). WIPRO, LTTS, TCS, SAIL and PERSISTENT are the other five. Across all closed trades, mean MFE was 0.77R and mean give-back 0.78R according to the audit. Six trades recorded at least one target hit; 17 recorded none. Seventeen closed with `EMA9 EXIT` and six with `SL HIT`.

This warrants a controlled test of partial profit-taking or a trailing stop after a favorable move. **MFE is an after-entry, hindsight statistic, not an entry signal.** The report does not reveal whether an intrabar stop and target were touched in which order, the liquidity at those prices, or whether partial exits would have met costs. We cannot assert that moving a stop at +1R would have improved this book. Replay against timestamped raw OHLC/quotes under conservative ambiguous-bar ordering, then include additional transaction costs and realistic stop slippage.

## Missed leaders and blocked signals

The audit lists 106 blocked **records**, not 106 unique profitable trades: 54 `earlier` and 52 `follower`. Examples include BANKBARODA and UNIONBANK with historical fires labeled 09:20, HDFCBANK 09:25 and TATASTEEL 09:40; APLAPOLLO is listed as a MET follower. Some names have multiple block records. These are useful leads for a counterfactual replay, **not proof the blocks were mistakes**. A later move from the open cannot be credited as P&L from a trade that wasn't entered; the fire time, direction, decision-time availability, risk, eligibility, spread and subsequent path must be reconstructed.

Audit aggregates 24 recorded entries, 13 catch-ups and 15 late entries, but supplies no per-trade late/catch-up classification or original signal-to-delivery latency. They should not be summed as independent populations or mapped to individual outcomes without the event ledger. `early_sent` is null, not zero. Diagnostics say 67 judged, 72 candles, and list no-bar labels 15:15, 15:20 and 15:25; the available audit alone does not reconcile every difference, so investigate raw state/logs rather than equating 72−67 with exactly those three gaps. `undelivered=0` is an internal counter, not Telegram or broker receipt evidence.

Board metadata reports 72 sweeps and 13,680 name observations, zero recorded stale and zero listed errors, but its `seconds` list has only 20 values (approximately 4.68–11.97 seconds). These are recorded sweep durations, not end-to-end alert latency or proof that a stock's underlying bar was timely. Full rank history, raw quotes, per-event delivery timestamps, and entry fills are needed to evaluate a one-second requirement.

## Every closed trade, 23 November

`Entry` is the recorded ledger time; `R` is net after the audit's modeled costs. Ordered by entry, not a realizable ranking.

| Entry | Name | Side | Sector | Exit | Net ₹ | Net R | MFE R | MAE R |
|---|---|---|---|---|---:|---:|---:|---:|
| 09:40 | WIPRO | BUY | IT | EMA9 | 346.01 | 0.48 | 1.76 | 0.00 |
| 09:40 | TECHM | BUY | IT | EMA9 | 412.93 | 0.22 | 0.63 | −0.02 |
| 09:40 | TCS | BUY | IT | EMA9 | 1,209.60 | 0.95 | 1.81 | −0.05 |
| 09:40 | LTIM | BUY | IT | SL | −97.51 | −0.11 | 1.21 | −0.17 |
| 09:40 | PERSISTENT | BUY | IT | EMA9 | 1,172.34 | 1.67 | 2.42 | −0.21 |
| 09:45 | COFORGE | BUY | IT | EMA9 | −484.53 | −0.63 | 0.45 | −0.68 |
| 09:45 | INFY | BUY | IT | EMA9 | −99.95 | −0.10 | 0.85 | −0.23 |
| 09:50 | LTTS | BUY | IT | EMA9 | 416.05 | 0.53 | 1.74 | 0.00 |
| 09:50 | MPHASIS | BUY | IT | SL | −98.43 | −0.13 | 1.05 | −0.25 |
| 09:50 | HCLTECH | BUY | IT | EMA9 | −26.65 | −0.05 | 1.71 | −0.74 |
| 10:20 | RBLBANK | SELL | BANK | EMA9 | −393.29 | −0.29 | 0.03 | −0.22 |
| 10:20 | AXISBANK | SELL | BANK | EMA9 | −574.91 | −0.29 | 0.07 | −0.30 |
| 10:20 | FEDERALBNK | SELL | BANK | EMA9 | −1,032.05 | −0.59 | 0.00 | −0.57 |
| 10:20 | IDFCFIRSTB | SELL | BANK | SL | −99.96 | −0.11 | 0.90 | −0.10 |
| 10:20 | CENTRALBK | SELL | PSUBK | EMA9 | −337.86 | −0.25 | 0.37 | −0.22 |
| 10:30 | SAIL | SELL | MET | EMA9 | 184.89 | 0.27 | 1.28 | 0.00 |
| 11:00 | ICICIBANK | SELL | BANK | SL | −352.84 | −1.39 | 0.05 | −1.52 |
| 11:50 | JSWSTEEL | SELL | MET | SL | −279.79 | −1.55 | 0.68 | −2.32 |
| 12:00 | INDUSINDBK | SELL | BANK | SL | −527.83 | −1.23 | 0.18 | −1.60 |
| 13:30 | UCOBANK | SELL | PSUBK | EMA9 | −140.32 | −0.07 | 0.18 | −0.08 |
| 13:30 | PFC | SELL | FIN | EMA9 | −130.50 | −0.11 | 0.28 | −0.03 |
| 13:50 | PSB | SELL | PSUBK | EMA9 | −327.94 | −0.24 | 0.13 | −0.17 |
| 15:00 | BAJAJFINSV | SELL | FIN | EMA9 | −380.40 | −0.33 | 0.00 | −0.24 |

## How to prove or reject an edge

1. Gather at least 30–60 **independent trading sessions**, including multiple trend, reversal and range days. Save 1-minute OHLC (preferably quotes), 5-minute sector snapshots with data-available timestamps, universe membership, full candidates and rejection reasons, event/send/receipt times, entry rules, exit decisions, position sizes, costs, and configuration hashes. A count of 1-minute rows is not a count of independent days. Keep the 23 Nov report untouched as exploratory data.
2. Specify baseline and two symmetric variants before testing: **freshness** (two-bar persistence change and move already completed), and **exit management** (partial/stop rule). A third portfolio-risk experiment caps shared sector exposure. Define missing-data policy, spread/slippage, order of intrabar stop/target hits, and short-sale constraints before looking at performance.
3. Replay chronological rolling windows with no lookahead: decisions only use information published by decision time, fills use the next available executable price; include realistic latency, costs, non-fills and partial exits. Cluster or bootstrap uncertainty by day, not by correlated individual trade. Compare on the same candidate universe.
4. Freeze rules and thresholds using training sessions only. Evaluate on a strictly later untouched holdout, preferably also paper trade forward. Report net expectancy in R/trade, number of trades and trading days, win rate, profit factor, max drawdown, turnover, missed opportunities, slippage sensitivity and uncertainty intervals, overall and separately for longs/shorts and market regimes. Include open positions consistently.
5. A previously discussed project gate is **net expectancy ≥ +0.30R/trade and precision ≥45%**, after costs, with adequate independent days and no reliance on one sector/day. Treat this as a proposed hurdle, not as something these 28 closed observations have passed. The prior frozen 20 Nov bot-4 holdout is a different study and cannot validate a 1m scanner rule tuned on these reports.

**Bottom line:** investigate fresh, strengthening sector leadership and correlated exposure first; investigate exits second. Nothing here supports declaring a profitable edge, a guaranteed fast alert, or changing the live scanner on two sessions of modeled outcomes.

## Implementation following approval

Bot 3 now prioritizes a candidate when its own sector's aligned persistence and intraday move both increased between the two most recent consecutive completed 5-minute board snapshots, and the stock is in that sector's top five with an increasing combined score and aligned move. This is a simple **ordering heuristic**, not the post-hoc +10 cutoff or a validated edge. Existing signal, market pick, sector-follow, freshness and risk gates still apply. The sector selector itself is unchanged; this ranks candidates *within the sectors the existing scan has selected*, not the entire universe.

Only board rows already present in the same-session ledger are used; current/forming candles and missing or discontinuous snapshots are never inferred. If evidence is incomplete or the sector changed side, baseline ranking applies without claiming fresh leadership. The audit's `diagnostics.fresh_leaders` stores status and component deltas for each evaluated candidate, stage and candle.

`SCAN1M_ALERT_CAP` (default 3) now also limits actual bot-3 entries per 5-minute candle, shared across catch-up promotions and close confirmations. Projection messages have a separate cap and count across minute retries. A catch-up with no entry capacity remains an informational message. `0` disables both caps. The cap is a per-candle concentration control, **not** a total daily or correlated-sector exposure limit; it can suppress a later winner. It does not change bot 1, bot 4, or exits. Position sizing, partial exits and portfolio-wide sector-risk limits remain unvalidated and unchanged.

The cap and ranking use data available at each decision, not omniscient ranking of all future signals in the candle: the first valid signals may consume the slots before a better one arrives. Paper/replay validation remains essential; live returns and alert latency have not been verified by the local tests.
