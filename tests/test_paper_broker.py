"""The simulated exchange that makes paper mode run the real ladder.

Paper used to short-circuit in _place_entry (instant 'open', a PAPER-STOP-* that
could never trigger) and everything downstream of a fill was gated live-only. These
tests pin the counterparty that replaced that: order resting, fill-on-touch, the
no-look-ahead rule, stop triggers, long-only closing, post-only rejection, and the
end-to-end fill -> stop -> next-rung chain running in paper.
"""
import time

import pytest

from deepfield import broker, config, executor as ex_mod, paper_broker, store

SYM = "BTC/USD"
MPAIR = "XBTUSD:BTNL"

# conftest bridges broker.query_orders -> broker.query_order so legacy per-txid fakes
# keep working. Here BOTH are real and query_order is implemented ON query_orders, so
# that bridge is infinite recursion. Captured before the autouse fixture rebinds it.
_REAL_QUERY_ORDERS = broker.query_orders


@pytest.fixture
def sim(tmp_path, monkeypatch):
    """A simulated exchange attached to a throwaway DB. Always detaches, so a
    failing test can never leave broker.private() rebound for the next one."""
    monkeypatch.setattr(broker, "query_orders", _REAL_QUERY_ORDERS)
    conn = store.connect(str(tmp_path / "p.db"))
    store.upsert_pair(conn, "XXBTZUSD", SYM, "BTC", 0.00005, 0.5, 8)
    conn.commit()          # release the write lock; the simulator opens its own conn
    paper_broker.attach(str(tmp_path / "p.db"))
    try:
        yield conn
    finally:
        paper_broker.detach()


def _settle():
    """Poke the exchange so it advances to the current bar. In the running bot the
    poll loop's TradeBalance call does this every ~8s."""
    broker.private("/0/private/TradeBalance", {"asset": "ZUSD"})


def _bar(conn, ts, o, h, l, c, closed=0, sym=SYM):
    store.upsert_candle(conn, sym, 15, ts, o, h, l, c, 1.0, closed)
    conn.commit()


def _now_bar(conn, price, lo=None, hi=None):
    """A forming bar whose OPEN is in the past — the normal live shape."""
    ts = int(time.time()) - 300
    _bar(conn, ts, price, hi if hi is not None else price,
         lo if lo is not None else price, price)
    return ts


def _add(price, otype="buy", ordertype="limit", vol=0.001, oflags="post"):
    p = {"pair": MPAIR, "type": otype, "ordertype": ordertype, "volume": str(vol),
         "leverage": "10", "price": str(price)}
    if oflags:
        p["oflags"] = oflags
    return broker.private("/0/private/AddOrder", p, idempotent=False)


# ── attach / wall ────────────────────────────────────────────────────────────

def test_attach_rebinds_and_detach_restores(tmp_path):
    real = broker.private
    paper_broker.attach(str(tmp_path / "a.db"))
    assert broker.private is not real and paper_broker.attached()
    paper_broker.detach()
    assert broker.private is real and not paper_broker.attached()


def test_real_private_refuses_in_paper_mode_when_unattached(monkeypatch):
    """The last-resort wall: if attach never happened, a paper-mode call must NOT
    reach the live account (the rate limit is per-ACCOUNT and may be in use)."""
    monkeypatch.setattr(config, "EXEC_MODE", "paper")
    meta = {}
    assert broker.private("/0/private/AddOrder", {"pair": MPAIR}, meta=meta) is None
    assert meta["definite"] is True          # definitively NOT on the book


# ── resting, fills, and the no-look-ahead rule ───────────────────────────────

def test_limit_rests_until_price_touches_it(sim):
    _now_bar(sim, 100.0)
    res = _add(90.0)
    txid = res["txid"][0]
    q = broker.query_orders([txid])
    assert q[txid]["status"] == "open" and float(q[txid]["vol_exec"]) == 0

    _now_bar(sim, 89.5)                       # market comes to the bid
    q = broker.query_orders([txid])
    assert q[txid]["status"] == "closed"
    assert float(q[txid]["vol_exec"]) == pytest.approx(0.001)
    # a resting maker fills AT its price, which is what the cost basis assumes
    assert float(q[txid]["price"]) == pytest.approx(90.0)


def test_no_look_ahead_wick_already_printed_does_not_fill(sim):
    """A bar that was ALREADY forming when the order was placed must not fill it
    off a low that printed beforehand — the classic backtest look-ahead bug, which
    here would invent fills the live bot could never have won."""
    ts = int(time.time()) - 300
    _bar(sim, ts, 100.0, 100.0, 80.0, 100.0)   # low 80 printed BEFORE we rest
    res = _add(90.0)
    txid = res["txid"][0]
    assert broker.query_orders([txid])[txid]["status"] == "open"

    # a NEW bar opening after placement may use its extremes
    _bar(sim, ts + 900, 100.0, 100.0, 85.0, 100.0)
    assert broker.query_orders([txid])[txid]["status"] == "closed"


