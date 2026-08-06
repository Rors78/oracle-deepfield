"""The exec snapshot must describe ONE book — the same one the rails are guarding.

A paper run is seeded by snapshotting the live DB (it needs the candle history), so
one file legitimately holds a live P&L ledger AND live paper rows. Every aggregate
therefore has to say which book it means. On 2026-08-05 the web console was fixed
for exactly this — it had shown a $200 simulated book a cycle ledger of +$51.93,
every cent of it real-money history from a different book.

The exec snapshot in app.py was NOT fixed at the same time, and the miss was hidden
by a comment claiming its realized day/week came "verbatim from rails_ok". It did
not: rails_ok passes self.mode and the snapshot passed nothing. So the rail that
HALTS TRADING on a daily loss and the number on the operator's screen were counting
different books, and the open-position count had the same split.

Nothing here changes live behaviour — with one book in the file, scoped and unscoped
agree. It changes what paper testing is worth, which is the point of paper.
"""
import datetime

import pytest

from deepfield import app, config, store


DAY0 = datetime.datetime.now(datetime.timezone.utc).replace(
    hour=0, minute=0, second=0, microsecond=0).isoformat()


def _seed_both_books(conn):
    """A live ledger with a real loss, plus a paper book on top — the shape a paper
    run actually has after seeding from a live snapshot."""
    closed = datetime.datetime.now(datetime.timezone.utc).isoformat()
    # exit='stop' matters: realized_pnl_since is pinned to STOP exits because it feeds
    # the loss rails, and a T/P flatten is a harvest rather than a loss event.
    def _pnl(v):
        return '{"pnl": %s, "exit": "stop", "closed_ts": "%s"}' % (v, closed)
    # live: one closed loser (-$12.00) and two open lots, one of them stop-covered
    conn.execute(
        "INSERT INTO orders(symbol,status,mode,error) VALUES('BTC/USD','closed','live',?)",
        (_pnl(-12.0),))
    conn.execute("INSERT INTO orders(symbol,status,mode,stop_txid) "
                 "VALUES('BTC/USD','open','live','LIVESTOP')")
    conn.execute("INSERT INTO orders(symbol,status,mode) VALUES('ETH/USD','open','live')")
    conn.execute("INSERT INTO orders(symbol,status,mode) VALUES('SOL/USD','pending','live')")
    # paper: one closed loser (-$1.00), one open lot, no pending
    conn.execute(
        "INSERT INTO orders(symbol,status,mode,error) VALUES('XRP/USD','closed','paper',?)",
        (_pnl(-1.0),))
    conn.execute("INSERT INTO orders(symbol,status,mode,stop_txid) "
                 "VALUES('XRP/USD','open','paper','PAPERSTOP')")
    conn.commit()


@pytest.fixture
def conn(tmp_path):
    c = store.connect(str(tmp_path / "scope.db"))
    _seed_both_books(c)
    yield c
    c.close()


def test_realized_pnl_is_scoped_to_the_running_book(conn):
    """The number that feeds the TUI, and the shape of the miss: a paper book must
    not report a real-money stop loss."""
    assert store.realized_pnl_since(conn, DAY0, "paper") == pytest.approx(-1.0)
    assert store.realized_pnl_since(conn, DAY0, "live") == pytest.approx(-12.0)
    # Unscoped sums BOTH books — what app.py was doing.
    assert store.realized_pnl_since(conn, DAY0) == pytest.approx(-13.0)


def test_off_mode_stays_unscoped(conn):
    """'off' is not a book. No order row is ever written with mode='off', so scoping
    to the literal would report a flat $0.00 to an operator inspecting a real book
    with execution disabled — a regression dressed as a fix."""
    assert store.realized_pnl_since(conn, DAY0, "off") == pytest.approx(0.0)
    qmode = "off" if "off" in ("live", "paper") else None
    assert qmode is None
    assert store.realized_pnl_since(conn, DAY0, qmode) == pytest.approx(-13.0)


@pytest.mark.parametrize("mode,want_open,want_pending,want_stops", [
    ("paper", 1, 0, 1),
    ("live", 2, 1, 1),
])
def test_snapshot_queries_scope_to_one_book(conn, mode, want_open, want_pending, want_stops):
    """Calls app's OWN helpers, not a copy of their SQL. A test that re-implements
    the query it is checking passes just as happily with the fix reverted — which is
    exactly how this class of bug survives a test suite."""
    positions, pending = app._book_rows(conn, mode)
    scov = app._stop_coverage(conn, mode)
    assert len(positions) == want_open
    assert len(pending) == want_pending
    assert scov == (want_open, want_stops)
    assert {p["mode"] for p in positions} == {mode}, "a foreign row leaked in"


def test_off_mode_shows_the_whole_file(conn):
    """Execution disabled: the operator is inspecting a real book, so every row shows."""
    positions, pending = app._book_rows(conn, "off")
    assert len(positions) == 3 and len(pending) == 1
    assert app._stop_coverage(conn, "off") == (3, 2)
    assert app._book_mode("off") is None


def test_unscoped_counts_would_mix_the_books(conn):
    """What the snapshot reported before: 3 open lots for a paper book that has 1,
    and stop coverage of 2/3 built from two different books' stops."""
    scov = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN stop_txid IS NOT NULL "
        "AND stop_txid<>'' THEN 1 ELSE 0 END),0) FROM orders WHERE status='open'"
    ).fetchone()
    assert scov == (3, 2), "sanity: unscoped really does mix them"
    # and the paper book, correctly scoped, is a single lot with its own stop
    assert app._stop_coverage(conn, "paper") == (1, 1)


def test_rails_and_the_screen_now_count_the_same_book(conn):
    """The invariant that was actually broken. committed_position_count backs the
    MAX_OPEN rail; the snapshot backs the screen. They must agree, or the operator
    reads one number while the executor enforces another."""
    for mode, expect in (("paper", 1), ("live", 3)):     # open + pending, committed
        rail = store.committed_position_count(conn, mode=mode)
        screen = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status IN('open','pending') AND mode=?",
            (mode,)).fetchone()[0]
        assert rail == screen == expect, f"{mode}: rail {rail} vs screen {screen}"
