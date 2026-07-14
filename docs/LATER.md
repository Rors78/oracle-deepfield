# LATER — parked ideas (one-liners, not code; operator-gated)

- sig3 fresh-cross variant: convert `W-MACD Hist Crossup` from state to event
  (`hist[-1]>0 AND hist[-2]<=0`). Strategy change — operator must gate.

- continuous laddering (built in v6 worktree, NOT deployed): drop next post-only
  rung 1% below each fill down to the stop. My "thesis-gated" refinement (re-run
  engine before each rung) is WEAKER than it sounds — the 7 signals are daily/weekly,
  so an intraday gate is inert between closes. Key finding: a strategy-paced ladder
  ALREADY exists — confirmed-BUY re-fires on every daily+weekly close (cooldown off,
  REALERT_HOURS=0) and the 24h ENTRY_TTL means ~one rolling bid per confirmed pair,
  re-priced daily, filling as price dips. The 1% grid only adds sub-daily price-only
  buys no signal can justify. Disciplined lever for MORE size = conviction-weighted
  tranches at the qualifying close, not a between-close grid. Operator-gated.
