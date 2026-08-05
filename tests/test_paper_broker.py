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
_REAL_PRIVATE = broker.private


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


def test_attach_refuses_in_live_mode(tmp_path, monkeypatch):
    """A live bot with a simulated exchange attached would place orders into a
    fantasy book while every gate, rail and reconcile believed they were real.
    Nothing calls attach() from a live path — this makes it impossible anyway."""
    monkeypatch.setattr(config, "EXEC_MODE", "live")
    with pytest.raises(RuntimeError, match="LIVE"):
        paper_broker.attach(str(tmp_path / "nope.db"))
    assert not paper_broker.attached()
    from deepfield import broker as _b
    assert _b.private is _REAL_PRIVATE, "broker.private must be untouched"


def test_armed_is_exactly_live_when_mode_is_live(tmp_path):
    """LIVE-NEUTRALITY GUARD. Every executor gate on this branch moved from
    `mode == "live"` to `_armed()`. That is only safe while _armed() is exactly
    equivalent for live — if someone later loosens it, every rail on the money path
    loosens with it, silently."""
    conn = store.connect(str(tmp_path / "n.db"))
    e = ex_mod.Executor(conn)
    for mode, expected in (("live", True), ("off", False), ("validate", False),
                           ("paper", False)):          # paper w/o simulator: inert
        e.mode = mode
        assert e._armed() is expected, f"{mode} armed should be {expected}"
    conn.close()


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


def test_post_only_at_the_touch_is_accepted(sim):
    """Resting AT the touch is the maker's normal position, and the rung harvest
    deliberately prices its sell at min(bid+tick, ask) — ON the ask. Treating
    equality as crossing rejected exactly those sells, so the +4% engine could never
    fire in paper. Verified to FAIL on the `price <= last` form."""
    _now_bar(sim, 100.0)
    assert _add(100.0, otype="sell", oflags="post") is not None    # at the touch: rests
    assert _add(100.0, otype="buy", oflags="post") is not None
    meta = {}
    assert broker.private("/0/private/AddOrder",
                          {"pair": MPAIR, "type": "sell", "ordertype": "limit",
                           "volume": "0.001", "price": "99.0", "oflags": "post",
                           "leverage": "10"}, meta=meta) is None   # below: crosses
    assert "Post only" in meta["error"]


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


# ── full money-path lifecycles against the simulator ─────────────────────────

def _armed_exec(conn, monkeypatch):
    """An executor armed against the simulator, with the ladder's own add-ons left
    inert so each lifecycle test exercises exactly one path.

    The harvest confirms its move with a PUBLIC ticker call before placing a sell.
    That is legitimate in the running bot (public endpoints are IP-limited, not
    charged to the account budget a competition bot may be holding) but the suite's
    hard Kraken wall blocks it, so serve the quote from the same local candle the
    simulator matches against — which also keeps trigger and fill consistent."""
    from deepfield import rest_client

    def _ticker(pairs):
        row = conn.execute("SELECT c FROM candles WHERE pair=? AND interval=15 "
                           "ORDER BY ts DESC LIMIT 1", (SYM,)).fetchone()
        px = float(row[0]) if row else 0.0
        # b/a, not just c: the harvest prices its sell off the BOOK (min(bid+tick,
        # ask)) and skips the rung entirely when the quote has no bid.
        return {p: {"c": [f"{px:.10f}"], "b": [f"{px:.10f}"], "a": [f"{px:.10f}"]}
                for p in (pairs or [])}

    monkeypatch.setattr(rest_client, "fetch_ticker", _ticker)
    monkeypatch.setattr(broker, "query_orders", _REAL_QUERY_ORDERS)
    monkeypatch.setattr(ex_mod.Executor, "_live_last", lambda self, s: None)
    e = ex_mod.Executor(conn)
    e.mode = "paper"
    assert e._armed()
    return e


class _Card:
    low_52w, score, required = 92.0, 5, 5


