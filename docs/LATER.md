# LATER — parked ideas (one-liners, not code; operator-gated)

- sig3 fresh-cross variant: convert `W-MACD Hist Crossup` from state to event
  (`hist[-1]>0 AND hist[-2]<=0`). Strategy change — operator must gate.

- continuous laddering (DEPLOYED — `LADDER_CONTINUOUS=True` has been live on master
  since mid-July; this note predates the cutover): drop next post-only
  rung 1% below each fill down to the stop. My "thesis-gated" refinement (re-run
  engine before each rung) is WEAKER than it sounds — the 7 signals are daily/weekly,
  so an intraday gate is inert between closes. Key finding: a strategy-paced ladder
  ALREADY exists — confirmed-BUY re-fires on every daily+weekly close (cooldown off,
  REALERT_HOURS=0) and the 24h ENTRY_TTL means ~one rolling bid per confirmed pair,
  re-priced daily, filling as price dips. The 1% grid only adds sub-daily price-only
  buys no signal can justify. Disciplined lever for MORE size = conviction-weighted
  tranches at the qualifying close, not a between-close grid. Operator-gated.


## Standing deliberate decisions — do NOT re-fix (2026-08-09)

Each of these was examined during the 2026-08-07/08 audit waves and left AS IS on
purpose. A future audit that flags one of them should find this note, read the
reasoning, and move on — re-litigating them is the waste this section prevents.

- **`_place_entry` sizes at `entry_price`, places at `px`** (one post-only slip
  below, ~0.1% apart). Unifying would change live sizing for zero practical gain
  — `costmin` is $0.50 against $3–8 notionals. `_place_ladder_rung` does it the
  "right" way (sizes at its actual price); the asymmetry is known.
- **`engine.tranche` keeps its own min-order floor** alongside
  `executor._min_volume`. Provably equivalent while every `ordermin` sits on the
  lot grid (verified across all 28 pairs); engine cannot import executor without
  a cycle. The equivalence condition is stated at both sites.
- **`rails_ok` / `rails_detail` are two implementations on purpose** — money path
  vs picture — pinned against each other by `test_rails_detail_agrees_with_rails_ok`.
  This is the TEMPLATE for anything that must exist twice.
- **The stress cascade errs tighter under old blobs** (pair-level fallback when
  per-fill arrays are absent) — the safe direction for a risk read.
- **ADA/AVAX stay in the roster at 10x** despite Gate A clamping their stops
  inside one average day's range (SL/ATR 0.76 / 0.89). Zero and one lifetime
  stop exits respectively — the churn case is a forecast, not a measurement.
  **Pre-registered tripwire: stop exits at the 4.20% clamp on either pair =
  revisit the geometry.**
- **`tp_baseline` $289.83 is CORRECT** (settled 2026-08-08 from the raw Ledgers
  responses: three deposits netting +$85.04, zero withdrawals; the "$54 equity"
  counter-read was the eb/tb collateral pocket). When auditing external flows,
  compare against **eb**, never tb/equity.
