"""Executor: 2%-risk sizing, stop clamp, risk rails, paper/off/validate paths.

Never touches the network (live/validate broker calls are monkeypatched). No
test can place a real order — paper and off are the only self-contained modes.
"""
import os
import time
import json
import sqlite3
import datetime

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


def test_size_conviction_weighted_min(tmp_path):
    """Tier 1: with a card, the min-mode order is CONVICTION-WEIGHTED by
    score-over-required (delta 0 -> 1.0x STARTER, +1 -> 1.5x, +2 -> 2.0x) —
    reusing the same engine.tranche the champion card shows, so the live fill
    matches the displayed qty. A 7/7 sizes exactly 2x a bare-threshold 5/7."""
    from deepfield import engine
    conn = _conn(tmp_path, ordermin=0.1, costmin=0.5, lot_dec=8)
    e = _exec(conn)   # min mode
    starter = Card(); starter.score, starter.required = 5, 5   # delta 0 -> 1.0x
    mid = Card(); mid.score, mid.required = 6, 5               # delta 1 -> 1.5x
    strong = Card(); strong.score, strong.required = 7, 5      # delta 2 -> 2.0x
    kw = dict(entry=100.0, stop=90.0, leverage=10, equity=1000.0)
    s0 = e.size(SYM, card=starter, **kw)
    s1 = e.size(SYM, card=mid, **kw)
    s2 = e.size(SYM, card=strong, **kw)
    assert (s0["conviction_mult"], s1["conviction_mult"], s2["conviction_mult"]) == (1.0, 1.5, 2.0)
    # the wiring contract: live size == the EXACT engine.tranche qty the card displays.
    for card, s in ((starter, s0), (mid, s1), (strong, s2)):
        qty, _ = engine.tranche(card.score, card.required, 0.1, 0.5, 8, 100.0)
        assert s["volume"] == qty
    assert s0["volume"] < s1["volume"] < s2["volume"]         # scales up with conviction
    assert s2["volume"] == 2.0 * s0["volume"]                 # 7/7 = exactly 2x the STARTER
    # conviction only scales UP: the STARTER equals the flat card-less min, and a
    # scaled rung reports it's no longer floored to the exchange minimum.
    flat = e.size(SYM, **kw)
    assert s0["volume"] == flat["volume"] and s0["floored_to_min"] is True
    assert s2["floored_to_min"] is False
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


def _seed_pending_entry(conn, txid, entry, stop=90.0, vol=0.1):
    """A resting entry WITH a fill/entry price — the ladder steps off this."""
    cur = conn.execute(
        "INSERT INTO orders(symbol,margin_pair,volume,leverage,stop,entry,txid,status,mode) "
        "VALUES(?,?,?,?,?,?,?, 'pending','live')", (SYM, "XBTUSD:BTNL", vol, 10, stop, entry, txid))
    conn.commit()
    return cur.lastrowid


def _ladder_private(sent):
    """AddOrder mock: stop-loss -> OSTOP, ladder buy-limit -> ORUNG."""
    def fp(ep, p=None, **kw):
        sent.append(p)
        if p and p.get("ordertype") == "stop-loss":
            return {"txid": ["OSTOP-1"]}
        return {"txid": ["ORUNG-1"]}
    return fp


