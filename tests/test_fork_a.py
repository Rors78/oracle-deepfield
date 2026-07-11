"""FORK A part 1 — de-lever (2x) + regime-gated accumulation.

The gate lives in Executor._accumulation_allowed(); it reads the regime label
ingest persists to `meta`. Critically it FAILS OPEN — only an unambiguous BULL
regime pauses accumulation; missing/unknown/other regimes still accumulate, so a
stale or unavailable regime can never silently halt entries (no-blockers stance).
"""
import sqlite3
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


def test_place_entry_returns_none_when_gated(tmp_path, monkeypatch):
    """End-to-end: _place_entry short-circuits to None under the gate BEFORE any
    sizing/broker call (BULL regime, gate on)."""
    monkeypatch.setattr(config, "ACCUMULATE_ONLY_IN_BEAR", True)
    e, conn = _exec(tmp_path)
    store.meta_set(conn, "regime", "BULL")
    # if the gate leaks, this would try to size/place and hit the network — the
    # test's value is that it returns None without doing so.
    assert e.place_entry(SYM, 100.0, object()) is None
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


# ── de-lever ──────────────────────────────────────────────────────────────────

def test_all_pairs_delevered_to_2x():
    """Every traded pair is 2x (Kraken spot-margin floor) after the fork-A de-lever,
    and every leverage key still has a :BTNL margin pair to trade on."""
    assert set(config.PER_PAIR_LEVERAGE.values()) == {2}, config.PER_PAIR_LEVERAGE
    assert set(config.PER_PAIR_LEVERAGE) <= set(config.MARGIN_PAIR)