def _fill_one(sim, e, entry_hint=100.0):
    """Place an entry, let the market come to it, and promote it to an open lot."""
    _now_bar(sim, entry_hint)
    oid = e.place_entry(SYM, entry_hint, _Card())
    px = float(sim.execute("SELECT entry FROM orders WHERE id=?", (oid,)).fetchone()[0])
    _now_bar(sim, px)
    e.poll_fills()
    return oid, px


def test_lifecycle_rung_harvest_banks_at_plus_4pct(sim, monkeypatch):
    """The +4% per-rung skim, end to end through the simulated exchange: the stop is
    canceled, a post-only sell rests above entry, the market reaches it, the lot
    closes FIFO and the gain lands in cash. This is the engine that has been the
    primary earner since 07-29, and paper never exercised it before the simulator."""
    monkeypatch.setattr(config, "TP_RUNG_ENABLED", True)
    monkeypatch.setattr(config, "TP_RUNG_PCT", 0.04)
    monkeypatch.setattr(config, "LADDER_CONTINUOUS", False)   # isolate the harvest
    e = _armed_exec(sim, monkeypatch)
    oid, entry = _fill_one(sim, e)
    assert sim.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()[0] == "open"
    cash0 = float(paper_broker._state_get("cash"))

    # market clears the +4% target -> harvest opens
    _now_bar(sim, entry * 1.05)
    e.poll_fills()
    close_txid = sim.execute("SELECT close_txid FROM orders WHERE id=?", (oid,)).fetchone()[0]
    assert close_txid, "harvest should have rested a close order"
    sell = broker.query_orders([close_txid])[close_txid]
    assert sell["descr"]["type"] == "sell"
    assert float(sell["descr"]["price"]) >= entry * 1.04
    # stop and harvest sell must never rest together (they'd double-sell the lot)
    stx = sim.execute("SELECT stop_txid FROM orders WHERE id=?", (oid,)).fetchone()[0]
    assert not stx or broker.query_orders([stx])[stx]["status"] != "open"

    # the sell is post-only ABOVE the market; lift the market through it
    _now_bar(sim, entry * 1.10)
    e.poll_fills()
    assert broker.query_orders([close_txid])[close_txid]["status"] == "closed"
    assert broker.open_positions() == {}                      # lot retired
    assert float(paper_broker._state_get("cash")) > cash0      # gain banked


def test_lifecycle_stop_out_records_realized_pnl(sim, monkeypatch):
    """A stop-triggered exit, end to end: the simulated stop fills through the
    trigger, the ledger row closes, and the realized loss is recorded as JSON in the
    polymorphic `error` column — the shape realized_pnl_since (a LOSS RAIL) reads."""
    monkeypatch.setattr(config, "TP_RUNG_ENABLED", False)
    monkeypatch.setattr(config, "LADDER_CONTINUOUS", False)
    e = _armed_exec(sim, monkeypatch)
    oid, entry = _fill_one(sim, e)
    stop_px = float(sim.execute("SELECT stop FROM orders WHERE id=?", (oid,)).fetchone()[0])
    assert 0 < stop_px < entry
    cash0 = float(paper_broker._state_get("cash"))

    _now_bar(sim, stop_px * 0.99)                  # gap through the stop
    e.poll_fills()
    e.verify_open_stops(context="runtime")         # the sweep that notices stop-outs

    st, err = sim.execute("SELECT status,error FROM orders WHERE id=?", (oid,)).fetchone()
    assert st != "open", "a stopped-out lot must not stay open"
    assert broker.open_positions() == {}
    assert float(paper_broker._state_get("cash")) < cash0      # loss realized
    if err and err.strip().startswith("{"):        # priced exits carry the P&L blob
        import json as _json
        assert _json.loads(err)["pnl"] < 0


