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
    with execution disabled — a regression dressed as a fix.

    Asserted through app._book_mode — the production mapping — for every mode.
    The first version re-spelled the rule as a constant expression in the test
    body (`"off" if "off" in ("live","paper") else None`), which pins nothing:
    the TUI's realized-P&L closure held its own inline copy, and reverting THAT
    to bare `mode` passed this test untouched. The closure now calls _book_mode
    too, so this single assertion covers both consumers."""
    assert app._book_mode("off") is None
    assert app._book_mode("live") == "live"
    assert app._book_mode("paper") == "paper"
    assert app._book_mode("validate") is None
    assert store.realized_pnl_since(conn, DAY0, "off") == pytest.approx(0.0)
    assert store.realized_pnl_since(conn, DAY0, app._book_mode("off")) == pytest.approx(-13.0)


def test_no_inline_copy_of_the_book_mode_rule_survives():
    """Grep guard: the `in ("live", "paper")` membership test is _book_mode's
    body and must appear exactly once in app.py. A second occurrence is an
    inline re-spelling — the exact shape that made the TUI closure unpinnable."""
    import os
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "deepfield", "app.py")).read()
    assert src.count('in ("live", "paper")') == 1, \
        "the book-mode rule has been re-spelled outside _book_mode"


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


def test_stress_basket_weights_one_book(conn, monkeypatch):
    """_stress_weights feeds the SURVIVABILITY verdict. Weighting a simulated basket
    by real positions makes the stress card describe a book nobody is trading."""
    conn.execute("UPDATE orders SET volume=1, entry=100 WHERE status='open'")
    conn.commit()
    monkeypatch.setattr(config, "EXEC_MODE", "paper")
    assert {s for s, _ in app._stress_weights(conn)} == {"XRP/USD"}
    monkeypatch.setattr(config, "EXEC_MODE", "live")
    assert {s for s, _ in app._stress_weights(conn)} == {"BTC/USD", "ETH/USD"}


def test_by_pair_ledger_shows_one_book(conn, monkeypatch):
    """The deck's per-pair ledger — fills AND resting bids."""
    class _S:
        pairs = {}
    monkeypatch.setattr(config, "EXEC_MODE", "paper")
    bp = app._build_by_pair(conn, _S())
    assert set(bp) == {"XRP/USD"}
    monkeypatch.setattr(config, "EXEC_MODE", "live")
    bp = app._build_by_pair(conn, _S())
    assert set(bp) == {"BTC/USD", "ETH/USD", "SOL/USD"}      # SOL is the resting bid


def test_boot_arm_asks_about_its_own_book(conn):
    """The one with teeth: has_pending_entry gates whether an ENTRY IS PLACED. A
    stale row from the other ledger must not suppress a real order — and the skip
    log would then point at a bid that does not exist in the book being traded."""
    # SOL has a live pending bid and no paper one.
    assert store.has_pending_entry(conn, "SOL/USD", mode="live") is True
    assert store.has_pending_entry(conn, "SOL/USD", mode="paper") is False
    # Unscoped — what ingest.py asked — cannot tell the two apart.
    assert store.has_pending_entry(conn, "SOL/USD") is True


def test_ghost_pairs_are_scoped(conn, monkeypatch, tmp_path):
    """A 'ghost' is an open position the console does not display — a watchdog
    signal. Unscoped, every live symbol reads as a ghost of a paper book."""
    from deepfield.web import server
    # build_health closes the connection it is handed, so it needs a FRESH one per
    # call — reusing the fixture's handle makes the second call fail silently into
    # its own except and return [], which looks exactly like a passing scope test.
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setattr(server, "DISPLAY", ["XRP/USD"])
    monkeypatch.setattr(server, "_ro_conn", lambda: store.connect(path))
    monkeypatch.setattr(server, "_web_live", lambda c: {"mode": "paper", "_fresh": True})
    out = server.build_health()
    assert out["db_ok"] is True, "the query must actually have run"
    assert out["ghost_pairs"] == []
    monkeypatch.setattr(server, "_web_live", lambda c: {"mode": "live", "_fresh": True})
    assert server.build_health()["ghost_pairs"] == ["BTC/USD", "ETH/USD"]


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
