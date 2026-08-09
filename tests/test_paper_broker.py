"""The simulated exchange that makes paper mode run the real ladder.

Paper used to short-circuit in _place_entry (instant 'open', a PAPER-STOP-* that
could never trigger) and everything downstream of a fill was gated live-only. These
tests pin the counterparty that replaced that: order resting, fill-on-touch, the
no-look-ahead rule, stop triggers, long-only closing, post-only rejection, and the
end-to-end fill -> stop -> next-rung chain running in paper.
"""
import time

import pytest

from .conftest import pin_vol
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
    pin_vol(sim, rung=1)          # `sim` IS the connection (see the fixture)
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
        #
        # The ask sits ONE TICK above the bid (BTC tick_dec=1 -> 0.1). This stub
        # used to serve bid == ask == last — a zero-spread book no venue ever
        # shows — and the harvest's degenerate-book abort (2026-08-08) correctly
        # refuses that state: a post-only sell at a price <= bid crosses. On a
        # zero spread there IS no maker price, so the old quotes only worked
        # because the old code sent a crossing sell the simulator happened to
        # rest. A one-tick spread is the minimal honest book.
        return {p: {"c": [f"{px:.10f}"], "b": [f"{px:.10f}"], "a": [f"{px + 0.1:.10f}"]}
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


def _exchange_stops():
    """Resting stop-loss orders per the EXCHANGE, not the ledger.

    The 'never both resting' invariant lives on Kraken's book. The ledger's
    stop_txid is NULLed the moment the harvest believes it canceled — so a DB
    read short-circuits (`not stx`) precisely when the invariant is at risk, and
    a cancel that never reached the exchange leaves an orphaned stop no DB query
    can see. Proven by mutation 2026-08-08: clearing stop_txid WITHOUT calling
    broker.cancel_order left the sim holding stop AND harvest sell together —
    a double-sell primed to short the lot — and every DB-based assertion passed."""
    oo = broker.open_orders() or {}
    return [t for t, o in oo.items()
            if "stop" in str((o.get("descr") or {}).get("ordertype", "")).lower()]


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
    # The target is per-pair and frozen on the row since 2026-08-06; pin the table to
    # the +4% this test is written around so it exercises the real resolver.
    pin_vol(sim, tp=4.0)
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
    # — judged on the EXCHANGE book; see _exchange_stops for why the DB can't.
    assert _exchange_stops() == [], \
        "harvest sell resting while a stop still sits on the exchange"

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
        # Judged on the EXCHANGE book, both halves: the ledger's stop_txid is
        # NULLed at cancel time, so a DB read goes blind at the exact moment the
        # invariant is exposed (see _exchange_stops).
        oo = broker.open_orders() or {}
        stops = [t for t, o in oo.items()
                 if "stop" in str((o.get("descr") or {}).get("ordertype", "")).lower()]
        sells = [t for t, o in oo.items()
                 if (o.get("descr") or {}).get("type") == "sell"
                 and "stop" not in str((o.get("descr") or {}).get("ordertype", "")).lower()]
        assert not (stops and sells), \
            f"stop AND harvest sell both resting at {px}: stops={stops} sells={sells}"


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


def _arm_flatten(sim, monkeypatch):
    """Shared setup: one filled lot, baseline armed so equity already clears the
    +20% target — the next poll starts the flatten."""
    monkeypatch.setattr(config, "TP_ENABLED", True)
    monkeypatch.setattr(config, "TP_PCT", 0.20)
    monkeypatch.setattr(config, "TP_RUNG_ENABLED", False)
    monkeypatch.setattr(config, "LADDER_CONTINUOUS", False)
    e = _armed_exec(sim, monkeypatch)
    oid, entry = _fill_one(sim, e)
    store.meta_set(sim, "tp_baseline", 800.0)
    store.meta_set(sim, "tp_trough", 800.0)
    store.meta_set(sim, "tp_cycle_flows", 0.0)
    return e, oid, entry


def test_flatten_backstop_cancels_a_stop_the_sweep_missed(sim, monkeypatch):
    """Stage 1b — the per-stop backstop behind the CancelOrderBatch sweep. Until
    2026-08-08 this branch was UNREACHABLE in tests: the simulator's batch cancel
    always succeeds, so clearing stop_txid WITHOUT cancelling (the naked-short
    landmine) passed every flatten test — a mutation the suite provably could not
    catch. Fault injection at the broker boundary: the batch 'loses' the stop
    txid, the backstop must cancel it individually, and the exchange book must
    end with the close resting and NO stop beside it."""
    e, oid, entry = _arm_flatten(sim, monkeypatch)
    stx = sim.execute("SELECT stop_txid FROM orders WHERE id=?", (oid,)).fetchone()[0]
    real_batch = broker.cancel_order_batch
    monkeypatch.setattr(broker, "cancel_order_batch",
                        lambda txids: real_batch([t for t in txids if t != stx]))
    _now_bar(sim, entry)
    e.poll_fills()                                   # flatten starts, sweep "misses" stx
    assert str(store.meta_get(sim, "tp_flatten_active", "") or "") == "1"
    assert sim.execute("SELECT close_txid FROM orders WHERE id=?", (oid,)).fetchone()[0], \
        "backstop cancel succeeded — the close must still rest"
    assert _exchange_stops() == [], \
        "the sweep-missed stop is STILL on the exchange beside the close (double-sell)"


def test_flatten_backstop_failure_keeps_the_pair_protected(sim, monkeypatch):
    """The other branch: sweep misses the stop AND the individual cancel fails.
    The pair must be BLOCKED — no close rested (a close beside a live stop is the
    double-sell), the stop still resting, the row still referencing it."""
    e, oid, entry = _arm_flatten(sim, monkeypatch)
    stx = sim.execute("SELECT stop_txid FROM orders WHERE id=?", (oid,)).fetchone()[0]
    real_batch = broker.cancel_order_batch
    real_cancel = broker.cancel_order
    monkeypatch.setattr(broker, "cancel_order_batch",
                        lambda txids: real_batch([t for t in txids if t != stx]))
    monkeypatch.setattr(broker, "cancel_order",
                        lambda t: None if t == stx else real_cancel(t))
    _now_bar(sim, entry)
    e.poll_fills()
    assert sim.execute("SELECT close_txid FROM orders WHERE id=?", (oid,)).fetchone()[0] is None, \
        "a close was rested beside a stop that could not be cancelled"
    assert len(_exchange_stops()) == 1, "the uncancellable stop must stay resting — protected"
    assert sim.execute("SELECT stop_txid FROM orders WHERE id=?", (oid,)).fetchone()[0] == stx, \
        "the ledger dropped its reference to a stop that is still live"


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
    # — judged on the EXCHANGE book; see _exchange_stops for why the DB can't.
    assert _exchange_stops() == [], \
        "flatten close resting while a stop still sits on the exchange"

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
    """Exclusion means "no NEW exposure", NOT removal: these five stay ingested, scored
    and shown, and their existing lots keep stops, harvest and reconcile.

    USDC/USD is deliberately absent from this list — it was REMOVED from the roster
    outright on 2026-08-06, a different thing, pinned in test_rails_rearm."""
    for sym in ("WLD/USD", "SHIB/USD", "NEAR/USD", "ALGO/USD", "ZEC/USD"):
        assert sym in config.EXCLUDED_PAIRS
        assert sym not in config.SEED_PAIRS, f"{sym} still seeds"
    # the roster itself is untouched — they are still ingested, scored and shown
    assert all(s in {p["ws"] for p in config.PAIRS} for s in config.EXCLUDED_PAIRS)


# ── run banner ───────────────────────────────────────────────────────────────

def test_run_banner_records_what_the_run_means(tmp_path, caplog, monkeypatch):
    """The log is append-only and never rotated, so one file holds every restart.
    The banner is what lets a grep be scoped to a single run — and what says which
    build produced the lines."""
    from deepfield import app
    monkeypatch.setattr(config, "EXEC_MODE", "paper")
    monkeypatch.setattr(config, "PAPER_PORTFOLIO_USD", 200.0)
    monkeypatch.setattr(config, "SIZE_MULT", 2.0)
    # Point at an empty DB: the banner now reads paper_state for the seeded cash,
    # and a test must not read whatever book happens to sit in the checkout.
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "empty.db"))
    with caplog.at_level("WARNING"):
        app._run_banner()
    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "RUN START" in line
    for expect in ("exec=paper", "size_mult=2", "rails=", "db=", "pid="):
        assert expect in line, f"banner missing {expect}: {line}"
    # No paper book seeded here, so it must say SEED and mark it fresh — never
    # imply the run holds money it does not.
    assert "paper_seed=$200" in line and "fresh book" in line, line


