"""The ladder floor: refuse before the ticker fetch, and narrate it once.

2026-08-07, found by reading 18 hours of live log. XRP's chain reached its floor at
23:23 and then re-derived the IDENTICAL refusal every 607 seconds for 15.5 hours —

    LADDER XRP/USD: next rung @ 1.00064 would hit stop 1.00275 — ladder floor reached

— 60 times, byte for byte, each one preceded by a ticker fetch. Two costs, both real:
a public API call every 10 minutes to reach a foregone conclusion, and 60 INFO lines
that buried every genuine reladder event on the deck. XRP was the only pair affected;
every other chain reladdered once and succeeded.

A chain sits at its floor until one of its lots EXITS. The inputs — the lowest open
fill and the chain's shared stop — cannot drift on their own, so the refusal is not
transient and polling for it changes nothing.

Two properties are pinned here, and the second is the subtle one:

  1. The floor is checked BEFORE the live clamp, because the clamp can only ever
     LOWER the target: a fill-anchored target already under the floor is still under
     it afterward, so the answer is settled without the fetch.

  2. The post-clamp check still exists. A target that CLEARS the pre-gate can be
     pulled onto the floor by the clamp when live price has fallen. Deleting that
     second check on the strength of the first would place a rung at/under the stop
     — the exact thing the floor exists to prevent.
"""
import logging

import pytest

from .conftest import pin_vol
from deepfield import config, executor as ex_mod, store

SYM = "ADA/USD"
MPAIR = "ADAUSD:BTNL"

# Captured at import, BEFORE conftest's autouse fixture inerts it (same idiom as
# test_paper_broker's _REAL_QUERY_ORDERS capture).
_REAL_ENSURE = ex_mod.Executor._ensure_ladder_rungs


@pytest.fixture
def ex(tmp_path, monkeypatch):
    conn = store.connect(str(tmp_path / "floor.db"))
    store.upsert_pair(conn, "ADAUSD", SYM, "ADA", 1.0, 0.5, 8)
    # rung spacing 1% -> a fill at 1.00 proposes 0.99
    pin_vol(conn, tp=4.0, sl=10.0, rung=1.0, symbols=[SYM])
    conn.commit()
    monkeypatch.setattr(config, "LADDER_CONTINUOUS", True)
    monkeypatch.setattr(config, "EXCLUDED_PAIRS", frozenset())
    e = ex_mod.Executor(conn)
    e.mode = "live"
    assert e._armed()
    yield e
    conn.close()


def _let_it_place(monkeypatch):
    """Everything downstream of the floor check, stubbed. Only needed by the tests
    that assert a rung DOES get placed; the refusal tests never reach any of it."""
    monkeypatch.setattr(ex_mod.Executor, "portfolio_value", lambda self: 1000.0)
    monkeypatch.setattr(ex_mod.Executor, "rails_ok", lambda self, eq: (True, ""))
    # third element is the DEBIT CALLABLE invoked once the rung actually rests —
    # not a number (a float stub raises 'float object is not callable' downstream)
    monkeypatch.setattr(ex_mod.Executor, "_respend_budget_ok",
                        lambda self, notional: (True, "", lambda *a, **k: None))
    monkeypatch.setattr(ex_mod.broker, "private",
                        lambda ep, p=None, **k: {"txid": ["ORUNG-1"]})


def _counting_live_last(e, monkeypatch, value):
    """Stub the ticker and count how often the ladder reaches for it."""
    calls = []

    def _ll(self, sym):
        calls.append(sym)
        return value

    monkeypatch.setattr(ex_mod.Executor, "_live_last", _ll)
    return calls


# ── 1. the pre-gate ──────────────────────────────────────────────────────────

def test_floor_refusal_does_not_fetch_a_ticker(ex, monkeypatch):
    """A fill at 1.00 proposes 0.99, which is under a stop of 0.995. Settled before
    the network is touched."""
    calls = _counting_live_last(ex, monkeypatch, 1.00)
    ex._place_ladder_rung(SYM, MPAIR, 10, 0.995, 1.00)
    assert calls == [], f"the ladder fetched a ticker to reach a foregone conclusion: {calls}"
    assert ex.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_a_fundable_rung_still_consults_the_live_price(ex, monkeypatch):
    """Converse control. A pre-gate that swallowed every call would pass the test
    above while disabling the post-only clamp entirely — the 2026-07-12 offline-gap
    incident all over again."""
    calls = _counting_live_last(ex, monkeypatch, 1.00)
    ex._place_ladder_rung(SYM, MPAIR, 10, 0.50, 1.00)     # floor far below -> proceeds
    assert calls, "a rung with room never reached for the live price"


# ── 2. the post-clamp check must survive ─────────────────────────────────────

