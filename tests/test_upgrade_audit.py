"""Regression tests for the post-M7 audit findings — each test pins one fix.

A1 clock-close fallback (§5b)      A2 live-price alerts (§11)
A3 writer survives poisoned event  A4 tri-state link honesty
A5 provisional cleared on close    A6 countdown monotonic guard
"""
import time
import asyncio
import datetime

from deepfield import store, events, alerter, engine
from deepfield.ingest import Ingest
from deepfield.state import AppState
from deepfield.profiles import FULL

SYM = "TEST/USD"


def _seed_minimal(conn, now, n=25):
    for i in range(n):
        ts = now - (n - i) * 7 * 86400
        store.upsert_candle(conn, SYM, 10080, ts, 10.0, 11.0, 9.0, 10.0, 50.0, closed=1)
    for i in range(n):
        ts = now - (n - i) * 86400
        store.upsert_candle(conn, SYM, 1440, ts, 10.0, 11.0, 9.0, 10.0, 50.0, closed=1)
    conn.commit()


def _fresh_tick(price, ts=None):
    return events.Tick(SYM, last=price, bid=price, ask=price, high24=price,
                       low24=price, volume24=1, vwap24=price, change_pct=0.0,
                       ts=ts if ts is not None else time.time())


# ── A1: clock-close fallback ─────────────────────────────────────────────────

def test_A1_clock_close_detects_overdue_and_rest_confirms(tmp_path, monkeypatch):
    """A forming bar past deadline (+grace) with NO WS close event must be
    found, REST-confirmed with authoritative values, flipped closed, and run
    through the same confirmed-recompute path a WS close would."""
    monkeypatch.setattr("deepfield.ingest.config.PAIRS",
                        [{"rest": "TESTUSD", "ws": SYM, "display": "TEST",
                          "ordermin": 0.1, "costmin": 0.5}])
    conn = store.connect(str(tmp_path / "t.db"))
    now = int(time.time())
    _seed_minimal(conn, now)
    # forming daily bar that closed 100s ago — silent border, no trade since
    overdue_ts = now - 86400 - 100
    store.upsert_candle(conn, SYM, 1440, overdue_ts, 10.0, 10.5, 9.5, 10.2, 42.0, closed=0)
    conn.commit()

    ing = Ingest(conn, AppState(), profile=FULL)
    overdue = ing.find_overdue(now=now)
    assert (SYM, "TESTUSD", 1440, overdue_ts) in overdue

    # REST returns the authoritative closed bar + the new forming bar
    rest_rows = [
        [overdue_ts, "10.0", "10.6", "9.4", "10.3", "10.1", "43.5", 7],   # final values differ from WS
        [overdue_ts + 86400, "10.3", "10.4", "10.2", "10.35", "10.3", "1.0", 1],
    ]
    ing.apply_rest_confirm(SYM, 1440, overdue_ts, rest_rows)

    row = conn.execute("SELECT closed, c, v FROM candles WHERE pair=? AND interval=1440 AND ts=?",
                       (SYM, overdue_ts)).fetchone()
    assert row == (1, 10.3, 43.5)                       # flipped, authoritative values
    forming = store.get_forming(conn, SYM, 1440)
    assert forming and forming["ts"] == overdue_ts + 86400   # new forming bar landed
    assert ing.state.pair(SYM).confirmed is not None          # recompute ran
    conn.close()


def test_A1_not_overdue_within_grace(tmp_path, monkeypatch):
    monkeypatch.setattr("deepfield.ingest.config.PAIRS",
                        [{"rest": "TESTUSD", "ws": SYM, "display": "TEST",
                          "ordermin": 0.1, "costmin": 0.5}])
    conn = store.connect(str(tmp_path / "t.db"))
    now = int(time.time())
    _seed_minimal(conn, now)
    store.upsert_candle(conn, SYM, 1440, now - 3600, 10, 10, 10, 10, 1, closed=0)  # mid-interval
    conn.commit()
    ing = Ingest(conn, AppState(), profile=FULL)
    assert ing.find_overdue(now=now) == []
    conn.close()


# ── A2: alert carries the LIVE price, not the stale close ───────────────────

def test_A2_alert_uses_live_price_when_tick_fresh(tmp_path, monkeypatch):
    conn = store.connect(str(tmp_path / "t.db"))
    now = int(time.time())
    _seed_minimal(conn, now)
    ing = Ingest(conn, AppState(), profile=FULL)
    ing.handle_tick(_fresh_tick(999.0))

    captured = {}
    monkeypatch.setattr(alerter, "fire",
                        lambda conn, sym, price, *a, **kw: captured.update(price=price))

    class Card:
        status = "BUY"; price = 10.0; score = 5; denom = 7; fired = ["x"]

    ing._maybe_alert(SYM, Card(), kind="confirmed")
    assert captured["price"] == 999.0
    conn.close()


