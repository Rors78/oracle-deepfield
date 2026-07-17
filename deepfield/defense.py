"""Liq-buffer defense engine (audit Wave 1). TELEMETRY + ALERT ESCALATION ONLY.

This module computes how far — in PRICE space — the account is from Kraken's
margin-call (ml 80%) and force-liquidation (ml 40%) lines, and drives an
edge-triggered escalation state machine over that buffer. It NEVER places,
cancels, or sizes an order; it reads numbers and returns decisions. See
DEEPFIELD_AUDIT_EVIDENCE.md Sections B and G for why this exists (the bot's
only prior margin awareness was Kraken's lagging `ml` ratio, alerted as 531
identical "seeds/rungs paused" lines in 24h; there was zero price-space liq
telemetry, so the operator learned of near-liquidation from the exchange app).

The unifying law. With e=equity, m=used margin, v=open-position notional (all
from the same live TradeBalance the app loop already polls):

    buffer_call_pct = (e - 0.80*m) / v * 100    # adverse basket move to ml=80%
    buffer_liq_pct  = (e - 0.40*m) / v * 100    # adverse basket move to ml=40%
    eff_leverage    = v / e

At all-pairs-10:1 this reduces to buffer_liq = 1/L - 0.04 and buffer_call =
1/L - 0.08 (L = eff_leverage), but we compute from live m/v so it holds at ANY
leverage mix. A flat book (v=0) is the SAFEST state: buffers = +inf, leverage =
0 — we never divide by zero, and the no-position/default case never maps to a
scarier OR safer number than real data. Unknown equity maps to None (unknown),
which the caller treats as fail-open (never alerts on missing data).
"""
import math
import logging

log = logging.getLogger("deepfield.defense")

CALL_ML = 0.80    # Kraken margin-call threshold (ml <= 80%)
LIQ_ML = 0.40     # Kraken force-liquidation threshold (ml <= 40%)

TIER_NOMINAL = "NOMINAL"
TIER_CAUTION = "CAUTION"
TIER_CRITICAL = "CRITICAL"
TIER_UNKNOWN = "UNKNOWN"

# Re-alert within CRITICAL after the liq buffer decays this many more points
# since the last alert (so a worsening crash keeps paging, not just the entry).
RECRITICAL_DECAY_PCT = 2.0


def compute(e, m, v):
    """Pure buffer math. Returns dict(buffer_call_pct, buffer_liq_pct,
    eff_leverage), or None when equity/margin is unknown or equity is
    non-positive. v<=0 (flat book) -> +inf buffers, 0 leverage: the default,
    no-position case is the safest, never scarier than real data."""
    try:
        e = float(e)
    except (TypeError, ValueError):
        return None
    if not (e > 0):
        return None
    try:
        m = float(m)
    except (TypeError, ValueError):
        return None                       # can't size the buffers honestly -> unknown
    if m < 0:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = 0.0
    if v <= 0:
        return {"buffer_call_pct": math.inf, "buffer_liq_pct": math.inf,
                "eff_leverage": 0.0}
    return {
        "buffer_call_pct": (e - CALL_ML * m) / v * 100.0,
        "buffer_liq_pct": (e - LIQ_ML * m) / v * 100.0,
        "eff_leverage": v / e,
    }


def tier_for(buffer_liq_pct, nominal, critical):
    """NOMINAL >= nominal > CAUTION >= critical > CRITICAL. None/NaN -> UNKNOWN.
    +inf (flat book) -> NOMINAL. nominal is the operator's ml-160 floor expressed
    in price space (ml 160 with all-10:1 == exactly a 12% liq buffer)."""
    if buffer_liq_pct is None:
        return TIER_UNKNOWN
    try:
        b = float(buffer_liq_pct)
    except (TypeError, ValueError):
        return TIER_UNKNOWN
    if math.isnan(b):
        return TIER_UNKNOWN
    if b >= nominal:
        return TIER_NOMINAL
    if b >= critical:
        return TIER_CAUTION
    return TIER_CRITICAL


