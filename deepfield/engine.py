"""Scoring engine — pure functions, zero I/O. SPEC §5/§7, invariant 5.

Publishes every number the UI shows. evaluate() runs the seven signals under a
Profile; confirmed uses closed series, provisional appends the forming bar (F13)
with sig6 pace-adjusted (RULINGS Q4). N/A shrinks the denominator, never inflates
(invariant 4). Regime (F9), tranche (F8), and pure freshness/cooldown helpers
(F5/F10) live here too.
"""
import math
import time
from dataclasses import dataclass, field

from . import indicators as ind
from . import signals as sg
from .signals import FIRED, NOT, NA, SignalResult
from .profiles import Profile, FULL
from .config import MIN_RATIO, STRICT_SEVEN, DANGER_DRSI, STALE_SECS, REALERT_HOURS, CONVICTION


@dataclass
class ScoreCard:
    symbol: str
    results: list                      # list[SignalResult], slots 1..7
    score: int
    denom: int
    required: int
    status: str
    weekly_rsi: float = None
    wrsi_ref: float = None             # W-RSI 3 bars back — sig2's turn reference,
                                       # published so the UI can show direction
                                       # without re-implementing math (invariant 5)
    daily_rsi: float = None
    price: float = None
    low_52w: float = None
    high_52w: float = None
    pct_above_low: float = None
    gap: dict = field(default_factory=dict)
    provisional: bool = False

    @property
    def fired(self):
        return [r.name for r in self.results if r.state == FIRED]

    @property
    def na(self):
        return [(r.name, r.reason) for r in self.results if r.state == NA]


def _required(denom, profile):
    if profile.f3 and not STRICT_SEVEN:
        return max(2, round(MIN_RATIO * denom))
    return 5  # v4.4 fixed MIN_SCORE / STRICT_SEVEN


def evaluate(symbol, weekly, daily, profile=FULL, provisional=False, elapsed_fraction=None):
    """weekly = (wo, wh, wl, wc, wvol); daily = (dc,). For provisional, the caller
    has already appended the forming bar to these series."""
    wo, wh, wl, wc, wvol = weekly
    (dc,) = daily

    # Indicators computed once (matches v4.4's single inline computation).
    w_ema200 = ind.calc_ema(wc, 200)
    w_rsi = ind.calc_rsi(wc, 14)
    _, _, w_hist = ind.calc_macd(wc, 12, 26, 9)
    d_rsi = ind.calc_rsi(dc, 14)
    w_vol_sma = ind.calc_sma(wvol, 20)

    price = dc[-1] if dc else 0.0
    w52 = [v for v in wc[-52:] if v > 0] if wc else []
    low_52w = min(w52) if w52 else None
    high_52w = max(w52) if w52 else None

    results = [
        sg.sig1_below_wema200(wc, w_ema200, profile),
        sg.sig2_wrsi_turning_up(w_rsi, profile),
        sg.sig3_macd_crossup(w_hist, profile),
        sg.sig4_drsi_divergence(dc, d_rsi, profile),
        sg.sig5_first_up_close(wc, profile),
        _sig6(wo, wh, wl, wc, wvol, w_vol_sma, profile, provisional, elapsed_fraction),
        sg.sig7_near_52w_low(price, low_52w, profile),
    ]

    score = sum(1 for r in results if r.state == FIRED)
    denom = sum(1 for r in results if r.state != NA)
    required = _required(denom, profile)
    if score >= required:
        status = "BUY"
    elif score >= 3:
        status = "WATCH"
    else:
        status = "---"

    cur_wrsi = w_rsi[-1] if w_rsi and w_rsi[-1] > 0 else None
    wrsi_ref = w_rsi[-4] if len(w_rsi) >= 4 and w_rsi[-4] > 0 else None
    cur_drsi = d_rsi[-1] if d_rsi and d_rsi[-1] > 0 else None
    pct_above_low = ((price - low_52w) / low_52w * 100) if (low_52w and low_52w > 0 and price > 0) else None

    return ScoreCard(
        symbol=symbol, results=results, score=score, denom=denom, required=required,
        status=status, weekly_rsi=cur_wrsi, wrsi_ref=wrsi_ref, daily_rsi=cur_drsi, price=price,
        low_52w=low_52w, high_52w=high_52w, pct_above_low=pct_above_low,
        gap=_gap_metrics(results, wc, w_ema200, w_rsi, w_hist, w_vol_sma, wvol, wo, price, low_52w),
        provisional=provisional,
    )


def _sig6(wo, wh, wl, wc, wvol, w_vol_sma, profile, provisional, elapsed_fraction):
    """sig6 with provisional pace-adjust (RULINGS Q4). Confirmed = verbatim."""
    if not provisional:
        return sg.sig6_vol_accumulation(wo, wh, wl, wc, wvol, w_vol_sma, profile)
    # Provisional: forming bar is the last element.
    if elapsed_fraction is None or elapsed_fraction < 0.15:
        r = SignalResult(6, sg._name(6, profile), NA, "provisional: <15% elapsed (~)")
        return r
    adj = wvol[:-1] + [wvol[-1] / elapsed_fraction]
    adj_sma = ind.calc_sma(adj, 20)
    return sg.sig6_vol_accumulation(wo, wh, wl, wc, adj, adj_sma, profile)


