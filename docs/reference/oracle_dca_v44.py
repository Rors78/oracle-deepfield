#!/usr/bin/env python3
"""
ORACLE DCA v4.4
Long-term crypto bottom detection scanner for Kraken spot markets.
Pydroid3 / Termux / Samsung S23 Ultra optimized. AMOLED color palette.
Pure Python 3 stdlib ONLY — zero external dependencies.

Strategy: DCA accumulation targeting genuine cycle bottoms.
Hold minimum 1 year. No leverage. No SL. No TP.

Continuous mode: rescans every REFRESH_HOURS. Run with --once for a
single scan (cron / testing). BUY alerts are deduped per symbol via
dca_log.csv (REALERT_HOURS cooldown) — restarts never re-alert.
"""

import os
import sys
import csv
import json
import time
import math
import signal
import logging
import urllib.request
import urllib.parse
import urllib.error
import subprocess
import shutil
import datetime
import traceback
import statistics
from collections import deque

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG BLOCK — edit these freely
# ─────────────────────────────────────────────────────────────────────────────

ACCOUNT_BALANCE = 17.62        # USD — display only
REFRESH_HOURS   = 4            # Hours between scans (loop mode)
REALERT_HOURS   = 24           # Per-symbol BUY re-alert/re-log cooldown (persisted via dca_log.csv)
MIN_SCORE       = 5            # Signals required for BUY NOW

MIN_CALL_GAP    = 0.6          # Seconds between Kraken public API calls (~33 calls/scan)
FETCH_RETRIES   = 2            # Extra attempts on transient fetch failures

VERSION = "4.4"

# Pairs in Kraken API format → (display symbol, minimum order size)
PAIRS = {
    "XXBTZUSD":  ("BTC",  0.0001),
    "XETHZUSD":  ("ETH",  0.001),
    "XXRPZUSD":  ("XRP",  1.0),
    "SOLUSD":    ("SOL",  0.01),
    "SUIUSD":    ("SUI",  1.0),
    "XDGUSD":    ("DOGE", 1.0),
    "XLTCZUSD":  ("LTC",  0.01),
    "LINKUSD":   ("LINK", 0.01),
    "ADAUSD":    ("ADA",  1.0),
    "AVAXUSD":   ("AVAX", 0.01),
    "AAVEUSD":   ("AAVE", 0.01),
    "UNIUSD":    ("UNI",  0.01),
    "DOTUSD":    ("DOT",  0.01),
    "BCHUSD":    ("BCH",  0.001),
    "ALGOUSD":   ("ALGO", 1.0),
}

USE_CLOSED_CANDLES = True   # Strip live unfinished candle before analysis

# Canonical signal names — used to compute missing signals per coin
ALL_SIGNALS = [
    "Below W-EMA200",
    "W-RSI<40 Turning Up",
    "W-MACD Hist Crossup",
    "D-RSI Divergence",
    "W-First Higher High",
    "W-Vol Accumulation",
    "Near 52w Low (<20%)",
]

WEEKLY_CANDLES = 200        # Weekly candles to fetch
DAILY_CANDLES  = 365        # Daily candles to fetch

LOG_FILE  = "dca_log.csv"
LOG_HARD  = "oracle_dca_hard_ledger.log"
LOG_SHOW  = 5               # Last N log entries shown on screen

# Kraken API intervals (minutes)
INTERVAL_WEEK = 10080
INTERVAL_DAY  = 1440

KRAKEN_OHLC   = "https://api.kraken.com/0/public/OHLC"
KRAKEN_TICKER = "https://api.kraken.com/0/public/Ticker"

# ─────────────────────────────────────────────────────────────────────────────
#  AMOLED COLOR PALETTE
# ─────────────────────────────────────────────────────────────────────────────

C = {
    "reset":   "\033[0m",
    "bold":    "\033[1m",
    "dim":     "\033[2m",
    "white":   "\033[97m",
    "silver":  "\033[37m",
    "gray":    "\033[90m",
    "gold":    "\033[38;5;220m",
    "amber":   "\033[38;5;214m",
    "green":   "\033[38;5;82m",
    "lime":    "\033[38;5;118m",
    "cyan":    "\033[38;5;51m",
    "red":     "\033[38;5;196m",
    "orange":  "\033[38;5;208m",
    "pink":    "\033[38;5;213m",
    "purple":  "\033[38;5;141m",
    "blue":    "\033[38;5;75m",
    "buy":     "\033[38;5;82m",
    "watch":   "\033[38;5;220m",
    "nope":    "\033[38;5;240m",
}

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    filename=LOG_HARD,
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ─────────────────────────────────────────────────────────────────────────────
#  PURE PYTHON MATH HELPERS
# ─────────────────────────────────────────────────────────────────────────────

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

def lst_mean(series):
    s = clean(series)
    if not s:
        return 0.0
    return sum(s) / len(s)

def lst_std(series):
    s = clean(series)
    if len(s) < 2:
        return 0.0
    m = sum(s) / len(s)
    variance = sum((x - m) ** 2 for x in s) / len(s)
    return math.sqrt(variance)

def lst_min(series):
    s = [v for v in clean(series) if v > 0]
    return min(s) if s else 0.0

def lst_max(series):
    s = clean(series)
    return max(s) if s else 0.0

# ─────────────────────────────────────────────────────────────────────────────
#  TECHNICAL INDICATORS  (pure Python lists, no numpy)
# ─────────────────────────────────────────────────────────────────────────────

def calc_ema(series, period):
    """
    Exponential Moving Average.
    Returns list same length as series; 0.0 where insufficient data.
    """
    s = clean(series)
    n = len(s)
    out = [0.0] * n
    if n < period:
        return out
    k = 2.0 / (period + 1)
    # Seed with SMA of first `period` values
    seed = sum(s[:period]) / period
    out[period - 1] = seed
    for i in range(period, n):
        out[i] = s[i] * k + out[i - 1] * (1.0 - k)
    return out

