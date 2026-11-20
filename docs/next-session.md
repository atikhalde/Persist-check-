# Next session — where to start

Read `docs/analysis-2026-11-20-1m.md` first; it is the tape this agenda comes from.
Nothing here is a guess: every item names the number that decided it.

## 1. What is live now

| knob | default | what it does |
|---|---|---|
| `PICK_CATCHUP` | `1` | a pick that arrives late re-opens the 40-min leader catch-up for its names (both bots). Anti-chase guards unchanged: the fire's stop must be untouched since, the move from the fire ≤ `SCAN5M_LEADER_MAX_R` / `SCAN1M_LEADER_MAX_R` |
| `TARGET_FALLBACK` | `1` | a name above every pivot gets a measured target at the `MIN_TARGET_R` / `MIN_TARGET_PCT` floor instead of a "no pivot target" refusal. A pivot *ahead but too close* is still a refusal |
| `SCAN1M_EARLY_FROM` | `09:30` | no projected heads-up for the first two candles of the session |
| `AUDIT_BOARD` | `1` | score the **whole board** — all 15 sectors, all names — once per 5m candle, 09:20–15:15, and ship it as a per-name summary in the audit, a `board-*.jsonl` companion and a printable `board-*.html` |
| `AUDIT_BOARD_EVERY_MIN` | `5` | the sweep's cadence on the candle grid (5 / 15 / 30) |
| `AUDIT_BOARD_PERIOD` | `1d` | 5m history the sweep fetches. `7d` costs ~430 MB/day for 190 symbols; `1d` is all `name_rank` reads |

Other defaults unchanged: `MAX_ENTRIES_PER_PICK=0`, `ENTRY_MIN_RANK=40`,
`ENTRY_RANK_BAND=0.75`, `NAME_RANK=combined`, `SECTOR_TOP_N=5`, triple pick on,
`EOD_REPORT=1`, `EOD_AUDIT=1`.

## 2. The four numbers to read off the next audit

All four are in the end-of-day message — no query needed.

1. **`Were the entries leaders?`** — the new section. 20 Nov printed `2 of 6`.
   If it stays near 2/6 for a week, the *entry filter* is the problem, not the
   pick: it is buying whatever is left after the leaders are refused.
2. **`LATE LEADER … older than the window N`** (in diagnostics) vs
   **`entries` in the picked sector**. 20 Nov: `81` refusals and `0` entries in
   BANK. With `PICK_CATCHUP=1` that number should convert into entries; if the
   refusals stay high *and* entries stay zero, the remaining guard that refuses
   them is the one to look at (`chased` / `stop` / `plan` in the same line).
3. **Costs as a share of gross.** 20 Nov: ₹495 of ₹513 = **96%**. Above ~35% the
   book is trading noise. This is a *sizing* signal, not a rule change.
4. **`candles never judged`.** 20 Nov: **7**, three of them inside BANK's pick
   window. If it is not zero, fix the loop before tuning anything: no rule can
   trade a candle that was never judged.

## 3. Open candidates — each needs data, not an opinion

| candidate | what would settle it | why it is still open |
|---|---|---|
| Gap veto on the pick | a day where the persisted sector is a gap **and** the runner-up is not | 20 Nov: refusing AUTO's +3.92pp gap would have picked FMCG, which finished **−0.21%** — worse. A veto fits one tape |
| No entries in the first candle | the same rule replayed over the 11–19 Nov tapes | 11 Nov's best entries were at 09:20 (+4.3R, +11.5R) |
| Kill bot 1's early alerts | `EARLY_*` counts over a week | 6 of 6 cancelled on 18–19 Nov — damning, but two days |
| Hold winners past the EMA9 trail | **MFE per trade** (not recorded yet) | every exit is the trail; TECHM gave back +0.41R → +0.33R. See §4 |
| Re-rank the *whole* sector after a refusal | a day where a leader is refused and a follower entered — 20 Nov is one | today's fix (target fallback) may make it moot; re-measure first |

## 3a. The board is the answer to "what did we never look at"

20 Nov could only be analysed for the 8 sectors some scan happened to touch. The
7 it never looked at were not *evaluated and refused*, they were **absent** — and
absence cannot be counted, so "how many winners did we not see" had one honest
answer: unknown. From the next session the audit holds every name of all 15
sectors, every candle, scored with the same `name_rank` the trim uses.

Read it in this order:

1. **`board-*.html`** (attached, or in `reports/`) — open it and print to PDF.
   Section 2, *Every name, the whole session*, sorted by last move: the column
   that matters is **in 5** (candles it sat in its sector's top five) against
   **taken**. Section 3 is the name × candle heat map per sector, so a leader's
   run and a follower's failure look different at a glance.
2. **`board-*.jsonl`** — one line per candle, `name_rank`'s three legs and the
   move for every name, plus the sector's own score. `pandas.read_json(path,
   lines=True)` reads it directly.
3. **`board.summary` in the audit JSON** — first / peak / trough / last move,
   best score, best position, candles in the top five, entered, net R, and the
   rule that refused it.

Two things to check on the first day it runs:

- **`meta.stale`** — name-candles skipped because the frame had not published
  that candle. A large number means the 190-symbol fetch is running behind the
  clock, not that the board is wrong.
- **`meta.skipped`** — candles with no sector numbers (the scan never judged
  them). Same signal as "candles never judged", but per candle.

The question this is meant to answer, on a day where it happens: *was the winner
in a sector we never scanned, or in a sector we scanned and refused?* Those are
two different bugs and 20 Nov could not tell them apart.

## 4. The one piece of data the ledger is missing

**Maximum favourable excursion (MFE) per trade.** The audit has entry, exit and
hold time, but not how far the trade went in its favour before it closed — so
"the exit is giving back the move" cannot be measured, only felt. Adding
`mfe`/`mae` per position (updated in `_track_positions`, which already sees every
candle) would make the exit question answerable and would let the EMA9 buffer be
tuned on evidence.

## 5. Housekeeping that paid for itself

- The audit JSON is what made 20 Nov analysable. Keep `EOD_AUDIT=1`.
- `reports/` is git-ignored; the files are attachments, not repo content.
- Three measurement bugs were fixed in this session (day % = 0 for bot 3,
  `risk ₹` = 0 everywhere, leadership marks showing the least useful reason).
  If a number looks impossible in a future report, check it against the JSON
  before drawing a conclusion from it — that is how all three were found.