def step(prev, computed, tier, now):
    """Edge-triggered escalation decision. PURE — no I/O. Returns
    (new_state, events). `prev` is the persisted state dict (or None on first
    run); `now` is a unix timestamp (passed in so this stays deterministic and
    testable). events is a list the caller renders to journal + alert:

        {"kind": "transition", "from": <tier|None>, "to": <tier>, "buffer": b}
        {"kind": "recritical", "buffer": b}   # >=2% further decay within CRITICAL

    UNKNOWN never emits an event (fail open, like every other guard here): the
    prior tier is retained so a momentary missing read doesn't fake a recovery."""
    prev = prev or {}
    prev_tier = prev.get("tier")
    b = computed.get("buffer_liq_pct") if computed else None
    if b is not None and math.isinf(b):
        b = None                          # flat book: no numeric buffer to track
    events = []

    if tier == TIER_UNKNOWN:
        out = dict(prev)
        out["tier"] = prev_tier if prev_tier else TIER_UNKNOWN
        return out, events

    if tier != prev_tier:
        events.append({"kind": "transition", "from": prev_tier, "to": tier, "buffer": b})
        return {"tier": tier, "since": now, "alert_buffer": b}, events

    # unchanged tier: only CRITICAL keeps paging, and only on further decay
    new_state = {"tier": tier, "since": prev.get("since", now),
                 "alert_buffer": prev.get("alert_buffer")}
    if tier == TIER_CRITICAL and b is not None:
        last = prev.get("alert_buffer")
        if last is not None and (last - b) >= RECRITICAL_DECAY_PCT:
            events.append({"kind": "recritical", "buffer": b})
            new_state["alert_buffer"] = b
    return new_state, events


def project_buffer(e, m, v, d_margin=0.0, d_value=0.0):
    """Liq buffer % AFTER shedding d_margin of used margin and d_value of notional
    by closing lots. Equity is ~conserved across a close (realized P&L replaces the
    unrealized that was already in equity; only fees nick it, ignored here as
    second-order). Pure. v-d_value<=0 (fully flat) -> +inf. None on bad e."""
    try:
        e = float(e); m = float(m); v = float(v)
    except (TypeError, ValueError):
        return None
    if not (e > 0):
        return None
    nv = v - d_value
    if nv <= 0:
        return math.inf
    return (e - LIQ_ML * (m - d_margin)) / nv * 100.0


def should_trim(buffer_liq_pct, trigger_pct):
    """The reverse-gear trigger. FAIL-OPEN: None/NaN/inf (unknown or flat book) is
    never a trim — the governor never sells on missing data. Pure."""
    if buffer_liq_pct is None:
        return False
    try:
        b = float(buffer_liq_pct)
    except (TypeError, ValueError):
        return False
    if math.isnan(b) or math.isinf(b):
        return False
    return b < trigger_pct


def plan_trim(lots, e, m, v, target_pct, max_lots):
    """Select whole lots to close to lift the liq buffer to target_pct. `lots` is a
    list of dicts each with 'margin', 'value' (current notional) and 'rank_key'
    (lower = close FIRST; default heuristic uses -value so the largest-notional lot,
    which sheds the most exposure per close, goes first). Greedy in rank order, capped
    at max_lots. Returns (selected_lots, projected_buffer_pct). Pure — the executor
    re-reads live balance after each real close, so this drives ORDER + the test
    oracle, not the live stop condition."""
    ordered = sorted(lots, key=lambda L: L.get("rank_key", -float(L.get("value", 0) or 0)))
    sel, dm, dv = [], 0.0, 0.0
    for L in ordered:
        b = project_buffer(e, m, v, dm, dv)
        if b is not None and not math.isinf(b) and b >= target_pct:
            break
        if len(sel) >= max_lots:
            break
        sel.append(L)
        dm += float(L.get("margin", 0) or 0)
        dv += float(L.get("value", 0) or 0)
    return sel, project_buffer(e, m, v, dm, dv)


def _fmt_pct(x):
    if x is None:
        return "?"
    try:
        if math.isinf(x):
            return "—"
        return f"{x:.1f}%"
    except (TypeError, ValueError):
        return "?"


def format_line(computed, tier):
    """One-line operator health string, e.g.
    'liq buffer 9.3% · call 5.3% · book 7.48x · CAUTION'. Safe on None/inf."""
    if not computed:
        return f"liq buffer ? · {tier or TIER_UNKNOWN}"
    lev = computed.get("eff_leverage")
    lev_s = f"{lev:.2f}x" if isinstance(lev, (int, float)) and not math.isinf(lev) else "?"
    return (f"liq buffer {_fmt_pct(computed.get('buffer_liq_pct'))} · "
            f"call {_fmt_pct(computed.get('buffer_call_pct'))} · "
            f"book {lev_s} · {tier}")