def test_run_banner_reports_seeded_cash_not_the_constant(tmp_path, caplog, monkeypatch):
    """PAPER_PORTFOLIO_USD seeds the simulated book exactly ONCE, so on every
    restart against an existing book the constant and the actual money diverge. On
    2026-08-05 the banner announced "paper_equity=$1000" over a $199.67 book. A boot
    line whose whole job is to identify the run must not state a number the run does
    not have."""
    from deepfield import app
    db = tmp_path / "paper.db"
    conn = store.connect(str(db))
    conn.execute("CREATE TABLE IF NOT EXISTS paper_state(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO paper_state(key,value) VALUES('cash','199.67')")
    conn.commit(); conn.close()
    monkeypatch.setattr(config, "EXEC_MODE", "paper")
    monkeypatch.setattr(config, "PAPER_PORTFOLIO_USD", 1000.0)
    monkeypatch.setattr(config, "DB_PATH", str(db))
    with caplog.at_level("WARNING"):
        app._run_banner()
    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "paper_cash=$199.67" in line, line
    assert "1000" not in line, f"the seed constant must not appear over a live book: {line}"


def test_run_banner_survives_an_unreadable_db(tmp_path, caplog, monkeypatch):
    """The banner reads the DB now. It must still never be the reason a trading
    process fails to start."""
    from deepfield import app
    monkeypatch.setattr(config, "EXEC_MODE", "paper")
    monkeypatch.setattr(config, "DB_PATH", "/nonexistent/dir/nope.db")
    with caplog.at_level("WARNING"):
        app._run_banner()                   # must not raise
    assert "RUN START" in "\n".join(r.getMessage() for r in caplog.records)


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
    # Rounded to the pair's tick, as the harvest does: entry * 1.04 is
    # 103.89600000000002, which Kraken rejects on a 1-decimal pair and the simulator
    # now rejects too (2026-08-06 — it used to accept any precision, which is how an
    # unrounded stop reached the live book and left it naked).
    sell = _add(round(entry * 1.04, config.MARGIN_TICK_DECIMALS.get(SYM, 2)),
                otype="sell", oflags=None)
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