def calc_rsi(series, period=14):
    """
    Wilder RSI.
    Returns list same length as series; 0.0 where insufficient data.
    """
    s = clean(series)
    n = len(s)
    out = [0.0] * n
    if n < period + 1:
        return out

    gains  = []
    losses = []
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
    """
    MACD. Returns (macd_line, signal_line, histogram) as same-length lists.
    """
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
    # Seed signal EMA from macd_line values starting at slow-1
    valid = [(i, macd_line[i]) for i in range(start, n) if macd_line[i] != 0.0]
    if len(valid) >= signal_period:
        seed_idx   = valid[signal_period - 1][0]
        seed_vals  = [v for _, v in valid[:signal_period]]
        sig_seed   = sum(seed_vals) / signal_period
        sig_line[seed_idx] = sig_seed
        k = 2.0 / (signal_period + 1)
        for i in range(seed_idx + 1, n):
            if macd_line[i] != 0.0:
                sig_line[i] = macd_line[i] * k + sig_line[i - 1] * (1.0 - k)
            else:
                sig_line[i] = sig_line[i - 1]

    histogram = [macd_line[i] - sig_line[i] for i in range(n)]
    return macd_line, sig_line, histogram

def calc_bollinger(series, period=20, num_std=2.0):
    """
    Bollinger Bands.
    Returns (upper, middle, lower, bandwidth) as same-length lists.
    bandwidth = (upper - lower) / middle * 100
    """
    s = clean(series)
    n = len(s)
    upper = [0.0] * n
    middle = [0.0] * n
    lower = [0.0] * n
    bw = [0.0] * n

    for i in range(period - 1, n):
        window = s[i - period + 1:i + 1]
        m  = sum(window) / period
        sd = math.sqrt(sum((x - m) ** 2 for x in window) / period)
        middle[i] = m
        upper[i]  = m + num_std * sd
        lower[i]  = m - num_std * sd
        bw[i]     = ((upper[i] - lower[i]) / m * 100.0) if m != 0.0 else 0.0

    return upper, middle, lower, bw

def calc_sma(series, period):
    """Simple Moving Average. Returns same-length list."""
    s = clean(series)
    n = len(s)
    out = [0.0] * n
    for i in range(period - 1, n):
        out[i] = sum(s[i - period + 1:i + 1]) / period
    return out

# ─────────────────────────────────────────────────────────────────────────────
#  RSI DIVERGENCE DETECTOR
# ─────────────────────────────────────────────────────────────────────────────

def find_bullish_divergence(closes, rsi_vals, lookback=60):
    """
    Bullish RSI divergence in last `lookback` bars.
    Price lower low + RSI higher low = divergence.
    """
    c = clean(closes[-lookback:])
    r = clean(rsi_vals[-lookback:])
    n = len(c)

    if n < 20:
        return False

    price_lows = []
    rsi_lows   = []

    for i in range(1, n - 1):
        if c[i] < c[i - 1] and c[i] < c[i + 1]:
            price_lows.append((i, c[i]))
        if r[i] > 0 and r[i] < r[i - 1] and r[i] < r[i + 1]:
            rsi_lows.append((i, r[i]))

    if len(price_lows) < 2 or len(rsi_lows) < 2:
        return False

    p1_i, p1 = price_lows[-2]
    p2_i, p2 = price_lows[-1]

    r1 = None
    r2 = None
    for ri, rv in rsi_lows:
        if abs(ri - p1_i) <= 5:
            r1 = rv
        if abs(ri - p2_i) <= 5:
            r2 = rv

    if r1 is None or r2 is None:
        return False

    return (p2 < p1) and (r2 > r1)

# ─────────────────────────────────────────────────────────────────────────────
#  KRAKEN DATA FETCHER
# ─────────────────────────────────────────────────────────────────────────────

_last_api_call = [0.0]

def _throttle():
    """Enforce MIN_CALL_GAP between Kraken public calls — be polite, avoid 429s."""
    gap = time.time() - _last_api_call[0]
    if gap < MIN_CALL_GAP:
        time.sleep(MIN_CALL_GAP - gap)
    _last_api_call[0] = time.time()

def fetch_btc_pulse():
    """
    Live BTC price and 24h stats from Kraken Ticker — zero cache, always fresh.
    Returns dict with: price, change_24h, change_pct_24h, high_24h, low_24h, vol_24h
    or None on error.
    """
    try:
        _throttle()
        url = "{}?pair=XXBTZUSD".format(KRAKEN_TICKER)
        req = urllib.request.Request(
            url, headers={"User-Agent": "OracleDCA/" + VERSION})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        if data.get("error"):
            return None
        result = data.get("result", {})
        key = [k for k in result if k != "last"][0]
        t = result[key]
        # Kraken ticker fields:
        # c = last trade [price, lot], o = open, h = [today,24h], l = [today,24h]
        # v = [today,24h] volume, p = [today,24h] vwap
        price     = safe_float(t["c"][0])
        open_24h  = safe_float(t["o"])
        high_24h  = safe_float(t["h"][1])
        low_24h   = safe_float(t["l"][1])
        vol_24h   = safe_float(t["v"][1])
        chg       = price - open_24h
        chg_pct   = (chg / open_24h * 100) if open_24h > 0 else 0.0
        return {
            "price":      price,
            "change":     chg,
            "change_pct": chg_pct,
            "high_24h":   high_24h,
            "low_24h":    low_24h,
            "vol_24h":    vol_24h,
            "open_24h":   open_24h,
        }
    except Exception:
        logging.exception("fetch_btc_pulse failed")
        return None


