"""Executor: 2%-risk sizing, stop clamp, risk rails, paper/off/validate paths.

Never touches the network (live/validate broker calls are monkeypatched). No
test can place a real order — paper and off are the only self-contained modes.
"""
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
    score-over-required (delta 0 -> 1.0x STARTER, +1 -> 2.0x, +2 -> 3.0x) —
    reusing the same engine.tranche the champion card shows, so the live fill
    matches the displayed qty. A 7/7 sizes exactly 3x a bare-threshold 5/7."""
    from deepfield import engine
    conn = _conn(tmp_path, ordermin=0.1, costmin=0.5, lot_dec=8)
    e = _exec(conn)   # min mode
    starter = Card(); starter.score, starter.required = 5, 5   # delta 0 -> 1.0x
    mid = Card(); mid.score, mid.required = 6, 5               # delta 1 -> 2.0x
    strong = Card(); strong.score, strong.required = 7, 5      # delta 2 -> 3.0x
    kw = dict(entry=100.0, stop=90.0, leverage=10, equity=1000.0)
    s0 = e.size(SYM, card=starter, **kw)
    s1 = e.size(SYM, card=mid, **kw)
    s2 = e.size(SYM, card=strong, **kw)
    assert (s0["conviction_mult"], s1["conviction_mult"], s2["conviction_mult"]) == (1.0, 2.0, 3.0)
    # the wiring contract: live size == the EXACT engine.tranche qty the card displays.
    for card, s in ((starter, s0), (mid, s1), (strong, s2)):
        qty, _ = engine.tranche(card.score, card.required, 0.1, 0.5, 8, 100.0)
        assert s["volume"] == qty
    assert s0["volume"] < s1["volume"] < s2["volume"]         # scales up with conviction
    assert abs(s1["volume"] - 2.0 * s0["volume"]) < 1e-6      # 6/7 ~ 2x the STARTER
    assert abs(s2["volume"] - 3.0 * s0["volume"]) < 1e-6      # 7/7 ~ 3x (exact qty via engine.tranche above)
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
    conn.execute("INSERT INTO orders(symbol,status,mode) VALUES('X/USD','open','live')")
    conn.commit()
    ok, reason = _exec(conn, mode="live").rails_ok(1000.0)
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
        "INSERT INTO orders(symbol,margin_pair,volume,leverage,stop,txid,status,mode) "
        "VALUES(?,?,?,?,?,?, 'pending','live')", (SYM, "XBTUSD:BTNL", 0.1, 10, stop, txid))
    conn.commit()
    return cur.lastrowid


def _seed_pending_entry(conn, txid, entry, stop=90.0, vol=0.1, score=None, required=None):
    """A resting entry WITH a fill/entry price — the ladder steps off this.
    Optional score/required = the entry's conviction that rides down the chain."""
    cur = conn.execute(
        "INSERT INTO orders(symbol,margin_pair,volume,leverage,stop,entry,score,required,txid,status,mode) "
        "VALUES(?,?,?,?,?,?,?,?,?, 'pending','live')",
        (SYM, "XBTUSD:BTNL", vol, 10, stop, entry, score, required, txid))
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


def test_ladder_rung_inherits_entry_conviction(tmp_path, monkeypatch):
    """Combine: the auto-placed rung is CONVICTION-sized off the entry's score —
    a 7/7 position (required 5 -> 3.0x) ladders a 3x rung, NOT a flat min, and the
    score/required are persisted on the rung so it rides down the chain."""
    conn = _conn(tmp_path, ordermin=0.1, costmin=0.5, lot_dec=8)
    monkeypatch.setattr(config, "LADDER_CONTINUOUS", True)
    monkeypatch.setattr(config, "LADDER_STEP_PCT", 0.01)
    monkeypatch.setattr(ex_mod.broker, "trade_balance", lambda: 1000.0)
    monkeypatch.setattr(ex_mod.broker, "private", _ladder_private([]))
    monkeypatch.setattr(ex_mod.broker, "query_order", lambda t: {"status": "closed", "vol_exec": "0.3"})
    e = _exec(conn, mode="live")
    _seed_pending_entry(conn, "OENTRY-7", entry=100.0, stop=90.0, vol=0.3, score=7, required=5)
    e.poll_fills()
    entry_px, vol, sc, rq = conn.execute(
        "SELECT entry, volume, score, required FROM orders WHERE txid='ORUNG-1'").fetchone()
    assert abs(entry_px - 99.0) < 1e-6          # one 1% step below the fill
    assert abs(vol - 0.3) < 1e-6                # 3.0x conviction min (0.1*3), NOT flat 0.1
    assert (sc, rq) == (7, 5)                   # conviction persisted for the next rung
    conn.close()


def test_ladder_conviction_does_not_decay_down_the_chain(tmp_path, monkeypatch):
    """rung1 fill -> rung2: the frozen entry conviction propagates row-to-row —
    the second hop is still 3x, not silently reset to flat min."""
    conn = _conn(tmp_path, ordermin=0.1, costmin=0.5, lot_dec=8)
    monkeypatch.setattr(config, "LADDER_CONTINUOUS", True)
    monkeypatch.setattr(config, "LADDER_STEP_PCT", 0.01)
    # Conviction propagation only — take the respend governor out of the picture. With
    # it armed at its live settings the $30 rung1 drains the $40 burst and rung2 is
    # paced away, so this test measured the throttle instead of the thing it names.
    monkeypatch.setattr(config, "RESPEND_BUDGET_USD_PER_HR", 0.0)
    monkeypatch.setattr(ex_mod.broker, "trade_balance", lambda: 1000.0)
    n = {"i": 0}

    def private(ep, p=None, **kw):
        if p and p.get("ordertype") == "stop-loss":
            return {"txid": ["OSTOP"]}
        n["i"] += 1
        return {"txid": [f"ORUNG-{n['i']}"]}    # unique txid per rung so the chain advances
    monkeypatch.setattr(ex_mod.broker, "private", private)
    monkeypatch.setattr(ex_mod.broker, "query_order", lambda t: {"status": "closed", "vol_exec": "0.3"})
    e = _exec(conn, mode="live")
    _seed_pending_entry(conn, "OENTRY-7", entry=100.0, stop=90.0, vol=0.3, score=7, required=5)
    e.poll_fills()      # entry fills -> rung1 placed
    e.poll_fills()      # rung1 fills -> rung2 placed
    rungs = conn.execute(
        "SELECT volume, score, required FROM orders WHERE txid LIKE 'ORUNG-%' ORDER BY id").fetchall()
    assert len(rungs) == 2
    for vol, sc, rq in rungs:
        assert abs(vol - 0.3) < 1e-6 and (sc, rq) == (7, 5)   # 3x conviction, never decays to 1x
    conn.close()


def test_ladder_rung_null_score_falls_back_to_flat_min(tmp_path, monkeypatch):
    """A row with no persisted score (pre-migration order / paper edge) -> the rung
    sizes at flat 1.0x min, never crashing on a NULL score."""
    conn = _conn(tmp_path, ordermin=0.1, costmin=0.5, lot_dec=8)
    monkeypatch.setattr(config, "LADDER_CONTINUOUS", True)
    monkeypatch.setattr(config, "LADDER_STEP_PCT", 0.01)
    monkeypatch.setattr(ex_mod.broker, "trade_balance", lambda: 1000.0)
    monkeypatch.setattr(ex_mod.broker, "private", _ladder_private([]))
    monkeypatch.setattr(ex_mod.broker, "query_order", lambda t: {"status": "closed", "vol_exec": "0.1"})
    e = _exec(conn, mode="live")
    _seed_pending_entry(conn, "OENTRY-N", entry=100.0, stop=90.0, vol=0.1)   # no score
    e.poll_fills()
    vol = conn.execute("SELECT volume FROM orders WHERE txid='ORUNG-1'").fetchone()[0]
    assert abs(vol - 0.1) < 1e-9                # flat min, not scaled
    conn.close()


def test_ladder_dedupe_skips_owned_level(tmp_path, monkeypatch):
    """Level-dedupe: the ladder does NOT place a rung at a price it already owns
    (within half a step) — so it descends cleanly instead of re-buying a band."""
    conn = _conn(tmp_path, ordermin=0.1, costmin=0.5, lot_dec=8)
    monkeypatch.setattr(config, "LADDER_CONTINUOUS", True)
    monkeypatch.setattr(config, "LADDER_STEP_PCT", 0.01)
    monkeypatch.setattr(ex_mod.broker, "trade_balance", lambda: 1000.0)
    monkeypatch.setattr(ex_mod.broker, "private", _ladder_private([]))
    monkeypatch.setattr(ex_mod.broker, "query_order", lambda t: {"status": "closed", "vol_exec": "0.1"})
    e = _exec(conn, mode="live")
    # already hold an OPEN rung at ~99.0 — exactly where the fill below would ladder to
    conn.execute("INSERT INTO orders(symbol,margin_pair,volume,leverage,stop,entry,status,mode) "
                 "VALUES(?,?,?,?,?,?, 'open','live')", (SYM, "XBTUSD:BTNL", 0.1, 10, 90.0, 99.0))
    conn.commit()
    _seed_pending_entry(conn, "OENTRY-D", entry=100.0, stop=90.0, vol=0.1)   # fills @100 -> target 99.0
    e.poll_fills()
    # target 99.0 is already owned -> NO new rung placed (only the stop went out)
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE txid='ORUNG-1'").fetchone()[0] == 0
    conn.close()