def test_ladder_places_next_rung_on_fill(tmp_path, monkeypatch):
    """v6: a fill drops the NEXT post-only rung one LADDER_STEP_PCT below the fill,
    same support stop — accumulation continues without a candle close/restart."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(config, "LADDER_CONTINUOUS", True)
    monkeypatch.setattr(config, "LADDER_STEP_PCT", 0.01)
    monkeypatch.setattr(ex_mod.broker, "trade_balance", lambda: 1000.0)
    sent = []
    monkeypatch.setattr(ex_mod.broker, "private", _ladder_private(sent))
    monkeypatch.setattr(ex_mod.broker, "query_order", lambda t: {"status": "closed", "vol_exec": "0.1"})
    e = _exec(conn, mode="live")
    oid = _seed_pending_entry(conn, "OENTRY-L", entry=100.0, stop=90.0)
    e.poll_fills()
    assert conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()[0] == "open"
    rung = conn.execute("SELECT entry, stop, status FROM orders WHERE txid='ORUNG-1'").fetchone()
    assert rung is not None
    assert abs(rung[0] - 99.0) < 1e-6           # 100 * (1 - 0.01)
    assert abs(rung[1] - 90.0) < 1e-6           # SAME support stop
    assert rung[2] == "pending"
    assert any(p.get("type") == "buy" and p.get("oflags") == "post" for p in sent)   # post-only
    conn.close()


def test_ladder_holds_at_stop_floor(tmp_path, monkeypatch):
    """The natural floor: a rung that would land at/under the stop is not placed."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(config, "LADDER_CONTINUOUS", True)
    monkeypatch.setattr(config, "LADDER_STEP_PCT", 0.01)
    monkeypatch.setattr(ex_mod.broker, "trade_balance", lambda: 1000.0)
    sent = []
    monkeypatch.setattr(ex_mod.broker, "private", _ladder_private(sent))
    monkeypatch.setattr(ex_mod.broker, "query_order", lambda t: {"status": "closed", "vol_exec": "0.1"})
    e = _exec(conn, mode="live")
    _seed_pending_entry(conn, "OENTRY-F", entry=90.5, stop=90.0)   # 90.5*0.99=89.6 <= stop
    e.poll_fills()
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0] == 0
    assert all(p.get("ordertype") == "stop-loss" for p in sent)   # only the stop, no ladder rung
    conn.close()


def test_ladder_disabled_places_nothing(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(config, "LADDER_CONTINUOUS", False)
    monkeypatch.setattr(ex_mod.broker, "trade_balance", lambda: 1000.0)
    sent = []
    monkeypatch.setattr(ex_mod.broker, "private", _ladder_private(sent))
    monkeypatch.setattr(ex_mod.broker, "query_order", lambda t: {"status": "closed", "vol_exec": "0.1"})
    e = _exec(conn, mode="live")
    oid = _seed_pending_entry(conn, "OENTRY-D", entry=100.0, stop=90.0)
    e.poll_fills()
    assert conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()[0] == "open"
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0] == 0
    conn.close()


