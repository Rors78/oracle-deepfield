"""Regime-gated accumulation (config.ACCUMULATE_ONLY_IN_BEAR).

The gate lives in Executor._accumulation_allowed(); it reads the regime label
ingest persists to `meta`. Critically it FAILS OPEN — only an unambiguous BULL
regime pauses accumulation; missing/unknown/other regimes still accumulate, so a
stale or unavailable regime can never silently halt entries (no-blockers stance).
"""
import pytest

from deepfield import store, config, executor as ex_mod
from deepfield.ingest import Ingest
from deepfield.state import AppState
from deepfield.profiles import FULL

SYM = "BTC/USD"


def _exec(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    e = ex_mod.Executor(conn)
    e.mode = "live"
    return e, conn


# ── regime gate ───────────────────────────────────────────────────────────────

def test_gate_fails_open_when_regime_missing(tmp_path, monkeypatch):
    """No regime persisted yet (fresh DB / regime never computed) MUST allow
    accumulation — a missing regime is not a reason to silently stop buying."""
    monkeypatch.setattr(config, "ACCUMULATE_ONLY_IN_BEAR", True)
    e, conn = _exec(tmp_path)
    ok, _ = e._accumulation_allowed()
    assert ok is True
    conn.close()


@pytest.mark.parametrize("label", ["BEAR", "RECOVERY", "NEUTRAL", "UNKNOWN", "weird"])
def test_gate_allows_all_non_bull_regimes(tmp_path, monkeypatch, label):
    """FAIL OPEN: only BULL pauses. BEAR/RECOVERY/NEUTRAL/UNKNOWN (and any
    unexpected label) still accumulate."""
    monkeypatch.setattr(config, "ACCUMULATE_ONLY_IN_BEAR", True)
    e, conn = _exec(tmp_path)
    store.meta_set(conn, "regime", label)
    ok, _ = e._accumulation_allowed()
    assert ok is True, f"regime={label} should accumulate (fail-open)"
    conn.close()


def test_gate_blocks_in_bull(tmp_path, monkeypatch):
    """The one case that pauses: a confirmed BULL regime — 'stop adding once BULL'."""
    monkeypatch.setattr(config, "ACCUMULATE_ONLY_IN_BEAR", True)
    e, conn = _exec(tmp_path)
    store.meta_set(conn, "regime", "BULL")
    ok, reason = e._accumulation_allowed()
    assert ok is False
    assert "BULL" in reason
    conn.close()


def test_gate_disabled_allows_bull(tmp_path, monkeypatch):
    """Gate off (config.ACCUMULATE_ONLY_IN_BEAR=False) accumulates in any regime,
    including BULL — the knob fully disables the behavior."""
    monkeypatch.setattr(config, "ACCUMULATE_ONLY_IN_BEAR", False)
    e, conn = _exec(tmp_path)
    store.meta_set(conn, "regime", "BULL")
    ok, _ = e._accumulation_allowed()
    assert ok is True
    conn.close()


def _fund_everything_downstream(monkeypatch):
    """Stub every stage past the regime gate to SUCCEED, so the gate is the only
    variable. The first version of this test stubbed nothing: in the test env
    _place_entry returns None down at least three unrelated paths (broker wall ->
    equity None -> rails refuse; missing keys; the AddOrder wall swallowed by the
    catch-all), so `assert place_entry(...) is None` held with the gate DELETED —
    proven by mutation on 2026-08-08. Worse, it constructed and signed real
    Kraken requests that only the conftest wall stopped, burning ~8s in backoff.
    A gate test is only a gate test when the gate is the only thing that can say
    no."""
    from deepfield import executor as ex_mod
    monkeypatch.setattr(ex_mod.Executor, "portfolio_value", lambda self: 1000.0)
    monkeypatch.setattr(ex_mod.Executor, "rails_ok", lambda self, eq: (True, ""))
    monkeypatch.setattr(ex_mod.Executor, "compute_stop",
                        lambda self, sym, px, card, sl_pct=None: px * 0.9)
    # notional must sit UNDER config.EXEC_MAX_ORDER_NOTIONAL_USD (the $50 sanity
    # ceiling) — at $100 the first draft was refused for SIZE, which made the
    # gated test pass for yet another wrong reason. The converse control caught it.
    monkeypatch.setattr(ex_mod.Executor, "size",
                        lambda self, *a, **k: {"volume": 0.001, "notional": 20.0,
                                               "margin": 10.0, "actual_risk": 1.0,
                                               "floored_to_min": False, "capped": False,
                                               "conviction_mult": 1.0, "size_mult": 1.0})
    sent = []
    monkeypatch.setattr(ex_mod.broker, "private",
                        lambda ep, p=None, **k: (sent.append(ep) or {"txid": ["OGATE-1"]}))
    return sent


def test_place_entry_returns_none_when_gated(tmp_path, monkeypatch):
    """End-to-end: _place_entry short-circuits to None under the gate BEFORE any
    sizing/broker call (BULL regime, gate on) — with everything downstream funded,
    so None can only mean the gate."""
    monkeypatch.setattr(config, "ACCUMULATE_ONLY_IN_BEAR", True)
    e, conn = _exec(tmp_path)
    sent = _fund_everything_downstream(monkeypatch)
    store.meta_set(conn, "regime", "BULL")
    assert e.place_entry(SYM, 100.0, object()) is None
    assert sent == [], f"the gate leaked — a broker call went out: {sent}"
    conn.close()


def test_place_entry_proceeds_when_gate_open(tmp_path, monkeypatch):
    """Converse control, same stubs: in a BEAR regime the identical call must get
    PAST the gate and reach the broker. Without this, a test env where placement
    fails for any unrelated reason makes the gated test pass vacuously — which is
    exactly what it did."""
    monkeypatch.setattr(config, "ACCUMULATE_ONLY_IN_BEAR", True)
    e, conn = _exec(tmp_path)
    sent = _fund_everything_downstream(monkeypatch)
    store.meta_set(conn, "regime", "BEAR")
    e.place_entry(SYM, 100.0, object())
    assert sent, "gate open but nothing reached the broker — the stubs no longer fund the path"
    conn.close()


# ── ingest -> meta wiring (the link the executor gate reads) ───────────────────

def test_recompute_regime_persists_label_to_meta(tmp_path):
    """ingest._recompute_regime must WRITE the regime label to meta so the executor
    gate can read it. With no BTC series the label is 'UNKNOWN' — still persisted
    (and 'UNKNOWN' fails open, so accumulation continues)."""
    conn = store.connect(str(tmp_path / "t.db"))
    ing = Ingest(conn, AppState(), profile=FULL)
    assert store.meta_get(conn, "regime", None) is None      # nothing yet
    ing._recompute_regime()
    persisted = store.meta_get(conn, "regime", None)
    assert persisted is not None                             # wiring works
    # and that persisted value drives the executor gate consistently (fail-open here)
    e = ex_mod.Executor(conn)
    ok, _ = e._accumulation_allowed()
    assert ok is True                                        # UNKNOWN -> accumulate
    conn.close()
