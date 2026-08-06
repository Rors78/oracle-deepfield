"""Volatility-scaled distances: the resolver, its clamps, and its self-veto gates.

Operator ruling 2026-08-06. One ATR table drives take-profit, stop-loss and rung
spacing, replacing three flat constants that could not serve a roster spanning a 260x
volatility range (measured: USDC 0.03%/day, WLD 7.89%/day).

The tests the dispatch asked for explicitly — clamp behaviour at BOTH bounds on all
three distances, a clean fallback when the candle read fails, a pair with 3 candles,
and an ATR spike mid-position asserting the stored targets do not move — plus the
gates, because a gate that silently stops firing is worse than no gate.
"""
import json
import math

import pytest

from .conftest import pin_vol
from deepfield import config, store, vol, executor as ex_mod

SYM = "BTC/USD"


def _conn(tmp_path):
    conn = store.connect(str(tmp_path / "v.db"))
    store.upsert_pair(conn, "XXBTZUSD", SYM, "BTC", 0.00005, 0.5, 8)
    return conn


def _candles(n, high=101.0, low=99.0, close=100.0, start=1_700_000_000):
    """n CLOSED daily candles with a constant true range of (high-low)."""
    return [(start + i * 86400, high, low, close) for i in range(n)]


# ── ATR itself ───────────────────────────────────────────────────────────────

def test_atr_is_wilder_and_expressed_against_the_last_close():
    """A flat 2-point range on a 100 close is 2% ATR. Wilder smoothing over constant
    true ranges converges on the range itself, so the arithmetic is checkable by hand."""
    assert vol.atr_pct(_candles(60, 101.0, 99.0, 100.0)) == pytest.approx(2.0, rel=1e-6)


def test_atr_needs_fifteen_closes_and_says_so_rather_than_guessing():
    """14 true ranges need 15 closes. A pair with 3 candles (the dispatch's case) has
    no measurement — returning a small number here would scale every distance on a
    newly listed pair off noise."""
    for n in (0, 1, 3, 14):
        assert vol.atr_pct(_candles(n)) is None, f"{n} candles must not produce an ATR"
    assert vol.atr_pct(_candles(15)) is not None


def test_atr_ignores_an_unclosed_day(tmp_path):
    """store.closed_daily_candles filters closed=1. An in-progress day carries a
    partial high/low, which understates range and would quietly tighten every distance
    on the pair for the rest of the session."""
    conn = _conn(tmp_path)
    for i in range(20):
        store.upsert_candle(conn, SYM, 1440, 1_700_000_000 + i * 86400,
                            100.0, 101.0, 99.0, 100.0, 1.0, 1)
    store.upsert_candle(conn, SYM, 1440, 1_700_000_000 + 20 * 86400,
                        100.0, 100.01, 99.99, 100.0, 1.0, 0)       # forming, tiny range
    conn.commit()
    rows = store.closed_daily_candles(conn, SYM)
    assert all(r[1] - r[2] == pytest.approx(2.0) for r in rows), "unclosed day leaked in"
    conn.close()


# ── clamps, at BOTH bounds, on all three distances ───────────────────────────

def test_clamps_at_the_lower_bound():
    d = vol.resolve(0.02, leverage=10)              # USDC-like peg
    assert d["tp_pct"] == vol.TP_FLOOR == 1.0
    assert d["sl_pct"] == vol.SL_FLOOR == 1.5
    assert d["rung_pct"] == vol.RUNG_FLOOR == 0.5
    assert d["tp_floored"] is True                  # gate E flag


def test_clamps_at_the_upper_bound():
    d = vol.resolve(40.0, leverage=2)               # far past every cap
    assert d["tp_pct"] == vol.TP_CAP == 15.0
    assert d["rung_pct"] == vol.RUNG_CAP == 7.0
    assert d["sl_raw_pct"] == vol.SL_CAP == 22.0
    assert d["tp_capped"] is True


def test_between_the_bounds_the_multipliers_are_exact():
    d = vol.resolve(4.0, leverage=None)             # no leverage -> no liq clamp
    assert d["tp_pct"] == pytest.approx(4.0)
    assert d["sl_pct"] == pytest.approx(6.0)
    assert d["rung_pct"] == pytest.approx(2.0)


# ── GATE A: the stop must be able to fire ────────────────────────────────────

def test_gate_a_clamps_a_stop_that_sits_beyond_liquidation():
    """At 10x a position liquidates ~6% against it (ML = 1 + L*r, liq at ML 40%). A
    1.5 x ATR stop on a 5.22%-ATR pair wants 7.83% — past the point where Kraken has
    already closed the position, so the stop is decorative."""
    assert vol.liq_distance_pct(10) == pytest.approx(6.0)
    d = vol.resolve(5.22, leverage=10)
    assert d["sl_raw_pct"] == pytest.approx(7.83)
    assert d["liq_clamped"] is True
    assert d["sl_pct"] == pytest.approx(6.0 * config.VOL_LIQ_SAFETY_FRAC)
    assert d["sl_pct"] < d["liq_pct"], "a clamped stop must still be inside liquidation"