def test_ladder_dedupe_allows_a_clean_step_down(tmp_path, monkeypatch):
    """Control: with nothing owned near the target, the rung IS placed — dedupe
    only suppresses re-buying a level, never a genuine next step down."""
    conn = _conn(tmp_path, ordermin=0.1, costmin=0.5, lot_dec=8)
    monkeypatch.setattr(config, "LADDER_CONTINUOUS", True)
    monkeypatch.setattr(config, "LADDER_STEP_PCT", 0.01)
    monkeypatch.setattr(ex_mod.broker, "trade_balance", lambda: 1000.0)
    monkeypatch.setattr(ex_mod.broker, "private", _ladder_private([]))
    monkeypatch.setattr(ex_mod.broker, "query_order", lambda t: {"status": "closed", "vol_exec": "0.1"})
    e = _exec(conn, mode="live")
    # own a rung far below (95.0) — nowhere near the 99.0 target
    conn.execute("INSERT INTO orders(symbol,margin_pair,volume,leverage,stop,entry,status,mode) "
                 "VALUES(?,?,?,?,?,?, 'open','live')", (SYM, "XBTUSD:BTNL", 0.1, 10, 90.0, 95.0))
    conn.commit()
    _seed_pending_entry(conn, "OENTRY-C", entry=100.0, stop=90.0, vol=0.1)   # fills @100 -> target 99.0
    e.poll_fills()
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE txid='ORUNG-1'").fetchone()[0] == 1
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
    # Rails re-arm 2026-07-30: armed rails block live placement on an unknown
    # equity (the kill-switch can't be evaluated blind) — this test is about
    # journal narration, so give it a live equity read.
    monkeypatch.setattr(ex_mod.broker, "trade_balance", lambda: 1000.0)
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
        "INSERT INTO orders(symbol,margin_pair,volume,leverage,stop,txid,status,ts,mode) "
        "VALUES(?,?,?,?,?,?, 'pending', ?,'live')", (SYM, "XBTUSD:BTNL", vol, 10, 90.0, txid, ts_iso))
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
        "INSERT INTO orders(symbol,margin_pair,volume,leverage,stop,txid,stop_txid,status,ts,entry,mode) "
        "VALUES(?,?,?,?,?,?,?, 'open', ?, ?,'live')",
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
    # reconcile fetches the stop status via the batch path; mirror fake_qo into it.
    monkeypatch.setattr(ex_mod.broker, "query_orders",
                        lambda txids: {t: fake_qo(t) for t in txids if t and fake_qo(t)})
    monkeypatch.setattr(ex_mod.broker, "open_orders", lambda: {})
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

    def fake_private(endpoint, params=None, **kw):
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
    conn.execute("INSERT INTO orders(symbol,status,mode) VALUES('X/USD','pending','live')")
    conn.commit()
    ok, reason = _exec(conn, mode="live").rails_ok(1000.0)
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
        "INSERT INTO orders(symbol,margin_pair,volume,leverage,stop,stop_txid,status,mode) "
        "VALUES(?,?,?,?,?,?, 'open','live')", (SYM, "XBTUSD:BTNL", vol, 10, stop, stop_txid))
    conn.commit()
    return cur.lastrowid


def _pos(vol):
    """A Kraken OpenPositions entry (long) on the BTC pair, rest-name form."""
    return {"pair": "XXBTZUSD", "type": "buy", "vol": str(vol), "vol_closed": "0"}


def _wire_broker(monkeypatch, positions, stop_status, sent, open_orders=None, cancel_ok=True):
    """positions: Kraken OpenPositions; stop_status: txid->status (a value of None means
    that txid is ABSENT from the batch map, i.e. status UNKNOWN — a failed query); sent:
    sink; open_orders: Kraken OpenOrders map for the adopt path (default {} = nothing to
    adopt); cancel_ok=False -> CancelOrder fails (returns None) after recording the try."""
    monkeypatch.setattr(ex_mod.broker, "open_positions", lambda: positions)
    monkeypatch.setattr(ex_mod.broker, "open_orders", lambda: (open_orders or {}))
    monkeypatch.setattr(ex_mod.broker, "query_order",
                        lambda t: {"status": stop_status[t]} if t and stop_status.get(t) is not None else None)
    # reconcile batches its stop-status lookups through query_orders; a txid whose status
    # is None is OMITTED from the map so the call site reads it as None (UNKNOWN).
    monkeypatch.setattr(ex_mod.broker, "query_orders",
                        lambda txids: {t: {"status": stop_status[t]} for t in txids if t and stop_status.get(t) is not None})
    monkeypatch.setattr(ex_mod.broker, "private",
                        lambda ep, p=None, **kw: (sent.append(("private", p)) or {"txid": ["ONEWSTOP"]}))
    monkeypatch.setattr(ex_mod.broker, "cancel_order",
                        lambda t: (sent.append(("cancel", t)) or ({} if cancel_ok else None)))


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


def test_verify_replace_gated_on_fresh_backing(tmp_path, monkeypatch):
    """07-29 ADA race regression: the sweep-START snapshot shows the position
    backed, but by the time PASS 2 reaches the re-place the position is GONE
    (operator hand-canceled the stops, then market-closed — the sweep raced the
    close). The stop must NOT be re-placed off the stale snapshot: the fresh
    re-read says unbacked, so the row's stop_txid clears and the per-poll
    _reprotect_naked_open (definite-state, backing-gated) owns re-arming."""
    conn = _conn(tmp_path)
    a = _seed_open(conn, "OSTOP-A")
    sent = []
    _wire_broker(monkeypatch,
                 positions={"P1": _pos(0.1)},          # sweep start: backed
                 stop_status={"OSTOP-A": "canceled"},  # stop externally canceled
                 sent=sent)
    snaps = [{"P1": _pos(0.1)}, {}]                    # 1st call: backed · fresh: gone
    monkeypatch.setattr(ex_mod.broker, "open_positions",
                        lambda: snaps.pop(0) if snaps else {})
    _exec(conn, mode="live").verify_open_stops()
    assert not any(kind == "private" for kind, _ in sent)   # NO orphan stop placed
    row = conn.execute("SELECT status, stop_txid FROM orders WHERE id=?", (a,)).fetchone()
    assert row[0] == "open" and row[1] is None              # handoff to reprotect
    conn.close()


def test_verify_fresh_backing_counts_higher_id_siblings(tmp_path, monkeypatch):
    """The fresh-backing budget must be ORDER-INDEPENDENT. Two backed rows on one
    pair: the LOW-id row's stop is gone, the HIGH-id sibling's stop rests. Half the
    volume closes mid-sweep. Budgeting incrementally (siblings-seen-so-far) would
    let the low-id row re-place against volume the sibling's live stop already
    claims -> resting-sell volume ABOVE open volume, the naked short the whole
    reconcile exists to prevent. The row must defer to reprotect instead."""
    conn = _conn(tmp_path)
    a = _seed_open(conn, "OSTOP-A")            # low id, stop GONE
    b = _seed_open(conn, "OSTOP-B")            # high id, stop RESTING
    sent = []
    _wire_broker(monkeypatch,
                 positions={"P1": _pos(0.1), "P2": _pos(0.1)},   # sweep start: 0.2 backs both
                 stop_status={"OSTOP-A": "canceled", "OSTOP-B": "open"},
                 sent=sent)
    snaps = [{"P1": _pos(0.1), "P2": _pos(0.1)}, {"P1": _pos(0.1)}]   # fresh: only 0.1 left
    monkeypatch.setattr(ex_mod.broker, "open_positions",
                        lambda: snaps.pop(0) if snaps else {"P1": _pos(0.1)})
    _exec(conn, mode="live").verify_open_stops()
    assert not any(kind == "private" for kind, _ in sent), \
        "re-placed against volume the sibling's resting stop already claims"
    assert conn.execute("SELECT stop_txid FROM orders WHERE id=?", (a,)).fetchone()[0] is None
    assert conn.execute("SELECT stop_txid FROM orders WHERE id=?", (b,)).fetchone()[0] == "OSTOP-B"
    conn.close()


def test_verify_replace_defers_when_fresh_read_unavailable(tmp_path, monkeypatch):
    """Fresh OpenPositions None at the re-place moment = cannot verify backing —
    defer (stop_txid NULL, reprotect owns it), never place on the stale snapshot."""
    conn = _conn(tmp_path)
    a = _seed_open(conn, "OSTOP-A")
    sent = []
    _wire_broker(monkeypatch,
                 positions={"P1": _pos(0.1)},
                 stop_status={"OSTOP-A": "canceled"},
                 sent=sent)
    snaps = [{"P1": _pos(0.1)}, None]                  # fresh read fails
    monkeypatch.setattr(ex_mod.broker, "open_positions",
                        lambda: snaps.pop(0) if snaps else None)
    _exec(conn, mode="live").verify_open_stops()
    assert not any(kind == "private" for kind, _ in sent)
    row = conn.execute("SELECT status, stop_txid FROM orders WHERE id=?", (a,)).fetchone()
    assert row[0] == "open" and row[1] is None
    conn.close()


