# 1m Scan Audit — 2026-11-25 (Missed Big Winners / Edge)

**Source data:** `audit-2026-11-25-1m.json`, `board-2026-11-25-1m.html` (uploaded).  
**Board scope:** 72 candles, 15 sectors, 13,679 name-scorings, every 5 min, 09:20–15:15.  
**Session:** 2026-11-25, bot 1m, BULL / conf 65 / TRANSITION. Pick: **FMCG long** (`PERSIST + FMCG qualified (score 97); also scanning PHA 77`).

## 1) Executive summary

| Metric | Value |
|---|---:|
| Candles / judged | 72 / 53 |
| Entries / alerts | 15 / 15 |
| Confirmed / cancelled / catch-up | 5 / 3 / 15 |
| Late entries | 8 |
| Closed trades | 14 (5W / 9L, 35.7% WR) |
| Net | +₹1,100.27 (+0.14R), PF 1.31 |
| Avg win / avg loss | ₹938.74 / -₹399.27 |
| Avg MFE / avg giveback | 1.17R / 0.98R (83.5% giveback) |
| Max DD | ₹2,098.36 |
| Open | 1 trade, +₹87.62 |

**Read:** Direction was mostly right (FMCG). The miss was not "wrong sector" — it was **selection timing and breadth**. The scanner caught some FMCG winners (GODREJCP +2.15R, VBL +2.26R, BRITANNIA +0.92R, PFC +1.43R) but missed the day's best persistent leaders and missed qualified FIN/PHA opportunities during FMCG lock.

## 2) Big winners missed

From `board.summary` (not entered / missed conversion). `peak_mv` = peak move from own open; `top5` = candles in sector top 5 out of 72.

| Name | Sector | Side | Peak % | Close % | Best rank | Best pos | Top5/72 | Rank avg | Blocked | Evidence |
|---|---|:---:|---:|---:|---:|---:|---:|---:|---|---|
| DABUR | FMCG | BUY | **7.54** | 6.24 | 99.9 | 1 | **71** | 81.4 | `earlier` | #1 FMCG at 09:25 (scanned, rank 85.5, s/v/t 73/100/96, lead +0.9pp) while pick was WAIT; stayed top1/top2 almost entire day. |
| HINDUNILVR | FMCG | BUY | **5.14** | 5.08 | 99.9 | 1 | 66 | 73.9 | `follower` | #6 at 09:25 (`scanned=false`), became #1 by 10:10 and dominated; fresh leader shows 09:55 confirm other / 10:00 catchup preferred, but never entered. |
| BAJAJFINSV | FIN | BUY | **5.04** | 1.51 | 99.8 | 1 | 62 | 73.8 | `follower` | FIN qualified 10:15–11:20 while FMCG held pick; rank 7 unscanned at 10:25, rank 3 scanned at 10:30; 09:55 catchup preferred in fresh leaders. |
| ITC | FMCG | BUY | **3.59** | 2.76 | 87.6 | 1 | 55 | 59.2 | `earlier` | Scanned top3/1 through early bars; 09:35 catchup other, 09:55 projection/confirm preferred in fresh leaders; board final not entered. |
| DIVISLAB | PHA | BUY | **3.26** | 3.26 | 99.9 | 1 | 58 | 70.7 | `earlier` | PHA was extra/late-qualified only; DIVISLAB closed at day high, persistent top1. |
| HDFCLIFE | FIN | BUY | **3.12** | 2.15 | 91.9 | 1 | 37 | 57.9 | `follower` | FIN leader missed during FMCG concentration. |
| BIOCON | PHA | BUY | **2.50** | 1.87 | 97.8 | 1 | 50 | 67.1 | `follower` | Strong PHA rank persistence, not converted. |
| CIPLA | PHA | BUY | **2.37** | 1.53 | 95.2 | 1 | 42 | 65.4 | `follower` | PHA cohort miss. |

## 3) Why we missed them (root causes)

### A. "Earlier" — fired before sector was pickable/actionable

Code meaning (`pipeline.py` / `scan1m.py`): a signal fired before its sector was picked is noted as `earlier` ("fired before their sector was picked"). Catch-up exists but is narrow.

