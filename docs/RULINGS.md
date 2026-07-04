# Architect Rulings — authoritative overrides & clarifications

These supersede any looser prose in `SPEC.md`. M2 tests and the M3 parity gate
build against **these** definitions. Source: architect relay, 2026-07-03.

## Environment — drive & directory (ratified)
The Dell has a single 916G ext4 root disk (`sda2` at `/`); there is no separate
1 TB data mount. **That root disk IS the "1 TB drive"** — SPEC §3's `<mount>`
resolves to home. Project island = **`/home/golden/oracle-deepfield/`** (Option 1:
no clash with `/home/golden/Oracle`, no spaces, matches repo kebab-case). Isolation
is a file-boundary rule, not a spindle rule.

## B1 — v4.4 source (VERIFIED PRESENT)
Parity reference = `/home/golden/Downloads/oracle_dca.py`, **md5
`f9bf480f9c17839073c4014de401194b`, 56,682 bytes — MATCH confirmed 2026-07-03.**
**M3 stays as written** — real parity vs actual v4.4 output on identical data,
every diff traced to an F-item. No re-basing. (Vendor a pinned copy under
`docs/reference/` at M2.)

## Signal definitions (authoritative — port as-is)
- **sig2 · W-RSI<40 Turning Up:** `WRSI[-1] < 40 AND WRSI[-1] > WRSI[-4]`
  (turning up vs **three bars back**, not one).
- **sig3 · W-MACD Hist Crossup:** a **state, not an event** —
  `hist[-1] > 0 AND any(h < 0 for h in hist[-8:-1])`. Stays lit up to 7 weeks
  post-cross. Port as-is. (Fresh-cross variant → LATER.md, operator-gated.)
- **sig5 · W-First Up Close (F1):** `close[-1] > close[-2]` AND ≥ `DOWN_WEEKS`
  (default 3) consecutive lower closes immediately prior.
- **sig6 · W-Vol Accumulation:** `vol[-1] > SMA20(vol)` **AND**
  ( green `close > open` **OR** hammer `lower_wick > 1.5*body AND upper_wick < 0.5*body` ).
  The candle-shape gate is load-bearing (accumulation vs distribution).

## F9 — regime (fully specified)
- Series = BTC **weekly** closes (closed candles, from `candles` table).
- EMA200 on that series. `above` = last closed weekly close vs `ema[-1]`.
- slope = `ema[-1] - ema[-5]`.
- BULL = above ∧ slope>0 · BEAR = not above · RECOVERY = above ∧ slope≤0.
- D-RSI = RSI14 on BTC **daily** closes. M-RSI = RSI14 on end-anchored every-4th
  weekly close (per F4).

## Q1 — Champion selection
Highest **confirmed** score → tie-break lowest `pct_above_52w_low` → alphabetical.
(Card only; never affects who alerts. Operator may veto the value tiebreak.)

## Q2 — Tranche quantization (F8)
Add `lot_decimals INTEGER` to `pairs`, fetched from AssetPairs. Rule:
`base = max(ordermin, costmin/price)` → round **up** at lot_decimals precision →
apply conviction multiplier → quantize → assert `qty >= ordermin` AND
`qty*price >= costmin` post-quantization.

## Q3 — Neighbors
Every pre-existing dir/process on the box (GoldenEye, TrekBot, web-scanner Oracle,
BTC Reversal Monitor, .hydra_paper, maxbrain, all of it) is **off-limits**: never
kill/restart/write. DEEPFIELD binds no ports; only surface is CPU/RAM/disk (§8 budget).

## Q4 — Provisional sig6 (blindness-safe)
In **provisional** eval only: once ≥15% of interval elapsed, sig6 volume is
pace-adjusted `vol_so_far / elapsed_fraction`. Below 15%, sig6 renders `~` and
drops out of the provisional denominator. Marked `~` in UI either way.
**Confirmed eval untouched.**

## dca_log.csv
On the phone; gates only M7 seeding. If skipped: only cost is possible one-time
duplicate alerts for LTC/ADA/AVAX/BCH within 24h of the 11:23 v4.4 alerts.
