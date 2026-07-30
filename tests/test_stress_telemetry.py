"""Intraday liq-buffer stress telemetry (2026-07-19).

buffer_liq_pct is denominated in ADVERSE BASKET MOVE, so the useful question is how
big a basket move actually happens and what it would do to the current book. Daily
bars cannot answer the intraday half: a bar closing -3% may have wicked -9% and
recovered, and the point-in-time buffer poll never saw it — which is how the
operator ended up learning about near-liquidation from the exchange app.

These tests pin (a) the math against the same law compute() uses, (b) that 15m LOWS
surface a drawdown daily CLOSES structurally hide, and (c) that the whole thing
stays telemetry: it may journal and alert, never place/cancel/size an order.

Kraken is stubbed throughout. The live rate limit is per-ACCOUNT, so a test that
reached the real API would contend with the running bot.
"""
import json
import math

import pytest

from deepfield import app, config, defense, store

LIQ = defense.LIQ_ML


# ── pure math ───────────────────────────────────────────────────────────────
def test_stress_buffer_matches_the_compute_law_at_zero_move():
    e, m, v = 186.0, 20.0, 100.0
    assert defense.stress_buffer(e, m, v, 0.0) == pytest.approx(
        defense.compute(e, m, v)["buffer_liq_pct"])


def test_stress_buffer_marks_equity_down_but_not_margin():
    """Kraken sizes margin off OPENING cost, so it does not shrink with the mark —
    that asymmetry is exactly why a drawdown compresses the margin level."""
    e, m, v, d = 100.0, 20.0, 100.0, 0.10
    assert defense.stress_buffer(e, m, v, d) == pytest.approx(
        (e - v * d - LIQ * m) / (v * (1 - d)) * 100)


def test_stress_buffer_goes_negative_when_the_move_would_liquidate():
    """Must return a NUMBER past liquidation, not None — the caller needs to see
    how far past, and None would read as 'unknown' and fail open."""
    b = defense.stress_buffer(100.0, 20.0, 900.0, 0.12)
    assert b is not None and b < 0


def test_stress_buffer_flat_book_is_infinite_not_scary():
    assert defense.stress_buffer(100.0, 0.0, 0.0, 0.5) == math.inf


@pytest.mark.parametrize("args", [(None, 1, 1, 0.1), (0, 1, 1, 0.1), (-1, 1, 1, 0.1),
                                  (100, -1, 1, 0.1), (100, 1, 1, 1.5)])
def test_stress_buffer_rejects_unusable_input(args):
    assert defense.stress_buffer(*args) is None


def test_max_survivable_leverage_reproduces_the_buffer_law():
    """At the ceiling the buffer must equal the move it is meant to survive."""
    m, v, d = 20.0, 100.0, 0.1676
    L = defense.max_survivable_leverage(m, v, d)
    e = v / L                                    # equity implied by that leverage
    assert defense.compute(e, m, v)["buffer_liq_pct"] == pytest.approx(d * 100, abs=1e-6)


def test_max_survivable_leverage_uses_live_m_over_v_not_nominal_leverage():
    """A mixed-leverage book must not be judged by a nominal per-pair number."""
    assert (defense.max_survivable_leverage(20.0, 100.0, 0.1676)
            != defense.max_survivable_leverage(10.0, 100.0, 0.1676))


def test_max_survivable_leverage_flat_book_is_infinite():
    assert defense.max_survivable_leverage(0, 0, 0.2) == math.inf


# ── basket math ─────────────────────────────────────────────────────────────
def test_basket_index_normalises_and_weights():
    idx = defense.basket_index([(1.0, [100, 200]), (1.0, [100, 100])])
    assert idx[0] == pytest.approx(1.0)
    assert idx[-1] == pytest.approx(1.5)          # 50/50 of +100% and flat


def test_basket_index_truncates_to_the_shortest_series():
    """Unaligned series would invent moves that never happened."""
    assert len(defense.basket_index([(1, [1, 2, 3, 4]), (1, [1, 2])])) == 2


def test_basket_drawdown_finds_peak_to_trough():
    assert defense.basket_drawdown([(1.0, [100, 110, 88, 95])]) == pytest.approx(0.2)


def test_worst_move_is_a_fixed_span_not_a_drawdown():
    idx = defense.basket_index([(1.0, [100, 90, 100, 90])])
    assert defense.worst_move(idx, 1) == pytest.approx(0.10)
    assert defense.worst_move(idx, 3) == pytest.approx(0.10)
    assert defense.worst_move(idx, 99) == 0.0     # horizon longer than the series


def test_worst_move_returns_zero_when_nothing_is_adverse():
    assert defense.worst_move(defense.basket_index([(1, [1, 2, 3])]), 1) == 0.0


