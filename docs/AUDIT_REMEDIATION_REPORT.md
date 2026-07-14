# ORACLE DEEPFIELD — Audit Remediation Report

**To:** Fable (audit lead)  **Re:** your 8-finding audit of the executor + call path
**Date:** 2026-07-05  **Branch:** `master`

## Headline

All **8 findings fixed**, each with dedicated regression tests, committed, and then
**verified live** — the bot was restarted on `EXEC_MODE=live` and the new code ran
clean against real Kraken state (zero errors). Two sub-items you raised are **deferred
by explicit decision** (below), not dropped.

Two of your fixes needed a **course-correction** during implementation — flagged
plainly under F2 and F6, because in both cases the naïve version would have passed a
unit test while failing its actual purpose. Worth your eyes.

Commits: `48346c0` (F1) · `27c6f50` (F2/F4/F7) · `73ed6d1` (F3/F5/F6) · `140a17c` (F8).

---

## Finding-by-finding

### F1 — CRITICAL: `verify_open_stops` per-row logic vs per-pair identity → duplicate stop → short  ·  `48346c0`
Confirmed real. Reworked to **per-pair volume reconciliation**: sum Kraken net open
long volume per pair, budget it across the pair's open rows oldest-first — backed rows
keep/get exactly one stop, unbacked rows close and their orphan stops cancel. Invariant
enforced: *resting-stop volume ≤ open volume per pair* (no naked short).
Hardened beyond the sketch: exact **normalized** pair-key match (your substring concern),
**no** fragile `type=="buy"` filter (excludes only explicit shorts; a shape surprise can't
zero a pair and strip real stops), a **shape-sanity bail** on unparseable `OpenPositions`,
and **two-pass ordering** (all removals before any re-place, so resting volume never
transiently exceeds open). Field shape verified against the hydra reference.
**Live-verified:** on restart it ran against real 2-position `OpenPositions` (first
non-empty in this deployment) → both backed, took no action. The residual you'd have
flagged, cleared on live money.

### F2 — duplicate close → duplicate order  ·  `27c6f50`
Confirmed. **Course-correction:** your suggested gate on `flip_closed`'s rowcount is
subtly wrong here — `upsert_candle` overwrites `closed`, and the clock-close watchdog
sets `closed=1` *before* calling `handle_candle_closed`, so the rowcount comes back 0
and the gate would **silently suppress** orders on exactly the quiet pairs this strategy
targets. Replaced with explicit per-`(symbol, interval)` last-close tracking
(`Ingest._last_fired`). The regression test seeds `closed=1` on purpose, so it *fails*
the rowcount gate and *passes* this one.

### F4 — partial-fill holes in `poll_fills`  ·  `27c6f50`
Confirmed both. Now never transitions a row until the order is **terminal with a settled
`vol_exec`**; on cancel-failure / re-query-failure / still-non-terminal it stays
`pending` and converges next cycle. No stale-snapshot sizing, no orphaned remainder.

### F7 — `_next_nonce` not thread-safe  ·  `27c6f50`
Confirmed. Wrapped the read-modify-write plus nonce-file write in a `threading.Lock`.

### F3 — reconciler resweep fires the boot arm  ·  `73ed6d1`
Confirmed. The one-shot arm is now gated to **boot-time BUYs only** (`_boot_buys`,
snapshotted on the first `startup_sweep`; never re-expanded). Hourly-reconciler and
`f`-key resweeps stay quiet as documented; real closes still fire via the close path.
**Live-verified:** on restart the arm fired exactly the two boot-time BUYs (LTC, SUI).

### F5 — stale post-only bids never expire  ·  `73ed6d1`
Confirmed. Unfilled pendings past `ENTRY_TTL_SECS` (default 1 day, 0=off) are now expired
in `poll_fills` via the same F4 cancel→terminal-resolve machinery. Fills are untouched
(a filled bid is `open`, not `pending`), so stacking of *fills* still works.

### F6 — realized P&L never written → loss caps are placebo  ·  `73ed6d1`
Confirmed. Now recorded on a stop-triggered close from Kraken's own execution records
`(stop-sell cost − fee) − (entry-buy cost + fee)`. **Course-correction:** as first
drafted it would have passed a unit test but failed its purpose — `realized_pnl_since`
buckets by the **entry** `ts`, so a position entered days ago and stopped out today
lands in the wrong day and the loss caps still miss it. Re-bucketed by `closed_ts`;
cross-day regression test added. Documented limits: rollover/financing fees excluded
(loss slightly understated → cap trips marginally late); only stop-outs are recorded
(aligned — loss caps care about stop-outs).

### F8 — min-sizing is a knob, not an invariant  ·  `140a17c`
Implemented your notional ceiling: `EXEC_MAX_ORDER_NOTIONAL_USD` (default $50, 0=off)
refuses any order whose notional exceeds it — never halts the bot, never shrinks a valid
min order. **Live-verified:** the $3.80 / $4.58 restart orders passed.

---

## On your minors
- The `_startup` `len(kr)` TypeError-on-`None` masking `verify_open_stops`, and the
  `_has_position` substring fragility — both confirmed and subsumed by the F1 rewrite.
- **One didn't hold:** the `_maybe_alert` `%.0f`-on-`None`. `should_alert` returns `True`
  when the last-alert ts is `None`, so the suppression-log branch never formats `None`.
  Not reachable. (You'd hedged it as latent — it's a non-issue.)

## Deferred by explicit decision (flagged, not dropped)
- **Weekly-border daily+weekly double-fire** — two genuine per-interval closes; a strategy
  call (is a weekly border one pyramid step or two?). `_last_fired` makes cross-interval
  dedup a one-liner if the operator wants it.
- **Cooldown check/insert TOCTOU** — latent while `REALERT_HOURS=0`; only bites if the
  cooldown is re-enabled, at which point check+insert must be atomic on the writer.

## Verification
- **Full test suite: 86 passing.** The only 2 failures are pre-existing cooldown tests
  that contradict the operator's `REALERT_HOURS=0` override — proven independent of this
  work by a stash-test against the pre-change code. Every finding ships with dedicated
  regression tests.
- **Live restart** on `EXEC_MODE=live`: clean startup, both WS links up, zero
  errors; F1/F3/F8 exercised on real data as noted above.

*If any statement here disagrees with the diff, the diff wins — flag it and we'll
reconcile. This report is meant to make your re-review faster, not to pre-empt it.*
