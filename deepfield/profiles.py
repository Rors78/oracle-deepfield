"""Fix-toggle profiles for the parity gate (reviewer sharpening #1).

Each F-item that diverges from v4.4 is a switchable branch. COMPAT (all off)
must reproduce vendored v4.4 with ZERO diffs; FULL (all on) is DEEPFIELD.
M3 triangulates: compat==v4.4 proves the port is faithful; full-vs-compat
isolates each fix by construction.

Only fixes that alter a pair scorecard or the regime need a branch:
  f1 sig5 tighten · f2 sig4 pivot quality · f3 young-listing N/A + dynamic
  required · f4 monthly-RSI end-anchor (M-RSI only) · f9 regime 4-bar/no-NEUTRAL.
sig2/sig3/sig6/sig7 are verbatim (compat == full).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    f1: bool = True   # sig5: 3 consecutive lower closes (vs v4.4's loose OR)
    f2: bool = True   # sig4: pivot prominence + spacing
    f3: bool = True   # N/A young listings, denom = achievable, dynamic required
    f4: bool = True   # M-RSI end-anchored sampling
    f9: bool = True   # regime: 4-bar slope, BULL/BEAR/RECOVERY (no NEUTRAL)


FULL = Profile()
COMPAT = Profile(f1=False, f2=False, f3=False, f4=False, f9=False)
