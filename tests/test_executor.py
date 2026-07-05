"""Executor: 2%-risk sizing, stop clamp, risk rails, paper/off/validate paths.

Never touches the network (live/validate broker calls are monkeypatched). No
test can place a real order — paper and off are the only self-contained modes.
"""
import os
import time

import pytest

from deepfield import store, config, executor as ex_mod

SYM = "BTC/USD"


class Card:
    def __init__(self, low_52w=None):
        self.low_52w = low_52w
        self.price = 100.0
        self.score = 5
        self.denom = 7
        self.required = 5
        self.status = "BUY"
        self.fired = ["x"]


def _conn(tmp_path, ordermin=0.00005, costmin=0.5, lot_dec=8):
    conn = store.connect(str(tmp_path / "t.db"))
    store.upsert_pair(conn, "XXBTZUSD", SYM, "BTC", ordermin, costmin, lot_dec)
    return conn


def _exec(conn, mode="paper"):
    e = ex_mod.Executor(conn)
    e.mode = mode
    return e


# ── sizing ───────────────────────────────────────────────────────────────────

def test_size_min_mode_is_minimum_order(tmp_path):
    """Default: buy the minimum order (ordermin, cost-floored) — tiny, no
    liquidation worry. risk_usd is 0 (not risk-sized)."""
    conn = _conn(tmp_path, ordermin=0.1, costmin=0.5, lot_dec=8)
    e = _exec(conn)   # EXEC_SIZE_MODE default = "min"
    s = e.size(SYM, entry=100.0, stop=90.0, leverage=10, equity=1000.0)
    assert s["size_mode"] == "min"
    assert s["volume"] == 0.1                      # ordermin (cost 0.5/100 < 0.1)
    assert abs(s["margin"] - (0.1 * 100 / 10)) < 1e-9   # notional/leverage = $1
    assert s["risk_usd"] == 0.0
    conn.close()


def test_size_risk_2pct_off_the_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXEC_SIZE_MODE", "risk")
    conn = _conn(tmp_path)
    e = _exec(conn)
    # equity 1000, risk 2% = $20; entry 100, stop 90 -> $10 stop dist -> vol 2.0
    s = e.size(SYM, entry=100.0, stop=90.0, leverage=10, equity=1000.0)
    assert abs(s["volume"] - 2.0) < 1e-9
    assert abs(s["notional"] - 200.0) < 1e-9
    assert abs(s["margin"] - 20.0) < 1e-9          # notional/leverage
    assert abs(s["actual_risk"] - 20.0) < 1e-9     # == 2% of equity
    conn.close()


def test_size_margin_cap_binds_on_tight_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXEC_SIZE_MODE", "risk")
    conn = _conn(tmp_path)
    e = _exec(conn)
    # razor stop dist 0.5 -> naive vol = 20/0.5 = 40, notional 4000, margin 400.
    # cap: 0.9*1000*leverage/entry ... margin cap = 900 -> vol cap 900*10/100=90.
    # 40 < 90 so not capped here; make it bind with leverage 1.
    s = e.size(SYM, entry=100.0, stop=99.5, leverage=1, equity=1000.0)
    assert s["capped"] is True
    assert s["margin"] <= 1000.0 * config.MARGIN_CAP_PCT + 1e-6
    conn.close()


