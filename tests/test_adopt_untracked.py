"""Adoption of untracked exchange volume (_adopt_surplus).

Reconcile has detected open volume that no ledger row tracks since the 2026-07-13
M1 audit, but only ever logged and alerted it — PASS 2 arms stops by iterating
`backed`, and untracked volume never enters that list, so a manual position sat
naked indefinitely (operator's HYPE/NEAR buys, 2026-07-22: ~7h unstopped through
a restart). Adoption gives such volume a ledger row so the normal protect path
rests a stop over it.

The whole risk of adopting lives in TELLING APART two causes that look identical
at the reconcile:

  * volume this bot filled but has not booked yet — the fill recovery owns it,
    and adopting it would staple a second row (and later a second stop) onto one
    position: the doubled stop-sell -> naked short that _find_adoptable_stop
    exists to prevent;
  * volume this bot never created — nothing will ever claim it, so alerting
    alone leaves it naked forever.

These tests pin that discrimination. Never touches the network: the DB is
in-memory and the live mark is stubbed.
"""
import time
import sqlite3

import pytest

from deepfield import config, executor as ex_mod

SYM = "NEAR/USD"
MP = "NEARUSD:BTNL"
MARK = 2.00
SURPLUS = 4.0

SCHEMA = """
CREATE TABLE orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, margin_pair TEXT,
    side TEXT, ordertype TEXT, mode TEXT, entry REAL, stop REAL, volume REAL,
    leverage INTEGER, notional REAL, margin REAL, risk_usd REAL, txid TEXT,
    stop_txid TEXT, status TEXT, error TEXT, score INTEGER, required INTEGER,
    userref INTEGER, close_txid TEXT);
CREATE TABLE journal(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT,
    symbol TEXT, text TEXT);
"""


@pytest.fixture(autouse=True)
def _adoption_on(monkeypatch):
    monkeypatch.setattr(config, "ADOPT_UNTRACKED", True)
    monkeypatch.setattr(config, "ADOPT_GRACE_SECS", 1800)


@pytest.fixture
def ex():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    e = ex_mod.Executor(conn)
    e._live_last = lambda s: MARK          # no network
    e._safety = lambda *a, **k: None
    e._journal = lambda *a, **k: None
    return e


def adopted(e):
    return e.conn.execute(
        "SELECT id,entry,stop,volume,status,leverage FROM orders WHERE ordertype='adopted'"
    ).fetchall()


def age_out(e, vol=SURPLUS):
    """Backdate the sighting so the next call is past the grace window."""
    e._surplus_seen[SYM] = (vol, time.time() - config.ADOPT_GRACE_SECS - 1)


# ── provenance: our own in-flight fill must never be adopted ────────────────

def test_pending_row_blocks_adoption(ex):
    """A 'pending' row means one of OUR fills may be in flight — a resting entry,
    or the ambiguous AddOrder recorded pending-with-no-txid. That volume belongs
    to the userref/fill recovery, which will attach it to its real row with the
    real cost basis. Adopting here would double-row (and later double-stop) it."""
    ex.conn.execute("INSERT INTO orders(symbol,status,volume) VALUES(?,'pending',8.0)", (SYM,))
    ex._adopt_surplus("t", SYM, MP, SURPLUS)
    ex._adopt_surplus("t", SYM, MP, SURPLUS)      # still refuses on a later pass
    assert adopted(ex) == []


def test_pending_row_also_holds_the_clock_down(ex):
    """The grace clock must not accrue while a pending row exists, or the surplus
    would adopt the instant the pending row clears — exactly when the fill
    recovery is about to claim it."""
    ex.conn.execute("INSERT INTO orders(symbol,status,volume) VALUES(?,'pending',8.0)", (SYM,))
    ex._adopt_surplus("t", SYM, MP, SURPLUS)
    assert SYM not in ex._surplus_seen


# ── grace window: only a standing, stable surplus is external ──────────────

def test_first_sighting_starts_clock_without_adopting(ex):
    ex._adopt_surplus("t", SYM, MP, SURPLUS)
    assert adopted(ex) == []
    assert SYM in ex._surplus_seen


def test_stable_and_aged_surplus_is_adopted(ex):
    ex._adopt_surplus("t", SYM, MP, SURPLUS)
    age_out(ex)
    ex._adopt_surplus("t", SYM, MP, SURPLUS)
    rows = adopted(ex)
    assert len(rows) == 1
    _id, entry, stop, vol, status, lev = rows[0]
    assert vol == pytest.approx(SURPLUS)
    assert entry == pytest.approx(MARK)         # mark, not a real fill
    assert stop < MARK                          # never rests at/above the market
    assert status == "open"                     # so the protect pass sees it
    assert lev == config.PER_PAIR_LEVERAGE.get(SYM)


def test_adoption_clears_tracking_so_it_cannot_repeat(ex):
    ex._adopt_surplus("t", SYM, MP, SURPLUS)
    age_out(ex)
    ex._adopt_surplus("t", SYM, MP, SURPLUS)
    ex._adopt_surplus("t", SYM, MP, SURPLUS)     # a re-sight restarts, never double-adopts
    assert len(adopted(ex)) == 1


def test_moving_surplus_restarts_the_clock(ex):
    """A changing amount is still settling — only a stable figure is evidence of
    a standing external position."""
    ex._adopt_surplus("t", SYM, MP, SURPLUS)
    age_out(ex)
    ex._adopt_surplus("t", SYM, MP, 6.0)
    assert adopted(ex) == []
    assert ex._surplus_seen[SYM][0] == pytest.approx(6.0)


# ── stop selection ─────────────────────────────────────────────────────────

def test_reuses_sibling_invalidation_level(ex):
    """An adopted lot should stop out WITH the pair's other lots, not at a lone
    level of its own."""
    ex.conn.execute("INSERT INTO orders(symbol,status,stop,volume) VALUES(?,'open',1.822,8.0)", (SYM,))
    ex._adopt_surplus("t", SYM, MP, SURPLUS)
    age_out(ex)
    ex._adopt_surplus("t", SYM, MP, SURPLUS)
    assert adopted(ex)[0][2] == pytest.approx(1.822)


def test_sibling_stop_above_mark_is_not_reused(ex):
    """If the pair is under water its siblings' stop sits ABOVE the mark; resting
    the adopted lot's stop there fires it on contact — an instant unasked-for
    market sell on a position that never entered at that level."""
    ex.conn.execute("INSERT INTO orders(symbol,status,stop,volume) VALUES(?,'open',2.50,8.0)", (SYM,))
    ex._adopt_surplus("t", SYM, MP, SURPLUS)
    age_out(ex)
    ex._adopt_surplus("t", SYM, MP, SURPLUS)
    rows = adopted(ex)
    assert len(rows) == 1
    assert rows[0][2] < MARK


# ── refusals ───────────────────────────────────────────────────────────────

def test_no_live_mark_refuses_rather_than_guessing(ex):
    ex._live_last = lambda s: None
    ex._adopt_surplus("t", SYM, MP, SURPLUS)
    age_out(ex)
    ex._adopt_surplus("t", SYM, MP, SURPLUS)
    assert adopted(ex) == []


def test_kill_switch_disables_adoption(ex, monkeypatch):
    monkeypatch.setattr(config, "ADOPT_UNTRACKED", False)
    ex._adopt_surplus("t", SYM, MP, SURPLUS)
    age_out(ex)
    ex._adopt_surplus("t", SYM, MP, SURPLUS)
    assert adopted(ex) == []
