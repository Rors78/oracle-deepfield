"""Scoring engine — pure functions, zero I/O. SPEC §5/§7, invariant 5.

Input: candle series (optionally + forming candle). Output: a ScoreCard per pair
publishing EVERY number the UI displays (the UI never re-implements a formula).

Evaluates twice (F13): closed series -> confirmed; closed + forming -> provisional
(marked, sig6 pace-adjusted per RULINGS Q4). N/A signals shrink the denominator,
never inflate a score (invariant 4). required = max(2, round(MIN_RATIO*achievable))
unless STRICT_SEVEN. Also: regime (F9), tranche (F8), gap metrics ('need<40').

TODO(M2): ScoreCard dataclass + evaluate(); parity-gated at M3.
"""