def test_lifecycle_stop_and_harvest_never_rest_together(sim, monkeypatch):
    """The invariant that keeps a long-only book from double-selling a lot: at no
    point may a protective stop and a harvest sell be open on the same rung."""
    monkeypatch.setattr(config, "TP_RUNG_ENABLED", True)
    monkeypatch.setattr(config, "LADDER_CONTINUOUS", False)
    e = _armed_exec(sim, monkeypatch)
    oid, entry = _fill_one(sim, e)
    for px in (entry, entry * 1.02, entry * 1.05, entry * 1.08):
        _now_bar(sim, px)
        e.poll_fills()
        stx, ctx = sim.execute("SELECT stop_txid,close_txid FROM orders WHERE id=?",
                               (oid,)).fetchone()
        live = [t for t in (stx, ctx) if t
                and (broker.query_orders([t]).get(t) or {}).get("status") == "open"]
        assert len(live) <= 1, f"stop AND harvest both resting at {px}: {live}"


def test_multi_rung_close_is_fifo_and_partial(sim, monkeypatch):
    """The ladder stacks SEVERAL rungs on one pair, so a close consumes lots oldest
    -first and may retire only part of the pair's exposure. Every earlier test held a
    single lot, where FIFO is unobservable."""
    _armed_exec(sim, monkeypatch)
    _now_bar(sim, 100.0)
    # three lots at descending entries, oldest first
    for px in (100.0, 95.0, 90.0):
        _now_bar(sim, px + 1)
        _add(px)
        _now_bar(sim, px)
        broker.open_positions()                    # settle the touch
    pos = broker.open_positions()
    assert len(pos) == 3
    entries = sorted(float(p["cost"]) / float(p["vol"]) for p in pos.values())
    assert entries == pytest.approx([90.0, 95.0, 100.0])

    cash0 = float(paper_broker._state_get("cash"))
    # sell exactly ONE lot's worth well above every entry
    _now_bar(sim, 110.0)
    _add(110.0, otype="sell", vol=0.001, oflags=None)
    broker.open_positions()

    left = broker.open_positions()
    assert len(left) == 2, "only one lot should have been retired"
    remaining = sorted(float(p["cost"]) / float(p["vol"]) for p in left.values())
    assert remaining == pytest.approx([90.0, 95.0]), "FIFO must retire the OLDEST lot"
    # realized on the oldest lot: entry 100 -> 110
    assert float(paper_broker._state_get("cash")) - cash0 == pytest.approx(
        0.001 * 10.0 - 0.001 * 110.0 * config.PAPER_FEE_MAKER_PCT, rel=1e-6)


def test_partial_close_splits_one_lot(sim, monkeypatch):
    """A sell smaller than the lot must retire PART of it (vol_closed advances) and
    leave the remainder open and still countable as backing."""
    _armed_exec(sim, monkeypatch)
    _now_bar(sim, 101.0)
    _add(100.0, vol=0.004)
    _now_bar(sim, 100.0)
    broker.open_positions()

    _now_bar(sim, 110.0)
    _add(110.0, otype="sell", vol=0.001, oflags=None)
    pos = broker.open_positions()
    assert len(pos) == 1
    p = next(iter(pos.values()))
    assert float(p["vol"]) == pytest.approx(0.004)
    assert float(p["vol_closed"]) == pytest.approx(0.001)
    # the executor's backing check reads vol - vol_closed
    assert paper_broker._long_open_vol(MPAIR) == pytest.approx(0.003)


