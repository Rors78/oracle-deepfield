"""A protective stop must be rounded to the pair's tick, everywhere, always.

2026-08-06, live incident. The volatility migration computed entry x (1 - sl_pct/100)
and persisted it RAW — 200.28072941616674 on a pair Kraken allows 2 decimals on. Every
other placement path in the codebase happened to round for itself, so nothing caught
it. Kraken answered every retry with

    EOrder:Invalid price:BCH/USD:BTNL price can only be specified up to 2 decimals.

and all 12 live lots — real leveraged longs — sat with NO stop on the exchange for
about four minutes while reprotect re-failed on each cycle.

Two things were wrong, and both are pinned here:

  1. Rounding lived at the call sites. That is a rule someone has to remember, and it
     held until one caller didn't. It now lives in _rest_stop, the single chokepoint
     every exchange stop passes through — a rule that cannot be forgotten.

  2. The paper simulator accepted the bad price. Two clean end-to-end migration runs
     and 570 tests certified code that could not work against the real exchange. A
     simulator more permissive than the venue it stands in for is worse than no
     simulator, because it manufactures confidence.
"""
import pytest

from .conftest import pin_vol
from deepfield import broker, config, paper_broker, store, executor as ex_mod

SYM = "BTC/USD"
MPAIR = "XBTUSD:BTNL"


def _conn(tmp_path):
    conn = store.connect(str(tmp_path / "p.db"))
    store.upsert_pair(conn, "XXBTZUSD", SYM, "BTC", 0.00005, 0.5, 8)
    return conn


# ── the chokepoint ───────────────────────────────────────────────────────────

def test_rest_stop_rounds_before_it_reaches_the_exchange(tmp_path, monkeypatch):
    """The exact failing value from the incident, sent through the real path."""
    conn = _conn(tmp_path)
    e = ex_mod.Executor(conn)
    e.mode = "live"
    sent = {}

    def _fake(path, params=None, **k):
        sent.update(params or {})
        return {"txid": ["OSTOP-1"]}

    monkeypatch.setattr(ex_mod.broker, "private", _fake)
    oid = store.insert_order(conn, {
        "ts": "2026-08-06T00:00:00+00:00", "symbol": SYM, "margin_pair": MPAIR,
        "side": "buy", "ordertype": "limit", "mode": "live", "entry": 64484.1,
        "volume": 0.0001, "leverage": 10, "status": "open"})
    e._rest_stop(SYM, MPAIR, 62127.99473449784, 0.0001, 10, oid, paper=False)
    assert sent["price"] == "62128.0", f"unrounded price reached the exchange: {sent['price']}"
    conn.close()


@pytest.mark.parametrize("sym,raw,want", [
    ("BTC/USD", 62127.99473449784, "62128.0"),      # 1 decimal
    ("ETH/USD", 1824.68344, "1824.68"),             # 2
    ("BCH/USD", 200.28072941616674, "200.28"),      # 2
    ("SUI/USD", 0.6577628, "0.6578"),               # 4
    ("XRP/USD", 1.01287424, "1.01287"),             # 5
    ("DOGE/USD", 0.0668116864, "0.0668117"),        # 7
])
def test_every_live_pair_from_the_incident_is_rounded(tmp_path, monkeypatch, sym, raw, want):
    """All six pairs that failed on 2026-08-06, at their real tick decimals."""
    conn = _conn(tmp_path)
    e = ex_mod.Executor(conn)
    e.mode = "live"
    sent = {}
    monkeypatch.setattr(ex_mod.broker, "private",
                        lambda path, params=None, **k: (sent.update(params or {}),
                                                        {"txid": ["OS"]})[1])
    oid = store.insert_order(conn, {
        "ts": "2026-08-06T00:00:00+00:00", "symbol": sym, "margin_pair": "X:BTNL",
        "side": "buy", "ordertype": "limit", "mode": "live", "entry": raw * 1.05,
        "volume": 1.0, "leverage": 10, "status": "open"})
    e._rest_stop(sym, "X:BTNL", raw, 1.0, 10, oid, paper=False)
    assert sent["price"] == want
    conn.close()


def test_an_already_rounded_stop_is_untouched(tmp_path, monkeypatch):
    """The guard must not perturb a correct price — a stop is a money level, and
    silently nudging one would be its own bug."""
    conn = _conn(tmp_path)
    e = ex_mod.Executor(conn)
    e.mode = "live"
    sent = {}
    monkeypatch.setattr(ex_mod.broker, "private",
                        lambda path, params=None, **k: (sent.update(params or {}),
                                                        {"txid": ["OS"]})[1])
    oid = store.insert_order(conn, {
        "ts": "2026-08-06T00:00:00+00:00", "symbol": SYM, "margin_pair": MPAIR,
        "side": "buy", "ordertype": "limit", "mode": "live", "entry": 64000.0,
        "volume": 0.0001, "leverage": 10, "status": "open"})
    e._rest_stop(SYM, MPAIR, 62128.0, 0.0001, 10, oid, paper=False)
    assert sent["price"] == "62128.0"
    conn.close()