def test_ladder_failure_never_unwinds_fill(tmp_path, monkeypatch):
    """Isolation: if the ladder rung's AddOrder blows up, the fill still promotes and
    the protective stop still rests — laddering can never unwind secured protection."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(config, "LADDER_CONTINUOUS", True)
    monkeypatch.setattr(ex_mod.broker, "trade_balance", lambda: 1000.0)

    def fp(ep, p=None, **kw):
        if p and p.get("ordertype") == "stop-loss":
            return {"txid": ["OSTOP-1"]}
        raise RuntimeError("kraken down")     # the ladder buy-limit blows up
    monkeypatch.setattr(ex_mod.broker, "private", fp)
    monkeypatch.setattr(ex_mod.broker, "query_order", lambda t: {"status": "closed", "vol_exec": "0.1"})
    e = _exec(conn, mode="live")
    oid = _seed_pending_entry(conn, "OENTRY-X", entry=100.0, stop=90.0)
    e.poll_fills()                            # must not raise
    status, stop_txid = conn.execute(
        "SELECT status, stop_txid FROM orders WHERE id=?", (oid,)).fetchone()
    assert status == "open" and stop_txid == "OSTOP-1"
    conn.close()


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


def test_realized_pnl_ignores_nonjson_error(tmp_path):
    """A manual close writes plain text into the polymorphic `error` column
    ('closed manually by operator'); json_extract would raise 'malformed JSON'
    and crash the query (and the exec snapshot + rails). realized_pnl_since must
    skip such rows and still sum the JSON stop-exit rows. Regression for the bug
    the live TUI surfaced."""
    import json as _json
    conn = _conn(tmp_path)
    # a real stop-exit (JSON error) that should count
    conn.execute("INSERT INTO orders(symbol,status,error) VALUES(?, 'closed', ?)",
                 (SYM, _json.dumps({"pnl": -2.5, "exit": "stop",
                                    "closed_ts": "2026-07-05T10:00:00+00:00"})))
    # a manual close with PLAIN TEXT error — must not crash, contributes nothing
    conn.execute("INSERT INTO orders(symbol,status,error) VALUES(?, 'closed', ?)",
                 (SYM, "closed manually by operator"))
    conn.commit()
    got = store.realized_pnl_since(conn, "2026-07-05T00:00:00+00:00")
    assert abs(got - (-2.5)) < 1e-9        # JSON row counted, plain-text row skipped (no crash)
    conn.close()


def test_journal_rows_emitted_on_order_fill_stop(tmp_path, monkeypatch):
    """v6 stage5: the money-path lifecycle narrates itself into the JOURNAL —
    a resting live entry emits 'order', a confirmed fill emits 'fill', the
    protective stop emits 'stop'."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(ex_mod.broker, "private",
                        lambda ep, p=None, **kw: ({"txid": ["OENTRY-1"]}
                                                  if p and p.get("ordertype") == "limit"
                                                  else {"txid": ["OSTOP-1"]}))
    monkeypatch.setattr(ex_mod.broker, "query_order",
                        lambda t: {"status": "closed", "vol_exec": "0.1"})
    e = _exec(conn, mode="live")
    e.place_entry(SYM, 100.0, Card(low_52w=92.0))     # -> 'order'
    e.poll_fills()                                     # -> 'fill' + 'stop'
    kinds = [r[1] for r in store.recent_journal(conn, 20)]
    assert "order" in kinds and "fill" in kinds and "stop" in kinds
    conn.close()


def test_journal_failure_never_blocks_fill(tmp_path, monkeypatch):
    """v6 acceptance #5: the JOURNAL is display-truth, not the money path. A
    store.journal that raises inside poll_fills must NOT stop the fill from
    promoting or the protective stop from resting — same isolation rule as the
    alerter/dispatch fix. Proven by making journal blow up on every call."""
    conn = _conn(tmp_path)
    seq = []
    monkeypatch.setattr(ex_mod.broker, "private",
                        lambda ep, p=None, **kw: (seq.append(p) or {"txid": ["OSTOP-9"]}))
    monkeypatch.setattr(ex_mod.broker, "query_order",
                        lambda txid: {"status": "closed", "vol_exec": "0.1"})

    def _boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")   # journal write fails hard
    monkeypatch.setattr(ex_mod.store, "journal", _boom)

    e = _exec(conn, mode="live")
    oid = _seed_pending(conn, "OENTRY-J")
    e.poll_fills()                        # must not raise despite journal blowing up
    status, stop_txid, vol = conn.execute(
        "SELECT status,stop_txid,volume FROM orders WHERE id=?", (oid,)).fetchone()
    assert status == "open"               # fill still promoted
    assert stop_txid == "OSTOP-9"         # protective stop still rested
    assert abs(vol - 0.1) < 1e-9
    assert any(p.get("ordertype") == "stop-loss" for p in seq)
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


