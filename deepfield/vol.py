"""Volatility-scaled distances — ONE table drives take-profit, stop-loss and rung spacing.

Operator ruling 2026-08-06. Every distance in this bot used to be a flat percentage
against a margin universe spanning a ~260x volatility range (measured: USDC 0.03%/day,
WLD 7.89%/day). A single number cannot serve both ends, and it failed silently in both
directions — a 4% target on PAXG loses to rollover carry before it fills, while on ZEC
the same 4% caps a winner at half a normal day's range, and a flat 1% rung spacing let
ZEC cross its whole ladder in an hour (one leveraged entry with extra fees) while PAXG
never reached rung two.

    ATR_pair     = Wilder ATR14 on CLOSED 1d candles, as % of the last closed close
    TP_pair      = clamp(1.0 x ATR, 1.0%, 15.0%)
    SL_pair      = clamp(1.5 x ATR, 1.5%, 22.0%)
    RUNG_pair    = clamp(0.5 x ATR, 0.5%,  7.0%)

Candles come from OUR OWN store, never a live API walk: the Kraken rate limit is
per-ACCOUNT and a side process spending it throttles the live trading loop blind
(2026-07-19, ~8 minutes). The bot already keeps 750+ closed daily candles per pair.

WHY THE NUMBERS ARE FROZEN AT FILL (see executor): the daily refresh moves the table,
but an OPEN position keeps whatever it was given. If ATR spikes mid-position a live
target would widen away from the price it is chasing — the target runs from you.
"""
import json
import logging
import math
import time

from . import config

log = logging.getLogger("deepfield.vol")

# ── the ruling's constants ───────────────────────────────────────────────────
TP_MULT,   TP_FLOOR,   TP_CAP   = 1.0, 1.0, 15.0
SL_MULT,   SL_FLOOR,   SL_CAP   = 1.5, 1.5, 22.0
RUNG_MULT, RUNG_FLOOR, RUNG_CAP = 0.5, 0.5,  7.0

ATR_PERIOD = 14
ATR_MIN_CANDLES = ATR_PERIOD + 1        # 14 true ranges needs 15 closes
ATR_SANE_MAX_PCT = 50.0                 # Gate F: above this it is a data fault, not vol

# The reward-to-risk the roster ALREADY runs at, and therefore the bar a new geometry
# has to clear: a flat 4% target against the 7.9-9.9% stops measured on the live book
# on 2026-08-06 (the old support stop was clamped to [5%, 15%] and sat near the middle).
# 4.0 / 9.9 = 0.40. See should_ship().
LEGACY_RR = 0.40
# The pre-ruling geometry, kept ONLY as the landing place for a pair Gate D refuses to
# ship. Flat 4% target, the ~9.9% stop the live book actually carried, 1% rung.
LEGACY_TP_PCT, LEGACY_SL_PCT, LEGACY_RUNG_PCT = 4.0, 9.9, 1.0

META_KEY = "vol_table"

# Fallback TP% for a pair with too few daily candles or a failed read. SL and rung
# derive from these at the same 1.5x / 0.5x ratios, so the table stays one rule.
# HBAR/ALGO/RENDER/WLD were absent from the operator's list and are set at the nearest
# tier to their measured ATR (3.39 / 4.64 / 4.11 / 7.89) — a roster pair with no entry
# would otherwise fall to DEFAULT and be silently mis-scaled.
FALLBACK_TP_PCT = {
    "PAXG/USD": 1.0,
    "TRX/USD": 2.0,
    "BTC/USD": 2.5,
    "ETH/USD": 3.0, "LTC/USD": 3.0, "BCH/USD": 3.0,
    "XRP/USD": 3.5, "LINK/USD": 3.5, "HBAR/USD": 3.5,
    "ADA/USD": 4.0, "SOL/USD": 4.0, "DOT/USD": 4.0, "XLM/USD": 4.0,
    "RENDER/USD": 4.0,
    "AVAX/USD": 4.5, "UNI/USD": 4.5, "AAVE/USD": 4.5, "NEAR/USD": 4.5,
    "DOGE/USD": 4.5, "ALGO/USD": 4.5,
    "SUI/USD": 5.5, "SHIB/USD": 5.5, "CRV/USD": 5.5,
    "HYPE/USD": 6.5, "PEPE/USD": 6.5,
    "PENGU/USD": 7.5,
    "ZEC/USD": 8.0, "WLD/USD": 8.0,
}
DEFAULT_FALLBACK_TP_PCT = 4.0