def test_verify_replace_proceeds_on_confirmed_fresh_backing(tmp_path, monkeypatch):
    """The healthy case is unchanged: fresh backing confirms the position and the
    missing stop is re-placed exactly once, immediately."""
    conn = _conn(tmp_path)
    a = _seed_open(conn, "OSTOP-A")
    sent = []
    _wire_broker(monkeypatch,
                 positions={"P1": _pos(0.1)},          # constant: both reads backed
                 stop_status={"OSTOP-A": "canceled"},
                 sent=sent)
    _exec(conn, mode="live").verify_open_stops()
    placed = [p for kind, p in sent if kind == "private"]
    assert len(placed) == 1 and placed[0]["ordertype"] == "stop-loss"
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


# ── 2026-07-10 bug-hunt: reconcile hardening (ranks 2/3, pilot, gaps A/B) ─────

def test_verify_executed_stop_closes_row_and_preserves_sibling_stop(tmp_path, monkeypatch):
    """rank 2: a pair holds two stacked longs with DIFFERENT stops; the OLDER one's stop
    executed, the newer survives. The executed row must close (P&L path) WITHOUT eating
    the surviving sibling's backing — so the sibling's live stop is NOT canceled and no
    stale-priced stop is re-placed against it. (Old oldest-first budgeting did both.)"""
    conn = _conn(tmp_path)
    older = _seed_open(conn, "OSTOP-OLD", vol=10, stop=95.0)   # its stop executed
    newer = _seed_open(conn, "OSTOP-NEW", vol=10, stop=80.0)   # survives
    sent = []
    _wire_broker(monkeypatch,
                 positions={"P": _pos(10)},                    # only 10 open = the survivor
                 stop_status={"OSTOP-OLD": "closed", "OSTOP-NEW": "open"},
                 sent=sent)
    _exec(conn, mode="live").verify_open_stops()
    st = dict(conn.execute("SELECT id,status FROM orders").fetchall())
    assert st[older] == "closed"                               # executed row retired
    assert st[newer] == "open"                                 # survivor kept
    assert ("cancel", "OSTOP-NEW") not in sent                 # survivor's live stop NOT canceled
    assert not any(kind == "private" for kind, _ in sent)      # no stale-priced re-place
    conn.close()


def test_verify_unbacked_cancel_failure_leaves_row_open(tmp_path, monkeypatch):
    """pilot: an unbacked row whose orphan-stop CANCEL fails must stay 'open' and retry
    next restart — closing it would strand a live stop that later opens a naked short."""
    conn = _conn(tmp_path)
    a = _seed_open(conn, "OSTOP-A", vol=0.1)
    sent = []
    _wire_broker(monkeypatch, positions={}, stop_status={"OSTOP-A": "open"},
                 sent=sent, cancel_ok=False)                   # CancelOrder returns None
    _exec(conn, mode="live").verify_open_stops()
    assert ("cancel", "OSTOP-A") in sent                       # cancel WAS attempted
    assert conn.execute("SELECT status FROM orders WHERE id=?", (a,)).fetchone()[0] == "open"
    conn.close()


def test_verify_unbacked_unknown_stop_leaves_row_open(tmp_path, monkeypatch):
    """pilot: an unbacked row whose stop status is UNKNOWN (query failed/rate-limited)
    must stay 'open' — we cannot prove the stop is gone, so closing it could strand it."""
    conn = _conn(tmp_path)
    a = _seed_open(conn, "OSTOP-A", vol=0.1)
    sent = []
    _wire_broker(monkeypatch, positions={},
                 stop_status={"OSTOP-A": None},                # None -> absent from map -> UNKNOWN
                 sent=sent)
    _exec(conn, mode="live").verify_open_stops()
    assert conn.execute("SELECT status FROM orders WHERE id=?", (a,)).fetchone()[0] == "open"
    assert not any(kind == "cancel" for kind, _ in sent)       # never cancel on UNKNOWN
    assert not any(kind == "private" for kind, _ in sent)
    conn.close()


def test_verify_adopts_resting_orphan_stop_no_duplicate(tmp_path, monkeypatch):
    """rank 3 / gap A: a backed row whose ledger stop is gone but a stop IS resting on
    Kraken (persist-race orphan) must ADOPT that stop, not place a duplicate — a doubled
    stop-sell opens a naked short when triggered."""
    conn = _conn(tmp_path)
    a = _seed_open(conn, "OSTOP-A", vol=0.1)
    sent = []
    orphan = {"ORPHAN-1": {"descr": {"type": "sell", "ordertype": "stop-loss",
                                     "pair": "XXBTZUSD"}, "vol": "0.1"}}
    _wire_broker(monkeypatch, positions={"P": _pos(0.1)},      # backed
                 stop_status={"OSTOP-A": "canceled"},          # ledger stop gone
                 sent=sent, open_orders=orphan)
    _exec(conn, mode="live").verify_open_stops()
    assert conn.execute("SELECT stop_txid FROM orders WHERE id=?", (a,)).fetchone()[0] == "ORPHAN-1"
    assert not any(kind == "private" for kind, _ in sent)      # NO duplicate stop placed
    conn.close()


def test_reprotect_naked_open_rests_missing_stop(tmp_path, monkeypatch):
    """gap B: a live 'open' position whose stop-rest failed (stop_txid NULL) is
    re-protected at runtime by poll_fills, not left naked until a manual restart."""
    conn = _conn(tmp_path)
    a = _seed_open(conn, None, vol=0.1)                        # open row, NO stop
    sent = []
    _wire_broker(monkeypatch, positions={"P": _pos(0.1)}, stop_status={}, sent=sent)
    _exec(conn, mode="live").poll_fills()
    stop_txid = conn.execute("SELECT stop_txid FROM orders WHERE id=?", (a,)).fetchone()[0]
    assert stop_txid == "ONEWSTOP"                             # a stop was rested via AddOrder
    assert any(kind == "private" for kind, _ in sent)
    conn.close()


# ── 2026-07-13 operator stack: SIZE_MULT · seeded chains · equity take-profit ─

def test_size_mult_scales_min_and_conviction(tmp_path, monkeypatch):
    """SIZE_MULT multiplies the min fill; conviction stacks ON TOP of it, and the
    two multipliers are reported separately (the notional ceiling scales only by
    conviction, so SIZE_MULT never widens the corrupt-row guard)."""
    monkeypatch.setattr(config, "SIZE_MULT", 3.0)
    conn = _conn(tmp_path, ordermin=0.1, costmin=0.5, lot_dec=8)
    e = _exec(conn)
    flat = e.size(SYM, entry=100.0, stop=90.0, leverage=10, equity=1000.0)
    assert flat["volume"] == pytest.approx(0.3)                # 3x the 0.1 min
    assert flat["size_mult"] == 3.0 and flat["conviction_mult"] == 1.0
    strong = Card(); strong.score, strong.required = 7, 5      # 3x conviction
    s = e.size(SYM, entry=100.0, stop=90.0, leverage=10, equity=1000.0, card=strong)
    assert s["volume"] == pytest.approx(0.9)                   # 3x conviction x 3x size
    assert s["conviction_mult"] == 3.0
    conn.close()


def _wire_seed(monkeypatch, e, sent, live=100.0):
    monkeypatch.setattr(e, "_live_last", lambda sym: live)
    monkeypatch.setattr(ex_mod.broker, "trade_balance", lambda: 1000.0)
    monkeypatch.setattr(ex_mod.broker, "private",
                        lambda ep, p=None, **kw: (sent.append(p) or {"txid": ["OSEED-1"]}))


def test_seed_chains_starts_missing_chain_once(tmp_path, monkeypatch):
    """A SEED_PAIRS symbol with no open rows and no resting bid gets ONE post-only
    starter bid just below live; while that bid rests, no second seed fires."""
    monkeypatch.setattr(config, "SEED_PAIRS", (SYM,))
    ex_mod._seed_next.clear()
    conn = _conn(tmp_path)
    e = _exec(conn, mode="live")
    sent = []
    _wire_seed(monkeypatch, e, sent)
    e._seed_chains()
    rows = conn.execute("SELECT status, side FROM orders WHERE symbol=?", (SYM,)).fetchall()
    assert rows == [("pending", "buy")]
    assert len(sent) == 1 and sent[0]["type"] == "buy" and sent[0]["oflags"] == "post"
    ex_mod._seed_next.clear()          # defeat the backoff; the bid must gate it alone
    e._seed_chains()
    assert len(sent) == 1
    conn.close()