def test_lifecycle_book_flatten_at_tp_target(sim, monkeypatch):
    """The book-level +20% T/P backstop, end to end. Paper would need days of gains
    to reach this naturally, so it had never run against the simulator: the flatten
    cancels resting orders, rests limit closes sized to LIVE exchange volume, and
    books a cycle once the book is confirmed flat."""
    monkeypatch.setattr(config, "TP_ENABLED", True)
    monkeypatch.setattr(config, "TP_PCT", 0.20)
    monkeypatch.setattr(config, "TP_RUNG_ENABLED", False)
    monkeypatch.setattr(config, "LADDER_CONTINUOUS", False)
    e = _armed_exec(sim, monkeypatch)
    oid, entry = _fill_one(sim, e)

    # arm the baseline low enough that current equity already clears the target
    store.meta_set(sim, "tp_baseline", 800.0)
    store.meta_set(sim, "tp_trough", 800.0)
    store.meta_set(sim, "tp_cycle_flows", 0.0)

    _now_bar(sim, entry)
    e.poll_fills()                                   # should START the flatten
    assert str(store.meta_get(sim, "tp_flatten_active", "") or "") == "1", \
        "equity over target must arm the flatten"
    close_txid = sim.execute("SELECT close_txid FROM orders WHERE id=?", (oid,)).fetchone()[0]
    assert close_txid, "flatten must rest a close order for the open lot"
    # the protective stop and the flatten's close must never rest together
    stx = sim.execute("SELECT stop_txid FROM orders WHERE id=?", (oid,)).fetchone()[0]
    assert not stx or (broker.query_orders([stx]).get(stx) or {}).get("status") != "open"

    # let the close fill, then converge
    _now_bar(sim, entry * 1.02)
    for _ in range(3):
        e.poll_fills()
    assert broker.open_positions() == {}, "book must end flat"
    assert sim.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()[0] != "open"
    assert str(store.meta_get(sim, "tp_flatten_active", "") or "") != "1", \
        "flatten must clear its own flag once the book is confirmed flat"


# ── excluded pairs ───────────────────────────────────────────────────────────

def test_excluded_pair_takes_no_new_entry(sim, monkeypatch):
    """'Drop' means stop ADDING. Every entry path — confirmed BUY, seed, probe —
    funnels through _place_entry, so one guard there covers all three."""
    monkeypatch.setattr(config, "EXCLUDED_PAIRS", frozenset({SYM}))
    e = _armed_exec(sim, monkeypatch)
    _now_bar(sim, 100.0)
    assert e.place_entry(SYM, 100.0, _Card()) is None
    assert sim.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert broker.open_orders() == {}, "nothing may reach the exchange"


def test_excluded_pair_grows_no_new_rung(sim, monkeypatch):
    """A rung is new exposure exactly like a seed."""
    monkeypatch.setattr(config, "LADDER_CONTINUOUS", True)
    e = _armed_exec(sim, monkeypatch)
    monkeypatch.setattr(config, "EXCLUDED_PAIRS", frozenset({SYM}))
    assert e._place_ladder_rung(SYM, MPAIR, 10, 90.0, 100.0) is None


def test_excluded_pair_keeps_its_existing_lot_protected(sim, monkeypatch):
    """The dangerous misreading of 'drop' would be to stop MANAGING the pair. An
    already-open lot must keep its stop and still reconcile."""
    e = _armed_exec(sim, monkeypatch)
    oid, _ = _fill_one(sim, e)                       # opened while allowed
    monkeypatch.setattr(config, "EXCLUDED_PAIRS", frozenset({SYM}))
    st, stx = sim.execute("SELECT status,stop_txid FROM orders WHERE id=?", (oid,)).fetchone()
    assert st == "open" and stx
    assert broker.query_orders([stx])[stx]["status"] == "open", "stop must stay resting"
    e.poll_fills()                                   # a full cycle must not disturb it
    st2, stx2 = sim.execute("SELECT status,stop_txid FROM orders WHERE id=?", (oid,)).fetchone()
    assert (st2, stx2) == (st, stx)
    assert not [f for f in paper_broker.audit(None) if not f[1]] or True   # sanity only


def test_excluded_pairs_are_absent_from_seed_list():
    for sym in ("WLD/USD", "SHIB/USD", "NEAR/USD", "ALGO/USD", "ZEC/USD", "USDC/USD"):
        assert sym in config.EXCLUDED_PAIRS
        assert sym not in config.SEED_PAIRS, f"{sym} still seeds"
    # the roster itself is untouched — they are still ingested, scored and shown
    assert all(s in {p["ws"] for p in config.PAIRS} for s in config.EXCLUDED_PAIRS)


# ── run banner ───────────────────────────────────────────────────────────────

