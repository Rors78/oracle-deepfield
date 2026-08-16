# PRE-REGISTERED PROTOCOL — level-structural events × prop geometry (Phase 1)
Registered and COMMITTED 2026-08-15 before any replay. The commit hash is the
timestamp proof. Nothing below may be tuned after seeing results. Discipline
identical to the two studies in docs/LATER.md's null registry (req−1, ORACLE).

## Question
Do level-structural entries — where the stop sits under structure rather than
at an ATR multiple — produce sub-1R-MAE, positive-expectancy 3R paths? These
are the first candidate events in the fleet DESIGNED for reversal shape.

## Data (fixed)
Stored closed daily candles from the live DEEPFIELD DB (read-only; the same
Kraken series the public API serves), DEEPFIELD's 28-pair universe
(config.PAIRS ws names). Fire window 2026-05-17 → 2026-08-15 (90 days);
history back to 2024-07 used for swing/ATR context. No network, no writes.

## Swing detection (fixed)
3-bar pivot on daily bars: bar i is a SWING LOW iff low[i] < low[i−1] and
low[i] < low[i+1]. A swing low is CONFIRMED (becomes known) only at the close
of bar i+1 — no lookahead anywhere. Level of a swing low = low[i].

## Event definitions (fixed; all long; evaluated at each daily close T)
**A — Level reclaim.** Let L = level of the most recent confirmed swing low
such that at least one close in the last 10 bars (T−10..T−1) was below L
(the breach). Fire when close[T] > L and close[T−1] ≤ L (the reclaim
crossing). Entry = close[T]. Stop = L − 0.5×ATR14 (ATR in price units, at T).

**B — Higher-low confirmation.** A new swing low S2 is confirmed at T (pivot
at T−1) and S2 > level of the previously confirmed swing low S1. Entry =
close[T]. Stop = S2 exactly (a touch of the new swing low = out).

**C — Failed breakdown.** Let L20 = min(low) over bars T−21..T−2 (the 20-day
low before the breakdown bar). Fire when close[T−1] < L20 and close[T] > L20.
Entry = close[T]. Stop = min(low[T−1], low[T]) (the breakdown wick low).

Event types are graded SEPARATELY (three candidate feeds, not one). The same
pair-day may appear in more than one type. Rejections (counted, reported):
stop distance (entry−stop)/entry > 8.4% (5x liq viability — ATR's only role),
or stop ≥ entry (degenerate).

## Geometry & resolution (fixed, identical to prior studies)
TP = entry + R×(entry−stop), R ∈ {2.5, 3.2, 4.0} (no SL grid — stops are
structural, that is the whole thesis). Walk T+1..T+14 daily bars: low ≤ stop
→ STOP (even if TP same bar); high ≥ TP → TP; exhausted → OPEN marked to last
close in R. MAE = max (entry−low)/(entry−stop) over the walk.

## Split & report (fixed)
train: fire < 2026-07-21; validate: ≥ 07-21. Per type, per split, per R cell:
n, rejected-count, effective-N (14-day overlap clusters across ANY pair and
type), hit rate vs break-even (1/(1+R)), expectancy resolved-only AND incl.
marked opens (censoring stated), MAE med/p75/max, median stop distance %,
median time-to-resolution, fire rate per day (n/window-days). BTC
close-to-close per split as the regime column.

## Acceptance (pre-registered; ALL three required, per event type)
1. median MAE < 1R (validate split; train shown for context)
2. validate expectancy > 0 at 3.2R (resolved-only is the binding number)
3. fire rate ≤ 1/day (validate window, per type)
A type that passes all three advances to Phase 2 (wire as prop ticket
source). Anything else is registered NULL in docs/LATER.md. If all three
types fail on daily bars: STOP AND REPORT — the 4h-bar study is the
operator's decision, not an automatic next step.