def fetch_ohlc(pair, interval, count=720):
    """
    Fetch OHLC from Kraken public API.
    Returns (times, opens, highs, lows, closes, volumes) as float lists, or None.

    Kraken REST OHLC hard-caps at 720 candles regardless of 'since'.
    We omit 'since' so Kraken returns the most recent 720 bars automatically.
    720 weekly = ~13.8 years. 720 daily = ~2 years. Both sufficient for all signals.

    Retries transient failures (network errors, rate limits) FETCH_RETRIES times
    with backoff so one hiccup doesn't ERR a pair for a whole refresh cycle.
    """
    params = urllib.parse.urlencode({
        "pair":     pair,
        "interval": interval,
    })
    url = f"{KRAKEN_OHLC}?{params}"

    raw = None
    for attempt in range(FETCH_RETRIES + 1):
        _throttle()
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "OracleDCA/" + VERSION},
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except Exception:
            if attempt == FETCH_RETRIES:
                logging.exception(f"fetch_ohlc network error pair={pair} interval={interval}")
                return None
            time.sleep(2.0 * (attempt + 1))
            continue

        errs = raw.get("error") or []
        if errs:
            transient = any(
                ("Too many" in str(e)) or ("Unavailable" in str(e)) or ("EService" in str(e))
                for e in errs
            )
            if transient and attempt < FETCH_RETRIES:
                time.sleep(3.0 * (attempt + 1))
                continue
            logging.error(f"Kraken API error {pair}: {errs}")
            return None
        break

    if raw is None:
        return None

    result = raw.get("result", {})
    data_key = None
    for k in result:
        if k != "last":
            data_key = k
            break
    if data_key is None:
        return None

    candles = result[data_key]
    if not candles or len(candles) < 20:
        return None

    times  = []
    opens  = []
    highs  = []
    lows   = []
    closes = []
    vols   = []

    for row in candles:
        # Kraken OHLC columns: time,open,high,low,close,vwap,volume,count
        if len(row) < 7:
            continue
        times.append(safe_float(row[0]))
        opens.append(safe_float(row[1]))
        highs.append(safe_float(row[2]))
        lows.append(safe_float(row[3]))
        closes.append(safe_float(row[4]))
        vols.append(safe_float(row[6]))

    if not closes:
        return None

    if USE_CLOSED_CANDLES and len(closes) > 1:
        times  = times[:-1]
        opens  = opens[:-1]
        highs  = highs[:-1]
        lows   = lows[:-1]
        closes = closes[:-1]
        vols   = vols[:-1]

    return times, opens, highs, lows, closes, vols

