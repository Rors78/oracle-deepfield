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


# ── pt2: harvest / gain-realization ───────────────────────────────────────────

class _Card:
    low_52w = 92.0; price = 100.0; score = 5; denom = 7; required = 5; status = "BUY"; fired = ["x"]


def _open_row(conn, stop_txid="S1", harvest_txid=None, entry_txid="E1", vol=1.0):
    oid = store.insert_order(conn, {
        "ts": "2026-07-11T00:00:00+00:00", "symbol": SYM, "margin_pair": "XBTUSD:BTNL",
        "side": "buy", "ordertype": "limit", "mode": "live", "entry": 100.0, "stop": 90.0,
        "volume": vol, "leverage": 2, "notional": 200.0, "margin": 100.0, "risk_usd": 1.0,
        "score": 5, "required": 5, "txid": entry_txid, "stop_txid": stop_txid,
        "status": "open", "error": None})
    conn.execute("UPDATE orders SET harvest_txid=? WHERE id=?", (harvest_txid, oid))
    conn.commit()
    return oid


def test_orders_has_harvest_txid_column(tmp_path):
    """Additive migration / DDL: the orders table has harvest_txid after connect."""
    conn = store.connect(str(tmp_path / "t.db"))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
    assert "harvest_txid" in cols
    conn.close()


def test_rest_harvest_paper_places_target_sell_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HARVEST_ENABLED", True)
    conn = store.connect(str(tmp_path / "t.db"))
    store.upsert_pair(conn, "XXBTZUSD", SYM, "BTC", 0.00005, 0.5, 8)
    e = ex_mod.Executor(conn); e.mode = "paper"
    oid = e.place_entry(SYM, 100.0, _Card())
    htx = conn.execute("SELECT harvest_txid FROM orders WHERE id=?", (oid,)).fetchone()[0]
    assert htx and htx.startswith("PAPER-HARVEST")
    conn.close()


def test_rest_harvest_noop_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HARVEST_ENABLED", False)
    conn = store.connect(str(tmp_path / "t.db"))
    store.upsert_pair(conn, "XXBTZUSD", SYM, "BTC", 0.00005, 0.5, 8)
    e = ex_mod.Executor(conn); e.mode = "paper"
    oid = e.place_entry(SYM, 100.0, _Card())
    assert conn.execute("SELECT harvest_txid FROM orders WHERE id=?", (oid,)).fetchone()[0] is None
    conn.close()


def test_oco_harvest_fill_closes_and_cancels_stop(tmp_path, monkeypatch):
    """Harvest fills -> row closed at a profit, and the now-orphan STOP is canceled
    (else it would open a short). This is the money-safety core."""
    monkeypatch.setattr(config, "HARVEST_ENABLED", True)
    conn = store.connect(str(tmp_path / "t.db"))
    oid = _open_row(conn, stop_txid="STOP1", harvest_txid="HARV1", entry_txid="ENT1")
    e = ex_mod.Executor(conn); e.mode = "live"
    canceled = []
    monkeypatch.setattr(ex_mod.broker, "query_orders",
                        lambda ids: {"HARV1": {"status": "closed", "cost": 120.0, "fee": 0.2, "vol_exec": 1.0},
                                     "STOP1": {"status": "open"}})
    monkeypatch.setattr(ex_mod.broker, "query_order",
                        lambda t: {"cost": 100.0, "fee": 0.2} if t == "ENT1" else None)
    monkeypatch.setattr(ex_mod.broker, "cancel_order", lambda t: canceled.append(t) or {"count": 1})
    e.poll_harvest_oco()
    assert conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()[0] == "closed"
    assert canceled == ["STOP1"]                              # stop killed, harvest (filled) not
    err = conn.execute("SELECT error FROM orders WHERE id=?", (oid,)).fetchone()[0]
    assert err and '"exit": "harvest"' in err                # profit P&L attributed
    conn.close()


def test_oco_stop_fill_closes_and_cancels_harvest(tmp_path, monkeypatch):
    """Symmetric: stop fills -> row closed at a loss, and the now-orphan HARVEST is
    canceled (else it would open a short on a bounce)."""
    monkeypatch.setattr(config, "HARVEST_ENABLED", True)
    conn = store.connect(str(tmp_path / "t.db"))
    oid = _open_row(conn, stop_txid="STOP1", harvest_txid="HARV1", entry_txid="ENT1")
    e = ex_mod.Executor(conn); e.mode = "live"
    canceled = []
    monkeypatch.setattr(ex_mod.broker, "query_orders",
                        lambda ids: {"STOP1": {"status": "closed", "cost": 80.0, "fee": 0.2, "vol_exec": 1.0},
                                     "HARV1": {"status": "open"}})
    monkeypatch.setattr(ex_mod.broker, "query_order",
                        lambda t: {"cost": 100.0, "fee": 0.2} if t == "ENT1" else None)
    monkeypatch.setattr(ex_mod.broker, "cancel_order", lambda t: canceled.append(t) or {"count": 1})
    e.poll_harvest_oco()
    assert conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()[0] == "closed"
    assert canceled == ["HARV1"]                              # harvest killed, stop (filled) not
    conn.close()


