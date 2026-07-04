"""The seven Oracle signals — pure functions, zero I/O. SPEC §7 + docs/RULINGS.md.

Each signal returns a SignalResult keyed by SLOT (1..7), not display string, so
the sig5 rename never ghosts a parity diff (reviewer #3). Tri-state:
FIRED / NOT / NA. NA (F3, only under profile.f3) shrinks the denominator and can
never inflate a score (invariant 4); in compat, insufficient data -> NOT, matching
v4.4 which counts it in the fixed denominator of 7.

Indicator inputs (w_ema200, w_rsi, w_hist, d_rsi, w_vol_sma) are computed ONCE by
the engine and passed in, matching v4.4's single inline computation.
"""
from dataclasses import dataclass

from .config import DOWN_WEEKS, PIVOT_MIN_DEPTH

FIRED = "fired"
NOT = "not"
NA = "na"

# Slot -> (compat name, full name). Only slot 5 differs (F1 rename).
NAMES = {
    1: ("Below W-EMA200", "Below W-EMA200"),
    2: ("W-RSI<40 Turning Up", "W-RSI<40 Turning Up"),
    3: ("W-MACD Hist Crossup", "W-MACD Hist Crossup"),
    4: ("D-RSI Divergence", "D-RSI Divergence"),
    5: ("W-First Higher High", "W-First Up Close"),
    6: ("W-Vol Accumulation", "W-Vol Accumulation"),
    7: ("Near 52w Low (<20%)", "Near 52w Low (<20%)"),
}


@dataclass
class SignalResult:
    slot: int
    name: str
    state: str          # FIRED | NOT | NA
    reason: str = ""    # populated on NA


def _name(slot, profile):
    compat_name, full_name = NAMES[slot]
    return full_name if (slot == 5 and profile.f1) else compat_name


def _mk(slot, profile, fired, na=False, reason=""):
    if na:
        return SignalResult(slot, _name(slot, profile), NA, reason)
    return SignalResult(slot, _name(slot, profile), FIRED if fired else NOT)


# ── sig1: price below weekly EMA200 (F3 makes <200 bars N/A) ────────────────
def sig1_below_wema200(wc, w_ema200, profile):
    if len(wc) < 200 or w_ema200[-1] <= 0:
        if profile.f3:
            return _mk(1, profile, False, na=True, reason="needs 200 weekly bars")
        return _mk(1, profile, False)  # v4.4: False, still in denominator
    return _mk(1, profile, wc[-1] < w_ema200[-1])


# ── sig2: weekly RSI < 40 and turning up vs 3 bars back (verbatim) ──────────
def sig2_wrsi_turning_up(w_rsi, profile):
    if not (w_rsi and w_rsi[-1] > 0) or len(w_rsi) < 5:
        if profile.f3:
            return _mk(2, profile, False, na=True, reason="needs 5 weekly RSI bars")
        return _mk(2, profile, False)
    ref = w_rsi[-4] if w_rsi[-4] > 0 else None
    if ref is None:
        return _mk(2, profile, False)
    return _mk(2, profile, (w_rsi[-1] < 40.0) and (w_rsi[-1] > ref))


# ── sig3: weekly MACD hist positive, was negative within 8 bars (verbatim) ──
def sig3_macd_crossup(w_hist, profile):
    if len(w_hist) < 4:
        if profile.f3:
            return _mk(3, profile, False, na=True, reason="needs 4 weekly MACD bars")
        return _mk(3, profile, False)
    now_pos = w_hist[-1] > 0
    lookback = w_hist[-8:] if len(w_hist) >= 8 else w_hist
    had_neg = any(h < 0 for h in lookback[:-1])
    return _mk(3, profile, had_neg and now_pos)


# ── sig4: daily RSI bullish divergence (F2 adds pivot quality) ──────────────
def _pivots(vals, min_depth, min_spacing):
    """3-bar local minima; F2: prominence >= min_depth vs BOTH neighbors
    (proportional) and accepted pivots >= min_spacing bars apart."""
    lows = []
    last_i = None
    for i in range(1, len(vals) - 1):
        v, a, b = vals[i], vals[i - 1], vals[i + 1]
        if not (v < a and v < b):
            continue
        if min_depth is not None and v > 0:
            if (a - v) / v < min_depth or (b - v) / v < min_depth:
                continue
        if min_spacing is not None and last_i is not None and (i - last_i) < min_spacing:
            continue
        lows.append((i, v))
        last_i = i
    return lows