def test_alerts_are_quiet_whenever_a_simulated_exchange_is_attached(sim, monkeypatch):
    """EXEC_MODE alone was not enough. A harness that attaches the simulator and
    drives an Executor with mode='paper' leaves EXEC_MODE at 'off' — and exactly
    that fired three critical popups and sirens at the operator on 2026-08-05.
    A fake counterparty means no alert from it is real, whatever the mode says."""
    from deepfield import alerter
    monkeypatch.setattr(alerter, "_safety_last", {})
    monkeypatch.setattr(config, "EXEC_MODE", "off")     # the harness case
    assert paper_broker.attached()
    r = alerter.fire_safety("stop-fired", "*", "protective stops EXECUTED")
    assert r["quiet"] is True and r["sound"] is None and r["notify"] is False


def test_market_order_fills_at_the_market_not_at_zero(sim):
    """A market order carries no price. Treating it as a limit stored price=0, and
    for a sell `last >= 0` is always true — so it filled at ZERO and destroyed the
    lot's entire notional instead of realizing its P&L. The reverse gear is the only
    caller that sends market orders, which is why this survived until the gear was
    first simulated. Verified to FAIL before the fix."""
    _now_bar(sim, 100.0)
    _add(90.0)
    _now_bar(sim, 90.0)
    broker.open_positions()                       # a lot at 90
    cash0 = float(paper_broker._state_get("cash"))

    _now_bar(sim, 95.0)
    r = broker.private("/0/private/AddOrder",
                       {"pair": MPAIR, "type": "sell", "ordertype": "market",
                        "volume": "0.001", "leverage": "10"}, idempotent=False)
    assert r and r.get("txid")
    broker.open_positions()                       # settle
    q = broker.query_orders([r["txid"][0]])[r["txid"][0]]
    assert q["status"] == "closed"
    fill = float(q["price"])
    assert 94.0 < fill <= 95.0, f"market sell filled at {fill}, expected ~95"
    # closing 90 -> ~95 must GAIN, not wipe the notional
    assert float(paper_broker._state_get("cash")) > cash0
    assert broker.open_positions() == {}