# ── the migration persists a rounded value, not just a rounded wire format ───

def test_the_migration_stores_a_rounded_stop(tmp_path, monkeypatch):
    """orders.stop is read back by reprotect and reconcile for the life of the row, so
    an unrounded value in the ledger is a landmine for every later reader — not merely
    a formatting detail at send time."""
    conn = _conn(tmp_path)
    pin_vol(conn, tp=2.44, sl=3.653777079159295, rung=1.22)
    monkeypatch.setattr(config, "VOL_MIGRATE_ENABLED", True)
    e = ex_mod.Executor(conn)
    e.mode = "live"
    monkeypatch.setattr(e, "_last_local_price", lambda s: 64484.1)
    monkeypatch.setattr(ex_mod.broker, "cancel_order", lambda t: True)
    oid = store.insert_order(conn, {
        "ts": "2026-08-06T00:00:00+00:00", "symbol": SYM, "margin_pair": MPAIR,
        "side": "buy", "ordertype": "limit", "mode": "live", "entry": 64484.1,
        "stop": 58000.0, "volume": 0.0001, "leverage": 10, "status": "open",
        "stop_txid": "OOLD-1"})
    e._migrate_vol_distances()
    stop = conn.execute("SELECT stop FROM orders WHERE id=?", (oid,)).fetchone()[0]
    dec = config.MARGIN_TICK_DECIMALS.get(SYM, 2)
    assert round(stop, dec) == stop, f"migration persisted an unrounded stop: {stop!r}"
    conn.close()


# ── the simulator must be as strict as the venue ─────────────────────────────

def test_paper_rejects_a_price_with_too_many_decimals(tmp_path):
    """THE test that would have caught this before it reached real money."""
    paper_broker.attach(str(tmp_path / "sim.db"))
    try:
        meta = {}
        res = broker.private("/0/private/AddOrder", {
            "pair": MPAIR, "type": "sell", "ordertype": "stop-loss",
            "price": "62127.99473449784", "volume": "0.0001", "leverage": "10",
        }, idempotent=False, meta=meta)
        assert res is None, "the simulator accepted a price Kraken rejects"
        assert "Invalid price" in meta.get("error", "")
        assert "1 decimals" in meta["error"], meta["error"]
    finally:
        paper_broker.detach()


def test_paper_accepts_a_correctly_rounded_price(tmp_path):
    """The converse — a strictness bug that rejected everything would pass the test
    above while silently disabling the whole simulator.

    Needs a real schema and a bar, because a valid order continues past the precision
    check into the book; the rejection test above stops before it."""
    import time as _t
    db = str(tmp_path / "sim2.db")
    conn = store.connect(db)
    store.upsert_pair(conn, "XXBTZUSD", SYM, "BTC", 0.00005, 0.5, 8)
    store.upsert_candle(conn, SYM, 15, int(_t.time()) - 300,
                        64000.0, 64100.0, 63900.0, 64000.0, 1.0, 0)
    conn.commit()
    conn.close()
    paper_broker.attach(db)
    try:
        meta = {}
        res = broker.private("/0/private/AddOrder", {
            "pair": MPAIR, "type": "sell", "ordertype": "stop-loss",
            "price": "62128.0", "volume": "0.0001", "leverage": "10",
        }, idempotent=False, meta=meta)
        assert res and res.get("txid"), f"a valid stop was rejected: {meta}"
    finally:
        paper_broker.detach()


def test_paper_market_orders_are_not_judged_on_price(tmp_path):
    """A market order carries no price; precision cannot apply to it. Guards the
    reverse-gear and flatten paths, which close at market."""
    paper_broker.attach(str(tmp_path / "sim3.db"))
    try:
        meta = {}
        broker.private("/0/private/AddOrder", {
            "pair": MPAIR, "type": "buy", "ordertype": "market",
            "volume": "0.0001", "leverage": "10"}, idempotent=False, meta=meta)
        assert "Invalid price" not in meta.get("error", "")
    finally:
        paper_broker.detach()


