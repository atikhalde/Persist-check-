# 16 Nov 2026 — the three live defects: cause, fix, and how to prove it

Reported from the live feed:

1. **Both scanners fired the same alert at the same second.** The 1-minute scan
   exists to deliver the signal up to four minutes before the 5m scan; an
   identical simultaneous alert is the one outcome that makes it pointless.
2. **The scan missed the sector leader** and sent late alerts for the followers
   — "many FMCG alerts but ITC missed", the same in pharma.
3. **A TARGET HIT alert belonged to another day**:
   `🎯 TARGET HIT — BUY PNB · T1 136.75 (+0.75%, +1.3R) · T2 141.02 (+3.90%, +6.7R) · 5m 09:15 · entry 135.73`
   — PNB was signalled on **11 Nov**; the alert fired on **16 Nov**.

Each has a single, code-level cause. None of them is a rule difference between
the two scans, and none of them needed a rule change to fix.

---

## 1 + 2. One root cause: the projection used the *previous* candle's pick

`scan1m.pick_for(bar, projection=True)` — the pick used to decide *which names*
a forming candle may alert — returned the pick of `bar − 5m`:

```python
allowed = [(bar - timedelta(minutes=BAR_MINUTES)).strftime("%Y-%m-%d %H:%M")]
```

That is the pick the 5m scan decided at the **last close**. But the 5m scan
alerts an entry on the candle whose close establishes the pick
(`pipeline.run_once` decides `md` from `last` and then scans `md.pick_sector`
for signals **on that same bar**). So the two scans are structurally
misaligned on exactly one kind of candle: **the candle where the market flips
to a new sector.**

What that does, in the order it hurts:

| on the flip candle (say 10:00, FMCG becoming the pick) | bot 1 (5m) | bot 3 (1m, before this fix) |
|---|---|---|
| 10:01–10:04 | nothing (waits for the close) | scans the **old** sector. FMCG is not on the list, so ITC — the leader that moves first when FMCG flips — is never even looked at |
| 10:05 close | pick = FMCG → ITC's entry alert | pick = FMCG (confirm pass) → the **same** ITC entry alert, same second |
| 10:10, 10:15 … | follows the sector | now scans FMCG, and the *followers* (which fire on the later candles — persistent NORMAL/base conditions) get genuine `⚡ EARLY` alerts |

That is defect 1 and defect 2 in one mechanism: the **leader** is the name that
fires on the flip candle (no lead, or missed), the **followers** are the names
that fire afterwards (early — so the feed reads "late leader, early followers").

**Fix — `scan1m.projected_market`.** The pick is now projected the same way a
stock is: Nifty, VIX and every sector index are rebuilt as
`[completed 5m bars] + [forming candle from 1m prints]` and the **unchanged**
`run_market` runs on them (`_prepare_index`, `sanitize_index_5m`,
`_complementary_pick`, `_follow_leaders`, `SECTOR_ORDER` — all of them, no
fork). `pick_for(projection=True)` now prefers, in order:

1. a pick the 5m scan has already written for `bar` itself;
2. **the forming candle's own verdict** (`projected_market`);
3. the previous candle's pick — the old behaviour, used only when (2) cannot be
   computed (no 1m prints for the indices, a lagging feed, sanitation moving
   the bar, or `SCAN1M_MARKET_SOURCE=ledger`).

Consequences worth knowing:

* a **projected** pick never sends a MARKET/SECTOR message — that statement
  belongs to a finished candle, so it waits for the close, exactly as on bot 1;
* `SCAN1M_MARKET_SOURCE=ledger` can never project (the 5m ledger holds closed
  candles only); `own`/`auto` is what buys the lead. The workflow already runs
  `own`;
* the projection is recomputed every minute of the candle, so a pick that flips
  at minute 3 alerts at minute 3 (2 minutes early), not minute 1.

### Measured: the flip candle is real, and it is the one bot 3 could not see

