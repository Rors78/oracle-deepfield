# PRE-REGISTERED PROTOCOL — level-structural events × prop geometry, 4h bars,
# close-entry vs retest-entry (Phase 1b)
Registered and COMMITTED before any fetch or replay; the commit hash is the
timestamp proof. Nothing below may be tuned after seeing results.

## Question under test
The daily-bar study's mechanism finding: entry at the confirming close sits
~a full stop-distance above the structure, and the market revisits the level
before any 3R path (median MAE ≥ 1R in all six feeds measured). Two half/full
attacks on that variable:
- 4h bars shrink the confirmation-close-to-level gap (half attack).
- Retest entry places the entry AT the level (full attack).
If retest entry does not fix MAE, the registered finding becomes: these pairs
do not respect structure at 5x-viable stop widths, and the prop wallet is a
hand-discretion account with the module as sizing calculator only.

## Data (fixed)
Public Kraken 4h OHLC (interval=240, ~720 bars ≈ 120 days), DEEPFIELD's
28-pair universe (config.PAIRS ws names), fetched once at ≥0.35s pacing,
cached; forming last bar dropped. No account API touched, no writes.
Fire window: every evaluable 4h close from the first bar with ≥25 bars of
history through 2026-08-15; actual coverage reported. Split: fire (trigger)
timestamp < 2026-07-21 → train, else validate.

## Structure & events on 4h bars (fixed; long only)
SWING LOW: 3-bar pivot (low[i] < low[i−1] and low[i] < low[i+1]), CONFIRMED
at close of bar i+1. Level = low[i]. At each 4h close T:
**A — reclaim.** L = level of most recent confirmed swing low with ≥1 close
below L in bars T−10..T−1; fire when close[T] > L and close[T−1] ≤ L.
**B — higher-low.** New swing low S2 confirmed exactly at T with S2 > level
of the previous confirmed swing low. Level = S2.
**C — failed breakdown.** L20 = min(low) over bars T−21..T−2; fire when
close[T−1] < L20 and close[T] > L20. Level = L20.

## Entry modes (fixed) — the actual test
**Mode 1 (close-entry).** Entry = close[T]. Stops as in the daily study:
A: L − 0.5×ATR14(4h at T); B: S2 exactly; C: min(low[T−1], low[T]).
**Mode 2 (retest-entry).** Buy limit AT the level (A: L, B: S2, C: L20),
valid bars T+1..T+12, canceled if unfilled (fill rate reported; unfilled
triggers are not outcomes). Fill on the first bar k with low[k] ≤ level;
fill price = level (standard limit convention; symmetric across modes).
Stop = level − 0.5×ATR14(4h at trigger T), uniform for all three events.
On the FILL BAR itself: low[k] ≤ stop → STOP (conservative; TP is never
awarded on the fill bar). Walk continues k+1 onward.

## Geometry & resolution (fixed)
TP = entry + R×(entry−stop), R ∈ {2.5, 3.2, 4.0}. Walk 4h bars (mode 1 from
T+1; mode 2 from fill bar per above), cap 84 bars (= 14 days) from entry.
low ≤ stop → STOP (stop-first same bar); high ≥ TP → TP; exhausted → OPEN
marked to last close in R. MAE = max (entry−low)/(entry−stop) over the walk.
Rejections (counted): stop ≥ entry; stop distance > 8.4% of entry.

## Report (fixed)
Per event × mode × split × R cell: n (mode 2: triggers, fills, fill rate),
effective-N (14-day overlap, any pair/type), hit rate vs break-even 1/(1+R),
expectancy resolved-only AND incl. opens (censoring stated), MAE med/p75/max,
median stop distance %, fires per day (mode 2: fills/day), median
time-to-resolution. THE MECHANISM COLUMN: entry-to-structure gap, reported
as gap_R = (entry − level)/(entry − stop), med/p75, mode 1 vs mode 2.
BTC close-to-close per split. Write-up leads with C.

## Acceptance (unchanged, per event × mode at 3.2R validate)
1. MAE median < 1R  2. validate expectancy (resolved) > 0  3. ≤1 fire/day
(mode 2: fills/day). Any passing cell → Phase 2 build as previously specced.
All fail → STOP AND REPORT; the operator decides what's next.
