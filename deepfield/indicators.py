"""Verified indicator math — Appendix A, ported VERBATIM. Pure, zero I/O.

Hand-verified correct by the architect; do NOT 'improve'. Same functions the
v4.4 reference uses (docs/reference/oracle_dca_v44.py lines 135-300), so on
identical series the outputs match to the float. Unit-tested at M2.
"""
import math


def safe_float(v):
    """Convert to float, return 0.0 on any failure."""
    try:
        f = float(v)
        return f if math.isfinite(f) else 0.0
    except Exception:
        return 0.0


def clean(series):
    """Return list of finite floats, replacing non-finite with 0.0."""
    out = []
    for v in series:
        f = safe_float(v)
        out.append(f if math.isfinite(f) else 0.0)
    return out


def calc_ema(series, period):
    """EMA, SMA-seeded. Same length as input; 0.0 where insufficient data."""
    s = clean(series)
    n = len(s)
    out = [0.0] * n
    if n < period:
        return out
    k = 2.0 / (period + 1)
    seed = sum(s[:period]) / period
    out[period - 1] = seed
    for i in range(period, n):
        out[i] = s[i] * k + out[i - 1] * (1.0 - k)
    return out


def calc_rsi(series, period=14):
    """Wilder RSI. Same length as input; 0.0 where insufficient data."""
    s = clean(series)
    n = len(s)
    out = [0.0] * n
    if n < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, n):
        delta = s[i] - s[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    if len(gains) < period:
        return out
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0.0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - (100.0 / (1.0 + rs))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0.0:
            out[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i + 1] = 100.0 - (100.0 / (1.0 + rs))
    return out


def calc_macd(series, fast=12, slow=26, signal_period=9):
    """MACD -> (macd_line, signal_line, histogram), same-length lists."""
    s = clean(series)
    n = len(s)
    e_fast = calc_ema(s, fast)
    e_slow = calc_ema(s, slow)
    macd_line = [0.0] * n
    for i in range(n):
        if e_fast[i] != 0.0 and e_slow[i] != 0.0:
            macd_line[i] = e_fast[i] - e_slow[i]
    sig_line = [0.0] * n
    start = slow - 1
    valid = [(i, macd_line[i]) for i in range(start, n) if macd_line[i] != 0.0]
    if len(valid) >= signal_period:
        seed_idx = valid[signal_period - 1][0]
        seed_vals = [v for _, v in valid[:signal_period]]
        sig_line[seed_idx] = sum(seed_vals) / signal_period
        k = 2.0 / (signal_period + 1)
        for i in range(seed_idx + 1, n):
            if macd_line[i] != 0.0:
                sig_line[i] = macd_line[i] * k + sig_line[i - 1] * (1.0 - k)
            else:
                sig_line[i] = sig_line[i - 1]
    histogram = [macd_line[i] - sig_line[i] for i in range(n)]
    return macd_line, sig_line, histogram


def calc_sma(series, period):
    """Simple Moving Average. Same-length list."""
    s = clean(series)
    n = len(s)
    out = [0.0] * n
    for i in range(period - 1, n):
        out[i] = sum(s[i - period + 1:i + 1]) / period
    return out