def test_oco_noop_when_disabled(tmp_path, monkeypatch):
    """Feature off -> poll_harvest_oco does nothing (row stays open, no broker calls)."""
    monkeypatch.setattr(config, "HARVEST_ENABLED", False)
    conn = store.connect(str(tmp_path / "t.db"))
    oid = _open_row(conn, stop_txid="STOP1", harvest_txid="HARV1")
    e = ex_mod.Executor(conn); e.mode = "live"

    def _boom(*a, **k):
        raise AssertionError("broker touched while harvest disabled")
    monkeypatch.setattr(ex_mod.broker, "query_orders", _boom)
    e.poll_harvest_oco()
    assert conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()[0] == "open"
    conn.close()


def test_oco_leaves_open_when_stop_cancel_fails(tmp_path, monkeypatch):
    """NAKED-SHORT GUARD (the path that matters): harvest fills but the stop cancel
    returns None (transient API failure) -> the row must stay 'open' and NO P&L is
    written, so the next poll retries. Closing here would strand a resting stop against
    a gone position -> naked short, never revisited."""
    monkeypatch.setattr(config, "HARVEST_ENABLED", True)
    conn = store.connect(str(tmp_path / "t.db"))
    oid = _open_row(conn, stop_txid="STOP1", harvest_txid="HARV1", entry_txid="ENT1")
    e = ex_mod.Executor(conn); e.mode = "live"
    monkeypatch.setattr(ex_mod.broker, "query_orders",
                        lambda ids: {"HARV1": {"status": "closed", "cost": 120.0, "fee": 0.2, "vol_exec": 1.0},
                                     "STOP1": {"status": "open"}})
    monkeypatch.setattr(ex_mod.broker, "query_order",
                        lambda t: {"cost": 100.0, "fee": 0.2} if t == "ENT1" else None)
    monkeypatch.setattr(ex_mod.broker, "cancel_order", lambda t: None)     # transient failure
    e.poll_harvest_oco()
    status, err = conn.execute("SELECT status, error FROM orders WHERE id=?", (oid,)).fetchone()
    assert status == "open", "must NOT close over a still-resting stop"
    assert err is None, "no P&L until the row actually closes"
    conn.close()


def test_oco_leaves_open_when_harvest_cancel_fails(tmp_path, monkeypatch):
    """Symmetric guard: stop fills but the harvest cancel fails -> row stays 'open'."""
    monkeypatch.setattr(config, "HARVEST_ENABLED", True)
    conn = store.connect(str(tmp_path / "t.db"))
    oid = _open_row(conn, stop_txid="STOP1", harvest_txid="HARV1", entry_txid="ENT1")
    e = ex_mod.Executor(conn); e.mode = "live"
    monkeypatch.setattr(ex_mod.broker, "query_orders",
                        lambda ids: {"STOP1": {"status": "closed", "cost": 80.0, "fee": 0.2, "vol_exec": 1.0},
                                     "HARV1": {"status": "open"}})
    monkeypatch.setattr(ex_mod.broker, "query_order",
                        lambda t: {"cost": 100.0, "fee": 0.2} if t == "ENT1" else None)
    monkeypatch.setattr(ex_mod.broker, "cancel_order", lambda t: None)     # transient failure
    e.poll_harvest_oco()
    assert conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()[0] == "open"
    conn.close()


def test_cancel_sibling_contract(tmp_path, monkeypatch):
    """_cancel_sibling: True when nothing to strand or Kraken accepts; False only on a
    transient None (so the caller keeps the row open)."""
    conn = store.connect(str(tmp_path / "t.db"))
    e = ex_mod.Executor(conn); e.mode = "live"
    assert e._cancel_sibling(None) is True                    # nothing to strand
    assert e._cancel_sibling("PAPER-STOP-1") is True          # paper: nothing live
    monkeypatch.setattr(ex_mod.broker, "cancel_order", lambda t: {"count": 1})
    assert e._cancel_sibling("STOP1") is True                 # accepted
    monkeypatch.setattr(ex_mod.broker, "cancel_order", lambda t: {"count": 0})
    assert e._cancel_sibling("STOP1") is True                 # already terminal (count 0)
    monkeypatch.setattr(ex_mod.broker, "cancel_order", lambda t: None)
    assert e._cancel_sibling("STOP1") is False                # transient failure -> don't close
    conn.close()


def test_retrofit_budgets_harvest_to_live_open_volume(tmp_path, monkeypatch):
    """3 open DB rows (vol 1 each = 3.0) but Kraken live open long is only 2.0 -> the
    retrofit places at most 2 harvests (oldest-first); it NEVER over-places sell volume
    beyond the live position (which, with no reduce_only, would open a short)."""
    monkeypatch.setattr(config, "HARVEST_ENABLED", True)
    conn = store.connect(str(tmp_path / "t.db"))
    for i in range(3):
        _open_row(conn, stop_txid=f"S{i}", harvest_txid=None, entry_txid=f"E{i}", vol=1.0)
    e = ex_mod.Executor(conn); e.mode = "live"
    placed = []
    monkeypatch.setattr(ex_mod.broker, "open_positions",
                        lambda: {"P1": {"pair": "XXBTZUSD", "vol": "2.0", "vol_closed": "0", "type": "buy"}})
    monkeypatch.setattr(ex_mod.broker, "private",
                        lambda ep, params, **k: (placed.append(params) or {"txid": [f"H{len(placed)}"]}))
    e._reconcile_harvests()
    assert len(placed) == 2, f"expected 2 harvests (budget=2.0), got {len(placed)}"
    assert all(p["type"] == "sell" and p["oflags"] == "post" for p in placed)   # post-only reducing sells
    conn.close()