def test_gate_a_leaves_a_safe_stop_alone():
    d = vol.resolve(2.44, leverage=10)              # BTC: 3.66% vs 6.0% available
    assert d["liq_clamped"] is False
    assert d["sl_pct"] == pytest.approx(3.66)


def test_gate_a_can_be_disarmed(monkeypatch):
    """One flag, so the operator can ship the raw 1.5x and accept decorative stops."""
    monkeypatch.setattr(config, "VOL_LIQ_CLAMP", False)
    d = vol.resolve(5.22, leverage=10)
    assert d["liq_clamped"] is False and d["sl_pct"] == pytest.approx(7.83)


def test_gate_a_raises_reward_to_risk_rather_than_lowering_it():
    """The clamp tightens the STOP, never the target, so it can only improve R:R.
    Worth pinning because it is the only thing that lifts any pair off the ruling's
    structural 0.667."""
    unclamped = vol.resolve(2.44, leverage=10)["rr"]
    clamped = vol.resolve(5.22, leverage=10)["rr"]
    assert unclamped == pytest.approx(2 / 3, rel=1e-3)
    assert clamped > 1.0


# ── GATE D: never ship a worse payoff ratio than the pair already has ────────

def test_gate_d_ships_the_rulings_own_geometry():
    """TP = 1.0 x ATR against SL = 1.5 x ATR is 0.667 — below the dispatch's literal
    1:1 bar, but well above the 0.40 the pre-ruling flat geometry actually ran at
    (4% target, ~9.9% measured stop). Shipping is the outcome that improves the ratio;
    the literal reading would have reverted 26 of 28 pairs to something worse."""
    d = vol.resolve(3.0, leverage=None)
    assert d["rr"] == pytest.approx(2 / 3, rel=1e-3)
    assert d["rr"] > vol.LEGACY_RR
    assert d["ships"] is True and d["veto"] is None


def test_gate_d_refuses_a_geometry_worse_than_the_status_quo(monkeypatch):
    """The gate has to be able to fire, or it is decoration. Widen SL far past TP and
    the pair must fall back to the old settings rather than ship."""
    monkeypatch.setattr(vol, "SL_MULT", 12.0)
    monkeypatch.setattr(config, "VOL_LIQ_CLAMP", False)
    d = vol.resolve(1.5, leverage=None)
    assert d["rr"] < vol.LEGACY_RR
    assert d["ships"] is False and "old settings" in d["veto"]


def test_a_vetoed_pair_lands_on_the_legacy_geometry(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    for i in range(30):
        store.upsert_candle(conn, SYM, 1440, 1_700_000_000 + i * 86400,
                            100.0, 101.5, 98.5, 100.0, 1.0, 1)
    conn.commit()
    monkeypatch.setattr(vol, "SL_MULT", 12.0)
    monkeypatch.setattr(config, "VOL_LIQ_CLAMP", False)
    t = vol.build_table(conn, [SYM], {SYM: 10})
    assert t[SYM]["tp_pct"] == vol.LEGACY_TP_PCT
    assert t[SYM]["sl_pct"] == vol.LEGACY_SL_PCT
    assert t[SYM]["rung_pct"] == vol.LEGACY_RUNG_PCT
    assert "gate D" in t[SYM]["source"]
    conn.close()


# ── GATE F: a bad ATR is not a small ATR ─────────────────────────────────────

@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), 51.0, None, "x"])
def test_gate_f_rejects_an_impossible_atr(bad):
    assert vol.sane_atr(bad) is False


@pytest.mark.parametrize("ok", [0.03, 2.44, 7.89, 50.0])
def test_gate_f_accepts_a_real_one(ok):
    assert vol.sane_atr(ok) is True


# ── fallback ─────────────────────────────────────────────────────────────────