def test_fill_opens_position_and_moves_trade_balance(sim):
    _now_bar(sim, 100.0)
    _add(90.0)
    _now_bar(sim, 90.0)
    pos = broker.open_positions()
    assert len(pos) == 1
    p = next(iter(pos.values()))
    assert p["type"] == "buy" and float(p["vol"]) == pytest.approx(0.001)

    tb = broker.private("/0/private/TradeBalance", {"asset": "ZUSD"})
    assert float(tb["m"]) == pytest.approx(0.001 * 90.0 / 10, rel=1e-6)   # 10x
    assert float(tb["c"]) == pytest.approx(0.09)
    # equity = cash (start - fee) + unrealized(0 at the fill price)
    assert float(tb["e"]) == pytest.approx(config.PAPER_PORTFOLIO_USD
                                           - 0.09 * config.PAPER_FEE_MAKER_PCT, rel=1e-9)


def test_unrealized_pnl_tracks_the_mark(sim):
    _now_bar(sim, 100.0)
    _add(90.0)
    _now_bar(sim, 90.0)
    _settle()                                  # the touch fills the bid
    _now_bar(sim, 99.0)                        # mark up
    tb = broker.private("/0/private/TradeBalance", {"asset": "ZUSD"})
    assert float(tb["n"]) == pytest.approx(0.001 * (99.0 - 90.0), rel=1e-6)


# ── stops ────────────────────────────────────────────────────────────────────

def test_stop_triggers_closes_long_and_realizes_loss(sim):
    _now_bar(sim, 100.0)
    _add(90.0)
    _now_bar(sim, 90.0)                        # entry fills
    stop = _add(85.0, otype="sell", ordertype="stop-loss", oflags=None)
    stx = stop["txid"][0]
    assert broker.query_orders([stx])[stx]["status"] == "open"

    _now_bar(sim, 84.0)                        # through the stop
    q = broker.query_orders([stx])[stx]
    assert q["status"] == "closed"
    # market exit: fills at the market it gapped to, then slippage — never better
    # than the trigger
    assert float(q["price"]) <= 85.0
    assert broker.open_positions() == {}       # long is gone
    tb = broker.private("/0/private/TradeBalance", {"asset": "ZUSD"})
    assert float(tb["e"]) < config.PAPER_PORTFOLIO_USD          # realized a loss


def test_stop_does_not_trigger_above_it(sim):
    _now_bar(sim, 100.0)
    _add(90.0)
    _now_bar(sim, 90.0)
    stop = _add(85.0, otype="sell", ordertype="stop-loss", oflags=None)
    _now_bar(sim, 86.0)
    assert broker.query_orders([stop["txid"][0]])[stop["txid"][0]]["status"] == "open"


def test_sell_never_opens_a_short(sim):
    """LONG ONLY. An orphan stop whose position already closed must go nowhere,
    not invent a short."""
    _now_bar(sim, 100.0)
    sell = _add(90.0, otype="sell", ordertype="stop-loss", oflags=None)
    _now_bar(sim, 80.0)                        # would 'trigger' with nothing to sell
    assert broker.open_positions() == {}
    assert broker.query_orders([sell["txid"][0]])[sell["txid"][0]]["status"] == "canceled"


# ── post-only ────────────────────────────────────────────────────────────────

def test_post_only_buy_that_would_cross_is_rejected(sim):
    """Kraken rejects a maker order that would take liquidity. The ladder's
    below-market clamp exists to respect this, so the simulator must enforce it."""
    _now_bar(sim, 100.0)
    meta = {}
    assert broker.private("/0/private/AddOrder",
                          {"pair": MPAIR, "type": "buy", "ordertype": "limit",
                           "volume": "0.001", "price": "101.0", "oflags": "post",
                           "leverage": "10"}, meta=meta) is None
    assert "Post only" in meta["error"]


def test_non_post_only_may_cross(sim):
    _now_bar(sim, 100.0)
    assert _add(101.0, oflags=None) is not None


# ── cancel ───────────────────────────────────────────────────────────────────

def test_cancel_and_batch_cancel(sim):
    _now_bar(sim, 100.0)
    a, b = _add(90.0)["txid"][0], _add(89.0)["txid"][0]
    assert broker.cancel_order(a) == {"count": 1}
    assert broker.query_orders([a])[a]["status"] == "canceled"
    assert broker.cancel_order_batch([b]) == 1
    assert broker.open_orders() == {}


