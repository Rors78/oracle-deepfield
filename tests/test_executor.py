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

def test_size_risk_2pct_off_the_stop(tmp_path):
    conn = _conn(tmp_path)
    e = _exec(conn)
    # equity 1000, risk 2% = $20; entry 100, stop 90 -> $10 stop dist -> vol 2.0
    s = e.size(SYM, entry=100.0, stop=90.0, leverage=10, equity=1000.0)
    assert abs(s["volume"] - 2.0) < 1e-9
    assert abs(s["notional"] - 200.0) < 1e-9
    assert abs(s["margin"] - 20.0) < 1e-9          # notional/leverage
    assert abs(s["actual_risk"] - 20.0) < 1e-9     # == 2% of equity
    conn.close()


def test_size_margin_cap_binds_on_tight_stop(tmp_path):
    conn = _conn(tmp_path)
    e = _exec(conn)
    # razor stop dist 0.5 -> naive vol = 20/0.5 = 40, notional 4000, margin 400.
    # cap: 0.9*1000*leverage/entry ... margin cap = 900 -> vol cap 900*10/100=90.
    # 40 < 90 so not capped here; make it bind with leverage 1.
    s = e.size(SYM, entry=100.0, stop=99.5, leverage=1, equity=1000.0)
    assert s["capped"] is True
    assert s["margin"] <= 1000.0 * config.MARGIN_CAP_PCT + 1e-6
    conn.close()


def test_size_floors_to_ordermin(tmp_path):
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


def test_rails_kill_switch_on_drawdown(tmp_path):
    conn = _conn(tmp_path)
    store.meta_set(conn, "peak_equity", 1000.0)
    ok, reason = _exec(conn).rails_ok(750.0)   # -25% > 20% DD limit
    assert not ok and "KILL SWITCH" in reason
    ok2, _ = _exec(conn).rails_ok(850.0)        # -15% within limit
    assert ok2
    conn.close()


def test_rails_max_positions_blocks(tmp_path, monkeypatch):
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
    assert captured["params"]["oflags"] == "post"            # post-only maker
    row = conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()
    assert row[0] == "validated"
    conn.close()