def test_fresh_seed_inherits_no_book_state(tmp_path):
    """A paper DB is seeded by snapshotting the LIVE one, for its candle history.
    That is right for what describes the MARKET and wrong for everything that
    describes a BOOK — and it went wrong five separate ways on 2026-08-05: the fee
    anchors, the T/P cycle ledger, the equity curve (a $1000->$200 reset drawn as an
    80% crash), the journal (the HARVEST filter showed a LIVE rung bank), and the
    alert ledger. This pins the whole family so a sixth cannot creep in.

    Note what is deliberately NOT reset: candles and pairs. Inheriting those is the
    entire reason to seed from a snapshot rather than re-backfill the roster over the
    shared public API."""
    db = str(tmp_path / "seeded.db")
    conn = store.connect(db)
    # a snapshot of a live book: market data AND book state
    store.upsert_pair(conn, "XXBTZUSD", SYM, "BTC", 0.00005, 0.5, 8)
    store.upsert_candle(conn, SYM, 15, 1785000000, 1, 1, 1, 1, 1.0, 1)
    store.meta_set(conn, "fees_total", "10.579")
    store.meta_set(conn, "fees_epoch", "1784449954.0")
    store.journal(conn, "tp-rung", "HBAR/USD", "rung 877 banked $+0.1285 (55 sold)")
    store.journal(conn, "safety", "*", "[recon-mismatch] 107 ledger rows retired")
    conn.execute("INSERT INTO equity_history(ts,equity) VALUES(?,?)", (1785000000, 1000.21))
    conn.execute("INSERT INTO alerts(ts,symbol,price,score,denom,signals,kind) "
                 "VALUES(?,?,?,?,?,?,?)", ("2026-07-04T10:23", SYM, 1.0, 5, 7, "", "confirmed"))
    conn.commit()

    market_before = (conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0],
                     conn.execute("SELECT COUNT(*) FROM pairs").fetchone()[0])
    assert market_before == (1, 1)

    paper_broker.attach(db)
    try:
        for tbl in ("journal", "alerts", "equity_history", "tp_cycles"):
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            assert n == 0, f"{tbl} inherited {n} row(s) from the snapshot"
        # fee accounting re-anchored to this book, prior values recoverable
        assert float(store.meta_get(conn, "fees_total")) == 0.0
        assert store.meta_get(conn, "prepaper_fees_total") == "10.579"
        # and the market data — the reason for the snapshot — is untouched
        assert (conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]) == market_before
    finally:
        paper_broker.detach()
    conn.close()


# ── money-path invariants that were documented but never enforced ────────────

def _mk(conn, sym=SYM, entry=100.0, stop=90.0, stop_prot=None):
    conn.execute("INSERT INTO orders(ts,symbol,margin_pair,side,ordertype,mode,entry,stop,"
                 "stop_prot,volume,leverage,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("2026-08-05T00:00:00+00:00", sym, MPAIR, "buy", "limit", "paper",
                  entry, stop, stop_prot, 1.0, 10, "open"))
    conn.commit()
    return conn.execute("SELECT MAX(id) FROM orders").fetchone()[0]