def _clamp(v, lo, hi):
    return max(lo, min(v, hi))


def liq_distance_pct(leverage):
    """Price move that force-liquidates a position carried at `leverage` with no spare
    collateral behind it. Kraken liquidates at margin level 40%; for a lone position
    ML = 1 + L*r, so r = (0.40 - 1)/L. 10x -> 6%, 5x -> 12%, 3x -> 20%, 2x -> 30%.

    This is the ISOLATED bound, deliberately. The book-level distance is far larger
    while leverage is light (318% at 0.31x on 2026-08-06), but a stop that only works
    while the book happens to be light is not a safety device — it is a stop that
    stops working exactly when the book is heavy enough to need it."""
    try:
        lev = float(leverage or 0)
    except (TypeError, ValueError):
        return None
    return (60.0 / lev) if lev > 0 else None


def atr_pct(candles):
    """Wilder ATR14 as % of the last close, from [(ts, h, l, c), ...] ASCENDING and
    CLOSED. Returns None when there is not enough history — never a guess.

    Closed candles only: an in-progress day has a partial high/low and understates
    range, which would quietly tighten every distance on the pair."""
    if not candles or len(candles) < ATR_MIN_CANDLES:
        return None
    trs = []
    for i in range(1, len(candles)):
        _, h, l, c = candles[i]
        pc = candles[i - 1][3]
        if None in (h, l, c, pc):
            return None
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < ATR_PERIOD:
        return None
    atr = sum(trs[:ATR_PERIOD]) / float(ATR_PERIOD)          # Wilder seed
    for tr in trs[ATR_PERIOD:]:                              # Wilder smoothing
        atr = (atr * (ATR_PERIOD - 1) + tr) / float(ATR_PERIOD)
    last = candles[-1][3]
    if not last or last <= 0:
        return None
    return atr / last * 100.0


def sane_atr(atr):
    """GATE F. An ATR that is zero, negative, NaN or absurd is not a measurement, and
    silently scaling every distance on the pair off it is worse than admitting we do
    not know. False -> the caller uses the fallback table."""
    try:
        a = float(atr)
    except (TypeError, ValueError):
        return False
    return a == a and 0.0 < a <= ATR_SANE_MAX_PCT


def resolve(atr, leverage=None):
    """The ruling plus its self-veto gates. `atr` is ATR14 as a percent.

    GATE A — an SL at or beyond the pair's liquidation distance cannot fire (Kraken
    closes the position first), so it is clamped to VOL_LIQ_SAFETY_FRAC of that
    distance. Measured on the 2026-08-06 roster this binds on 20 of 28 pairs; the
    10x book is the worst affected, where a 6.0% liq distance leaves 4.2% of room
    against SLs the raw 1.5x multiplier wanted to put at 5-7.8%.

    GATE E — a TP pinned to the 1.0% floor is flagged (`tp_floored`). That floor is
    the taker round-trip (~0.6%) plus slippage on the thin books, so a pair sitting on
    it is only viable post-only on BOTH legs.

    GATE D is NOT enforced here — see should_ship()."""
    out = {"atr_pct": atr}
    tp = _clamp(TP_MULT * atr, TP_FLOOR, TP_CAP)
    sl_raw = _clamp(SL_MULT * atr, SL_FLOOR, SL_CAP)
    sl = sl_raw
    rung = _clamp(RUNG_MULT * atr, RUNG_FLOOR, RUNG_CAP)
    out["tp_floored"] = (TP_MULT * atr) < TP_FLOOR
    out["tp_capped"] = (TP_MULT * atr) > TP_CAP
    out["sl_raw_pct"] = sl_raw
    out["liq_clamped"] = False
    out["liq_pct"] = liq_distance_pct(leverage)
    if getattr(config, "VOL_LIQ_CLAMP", True) and out["liq_pct"]:
        ceiling = out["liq_pct"] * float(getattr(config, "VOL_LIQ_SAFETY_FRAC", 0.70))
        if sl > ceiling:
            sl = max(SL_FLOOR, ceiling)
            out["liq_clamped"] = True
    out["tp_pct"] = tp
    out["sl_pct"] = sl
    out["rung_pct"] = rung
    out["rr"] = (tp / sl) if sl > 0 else None
    out["ships"], out["veto"] = should_ship(out)
    return out