def test_clamp_can_still_push_a_rung_onto_the_floor(ex, monkeypatch):
    """Clears the pre-gate (0.99 > 0.98 x 1.001), then live has fallen to 0.97 so the
    clamp drags the target to ~0.9699 — under the floor. The second check is what
    catches this; without it the rung rests at/under the stop."""
    _counting_live_last(ex, monkeypatch, 0.97)
    # Everything downstream MUST be funded, or this test passes for the wrong reason:
    # with sizing unmocked the rung is refused for lack of equity and the assertion
    # below holds whether or not the floor check exists. (It did exactly that on the
    # first draft — the mutation caught it.)
    _let_it_place(monkeypatch)
    ex._place_ladder_rung(SYM, MPAIR, 10, 0.98, 1.00)
    assert ex.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0, \
        "a rung was placed at/under the chain stop"


# ── 3. narration is deduplicated, the CHECK is not ───────────────────────────

def test_identical_refusal_narrates_once(ex, monkeypatch, caplog):
    _counting_live_last(ex, monkeypatch, 1.00)
    with caplog.at_level(logging.INFO, logger="deepfield.exec"):
        for _ in range(5):
            ex._place_ladder_rung(SYM, MPAIR, 10, 0.995, 1.00)
    floor = [r for r in caplog.records if "ladder floor reached" in r.getMessage()]
    assert len(floor) == 1, f"expected one INFO line, got {len(floor)}"


def test_a_moved_floor_narrates_again(ex, monkeypatch, caplog):
    """Dedup must key on the NUMBERS, not the symbol. A chain whose stop ratcheted is
    real news; silencing it would hide the thing an operator actually wants to see."""
    _counting_live_last(ex, monkeypatch, 1.00)
    with caplog.at_level(logging.INFO, logger="deepfield.exec"):
        ex._place_ladder_rung(SYM, MPAIR, 10, 0.995, 1.00)
        ex._place_ladder_rung(SYM, MPAIR, 10, 0.995, 1.00)   # identical -> quiet
        ex._place_ladder_rung(SYM, MPAIR, 10, 0.996, 1.00)   # stop moved -> speaks
    floor = [r for r in caplog.records if "ladder floor reached" in r.getMessage()]
    assert len(floor) == 2, f"expected two INFO lines, got {len(floor)}"


def test_successful_rung_narrates_at_info_not_error(ex, monkeypatch, caplog):
    """A successful placement must produce its 'next rung resting' INFO line and
    NO error — pinning the narration tail explicitly, because it sits inside the
    catch-all: bfee1fc (2026-08-08) deleted the ladder's cmult assignment while
    extracting the ceiling helper, and every successful rung then raised
    NameError AFTER insert_order+debit — order fine, money fine, but the INFO
    and journal entry were replaced by a false ERROR traceback, one per rung,
    for a full live boot before log-forensics caught it. No test asserted the
    happy path's own narration; now one does. The conviction variant runs too:
    the lost variable only mattered when a score rode the chain."""
    import logging
    _counting_live_last(ex, monkeypatch, 1.00)
    _let_it_place(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="deepfield.exec"):
        ex._place_ladder_rung(SYM, MPAIR, 10, 0.50, 1.00)
        # clear the resting bid — one pending per symbol, and the second call must
        # actually PLACE for the conviction narration branch to run
        ex.conn.execute("DELETE FROM orders WHERE status='pending'")
        ex.conn.commit()
        ex._place_ladder_rung(SYM, MPAIR, 10, 0.50, 0.90, score=7, required=5)
    msgs = [r for r in caplog.records if "next rung resting" in r.getMessage()]
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(msgs) == 2, f"successful rungs did not narrate: {[r.getMessage() for r in caplog.records]}"
    assert not errs, f"a successful placement logged an error: {[r.getMessage() for r in errs]}"


def test_dedup_survives_the_per_cycle_executor_rebuild(tmp_path, monkeypatch, caplog):
    """THE production shape, which every earlier test in this file missed:
    _poll_fills_threaded builds a FRESH Executor each poll cycle, so per-instance
    dedup state was empty on every call and the dedup never engaged live — 103
    identical ADA INFO lines in one window while this file was green, because
    its fixtures hold ONE instance across calls (log-forensics, 2026-08-10; the
    same convenient-world sin as the PAIRS-tuple fixture). The state is module
    level now; this test constructs a new Executor per refusal, exactly like
    production, and the second identical refusal must be quiet at INFO."""
    import logging
    monkeypatch.setattr(config, "LADDER_CONTINUOUS", True)
    monkeypatch.setattr(config, "EXCLUDED_PAIRS", frozenset())
    conn = store.connect(str(tmp_path / "rebuild.db"))
    store.upsert_pair(conn, "ADAUSD", SYM, "ADA", 1.0, 0.5, 8)
    pin_vol(conn, tp=4.0, sl=10.0, rung=1.0, symbols=[SYM])
    conn.commit()

    def _fresh():
        e = ex_mod.Executor(conn)
        e.mode = "live"
        return e

    monkeypatch.setattr(ex_mod.Executor, "_live_last", lambda self, s: 1.00)
    with caplog.at_level(logging.INFO, logger="deepfield.exec"):
        _fresh()._place_ladder_rung(SYM, MPAIR, 10, 0.995, 1.00)
        _fresh()._place_ladder_rung(SYM, MPAIR, 10, 0.995, 1.00)   # new instance!
    floor = [r for r in caplog.records if "ladder floor reached" in r.getMessage()]
    assert len(floor) == 1, \
        f"the rebuild reset the dedup again — {len(floor)} INFO lines from 2 cycles"