def test_size_floors_to_ordermin(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXEC_SIZE_MODE", "risk")
    conn = _conn(tmp_path, ordermin=0.1)   # LTC-like floor, but on BTC row for test
    e = _exec(conn)
    # tiny risk: equity 10 -> risk $0.20, stop dist 10 -> vol 0.02 < ordermin 0.1
    s = e.size(SYM, entry=100.0, stop=90.0, leverage=10, equity=10.0)
    assert s["floored_to_min"] is True
    assert s["volume"] >= 0.1
    conn.close()


# ── stop clamp ───────────────────────────────────────────────────────────────

def test_stop_clamped_to_band(tmp_path):
    conn = _conn(tmp_path)
    e = _exec(conn)
    # support far below -> clamp to widest allowed (STOP_MAX_PCT)
    assert abs(e.compute_stop(SYM, 100.0, Card(low_52w=10.0)) - 100.0 * (1 - config.STOP_MAX_PCT)) < 1e-9
    # support very close -> clamp to tightest allowed (STOP_MIN_PCT)
    assert abs(e.compute_stop(SYM, 100.0, Card(low_52w=99.0)) - 100.0 * (1 - config.STOP_MIN_PCT)) < 1e-9
    # support within band -> used as-is
    assert abs(e.compute_stop(SYM, 100.0, Card(low_52w=92.0)) - 92.0) < 1e-9
    conn.close()


# ── risk rails ───────────────────────────────────────────────────────────────

def test_rails_halt_file_blocks(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    halt = tmp_path / "HALT"
    halt.write_text("x")
    monkeypatch.setattr(config, "HALT_FILE", str(halt))
    ok, reason = _exec(conn).rails_ok(1000.0)
    assert not ok and "HALT" in reason
    conn.close()


def test_rails_kill_switch_on_drawdown(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RAILS_ENABLED', True)
    conn = _conn(tmp_path)
    store.meta_set(conn, "peak_equity", 1000.0)
    ok, reason = _exec(conn).rails_ok(750.0)   # -25% > 20% DD limit
    assert not ok and "KILL SWITCH" in reason
    ok2, _ = _exec(conn).rails_ok(850.0)        # -15% within limit
    assert ok2
    conn.close()


def test_rails_max_positions_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RAILS_ENABLED', True)
    conn = _conn(tmp_path)
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 1)
    conn.execute("INSERT INTO orders(symbol,status) VALUES('X/USD','open')")
    conn.commit()
    ok, reason = _exec(conn).rails_ok(1000.0)
    assert not ok and "max open positions" in reason
    conn.close()


# ── modes ────────────────────────────────────────────────────────────────────

def test_off_mode_is_noop(tmp_path):
    conn = _conn(tmp_path)
    e = _exec(conn, mode="off")
    assert e.place_entry(SYM, 100.0, Card(low_52w=92.0)) is None
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    conn.close()


def test_paper_mode_records_open_order_and_stop(tmp_path):
    conn = _conn(tmp_path)
    e = _exec(conn, mode="paper")
    oid = e.place_entry(SYM, 100.0, Card(low_52w=92.0))
    row = conn.execute("SELECT symbol,margin_pair,mode,status,leverage,stop_txid FROM orders WHERE id=?",
                       (oid,)).fetchone()
    assert row[0] == SYM
    assert row[1] == "XBTUSD:BTNL"          # :BTNL routing
    assert row[2] == "paper" and row[3] == "open"
    assert row[4] == 10                       # BTC max leverage
    assert row[5] and row[5].startswith("PAPER-STOP")   # protective stop rested (sim)
    conn.close()


def test_place_entry_never_raises(tmp_path):
    conn = _conn(tmp_path)
    e = _exec(conn, mode="paper")
    # unknown symbol -> no margin pair -> logged + None, never an exception
    assert e.place_entry("NOTA/PAIR", 100.0, Card()) is None
    conn.close()


# ── live fill lifecycle: pending -> (fill) open+stop | (unfilled) canceled ─────

def _seed_pending(conn, txid="OENTRY", stop=90.0):
    cur = conn.execute(
        "INSERT INTO orders(symbol,margin_pair,volume,leverage,stop,txid,status) "
        "VALUES(?,?,?,?,?,?, 'pending')", (SYM, "XBTUSD:BTNL", 0.1, 10, stop, txid))
    conn.commit()
    return cur.lastrowid


def test_live_entry_is_pending_with_no_stop(tmp_path, monkeypatch):
    conn = _conn(tmp_path, ordermin=0.1)
    orders = []

    def fake_private(ep, p=None, **kw):
        if "TradeBalance" in ep:
            return {"e": "1000"}                 # rails equity
        orders.append(p)
        return {"txid": ["OENTRY-1"]}

    monkeypatch.setattr(ex_mod.broker, "private", fake_private)
    e = _exec(conn, mode="live")
    oid = e.place_entry(SYM, 100.0, Card(low_52w=92.0))
    status, txid, stop_txid = conn.execute(
        "SELECT status,txid,stop_txid FROM orders WHERE id=?", (oid,)).fetchone()
    assert status == "pending"          # a resting limit is NOT a position
    assert txid == "OENTRY-1"
    assert stop_txid is None            # NO stop rested for an unfilled entry
    # exactly ONE order was sent — the entry buy limit; no stop-loss yet
    assert len(orders) == 1
    assert orders[0]["type"] == "buy" and orders[0]["ordertype"] == "limit"
    assert all(o.get("ordertype") != "stop-loss" for o in orders)
    conn.close()


def test_poll_fills_promotes_filled_and_rests_stop(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    seq = []
    monkeypatch.setattr(ex_mod.broker, "private",
                        lambda ep, p=None, **kw: (seq.append(p) or {"txid": ["OSTOP-1"]}))
    monkeypatch.setattr(ex_mod.broker, "query_order",
                        lambda txid: {"status": "closed", "vol_exec": "0.1"})
    e = _exec(conn, mode="live")
    oid = _seed_pending(conn, "OENTRY-2")
    e.poll_fills()
    status, stop_txid, vol = conn.execute(
        "SELECT status,stop_txid,volume FROM orders WHERE id=?", (oid,)).fetchone()
    assert status == "open"             # filled -> position
    assert stop_txid == "OSTOP-1"       # protective stop rested only now
    assert abs(vol - 0.1) < 1e-9
    assert any(p.get("type") == "sell" and p.get("ordertype") == "stop-loss" for p in seq)
    conn.close()


def test_poll_fills_unfilled_cancel_opens_nothing(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    calls = []
    monkeypatch.setattr(ex_mod.broker, "private", lambda ep, p=None, **kw: calls.append(p))
    monkeypatch.setattr(ex_mod.broker, "query_order",
                        lambda txid: {"status": "canceled", "vol_exec": "0"})
    e = _exec(conn, mode="live")
    oid = _seed_pending(conn, "OENTRY-3")
    e.poll_fills()
    status, stop_txid = conn.execute(
        "SELECT status,stop_txid FROM orders WHERE id=?", (oid,)).fetchone()
    assert status == "canceled"         # never filled -> no position
    assert stop_txid is None
    assert calls == []                  # and NO order of any kind was sent
    conn.close()


def test_validate_mode_builds_order_without_executing(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    captured = {}

    def fake_private(endpoint, params=None):
        captured["endpoint"] = endpoint
        captured["params"] = params
        return {"descr": {"order": "buy 2.0 XBTUSD:BTNL @ limit 100 with 10:1 leverage"}}

    monkeypatch.setattr(ex_mod.broker, "private", fake_private)
    e = _exec(conn, mode="validate")
    oid = e.place_entry(SYM, 100.0, Card(low_52w=92.0))
    assert captured["params"]["validate"] == "true"          # never executes
    assert captured["params"]["pair"] == "XBTUSD:BTNL"
    assert captured["params"]["leverage"] == "10"
    assert captured["params"]["ordertype"] == "limit"        # post-only maker (NO market)
    assert captured["params"]["oflags"] == "post"
    row = conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()
    assert row[0] == "validated"
    conn.close()


# ── code-review hardening regressions ─────────────────────────────────────────

def test_rails_block_when_live_equity_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RAILS_ENABLED', True)
    """Live TradeBalance failure -> equity None -> must BLOCK (kill-switch can't be
    evaluated, and min-size sizing ignores equity so it would otherwise slip through)."""
    conn = _conn(tmp_path)
    ok, reason = _exec(conn, mode="live").rails_ok(None)
    assert not ok and "equity unavailable" in reason
    conn.close()


def test_cap_counts_pending_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RAILS_ENABLED', True)
    """A resting 'pending' entry limit counts toward MAX_OPEN_POSITIONS (it will fill)."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 1)
    conn.execute("INSERT INTO orders(symbol,status) VALUES('X/USD','pending')")
    conn.commit()
    ok, reason = _exec(conn).rails_ok(1000.0)
    assert not ok and "max open positions" in reason
    conn.close()


def test_post_only_entry_prices_below_last(tmp_path, monkeypatch):
    """Post-only maker BUY must be priced below last so it can't cross the ask."""
    conn = _conn(tmp_path, ordermin=0.1)
    orders = []

    def fake_private(ep, p=None, **kw):
        if "TradeBalance" in ep:
            return {"e": "1000"}
        orders.append(p)
        return {"txid": ["O-1"]}

    monkeypatch.setattr(ex_mod.broker, "private", fake_private)
    _exec(conn, mode="live").place_entry(SYM, 100.0, Card(low_52w=92.0))
    assert orders[0]["oflags"] == "post"
    assert float(orders[0]["price"]) < 100.0      # bid below last -> rests as maker
    conn.close()


def test_verify_open_stops_skips_when_positions_unavailable(tmp_path, monkeypatch):
    """Transient OpenPositions failure must NOT abandon a real open position."""
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO orders(symbol,margin_pair,volume,leverage,stop,stop_txid,status) "
                 "VALUES(?,?,?,?,?,?, 'open')", (SYM, "XBTUSD:BTNL", 0.1, 10, 90.0, "OSTOP"))
    conn.commit()
    sent = []
    monkeypatch.setattr(ex_mod.broker, "open_positions", lambda: None)     # API failure
    monkeypatch.setattr(ex_mod.broker, "private", lambda *a, **k: sent.append(a) or None)
    monkeypatch.setattr(ex_mod.broker, "query_order", lambda t: sent.append(t) or None)
    _exec(conn, mode="live").verify_open_stops()
    assert conn.execute("SELECT status FROM orders WHERE symbol=?", (SYM,)).fetchone()[0] == "open"
    assert sent == []          # nothing queried, nothing (re-)placed
    conn.close()
