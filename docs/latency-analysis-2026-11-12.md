# Why entries arrived late — latency audit, 12 Nov 2026

Source: GitHub Actions run history and step timestamps of this repository
(run logs themselves were not readable from the audit sandbox), plus the
scanner code. Times are IST.

## 1. Anatomy of one real alert (PNB, 11 Nov, run 42962938266)

| time | event | after the 10:30 close |
|---|---|---|
| 10:25:00 | signal candle opens — the breakout happens somewhere in here | |
| 10:30:00 | candle closes; the signal is now knowable | 0 s |
| 10:31:05 | external scheduler sends `workflow_dispatch` | +65 s |
| 10:31:09 | runner assigned | +69 s |
| 10:31:25 | checkout, Python setup, `pip install` finished; scan starts | +85 s |
| 10:31:34 | `Signal calculated 10:31:34 IST` — Telegram POST | +94 s |

Of the 94 s, only 9 s was the scanner working. 85 s was the trigger arriving
late and the job cold-starting. And the 94 s sits on top of the candle
itself: R2 (136.75) was touched about 10 minutes after the alert, so the
head start that mattered was inside and right after the 10:25 candle.

## 2. Most candles were not scanned at all

Runs of `NSE 5m live scan` per session day:

| day | `workflow_dispatch` runs | spacing |
|---|---|---|
| 4, 5, 6, 9 Nov | 80–86 | every 5 min (≈ +10–20 s after each close) |
| 10, 11, 12 Nov | 7–8 | **hourly, at xx:31:05 IST** |

Since 10 Nov the external scheduler fires once an hour (09:31 … 15:31 IST).
The scanner only alerts on the candle that has just closed, so on those days
it evaluated the seven candles closing at xx:30 and nothing else — 68 of 75
candles per session could not produce an alert whatever happened in them.

GitHub's own `schedule` trigger is not a substitute here: it produced 2–4
runs a day at arbitrary times (12 Nov: 17:23, 18:00 and 23:35 IST — all after
the market had closed).

## 3. What a close-only design can and cannot reach

Even with a zero-latency pipeline, a close-confirmed 5m alert arrives after
the candle that contains the breakout. Shaving the 94 s helps; it does not
get you in while the move is starting. That needs a look at the candle
before it closes.

## 4. Changes made

1. **Early alert** — 60 s before each close the forming candle is rebuilt
   from 1m bars and scanned with the unchanged engine; an
   `EARLY … (unconfirmed)` alert is sent, then confirmed or cancelled at the
   close. Compared with the PNB timeline above that is roughly 10:29:05
   instead of 10:31:34.
2. **One long-running loop per session in Actions** — no per-candle trigger,
   no per-candle boot; every candle is scanned. Triggers became a watchdog.
3. **Faster confirmed scan** — starts 10 s after the close, re-polls Yahoo
   every 5 s (was 15 s), daily candles fetched once per session instead of
   three batches per candle. Each alert prints its delay after the close.
4. **`early-check`** — replays a session from 1m data and reports how many
   early alerts their own close confirmed or cancelled.

## 5. Not verified in this audit

* Yahoo could not be reached from the audit sandbox. The early pass and the
  loop were exercised with unit tests and with a synthetic 1m feed through
  the real stock engine, not with live NSE data. Run `early-check` on a few
  recent sessions and a `dry_run` workflow during market hours before
  relying on it.
* How quickly Yahoo publishes 1m and 5m NSE bars is unknown; the delay
  printed on each alert will show it.