def test_seed_chains_skips_working_chain(tmp_path, monkeypatch):
    """A pair with an open position already has a chain (reladder keeps it bidding)
    — the seeder must not stack a second chain on it."""
    monkeypatch.setattr(config, "SEED_PAIRS", (SYM,))
    ex_mod._seed_next.clear()
    conn = _conn(tmp_path)
    _seed_open(conn, "OSTOP-A", vol=0.1)
    e = _exec(conn, mode="live")
    sent = []
    _wire_seed(monkeypatch, e, sent)
    e._seed_chains()
    assert sent == []
    conn.close()


def _wire_tp(monkeypatch, *, equity, positions, open_orders, terminal, sent,
             addorder_ok=True, cancel_ok=True, bid=64990.0, ask=65000.0):
    """Endpoint-routed broker mock for the T/P flatten path (limit closes need a
    Ticker quote — served for every requested pair)."""
    monkeypatch.setattr(config, "TP_ENABLED", True)
    monkeypatch.setattr(ex_mod.broker, "trade_balance", lambda: equity)
    monkeypatch.setattr(ex_mod.broker, "open_positions", lambda: positions)
    monkeypatch.setattr(ex_mod.broker, "open_orders", lambda: open_orders)
    monkeypatch.setattr(ex_mod.broker, "query_orders",
                        lambda txids: {t: terminal[t] for t in txids if t in terminal})
    monkeypatch.setattr(ex_mod.broker, "cancel_order",
                        lambda t: (sent.append(("cancel", t)) or ({} if cancel_ok else None)))
    monkeypatch.setattr(ex_mod.rest_client, "fetch_ticker",
                        lambda pairs: {p: {"b": [str(bid), "1", "1"],
                                           "a": [str(ask), "1", "1"]} for p in pairs})

    def fp(ep, p=None, **kw):
        sent.append((ep, p))
        if ep.endswith("CancelAll"):
            return {"count": 1}
        return {"txid": ["OCLOSE-1"]} if addorder_ok else None
    monkeypatch.setattr(ex_mod.broker, "private", fp)