`tests/test_projection_lead.py::RealMarketEngineTests` drives the **real**
`run_market` (no mock) over a synthetic session where one sector index runs up
and the rest drift down. Sweeping the candles from 10:55:

```
10:55  FORMING candle says AUTO 1 (SCAN LONG) | last CLOSED candle said WAIT 0
       → bot 1 learns this at 11:00; bot 3 can alert from 10:56
```

One flip in the window, and it is exactly the reported shape: the 5m scan has
*nothing* to scan on the candle the pick appears on (it still holds WAIT), while
the forming candle already says `AUTO, long`. Before this change bot 3 also held
WAIT — it scanned nothing for four minutes and then echoed bot 1's entry at
11:00. The leader of AUTO fires on the 10:55–11:00 candle; the followers fire on
the candles after it. Same seed, same result on every run.

## 2b. The second half of the miss: a partial candle's volume

`data.provisional_5m` sums the 1m prints seen so far. At minute 1 that is about
a fifth of the candle, and the engine reads volume in three places:

* `rel_vol >= 0.2` gates `entry_buy_today` / `entry_sell_today`;
* `vol_sc` gives 15 score points only when `rel_vol > 1.5` (10 otherwise);
* `entry_buy = agg_buy and total > 60`.

So a name whose completed candle scores 65 (with `rel_vol > 1.5`) scored **60**
at minute 1 — not an entry — and only crossed 60 once the candle was nearly
over. The projection then appeared at minute 3–4, i.e. 1–2 minutes before bot 1,
or not at all when the score sat exactly on the line.

