# Opening range: 30 minutes or 15? — how to measure it

The 18 Nov question, in one line: **KALYANKJIL fired at 09:20, DIXON and TITAN at 09:25, and the
sector that made them relevant was only picked at 09:45.** Seven of the twelve names of that
sector were inside the opening range. Would a shorter range have caught them, and what would it
have cost?

The arithmetic, before any measuring — the gate is `mom = bar time − 09:15 >= OR_MINUTES`, and a
fire is eligible only if its own candle is one where the pick was live:

| opening range | first bar a pick is possible on | judged at | fires it can rescue (18 Nov) |
|---|---|---|---|
| **30 min** (live) | 09:45 | 09:50 | LGEINDIA 10:15, BATAINDIA 10:50 |
| **15 min** | 09:30 | 09:35 | WHIRLPOOL 09:40 — **not** the 09:20/09:25 cluster |
| **10 min** | 09:25 | 09:30 | CROMPTON, DIXON, HAVELLS, TITAN, VOLTAS @09:25 |
| **5 min** | 09:20 | 09:25 | KALYANKJIL @09:20 |

So "15 minutes would have caught DIXON and TITAN" is **false** — it needs 10, and KALYANKJIL needs
5. Run `--or 30,15,10,5` to see the whole ladder; each step down is more whipsaw, and that is what
the table is for.

This is the measuring tool for that question. It is **not** a knob and not a rule change: nothing in
the live loops imports it, it sends no Telegram message, it never touches the live ledger, and
`market.OR_MINUTES` is patched inside the comparison and restored on the way out.

## Run it

GitHub → **Actions → OR comparison (measurement) → Run workflow**

| input | meaning |
|---|---|
| `or` | opening ranges to compare, minutes: `30,15` (or `30,20,15`) |
| `dates` | sessions to replay, `2026-11-18,2026-11-17` — empty means "the last N weekdays" |
| `days` | how many weekdays to replay when `dates` is empty (default 5) |

The report appears in the run's **Summary** page and as a downloadable artifact. The same thing
locally, wherever Yahoo is reachable:

```
python -m nse_scanner or-compare --or 30,15 --dates 2026-11-18
python -m nse_scanner or-compare --or 30,15 --days 5 --end 2026-11-18
```

## What it reports

```
### 2026-11-18
| | OR 30 min | OR 15 min |
| First pick | 09:45 CDUR short | 09:30 CDUR short |
| Sector held | CDUR short | CDUR short |
| WAIT bars after 09:45 | 3 | 0 |
| Entries | 1 — LGEINDIA@10:15 (+0.4R) | 3 — DIXON@09:30 (+0.6R), TITAN@09:30 … |
| Net R at the close | +0.4R | +1.2R |
| Stopped out | 0 | 1 |
```

and, across every replayed session: entries, net R, average R, winners, stops — per opening range.

## How it decides

* **same engine** — `market.run_market` gives the direction and the pick bar by bar, once per
  opening-range length; `stock.scan_stock` gives the fires, exactly as the scanner calls them;
* **an entry exists only if its fire happened on a candle whose pick was live** — that is the rule
  the shorter range relaxes, and it is why the two columns differ;
* **one entry per name per session**, at the fire candle's close, with **the fire's own EMA9 stop**;
* **the outcome** is −1R if the stop is touched first, otherwise the R at the `15:25` close; a bar
  that touches both the stop and a better price is scored as the stop.

## What it does not measure

The entry cap (`MAX_ENTRIES_PER_PICK`), the OI gate, the 1m projections, the late-leader catch-ups,
the Telegram side and the 5m alert lag are **not** replayed. Two sessions can therefore differ from
what the bots actually sent. This tool compares **the two opening ranges on one tape**; it is not a
P&L statement for either bot.

It also cannot answer "would the trade have been taken at the fire price in real life": the fire is
only knowable when its candle closes, and the alert goes out seconds later.

## Reading the result honestly

A shorter opening range moves *more* fires into eligibility and moves each one *earlier* — both the
wins and the whipsaws. The number that decides it is **net R together with stops**, over as many
sessions as Yahoo will serve (60 days of 5m data), not a single day. One day is an anecdote; five
days is a hint; ten is worth acting on.

## Tests

`tests/test_or_compare.py` covers the patch/restore, the −1R-first scoring, the live-pick rule, the
table and the totals — all with synthetic bars, no network.