def test_poll_fills_partial_uses_terminal_volume_after_cancel(tmp_path, monkeypatch):
    """FINDING 4: on a partial-while-resting fill, size the position and stop off the
    volume re-read AFTER the cancel settles — more can fill between the first query and
    the cancel landing, so the first snapshot would be short."""
    conn = _conn(tmp_path)
    oid = _seed_pending(conn, "OENTRY-P")
    q = iter([
        {"status": "open", "vol_exec": "0.05"},        # partial, still resting (snapshot)
        {"status": "canceled", "vol_exec": "0.08"},    # settled: 0.08 filled before cancel
    ])
    monkeypatch.setattr(ex_mod.broker, "query_order", lambda t: next(q))
    cancels = []
    monkeypatch.setattr(ex_mod.broker, "cancel_order", lambda t: cancels.append(t) or {"count": 1})
    stops = []
    monkeypatch.setattr(ex_mod.broker, "private",
                        lambda ep, p=None, **kw: (stops.append(p) or {"txid": ["OSTOP-P"]}))
    e = _exec(conn, mode="live")
    e.poll_fills()
    status, vol, stop_txid = conn.execute(
        "SELECT status,volume,stop_txid FROM orders WHERE id=?", (oid,)).fetchone()
    assert status == "open"
    assert abs(vol - 0.08) < 1e-9              # terminal volume, NOT the 0.05 snapshot
    assert cancels == ["OENTRY-P"]
    assert stop_txid == "OSTOP-P"
    assert stops and float(stops[0]["volume"]) == 0.08   # stop sized to the settled fill
    conn.close()


def test_poll_fills_partial_cancel_failure_stays_pending(tmp_path, monkeypatch):
    """FINDING 4: a FAILED cancel must NOT transition the row — flipping to 'open'
    would orphan the still-resting remainder (poll_fills only revisits 'pending')."""
    conn = _conn(tmp_path)
    oid = _seed_pending(conn, "OENTRY-Q")
    monkeypatch.setattr(ex_mod.broker, "query_order", lambda t: {"status": "open", "vol_exec": "0.05"})
    monkeypatch.setattr(ex_mod.broker, "cancel_order", lambda t: None)   # cancel FAILS
    calls = []
    monkeypatch.setattr(ex_mod.broker, "private", lambda ep, p=None, **kw: calls.append(p) or {"txid": ["X"]})
    _exec(conn, mode="live").poll_fills()
    status, stop_txid = conn.execute("SELECT status,stop_txid FROM orders WHERE id=?", (oid,)).fetchone()
    assert status == "pending"                # remainder not orphaned; will retry
    assert stop_txid is None                  # no stop rested against an unsettled order
    assert calls == []                        # nothing placed
    conn.close()


def test_poll_fills_partial_not_terminal_after_cancel_stays_pending(tmp_path, monkeypatch):
    """FINDING 4: cancel accepted but the order is not terminal yet -> do not transition
    (it can still fill); converge on a later cycle."""
    conn = _conn(tmp_path)
    oid = _seed_pending(conn, "OENTRY-R")
    monkeypatch.setattr(ex_mod.broker, "query_order", lambda t: {"status": "open", "vol_exec": "0.05"})
    monkeypatch.setattr(ex_mod.broker, "cancel_order", lambda t: {"count": 1})
    calls = []
    monkeypatch.setattr(ex_mod.broker, "private", lambda ep, p=None, **kw: calls.append(p) or {"txid": ["X"]})
    _exec(conn, mode="live").poll_fills()
    status, stop_txid = conn.execute("SELECT status,stop_txid FROM orders WHERE id=?", (oid,)).fetchone()
    assert status == "pending"
    assert stop_txid is None
    assert calls == []
    conn.close()


def _seed_pending_ts(conn, txid, ts_iso, vol=0.1):
    cur = conn.execute(
        "INSERT INTO orders(symbol,margin_pair,volume,leverage,stop,txid,status,ts) "
        "VALUES(?,?,?,?,?,?, 'pending', ?)", (SYM, "XBTUSD:BTNL", vol, 10, 90.0, txid, ts_iso))
    conn.commit()
    return cur.lastrowid