def _gap_metrics(results, wc, w_ema200, w_rsi, w_hist, w_vol_sma, wvol, wo, price, low_52w):
    """Numeric distance to each unfired signal. 'n<40' typo fixed -> 'need<40'."""
    by_slot = {r.slot: r for r in results}
    gap = {}
    r1 = by_slot[1]
    if r1.state == NOT and len(wc) >= 200 and w_ema200[-1] > 0:
        gap[1] = "+{:.1f}% EMA200".format((wc[-1] / w_ema200[-1] - 1) * 100)
    r2 = by_slot[2]
    if r2.state == NOT and w_rsi and w_rsi[-1] > 0:
        cur = w_rsi[-1]
        if cur >= 40.0:
            gap[2] = "{:.1f} need<40 ({:+.0f})".format(cur, cur - 40.0)
        elif len(w_rsi) >= 4 and w_rsi[-4] > 0:
            gap[2] = "{:.1f} {:+.1f} vs -4".format(cur, cur - w_rsi[-4])
    r3 = by_slot[3]
    if r3.state == NOT and w_hist:
        h = w_hist[-1]
        gap[3] = "h {:+.4f} {}".format(h, "stale" if h > 0 else "need>0")
    if by_slot[4].state == NOT:
        gap[4] = "no divergence yet"
    r5 = by_slot[5]
    if r5.state == NOT and len(wc) >= 2:
        need = wc[-2] - wc[-1]
        gap[5] = ("+{:.4g} higher".format(need) if need > 0 else "downtrend unconfirmed")
    r6 = by_slot[6]
    if r6.state == NOT and len(wvol) >= 21 and w_vol_sma[-1] > 0:
        ratio = wvol[-1] / w_vol_sma[-1]
        color = "green" if (wc and wo and wc[-1] > wo[-1]) else "red"
        gap[6] = "v{:.0f}% avg {}".format(ratio * 100, color)
    if by_slot[7].state == NOT and low_52w and low_52w > 0 and price > 0:
        gap[7] = "+{:.1f}% 52wk low".format((price - low_52w) / low_52w * 100)
    return gap


# ── Regime (F9) ─────────────────────────────────────────────────────────────
def monthly_closes(wc, end_anchored=True):
    """F4: sample every 4th weekly close. End-anchored keeps the latest weekly
    close as the final monthly sample; v4.4's start-anchored dropped up to 3."""
    return wc[(len(wc) - 1) % 4::4] if end_anchored else wc[::4]


@dataclass
class Regime:
    label: str
    daily_rsi: float = None
    monthly_rsi: float = None
    danger: bool = False


def regime(btc_weekly_closes, btc_daily_closes, profile=FULL):
    wc = btc_weekly_closes
    dc = btc_daily_closes
    ema = ind.calc_ema(wc, 200)

    label = "UNKNOWN"
    if profile.f9:
        if len(wc) >= 204 and ema[-1] > 0 and ema[-5] > 0:
            slope = ema[-1] - ema[-5]
            above = wc[-1] > ema[-1]
            if above and slope > 0:
                label = "BULL"
            elif not above:
                label = "BEAR"
            else:
                label = "RECOVERY"
    else:
        if len(wc) >= 202 and ema[-1] > 0 and ema[-2] > 0:
            slope = ema[-1] - ema[-2]
            above = wc[-1] > ema[-1]
            if above and slope > 0:
                label = "BULL"
            elif (not above) or slope < 0:
                label = "BEAR"
            else:
                label = "NEUTRAL"

    d_rsi = ind.calc_rsi(dc, 14)
    drsi = d_rsi[-1] if d_rsi and d_rsi[-1] > 0 else None
    danger = drsi is not None and drsi < DANGER_DRSI

    mrsi = None
    if len(wc) >= 60:
        monthly = monthly_closes(wc, profile.f4)
        if len(monthly) >= 15:
            m_rsi = ind.calc_rsi(monthly, 14)
            if m_rsi and m_rsi[-1] > 0:
                mrsi = m_rsi[-1]
    return Regime(label=label, daily_rsi=drsi, monthly_rsi=mrsi, danger=danger)


# ── Tranche (F8) ────────────────────────────────────────────────────────────
def _ceil_lot(x, lot_decimals):
    ld = 8 if lot_decimals is None else lot_decimals
    f = 10 ** ld
    return math.ceil(x * f) / f


def tranche(score, required, ordermin, costmin, lot_decimals, price):
    """Q2 rule: base=max(ordermin, ceil(costmin/price)); *conviction; quantize up;
    assert qty>=ordermin AND qty*price>=costmin. Returns (qty, mult)."""
    if price <= 0:
        return 0.0, 0.0
    base = max(ordermin, _ceil_lot(costmin / price, lot_decimals))
    delta = max(0, score - required)
    mult = CONVICTION.get(min(delta, 2), 1.0)
    qty = _ceil_lot(base * mult, lot_decimals)
    assert qty >= ordermin, f"qty {qty} < ordermin {ordermin}"
    assert qty * price >= costmin - 1e-9, f"qty*price {qty*price} < costmin {costmin}"
    return qty, mult


# ── Freshness (F5) & cooldown (F10) — pure predicates ───────────────────────
def candle_close_age(bar_open_ts, interval_min, now=None):
    """Seconds since the bar CLOSED (not opened). F5."""
    now = time.time() if now is None else now
    return now - (bar_open_ts + interval_min * 60)


def is_stale(tick_age, stale_secs=STALE_SECS):
    return tick_age > stale_secs


def should_alert(last_alert_ts, now, realert_hours=REALERT_HOURS):
    """F10: re-alert only after REALERT_HOURS. Unix seconds; None = never alerted."""
    if last_alert_ts is None:
        return True
    return (now - last_alert_ts) >= realert_hours * 3600