def find_bullish_divergence(closes, rsi_vals, lookback=60, min_depth=None, min_spacing=None):
    """Price lower-low + RSI higher-low in the last `lookback` bars.
    v4.4 structure; F2 params add price-pivot prominence + spacing."""
    from .indicators import clean
    c = clean(closes[-lookback:])
    r = clean(rsi_vals[-lookback:])
    n = len(c)
    if n < 20:
        return False
    price_lows = _pivots(c, min_depth, min_spacing)
    # RSI pivots keep the v4.4 detector (spacing applied, no proportional depth).
    rsi_lows = []
    last_ri = None
    for i in range(1, n - 1):
        if r[i] > 0 and r[i] < r[i - 1] and r[i] < r[i + 1]:
            if min_spacing is not None and last_ri is not None and (i - last_ri) < min_spacing:
                continue
            rsi_lows.append((i, r[i]))
            last_ri = i
    if len(price_lows) < 2 or len(rsi_lows) < 2:
        return False
    p1_i, p1 = price_lows[-2]
    p2_i, p2 = price_lows[-1]
    r1 = r2 = None
    for ri, rv in rsi_lows:
        if abs(ri - p1_i) <= 5:
            r1 = rv
        if abs(ri - p2_i) <= 5:
            r2 = rv
    if r1 is None or r2 is None:
        return False
    return (p2 < p1) and (r2 > r1)


def sig4_drsi_divergence(dc, d_rsi, profile):
    if len(dc) < 21:  # find_bullish_divergence needs a >=20-bar window
        if profile.f3:
            return _mk(4, profile, False, na=True, reason="needs 20 daily bars")
        return _mk(4, profile, False)
    if profile.f2:
        fired = find_bullish_divergence(dc, d_rsi, 60, PIVOT_MIN_DEPTH, 3)
    else:
        fired = find_bullish_divergence(dc, d_rsi, 60)
    return _mk(4, profile, fired)


# ── sig5: first weekly up-close after a downtrend (F1 tightens) ─────────────
def sig5_first_up_close(wc, profile):
    need = (DOWN_WEEKS + 2) if profile.f1 else 5
    if len(wc) < need:
        if profile.f3:
            return _mk(5, profile, False, na=True, reason=f"needs {need} weekly bars")
        return _mk(5, profile, False)
    first_up = wc[-1] > wc[-2]
    if profile.f1:
        # DOWN_WEEKS consecutive lower closes immediately prior.
        downtrend = all(wc[-i - 1] < wc[-i - 2] for i in range(1, DOWN_WEEKS + 1))
    else:
        downtrend = (wc[-2] < wc[-3]) or (wc[-3] < wc[-4])
    return _mk(5, profile, first_up and downtrend)


# ── sig6: volume > SMA20 on a green/hammer weekly candle (verbatim) ─────────
def sig6_vol_accumulation(wo, wh, wl, wc, wvol, w_vol_sma, profile):
    if len(wvol) < 21 or w_vol_sma[-1] <= 0 or not (wo and wh and wl):
        if profile.f3:
            return _mk(6, profile, False, na=True, reason="needs 21 weekly bars")
        return _mk(6, profile, False)
    vol_above = wvol[-1] > w_vol_sma[-1]
    o, h, l, c = wo[-1], wh[-1], wl[-1], wc[-1]
    body = abs(c - o)
    is_green = c > o
    lower_wick = min(o, c) - l
    upper_body = h - max(o, c)
    is_hammer = (lower_wick > body * 1.5) and (upper_body < body * 0.5)
    return _mk(6, profile, vol_above and (is_green or is_hammer))


# ── sig7: price within 20% of 52-week low (verbatim) ───────────────────────
def sig7_near_52w_low(price, low_52w, profile):
    if not (low_52w and low_52w > 0 and price > 0):
        if profile.f3:
            return _mk(7, profile, False, na=True, reason="no 52w low")
        return _mk(7, profile, False)
    return _mk(7, profile, (price - low_52w) / low_52w <= 0.20)