def test_poll_fills_expires_stale_unfilled_bid(tmp_path, monkeypatch):
    """FINDING 5: an unfilled post-only bid older than ENTRY_TTL_SECS is canceled so
    stale bids don't pile up against Kraken's open-order cap and crowd out stops."""
    conn = _conn(tmp_path)
    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)).isoformat()
    oid = _seed_pending_ts(conn, "OLD-BID", old)
    monkeypatch.setattr(config, "ENTRY_TTL_SECS", 86400)          # 1 day
    q = iter([{"status": "open", "vol_exec": "0"},                # still resting, unfilled
              {"status": "canceled", "vol_exec": "0"}])           # terminal after cancel
    monkeypatch.setattr(ex_mod.broker, "query_order", lambda t: next(q))
    cancels = []
    monkeypatch.setattr(ex_mod.broker, "cancel_order", lambda t: cancels.append(t) or {"count": 1})
    calls = []
    monkeypatch.setattr(ex_mod.broker, "private", lambda ep, p=None, **kw: calls.append(p) or {"txid": ["X"]})
    _exec(conn, mode="live").poll_fills()
    status, stop_txid = conn.execute("SELECT status,stop_txid FROM orders WHERE id=?", (oid,)).fetchone()
    assert status == "canceled"           # stale bid expired
    assert cancels == ["OLD-BID"]
    assert stop_txid is None and calls == []   # no position opened, no stop placed
    conn.close()


def test_poll_fills_keeps_fresh_unfilled_bid(tmp_path, monkeypatch):
    """A fresh unfilled bid within TTL is left resting (patient bottom bid)."""
    conn = _conn(tmp_path)
    fresh = datetime.datetime.now(datetime.timezone.utc).isoformat()
    oid = _seed_pending_ts(conn, "FRESH-BID", fresh)
    monkeypatch.setattr(config, "ENTRY_TTL_SECS", 86400)
    monkeypatch.setattr(ex_mod.broker, "query_order", lambda t: {"status": "open", "vol_exec": "0"})
    cancels = []
    monkeypatch.setattr(ex_mod.broker, "cancel_order", lambda t: cancels.append(t) or {"count": 1})
    _exec(conn, mode="live").poll_fills()
    assert conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()[0] == "pending"
    assert cancels == []                  # left resting, not canceled
    conn.close()


def test_poll_fills_ttl_disabled_keeps_old_bid(tmp_path, monkeypatch):
    """ENTRY_TTL_SECS=0 disables expiry — an old unfilled bid is left alone."""
    conn = _conn(tmp_path)
    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)).isoformat()
    oid = _seed_pending_ts(conn, "ANCIENT-BID", old)
    monkeypatch.setattr(config, "ENTRY_TTL_SECS", 0)
    monkeypatch.setattr(ex_mod.broker, "query_order", lambda t: {"status": "open", "vol_exec": "0"})
    cancels = []
    monkeypatch.setattr(ex_mod.broker, "cancel_order", lambda t: cancels.append(t) or {"count": 1})
    _exec(conn, mode="live").poll_fills()
    assert conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()[0] == "pending"
    assert cancels == []
    conn.close()


