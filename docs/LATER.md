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


## Edge-hunt null registry (2026-08-12) — tested and DEAD, do not re-ship

Three-agent research sweep (train <07-21, validate 07-21->08-12, paired
first-touch races on our own candles; full outputs in the session record).
Everything here FAILED its out-of-sample gate. Re-proposing any of it without
NEW evidence is re-litigating a measured null:

- **Pair concentration** (top-quartile respend): ranking reshuffles across the
  split (72h Spearman +0.20); UNI was fit-#1 then went 0-for-61. Regime-
  dependent on ~2-week timescales. Only the CRV/PEPE-first SERVICE ORDER
  survived (shipped as RESPEND_PRIORITY).
- **"Buy the turn, not the fall"** as a placement gate: NULL. First actual test
  of the July slogan — falling-knife entries measure no worse (val diff -0.13pp,
  sign-agreement 68%, MTM sensitivity -0.008). The memory phrase is a design
  rationale for close-driven laddering, not a tested edge.
- **Vol-expansion gate**: +1.67pp in train at 97% bootstrap agreement, -0.80pp
  in validation. The textbook overfit of the whole sweep. Its in-sample chart
  will look convincing again someday; it is dead.
- **Session-of-day gate**: null (val sign 53% = coin flip), though the bot's
  real stops do cluster 18-24 UTC and at daily close.
- **BTC<EMA20 gate**: stably HARMFUL both windows (below-EMA entries were the
  better ones). Inverting it = a new entry signal = banned by the no-edge
  finding.
- **Post-stop-cluster respend burst**: per-event bounce (+0.9%/24h) collapses
  on independent windows (-1.1%); ~1.2pp of the apparent rebound is mechanical
  (stop fills at the depressed print). Best defensible effect ~+0.2-0.8pp/72h
  does not clear ~0.27pp carry + fees.
- **Harvest TP multiple**: 0.75x/1.25x/1.5x ATR all rejected (sign-flips or
  <=0 on independent sets, both windows). 1.0xATR CONFIRMED as the ATR-era
  successor of the 446-rung 4% finding.
- Sweep-wide regime note: the ENTIRE placement stream went EV-negative after
  07-21 (-0.18%/rung, worse with censoring). No measurable placement state
  concentrates it. The ladder pays to run in bear chop; the payoff remains the
  regime turn. No gate fixes beta.
- **Prop-feed validation: req-1 crossings x R-multiple geometry (2026-08-15)**:
  NULL — negative in BOTH splits, EVERY cell. Paired split (train 2024-08 ->
  07-21, validate 07-21 -> 08-15), 145/9 strict rising crossings (funnel's
  11 included decays from BUY; strict-rising is the tickable event). Center
  3.2R/1.5xATR (pre-registered): train 14.3% hit, -0.40R resolved (break-even
  needs 23.8%); validate 0-for-5. Full 3x3 grid (2.5/3.2/4.0R x
  1.0/1.5/2.0xATR): no positive cell in either split — a floor, not a ridge.
  Confirmed BUYs on identical geometry: train -0.38R, validate 0-for-2 (the
  no-edge finding re-confirmed in R-space). Effective-N: 145 raw train events
  = ~9 overlap-independent clusters; validate = 1. Only positive subgroup:
  sig4-divergence-fresh at the crossing (train +0.12R resolved, n=28, ~2-3
  clusters, validate n=2 both losers) — post-hoc, thin, NOT evidence; noted
  for a future pre-registered test only. Gate A context: the 8.4% 5x-liq cap
  bound on 99/145 events at 1.5xATR. VERDICT: the DEEPFIELD signal stack does
  not produce 3R-shaped winners at daily geometry in any stage; prop feed
  stays on confirmed BUYs (near-silent by construction) and NOTHING is armed.
- **ORACLE detector x prop geometry (2026-08-15)**: NULL — same protocol as the
  req-1 study (pre-registered in Oracle/reports/prop_feed_validation_PROTOCOL_
  20260815.md BEFORE the run; full results + runner archived alongside).
  Replayed ORACLE's shipped pipeline (4h primary + 1d, 200-bar windows, 1h
  omitted uniformly) daily over 93 pairs x 92 days; fresh fires into
  {DEEP VALUE, DIVERGENCE BUY, PULLBACK BUY}: 383 train / 172 validate.
  Center 3.2R/1.5xATR: train 15.4% hit -0.35R resolved; validate 6.7% hit
  -0.72R resolved (break-even 23.8%) — WORSE out-of-sample. All 9 grid cells
  negative in both splits. MAE median ~1.04R train (the same visits-not-
  reverses tell as DEEPFIELD). Effective-N = 1 per split: 555 fires blanket
  every single day — the detector is effectively always-on (6.6 fires/day in
  validate), disqualifying as a hand feed regardless of expectancy. Per class:
  DIVERGENCE BUY (bulk) -0.36R/-0.75R; DEEP VALUE closest but negative
  resolved both splits (n 35/17); PULLBACK BUY 0-for-14. Regime caveat: bear
  tape both splits (BTC -17.2%/-3.4%). VERDICT: no system in the fleet
  produces 3R-shaped winners at daily ATR geometry. The prop wallet's feed,
  if built, must be level-structural (reclaim/higher-low, stop under
  structure, sub-1R MAE by construction) — built new and validated by this
  same protocol before it ever emits a ticket.