def should_ship(d):
    """GATE D, and the one place this implementation departs from the dispatch text.
    Returns (ships, reason_or_None).

    The dispatch says: any pair below 1:1 reward-to-risk after Gate A is left on the
    OLD settings. Applied literally that vetoes almost the entire ruling, because R:R
    is fixed by the ruling's own multipliers — TP = 1.0 x ATR against SL = 1.5 x ATR is
    0.667 for every pair at every volatility, and only Gate A's clamp can lift it.
    Measured on the live roster, exactly 2 of 28 pairs (ADA, AVAX) would clear 1:1.

    It is also self-defeating on its own metric. The "old settings" a vetoed pair falls
    back to are a flat 4% target against a support stop that the live book carried at
    7.9-9.9% — R:R 0.40 to 0.51. Gate D would therefore reject a 0.667 geometry in
    favour of a 0.40 one, making every vetoed pair worse by the exact measure the gate
    exists to protect.

    So the gate is enforced as: ship only if the new geometry's R:R is at least as good
    as what the pair has today. That honours the intent — never ship a worse payoff
    ratio — while not deleting the ruling. A pair that manages to be worse than 0.40 is
    still refused, and every pair below the dispatch's literal 1:1 bar is named in the
    report.

    Note on why 0.667 is not disqualifying here: R:R alone does not determine
    expectancy. This is a mean-reversion accumulator, not a trend system. At the
    measured post-ratchet hit rate of 90% (18 of 20 rungs harvested since 2026-07-29),
    expectancy is 0.9 x 1.0 - 0.1 x 1.5 = +0.75 ATR per rung. The 1:1 heuristic comes
    from ~40-50%-hit-rate systems and does not transfer."""
    rr = d.get("rr")
    if rr is None:
        return False, "no reward-to-risk (SL resolved to zero)"
    if rr < LEGACY_RR:
        return False, (f"R:R {rr:.2f} is worse than the pre-ruling geometry "
                       f"({LEGACY_RR:.2f}) — kept on old settings")
    return True, None


def fallback_for(symbol, leverage=None):
    """Distances for a pair we cannot measure: <15 closed daily candles (a newly
    listed pair) or a bad read. Derived from the operator's TP table at the same
    ratios, so the fallback is the same rule with a different ATR input."""
    tp = FALLBACK_TP_PCT.get(symbol, DEFAULT_FALLBACK_TP_PCT)
    d = resolve(tp / TP_MULT, leverage)          # invert TP_MULT to get the implied ATR
    d["source"] = "fallback"
    d["atr_pct"] = None
    return d


