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
- **The ADA/AVAX clamp tripwire FIRED 2026-08-11 and was adjudicated by
  measurement** (risk-rails, 31-day paired first-touch race over our own
  candles — clamped vs unclamped stop on identical entries/paths):
  * **AVAX: EXCLUDED.** The one pair where the clamp flips expectancy negative
    (+2.33% -> -1.08%/cycle, 63% stop-first at ~1:1 reward, worsening at every
    horizon), with ZERO harvests in 29 orders — no income forfeited. Removed
    while flat.
  * **ADA: KEPT.** Its 08-11 exits were correct invalidation (price fell a
    further 3.7%, never reclaimed entry — the stops SAVED $0.37 vs holding),
    the unclamped ATR stop missed firing by only 0.11 ATR, and clamped EV stays
    positive (+1.68%/cycle) on a 1.25:1 TP:SL. **Numeric re-arm threshold: two
    further clamp-fired ADA exits that reclaim entry within 24h without a lower
    low first, OR SL/ATR < 0.70 on the daily refresh (0.80 today) = exclude.**
  * **XRP: KEPT** (was never in the tripwire). Its 08-11 stops were
    clamp-independent — the unclamped floor was breached too, 8h later at a
    worse price. Its churn/harvest ratio is pair-weakness (beta), not geometry.
  * Control worth remembering: RENDER, unclamped at the full 1.5xATR floor,
    took the day's LARGEST stop loss with price 2.4% below even the ATR floor —
    on a genuine down day, ATR stops die too. The clamp is not the only thing
    that fires.
- **`tp_baseline` $289.83 is CORRECT** (settled 2026-08-08 from the raw Ledgers
  responses: three deposits netting +$85.04, zero withdrawals; the "$54 equity"
  counter-read was the eb/tb collateral pocket). When auditing external flows,
  compare against **eb**, never tb/equity.