# ─────────────────────────────────────────────────────────────────────────────
#  PAIR ANALYSIS ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def analyze_pair(kraken_pair, display_sym, min_qty):
    """
    Full bottom-detection analysis for one pair.
    Returns result dict. Never raises — errors stored in result["error"].
    """
    result = {
        "symbol":        display_sym,
        "kraken_pair":   kraken_pair,
        "min_qty":       min_qty,
        "score":         0,
        "signals":       [],
        "weekly_rsi":    None,
        "daily_rsi":     None,
        "price":         None,
        "weekly_support": None,
        "low_52w":       None,
        "high_52w":      None,
        "pct_above_low": None,
        "data_ts":       0.0,
        "status":        "---",
        "error":         None,
        # Gap metrics — numerical distance to each unfired signal
        "gap": {},
    }

    # ── Weekly data ────────────────────────────────────────────────────────
    weekly = fetch_ohlc(kraken_pair, INTERVAL_WEEK, WEEKLY_CANDLES)
    if weekly is None:
        result["error"] = "no weekly data"
        return result

    wts, wo, wh, wl, wc, wvol = weekly

    if len(wc) < 30:
        result["error"] = "too few weekly bars"
        return result

    # ── Daily data ─────────────────────────────────────────────────────────
    daily = fetch_ohlc(kraken_pair, INTERVAL_DAY, DAILY_CANDLES)
    if daily is None:
        result["error"] = "no daily data"
        return result

    dts, do, dh, dl, dc, dvol = daily

    if len(dc) < 30:
        result["error"] = "too few daily bars"
        return result

    # ── Price ──────────────────────────────────────────────────────────────
    # Use last completed DAILY close for display — more current than weekly.
    # Weekly close lags by up to 6 days when USE_CLOSED_CANDLES strips live bar.
    price = dc[-1]
    result["price"] = price

    # Store data timestamp for staleness display
    try:
        result["data_ts"] = float(dts[-1]) if dts else 0.0
    except Exception:
        result["data_ts"] = 0.0


    # ── 52-week metrics ────────────────────────────────────────────────────
    w52 = wc[-52:] if len(wc) >= 52 else wc
    w52_valid = [v for v in w52 if v > 0]
    result["low_52w"]        = min(w52_valid) if w52_valid else None
    result["high_52w"]       = max(w52_valid) if w52_valid else None
    result["weekly_support"] = min(w52_valid) if w52_valid else None

    # ── SIGNAL 1: Price below 200-period weekly EMA ────────────────────────
    w_ema200 = calc_ema(wc, 200)
    sig1 = False
    if len(wc) >= 200 and w_ema200[-1] > 0:
        # Use weekly close for this signal — keeps it on weekly timeframe
        sig1 = wc[-1] < w_ema200[-1]
    if sig1:
        result["score"] += 1
        result["signals"].append("Below W-EMA200")

    # ── SIGNAL 2: Weekly RSI < 40 and turning up (vs 3 bars ago) ──────────
    w_rsi = calc_rsi(wc, 14)
    cur_wrsi = w_rsi[-1] if w_rsi and w_rsi[-1] > 0 else None
    result["weekly_rsi"] = cur_wrsi

    sig2 = False
    if cur_wrsi is not None and len(w_rsi) >= 5:
        ref = w_rsi[-4] if w_rsi[-4] > 0 else None
        if ref is not None:
            sig2 = (cur_wrsi < 40.0) and (cur_wrsi > ref)
    if sig2:
        result["score"] += 1
        result["signals"].append("W-RSI<40 Turning Up")

    # ── SIGNAL 3: Weekly MACD histogram negative→positive (last 3 bars) ───
    _, _, w_hist = calc_macd(wc, 12, 26, 9)
    sig3 = False
    if len(w_hist) >= 4:
        now_pos = w_hist[-1] > 0
        # Look back up to 8 bars for the negative→positive cross
        # A cross 5-6 bars ago is still a valid recent structural shift
        lookback_hist = w_hist[-8:] if len(w_hist) >= 8 else w_hist
        had_neg = any(h < 0 for h in lookback_hist[:-1])
        # Also accept: currently positive and was negative within 8 bars
        sig3 = had_neg and now_pos
    if sig3:
        result["score"] += 1
        result["signals"].append("W-MACD Hist Crossup")

    # ── SIGNAL 4: Daily RSI bullish divergence (last 60 bars) ─────────────
    d_rsi = calc_rsi(dc, 14)
    cur_drsi = d_rsi[-1] if d_rsi and d_rsi[-1] > 0 else None
    result["daily_rsi"] = cur_drsi

    sig4 = False
    try:
        sig4 = find_bullish_divergence(dc, d_rsi, lookback=60)
    except Exception:
        logging.exception(f"divergence error {kraken_pair}")
    if sig4:
        result["score"] += 1
        result["signals"].append("D-RSI Divergence")

    # ── SIGNAL 5: First weekly higher high after downtrend ───────────────────
    # Current weekly close > prior weekly close, after at least 3 down weeks.
    # This is the first higher high — early confirmation the trend is turning.
    sig5 = False
    if len(wc) >= 5:
        cur_close  = wc[-1]
        prev_close = wc[-2]
        # Check that we had consecutive lower closes before this
        was_downtrend = (wc[-2] < wc[-3]) or (wc[-3] < wc[-4])
        first_up = cur_close > prev_close
        sig5 = first_up and was_downtrend
    if sig5:
        result["score"] += 1
        result["signals"].append("W-First Higher High")

    # ── SIGNAL 6: Volume above average on a green or hammer weekly candle ───
    # High volume on a red candle = distribution. Require green or hammer body
    # to confirm accumulation intent, not just activity.
    w_vol_sma = calc_sma(wvol, 20)
    sig6 = False
    if len(wvol) >= 21 and w_vol_sma[-1] > 0 and len(wo) >= 1 and len(wh) >= 1 and len(wl) >= 1:
        vol_above_avg = wvol[-1] > w_vol_sma[-1]
        candle_open   = wo[-1]
        candle_close  = wc[-1]
        candle_high   = wh[-1]
        candle_low    = wl[-1]
        candle_range  = candle_high - candle_low if candle_high > candle_low else 1
        body_size     = abs(candle_close - candle_open)
        # Green candle: close > open
        is_green = candle_close > candle_open
        # Hammer: small body in upper half, long lower wick (reversal candle)
        lower_wick  = min(candle_open, candle_close) - candle_low
        upper_body  = candle_high - max(candle_open, candle_close)
        is_hammer   = (lower_wick > body_size * 1.5) and (upper_body < body_size * 0.5)
        sig6 = vol_above_avg and (is_green or is_hammer)
    if sig6:
        result["score"] += 1
        result["signals"].append("W-Vol Accumulation")

    # ── SIGNAL 7: Price within 20% of 52-week low ────────────────────────────
    # Only accumulate near actual cycle lows, not just any oversold reading.
    # Within 20% of the 52w low means you are genuinely near the bottom range.
    sig7 = False
    if result["low_52w"] and result["low_52w"] > 0 and price > 0:
        pct_above_low = (price - result["low_52w"]) / result["low_52w"]
        sig7 = pct_above_low <= 0.20
    if result["low_52w"] and result["low_52w"] > 0 and price > 0:
        result["pct_above_low"] = (price - result["low_52w"]) / result["low_52w"] * 100
    if sig7:
        result["score"] += 1
        result["signals"].append("Near 52w Low (<20%)")

    # ── Gap metrics — numerical distance to each unfired signal ──────────────
    gap = {}

    # Gap 1: distance from W-EMA200
    if not sig1 and len(wc) >= 200 and w_ema200[-1] > 0:
        gap["EMA200"] = "+{:.1f}% EMA200".format((wc[-1] / w_ema200[-1] - 1) * 100)

    # Gap 2: RSI below 40 gap + turning up gap
    if not sig2 and cur_wrsi is not None:
        if cur_wrsi >= 40.0:
            gap["RSI_level"] = "{:.1f} n<40 ({:+.0f})".format(
                cur_wrsi, cur_wrsi - 40.0)
        else:
            # Below 40 but still falling — needs to reverse
            ref2 = w_rsi[-4] if len(w_rsi) >= 4 and w_rsi[-4] > 0 else None
            if ref2 is not None:
                delta = cur_wrsi - ref2
                arrow = "↓" if delta < 0 else "↑"
                direction = "falling" if delta < 0 else "up"
                gap["RSI_turn"] = "{:.1f} {}{:+.1f} {}".format(
                    cur_wrsi, arrow, delta,
                    "fall" if direction == "falling" else "up↑")

    # Gap 3: MACD histogram — show value and direction context
    if not sig3 and len(w_hist) >= 1:
        hval = w_hist[-1]
        if hval > 0:
            # Positive but no prior negative found in lookback — cross was too long ago
            gap["MACD"] = "h {:+.4f} stale".format(hval)
        else:
            # Still negative — show distance to zero
            gap["MACD"] = "h {:.4f} need>0".format(hval)

    # Gap 4: divergence — binary, no useful numeric gap
    if not sig4:
        gap["DIV"] = "no divergence yet"

    # Gap 5: higher high gap — how much price needs to rise vs last close
    if not sig5 and len(wc) >= 2:
        needed = wc[-2] - wc[-1]
        if needed > 0:
            gap["HH"] = "+{} higher".format(fmt_price(needed))
        else:
            gap["HH"] = "downtrend not confirmed"

    # Gap 6: volume accumulation — vol ratio and candle color
    if not sig6 and len(wvol) >= 21 and w_vol_sma[-1] > 0:
        vol_ratio = wvol[-1] / w_vol_sma[-1]
        candle_dir = "green" if len(wc) >= 1 and len(wo) >= 1 and wc[-1] > wo[-1] else "red"
        gap["VOL"] = "v{:.0f}% avg {}".format(vol_ratio * 100, candle_dir)

    # Gap 7: % above 52w low
    if not sig7 and result["low_52w"] and result["low_52w"] > 0:
        gap["LOW"] = "+{:.1f}% 52wk low".format(
            (price - result["low_52w"]) / result["low_52w"] * 100)

    result["gap"] = gap

    # ── Status label ───────────────────────────────────────────────────────
    if result["score"] >= MIN_SCORE:
        result["status"] = "BUY"
    elif result["score"] >= 3:
        result["status"] = "WATCH"
    else:
        result["status"] = "---"

    return result

# ─────────────────────────────────────────────────────────────────────────────
#  BTC REGIME
# ─────────────────────────────────────────────────────────────────────────────