def test_fallback_used_when_the_candle_read_fails(tmp_path, monkeypatch):
    """A raised exception in the candle read must not take the table down with it —
    the pair falls back and the rest of the roster still computes."""
    conn = _conn(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("simulated OHLC failure")

    monkeypatch.setattr(store, "closed_daily_candles", _boom)
    t = vol.build_table(conn, [SYM], {SYM: 10})
    assert t[SYM]["source"] == "fallback"
    assert t[SYM]["tp_pct"] == pytest.approx(vol.FALLBACK_TP_PCT[SYM])
    assert t[SYM]["atr_pct"] is None
    conn.close()


def test_a_pair_with_three_candles_falls_back(tmp_path):
    conn = _conn(tmp_path)
    for i in range(3):
        store.upsert_candle(conn, SYM, 1440, 1_700_000_000 + i * 86400,
                            100.0, 101.0, 99.0, 100.0, 1.0, 1)
    conn.commit()
    t = vol.build_table(conn, [SYM], {SYM: 10})
    assert t[SYM]["source"] == "fallback"
    assert t[SYM]["tp_pct"] == pytest.approx(2.5)          # BTC's fallback tier
    conn.close()


def test_fallback_keeps_the_same_ratios():
    """The fallback is the SAME rule with a different ATR input, not a second table of
    hand-set stops — otherwise it is one rule with two implementations again."""
    d = vol.fallback_for("SUI/USD", leverage=None)
    assert d["tp_pct"] == pytest.approx(5.5)
    assert d["sl_pct"] == pytest.approx(5.5 * 1.5)
    assert d["rung_pct"] == pytest.approx(5.5 * 0.5)


def test_every_roster_pair_has_a_fallback_entry():
    """A roster pair missing from the table silently takes DEFAULT and is mis-scaled.
    HBAR, ALGO, RENDER and WLD were absent from the operator's list; WLD is the
    highest-ATR pair on the roster (7.89%), so a 4.0% default would have been badly
    wrong exactly where it matters most."""
    missing = [p["ws"] for p in config.PAIRS if p["ws"] not in vol.FALLBACK_TP_PCT]
    assert not missing, f"roster pairs with no fallback: {missing}"


# ── persistence + the resolver ───────────────────────────────────────────────

def test_table_round_trips_and_the_resolver_reads_it(tmp_path):
    conn = _conn(tmp_path)
    vol.save_table(conn, {SYM: {"tp_pct": 3.0, "sl_pct": 4.5, "rung_pct": 1.5,
                                "source": "test"}})
    table, updated = vol.load_table(conn)
    assert table[SYM]["tp_pct"] == 3.0 and updated
    assert vol.distances(conn, SYM)["sl_pct"] == 4.5
    conn.close()


def test_resolver_computes_on_demand_for_a_pair_missing_from_the_table(tmp_path):
    """A roster addition between daily refreshes must not read as "no distance"."""
    conn = _conn(tmp_path)
    vol.save_table(conn, {"ETH/USD": {"tp_pct": 3.0, "sl_pct": 4.5, "rung_pct": 1.5}})
    d = vol.distances(conn, SYM)
    assert d["tp_pct"] > 0 and d["sl_pct"] > 0 and d["rung_pct"] > 0
    conn.close()


def test_resolver_survives_a_corrupt_table(tmp_path):
    conn = _conn(tmp_path)
    store.meta_set(conn, vol.META_KEY, "{not json")
    d = vol.distances(conn, SYM)
    assert d["tp_pct"] > 0, "a corrupt table must fall back, not crash the money path"
    conn.close()


# ── FREEZE AT FILL: the ATR spike test the dispatch asked for ────────────────

def test_an_atr_spike_mid_position_does_not_move_a_stored_target(tmp_path, monkeypatch):
    """THE freeze rule (§b). Resolve at fill, persist on the row, and leave it there.

    If the target were recomputed live, a volatility spike would widen it away from the
    price the position is chasing — the target runs from you exactly when the move you
    were waiting for arrives."""
    conn = _conn(tmp_path)
    pin_vol(conn, tp=3.0, sl=4.5, rung=1.5)
    e = ex_mod.Executor(conn)
    e.mode = "live"
    oid = store.insert_order(conn, {
        "ts": "2026-08-06T00:00:00+00:00", "symbol": SYM, "margin_pair": "XBTUSD:BTNL",
        "side": "buy", "ordertype": "limit", "mode": "live", "entry": 100.0,
        "stop": 95.5, "volume": 0.001, "leverage": 10, "status": "open",
        "tp_pct": 3.0, "sl_pct": 4.5, "rung_pct": 1.5,
    })
    conn.commit()

    # ATR triples overnight; the daily refresh moves the table for NEW entries.
    pin_vol(conn, tp=9.0, sl=13.5, rung=4.5)
    assert vol.distances(conn, SYM)["tp_pct"] == 9.0, "table must have moved"

    frozen = conn.execute("SELECT tp_pct, sl_pct, rung_pct FROM orders WHERE id=?",
                          (oid,)).fetchone()
    assert frozen == (3.0, 4.5, 1.5), "an open position's targets must not move"
    assert e._row_tp_pct(SYM, frozen[0]) == 3.0, "the harvester must read the frozen one"
    conn.close()


def test_a_pre_ruling_row_is_not_stranded_and_never_reads_as_zero(tmp_path):
    """Rows written before 2026-08-06 carry NULL. The constant they used is deleted, so
    they resolve off today's table — and must never fall through to 0%, which would
    harvest the entire legacy book at market on the first poll."""
    conn = _conn(tmp_path)
    pin_vol(conn, tp=3.0)
    e = ex_mod.Executor(conn)
    e.mode = "live"
    for empty in (None, 0, 0.0, "", "junk"):
        assert e._row_tp_pct(SYM, empty) == 3.0
    conn.close()


# ── the executor routes everything through one resolver ──────────────────────

def test_stop_ladder_and_harvest_all_read_the_same_table(tmp_path):
    """The point of the ruling: three distances, one source. If any of them kept its
    own constant the others would drift away from it."""
    conn = _conn(tmp_path)
    pin_vol(conn, tp=3.0, sl=6.0, rung=2.0)
    e = ex_mod.Executor(conn)
    e.mode = "live"
    d = e._distances(SYM)
    assert (d["tp_pct"], d["sl_pct"], d["rung_pct"]) == (3.0, 6.0, 2.0)
    assert e.compute_stop(SYM, 100.0, None) == pytest.approx(94.0)   # 1 - 6%