# ── the two paths the "single chokepoint" claim missed ───────────────────────
#
# 2026-08-06, 8.5h after the incident above. A routine log check found orders.stop
# STILL holding 1.00274818 (XRP, tick 5) and 70.64291999999999 (SOL, tick 2) on live
# rows, plus six more across resting rungs. _rest_stop had been rounding correctly the
# whole time — it simply is not the only path, and the comment declaring it "the single
# chokepoint every exchange stop passes through" is precisely what stopped anyone from
# looking further. A false claim of coverage is worse than no claim.
#
#   * _place_ladder_rung COPIES its parent's stop into the new row verbatim, so one
#     over-precise value walks down an entire accumulation chain (id 1065 -> 1073).
#   * verify_open_stops' re-place leg builds its own AddOrder — it carries claiming,
#     recon counters and committed_vol bookkeeping that _rest_stop has no place for —
#     and handed orders.stop to Kraken RAW.
#
# The second is the dangerous one: it runs ONLY when a lot is already naked, so a bad
# stored value converts a one-cycle gap into a permanent one, re-failing every cycle.
# That is the incident above, reproduced inside the code written to end it.

def test_reconcile_replace_rounds_the_stop_it_read_from_the_ledger(tmp_path, monkeypatch):
    """The recovery path, in the exact shape the incident left behind: the stop was
    REJECTED, so stop_txid is NULL while the position is very much open. Reconcile
    must re-place it — and must round the stored price on the way out, or the retry
    is rejected too and the naked position simply stays naked, every cycle, forever."""
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO orders(symbol,margin_pair,volume,leverage,entry,stop,txid,stop_txid,"
        "status,mode) VALUES(?,?,?,?,?,?,?,NULL, 'open','live')",
        (SYM, MPAIR, 0.1, 10, 64484.1, 62127.99473449784, "OENTRY-1"))
    conn.commit()
    sent = []
    monkeypatch.setattr(ex_mod.broker, "open_positions",
                        lambda: {"P1": {"pair": "XXBTZUSD", "type": "buy",
                                        "vol": "0.1", "vol_closed": "0"}})
    monkeypatch.setattr(ex_mod.broker, "open_orders", lambda: {})
    monkeypatch.setattr(ex_mod.broker, "query_orders", lambda txids: {})
    monkeypatch.setattr(ex_mod.broker, "query_order", lambda t: None)
    monkeypatch.setattr(ex_mod.broker, "private",
                        lambda ep, p=None, **kw: (sent.append(p) or {"txid": ["ONEW"]}))
    monkeypatch.setattr(ex_mod.Executor, "_live_last", lambda self, s: None)
    e = ex_mod.Executor(conn)
    e.mode = "live"
    e.verify_open_stops()

    adds = [p for p in sent if p and p.get("ordertype") == "stop-loss"]
    assert adds, "the backed, stopless row was never re-protected at all"
    assert adds[0]["price"] == "62128.0", \
        f"recovery path sent an unrounded price: {adds[0]['price']}"
    # ...and the row must stop being a landmine for the NEXT reader, not just this one
    assert conn.execute("SELECT stop FROM orders").fetchone()[0] == 62128.0
    conn.close()


def test_ladder_rung_snaps_the_stop_it_inherits(tmp_path, monkeypatch):
    """The propagation path. A rung inherits its parent's stop; if it inherits the raw
    value too, every row down the chain carries the same landmine."""
    import time as _t
    from deepfield import rest_client
    conn = store.connect(str(tmp_path / "rung.db"))
    store.upsert_pair(conn, "XXBTZUSD", SYM, "BTC", 0.00005, 0.5, 8)
    pin_vol(conn, tp=4.0, sl=10.0, rung=1.0, symbols=[SYM])
    store.upsert_candle(conn, SYM, 15, int(_t.time()) - 300,
                        64000.0, 64000.0, 64000.0, 64000.0, 1.0, 0)
    conn.commit()
    paper_broker.attach(str(tmp_path / "rung.db"))
    try:
        monkeypatch.setattr(config, "LADDER_CONTINUOUS", True)
        monkeypatch.setattr(rest_client, "fetch_ticker", lambda pairs: {
            p: {"c": ["64000.0"], "b": ["64000.0"], "a": ["64000.0"]}
            for p in (pairs or [])})
        monkeypatch.setattr(ex_mod.Executor, "_live_last", lambda self, s: None)
        e = ex_mod.Executor(conn)
        e.mode = "paper"
        assert e._armed()
        e._place_ladder_rung(SYM, MPAIR, 10, 62127.99473449784, 64484.1)
        stops = [r[0] for r in conn.execute(
            "SELECT stop FROM orders WHERE stop IS NOT NULL").fetchall()]
        assert stops, "no rung row was written — the test would prove nothing"
        assert all(s == 62128.0 for s in stops), \
            f"rung persisted an unrounded inherited stop: {stops}"
    finally:
        paper_broker.detach()
        conn.close()