def btc_regime():
    """
    Weekly EMA200 slope + price position → BULL / BEAR / UNKNOWN.
    Also fetches BTC daily RSI and monthly RSI for macro context.
    Returns (label, color, daily_rsi, monthly_rsi, btc_daily_danger)
    btc_daily_danger = True when daily RSI < 30 (extreme oversold, knife-catch risk)
    """
    # Weekly regime
    wdata = fetch_ohlc("XXBTZUSD", INTERVAL_WEEK, 210)
    if wdata is None:
        return "UNKNOWN", C["gray"], None, None, False
    _, _, _, _, wc, _ = wdata
    if len(wc) < 202:
        return "UNKNOWN", C["gray"], None, None, False
    w_ema200 = calc_ema(wc, 200)
    if w_ema200[-1] <= 0 or w_ema200[-2] <= 0:
        return "UNKNOWN", C["gray"], None, None, False
    slope = w_ema200[-1] - w_ema200[-2]
    price = wc[-1]
    above = price > w_ema200[-1]
    if above and slope > 0:
        regime_label = "BULL"
        regime_col   = C["green"]
    elif not above or slope < 0:
        regime_label = "BEAR"
        regime_col   = C["red"]
    else:
        regime_label = "NEUTRAL"
        regime_col   = C["gold"]

    # Daily RSI — how oversold is BTC right now on the fast timeframe
    btc_daily_rsi = None
    btc_daily_danger = False
    ddata = fetch_ohlc("XXBTZUSD", INTERVAL_DAY, 720)
    if ddata is not None:
        _, _, _, _, dc, _ = ddata
        if len(dc) >= 15:
            d_rsi = calc_rsi(dc, 14)
            if d_rsi[-1] > 0:
                btc_daily_rsi = d_rsi[-1]
                # Danger: daily RSI < 30 = extreme oversold, high knife-catch risk
                # Counter-intuitively this is also where bounces come from,
                # but for altcoins it means BTC hasn't stabilized yet
                btc_daily_danger = btc_daily_rsi < 30

    # Monthly RSI — derived from weekly closes (sample every 4th bar ≈ monthly)
    btc_monthly_rsi = None
    if len(wc) >= 60:
        # Take every 4th weekly close as a proxy monthly close
        monthly_closes = wc[::4]
        if len(monthly_closes) >= 15:
            m_rsi = calc_rsi(monthly_closes, 14)
            if m_rsi[-1] > 0:
                btc_monthly_rsi = m_rsi[-1]

    return regime_label, regime_col, btc_daily_rsi, btc_monthly_rsi, btc_daily_danger

# ─────────────────────────────────────────────────────────────────────────────
#  PERSISTENT LOG
# ─────────────────────────────────────────────────────────────────────────────

def log_buy_signal(symbol, price, score, signals):
    row = {
        "date":    datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "symbol":  symbol,
        "price":   str(price),
        "score":   str(score),
        "signals": "|".join(signals),
    }
    file_exists = os.path.isfile(LOG_FILE)
    try:
        with open(LOG_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["date","symbol","price","score","signals"])
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
    except Exception:
        logging.exception("Failed to write dca_log.csv")

