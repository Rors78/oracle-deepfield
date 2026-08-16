# PRE-REGISTERED PROTOCOL — Study 1 (structural SHORT inversion) and
# Study 2 (wide-stop 1x longs). Phase 1c, run in this order.
Registered and COMMITTED before any replay; commit hash is the timestamp.
Data: the cached 4h series from Phase 1b (df_4h_cache.json, fetched 2026-08-15,
coverage 2026-04-18 → 08-15) and the stored daily candles (Study 2 daily rerun).
No new fetches, no account API, no writes. Split at 2026-07-21 throughout;
resolve/walk/MAE/effective-N rules identical to Phase 1b (dcbaf28) except
where explicitly mirrored below.

## STUDY 1 — inversion: short the revisit
Phase 1b's finding was that a revisit of reclaimed structure usually violates
it, resolves in ~1 bar, and runs to multiple R past the level. This study bets
WITH that finding instead of against it. The prop account allows shorts;
nothing in the fleet does — these are prop-only candidates.

Triggers: A/B/C exactly as in Phase 1b (unchanged detection, long-shaped
confirmations). Levels: A = reclaimed swing low L; B = new swing low S2;
C = the 20-bar low L20.

**Retest-short (per trigger type, cells A-s/B-s/C-s).** Sell limit AT the
level, valid bars T+1..T+12, fill on first bar k with low[k] ≤ level, fill
price = level. Stop = level + 0.5×ATR14(4h at T), ABOVE the level. On the
fill bar: high[k] ≥ stop → STOP (conservative; TP never on the fill bar).
Walk k+1 onward, cap 84 bars from fill: high ≥ stop → STOP (stop-first same
bar); low ≤ TP → TP; exhausted → OPEN marked to last close.
TP = entry − R×(stop − entry), R ∈ {2.5, 3.2, 4.0}.
MAE = max (high − entry)/(stop − entry).

**Event D — breach-close short (pooled A/B/C triggers).** Same retest fill
event; if the retest bar k CLOSES below the level (close[k] < level), enter
short at close[k] (no limit). Stop = level + 0.5×ATR14(4h at T). Walk k+1
onward, same rules. Per-source-trigger counts reported; graded as one cell.

Rejections (counted): stop ≤ entry (degenerate); stop distance
(stop − entry)/entry > 8.4% (Study 1 stays in the 5x prop envelope).
Fee gate: shorts pay the same 0.04%/side + 0.033%/day funding on notional —
gain = (entry − tp) − fee·entry − fee·tp − fund·entry;
loss = (stop − entry) + fee·entry + fee·stop + fund·entry; pass iff ≥ 3.0.

Acceptance (per cell, validate 3.2R): MAE median < 1R AND expectancy
(resolved) > 0 AND ≤ 1 fill/day. Any pass → Phase 2 build with a short-side
path added to the sizing engine.

## STUDY 2 — wide-stop 1x longs
The level-holds thesis was only ever tested inside the 8.4% 5x-liq cap.
At 1x the cap is gone; a wide structural stop may sit where the wicks
cannot reach. Rerun A/B/C LONG:
- 4h data, BOTH modes (close-entry and retest-entry), Phase 1b mechanics
  unchanged, with the 8.4% rejection REMOVED (degenerate stops still
  rejected) — this re-admits every previously capped/rejected event.
- Daily data, mode 1 (as Phase 1a), 8.4% rejection removed — re-admitting
  the 51 daily rejections.
Sizing context at 1x: $40 risk → notional = 40/stop_fraction; report median
and p90 notional per cell (a sanity column, not a criterion — $5k wallet).
Report the stop-width distribution med/p75/max per cell. Fee gate unchanged
(funding on the full notional; wide stops make the 3.2R gross gate easier,
shown per cell). Acceptance identical (MAE < 1R, validate exp > 0 at 3.2R,
≤1 fire/day per cell).

## Reporting (both studies)
Same tables as Phase 1b: n (triggers/fills/fill-rate where applicable),
effective-N, hit vs break-even, expectancy resolved AND incl. opens
(censoring stated), MAE med/p75/max, stop-width med, fires-per-day, t_med,
BTC regime per split. Registered in docs/LATER.md either way. If every cell
in both studies fails: the campaign's final landing is the operator's
pre-declared one — hand-discretion wallet, module as sizing calculator —
and no further feed candidates are proposed from existing data.
