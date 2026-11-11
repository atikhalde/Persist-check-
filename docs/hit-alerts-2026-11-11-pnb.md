# One-line hit alerts — replay on PNB, 11 Nov 2026

Scope: alerting only. Targets, the stop rule, ATR sizing and exit behaviour are
unchanged. Each completed 5m candle is now checked for three events, and each event
is sent as **one line**:

| event | trigger | position |
|---|---|---|
| 🎯 `TARGET HIT` | candle trades through a pivot target (high/low touch) | stays open, each `T1/T2/T3` fires once |
| 🛑 `SL HIT` | candle low (BUY) / high (SELL) reaches the initial SL | closes |
| 🔔 `EMA9 EXIT` | candle **closes** beyond EMA9 (unchanged rule) | closes |

## Replay of the real session

The actual Yahoo 5m bars for `PNB.NS` on 11 Nov 2026 (64 candles, 49 after the
10:25 entry) were pushed through the production `_track_positions()` one candle at a
time, with the live trade record (entry 135.73, SL 134.94, T1 = R2 136.75, T2 = R3
141.02).

Exactly two messages would have been delivered:

```
🎯 TARGET HIT — BUY  PNB · T1 R2 136.75 (+0.75%, +1.3R) · 5m 10:35 · entry 135.73
🔔 EMA9 EXIT — BUY  PNB · close 138.06 below EMA9 138.35 (+1.72%, +2.9R) · 5m 12:10 · entry 135.73
```

- **SL never fired** — the lowest low after entry was 135.54, above 134.94.
- **T2 = R3 141.02 never fired** — the day's high was 139.00, so no alert (correct:
  before this change the miss was silent, which is what made the 141.02 level look
  like it had been "hit").
- The 10:30 candle traded to 136.19, below the target, and produced nothing.

## Bug this replay caught

The first replay reported `SL HIT … 5m 09:15`, a candle from *before* the entry. The
resume cursor (last processed candle) was parsed with a string test that is false for
`+05:30` offsets, so the window silently reset to the start of the session. Fixed by
parsing the stored ISO timestamp with `pd.Timestamp`; `test_entry_bar_never_produces_hit_alerts`
now fails if the cursor regresses.

## Re-driving the session produces nothing (verified on the same bars)

| pass | state | new alerts |
|---|---|---|
| 1 | real session from the 10:25 entry | **2** (T1 10:35, EMA9 exit 12:10) |
| 2 | same state again, nothing open | 0 |
| 3 | the original position record re-inserted, cursor rewound to 10:25 | 0 — reconciled silently |
| 4 | `hit_targets` wiped **and** the EXIT ledger entry deleted by hand | 1 (only the wiped level re-fires) |

Pass 3 is the case that used to be dangerous: a position record whose cursor points back
at the entry candle is now suppressed by the ledger instead of replaying the whole
session. Pass 4 is the same exposure the entry alert has — deleting a ledger record by
hand is the only way to get a repeat.

## Behaviour notes

- Every candle since the last processed one is replayed, so a slow or skipped Actions
  run still reports the hit on the candle where it happened — the line is stamped with
  **that candle's** time, not the delivery time.
- A failed Telegram POST stops the replay at that candle; the next cycle retries it and
  never re-sends what already went out.
- Alerts are notifications, not orders: the bot still does not execute stops or targets.