def _prot(conn, oid):
    return conn.execute("SELECT stop_prot, stop FROM orders WHERE id=?", (oid,)).fetchone()


def test_ratchet_never_lowers_a_stop(sim, monkeypatch):
    """MONOTONIC. A protective stop may only ever move UP. Lowering one re-opens
    risk the operator already banked, silently."""
    monkeypatch.setattr(config, "TP_RUNG_RATCHET_ENABLED", True)
    monkeypatch.setattr(config, "TP_RUNG_RATCHET_PCT", 0.0)
    e = _armed_exec(sim, monkeypatch)
    oid = _mk(sim, entry=100.0, stop=90.0, stop_prot=105.0)   # already ratcheted high
    e._ratchet_stop_prot(oid, SYM, 100.0, 130.0)              # would imply 100.0
    assert _prot(sim, oid)[0] == 105.0, "ratchet lowered an existing protective stop"


def test_ratchet_refuses_to_rest_at_or_above_the_market(sim, monkeypatch):
    """BELOW THE MARKET. A stop at/above the bid fires on contact — it would close
    the lot the instant it was placed."""
    monkeypatch.setattr(config, "TP_RUNG_RATCHET_ENABLED", True)
    monkeypatch.setattr(config, "TP_RUNG_RATCHET_PCT", 0.0)
    e = _armed_exec(sim, monkeypatch)
    oid = _mk(sim, entry=100.0, stop=90.0)
    e._ratchet_stop_prot(oid, SYM, 100.0, 100.0)     # bid == the level
    assert _prot(sim, oid)[0] is None
    e._ratchet_stop_prot(oid, SYM, 100.0, 99.0)      # bid BELOW the level
    assert _prot(sim, oid)[0] is None
    e._ratchet_stop_prot(oid, SYM, 100.0, 130.0)     # comfortably above -> allowed
    assert _prot(sim, oid)[0] == 100.0


def test_ratchet_never_touches_the_chain_stop(sim, monkeypatch):
    """`stop` is the ladder's inherited floor AND the next rung's sizing denominator.
    Ratcheting it to breakeven reads as 'ladder floor reached' and silently freezes
    accumulation on a pair that is working — which is why stop_prot is a separate
    column at all."""
    monkeypatch.setattr(config, "TP_RUNG_RATCHET_ENABLED", True)
    e = _armed_exec(sim, monkeypatch)
    oid = _mk(sim, entry=100.0, stop=90.0)
    e._ratchet_stop_prot(oid, SYM, 100.0, 130.0)
    prot, chain = _prot(sim, oid)
    assert prot == 100.0 and chain == 90.0, "the chain's invalidation level moved"


def test_min_volume_never_lands_under_either_floor(sim, monkeypatch):
    """Rounding to the lot grid must round UP. Down would place an order Kraken
    rejects for being under ordermin or costmin — silently, every time."""
    e = _armed_exec(sim, monkeypatch)
    for ordermin, costmin, entry, dec in [
            (0.00005, 0.5, 64000.0, 8), (5.0, 0.5, 0.69, 8), (0.1, 10.0, 74.0, 4),
            (1.0, 0.5, 0.0000049, 0), (0.02, 5.0, 213.0, 8)]:
        v = e._min_volume(ordermin, costmin, entry, dec)
        assert v >= ordermin - 1e-12, f"under ordermin: {v} < {ordermin}"
        assert v * entry >= costmin - 1e-9, f"under costmin: {v*entry} < {costmin}"
        assert abs(v * 10**dec - round(v * 10**dec)) < 1e-6, f"{v} off the lot grid"


def test_owns_level_near_is_mode_scoped(sim, monkeypatch):
    """The ladder owns each price level ONCE. If this saw another mode's rows it
    would refuse to ladder a level this book does not actually hold."""
    e = _armed_exec(sim, monkeypatch)
    _mk(sim, entry=100.0)
    assert e._owns_level_near(SYM, 100.5, 0.01) is True
    assert e._owns_level_near(SYM, 120.0, 0.01) is False
    sim.execute("UPDATE orders SET mode='live' WHERE symbol=?", (SYM,)); sim.commit()
    assert e._owns_level_near(SYM, 100.5, 0.01) is False, "saw another mode's position"