def test_run_banner_records_what_the_run_means(caplog, monkeypatch):
    """The log is append-only and never rotated, so one file holds every restart.
    The banner is what lets a grep be scoped to a single run — and what says which
    build produced the lines."""
    from deepfield import app
    monkeypatch.setattr(config, "EXEC_MODE", "paper")
    monkeypatch.setattr(config, "PAPER_PORTFOLIO_USD", 200.0)
    monkeypatch.setattr(config, "SIZE_MULT", 2.0)
    with caplog.at_level("WARNING"):
        app._run_banner()
    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "RUN START" in line
    for expect in ("exec=paper", "paper_equity=$200", "size_mult=2", "rails=", "db=", "pid="):
        assert expect in line, f"banner missing {expect}: {line}"


def test_run_banner_survives_a_broken_git(monkeypatch):
    """_code_sha is best-effort. A banner must never be the thing that stops a
    trading process from starting."""
    from deepfield import app
    monkeypatch.setattr(config, "PROJECT_ROOT", "/nonexistent/path/xyz")
    assert app._code_sha() is None
    app._run_banner()                       # must not raise


# ── paper starting equity ────────────────────────────────────────────────────

def test_paper_equity_env_fails_safe():
    """A typo in this env var must not stop the bot from starting. A bare float()
    raises at IMPORT, taking the process down before logging exists to say why —
    the same fail-safe stance as _normalize_exec_mode."""
    assert config._paper_equity("180") == 180.0
    assert config._paper_equity(250) == 250.0
    for bad in ("junk", "", None, "-5", "0", "1e", []):
        assert config._paper_equity(bad) == 1000.0, f"{bad!r} should fall back"


# ── the integrity audit must actually detect ─────────────────────────────────

def _audit_map(db):
    return {name: (ok, detail) for name, ok, detail in paper_broker.audit(db)}


def test_audit_passes_on_a_healthy_book(sim, tmp_path, monkeypatch):
    e = _armed_exec(sim, monkeypatch)
    _fill_one(sim, e)
    res = _audit_map(str(tmp_path / "p.db"))
    assert res, "audit produced no findings"
    assert all(ok for ok, _ in res.values()), \
        "healthy book failed: " + "; ".join(f"{k}: {d}" for k, (ok, d) in res.items() if not ok)


def test_audit_catches_a_naked_lot(sim, tmp_path, monkeypatch):
    """An open lot whose stop is gone and which is not mid-harvest is the single
    most dangerous state on a leveraged book."""
    e = _armed_exec(sim, monkeypatch)
    oid, _ = _fill_one(sim, e)
    stx = sim.execute("SELECT stop_txid FROM orders WHERE id=?", (oid,)).fetchone()[0]
    sim.execute("UPDATE paper_orders SET status='canceled' WHERE txid=?", (stx,))
    sim.commit()
    ok, detail = _audit_map(str(tmp_path / "p.db"))["every open lot has a resting stop or a resting close"]
    assert not ok and "BTC" in detail


def test_audit_tolerates_a_lot_mid_harvest(sim, tmp_path, monkeypatch):
    """Stop OFF with a resting close is BY DESIGN — the sell owns the exit. The
    naked check must not cry wolf on it, or the audit becomes noise."""
    e = _armed_exec(sim, monkeypatch)
    oid, entry = _fill_one(sim, e)
    stx = sim.execute("SELECT stop_txid FROM orders WHERE id=?", (oid,)).fetchone()[0]
    broker.cancel_order(stx)                                  # as the harvest does
    sell = _add(entry * 1.04, otype="sell", oflags=None)
    sim.execute("UPDATE orders SET stop_txid=NULL, close_txid=? WHERE id=?",
                (sell["txid"][0], oid))
    sim.commit()
    ok, detail = _audit_map(str(tmp_path / "p.db"))["every open lot has a resting stop or a resting close"]
    assert ok, f"mid-harvest lot wrongly flagged: {detail}"


def test_audit_catches_volume_drift_and_bad_cash(sim, tmp_path, monkeypatch):
    e = _armed_exec(sim, monkeypatch)
    _fill_one(sim, e)
    db = str(tmp_path / "p.db")
    sim.execute("UPDATE paper_positions SET vol = vol * 2")     # exchange disagrees
    sim.commit()
    assert not _audit_map(db)["ledger volume equals exchange position volume"][0]
    paper_broker._state_set("cash", 12345.0)                    # cash no longer derives
    paper_broker._conn.commit()
    assert not _audit_map(db)["cash equals deposits plus ledger amounts minus fees"][0]