- **DABUR:** FMCG not qualified at 09:25 (`pick=WAIT`, `qual=['MEDIA']`, FMCG persist 71). DABUR already rank 1, scanned. FMCG picked at 09:30, then WAIT 09:35–09:50. DABUR catchup at 09:35 was `status=other` in fresh leaders; never entered.
- **ITC:** similar early-fire pattern; saw projection/confirm preferred at 09:55 but still not entered as board final.
- **DIVISLAB:** PHA never had sustained primary pick. PHA picked briefly 13:30 (NEUTRAL conf 68), otherwise only qualified late. DIVISLAB trended all day.

**Failure mode:** the best trend often starts before sector qualification/pick stabilizes; "earlier" rejects the highest-quality leader.

### B. "Follower" — outside top-N scan at the critical time

Code meaning: `follower` = "followers not scanned — outside the sector's top" (`pipeline.py` logs outside top N; `scan1m` rejects follower).

- **HINDUNILVR:** 09:25 rank 6, `scanned=false`; later became #1. The transition from follower to leader was missed.
- **BAJAJFINSV:** 10:25 rank 7 unscanned, 10:30 rank 3 scanned; FIN was qualified but FMCG remained pick.

**Failure mode:** top-N is a lagging filter. Names about to lead are often just outside top-N before expansion.

### C. Sector pick concentration / single-pick starvation

Timeline evidence:
- FMCG picked continuously 10:15–13:10, STRONG BULL conf 99–100 for long stretch.
- FIN qualified 10:15–11:20+ but was not picked over FMCG.
- PHA qualified 13:30, 14:00, 14:10, 14:55–15:10; picked only 13:30 briefly.

Qualified sectors became "observed but unallocated". BAJAJFINSV/HDFCLIFE (FIN) and DIVISLAB/BIOCON/CIPLA (PHA) moved without allocation.

### D. Catch-up was too restrictive

Diagnostics: `catchup_skip = {chased: 38, stop: 26, old: 56, none: 2}` (122 skips vs 15 catch-ups). `SCAN1M` catch-up window is 10 min after pick appears (`_CATCHUP_WINDOW_MINUTES = 10`).

- **old (56):** early fires aged out before sector became actionable.
- **chased (38):** big winners look chased early; raw-move chase filter penalizes exactly the names that become 5–7% winners.
- **stop (26):** legitimate in many cases, but needs review against leader persistence.

### E. Giveback eroded captured winners

Avg MFE 1.17R, avg giveback 0.98R (83.5%). Examples: COLPAL 0.32R MFE -> -0.84R; NESTLEIND -0.06R despite +3.11% close; TORNTPHARM -0.62R despite +2.24% peak. Good holds: GODREJCP +2.15R, VBL +2.26R.

## 4) The edge

1. **Persistent Leader Catch-up (primary edge).** If a name was earlier/follower but remains top1/top2 with strong rank/structure and positive side move, and its sector is qualified/picked/strong, treat as high-priority forward candidate — not a silent reject.
2. **Rising leader probe.** Add a probe just outside top-N for rank-jump + lead_pp surge + structure/volume expansion.
3. **Multi-sector allocation.** When multiple sectors qualify, allow controlled allocation per qualified sector (risk-capped), instead of single-pick starvation.
4. **Chase filter by structure, not raw move alone.** Skip when signal structure/stop/RR is invalidated, not merely because price moved.
5. **Giveback-aware trailing for persistent leaders.** Use structure/trailing for high top5/rank_avg leaders; avoid fixed early exits on clean trends.

## 5) Forward-testing implementation (added)

Implemented in `nse_scanner/scan1m.py` as a **separate, observation-only** alert path. Original 1m scan logic is intact.

- **Flag:** `SCAN1M_PERSIST_FWD_ALERT` (default `1`). Set `0` to disable.
- **Hook:** runs after `sweep_board(now=now)` in `live_loop_1m`; does not run inside original confirm/project decision paths.
- **State:** writes only to `stats.persist_fwd_alerted` (dedupe key `sector|name|side`). **Never** writes `sent`, `positions`, or `early`.
- **Alert channel:** `telegram.send_bot3` with clear label: `🔬 [FORWARD TEST] PERSISTENT LEADER — observation only, not an entry`.
- **Signal (conservative v1):** latest board bar top1–2, rank >= 72, structure >= 55, favorable move >= 0.5%, sector pick/gate or persist >= 70, top5 persistence in recent 8 board bars (>=4/8, or strong top2). Skips names already in sent/positions or already forward-alerted.
- **Reason tag:** includes best-effort original miss reason (`earlier`, `follower`, etc.) from reject book.

This is for forward testing only. Track outcomes before considering any promotion into the real entry path.