def test_verify_records_realized_pnl_on_stop_exit_bucketed_by_close(tmp_path, monkeypatch):
    """FINDING 6: a stop-triggered close records realized P&L from Kraken execution
    records, bucketed by CLOSE date. The entry was 3 days ago; the loss must still count
    toward TODAY's realized_pnl_since (the loss caps ask a realization-date question)."""
    conn = _conn(tmp_path)
    three_days_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)).isoformat()
    cur = conn.execute(
        "INSERT INTO orders(symbol,margin_pair,volume,leverage,stop,txid,stop_txid,status,ts,entry) "
        "VALUES(?,?,?,?,?,?,?, 'open', ?, ?)",
        (SYM, "XBTUSD:BTNL", 0.1, 10, 90.0, "OENTRY", "OSTOP", three_days_ago, 100.0))
    conn.commit()
    oid = cur.lastrowid
    monkeypatch.setattr(ex_mod.broker, "open_positions", lambda: {})     # pair flat -> row unbacked

    def fake_qo(txid):
        if txid == "OSTOP":                                             # stop triggered (sell exit)
            return {"status": "closed", "vol_exec": "0.1", "cost": "9.0", "fee": "0.02"}
        if txid == "OENTRY":                                            # entry buy fill
            return {"status": "closed", "vol_exec": "0.1", "cost": "10.0", "fee": "0.03"}
        return None
    monkeypatch.setattr(ex_mod.broker, "query_order", fake_qo)
    monkeypatch.setattr(ex_mod.broker, "cancel_order", lambda t: {"count": 1})
    monkeypatch.setattr(ex_mod.broker, "private", lambda *a, **k: None)
    _exec(conn, mode="live").verify_open_stops()

    status, err = conn.execute("SELECT status,error FROM orders WHERE id=?", (oid,)).fetchone()
    assert status == "closed"
    obj = json.loads(err)
    # pnl = (stop_cost - stop_fee) - (entry_cost + entry_fee) = (9.0-0.02) - (10.0+0.03) = -1.05
    assert abs(obj["pnl"] - (-1.05)) < 1e-6 and obj["exit"] == "stop" and "closed_ts" in obj
    now = datetime.datetime.now(datetime.timezone.utc)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    assert abs(store.realized_pnl_since(conn, day0) - (-1.05)) < 1e-6   # counts toward TODAY
    conn.close()


def test_notional_ceiling_refuses_oversize_order(tmp_path, monkeypatch):
    """FINDING 8: an order whose notional exceeds the ceiling is REFUSED and never sent
    (even in paper) — the guard against a corrupt pairs row / flipped size mode."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(config, "EXEC_MAX_ORDER_NOTIONAL_USD", 0.25)   # below the ~$0.50 min order
    e = _exec(conn, mode="paper")
    assert e.place_entry(SYM, 100.0, Card(low_52w=92.0)) is None
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0   # nothing recorded/sent
    conn.close()


def test_notional_ceiling_allows_normal_min_order(tmp_path, monkeypatch):
    """A normal min-size order is far under the ceiling and places fine."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(config, "EXEC_MAX_ORDER_NOTIONAL_USD", 50.0)
    e = _exec(conn, mode="paper")
    assert e.place_entry(SYM, 100.0, Card(low_52w=92.0)) is not None
    conn.close()