def test_target_jitter_at_the_same_floor_stays_quiet(ex, monkeypatch, caplog):
    """The gap the first live cycle exposed (2026-08-10 11:30): the target is
    clamped to the LIVE price, so it wobbles a tick between retries — and a
    (target, stop) dedup key re-narrated on every wobble. Same stop = same news:
    a moved target under an unmoved floor must stay at DEBUG."""
    import logging
    calls = _counting_live_last(ex, monkeypatch, 1.00)
    with caplog.at_level(logging.INFO, logger="deepfield.exec"):
        ex._place_ladder_rung(SYM, MPAIR, 10, 0.995, 1.00)     # target 0.99
        ex._place_ladder_rung(SYM, MPAIR, 10, 0.995, 1.003)    # target 0.9929 — jitter
    floor = [r for r in caplog.records if "ladder floor reached" in r.getMessage()]
    assert len(floor) == 1, \
        f"target jitter under the same floor re-narrated: {[r.getMessage() for r in floor]}"


def test_floored_chain_reladders_without_announce_or_journal(tmp_path, monkeypatch, caplog):
    """The other half of the ADA noise (2026-08-10): even with the floor line
    deduped, every 600s retry logged 'RELADDER ... re-placing next rung' at INFO
    AND wrote a journal entry onto the deck — an announce for an attempt whose
    refusal was already knowable. _reladder now screens the floor BEFORE the
    announce, through the same _at_ladder_floor predicate the placer uses. A
    floored chain's whole record per cycle is the deduped floor narration:
    first cycle one INFO line, second cycle (fresh Executor, cleared backoff —
    production 600s later) nothing at INFO, zero journal rows, ever."""
    import logging
    monkeypatch.setattr(config, "LADDER_CONTINUOUS", True)
    monkeypatch.setattr(config, "EXCLUDED_PAIRS", frozenset())
    monkeypatch.setattr(ex_mod.Executor, "_ensure_ladder_rungs", _REAL_ENSURE)
    monkeypatch.setattr(ex_mod.Executor, "_live_last", lambda self, s: 1.00)
    conn = store.connect(str(tmp_path / "silent.db"))
    store.upsert_pair(conn, "ADAUSD", SYM, "ADA", 1.0, 0.5, 8)
    pin_vol(conn, tp=4.0, sl=10.0, rung=1.0, symbols=[SYM])
    # an open lot whose next rung (1.00 x 0.99 = 0.99) sits under its stop 0.995
    conn.execute("INSERT INTO orders(ts,symbol,margin_pair,side,ordertype,mode,entry,"
                 "stop,volume,leverage,status,txid,stop_txid) VALUES"
                 "('2026-08-10T00:00:00+00:00',?,?,'buy','limit','live',1.00,0.995,"
                 "100,10,'open','T1','S1')", (SYM, MPAIR))
    conn.commit()

    def _cycle():
        e = ex_mod.Executor(conn)
        e.mode = "live"
        e._ensure_ladder_rungs()

    with caplog.at_level(logging.INFO, logger="deepfield.exec"):
        _cycle()
        ex_mod._reladder_next.clear()          # production: 600s later, fresh instance
        _cycle()
    announce = [r for r in caplog.records if "RELADDER" in r.getMessage()]
    floor = [r for r in caplog.records if "ladder floor reached" in r.getMessage()]
    journal = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    assert announce == [], \
        f"floored chain still announces: {[r.getMessage() for r in announce]}"
    assert len(floor) == 1, f"floor narration not deduped across cycles: {len(floor)}"
    assert journal == 0, f"floored chain wrote {journal} journal rows onto the deck"
    conn.close()


def test_refusal_is_re_evaluated_every_call_not_cached(ex, monkeypatch):
    """Only the NARRATION is deduplicated. A chain that refused once must still be
    able to place a rung the moment its floor allows one — caching the refusal itself
    would freeze accumulation on that pair until a restart."""
    _counting_live_last(ex, monkeypatch, 1.00)
    _let_it_place(monkeypatch)
    ex._place_ladder_rung(SYM, MPAIR, 10, 0.995, 1.00)       # refused
    assert ex.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    ex._place_ladder_rung(SYM, MPAIR, 10, 0.50, 1.00)        # room now -> must proceed
    assert ex.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] >= 1, \
        "the ladder stayed frozen after an earlier floor refusal"