def test_A2_alert_falls_back_to_close_when_tick_stale(tmp_path, monkeypatch):
    conn = store.connect(str(tmp_path / "t.db"))
    now = int(time.time())
    _seed_minimal(conn, now)
    ing = Ingest(conn, AppState(), profile=FULL)
    ing.handle_tick(_fresh_tick(999.0, ts=time.time() - 3600))  # an hour old

    captured = {}
    monkeypatch.setattr(alerter, "fire",
                        lambda conn, sym, price, *a, **kw: captured.update(price=price))

    class Card:
        status = "BUY"; price = 10.0; score = 5; denom = 7; fired = ["x"]

    ing._maybe_alert(SYM, Card(), kind="confirmed")
    assert captured["price"] == 10.0
    conn.close()


# ── Finding 2: a duplicate close (WS + clock-close watchdog) fires ONCE ───────

def test_finding2_duplicate_close_does_not_double_fire(tmp_path, monkeypatch):
    """The WS feed and the clock-close watchdog both emit CandleClosed for the same
    bar -> must fire the alert/exec EXACTLY ONCE. Critically, the bar is already
    closed=1 (the clock-close path upserts closed=1 BEFORE calling handle_candle_closed),
    so a fix keyed on flip_closed's rowcount would wrongly fire ZERO times and silently
    suppress orders on quiet pairs. Assert once, not zero."""
    conn = store.connect(str(tmp_path / "t.db"))
    now = int(time.time())
    _seed_minimal(conn, now)
    ing = Ingest(conn, AppState(), profile=FULL)
    ing.handle_tick(_fresh_tick(10.0))          # fresh tick so the alert isn't stale-suppressed

    class Card:
        status = "BUY"; price = 10.0; score = 5; denom = 7; required = 5; fired = ["x"]
    monkeypatch.setattr(engine, "evaluate", lambda *a, **k: Card())
    fires = []
    monkeypatch.setattr(ing, "_maybe_alert", lambda sym, card, kind: fires.append((sym, kind)))

    ts = now - (now % 86400)                     # a daily boundary
    store.upsert_candle(conn, SYM, 1440, ts, 10, 11, 9, 10, 50, closed=1)   # already flipped closed
    conn.commit()
    ev = events.CandleClosed(SYM, 1440, ts)
    ing.handle_candle_closed(ev)                 # first sight of this bar -> fires (even though closed=1)
    ing.handle_candle_closed(ev)                 # same bar again (WS/watchdog dup) -> no re-fire
    assert fires == [(SYM, "confirmed")]         # exactly one — not zero (suppression), not two (dup)

    # a genuinely new bar for the same interval DOES fire again (per-close cadence intact)
    ts2 = ts + 86400
    store.upsert_candle(conn, SYM, 1440, ts2, 10, 11, 9, 10, 50, closed=1)
    conn.commit()
    ing.handle_candle_closed(events.CandleClosed(SYM, 1440, ts2))
    assert fires == [(SYM, "confirmed"), (SYM, "confirmed")]
    conn.close()


# ── Finding 3: reconciler resweep must not fire the boot arm ──────────────────

_TEST_PAIRS = [{"rest": "XTEST", "wsname": "TEST/USD", "ws": "TEST/USD",
                "display": "TEST", "ordermin": 1, "costmin": 0.5}]


class _NotBuy:  status = "HOLD"; price = 10.0; score = 1; denom = 7; required = 5; fired = []
class _Buy:     status = "BUY";  price = 10.0; score = 5; denom = 7; required = 5; fired = ["x"]


def test_finding3_reconciler_resweep_stays_quiet(tmp_path, monkeypatch):
    """A symbol that becomes BUY only via a reconciler resweep (or 'f'-key) must NOT
    fire the one-shot boot arm — that path is documented alert-silent. Only symbols BUY
    at BOOT may arm."""
    conn = store.connect(str(tmp_path / "t.db"))
    _seed_minimal(conn, int(time.time()))
    monkeypatch.setattr("deepfield.ingest.config.PAIRS", _TEST_PAIRS)
    ing = Ingest(conn, AppState(), profile=FULL)
    monkeypatch.setattr(ing, "_recompute_regime", lambda: None)

    monkeypatch.setattr(engine, "evaluate", lambda *a, **k: _NotBuy())
    ing.startup_sweep()                          # boot: nothing BUY -> boot snapshot empty

    fires = []
    monkeypatch.setattr(ing, "_maybe_alert", lambda s, c, kind: fires.append(s))
    monkeypatch.setattr(engine, "evaluate", lambda *a, **k: _Buy())
    ing.startup_sweep()                          # reconciler resweep flips TEST -> BUY
    ing.handle_tick(_fresh_tick(10.0))           # first fresh tick -> arm evaluated
    assert fires == []                           # not BUY at boot -> arm stays quiet
    conn.close()


