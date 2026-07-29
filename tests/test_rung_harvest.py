"""Per-rung take-profit harvest (operator 2026-07-29 "build it at 4%").

Covers the money-path invariants:
  - trigger: stop canceled BEFORE the post-only sell rests (never both resting)
  - zero API calls when nothing is near target (cheap healthy path)
  - floor guard: a faded quote aborts the start before the stop is touched
  - settle: filled close -> row closed w/ {'pnl','exit':'tp-rung'} JSON
  - abort: bid under floor cancels the sell, terminal settle re-arms the stop path
  - backing: a rung not covered by free live volume is never sold
  - flatten gate: tp_flatten_active owns the book, harvester stands down
  - reconcile: a filled solo harvest close settles BEFORE budget allocation, so
    the sibling's live stop is never canceled as an "orphan"
"""
import json

from deepfield import config, store, executor as ex_mod

TS = "2026-07-29T00:00:00+00:00"
SYM, MP = "ADA/USD", "ADAUSD:BTNL"
REST = ex_mod._REST_PAIR[SYM]


def _mk_conn(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.upsert_pair(conn, MP.split(":")[0], SYM, "ADA", 1, 0.5, 8)
    return conn


def _seed_rung(conn, entry=1.0, vol=100.0, stop=0.9, stop_txid="STOP-1", oid_ts=TS):
    cur = conn.execute(
        "INSERT INTO orders(ts,symbol,margin_pair,side,ordertype,mode,entry,stop,"
        "volume,leverage,notional,margin,status,stop_txid,txid) VALUES"
        "(?,?,?,'buy','limit','live',?,?,?,3,?,?,'open',?,?)",
        (oid_ts, SYM, MP, entry, stop, vol, entry * vol, entry * vol / 3,
         stop_txid, "ENTRY-1"))
    conn.commit()
    return cur.lastrowid


def _seed_candle(conn, close, ts=1_800_000_000):
    conn.execute("INSERT OR REPLACE INTO candles(pair,interval,ts,o,h,l,c,v,closed) "
                 "VALUES(?,?,?,?,?,?,?,?,1)", (SYM, 15, ts, close, close, close, close, 1))
    conn.commit()


def _mock_broker(monkeypatch, *, quotes=None, positions=None, close_orders=None,
                 cancel_fails=False):
    calls = {"adds": [], "cancels": [], "queries": []}

    def fake_private(endpoint, params=None, **kw):
        if endpoint.endswith("AddOrder"):
            calls["adds"].append(params)
            return {"txid": [f"SELL-{len(calls['adds'])}"]}
        return {}

    def fake_cancel(txid):
        calls["cancels"].append(txid)
        return None if cancel_fails else {"count": 1}

    def fake_query_orders(txids):
        calls["queries"].append(list(txids))
        return {t: (close_orders or {}).get(t) for t in txids
                if (close_orders or {}).get(t) is not None}

    monkeypatch.setattr(ex_mod.broker, "private", fake_private)
    monkeypatch.setattr(ex_mod.broker, "cancel_order", fake_cancel)
    monkeypatch.setattr(ex_mod.broker, "query_orders", fake_query_orders)
    monkeypatch.setattr(ex_mod.broker, "open_positions",
                        lambda: positions if positions is not None else {})
    monkeypatch.setattr(ex_mod.rest_client, "fetch_ticker",
                        lambda pairs: quotes or {})
    return calls


def _mk_exec(conn, monkeypatch, **cfg):
    monkeypatch.setattr(config, "TP_RUNG_ENABLED", cfg.get("enabled", True))
    monkeypatch.setattr(config, "TP_RUNG_PCT", cfg.get("pct", 0.04), raising=False)
    monkeypatch.setattr(config, "TP_RUNG_FLOOR_PCT", cfg.get("floor", 0.02), raising=False)
    monkeypatch.setattr(config, "TP_RUNG_MAX_PER_PASS", cfg.get("cap", 4), raising=False)
    e = ex_mod.Executor(conn)
    e.mode = "live"
    return e


def _pos(vol, pair=None):
    return {"P1": {"pair": (pair or MP.split(":")[0]), "type": "buy",
                   "vol": str(vol), "vol_closed": "0"}}


def _row(conn, oid):
    return conn.execute(
        "SELECT status, stop_txid, close_txid, volume, error FROM orders WHERE id=?",
        (oid,)).fetchone()


# ── start: trigger fires, stop canceled first, sell rests ────────────────────

def test_trigger_cancels_stop_then_rests_postonly_sell(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path)
    oid = _seed_rung(conn, entry=1.0, vol=100.0)
    _seed_candle(conn, 1.05)                       # +5% > 4% target
    calls = _mock_broker(monkeypatch,
                         quotes={REST: {"b": ["1.049"], "a": ["1.051"]}},
                         positions=_pos(100.0))
    _mk_exec(conn, monkeypatch)._check_rung_harvest()
    assert calls["cancels"] == ["STOP-1"]          # stop canceled, exactly once
    assert len(calls["adds"]) == 1
    add = calls["adds"][0]
    assert add["type"] == "sell" and add["ordertype"] == "limit"
    assert add["oflags"] == "post"
    status, stop_txid, close_txid, _, _ = _row(conn, oid)
    assert status == "open" and stop_txid is None and close_txid == "SELL-1"


def test_no_trigger_means_zero_api_calls(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path)
    _seed_rung(conn, entry=1.0)
    _seed_candle(conn, 1.02)                       # +2% < 4% target
    calls = _mock_broker(monkeypatch)
    _mk_exec(conn, monkeypatch)._check_rung_harvest()
    assert calls["adds"] == [] and calls["cancels"] == [] and calls["queries"] == []


def test_faded_quote_leaves_stop_untouched(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path)
    oid = _seed_rung(conn, entry=1.0)
    _seed_candle(conn, 1.05)                       # candle says go...
    calls = _mock_broker(monkeypatch,
                         quotes={REST: {"b": ["1.015"], "a": ["1.017"]}},  # ...bid under +2% floor
                         positions=_pos(100.0))
    _mk_exec(conn, monkeypatch)._check_rung_harvest()
    assert calls["cancels"] == [] and calls["adds"] == []
    assert _row(conn, oid)[1] == "STOP-1"          # still protected


def test_unbacked_rung_is_never_sold(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path)
    oid = _seed_rung(conn, entry=1.0, vol=100.0)
    _seed_rung(conn, entry=1.0, vol=100.0, stop_txid="STOP-2")   # sibling stop commits 100
    _seed_candle(conn, 1.05)
    calls = _mock_broker(monkeypatch,
                         quotes={REST: {"b": ["1.049"], "a": ["1.051"]}},
                         positions=_pos(150.0))    # live 150 < 100 free + 100 sibling
    _mk_exec(conn, monkeypatch)._check_rung_harvest()
    assert calls["adds"] == []
    assert _row(conn, oid)[1] == "STOP-1"


def test_flatten_active_gates_everything(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path)
    _seed_rung(conn)
    _seed_candle(conn, 1.10)
    store.meta_set(conn, "tp_flatten_active", "1")
    calls = _mock_broker(monkeypatch,
                         quotes={REST: {"b": ["1.09"], "a": ["1.11"]}},
                         positions=_pos(100.0))
    _mk_exec(conn, monkeypatch)._check_rung_harvest()
    assert calls["adds"] == [] and calls["cancels"] == [] and calls["queries"] == []


def test_stop_cancel_failure_skips_pair(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path)
    oid = _seed_rung(conn)
    _seed_candle(conn, 1.05)
    calls = _mock_broker(monkeypatch, cancel_fails=True,
                         quotes={REST: {"b": ["1.049"], "a": ["1.051"]}},
                         positions=_pos(100.0))
    _mk_exec(conn, monkeypatch)._check_rung_harvest()
    assert calls["adds"] == []                     # no sell beside a live stop
    assert _row(conn, oid)[1] == "STOP-1"


# ── manage: settle, abort, partial ───────────────────────────────────────────

def test_filled_close_settles_with_tp_rung_pnl(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path)
    oid = _seed_rung(conn, entry=1.0, vol=100.0, stop_txid=None)
    conn.execute("UPDATE orders SET close_txid='SELL-X', stop_txid=NULL WHERE id=?", (oid,))
    conn.commit()
    _mock_broker(monkeypatch, close_orders={
        "SELL-X": {"status": "closed", "vol_exec": "100.0", "cost": "104.0",
                   "fee": "0.17", "closetm": 1753747200.0}})
    _mk_exec(conn, monkeypatch)._check_rung_harvest()
    status, _, close_txid, _, err = _row(conn, oid)
    assert status == "closed" and close_txid is None
    rec = json.loads(err)
    assert rec["exit"] == "tp-rung"
    assert abs(rec["pnl"] - (104.0 - 0.17 - 100.0)) < 1e-6


def test_bid_under_floor_aborts_then_terminal_settle_rearms(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path)
    oid = _seed_rung(conn, entry=1.0, vol=100.0, stop_txid=None)
    conn.execute("UPDATE orders SET close_txid='SELL-X', stop_txid=NULL WHERE id=?", (oid,))
    conn.commit()
    # pass 1: resting, bid sagged under the +2% floor -> cancel, ref kept
    calls = _mock_broker(monkeypatch,
                         quotes={REST: {"b": ["1.01"], "a": ["1.012"]}},
                         close_orders={"SELL-X": {"status": "open", "vol_exec": "0",
                                                  "descr": {"price": "1.045"}}})
    e = _mk_exec(conn, monkeypatch)
    e._check_rung_harvest()
    assert calls["cancels"] == ["SELL-X"]
    assert _row(conn, oid)[2] == "SELL-X"          # ref kept for the terminal settle
    # pass 2: terminal canceled, nothing filled -> ref cleared, row back to normal
    _mock_broker(monkeypatch, close_orders={
        "SELL-X": {"status": "canceled", "vol_exec": "0", "cost": "0", "fee": "0"}})
    e._check_rung_harvest()
    status, stop_txid, close_txid, vol, _ = _row(conn, oid)
    assert status == "open" and close_txid is None and stop_txid is None
    assert float(vol) == 100.0                     # reprotect re-arms the stop from here


def test_partial_fill_shrinks_row_and_clears_ref(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path)
    oid = _seed_rung(conn, entry=1.0, vol=100.0, stop_txid=None)
    conn.execute("UPDATE orders SET close_txid='SELL-X', stop_txid=NULL WHERE id=?", (oid,))
    conn.commit()
    _mock_broker(monkeypatch, close_orders={
        "SELL-X": {"status": "canceled", "vol_exec": "40.0", "cost": "41.6", "fee": "0.07"}})
    _mk_exec(conn, monkeypatch)._check_rung_harvest()
    status, _, close_txid, vol, _ = _row(conn, oid)
    assert status == "open" and close_txid is None
    assert abs(float(vol) - 60.0) < 1e-9


def test_unknown_close_status_is_left_alone(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path)
    oid = _seed_rung(conn, entry=1.0, vol=100.0, stop_txid=None)
    conn.execute("UPDATE orders SET close_txid='SELL-X', stop_txid=NULL WHERE id=?", (oid,))
    conn.commit()
    calls = _mock_broker(monkeypatch, close_orders={})   # query returns nothing
    _mk_exec(conn, monkeypatch)._check_rung_harvest()
    assert calls["cancels"] == [] and calls["adds"] == []
    assert _row(conn, oid)[2] == "SELL-X"


def test_one_harvest_per_pair_at_a_time(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path)
    _seed_rung(conn, entry=1.0, vol=100.0)                       # would trigger...
    oid2 = _seed_rung(conn, entry=1.0, vol=50.0, stop_txid=None)
    conn.execute("UPDATE orders SET close_txid='SELL-X', stop_txid=NULL WHERE id=?", (oid2,))
    conn.commit()                                                # ...but pair already harvesting
    _seed_candle(conn, 1.05)
    calls = _mock_broker(monkeypatch,
                         quotes={REST: {"b": ["1.049"], "a": ["1.051"]}},
                         positions=_pos(150.0),
                         close_orders={"SELL-X": {"status": "open", "vol_exec": "0",
                                                  "descr": {"price": "1.049"}}})
    _mk_exec(conn, monkeypatch)._check_rung_harvest()
    assert calls["adds"] == []                     # no second sell on the pair


# ── reconcile: filled harvest close settles before budget allocation ─────────

def test_reconcile_settles_filled_harvest_before_budget(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path)
    harvested = _seed_rung(conn, entry=1.0, vol=100.0, stop_txid=None)
    conn.execute("UPDATE orders SET close_txid='SELL-X', stop_txid=NULL WHERE id=?",
                 (harvested,))
    sibling = _seed_rung(conn, entry=0.95, vol=100.0, stop_txid="STOP-SIB")
    conn.commit()
    # exchange truth: harvest sell FILLED -> only the sibling's 100 remains live
    calls = _mock_broker(monkeypatch, positions=_pos(100.0), close_orders={
        "SELL-X": {"status": "closed", "vol_exec": "100.0", "cost": "104.0",
                   "fee": "0.17", "closetm": 1753747200.0},
        "STOP-SIB": {"status": "open"}})
    monkeypatch.setattr(ex_mod.broker, "open_orders", lambda: {"STOP-SIB": {}})
    e = _mk_exec(conn, monkeypatch)
    e.verify_open_stops(context="runtime")
    # harvested row retired w/ P&L; sibling untouched, its stop NOT canceled
    status, _, _, _, err = _row(conn, harvested)
    assert status == "closed" and json.loads(err)["exit"] == "tp-rung"
    s_status, s_stop, _, _, _ = _row(conn, sibling)
    assert s_status == "open" and s_stop == "STOP-SIB"
    assert "STOP-SIB" not in calls["cancels"]


def test_reconcile_leaves_flatten_shared_close_refs_alone(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path)
    a = _seed_rung(conn, entry=1.0, vol=60.0, stop_txid=None)
    b = _seed_rung(conn, entry=1.0, vol=40.0, stop_txid=None)
    conn.execute("UPDATE orders SET close_txid='FLAT-X', stop_txid=NULL WHERE id IN (?,?)",
                 (a, b))
    conn.commit()
    calls = _mock_broker(monkeypatch, positions=_pos(100.0), close_orders={})
    monkeypatch.setattr(ex_mod.broker, "open_orders", lambda: {})
    _mk_exec(conn, monkeypatch).verify_open_stops(context="runtime")
    # 1:N close refs are the flatten's — the harvest-settle branch must not touch them
    assert _row(conn, a)[0] == "open" and _row(conn, b)[0] == "open"
    assert _row(conn, a)[2] == "FLAT-X" and _row(conn, b)[2] == "FLAT-X"
    assert calls["adds"] == []                     # and no stop re-placed beside them


# ── stop ratchet: a proved rung re-arms at breakeven, ladder floor unchanged ──
#
# The harvest layer is asymmetric (+4% target vs ~7.8% mean stop distance), so it
# needs ~70% of rungs to reach target before stop just to break even. The ratchet
# closes the one-sided leak: a rung that PROVED the target and then faded used to
# re-arm at its original invalidation level and ride ~8% further down.

def _prot(conn, oid):
    """The level a stop would actually rest at — what reprotect/reconcile read."""
    return conn.execute("SELECT COALESCE(stop_prot, stop) FROM orders WHERE id=?",
                        (oid,)).fetchone()[0]


def test_harvest_start_ratchets_protective_stop_to_breakeven(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path)
    oid = _seed_rung(conn, entry=1.0, vol=100.0, stop=0.92)
    _seed_candle(conn, 1.05)
    _mock_broker(monkeypatch, quotes={REST: {"b": ["1.049"], "a": ["1.051"]}},
                 positions=_pos(100.0))
    _mk_exec(conn, monkeypatch)._check_rung_harvest()
    assert _prot(conn, oid) == 1.0                 # protective stop -> breakeven
    # ...and the chain's INVALIDATION level is untouched, so the ladder still
    # has room below (this is the whole reason stop_prot is its own column).
    assert conn.execute("SELECT stop FROM orders WHERE id=?", (oid,)).fetchone()[0] == 0.92


def test_ratcheted_rung_reprotects_at_breakeven_after_abort(tmp_path, monkeypatch):
    """End to end: harvest starts (+5%), fades under the floor, aborts, settles —
    the re-armed stop must be breakeven, not the original 0.92."""
    conn = _mk_conn(tmp_path)
    oid = _seed_rung(conn, entry=1.0, vol=100.0, stop=0.92)
    _seed_candle(conn, 1.05)
    _mock_broker(monkeypatch, quotes={REST: {"b": ["1.049"], "a": ["1.051"]}},
                 positions=_pos(100.0))
    e = _mk_exec(conn, monkeypatch)
    e._check_rung_harvest()                        # sell rests, stop ratcheted
    assert _row(conn, oid)[2] == "SELL-1"
    # market fades under the +2% floor -> abort cancels the sell
    _mock_broker(monkeypatch, quotes={REST: {"b": ["1.005"], "a": ["1.007"]}},
                 positions=_pos(100.0), close_orders={"SELL-1": {"status": "open"}})
    _mk_exec(conn, monkeypatch)._check_rung_harvest()
    # terminal settle: canceled unfilled -> close_txid clears, row is naked
    _mock_broker(monkeypatch, quotes={REST: {"b": ["1.005"], "a": ["1.007"]}},
                 positions=_pos(100.0),
                 close_orders={"SELL-1": {"status": "canceled", "vol_exec": "0"}})
    _mk_exec(conn, monkeypatch)._check_rung_harvest()
    assert _row(conn, oid)[2] is None and _row(conn, oid)[1] is None
    # reprotect re-arms: the stop it rests must be the RATCHETED level
    calls = _mock_broker(monkeypatch, positions=_pos(100.0))
    monkeypatch.setattr(ex_mod.broker, "open_orders", lambda: {})
    monkeypatch.setattr(config, "PROTECTIVE_STOP", True)
    _mk_exec(conn, monkeypatch)._reprotect_naked_open()
    assert len(calls["adds"]) == 1
    assert float(calls["adds"][0]["price"]) == 1.0        # breakeven, not 0.92
    assert calls["adds"][0]["ordertype"] == "stop-loss"


def test_ratchet_never_lowers_an_existing_protective_stop(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path)
    oid = _seed_rung(conn, entry=1.0, vol=100.0, stop=0.92)
    conn.execute("UPDATE orders SET stop_prot=1.03 WHERE id=?", (oid,))   # already higher
    conn.commit()
    _seed_candle(conn, 1.05)
    _mock_broker(monkeypatch, quotes={REST: {"b": ["1.049"], "a": ["1.051"]}},
                 positions=_pos(100.0))
    _mk_exec(conn, monkeypatch)._check_rung_harvest()
    assert _prot(conn, oid) == 1.03                # monotonic — never ratchets down


def test_ratchet_skipped_when_level_would_rest_at_or_above_market(tmp_path, monkeypatch):
    """A stop resting at/above the market fires on contact. With the ratchet pct
    misconfigured above the floor, the per-rung bid guard must no-op it."""
    conn = _mk_conn(tmp_path)
    oid = _seed_rung(conn, entry=1.0, vol=100.0, stop=0.92)
    _seed_candle(conn, 1.05)
    _mock_broker(monkeypatch, quotes={REST: {"b": ["1.03"], "a": ["1.032"]}},
                 positions=_pos(100.0))
    # ratchet 10% -> 1.10, above the 1.03 bid: refuse rather than arm a live trigger
    monkeypatch.setattr(config, "TP_RUNG_RATCHET_PCT", 0.10, raising=False)
    _mk_exec(conn, monkeypatch)._check_rung_harvest()
    assert _prot(conn, oid) == 0.92                # untouched


def test_ratchet_disabled_leaves_stop_alone(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path)
    oid = _seed_rung(conn, entry=1.0, vol=100.0, stop=0.92)
    _seed_candle(conn, 1.05)
    _mock_broker(monkeypatch, quotes={REST: {"b": ["1.049"], "a": ["1.051"]}},
                 positions=_pos(100.0))
    monkeypatch.setattr(config, "TP_RUNG_RATCHET_ENABLED", False, raising=False)
    _mk_exec(conn, monkeypatch)._check_rung_harvest()
    assert _prot(conn, oid) == 0.92


def test_ratchet_does_not_freeze_the_ladder(tmp_path, monkeypatch):
    """Regression for the reason stop_prot is a separate column: _reladder inherits
    the lowest open fill's `stop` as the next rung's FLOOR. The lowest entry in a
    long chain is the most in profit — i.e. exactly the rung that gets ratcheted —
    so writing breakeven into `stop` would trip "ladder floor reached" and silently
    stop accumulating on a winning pair."""
    conn = _mk_conn(tmp_path)
    oid = _seed_rung(conn, entry=1.0, vol=100.0, stop=0.92)
    _seed_candle(conn, 1.05)
    _mock_broker(monkeypatch, quotes={REST: {"b": ["1.049"], "a": ["1.051"]}},
                 positions=_pos(100.0))
    _mk_exec(conn, monkeypatch)._check_rung_harvest()
    # what _reladder hands _place_ladder_rung as the floor:
    stop_for_ladder = conn.execute(
        "SELECT stop FROM orders WHERE status='open' AND entry IS NOT NULL "
        "ORDER BY entry ASC LIMIT 1").fetchone()[0]
    next_rung = 1.0 * (1 - config.LADDER_STEP_PCT)
    assert stop_for_ladder == 0.92
    assert next_rung > stop_for_ladder * (1 + config.LADDER_STOP_BUFFER)   # room remains