**Fix — `data.project_volume`.** The forming bar's volume is finished from its
own prints (`volume so far × 5 / minutes covered`). The factor is exactly 1.0
at the close, so `closed_frame` — the frame *both* scanners confirm at the
close — is untouched: this can only make a projection appear earlier, never
change what the close confirms. It is applied by `scan1m.projected_frame` only;
`pipeline.run_early` (bot 1's own 60-second heads-up) is unchanged.

## 3. A trade is intraday — the stale TARGET HIT

`pipeline._track_positions` replayed "every candle since the last check" *within
the current session*:

```python
replay = [ts for ts in grid.index
          if (cursor is None or ts > cursor) and session_date(to_ist(ts)) == session]
```

and `state.reset_if_new_day` deliberately carried positions across days
("trail until an exit, even across sessions"). The two together are the bug: a
position opened on 11 Nov is still open on 16 Nov, its `hit_targets` is stale,
and the **16th's** 09:15 candle is checked against the **11th's** entry. A gap
up through both levels in one candle produces exactly the reported line — two
targets of an eleven-day-old trade "hit" at 09:15, an entry price from a
different week.

**Fix — the position dies with its session.**

* `state.position_session(pos)` — the session a trade belongs to (its
  `entry_bar`, or its stored `session`);
* `state.reset_if_new_day` keeps only positions of the new session;
* `pipeline._track_positions` **also** drops, before any fetch, a position whose
  session is not today (the live ledger had already been rolled over to the
  16th, so the rollover alone cannot see it). It logs
  `dropping PNB: trade from 2026-11-11 is not today's (2026-11-16)` and sends
  nothing. Both `_note_reject`-free and message-free by design: an alert about
  a trade that is not today's is noise, not news;
* new positions store `"session"` (both scans).

A restart **inside** the same session still keeps an open trade open — that is
the only reason the carry-over existed.

---

## Proving it tomorrow, from the log

The runner's log is the audit trail. Per alert, these lines are enough:

```
grep -E "1m EARLY|1m CONFIRMED|ALERT |SKIP historical|1m no projection|1m: last .* fire|dropping " live.log
```

What to look for:

| check | pass condition |
|---|---|
| lead | every `1m EARLY … (minute k of 4, n min before the close)` has `n ≥ 2`; `minute 1` is the ideal |
| not a duplicate | no `1m CONFIRMED` whose text repeats bot 1's `ALERT` **without** a preceding `1m EARLY` for the same name and candle |
| leader first | on the candle the pick flips (the `market … pick=FMCG` line), the first alert of that sector is on its **strongest** name |
| pick source | `1m EARLY … (projected pick, …)` — `ledger pick` means the projection could not be built and the lead was lost |
| missed names | `SKIP historical <NAME> fires=ENTRY BUY@09:20` = the signal fired **before** its sector was picked (no catch-up rule); `no signal on the forming candle` = the engine did not fire on this candle (see the score arithmetic above) |
| stale trades | no `TARGET HIT` / `SL HIT` line whose `5m hh:mm` is on a different date than the entry's |

The 15:33 summary now carries two extra lines so the day is self-documenting:

```
Average lead 3.2 min before the 5m close (earliest 4 min)
Checked, not projected: no signal on the forming candle 96 · no valid EMA9 stop yet 7 · 1m feed missing/stale 2
Signals not alerted: 1 fired before their sector was picked (ITC 09:20)
```

That last line is the one that answers "was the leader missed, or did it never
fire?" — and it is the only remaining way a leader can be absent, because the
`earlier` reject is deliberate: a signal calculated from a candle the scan was
not watching for that sector is not an entry alert (no catch-up trades). If the
log shows `earlier ITC@09:20` for a leader you wanted, that is a **policy**
question, not a bug — see the open question at the end of this note.

## What this did not change

* `stock.py`, `market.py`, `trade.py`, `telegram.send()`, `live-scan.yml`: no
  edits. Bot 1 and bot 2 behave exactly as they did.
* The confirmed stream on bot 3 is still `pipeline.format_stock`, character for
  character, and still uses the *closed* candle's pick — the `projection=False`
  path is untouched.
* Silence on a failed projection is still the default
  (`SCAN1M_CANCEL_MESSAGE=1` to report it).

## The alert-by-alert analysis you asked for

I could not do it from this workspace: there is no alert log, ledger or state
file in the repository (`data/` holds only `oi_history.csv`, `state/` is empty),
and this sandbox has no route to Yahoo/Google, so the day's candles cannot be
re-fetched here either. What is needed, either way:

* the day's bot-1 **and** bot-3 messages (Telegram export, or the Actions log
  lines above) — timestamps to the second;
* for each alert: the name, the 5m bar, the sector pick at that candle, and the
  lead (or its absence).

With those, each alert gets the same three questions — (i) which candle's pick
was in force, (ii) did the name fire on that candle or an earlier one, (iii) how
many minutes separated bot 3 from bot 1 — and the result is a table like the
one above, per alert, with the price move added from the runner (the bot has
the 5m bars; this sandbox does not).

## Open question (needs your call)

A leader whose exception fired **before** its sector became the pick is still
silent by design (`stats.rejected.earlier`). Nothing alerts it, on either
scanner, because it is not a *new* signal on the candle being scanned.

Options:

1. **Leave it** — "no catch-up trades"; the leader is missed but the stream
   stays clean. (Current behaviour, and why 2b/1 matter: the projected pick
   makes the flip candle itself scannable, so most leaders are caught.)
2. **Leader catch-up alert** on bot 3 only: if a name of the *newly picked*
   sector fired earlier in the same session and the trade is still valid (stop
   not touched since, price still on the right side of the pick's side), send a
   clearly labelled `⏰ LATE LEADER — fired 09:20, sector picked 10:35, entry now`
   on bot 3, once per name per session. Bot 1 stays silent.
3. Same as 2, but only within a window (`SCAN1M_LEADER_MAX_MINUTES`, e.g. 30).

My recommendation is 3 (bounded, bot-3 only, labelled), if the log shows leaders
firing before their sector was picked. It needs a rule decision — what counts as
"still valid" — so it is not in this change.