# ── reverse gear internals: reachable code now the trigger is 16% ────────────

def test_pick_trim_lot_takes_the_largest_and_is_mode_scoped(sim, monkeypatch):
    """Largest-notional first sheds the most exposure per close, so a trim needs the
    fewest closes and pays the least fee drag. Mode scoping matters because a paper
    book must never pick a live row to shed."""
    e = _armed_exec(sim, monkeypatch)
    for entry, vol, notl in ((100.0, 1.0, 5.0), (100.0, 1.0, 50.0), (100.0, 1.0, 20.0)):
        sim.execute("INSERT INTO orders(ts,symbol,margin_pair,side,ordertype,mode,entry,"
                    "stop,volume,leverage,notional,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("2026-08-05T00:00:00+00:00", SYM, MPAIR, "buy", "limit", "paper",
                     entry, 90.0, vol, 10, notl, "open"))
    sim.commit()
    assert e._pick_trim_lot()["notional"] == 50.0

    # a closed row self-excludes, so a pass walks down the book
    sim.execute("UPDATE orders SET status='closed' WHERE notional=50.0"); sim.commit()
    assert e._pick_trim_lot()["notional"] == 20.0

    # another mode's rows are invisible
    sim.execute("UPDATE orders SET mode='live' WHERE status='open'"); sim.commit()
    assert e._pick_trim_lot() is None, "picked a lot belonging to another book"


def test_close_lot_caps_at_live_volume_and_never_flips_short(sim, monkeypatch):
    """THE no-short invariant. The ledger row can legitimately overstate what is
    actually held (a stop filled while we were down). Closing the ROW's volume would
    sell more than exists and open a short on a long-only book."""
    e = _armed_exec(sim, monkeypatch)
    _now_bar(sim, 100.0)
    _add(90.0)
    _now_bar(sim, 90.0)
    broker.open_positions()                       # a REAL 0.001 position exists
    held = paper_broker._long_open_vol(MPAIR)
    assert held == pytest.approx(0.001)

    oid = _mk(sim, entry=90.0, stop=80.0)         # ledger row claims 1.0 — 1000x reality
    lot = {"id": oid, "symbol": SYM, "margin_pair": MPAIR, "volume": 1.0,
           "leverage": 10, "stop_txid": None, "notional": 90.0}
    assert e._close_lot(lot) is True
    broker.open_positions()                       # settle the market close
    assert paper_broker._long_open_vol(MPAIR) == pytest.approx(0.0)
    assert broker.open_positions() == {}, "a short was opened"
    # Assert on what the EXECUTOR REQUESTED, not on what filled. The simulator has
    # its own long-only guard that caps a sell at the volume actually held, so
    # checking vol_exec would pass even with the executor's cap removed — it would
    # test the simulator's safety net instead of the executor's. `volume` is the
    # order as SENT; against a real exchange that is the number that matters.
    asked = sim.execute("SELECT SUM(volume) FROM paper_orders WHERE type='sell'").fetchone()[0]
    assert asked == pytest.approx(0.001), (
        f"executor asked to sell {asked} holding only {held} — uncapped, this shorts "
        f"on any exchange without its own guard")


def test_close_lot_refuses_to_close_blind(sim, monkeypatch):
    """If live net long cannot be read, closing would be a guess at size. Refuse and
    retry next cycle rather than sell an unknown quantity."""
    e = _armed_exec(sim, monkeypatch)
    monkeypatch.setattr(ex_mod.Executor, "_pair_net_long", lambda self, s: None)
    oid = _mk(sim, entry=90.0, stop=80.0)
    lot = {"id": oid, "symbol": SYM, "margin_pair": MPAIR, "volume": 1.0,
           "leverage": 10, "stop_txid": None, "notional": 90.0}
    assert e._close_lot(lot) is False
    assert sim.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()[0] == "open"
