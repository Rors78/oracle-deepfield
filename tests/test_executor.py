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