def test_finding3_boot_time_buy_still_arms(tmp_path, monkeypatch):
    """The legitimate case is preserved: a symbol BUY at boot fires the arm on its
    first fresh tick."""
    conn = store.connect(str(tmp_path / "t.db"))
    _seed_minimal(conn, int(time.time()))
    monkeypatch.setattr("deepfield.ingest.config.PAIRS", _TEST_PAIRS)
    ing = Ingest(conn, AppState(), profile=FULL)
    monkeypatch.setattr(ing, "_recompute_regime", lambda: None)
    monkeypatch.setattr(engine, "evaluate", lambda *a, **k: _Buy())
    ing.startup_sweep()                          # boot: TEST is BUY -> in boot snapshot

    fires = []
    monkeypatch.setattr(ing, "_maybe_alert", lambda s, c, kind: fires.append(s))
    ing.handle_tick(_fresh_tick(10.0))           # first fresh tick -> arm fires
    assert fires == ["TEST/USD"]
    conn.close()


# ── A3: the single writer must never die ─────────────────────────────────────

def test_A3_writer_survives_poisoned_event(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    now = int(time.time())
    _seed_minimal(conn, now)

    async def main():
        ing = Ingest(conn, AppState(), profile=FULL)
        ing.handle_tick = lambda ev: (_ for _ in ()).throw(RuntimeError("poison"))
        q = asyncio.Queue()
        task = asyncio.ensure_future(ing.run(q))
        await q.put(_fresh_tick(1.0))                                   # poisoned
        await q.put(events.CandleUpdate(SYM, 1440, now, 10, 11, 9, 10.5, 5.0))  # must still process
        await asyncio.sleep(0.1)
        assert not task.done(), "writer task died on a poisoned event"
        row = conn.execute("SELECT c FROM candles WHERE pair=? AND interval=1440 AND ts=?",
                           (SYM, now)).fetchone()
        assert row is not None and row[0] == 10.5
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(main())
    conn.close()


# ── A4: link status is per-connection, tri-state ─────────────────────────────

def test_A4_link_tristate_partial_outage():
    st = AppState()
    ing = Ingest.__new__(Ingest)   # no DB needed for link handlers
    ing.state = st
    ing.handle_link_up(events.LinkUp(0, "A"))
    ing.handle_link_up(events.LinkUp(0, "B"))
    assert st.link_status() == "UP" and st.link_up is True
    ing.handle_link_down(events.LinkDown("dns", "B"))
    assert st.link_status() == "PARTIAL" and st.link_up is False   # honest: not fully up
    ing.handle_link_down(events.LinkDown("dns", "A"))
    assert st.link_status() == "DOWN"
    ing.handle_link_up(events.LinkUp(1, "A"))
    ing.handle_link_up(events.LinkUp(1, "B"))
    assert st.link_status() == "UP" and st.total_reconnects() == 2


# ── A5: provisional cleared on close (no just-closed-bar double count) ───────

def test_A5_provisional_cleared_on_close(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    now = int(time.time())
    _seed_minimal(conn, now)
    ing = Ingest(conn, AppState(), profile=FULL)
    ing.handle_candle_update(events.CandleUpdate(SYM, 10080, now, 10, 11, 9, 10.5, 60.0))
    ing.handle_candle_update(events.CandleUpdate(SYM, 1440, now, 10, 10.6, 9.8, 10.4, 55.0))
    assert ing.state.pair(SYM).provisional is not None
    ing.handle_candle_closed(events.CandleClosed(SYM, 10080, now))
    assert ing.state.pair(SYM).provisional is None   # honest: unknown until fresh forming data
    conn.close()


# ── A6: countdown never regresses on a late old-bar update ───────────────────

def test_A6_countdown_monotonic(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    now = int(time.time())
    _seed_minimal(conn, now)
    ing = Ingest(conn, AppState(), profile=FULL)
    ing.handle_candle_update(events.CandleUpdate(SYM, 1440, now, 10, 11, 9, 10, 1))
    assert ing.state.daily_interval_begin == now
    ing.handle_candle_update(events.CandleUpdate(SYM, 1440, now - 86400, 10, 11, 9, 10, 1))
    assert ing.state.daily_interval_begin == now      # stale update ignored
    conn.close()
