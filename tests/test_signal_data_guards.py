"""Data guards on sig3 and the sig4 pivot filter (2026-07-19).

Both pin the same class of bug: a signal reading a number that exists in the array
but does not yet mean anything, and treating it as evidence.

  sig3 — calc_macd seeds macd_line at slow-1 (25) but the signal line only at 33,
  while histogram = macd_line - sig_line is computed across the whole array. In
  that gap the histogram IS macd_line, a large bogus value, and sig3's 8-bar window
  reads the seam as a negative->positive crossup. v4.4 asked for only 4 bars.

  _pivots — clean() maps non-finite input to 0.0. The prominence filter was written
  `if min_depth is not None and v > 0:`, so a 0.0 bar skipped the quality test
  rather than failing it, and was admitted as the lowest low — making the price
  lower-low leg of a divergence trivially true.

COMPAT must still reproduce v4.4 with zero diffs, so both fixes are profile-gated:
sig3's tighter guard on f3, the pivot rejection inside the f2-only min_depth branch.
"""
import math

import pytest

from deepfield import indicators as ind, signals as sg
from deepfield.config import MACD_LOOKBACK, MACD_MIN_BARS, MACD_SEED_BARS
from deepfield.profiles import COMPAT, FULL
from deepfield.signals import FIRED, NA, NOT


def _series(n):
    """Smooth, gap-free weekly closes — no data defect, only insufficient length."""
    return [100 + 10 * math.sin(i / 5) for i in range(n)]


def test_macd_seed_constant_tracks_the_real_seam():
    """The premise: histogram is meaningless until the signal line seeds."""
    _, sig_line, _ = ind.calc_macd(_series(60), 12, 26, 9)
    seeded_at = next(i for i, v in enumerate(sig_line) if v != 0.0)
    assert seeded_at == MACD_SEED_BARS - 1, "MACD_SEED_BARS must track the seed index"
    # A seeded signal line is NOT enough: sig3 reads a window, so the guard has to
    # clear the seam by the full lookback. This is the off-by-lookback the first
    # version of this fix got wrong — at 34 bars w_hist[-8:] still spans the seam.
    assert MACD_MIN_BARS == MACD_SEED_BARS + MACD_LOOKBACK - 1
    assert MACD_MIN_BARS > MACD_SEED_BARS


@pytest.mark.parametrize("n", [26, 30, 34, 36, 40, MACD_MIN_BARS - 1])
def test_sig3_is_na_while_its_window_straddles_the_seam(n):
    hist = ind.calc_macd(_series(n), 12, 26, 9)[2]
    assert sg.sig3_macd_crossup(hist, FULL).state == NA, \
        f"len={n} still reads unseeded histogram in its {MACD_LOOKBACK}-bar window"


def test_sig3_fires_on_the_artifact_without_the_guard():
    """Pin the bug itself: unguarded, these lengths FIRE on nothing."""
    fired = [n for n in range(26, MACD_MIN_BARS)
             if sg.sig3_macd_crossup(ind.calc_macd(_series(n), 12, 26, 9)[2],
                                     COMPAT).state == FIRED]
    assert fired, "expected spurious fires below MACD_MIN_BARS on a smooth series"
    for n in fired:
        hist = ind.calc_macd(_series(n), 12, 26, 9)[2]
        assert sg.sig3_macd_crossup(hist, FULL).state == NA, \
            f"len={n} fires spuriously under compat and must be N/A under FULL"


def test_sig3_still_evaluates_once_seeded():
    hist = ind.calc_macd(_series(80), 12, 26, 9)[2]
    assert sg.sig3_macd_crossup(hist, FULL).state in (FIRED, NOT)


def test_sig3_compat_guard_is_unchanged_for_parity():
    """COMPAT must reproduce v4.4: 4 bars, and never N/A."""
    assert sg.sig3_macd_crossup([0.1, 0.2, 0.3], COMPAT).state == NOT   # <4 bars
    for n in (26, 36, 80):
        hist = ind.calc_macd(_series(n), 12, 26, 9)[2]
        assert sg.sig3_macd_crossup(hist, COMPAT).state != NA, \
            "compat may never return N/A — it counts in the fixed denominator of 7"


def test_pivot_filter_rejects_unpriced_bars():
    vals = [10.0, 9.0, 10.0, 9.5, 0.0, 9.4, 10.0]      # idx 4 is a cleaned gap
    piv = sg._pivots(vals, 0.015, 3)
    assert all(v > 0 for _, v in piv), f"0.0 admitted as a pivot: {piv}"
    assert 4 not in [i for i, _ in piv]


def test_pivot_filter_still_finds_real_pivots():
    vals = [10.0, 9.0, 10.0, 10.2, 8.5, 10.1, 10.3]
    assert [i for i, _ in sg._pivots(vals, 0.015, 3)] == [1, 4]


def test_unpriced_low_no_longer_fabricates_a_divergence():
    """A gap-filled 0.0 used to become the lower low and satisfy p2 < p1."""
    closes = [10.0] * 12 + [9.0, 10.0, 9.5, 10.0, 0.0, 10.0] + [10.0] * 6
    rsi = [50.0] * 12 + [30.0, 50.0, 45.0, 50.0, 40.0, 50.0] + [50.0] * 6
    assert sg.find_bullish_divergence(closes, rsi, 60, 0.015, 3) is False


def test_pivot_compat_path_untouched():
    """min_depth None (f2 off) is v4.4 verbatim — the fix must not reach it."""
    vals = [10.0, 9.0, 10.0, 9.5, 0.0, 9.4, 10.0]
    assert [i for i, _ in sg._pivots(vals, None, None)] == [1, 4]


def test_parity_attributes_a_newly_na_sig3_to_F3():
    """The live universe has no pair short enough to exercise this through the M3
    gate (shortest is ~53 weekly bars vs a 41-bar guard), so pin it directly: a
    sig3 that goes N/A under FULL must attribute to F3, not stop the gate. The rule
    was written for slot 1 only and would have failed sig3 as UNATTRIBUTED."""
    from deepfield import parity

    assert parity._attribute(3, NOT, NA) == "F3"
    assert parity._attribute(1, NOT, NA) == "F3"      # original case still holds
    # A non-N/A disagreement on a verbatim slot is still a hard stop.
    assert parity._attribute(3, NOT, FIRED) == "UNATTRIBUTED"