def test_notional_ceiling_zero_disables(tmp_path, monkeypatch):
    """0 disables the ceiling entirely (no order is ever refused on notional)."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(config, "EXEC_MAX_ORDER_NOTIONAL_USD", 0)
    e = _exec(conn, mode="paper")
    assert e.place_entry(SYM, 100.0, Card(low_52w=92.0)) is not None
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


# ── Finding 1: per-pair volume reconciliation (stacked positions) ─────────────

def _seed_open(conn, stop_txid, vol=0.1, stop=90.0):
    """Seed one OPEN stacked long on the shared pair with its own protective stop."""
    cur = conn.execute(
        "INSERT INTO orders(symbol,margin_pair,volume,leverage,stop,stop_txid,status) "
        "VALUES(?,?,?,?,?,?, 'open')", (SYM, "XBTUSD:BTNL", vol, 10, stop, stop_txid))
    conn.commit()
    return cur.lastrowid


def _pos(vol):
    """A Kraken OpenPositions entry (long) on the BTC pair, rest-name form."""
    return {"pair": "XXBTZUSD", "type": "buy", "vol": str(vol), "vol_closed": "0"}


def _wire_broker(monkeypatch, positions, stop_status, sent):
    """positions: dict of Kraken OpenPositions; stop_status: txid->status; sent: sink."""
    monkeypatch.setattr(ex_mod.broker, "open_positions", lambda: positions)
    monkeypatch.setattr(ex_mod.broker, "query_order", lambda t: {"status": stop_status.get(t)} if t else None)
    monkeypatch.setattr(ex_mod.broker, "private",
                        lambda ep, p=None, **kw: (sent.append(("private", p)) or {"txid": ["ONEWSTOP"]}))
    monkeypatch.setattr(ex_mod.broker, "cancel_order", lambda t: sent.append(("cancel", t)) or {})


def test_verify_stacked_triggered_row_not_reprotected(tmp_path, monkeypatch):
    """FINDING 1 regression. Three stacked longs on one pair; the newest row's stop
    already TRIGGERED (position closed). Kraken shows only 2 positions of open volume.
    The triggered row must be CLOSED, and NO duplicate stop placed for it — the old
    per-pair-presence logic would have re-placed it, pushing stop volume above open
    volume -> a short on the next sweep."""
    conn = _conn(tmp_path)
    a = _seed_open(conn, "OSTOP-A")
    b = _seed_open(conn, "OSTOP-B")
    c = _seed_open(conn, "OSTOP-C")            # its stop triggered -> position gone
    sent = []
    _wire_broker(monkeypatch,
                 positions={"P1": _pos(0.1), "P2": _pos(0.1)},   # only 0.2 open (2 of 3)
                 stop_status={"OSTOP-A": "open", "OSTOP-B": "open", "OSTOP-C": "closed"},
                 sent=sent)
    _exec(conn, mode="live").verify_open_stops()
    st = dict(conn.execute("SELECT id,status FROM orders").fetchall())
    assert st[a] == "open" and st[b] == "open"          # backed rows keep their stops
    assert st[c] == "closed"                             # unbacked row retired
    assert not any(kind == "private" for kind, _ in sent)   # NO stop (re-)placed anywhere
    assert not any(kind == "cancel" for kind, _ in sent)    # C's stop already gone -> nothing to cancel
    conn.close()


def test_verify_unbacked_row_with_resting_stop_is_canceled(tmp_path, monkeypatch):
    """An unbacked row whose stop somehow STILL rests -> the orphan is canceled
    (a stop-sell with no position opens a short)."""
    conn = _conn(tmp_path)
    a = _seed_open(conn, "OSTOP-A")
    b = _seed_open(conn, "OSTOP-B")            # unbacked, but stop still resting
    sent = []
    _wire_broker(monkeypatch,
                 positions={"P1": _pos(0.1)},                    # only 1 position open
                 stop_status={"OSTOP-A": "open", "OSTOP-B": "open"},
                 sent=sent)
    _exec(conn, mode="live").verify_open_stops()
    st = dict(conn.execute("SELECT id,status FROM orders").fetchall())
    assert st[a] == "open" and st[b] == "closed"
    assert ("cancel", "OSTOP-B") in sent                # orphan stop canceled
    assert not any(kind == "private" for kind, _ in sent)
    conn.close()


def test_verify_backed_row_missing_stop_is_reprotected(tmp_path, monkeypatch):
    """The legit single-position case still works: a backed row whose stop is
    DEFINITELY gone gets exactly one re-placed stop."""
    conn = _conn(tmp_path)
    a = _seed_open(conn, "OSTOP-A")
    sent = []
    _wire_broker(monkeypatch,
                 positions={"P1": _pos(0.1)},                    # position backs the row
                 stop_status={"OSTOP-A": "canceled"},            # stop gone
                 sent=sent)
    _exec(conn, mode="live").verify_open_stops()
    assert conn.execute("SELECT status FROM orders WHERE id=?", (a,)).fetchone()[0] == "open"
    placed = [p for kind, p in sent if kind == "private"]
    assert len(placed) == 1                              # exactly one re-place
    assert placed[0]["type"] == "sell" and placed[0]["ordertype"] == "stop-loss"
    assert conn.execute("SELECT stop_txid FROM orders WHERE id=?", (a,)).fetchone()[0] == "ONEWSTOP"
    conn.close()


def test_verify_whole_pair_gone_closes_all_and_cancels_stops(tmp_path, monkeypatch):
    """Whole pair flat on Kraken -> every open row closed; any resting stop canceled;
    nothing re-placed."""
    conn = _conn(tmp_path)
    a = _seed_open(conn, "OSTOP-A")
    b = _seed_open(conn, "OSTOP-B")
    sent = []
    _wire_broker(monkeypatch,
                 positions={},                                   # nothing open
                 stop_status={"OSTOP-A": "open", "OSTOP-B": "closed"},
                 sent=sent)
    _exec(conn, mode="live").verify_open_stops()
    st = dict(conn.execute("SELECT id,status FROM orders").fetchall())
    assert st[a] == "closed" and st[b] == "closed"
    assert ("cancel", "OSTOP-A") in sent                # A's stop still rested -> canceled
    assert not any(kind == "private" for kind, _ in sent)
    conn.close()


def test_verify_orphan_cancel_precedes_reprotect(tmp_path, monkeypatch):
    """Two-pass ordering: ALL removals (orphan-stop cancels) happen before ANY
    re-place, so total resting-stop volume never transiently exceeds open volume —
    the exact excess-stop condition that would open a short on a simultaneous sweep."""
    conn = _conn(tmp_path)
    a = _seed_open(conn, "OSTOP-A")            # backed, but its stop is GONE -> re-place
    b = _seed_open(conn, "OSTOP-B")            # backed, stop resting -> fine
    c = _seed_open(conn, "OSTOP-C")            # unbacked, stop still resting -> cancel orphan
    sent = []
    _wire_broker(monkeypatch,
                 positions={"P1": _pos(0.1), "P2": _pos(0.1)},   # 0.2 open backs A+B, not C
                 stop_status={"OSTOP-A": "canceled", "OSTOP-B": "open", "OSTOP-C": "open"},
                 sent=sent)
    _exec(conn, mode="live").verify_open_stops()
    kinds = [k for k, _ in sent]
    assert ("cancel", "OSTOP-C") in sent and any(k == "private" for k in kinds)
    assert kinds.index("cancel") < kinds.index("private")   # cancel (removal) BEFORE re-place (add)
    st = dict(conn.execute("SELECT id,status FROM orders").fetchall())
    assert st[a] == "open" and st[b] == "open" and st[c] == "closed"
    conn.close()


def test_verify_short_position_not_counted_as_long(tmp_path, monkeypatch):
    """A short (type=sell) on the pair must NOT count as long volume — otherwise it
    would back a row that has no real long behind it and keep an excess stop."""
    conn = _conn(tmp_path)
    a = _seed_open(conn, "OSTOP-A")
    sent = []
    _wire_broker(monkeypatch,
                 positions={"pBuy": {"pair": "SOLUSD", "type": "buy", "vol": "1", "vol_closed": "0"},
                            "pSell": {"pair": "XXBTZUSD", "type": "sell", "vol": "0.5", "vol_closed": "0"}},
                 stop_status={"OSTOP-A": "open"},
                 sent=sent)
    _exec(conn, mode="live").verify_open_stops()
    # BTC row sees only the excluded short -> 0 long -> unbacked -> closed + stop canceled.
    assert conn.execute("SELECT status FROM orders WHERE id=?", (a,)).fetchone()[0] == "closed"
    assert ("cancel", "OSTOP-A") in sent
    conn.close()


def test_verify_bails_on_unparseable_positions_shape(tmp_path, monkeypatch):
    """If OpenPositions is non-empty but no entry parses to a long volume, the shape
    is unexpected — bail like 'could not check', never strip real stops."""
    conn = _conn(tmp_path)
    a = _seed_open(conn, "OSTOP-A")
    sent = []
    _wire_broker(monkeypatch,
                 positions={"p": {"foo": "bar"}},               # unrecognizable shape
                 stop_status={"OSTOP-A": "open"},
                 sent=sent)
    _exec(conn, mode="live").verify_open_stops()
    assert conn.execute("SELECT status FROM orders WHERE id=?", (a,)).fetchone()[0] == "open"
    assert sent == []                                           # nothing canceled or placed
    conn.close()