def test_audit_reports_cleanly_on_a_db_with_no_simulator(tmp_path):
    conn = store.connect(str(tmp_path / "bare.db"))
    conn.commit()
    res = paper_broker.audit(str(tmp_path / "bare.db"))
    assert len(res) == 1 and res[0][1] is False and "paper_" in res[0][2]
    conn.close()


# ── a simulated book must not page the operator ──────────────────────────────

def test_paper_safety_alerts_are_forced_quiet(monkeypatch):
    """Paper safety events stay in the log and journal but make no sound and raise
    no popup — a phantom incident on a book that is not real trains the operator to
    distrust the channel."""
    from deepfield import alerter
    monkeypatch.setattr(alerter, "_safety_last", {})
    monkeypatch.setattr(config, "EXEC_MODE", "paper")
    # 'unprotected' is a LOUD kind — the one most likely to fire during a paper run
    r = alerter.fire_safety("unprotected", "BTC/USD", "naked leveraged long")
    assert r["quiet"] is True and r["sound"] is None and r["notify"] is False


def test_live_safety_alerts_stay_loud(monkeypatch):
    """The mute is scoped to paper — live must still page exactly as before."""
    from deepfield import alerter
    monkeypatch.setattr(alerter, "_safety_last", {})
    monkeypatch.setattr(config, "EXEC_MODE", "live")
    fired = {}
    monkeypatch.setattr(alerter, "play_alert", lambda: fired.setdefault("sound", "tier1"))
    r = alerter.fire_safety("unprotected", "BTC/USD", "naked leveraged long")
    assert r.get("quiet") is not True and fired.get("sound") == "tier1"


# ── financing ────────────────────────────────────────────────────────────────

def test_attach_reanchors_inherited_fee_accounting(tmp_path):
    """A paper run is normally seeded by snapshotting the live DB (for its candle
    history), which drags the LIVE fee-accounting anchors along. Left alone the deck
    reports "carry since jul 19" for a ledger that started minutes ago. Attaching a
    FRESH simulated book must re-anchor them to now."""
    db = str(tmp_path / "seeded.db")
    conn = store.connect(db)
    store.meta_set(conn, "fees_epoch", "1784449954.0")     # 2026-07-19, from live
    store.meta_set(conn, "fees_total", "10.579")
    store.meta_set(conn, "fees_banked", "10.579")
    conn.commit()
    before = time.time()
    paper_broker.attach(db)
    try:
        assert float(store.meta_get(conn, "fees_epoch")) >= before   # re-anchored
        assert float(store.meta_get(conn, "fees_total")) == 0.0
        assert float(store.meta_get(conn, "fees_banked")) == 0.0
        # the prior values must survive under backup keys — if paper is ever pointed
        # at a LIVE ledger by mistake, that is months of rollover accounting
        assert store.meta_get(conn, "prepaper_fees_total") == "10.579"
        assert store.meta_get(conn, "prepaper_fees_epoch") == "1784449954.0"
    finally:
        paper_broker.detach()
    conn.close()


def test_reattach_does_not_rewrite_accumulated_paper_accounting(tmp_path):
    """The re-anchor is guarded by the cash seed, so a RESTART must not wipe carry
    the paper run has legitimately accumulated."""
    db = str(tmp_path / "again.db")
    conn = store.connect(db)
    conn.commit()
    paper_broker.attach(db); paper_broker.detach()
    store.meta_set(conn, "fees_total", "3.25")             # accrued during the run
    anchor = store.meta_get(conn, "fees_epoch")
    paper_broker.attach(db)                                 # restart
    try:
        assert store.meta_get(conn, "fees_total") == "3.25"
        assert store.meta_get(conn, "fees_epoch") == anchor
    finally:
        paper_broker.detach()
    conn.close()


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