# ── end to end ──────────────────────────────────────────────────────────────
@pytest.fixture
def live_book(tmp_path, monkeypatch):
    """One open position with a daily series that closes calm and a 15m series that
    wicks hard — the exact case daily bars hide."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "stress.db"))
    sym = config.PAIRS[0]["ws"]
    c = store.connect(config.DB_PATH)
    c.execute("INSERT INTO orders(symbol,status,volume,entry,mode) VALUES(?,?,?,?,?)",
              (sym, "open", 10.0, 100.0, "live"))
    for i in range(40):                              # daily closes: flat
        store.upsert_candle(c, sym, 1440, 1_000_000 + i * 86400, 100, 100, 100, 100, 1, 1)
    for i in range(40):                              # 15m: a deep wick that recovers
        low = 70.0 if i == 20 else 100.0
        store.upsert_candle(c, sym, 15, 2_000_000 + i * 900, 100, 100, low, 100, 1, 1)
    c.commit()
    c.close()
    monkeypatch.setattr(config, "STRESS_INTRADAY_LOOKBACK_BARS", 96)
    return sym


def _stub_broker(monkeypatch, e=100.0, m=20.0, v=100.0):
    from deepfield import broker
    monkeypatch.setattr(broker, "trade_balance_full", lambda: {"m": m, "v": v, "e": e})
    monkeypatch.setattr(broker, "equity", lambda b: e)


def _state():
    c = store.connect(config.DB_PATH)
    try:
        return json.loads(store.meta_get(c, "stress_state") or "{}")
    finally:
        c.close()


def test_intraday_wick_is_caught_where_daily_closes_show_nothing(live_book, monkeypatch):
    """The whole point of #2: the daily series is flat, so a daily-only view reports
    zero stress; the 15m lows show a 30% basket drawdown."""
    _stub_broker(monkeypatch)
    monkeypatch.setattr(app.alerter, "fire_safety", lambda *a, **k: None)
    app._poll_stress_threaded()
    s = _state()
    assert s["intraday_dd_pct"] == pytest.approx(30.0, abs=0.01)
    assert s["ref_move_pct"]["1"] == 0.0, "daily closes are flat — nothing to see there"
    assert s["buffer_under_intraday_pct"] < s["buffer_now_pct"], \
        "the stressed buffer must be worse than the calm one"


def test_flat_book_records_flat_and_does_not_alert(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "flat.db"))
    store.connect(config.DB_PATH).close()
    fired = []
    monkeypatch.setattr(app.alerter, "fire_safety", lambda *a, **k: fired.append(a))
    app._poll_stress_threaded()
    s = _state()
    assert s.get("flat") is True
    # Rails re-arm 2026-07-30: every stress_state write carries a freshness
    # stamp so the L_eff growth gate can fail open on a dead poll.
    assert s.get("updated", 0) > 0
    assert fired == []


def test_leverage_over_the_ceiling_alerts(live_book, monkeypatch):
    """v/e = 9x against a real reference move must page."""
    _stub_broker(monkeypatch, e=100.0, m=180.0, v=900.0)
    c = store.connect(config.DB_PATH)
    sym = live_book
    for i in range(40):                              # give the daily series a real move
        px = 100.0 if i < 20 else 80.0
        store.upsert_candle(c, sym, 1440, 1_000_000 + i * 86400, px, px, px, px, 1, 1)
    c.commit(); c.close()
    fired = []
    monkeypatch.setattr(app.alerter, "fire_safety", lambda *a, **k: fired.append(a))
    app._poll_stress_threaded()
    s = _state()
    assert s["lev_now"] == pytest.approx(9.0)
    assert s["lev_ceiling"] < s["lev_now"]
    assert fired and fired[0][0] == "liq-risk"


def test_never_touches_the_order_path(live_book, monkeypatch):
    """defense/stress is telemetry: it may journal and alert, never trade."""
    _stub_broker(monkeypatch)
    monkeypatch.setattr(app.alerter, "fire_safety", lambda *a, **k: None)
    from deepfield import broker
    for fn in ("add_order", "cancel_order"):
        if hasattr(broker, fn):
            monkeypatch.setattr(broker, fn, lambda *a, **k: pytest.fail(
                f"stress telemetry called broker.{fn} — it must never trade"))
    app._poll_stress_threaded()


def test_poll_never_raises_into_the_loop(live_book, monkeypatch):
    from deepfield import broker
    monkeypatch.setattr(broker, "trade_balance_full",
                        lambda: (_ for _ in ()).throw(RuntimeError("kraken down")))
    app._poll_stress_threaded()          # must swallow