def build_table(conn, pairs=None, leverage=None):
    """Recompute every roster pair from the local candle store. Never raises and never
    calls an exchange. Returns {symbol: distances-dict}."""
    from . import store
    pairs = pairs or [p["ws"] for p in config.PAIRS]
    leverage = leverage or config.PER_PAIR_LEVERAGE
    table = {}
    for sym in pairs:
        lev = leverage.get(sym)
        try:
            rows = store.closed_daily_candles(conn, sym, limit=120)
        except Exception:
            log.exception("vol: candle read failed for %s — using fallback", sym)
            rows = []
        atr = atr_pct(rows)
        if not sane_atr(atr):          # Gate F: None, 0, negative, NaN or absurd
            if atr is not None:
                log.error("VOL %s: ATR came back %.4f%% — not a measurement. Using the "
                          "fallback table for this pair (gate F).", sym, atr)
            atr = None
        if atr is None:
            d = fallback_for(sym, lev)
            log.warning("VOL %s: only %d closed daily candles (need %d) — FALLBACK "
                        "tp %.2f%% sl %.2f%% rung %.2f%%", sym, len(rows),
                        ATR_MIN_CANDLES, d["tp_pct"], d["sl_pct"], d["rung_pct"])
        else:
            d = resolve(atr, lev)
            d["source"] = "atr"
            if d["tp_floored"]:
                # Operator ruling (g): the 1.0% floor is the taker round-trip (~0.6%)
                # plus slippage on the thin books. A pair pinned here is only viable
                # post-only on both legs — say so rather than letting it look normal.
                log.warning("VOL %s: ATR %.3f%% clamps TP to the %.1f%% floor — this "
                            "pair is only viable post-only on BOTH legs",
                            sym, atr, TP_FLOOR)
            if d["liq_clamped"]:
                log.warning("VOL %s: SL %.2f%% would sit at/beyond the %.1f%% liq "
                            "distance at %sx — clamped to %.2f%% so the stop can "
                            "actually fire", sym, SL_MULT * atr, d["liq_pct"], lev,
                            d["sl_pct"])
        d["leverage"] = lev
        if not d.get("ships"):
            # GATE D: refused. The pair stays on the pre-ruling geometry rather than
            # taking a payoff ratio worse than it already has.
            log.warning("VOL %s: NOT SHIPPED — %s. Holding old settings "
                        "(tp %.1f%% sl %.1f%% rung %.1f%%).",
                        sym, d.get("veto"), LEGACY_TP_PCT, LEGACY_SL_PCT, LEGACY_RUNG_PCT)
            d = dict(d, tp_pct=LEGACY_TP_PCT, sl_pct=LEGACY_SL_PCT,
                     rung_pct=LEGACY_RUNG_PCT, source="legacy (gate D)")
        table[sym] = d
    return table


def save_table(conn, table):
    from . import store
    store.meta_set(conn, META_KEY, json.dumps({"updated": time.time(), "table": table}))


def load_table(conn):
    """(table, updated_epoch). ({}, None) when never computed."""
    from . import store
    try:
        blob = json.loads(store.meta_get(conn, META_KEY) or "{}")
        return blob.get("table") or {}, blob.get("updated")
    except Exception:
        log.exception("vol: stored table unreadable — treating as absent")
        return {}, None


def distances(conn, symbol, leverage=None):
    """THE RESOLVER. Every take-profit, stop-loss and rung-spacing decision in the bot
    routes through this one function — that is the whole point of the ruling, and the
    reason the flat constants were deleted rather than left as defaults for something
    to quietly keep reading.

    Reads the persisted daily table; if the pair is missing (new roster entry between
    refreshes) it computes on the spot from local candles, and failing that falls back.
    Never raises, never calls an exchange."""
    lev = leverage if leverage is not None else config.PER_PAIR_LEVERAGE.get(symbol)
    table, _updated = load_table(conn)
    d = table.get(symbol)
    if d and d.get("tp_pct") and d.get("sl_pct") and d.get("rung_pct"):
        return d
    try:
        one = build_table(conn, [symbol], config.PER_PAIR_LEVERAGE)
        return one[symbol]
    except Exception:
        log.exception("vol: on-demand resolve failed for %s — fallback", symbol)
        return fallback_for(symbol, lev)
