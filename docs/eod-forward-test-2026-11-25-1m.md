# EOD Forward-Test Audit Report — 2026-11-25 (1m)

**Type:** Forward-test / observation-only (separate from original 1m scan entries)  
**Session:** 2026-11-25, bot 1m, BULL conf 65 / TRANSITION, last pick FMCG long  
**Source:** `audit-2026-11-25-1m.json` + `board-2026-11-25-1m.html` board summary

## Forward-test rule (v1, now wired into 1m bot)

Alert when a name shows persistent leader quality but was missed by original scan:

- Sector is PICK / gate-qualified / persist >= 70
- Name is top1–2 in latest board bar, rank >= 72, structure >= 55
- Favorable move >= 0.5% (side-aware)
- Persistence: top5 in >=4 of last 8 board bars (or strong top2)
- Not in `sent` / `positions` / `early`
- Dedupe: `stats.persist_fwd_alerted` (`sector|name|side`), once per session
- **Observation only** — never writes `sent`/`positions`/`early`, never affects original entries

Env flag: `SCAN1M_PERSIST_FWD_ALERT=1` (default on).  
EOD/audit: included in `eod` text section "Forward-test persistent leader alerts" and in audit JSON `counts.persist_fwd_alerts` + `forward_test.persist_fwd_alerted`.

## Retrospective 2026-11-25 candidates (from board summary)

These are the names the forward-test persistent-leader rule is designed to surface. Status below is retrospective from board summary; live alerts will be emitted by the new 1m hook after board sweep.

| Name | Sector | Side | Peak % | Close % | Best rank | Top5/72 | Rank avg | Original blocked | Forward-test tag |
|---|---|:---:|---:|---:|---:|---:|---:|---|---|
| DABUR | FMCG | BUY | 7.54 | 6.24 | 99.9 | 71 | 81.4 | earlier | **QUALIFY** - persistent #1 leader, missed before pick stabilized |
| HINDUNILVR | FMCG | BUY | 5.14 | 5.08 | 99.9 | 66 | 73.9 | follower | **QUALIFY** - follower->leader transition, strong persistence |
| BAJAJFINSV | FIN | BUY | 5.04 | 1.51 | 99.8 | 62 | 73.8 | follower | **QUALIFY** - qualified FIN leader starved by FMCG lock |
| ITC | FMCG | BUY | 3.59 | 2.76 | 87.6 | 55 | 59.2 | earlier | **QUALIFY** - early leader, saw preferred fresh-leader signals but not entered |
| DIVISLAB | PHA | BUY | 3.26 | 3.26 | 99.9 | 58 | 70.7 | earlier | **QUALIFY** - persistent PHA #1, closed at day high |
| HDFCLIFE | FIN | BUY | 3.12 | 2.15 | 91.9 | 37 | 57.9 | follower | **QUALIFY** - FIN leader, persistence/met threshold borderline by window |
| BIOCON | PHA | BUY | 2.50 | 1.87 | 97.8 | 50 | 67.1 | follower | **QUALIFY** - persistent PHA top5 |
| CIPLA | PHA | BUY | 2.37 | 1.53 | 95.2 | 42 | 65.4 | follower | **QUALIFY** - PHA cohort persistence |
| GLENMARK | PHA | BUY | 1.62 | 1.45 | 89.5 | 41 | 64.1 | follower | **WATCH** - persistent, smaller move |
| TATACONSUM | FMCG | BUY | 1.86 | 1.86 | 74.4 | 9 | 44.6 | follower | **WATCH** - lower persistence |

## Not included (already handled by original scan)

Entered/alerted names are excluded from forward-test alerts: GODREJCP, VBL, BRITANNIA, NESTLEIND, COLPAL, PFC, TORNTPHARM, etc.

## Expected value of forward-test observation

On 2026-11-25, the rule would have surfaced the exact missed leaders: DABUR, HINDUNILVR, DIVISLAB, ITC, BAJAJFINSV. These were the largest clean opportunities the original scan missed.

## Next step (forward test)

- Keep `SCAN1M_PERSIST_FWD_ALERT=1` in live 1m bot.
- Review `stats.persist_fwd_alerted` in daily audit JSON and EOD report section.
- Compare forward-test alerts against realized move/quality over next sessions before promoting any alert into real entry logic.
- Do **not** auto-promote; this is observation-only until forward-test evidence is reviewed.