def load_recent_logs(n=LOG_SHOW):
    if not os.path.isfile(LOG_FILE):
        return []
    try:
        rows = []
        with open(LOG_FILE, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows[-n:]
    except Exception:
        logging.exception("Failed to read dca_log.csv")
        return []

def load_last_alerts():
    """
    Per-symbol timestamp of the most recent logged BUY, read from dca_log.csv.
    Disk is ground truth — restarts must never re-alert or duplicate rows.
    """
    last = {}
    if not os.path.isfile(LOG_FILE):
        return last
    try:
        with open(LOG_FILE, "r") as f:
            for row in csv.DictReader(f):
                sym = (row.get("symbol") or "").strip()
                if not sym:
                    continue
                try:
                    ts = datetime.datetime.strptime(
                        (row.get("date") or "").strip(), "%Y-%m-%d %H:%M")
                except Exception:
                    continue
                if sym not in last or ts > last[sym]:
                    last[sym] = ts
    except Exception:
        logging.exception("load_last_alerts failed")
    return last

# ─────────────────────────────────────────────────────────────────────────────
#  DISPLAY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def term_width():
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 50

def clr():
    sys.stdout.write("\033c")
    sys.stdout.flush()

def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

def fmt_price(p):
    if p is None or not math.isfinite(p):
        return "---"
    if p >= 1000:
        return "${:,.0f}".format(p)
    elif p >= 1:
        return "${:.2f}".format(p)
    else:
        return "${:.4f}".format(p)

def score_bar(score, total=7, width=14):
    filled = int(round(score / total * width))
    bar = "\u2588" * filled + "\u2591" * (width - filled)
    if score >= 6:
        col = C["green"]
    elif score >= 4:
        col = C["gold"]
    else:
        col = C["red"]
    return "{}{}{}".format(col, bar, C["reset"])

# ─────────────────────────────────────────────────────────────────────────────
#  AUDIO ALERT
# ─────────────────────────────────────────────────────────────────────────────

_audio_initialized = False

def play_alert():
    global _audio_initialized
    _audio_initialized = True
    played = False

    # Tier 1: termux-media-player
    try:
        result = subprocess.run(
            ["termux-media-player", "play"],
            timeout=3,
            capture_output=True,
        )
        if result.returncode == 0:
            played = True
    except Exception:
        pass

    # Tier 2: aplay with raw PCM beep
    if not played:
        try:
            import struct
            sample_rate = 8000
            freq        = 880
            duration    = 0.4
            samples     = int(sample_rate * duration)
            data        = b""
            for i in range(samples):
                val = int(127 * math.sin(2 * math.pi * freq * i / sample_rate))
                data += struct.pack("b", val)
            tmp = "/tmp/oracle_ding.raw"
            with open(tmp, "wb") as f:
                f.write(data)
            subprocess.run(
                ["aplay", "-r", str(sample_rate), "-f", "S8", "-c", "1", tmp],
                timeout=3,
                capture_output=True,
            )
            played = True
        except Exception:
            pass

    # Tier 3: terminal bell
    if not played:
        sys.stdout.write("\a")
        sys.stdout.flush()

# ─────────────────────────────────────────────────────────────────────────────
#  RENDER
# ─────────────────────────────────────────────────────────────────────────────

def render(results, regime_label, regime_col, scan_num, prev_buy_set, btc_drsi=None, btc_mrsi=None, btc_danger=False, data_age="?", btc_pulse=None):
    W   = min(term_width(), 60)
    sep = C["gray"] + "\u2500" * W + C["reset"]

    clr()

    # Header
    title = "ORACLE DCA v{}".format(VERSION)
    print("{}{}{} {}  {}{}{}".format(
        C["gold"], C["bold"], title, C["reset"],
        C["gray"], now_str(), C["reset"]
    ))
    print("{} ${:.2f}  weekly+daily{}".format(
        C["gray"], ACCOUNT_BALANCE, C["reset"]
    ))
    print(sep)

    # ── BTC LIVE PULSE ────────────────────────────────────────────────────
    if btc_pulse:
        bp       = btc_pulse
        bp_price = fmt_price(bp["price"])
        bp_chg   = bp["change"]
        bp_pct   = bp["change_pct"]
        chg_col  = C["green"] if bp_chg >= 0 else C["red"]
        chg_sign = "+" if bp_chg >= 0 else ""
        # Distance to key levels
        dist_62k = (bp["price"] - 62858) / 62858 * 100
        dist_57k = (bp["price"] - 57585) / 57585 * 100
        level_62 = "{:+.1f}%".format(dist_62k)
        level_57 = "{:+.1f}%".format(dist_57k)
        print(" {}BTC LIVE{} {} {}{}{}{:.1f}%{}".format(
            C["gold"], C["reset"],
            bp_price,
            chg_col, chg_sign, "", bp_pct, C["reset"],
        ))
        print(" {}24h  H:{} L:{}{}".format(
            C["gray"],
            fmt_price(bp["high_24h"]),
            fmt_price(bp["low_24h"]),
            C["reset"],
        ))
        print(" {}$62k:{} $57k:{}{}".format(
            C["gray"], level_62, level_57, C["reset"]
        ))
    else:
        print(" {}BTC LIVE  unavailable{}".format(C["gray"], C["reset"]))
    print()
    print(sep)

    # BTC regime + daily/monthly RSI dashboard
    regime_suffix = ""
    if regime_label == "BULL":
        regime_suffix = "{}(above & rising W-EMA200){}".format(C["gray"], C["reset"])
    elif regime_label == "BEAR":
        regime_suffix = "{}(below/falling W-EMA200){}".format(C["gray"], C["reset"])
    else:
        regime_suffix = "{}(W-EMA200 neutral){}".format(C["gray"], C["reset"])

    print(" BTC  {}{}{}{}  {}".format(
        regime_col, C["bold"], regime_label, C["reset"], regime_suffix
    ))

    # Daily RSI line with danger warning
    if btc_drsi is not None:
        if btc_drsi < 25:
            drsi_col = C["red"]
            drsi_tag = " ⚠ EXTREME OVERSOLD"
        elif btc_drsi < 35:
            drsi_col = C["orange"]
            drsi_tag = " ⚠ KNIFE-CATCH ZONE"
        elif btc_drsi < 45:
            drsi_col = C["gold"]
            drsi_tag = " recovering"
        else:
            drsi_col = C["silver"]
            drsi_tag = ""
        print(" D-RSI {}{:.0f}{}{}  M-RSI {}{}{}".format(
            drsi_col, btc_drsi, C["reset"],
            C["gray"] + drsi_tag + C["reset"],
            C["silver"],
            "{:.0f}".format(btc_mrsi) if btc_mrsi else "---",
            C["reset"],
        ))

    if btc_danger:
        print(" {}[!] BTC daily RSI < 30 — alt entries carry extra risk{}".format(
            C["orange"], C["reset"]
        ))
    print(sep)

    # Table header
    print("{}{}  SCR  WRSI  DRSI      PRICE  ST{}".format(
        C["silver"], "SYM", C["reset"]
    ))
    print(sep)

    buy_count   = 0
    watch_count = 0
    new_buys    = []

    sorted_results = sorted(
        results,
        key=lambda x: (-(x.get("score") or 0), x.get("symbol", "")),
    )

    for r in sorted_results:
        sym    = r["symbol"]
        score  = r.get("score", 0)
        wrsi   = r.get("weekly_rsi")
        drsi   = r.get("daily_rsi")
        price  = r.get("price")
        status = r.get("status", "---")
        error  = r.get("error")

        if error:
            print("{} {:<4}  ERR  {:<28}{}".format(
                C["gray"], sym, error[:28], C["reset"]
            ))
            continue

        wrsi_s  = "{:.0f}".format(wrsi)  if wrsi  and math.isfinite(wrsi)  else "---"
        drsi_s  = "{:.0f}".format(drsi)  if drsi  and math.isfinite(drsi)  else "---"
        price_s = fmt_price(price)        if price else "---"

        if status == "BUY":
            scol = C["buy"]
            buy_count += 1
            if sym not in prev_buy_set:
                new_buys.append(sym)
        elif status == "WATCH":
            scol = C["watch"]
            watch_count += 1
        else:
            scol = C["nope"]

        # Shorten status for display
        st_short = {"BUY": "BUY", "WATCH": "WCH", "---": "---"}.get(status, status[:3])
        print("{}{:<5}{}/7 {:>4} {:>4} {:>9} {}{}".format(
            scol,
            sym, score,
            wrsi_s, drsi_s, price_s,
            st_short,
            C["reset"],
        ))

    print(sep)

    # Counts
    print(" {}{} BUY{}  {}{} WATCH{}  {}Min {}/7{}".format(
        C["buy"],   buy_count,   C["reset"],
        C["watch"], watch_count, C["reset"],
        C["gray"],  MIN_SCORE,   C["reset"],
    ))
    print(sep)

    # Champion card
    buy_candidates = [r for r in sorted_results if r.get("status") == "BUY"]

    if buy_candidates:
        champ  = buy_candidates[0]
        sym    = champ["symbol"]
        score  = champ["score"]
        price  = champ["price"]
        minqty = champ["min_qty"]
        sup    = champ.get("weekly_support")
        lo52   = champ.get("low_52w")
        hi52   = champ.get("high_52w")
        sigs   = champ.get("signals", [])

        # Tranche sizing: scale entry by conviction score
        if score >= 7:
            tranche_mult  = 2.0
            tranche_label = "FULL (2x min)  — max conviction"
            tranche_col   = C["green"]
        elif score == 6:
            tranche_mult  = 1.0
            tranche_label = "STANDARD (1x min)  — high conviction"
            tranche_col   = C["lime"]
        else:  # score == 5
            tranche_mult  = 0.5
            tranche_label = "HALF (0.5x min)  — starter position"
            tranche_col   = C["gold"]
        tranche_qty = round(minqty * tranche_mult, 8)

        print("{}{} ★ CHAMPION: {}{}".format(C["gold"], C["bold"], sym, C["reset"]))
        print(" {}{}► BUY {}{}".format(tranche_col, C["bold"], tranche_label, C["reset"]))
        print(" {}NO LEVERAGE  ·  NO STOP LOSS  ·  NO TAKE PROFIT{}".format(
            C["gray"], C["reset"]
        ))
        if btc_danger:
            print(" {}⚠  BTC D-RSI < 30 — consider halving the tranche{}".format(
                C["orange"], C["reset"]
            ))
        print(" Qty to buy : {}{} {}{}".format(C["cyan"], tranche_qty, sym, C["reset"]))
        print(" Min order  : {}{} {}{}".format(C["gray"], minqty, sym, C["reset"]))
        print(" Entry      : {}{}{}".format(C["white"], fmt_price(price), C["reset"]))
        if sup:
            print(" W-Support  : {}{}{}".format(C["silver"], fmt_price(sup), C["reset"]))
        if lo52 and hi52:
            pct_abl = champ.get("pct_above_low")
            pct_str = " (+{:.0f}% above low)".format(pct_abl) if pct_abl is not None else ""
            print(" 52w range  : {}{}{} — {}{}{}  {}{}{}".format(
                C["red"],   fmt_price(lo52), C["reset"],
                C["green"], fmt_price(hi52), C["reset"],
                C["gray"], pct_str, C["reset"],
            ))
        print(" Score      : {} {}{}/7{}".format(
            score_bar(score),
            C["gold"], score, C["reset"],
        ))
        print(" Signals:")
        for sig in sigs:
            print("   {}✓{} {}".format(C["lime"], C["reset"], sig))
        print(sep)
    else:
        watch_candidates = [r for r in sorted_results if r.get("status") == "WATCH"]
        if watch_candidates:
            print("{}{} WATCH LIST — missing signals{}".format(
                C["gold"], C["bold"], C["reset"]
            ))
            for wc_r in watch_candidates:
                wsym    = wc_r["symbol"]
                wscore  = wc_r["score"]
                wneed   = MIN_SCORE - wscore
                fired   = set(wc_r.get("signals", []))
                missing = [s for s in ALL_SIGNALS if s not in fired]
                gap     = wc_r.get("gap", {})
                print(" {}{}{:<5}{} {}/7  needs {}:".format(
                    C["watch"], C["bold"], wsym, C["reset"],
                    wscore, wneed,
                ))
                gmap = {
                    "Below W-EMA200":      gap.get("EMA200"),
                    "W-RSI<40 Turning Up": gap.get("RSI_level") or gap.get("RSI_turn"),
                    "W-MACD Hist Crossup": gap.get("MACD"),
                    "D-RSI Divergence":    gap.get("DIV"),
                    "W-First Higher High": gap.get("HH"),
                    "W-Vol Accumulation":  gap.get("VOL"),
                    "Near 52w Low (<20%)": gap.get("LOW"),
                }
                for ms in missing[:3]:
                    gval = gmap.get(ms, "")
                    print("   {}□{} {}".format(C["gray"], C["reset"], ms))
                    if gval:
                        gval_t = gval[:18] if len(gval) > 18 else gval
                        print("     {}→ {}{}".format(C["gray"], gval_t, C["reset"]))
                print()
            print(sep)
        else:
            print("{} No active buy candidates this scan.{}".format(C["gray"], C["reset"]))
            print(sep)


    # ── SETUP PROXIMITY — top 3 NOT YET coins closest to threshold ──────────
    not_yet = [r for r in sorted_results
               if r.get("status") == "---" and not r.get("error") and r.get("score", 0) >= 2]
    if not_yet:
        top3 = not_yet[:3]
        print("{}{} CLOSEST NOT YET{}".format(C["purple"], C["bold"], C["reset"]))
        for ny in top3:
            nysym   = ny["symbol"]
            nyscore = ny["score"]
            fired   = set(ny.get("signals", []))
            missing = [s for s in ALL_SIGNALS if s not in fired]
            gap     = ny.get("gap", {})
            gmap = {
                "Below W-EMA200":      gap.get("EMA200"),
                "W-RSI<40 Turning Up": gap.get("RSI_level") or gap.get("RSI_turn"),
                "W-MACD Hist Crossup": gap.get("MACD"),
                "D-RSI Divergence":    gap.get("DIV"),
                "W-First Higher High": gap.get("HH"),
                "W-Vol Accumulation":  gap.get("VOL"),
                "Near 52w Low (<20%)": gap.get("LOW"),
            }
            print(" {}{:<5}{} {}/7".format(C["silver"], nysym, C["reset"], nyscore))
            for ms in missing[:2]:
                gval = gmap.get(ms, "")
                print("   {}□{} {}".format(C["gray"], C["reset"], ms))
                if gval:
                    gval_t = gval[:18] if len(gval) > 18 else gval
                    print("     {}→ {}{}".format(C["gray"], gval_t, C["reset"]))
            print()
        print(sep)

    # Accumulation log
    logs = load_recent_logs(LOG_SHOW)
    print("{}{} ACCUMULATION LOG \u2014 Last {}{}".format(
        C["purple"], C["bold"], LOG_SHOW, C["reset"]
    ))
    if not logs:
        print("{} No buys logged yet.{}".format(C["gray"], C["reset"]))
    else:
        for row in reversed(logs):
            d  = row.get("date", "---")
            s  = row.get("symbol", "---")
            p  = row.get("price", "---")
            sc = row.get("score", "---")
            try:
                p_fmt = fmt_price(float(p))
            except Exception:
                p_fmt = p
            print(" {}{}{} {}{:<5}{}  {}  {}{}/7{}".format(
                C["gray"], d, C["reset"],
                C["cyan"], s, C["reset"],
                p_fmt,
                C["gold"], sc, C["reset"],
            ))
    print(sep)

    # Footer — show scan number + time + data freshness
    if data_age in ("fresh", "?"):
        age_col = C["green"] if data_age == "fresh" else C["gray"]
    elif "h" in data_age:
        hours = float(data_age.replace("h old","").strip())
        age_col = C["gold"] if hours < 12 else C["orange"]
    else:
        age_col = C["red"]   # days old — stale warning

    print("{} Scan #{}  ·  {}  ·  Data: {}{}{}".format(
        C["gray"], scan_num,
        datetime.datetime.now().strftime("%H:%M"),
        age_col, data_age, C["reset"],
    ))

    return new_buys

# ─────────────────────────────────────────────────────────────────────────────
#  GRACEFUL EXIT
# ─────────────────────────────────────────────────────────────────────────────

def handle_sigint(signum, frame):
    print("\n{} ORACLE DCA \u2014 Session ended. Log saved to {}.{}\n".format(
        C["gold"], LOG_FILE, C["reset"]
    ))
    sys.exit(0)

signal.signal(signal.SIGINT, handle_sigint)

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────

def run(once=False):
    # Persistent alert state — seeded from dca_log.csv, survives restarts.
    # A symbol logged as BUY within the last REALERT_HOURS will not re-alert
    # or write a duplicate CSV row, even across process restarts.
    last_alert = load_last_alerts()
    scan_num   = 0

    logging.info("Oracle DCA v{} started. mode={} refresh={}h realert={}h".format(
        VERSION, "once" if once else "loop", REFRESH_HOURS, REALERT_HOURS))

    while True:
        scan_num += 1

        regime_label = "UNKNOWN"
        regime_col   = C["gray"]
        btc_drsi     = None
        btc_mrsi     = None
        btc_danger   = False
        btc_pulse    = None

        # Collect results
        new_results = []
        for kraken_pair, (display_sym, min_qty) in PAIRS.items():
            try:
                r = analyze_pair(kraken_pair, display_sym, min_qty)
                if r is not None:
                    new_results.append(r)
            except Exception:
                logging.exception("Unhandled error analyzing {}".format(kraken_pair))
                new_results.append({
                    "symbol":        display_sym,
                    "kraken_pair":   kraken_pair,
                    "min_qty":       min_qty,
                    "score":         0,
                    "signals":       [],
                    "weekly_rsi":    None,
                    "daily_rsi":     None,
                    "price":         None,
                    "weekly_support": None,
                    "low_52w":       None,
                    "high_52w":      None,
                    "status":        "---",
                    "error":         "exception",
                })

        # Live BTC pulse — Ticker endpoint, zero cache
        try:
            btc_pulse = fetch_btc_pulse()
        except Exception:
            logging.exception("BTC pulse fetch failed")
            btc_pulse = None

        # BTC regime
        try:
            regime_label, regime_col, btc_drsi, btc_mrsi, btc_danger = btc_regime()
        except Exception:
            logging.exception("BTC regime failed")
            regime_label = "UNKNOWN"
            regime_col   = C["gray"]
            btc_drsi     = None
            btc_mrsi     = None
            btc_danger   = False

        results = new_results

        # ── BUY alert gating — per-symbol cooldown, disk-backed ────────────
        # Log + alert a BUY only if the symbol has no logged BUY within
        # REALERT_HOURS. While a coin sits in BUY for weeks, this yields one
        # alert + one CSV row per cooldown window (DCA cadence), and restarts
        # never produce duplicates because state is read back from the CSV.
        now = datetime.datetime.now()
        fresh_alerts = set()
        current_buys = set()
        for r in results:
            if r.get("status") != "BUY":
                continue
            sym = r["symbol"]
            current_buys.add(sym)
            prev_ts = last_alert.get(sym)
            if prev_ts is not None and (now - prev_ts) < datetime.timedelta(hours=REALERT_HOURS):
                continue
            try:
                log_buy_signal(
                    sym,
                    r.get("price") or 0.0,
                    r.get("score") or 0,
                    r.get("signals") or [],
                )
                last_alert[sym] = now
                fresh_alerts.add(sym)
                logging.info("Logged BUY NOW: {} score={}".format(
                    sym, r.get("score")
                ))
            except Exception:
                logging.exception("Failed to log {}".format(sym))

        # render() highlights BUYs *not* in prev_buy_set — pass the suppressed
        # set so exactly the fresh alerts light up as new.
        suppressed = current_buys - fresh_alerts

        # Compute data age from most recent daily timestamp across all pairs
        data_age_str = "?"
        try:
            timestamps = [r.get("data_ts", 0) for r in results if r.get("data_ts", 0) > 0]
            if timestamps:
                newest_ts  = max(timestamps)
                age_secs   = time.time() - newest_ts
                age_hours  = age_secs / 3600
                if age_hours < 1:
                    data_age_str = "fresh"
                elif age_hours < 24:
                    data_age_str = "{:.0f}h old".format(age_hours)
                else:
                    age_days = age_hours / 24
                    data_age_str = "{:.0f}d old".format(age_days)
        except Exception:
            data_age_str = "?"

        # Render
        try:
            new_buys = render(results, regime_label, regime_col, scan_num, suppressed, btc_drsi, btc_mrsi, btc_danger, data_age_str, btc_pulse)
        except Exception:
            logging.exception("Render failed")
            new_buys = []

        # Audio
        if new_buys:
            try:
                play_alert()
            except Exception:
                logging.exception("Audio alert failed")

        if once:
            print()
            return

        # ── Sleep until next scan, live countdown, Ctrl-C stays responsive ──
        next_at = datetime.datetime.now() + datetime.timedelta(hours=REFRESH_HOURS)
        print(" {}Next scan {}  ·  Ctrl-C exits{}".format(
            C["gray"], next_at.strftime("%H:%M"), C["reset"]
        ))
        remaining = int(REFRESH_HOURS * 3600)
        while remaining > 0:
            h, rem = divmod(remaining, 3600)
            m = rem // 60
            sys.stdout.write("\r {}next scan in {}h{:02d}m {}".format(
                C["gray"], h, m, C["reset"]
            ))
            sys.stdout.flush()
            step = min(60, remaining)
            time.sleep(step)
            remaining -= step
        sys.stdout.write("\r" + " " * 32 + "\r")
        sys.stdout.flush()

# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run(once=("--once" in sys.argv))
