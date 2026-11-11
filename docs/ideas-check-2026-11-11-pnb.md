# Running every idea against one real signal — PNB, 11 Nov 2026 10:25

Data: **real Yahoo 5m/daily bars for PNB.NS**, pulled for this run (`range=1d&interval=5m`,
`range=2mo&interval=1d`). Nothing is reconstructed.

| item | value |
|---|---|
| signal bar (10:25–10:30) | O 135.27 · H 135.75 · **L 135.23** · C **135.73** |
| EMA9 @ 10:25 (implied by the sent SL 134.94) | 134.9670 |
| ATR5 (Wilder 14, 5m) | 0.5998 = **0.44 % of price** |
| pivot basis | 09 Nov 2026 (10 Nov = NSE holiday) H 132.48 / L 125.92 / C 132.17 → **range 4.96 %** |
| basis vs 20-session median | 2.26 % ⇒ **2.19×** |
| levels above entry | R2 136.75 (+0.75 %) · R3 141.02 (+3.90 %) |

## What actually happened after the signal

| | |
|---|---|
| T1 = R2 136.75 | **hit at 10:35** (≈5–10 min after the alert) |
| T2 = R3 141.02 | **never** — session high 139.00 |
| max favourable excursion | **139.00 at 12:05 = +2.41 %** |
| lowest low after entry | 135.54 — *no stop of any variant was ever tested* |
| EMA9 trail exit | **12:10 @ 138.06 = +1.72 % (+2.95R)** |

## Idea-by-idea, on this signal

| variant | SL | risk | % of price | T1 | T2 | outcome |
|---|---|---|---|---|---|---|
| **A** current HEAD (EMA9 − 0.02 %) | 134.94 | 0.79 | 0.58 % | 1.3R / +0.75 % | 6.7R / +3.90 % ⚠ | T1 hit, T2 missed |
| **B** fixed band 0.15–1.00 % + structure | 134.94 | 0.79 | 0.58 % | 1.3R / +0.75 % | 6.7R / +3.90 % ⚠ | identical |
| **C** ATR `0.75 × ATR5` | 135.22 | 0.51 | 0.38 % | 2.0R / +0.75 % | 10.4R / +3.90 % ⚠ | T1 hit, T2 missed |
| **D** ATR `1.00 × ATR5` | 135.13 | 0.60 | 0.44 % | 1.7R / +0.75 % | 8.8R / +3.90 % ⚠ | T1 hit, T2 missed |

**Variant C is the proof I was looking for.** `0.75 × ATR5` = 0.33 % → stop **135.28**, which
sits *inside* the signal candle (low 135.23): the trade would have been dead before the alert
was sent. It only becomes valid after the structural bound pulls it to 135.22. `1.00 × ATR5`
→ 135.13 clears the candle on its own. So: **0.75× is too tight, 1.0× is right** — measured,
not argued.

**Variant B changes nothing here** (risk 0.79 is already inside 0.15–1.00 % and the stop is
already outside the candle). It only earns its keep on the pathological bars from last week's
review (flat tape risk 0.03, spike risk 2.90).

## What each shipped idea did on this signal

| idea | result |
|---|---|
| basis line | prints `09 Nov H 132.48 L 125.92 C 132.17 range 4.96 % (2.2× median)` — fully auditable |
| `+%` next to R | T1 `(1.3R, +0.75 %)` unflagged, T2 `(6.7R, +3.90 % ⚠)` flagged → **matches reality** (MFE +2.41 %) |
| `⚠` on R > 4 **or** \|%\| > 2.0 | fires on T2 only — the one that was actually unreachable |
| wide-basis flag at 2.5× median | **did not fire** (2.19×) — my threshold is a guess, see below |
| compressed/special-session flag | not triggered: Yahoo publishes no Muhurat bar for PNB (sessions present: 4, 5, 6, 9, 11 Nov) |
| stale-basis guard, feed reconciliation | no-op here (5m and daily agree, gap 2 days over the holiday) |
| hit logging | would record: T1 10:35 · T2 never · MFE 12:05 +2.41 % (4.14R) · exit 12:10 +1.72 % (2.95R) |

## The uncomfortable finding

On this trade **the stop was never within 0.60 points of being touched** — every stop variant
survived, so the stop changed only the labels, never the money. What decided the result was
the **exit**: the EMA9 trail left 0.94 points (0.71R) on the table versus the 139.00 high, and
captured 71 % of the move.

So my earlier priority list was half wrong: for this system the **trail** is the bigger lever
than the stop width, and the `⚠`/"far target" thresholds should be calibrated from realised
MFE, not from a guess. Concretely, this one signal says: a 5m entry here travels **+2.4 %** at
best — which makes R3 (+3.90 %) correctly "far" and R2 (+0.75 %) correctly "near".

## Revised recommendation

1. **Structural stop bound + 0.15–1.00 % band** (B) — inert on good signals, fixes the
   pathological ones, 5 lines. Still worth shipping.
2. **`1.00 × ATR5` clamped, not 0.75×** — measured above; ship it after (1) so R labels have a stable meaning.
3. **Keep `+%` as the primary distance** and lower the wide-basis gate from 2.5× to **2.0×**
   (it stayed silent at 2.19× on the very day it was built for), or drop the gate and rely on
   the always-printed multiple.
4. **Log hits/MFE/trail giveback per position** — the only way to set these thresholds from
   data. This signal alone shows the trail, not the stop, is where the money is decided.