def test_open_orders_unwraps_and_filters_by_userref(sim):
    _now_bar(sim, 100.0)
    broker.private("/0/private/AddOrder",
                   {"pair": MPAIR, "type": "buy", "ordertype": "limit",
                    "volume": "0.001", "price": "90", "userref": "4242",
                    "leverage": "10"})
    assert len(broker.open_orders()) == 1
    txid, od = broker.find_order_by_userref(4242)
    assert txid and od["descr"]["pair"] == MPAIR
    assert broker.find_order_by_userref(999) == (None, None)


# ── end-to-end: the ladder actually runs in paper ────────────────────────────

def test_paper_executor_fills_rests_stop_and_places_next_rung(sim, monkeypatch):
    """The whole point: with a simulated exchange attached, a paper entry RESTS as
    a pending bid, poll_fills promotes it on a real touch, the protective stop goes
    on, and the chain continues with the next rung below the fill."""
    monkeypatch.setattr(config, "LADDER_CONTINUOUS", True)
    monkeypatch.setattr(config, "LADDER_STEP_PCT", 0.01)
    # conftest defaults _ensure_ladder_rungs inert for legacy tests; the chain is
    # exactly what this test exercises, so put the real method back.
    monkeypatch.setattr(ex_mod.Executor, "_ensure_ladder_rungs",
                        ex_mod.Executor.__dict__["_ensure_ladder_rungs"])
    monkeypatch.setattr(ex_mod.Executor, "_live_last", lambda self, s: 100.0)

    _now_bar(sim, 100.0)
    e = ex_mod.Executor(sim)
    e.mode = "paper"
    assert e._armed() is True                  # simulator attached -> fully armed

    class Card:
        low_52w, score, required = 92.0, 5, 5

    oid = e.place_entry(SYM, 99.0, Card())
    row = sim.execute("SELECT status,mode,txid,entry FROM orders WHERE id=?", (oid,)).fetchone()
    assert row[0] == "pending" and row[1] == "paper"     # RESTS — no instant fill
    assert not str(row[2]).startswith("PAPER-")          # a real (simulated) txid
    resting_px = float(row[3])

    e.poll_fills()                                        # not touched yet
    assert sim.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()[0] == "pending"

    _now_bar(sim, resting_px)                             # market comes to the bid
    e.poll_fills()
    st, stop_txid = sim.execute("SELECT status,stop_txid FROM orders WHERE id=?",
                                (oid,)).fetchone()
    assert st == "open"
    assert stop_txid and not str(stop_txid).startswith("PAPER-STOP")
    assert broker.query_orders([stop_txid])[stop_txid]["descr"]["ordertype"] == "stop-loss"

    # the chain continued: a NEW pending rung below the fill
    rungs = sim.execute("SELECT entry FROM orders WHERE status='pending' AND mode='paper'").fetchall()
    assert len(rungs) == 1 and rungs[0][0] < resting_px


def test_paper_without_simulator_keeps_legacy_instant_fill(tmp_path):
    """Paper with NO counterparty stays annotate-only — nothing to poll or ladder
    against. This is the behavior the legacy unit tests pin down."""
    conn = store.connect(str(tmp_path / "l.db"))
    store.upsert_pair(conn, "XXBTZUSD", SYM, "BTC", 0.00005, 0.5, 8)
    e = ex_mod.Executor(conn)
    e.mode = "paper"
    assert e._armed() is False

    class Card:
        low_52w, score, required = 92.0, 5, 5

    oid = e.place_entry(SYM, 100.0, Card())
    st, txid, stx = conn.execute("SELECT status,txid,stop_txid FROM orders WHERE id=?",
                                 (oid,)).fetchone()
    assert st == "open" and txid.startswith("PAPER-") and stx.startswith("PAPER-STOP")
    conn.close()


# ── financing ────────────────────────────────────────────────────────────────

def test_rollover_is_charged_on_open_notional(sim, monkeypatch):
    """Financing (~33%/yr) is the strategy's largest real cost — paper must carry
    it or every conclusion drawn from paper is wrong in the same direction."""
    monkeypatch.setattr(config, "PAPER_ROLLOVER_SECS", 0.001)
    _now_bar(sim, 100.0)
    _add(90.0)
    _now_bar(sim, 90.0)
    before = float(broker.private("/0/private/TradeBalance", {"asset": "ZUSD"})["eb"])
    time.sleep(0.01)
    broker.private("/0/private/TradeBalance", {"asset": "ZUSD"})   # settle charges it
    after = float(broker.private("/0/private/TradeBalance", {"asset": "ZUSD"})["eb"])
    assert after < before
    fees = broker.rollover_fees_since(0)
    assert fees is not None and fees[0] > 0