def test_tp_arms_baseline_on_first_sight(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    sent = []
    _wire_tp(monkeypatch, equity=100.0, positions={}, open_orders={}, terminal={}, sent=sent)
    e = _exec(conn, mode="live")
    assert e._check_take_profit() is False
    assert float(store.meta_get(conn, "tp_baseline", 0)) == 100.0
    assert not any(ep.endswith("CancelAll") for ep, _ in sent if isinstance(ep, str))
    conn.close()


def test_tp_below_target_does_nothing(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    store.meta_set(conn, "tp_baseline", 100.0)
    sent = []
    _wire_tp(monkeypatch, equity=119.99, positions={}, open_orders={}, terminal={}, sent=sent)
    e = _exec(conn, mode="live")
    assert e._check_take_profit() is False
    assert sent == []
    assert float(store.meta_get(conn, "tp_baseline", 0)) == 100.0
    conn.close()


def test_tp_trigger_rests_limit_close_then_completes(tmp_path, monkeypatch):
    """+20% hit, limit-flatten contract (operator 2026-07-21). PASS 1: bids
    canceled, stops cleared, ONE post-only LIMIT sell pegged a tick over best bid,
    sized to Kraken's live volume (never the ledger's) — rows stay open carrying
    close_txid, baseline kept, tp_flatten_active armed. PASS 2 (close filled on
    the exchange): rows retired, baseline re-armed at the new equity, cycle row
    written to the tp_cycles ledger with true trading profit."""
    conn = _conn(tmp_path)
    store.meta_set(conn, "tp_baseline", 100.0)
    a = _seed_open(conn, "OSTOP-A", vol=0.4)                   # ledger says 0.4
    b = _seed_pending(conn, "OENTRY-9")
    sent = []
    _wire_tp(monkeypatch, equity=120.0,
             positions={"P": _pos(0.5)},                       # exchange says 0.5 — truth
             open_orders={},                                   # batch sweep took the rest
             terminal={"OENTRY-9": {"status": "canceled", "vol_exec": "0"}},
             sent=sent, bid=64990.0, ask=65000.0)
    e = _exec(conn, mode="live")
    assert e._check_take_profit() is True
    assert conn.execute("SELECT status FROM orders WHERE id=?", (b,)).fetchone()[0] == "canceled"
    # pass 1: row OPEN, owned by the resting close; stop cleared
    assert conn.execute("SELECT status, stop_txid, close_txid FROM orders WHERE id=?",
                        (a,)).fetchone() == ("open", None, "OCLOSE-1")
    closes = [p for ep, p in sent if isinstance(ep, str) and ep.endswith("AddOrder")]
    assert len(closes) == 1
    assert closes[0]["type"] == "sell" and closes[0]["ordertype"] == "limit"
    assert closes[0]["oflags"] == "post"
    assert closes[0]["price"] == "64990.1"                     # bid + one tick (BTC dec=1)
    assert closes[0]["volume"] == "0.50000000"                 # LIVE volume, lot-gridded
    assert closes[0]["pair"] == "XBTUSD:BTNL" and closes[0]["leverage"] == "10"
    assert float(store.meta_get(conn, "tp_baseline", 0)) == 100.0   # NOT reset yet
    assert store.meta_get(conn, "tp_flatten_active") == "1"
    # PASS 2 — the close filled: exchange flat, order terminal
    _wire_tp(monkeypatch, equity=119.5, positions={},
             open_orders={}, terminal={"OCLOSE-1": {"status": "closed"}}, sent=sent)
    assert e._check_take_profit() is True
    assert conn.execute("SELECT status, close_txid FROM orders WHERE id=?", (a,)).fetchone() \
        == ("closed", None)
    assert float(store.meta_get(conn, "tp_baseline", 0)) == 119.5
    assert store.meta_get(conn, "tp_flatten_active") == "0"
    ts, base, settled, flows, profit, note = store.tp_cycles_list(conn, 1)[0]
    assert (base, settled, flows, note) == (100.0, 119.5, 0.0, "limit flatten")
    assert abs(profit - 19.5) < 1e-9
    conn.close()


def test_tp_partial_filled_bid_joins_the_close(tmp_path, monkeypatch):
    """A bid that PARTIALLY filled before the sweep is a real long: promoted, then
    covered by its pair's limit close — never stranded as a canceled row with live
    volume."""
    conn = _conn(tmp_path)
    store.meta_set(conn, "tp_baseline", 100.0)
    b = _seed_pending(conn, "OENTRY-9")
    sent = []
    _wire_tp(monkeypatch, equity=125.0,
             positions={"P": _pos(0.2)},
             open_orders={},
             terminal={"OENTRY-9": {"status": "canceled", "vol_exec": "0.2"}},
             sent=sent)
    e = _exec(conn, mode="live")
    assert e._check_take_profit() is True
    assert conn.execute("SELECT status, volume, close_txid FROM orders WHERE id=?",
                        (b,)).fetchone() == ("open", 0.2, "OCLOSE-1")
    closes = [p for ep, p in sent if isinstance(ep, str) and ep.endswith("AddOrder")]
    assert len(closes) == 1 and closes[0]["volume"] == "0.20000000"
    assert closes[0]["ordertype"] == "limit" and closes[0]["oflags"] == "post"
    conn.close()


def test_tp_chase_repegs_when_market_falls_away(tmp_path, monkeypatch):
    """A resting close the market fell away from (our price above the ask) is
    canceled so the next pass re-pegs at the new touch; one at/near the touch is
    left to work untouched."""
    conn = _conn(tmp_path)
    store.meta_set(conn, "tp_baseline", 100.0)
    store.meta_set(conn, "tp_flatten_active", "1")
    a = _seed_open(conn, "OSTOP-A", vol=0.4)
    conn.execute("UPDATE orders SET stop_txid=NULL, close_txid='OCLOSE-9' WHERE id=?", (a,))
    conn.commit()
    sent = []
    # our sell rests at 66000 but the book is now 64990/65000 — price fell away
    _wire_tp(monkeypatch, equity=110.0, positions={"P": _pos(0.4)}, open_orders={},
             terminal={"OCLOSE-9": {"status": "open",
                                    "descr": {"price": "66000.0"}}},
             sent=sent, bid=64990.0, ask=65000.0)
    e = _exec(conn, mode="live")
    assert e._check_take_profit() is True                      # flag drives the pass
    assert ("cancel", "OCLOSE-9") in sent
    assert conn.execute("SELECT close_txid FROM orders WHERE id=?", (a,)).fetchone()[0] is None
    # well-pegged close: left alone (no cancel, no new AddOrder)
    conn.execute("UPDATE orders SET close_txid='OCLOSE-9' WHERE id=?", (a,))
    conn.commit()
    sent2 = []
    _wire_tp(monkeypatch, equity=110.0, positions={"P": _pos(0.4)}, open_orders={},
             terminal={"OCLOSE-9": {"status": "open",
                                    "descr": {"price": "65000.0"}}},
             sent=sent2, bid=64990.0, ask=65000.0)
    assert e._check_take_profit() is True
    assert ("cancel", "OCLOSE-9") not in sent2
    assert not any(isinstance(ep, str) and ep.endswith("AddOrder") for ep, _ in sent2)
    conn.close()


def test_tp_close_unknown_status_blocks_pair(tmp_path, monkeypatch):
    """Close order status UNKNOWN (query dark): never place a second sell beside a
    possibly-live one — the pair is skipped this pass and the flatten stays
    incomplete."""
    conn = _conn(tmp_path)
    store.meta_set(conn, "tp_baseline", 100.0)
    store.meta_set(conn, "tp_flatten_active", "1")
    a = _seed_open(conn, "OSTOP-A", vol=0.4)
    conn.execute("UPDATE orders SET stop_txid=NULL, close_txid='OCLOSE-9' WHERE id=?", (a,))
    conn.commit()
    sent = []
    _wire_tp(monkeypatch, equity=110.0, positions={"P": _pos(0.4)}, open_orders={},
             terminal={}, sent=sent)                           # OCLOSE-9 absent -> unknown
    e = _exec(conn, mode="live")
    assert e._check_take_profit() is True
    assert not any(isinstance(ep, str) and ep.endswith("AddOrder") for ep, _ in sent)
    assert conn.execute("SELECT status, close_txid FROM orders WHERE id=?", (a,)).fetchone() \
        == ("open", "OCLOSE-9")
    assert store.meta_get(conn, "tp_flatten_active") == "1"
    conn.close()


def test_reprotect_skips_rows_owned_by_a_resting_close(tmp_path, monkeypatch):
    """A row carrying close_txid is the flatten's — reprotect must NOT re-arm a
    stop beside the resting limit sell (stop fires + close fills -> short)."""
    conn = _conn(tmp_path)
    a = _seed_open(conn, "OSTOP-A", vol=0.4)
    conn.execute("UPDATE orders SET stop_txid=NULL, close_txid='OCLOSE-9' WHERE id=?", (a,))
    conn.commit()
    sent = []
    monkeypatch.setattr(ex_mod.broker, "open_orders", lambda: {})
    monkeypatch.setattr(ex_mod.broker, "open_positions", lambda: {"P": _pos(0.4)})
    monkeypatch.setattr(ex_mod.broker, "private",
                        lambda ep, p=None, **kw: sent.append((ep, p)) or {"txid": ["OSTOP-NEW"]})
    e = _exec(conn, mode="live")
    e._reprotect_naked_open()
    assert sent == []                                          # early-return: nothing naked
    assert conn.execute("SELECT stop_txid FROM orders WHERE id=?", (a,)).fetchone()[0] is None
    conn.close()


def test_tp_exchange_dark_aborts_untouched(tmp_path, monkeypatch):
    """OpenOrders unavailable after the sweep: record NOTHING, keep the baseline,
    re-trigger next poll — never reconcile blind."""
    conn = _conn(tmp_path)
    store.meta_set(conn, "tp_baseline", 100.0)
    a = _seed_open(conn, "OSTOP-A", vol=0.4)
    sent = []
    _wire_tp(monkeypatch, equity=120.0, positions={"P": _pos(0.4)},
             open_orders=None, terminal={}, sent=sent)
    e = _exec(conn, mode="live")
    assert e._check_take_profit() is False
    assert conn.execute("SELECT status, stop_txid FROM orders WHERE id=?", (a,)).fetchone() \
        == ("open", "OSTOP-A")
    assert float(store.meta_get(conn, "tp_baseline", 0)) == 100.0
    conn.close()


def test_tp_failed_close_keeps_baseline_and_reprotectable_rows(tmp_path, monkeypatch):
    """A failed market close leaves its rows OPEN with stop_txid NULL (reprotect
    re-arms them) and the baseline UNRESET — the trigger re-fires and retries;
    resetting it would strand the pair until +20% over the NEW base."""
    conn = _conn(tmp_path)
    store.meta_set(conn, "tp_baseline", 100.0)
    a = _seed_open(conn, "OSTOP-A", vol=0.4)
    sent = []
    _wire_tp(monkeypatch, equity=120.0, positions={"P": _pos(0.4)},
             open_orders={}, terminal={}, sent=sent, addorder_ok=False)
    e = _exec(conn, mode="live")
    assert e._check_take_profit() is True                      # a pass ran (skip cycle)
    assert conn.execute("SELECT status, stop_txid FROM orders WHERE id=?", (a,)).fetchone() \
        == ("open", None)
    assert float(store.meta_get(conn, "tp_baseline", 0)) == 100.0
    conn.close()


# ── T/P trough ratchet (operator 2026-07-24) ─────────────────────────────────

def test_tp_trough_ratchets_down_never_up(tmp_path, monkeypatch):
    """A drawdown pulls tp_trough to the equity low; a recovery leaves it there.
    The baseline is never touched — it stays the ledger's profit yardstick."""
    conn = _conn(tmp_path)
    store.meta_set(conn, "tp_baseline", 100.0)
    sent = []
    _wire_tp(monkeypatch, equity=80.0, positions={}, open_orders={}, terminal={}, sent=sent)
    e = _exec(conn, mode="live")
    assert e._check_take_profit() is False
    assert float(store.meta_get(conn, "tp_trough")) == 80.0
    _wire_tp(monkeypatch, equity=90.0, positions={}, open_orders={}, terminal={}, sent=sent)
    assert e._check_take_profit() is False
    assert float(store.meta_get(conn, "tp_trough")) == 80.0        # sticks at the low
    assert float(store.meta_get(conn, "tp_baseline")) == 100.0     # untouched
    conn.close()


def test_tp_arm_seeds_trough_at_baseline(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    sent = []
    _wire_tp(monkeypatch, equity=100.0, positions={}, open_orders={}, terminal={}, sent=sent)
    e = _exec(conn, mode="live")
    assert e._check_take_profit() is False
    assert float(store.meta_get(conn, "tp_trough")) == 100.0
    conn.close()


def test_tp_fires_off_trough_and_books_red_cycle(tmp_path, monkeypatch):
    """UN-FLOORED branch (TP_TARGET_FLOOR_BASELINE=False): the target is
    min(baseline, trough)*(1+TP_PCT): equity 80 ratchets the trough, a bounce to
    96.5 (>= 80*1.2) fires the flatten even though it is below the 100 baseline —
    and the cycle row books the honest LOSS against the baseline, noting the
    trough it fired from. The default-on floor is exercised by
    test_tp_floor_at_baseline_never_banks_loss."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(config, "TP_TARGET_FLOOR_BASELINE", False)
    store.meta_set(conn, "tp_baseline", 100.0)
    sent = []
    _wire_tp(monkeypatch, equity=80.0, positions={}, open_orders={}, terminal={}, sent=sent)
    e = _exec(conn, mode="live")
    assert e._check_take_profit() is False                         # ratchet only
    _seed_open(conn, "OSTOP-A", vol=0.5)
    _wire_tp(monkeypatch, equity=96.5, positions={"P": _pos(0.5)}, open_orders={},
             terminal={}, sent=sent, bid=64990.0, ask=65000.0)
    assert e._check_take_profit() is True                          # fired off the trough
    assert store.meta_get(conn, "tp_flatten_active") == "1"
    # close filled → settle
    _wire_tp(monkeypatch, equity=96.0, positions={}, open_orders={},
             terminal={"OCLOSE-1": {"status": "closed"}}, sent=sent)
    assert e._check_take_profit() is True
    ts, base, settled, flows, profit, note = store.tp_cycles_list(conn, 1)[0]
    assert (base, settled) == (100.0, 96.0)
    assert abs(profit - (-4.0)) < 1e-9                             # honest red
    assert "trough $80.00" in note
    assert float(store.meta_get(conn, "tp_baseline")) == 96.0
    assert float(store.meta_get(conn, "tp_trough")) == 96.0        # re-seeded
    conn.close()


def test_tp_floor_at_baseline_never_banks_loss(tmp_path, monkeypatch):
    """FLOORED branch (TP_TARGET_FLOOR_BASELINE=True, the default, operator
    2026-07-27): the ratchet may lower the target toward the bounce but NEVER
    below baseline. Same drawdown as the red-cycle test — baseline 100, trough
    80 — but the bounce to 96.5 that used to fire a -$4 cycle now does NOT fire
    (target floored to 100). Only a recovery to baseline fires, booking a
    breakeven cycle, never a loss."""
    conn = _conn(tmp_path)
    assert getattr(config, "TP_TARGET_FLOOR_BASELINE", False) is True   # default on
    store.meta_set(conn, "tp_baseline", 100.0)
    sent = []
    _wire_tp(monkeypatch, equity=80.0, positions={}, open_orders={}, terminal={}, sent=sent)
    e = _exec(conn, mode="live")
    assert e._check_take_profit() is False                          # ratchet only
    assert float(store.meta_get(conn, "tp_trough")) == 80.0
    # the bounce that fired a red cycle un-floored — held here (96.5 < 100 floor)
    _seed_open(conn, "OSTOP-A", vol=0.5)
    _wire_tp(monkeypatch, equity=96.5, positions={"P": _pos(0.5)}, open_orders={},
             terminal={}, sent=sent, bid=64990.0, ask=65000.0)
    assert e._check_take_profit() is False                          # FLOORED — no loss-fire
    assert store.meta_get(conn, "tp_flatten_active") in (None, "0", "")
    assert float(store.meta_get(conn, "tp_baseline")) == 100.0      # cycle still open
    # recovery to baseline fires at breakeven
    _wire_tp(monkeypatch, equity=100.0, positions={"P": _pos(0.5)}, open_orders={},
             terminal={}, sent=sent, bid=64990.0, ask=65000.0)
    assert e._check_take_profit() is True                          # fires at the floor
    assert store.meta_get(conn, "tp_flatten_active") == "1"
    _wire_tp(monkeypatch, equity=100.0, positions={}, open_orders={},
             terminal={"OCLOSE-1": {"status": "closed"}}, sent=sent)
    assert e._check_take_profit() is True
    ts, base, settled, flows, profit, note = store.tp_cycles_list(conn, 1)[0]
    assert (base, settled) == (100.0, 100.0)
    assert profit >= 0.0                                           # never a loss
    conn.close()


def test_tp_no_ratchet_while_flatten_active(tmp_path, monkeypatch):
    """Mid-chase equity is half-settled noise — the trough must not ratchet while
    the flatten owns the book."""
    conn = _conn(tmp_path)
    store.meta_set(conn, "tp_baseline", 100.0)
    store.meta_set(conn, "tp_trough", 90.0)
    store.meta_set(conn, "tp_flatten_active", "1")
    _seed_open(conn, "OSTOP-A", vol=0.5)
    sent = []
    _wire_tp(monkeypatch, equity=50.0, positions={"P": _pos(0.5)}, open_orders={},
             terminal={}, sent=sent)
    e = _exec(conn, mode="live")
    assert e._check_take_profit() is True                          # flatten pass ran
    assert float(store.meta_get(conn, "tp_trough")) == 90.0        # untouched
    conn.close()


def test_tp_trough_above_baseline_clamps(tmp_path, monkeypatch):
    """A stale trough above the baseline (e.g. after a withdrawal shifted the
    baseline below it) clamps to the baseline — target never exceeds
    baseline*(1+TP_PCT)."""
    conn = _conn(tmp_path)
    store.meta_set(conn, "tp_baseline", 100.0)
    store.meta_set(conn, "tp_trough", 140.0)
    sent = []
    _wire_tp(monkeypatch, equity=120.0, positions={}, open_orders={}, terminal={}, sent=sent)
    e = _exec(conn, mode="live")
    assert e._check_take_profit() is True                          # 120 >= 100*1.2
    conn.close()


# ── deposit-aware T/P baseline (app._poll_external_flows, 2026-07-21) ────────

def _flows_stub(net, count=1, complete=True):
    import types as _t
    return _t.SimpleNamespace(external_flows_since=lambda ts: (net, count, complete))


def test_external_flow_first_poll_anchors_only(tmp_path):
    from deepfield import app as app_mod
    import datetime as _dt
    conn = _conn(tmp_path)
    store.meta_set(conn, "tp_baseline", 100.0)
    now = _dt.datetime.now(_dt.timezone.utc)
    app_mod._poll_external_flows(conn, _flows_stub(999.0), now)
    assert float(store.meta_get(conn, "tp_baseline")) == 100.0     # untouched
    assert abs(float(store.meta_get(conn, "flows_cursor")) - now.timestamp()) < 1
    conn.close()


def test_external_deposit_shifts_baseline_and_accumulates(tmp_path):
    """A $20 deposit moves the baseline $20 so the +20% target measures TRADING
    profit only (the 2026-07-19 deposit fired the flatten at ~+9% real gain)."""
    from deepfield import app as app_mod
    import datetime as _dt
    conn = _conn(tmp_path)
    store.meta_set(conn, "tp_baseline", 100.0)
    store.meta_set(conn, "flows_cursor", 123.0)
    now = _dt.datetime.now(_dt.timezone.utc)
    app_mod._poll_external_flows(conn, _flows_stub(20.0), now)
    assert float(store.meta_get(conn, "tp_baseline")) == 120.0
    assert float(store.meta_get(conn, "tp_cycle_flows")) == 20.0
    app_mod._poll_external_flows(conn, _flows_stub(-5.0), now)     # partial withdrawal
    assert float(store.meta_get(conn, "tp_baseline")) == 115.0
    assert float(store.meta_get(conn, "tp_cycle_flows")) == 15.0
    conn.close()


def test_external_withdrawal_wiping_baseline_clears_it(tmp_path):
    """A withdrawal >= baseline clears it to 0 — the executor re-arms at the next
    live equity read instead of chasing a negative target."""
    from deepfield import app as app_mod
    import datetime as _dt
    conn = _conn(tmp_path)
    store.meta_set(conn, "tp_baseline", 100.0)
    store.meta_set(conn, "flows_cursor", 123.0)
    app_mod._poll_external_flows(conn, _flows_stub(-150.0),
                                 _dt.datetime.now(_dt.timezone.utc))
    assert float(store.meta_get(conn, "tp_baseline")) == 0.0
    conn.close()


def test_external_flow_shifts_trough_with_baseline(tmp_path):
    """The trough measures the same trading-equity dollars as the baseline: a
    deposit/withdrawal shifts both, else the flow would double-count against the
    ratchet (withdrawal dips equity → ratchet takes it → baseline shift takes it
    again)."""
    from deepfield import app as app_mod
    import datetime as _dt
    conn = _conn(tmp_path)
    store.meta_set(conn, "tp_baseline", 100.0)
    store.meta_set(conn, "tp_trough", 70.0)
    store.meta_set(conn, "flows_cursor", 123.0)
    now = _dt.datetime.now(_dt.timezone.utc)
    app_mod._poll_external_flows(conn, _flows_stub(20.0), now)
    assert float(store.meta_get(conn, "tp_baseline")) == 120.0
    assert float(store.meta_get(conn, "tp_trough")) == 90.0
    app_mod._poll_external_flows(conn, _flows_stub(-5.0), now)
    assert float(store.meta_get(conn, "tp_baseline")) == 115.0
    assert float(store.meta_get(conn, "tp_trough")) == 85.0
    conn.close()


def test_external_withdrawal_clearing_baseline_clears_trough(tmp_path):
    from deepfield import app as app_mod
    import datetime as _dt
    conn = _conn(tmp_path)
    store.meta_set(conn, "tp_baseline", 100.0)
    store.meta_set(conn, "tp_trough", 70.0)
    store.meta_set(conn, "flows_cursor", 123.0)
    app_mod._poll_external_flows(conn, _flows_stub(-150.0),
                                 _dt.datetime.now(_dt.timezone.utc))
    assert float(store.meta_get(conn, "tp_baseline")) == 0.0
    assert float(store.meta_get(conn, "tp_trough")) == 0.0
    conn.close()


def test_external_flow_api_failure_keeps_cursor(tmp_path):
    from deepfield import app as app_mod
    import types as _t, datetime as _dt
    conn = _conn(tmp_path)
    store.meta_set(conn, "tp_baseline", 100.0)
    store.meta_set(conn, "flows_cursor", 123.0)
    stub = _t.SimpleNamespace(external_flows_since=lambda ts: None)
    app_mod._poll_external_flows(conn, stub, _dt.datetime.now(_dt.timezone.utc))
    assert float(store.meta_get(conn, "flows_cursor")) == 123.0    # retry same window
    assert float(store.meta_get(conn, "tp_baseline")) == 100.0
    conn.close()


# ── respend governor (leaky-bucket respend-RATE throttle) ────────────────────

def _bucket(conn, tokens, age_secs):
    """Seed the meta bucket as if `tokens` were stored `age_secs` ago."""
    store.meta_set(conn, "respend_bucket",
                   json.dumps({"tokens": tokens, "updated": time.time() - age_secs}))


def test_respend_debit_keeps_the_accrual_it_earned(tmp_path, monkeypatch):
    """The debit path must re-apply the same accrual the check did. Reading the
    STORED tokens and stamping updated=now discarded everything earned since the
    last write AND restarted the clock, so the bucket refilled strictly slower
    than the configured rate (07-27 audit: 966 blocks vs 26 rungs in 13h)."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(config, "RESPEND_BUDGET_USD_PER_HR", 5.0)
    monkeypatch.setattr(config, "RESPEND_BURST_USD", 40.0)
    e = _exec(conn, mode="live")
    _bucket(conn, tokens=0.0, age_secs=3600)          # empty an hour ago -> $5 accrued
    ok, _why, debit = e._respend_budget_ok(3.0)
    assert ok                                          # $5 accrued covers a $3 rung
    debit()
    left = json.loads(store.meta_get(conn, "respend_bucket"))["tokens"]
    assert abs(left - 2.0) < 0.01                      # $5 - $3, NOT $0
    conn.close()


def test_respend_debit_never_exceeds_burst(tmp_path, monkeypatch):
    """Accrual on the debit path is still capped at the burst ceiling — a long
    quiet spell can't mint more than RESPEND_BURST_USD of budget."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(config, "RESPEND_BUDGET_USD_PER_HR", 5.0)
    monkeypatch.setattr(config, "RESPEND_BURST_USD", 40.0)
    e = _exec(conn, mode="live")
    _bucket(conn, tokens=0.0, age_secs=3600 * 100)     # 100h idle = $500 uncapped
    ok, _why, debit = e._respend_budget_ok(10.0)
    assert ok
    debit()
    left = json.loads(store.meta_get(conn, "respend_bucket"))["tokens"]
    assert abs(left - 30.0) < 0.01                     # capped 40, minus 10
    conn.close()


def test_respend_credit_refunds_a_canceled_bid(tmp_path, monkeypatch):
    """A canceled-unfilled bid hands its notional back (2026-07-30: TTL/post-only
    re-places are not new growth — the churn was starving ZEC/USDC of re-seeds).
    The credit must ALSO apply accrual first, same as debit — and never mint past
    the burst ceiling."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(config, "RESPEND_BUDGET_USD_PER_HR", 5.0)
    monkeypatch.setattr(config, "RESPEND_BURST_USD", 40.0)
    e = _exec(conn, mode="live")
    _bucket(conn, tokens=1.0, age_secs=3600)           # $1 stored + $5 accrued = $6
    e._respend_credit(4.0, "ZEC/USD")
    left = json.loads(store.meta_get(conn, "respend_bucket"))["tokens"]
    assert abs(left - 10.0) < 0.01                     # $6 + $4 refund, accrual kept
    e._respend_credit(500.0, "ZEC/USD")                # refund can't mint past burst
    left = json.loads(store.meta_get(conn, "respend_bucket"))["tokens"]
    assert abs(left - 40.0) < 0.01
    conn.close()


def test_respend_credit_noop_when_disabled_or_empty(tmp_path, monkeypatch):
    """Governor off, or a row with no notional, must leave the bucket untouched."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(config, "RESPEND_BUDGET_USD_PER_HR", 0.0)   # OFF
    e = _exec(conn, mode="live")
    e._respend_credit(4.0, "ZEC/USD")
    assert store.meta_get(conn, "respend_bucket") is None
    monkeypatch.setattr(config, "RESPEND_BUDGET_USD_PER_HR", 5.0)
    _bucket(conn, tokens=1.0, age_secs=0)
    e._respend_credit(None, "ZEC/USD")                 # NULL notional row — no-op
    assert json.loads(store.meta_get(conn, "respend_bucket"))["tokens"] <= 1.01
    conn.close()


def test_respend_blocks_when_bucket_short(tmp_path, monkeypatch):
    """Under the notional, the governor refuses and the debit is a no-op."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(config, "RESPEND_BUDGET_USD_PER_HR", 5.0)
    monkeypatch.setattr(config, "RESPEND_BURST_USD", 40.0)
    e = _exec(conn, mode="live")
    _bucket(conn, tokens=1.0, age_secs=0)
    ok, why, debit = e._respend_budget_ok(30.0)
    assert not ok and "respend paced" in why
    debit()                                            # refused -> must not spend
    assert json.loads(store.meta_get(conn, "respend_bucket"))["tokens"] <= 1.01
    conn.close()


def test_respend_disabled_fails_open(tmp_path, monkeypatch):
    """Rate 0 = OFF: always allowed, bucket untouched (operator no-blockers)."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(config, "RESPEND_BUDGET_USD_PER_HR", 0.0)
    e = _exec(conn, mode="live")
    ok, _why, debit = e._respend_budget_ok(10_000.0)
    assert ok
    debit()
    assert store.meta_get(conn, "respend_bucket") is None
    conn.close()


def test_respend_pre_gate_skips_only_what_it_would_refuse(tmp_path, monkeypatch):
    """The cheap pre-gate must be conservative: it may skip only when the bucket
    cannot fund even the smallest possible rung, so it can never drop a rung the
    authoritative check would have funded."""
    conn = _conn(tmp_path, ordermin=0.1, costmin=0.5, lot_dec=8)
    monkeypatch.setattr(config, "RESPEND_BUDGET_USD_PER_HR", 5.0)
    monkeypatch.setattr(config, "RESPEND_BURST_USD", 40.0)
    monkeypatch.setattr(config, "LADDER_STEP_PCT", 0.01)
    e = _exec(conn, mode="live")
    # smallest rung at a 1%-lower price = 0.1 x 99.0 = $9.90
    _bucket(conn, tokens=1.0, age_secs=0)
    assert e._respend_would_refuse(SYM, 100.0) is True      # $1 can't fund $9.90
    _bucket(conn, tokens=20.0, age_secs=0)
    assert e._respend_would_refuse(SYM, 100.0) is False     # $20 can — do the work
    conn.close()


def test_respend_pre_gate_scales_with_size_mult(tmp_path, monkeypatch):
    """Every rung/seed sizes at min x SIZE_MULT, so the pre-gate's lower bound
    must carry the multiplier too. At SIZE_MULT=2 a bucket holding between 1x
    and 2x min used to pass the pre-gate — narrating RELADDER at INFO, burning
    the ticker fetch and the 600s backoff — only to be refused at DEBUG by the
    authoritative check (observed live on XLM minutes after the 07-31 1->2
    bump). Verified to FAIL on the unscaled bound."""
    conn = _conn(tmp_path, ordermin=0.1, costmin=0.5, lot_dec=8)
    monkeypatch.setattr(config, "RESPEND_BUDGET_USD_PER_HR", 5.0)
    monkeypatch.setattr(config, "RESPEND_BURST_USD", 40.0)
    monkeypatch.setattr(config, "LADDER_STEP_PCT", 0.01)
    monkeypatch.setattr(config, "SIZE_MULT", 2.0)
    e = _exec(conn, mode="live")
    # smallest REAL rung at 2x = 0.1 x 99.0 x 2 = $19.80
    _bucket(conn, tokens=15.0, age_secs=0)
    assert e._respend_would_refuse(SYM, 100.0) is True      # $15 funds 1x, not 2x
    _bucket(conn, tokens=25.0, age_secs=0)
    assert e._respend_would_refuse(SYM, 100.0) is False     # $25 funds the 2x rung
    conn.close()


def test_respend_pre_gate_fails_open(tmp_path, monkeypatch):
    """Governor off, or no usable reference price, never suppresses a rung."""
    conn = _conn(tmp_path, ordermin=0.1, costmin=0.5, lot_dec=8)
    e = _exec(conn, mode="live")
    monkeypatch.setattr(config, "RESPEND_BUDGET_USD_PER_HR", 0.0)
    _bucket(conn, tokens=0.0, age_secs=0)
    assert e._respend_would_refuse(SYM, 100.0) is False     # OFF
    monkeypatch.setattr(config, "RESPEND_BUDGET_USD_PER_HR", 5.0)
    assert e._respend_would_refuse(SYM, 0.0) is False       # unknown price
    assert e._respend_would_refuse("NOPE/USD", 100.0) is False   # unknown pair
    conn.close()


# ── realized-exit ledger (per-lot track record) ──────────────────────────────

def _seed_open_priced(conn, stop_txid, vol, entry, stop=90.0):
    """An OPEN lot carrying its entry price — the flatten's cost basis."""
    cur = conn.execute(
        "INSERT INTO orders(symbol,margin_pair,volume,leverage,entry,stop,stop_txid,status,mode) "
        "VALUES(?,?,?,?,?,?,?, 'open','live')",
        (SYM, "XBTUSD:BTNL", vol, 10, entry, stop, stop_txid))
    conn.commit()
    return cur.lastrowid


def _seed_closed_exit(conn, pnl, kind, closed_ts):
    """A retired row carrying a priced exit record in the polymorphic error column."""
    conn.execute(
        "INSERT INTO orders(symbol,side,status,mode,error) VALUES(?,'buy','closed','live',?)",
        (SYM, json.dumps({"pnl": pnl, "exit": kind, "closed_ts": closed_ts})))
    conn.commit()


def test_tp_flatten_records_per_lot_realized_pnl(tmp_path, monkeypatch):
    """The flatten retires MOST positions, and until 2026-07-27 it wrote only the plain
    'tp-flatten' marker — no per-lot P&L, so edge was unmeasurable (373 unpriced rows
    against 21 priced stop exits). One close order retires N lots, so proceeds are
    allocated pro-rata by lot volume at the close's real average fill."""
    conn = _conn(tmp_path)
    store.meta_set(conn, "tp_baseline", 100.0)
    a = _seed_open_priced(conn, "OSTOP-A", vol=0.4, entry=60000.0)
    b = _seed_open_priced(conn, "OSTOP-B", vol=0.6, entry=64000.0)
    sent = []
    _wire_tp(monkeypatch, equity=120.0, positions={"P": _pos(1.0)}, open_orders={},
             terminal={}, sent=sent, bid=64990.0, ask=65000.0)
    e = _exec(conn, mode="live")
    assert e._check_take_profit() is True                  # pass 1 rests the close
    # pass 2 — close filled: 1.0 @ 65000 avg, $10 fee
    _wire_tp(monkeypatch, equity=119.5, positions={}, open_orders={},
             terminal={"OCLOSE-1": {"status": "closed", "vol_exec": "1.0",
                                    "cost": "65000.0", "fee": "10.0", "closetm": 1785200000.0}},
             sent=sent)
    assert e._check_take_profit() is True
    recs = {oid: json.loads(err) for oid, err in conn.execute(
        "SELECT id, error FROM orders WHERE id IN (?,?)", (a, b)).fetchall()}
    # exit 65000/unit, fee 10/unit-of-volume -> effective 64990 per unit
    assert abs(recs[a]["pnl"] - (0.4 * (65000.0 - 10.0) - 0.4 * 60000.0)) < 1e-6
    assert abs(recs[b]["pnl"] - (0.6 * (65000.0 - 10.0) - 0.6 * 64000.0)) < 1e-6
    assert recs[a]["exit"] == recs[b]["exit"] == "tp-flatten"
    assert recs[a]["closed_ts"].startswith("2026-")        # the close's own execution time
    conn.close()


def test_tp_flatten_unpriceable_exit_keeps_plain_marker(tmp_path, monkeypatch):
    """A pair the stops swept first (or any close we can't price) must still retire —
    a missing P&L record can never cost us the close."""
    conn = _conn(tmp_path)
    store.meta_set(conn, "tp_baseline", 100.0)
    a = _seed_open_priced(conn, "OSTOP-A", vol=0.4, entry=60000.0)
    sent = []
    _wire_tp(monkeypatch, equity=120.0, positions={"P": _pos(0.4)}, open_orders={},
             terminal={}, sent=sent)
    e = _exec(conn, mode="live")
    assert e._check_take_profit() is True
    _wire_tp(monkeypatch, equity=119.5, positions={}, open_orders={},
             terminal={"OCLOSE-1": {"status": "closed"}},     # no cost/vol_exec to price
             sent=sent)
    assert e._check_take_profit() is True
    status, err = conn.execute("SELECT status, error FROM orders WHERE id=?", (a,)).fetchone()
    assert (status, err) == ("closed", "tp-flatten")          # retired, plainly marked
    conn.close()


def test_realized_pnl_since_counts_stop_exits_only(tmp_path):
    """RAILS INVARIANT. realized_pnl_since feeds the daily/weekly loss limits, whose
    question is what the STOPS took out. Now that the flatten also records per-lot P&L,
    an unfiltered SUM would silently change what a live risk rail counts — a harvest
    would read as loss-limit headroom. Pin the kind."""
    conn = _conn(tmp_path)
    _seed_closed_exit(conn, -5.0, "stop", "2026-07-27T10:00:00+00:00")
    _seed_closed_exit(conn, +40.0, "tp-flatten", "2026-07-27T11:00:00+00:00")
    conn.execute("INSERT INTO orders(symbol,side,status,mode,error) "
                 "VALUES(?,'buy','closed','live','tp-flatten')", (SYM,))   # plain text
    conn.commit()
    assert store.realized_pnl_since(conn, "2026-07-27T00:00:00+00:00") == -5.0
    conn.close()


def test_realized_ledger_counts_every_priced_exit(tmp_path):
    """The accounting view: both exit kinds, win/loss split, plain-text rows ignored."""
    conn = _conn(tmp_path)
    _seed_closed_exit(conn, -5.0, "stop", "2026-07-27T10:00:00+00:00")
    _seed_closed_exit(conn, -1.0, "stop", "2026-07-27T10:30:00+00:00")
    _seed_closed_exit(conn, +40.0, "tp-flatten", "2026-07-27T11:00:00+00:00")
    _seed_closed_exit(conn, -99.0, "stop", "2026-07-01T10:00:00+00:00")     # before window
    conn.execute("INSERT INTO orders(symbol,side,status,mode,error) "
                 "VALUES(?,'buy','closed','live','closed manually by operator')", (SYM,))
    conn.commit()
    since = "2026-07-27T00:00:00+00:00"
    all_ = store.realized_ledger_since(conn, since)
    assert (all_["n"], all_["wins"], all_["losses"]) == (3, 1, 2)
    assert abs(all_["total"] - 34.0) < 1e-9 and abs(all_["avg"] - 34.0 / 3) < 1e-9
    stops = store.realized_ledger_since(conn, since, kind="stop")
    assert (stops["n"], stops["total"]) == (2, -6.0)
    assert store.realized_ledger_since(conn, "2027-01-01T00:00:00+00:00") == {
        "n": 0, "total": 0.0, "wins": 0, "losses": 0, "avg": 0.0}
    conn.close()


def _seed_candle(conn, symbol, price, ts=1785200000):
    """One 15m candle so the seed pre-gate has a local reference price."""
    conn.execute("INSERT OR REPLACE INTO candles(pair,interval,ts,o,h,l,c,v,closed) "
                 "VALUES(?,15,?,?,?,?,?,1.0,1)", (symbol, ts, price, price, price, price))
    conn.commit()


def test_seed_pre_gate_skips_before_the_ticker_and_the_narration(tmp_path, monkeypatch):
    """The seed path is the reladder path's twin and was missed when that one got its
    pre-gate (07-27 watch). Symptom in the live log: 'SEED X: starting ladder with a
    post-only bid' every 10min at INFO with NO bid ever placed, because the governor's
    refusal had been demoted to debug — a dangling announcement that reads as a
    placement that vanished. A paced bucket must skip BEFORE the backoff stamp, the
    ticker call, and the narration."""
    monkeypatch.setattr(config, "SEED_PAIRS", (SYM,))
    monkeypatch.setattr(config, "RESPEND_BUDGET_USD_PER_HR", 5.0)
    monkeypatch.setattr(config, "RESPEND_BURST_USD", 40.0)
    monkeypatch.setattr(config, "LADDER_STEP_PCT", 0.01)
    ex_mod._seed_next.clear()
    conn = _conn(tmp_path, ordermin=0.1, costmin=0.5, lot_dec=8)
    _seed_candle(conn, SYM, 100.0)                 # smallest bid ~ 0.1 x 99 = $9.90
    _bucket(conn, tokens=1.0, age_secs=0)          # nowhere near it
    e = _exec(conn, mode="live")
    sent, ticks = [], []
    _wire_seed(monkeypatch, e, sent)
    monkeypatch.setattr(e, "_live_last", lambda sym: ticks.append(sym) or 100.0)
    e._seed_chains()
    assert sent == []                              # nothing placed (as before)
    assert ticks == []                             # ...and no REST ticker burned
    assert ex_mod._seed_next.get(SYM) is None      # ...and no 10-min backoff eaten
    conn.close()


def test_seed_pre_gate_lets_a_funded_bid_through(tmp_path, monkeypatch):
    """The gate must open once the bucket can fund the bid — a starved seeder that
    never recovers would quietly stop rebuilding stopped-out lines."""
    monkeypatch.setattr(config, "SEED_PAIRS", (SYM,))
    monkeypatch.setattr(config, "RESPEND_BUDGET_USD_PER_HR", 5.0)
    monkeypatch.setattr(config, "RESPEND_BURST_USD", 40.0)
    monkeypatch.setattr(config, "LADDER_STEP_PCT", 0.01)
    ex_mod._seed_next.clear()
    conn = _conn(tmp_path, ordermin=0.1, costmin=0.5, lot_dec=8)
    _seed_candle(conn, SYM, 100.0)
    _bucket(conn, tokens=40.0, age_secs=0)
    e = _exec(conn, mode="live")
    sent = []
    _wire_seed(monkeypatch, e, sent)
    e._seed_chains()
    assert len(sent) == 1 and sent[0]["type"] == "buy"
    conn.close()


def test_seed_pre_gate_fails_open_without_a_local_price(tmp_path, monkeypatch):
    """No candle for the pair (fresh roster entry) -> no reference price -> proceed.
    The gate is an optimisation; it must never be the reason a seed doesn't happen."""
    monkeypatch.setattr(config, "SEED_PAIRS", (SYM,))
    monkeypatch.setattr(config, "RESPEND_BUDGET_USD_PER_HR", 5.0)
    monkeypatch.setattr(config, "RESPEND_BURST_USD", 40.0)
    ex_mod._seed_next.clear()
    conn = _conn(tmp_path, ordermin=0.1, costmin=0.5, lot_dec=8)
    _bucket(conn, tokens=0.0, age_secs=0)          # empty bucket, but no price to judge
    e = _exec(conn, mode="live")
    assert e._last_local_price(SYM) is None
    sent, ticks = [], []
    _wire_seed(monkeypatch, e, sent)
    monkeypatch.setattr(e, "_live_last", lambda sym: ticks.append(sym) or 100.0)
    e._seed_chains()
    # The gate did NOT short-circuit: the pass ran on to price the bid. Whether an
    # order results is the authoritative _respend_budget_ok's call, not the gate's —
    # here the bucket really is empty, so nothing rests. That is the correct split.
    assert ticks == [SYM] and sent == []
    conn.close()
