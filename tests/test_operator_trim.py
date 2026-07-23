"""Operator trim (--trim SYMBOL) — the manual counterpart to the reverse gear.
Reuses the reverse-gear mock harness (self-consistent balance derived from the
open rows) so the assertions are about the trim's SCOPE and safety, not the math:
it must touch exactly one pair, cancel only that pair's bids, honor --lots, stop
on a failed close, and never fire outside live mode.
"""
import pytest

from deepfield import config, store, executor as ex_mod

from .test_reverse_gear import _seed_open_lots, _install_mock_broker


def _mk(conn):
    e = ex_mod.Executor(conn)
    e.mode = "live"
    return e


def _open(conn, sym=None):
    q = "SELECT COUNT(*) FROM orders WHERE status='open'"
    args = ()
    if sym:
        q += " AND symbol=?"
        args = (sym,)
    return conn.execute(q, args).fetchone()[0]


def test_trims_only_the_named_pair(tmp_path, monkeypatch):
    conn = store.connect(str(tmp_path / "t.db"))
    _, vol = _seed_open_lots(conn, n_per_pair=5)          # 3 pairs x 5 lots
    calls = _install_mock_broker(conn, monkeypatch, vol)
    res = _mk(conn).trim_pair("ADA/USD")
    assert res["closed"] == 5 and res["failed"] == 0
    assert _open(conn, "ADA/USD") == 0                    # the whole line is gone
    assert _open(conn) == 10                              # the other two pairs untouched
    assert len(calls["closes"]) == 5
    assert all(p["pair"].startswith("ADA") and p["type"] == "sell" for p in calls["closes"])


def test_lots_cap_sheds_largest_notional_first(tmp_path, monkeypatch):
    conn = store.connect(str(tmp_path / "t.db"))
    _, vol = _seed_open_lots(conn, n_per_pair=5)
    # make two ADA lots clearly the biggest so ordering is observable
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM orders WHERE symbol='ADA/USD' ORDER BY id")]
    conn.execute("UPDATE orders SET notional=900 WHERE id=?", (ids[3],))
    conn.execute("UPDATE orders SET notional=500 WHERE id=?", (ids[1],))
    conn.commit()
    _install_mock_broker(conn, monkeypatch, vol)
    res = _mk(conn).trim_pair("ADA/USD", max_lots=2)
    assert res["closed"] == 2
    assert _open(conn, "ADA/USD") == 3
    still_open = {r[0] for r in conn.execute(
        "SELECT id FROM orders WHERE symbol='ADA/USD' AND status='open'")}
    assert ids[3] not in still_open and ids[1] not in still_open   # the two biggest went


def test_cancels_only_that_pairs_bids(tmp_path, monkeypatch):
    conn = store.connect(str(tmp_path / "t.db"))
    _, vol = _seed_open_lots(conn, n_per_pair=2)
    calls = _install_mock_broker(conn, monkeypatch, vol)
    monkeypatch.setattr(ex_mod.broker, "open_orders", lambda: {
        "BID-ADA": {"descr": {"type": "buy", "pair": "ADAUSD"}},
        "BID-SOL": {"descr": {"type": "buy", "pair": "SOLUSD"}},
        "SELL-ADA": {"descr": {"type": "sell", "pair": "ADAUSD"}},   # a stop — never cancel here
    })
    res = _mk(conn).trim_pair("ADA/USD")
    assert res["bids_canceled"] == 1
    assert "BID-ADA" in calls["cancels"]
    assert "BID-SOL" not in calls["cancels"] and "SELL-ADA" not in calls["cancels"]


def test_stops_on_a_failed_close(tmp_path, monkeypatch):
    conn = store.connect(str(tmp_path / "t.db"))
    _, vol = _seed_open_lots(conn, n_per_pair=5)
    _install_mock_broker(conn, monkeypatch, vol)
    n = {"i": 0}

    def flaky(endpoint, params=None, **kw):
        if endpoint.endswith("AddOrder"):
            n["i"] += 1
            return None if n["i"] == 3 else {"txid": ["OCLOSE-XXXXX-YYYYY"]}
        return {}

    monkeypatch.setattr(ex_mod.broker, "private", flaky)
    res = _mk(conn).trim_pair("ADA/USD")
    assert res["closed"] == 2 and res["failed"] == 1
    assert _open(conn, "ADA/USD") == 3            # stopped selling; rest left intact
    # the failed lot's stop was canceled, so its row must be reprotect-eligible
    naked = conn.execute("SELECT COUNT(*) FROM orders WHERE symbol='ADA/USD' "
                         "AND status='open' AND stop_txid IS NULL").fetchone()[0]
    assert naked == 1


def test_never_flips_short_when_exchange_is_already_flat(tmp_path, monkeypatch):
    conn = store.connect(str(tmp_path / "t.db"))
    _, vol = _seed_open_lots(conn, n_per_pair=3)
    calls = _install_mock_broker(conn, monkeypatch, vol)
    monkeypatch.setattr(ex_mod.broker, "open_positions", lambda: {})   # nothing backs the rows
    res = _mk(conn).trim_pair("ADA/USD")
    assert res["closed"] == 3
    assert calls["closes"] == []                  # rows retired, but NOT ONE sell was sent
    assert _open(conn, "ADA/USD") == 0


def test_refuses_when_net_long_read_fails(tmp_path, monkeypatch):
    conn = store.connect(str(tmp_path / "t.db"))
    _, vol = _seed_open_lots(conn, n_per_pair=3)
    calls = _install_mock_broker(conn, monkeypatch, vol)
    monkeypatch.setattr(ex_mod.broker, "open_positions", lambda: None)  # API failure
    res = _mk(conn).trim_pair("ADA/USD")
    assert res["closed"] == 0 and res["failed"] == 1
    assert calls["closes"] == []                  # never sells blind
    assert _open(conn, "ADA/USD") == 3


def test_not_live_is_a_noop(tmp_path, monkeypatch):
    conn = store.connect(str(tmp_path / "t.db"))
    _, vol = _seed_open_lots(conn, n_per_pair=3)
    calls = _install_mock_broker(conn, monkeypatch, vol)
    e = ex_mod.Executor(conn)
    e.mode = "paper"
    res = e.trim_pair("ADA/USD")
    assert res["closed"] == 0 and calls["closes"] == []
    assert _open(conn, "ADA/USD") == 3


def test_unknown_pair_is_a_noop(tmp_path, monkeypatch):
    conn = store.connect(str(tmp_path / "t.db"))
    _, vol = _seed_open_lots(conn, n_per_pair=3)
    calls = _install_mock_broker(conn, monkeypatch, vol)
    res = _mk(conn).trim_pair("NOPE/USD")
    assert res["closed"] == 0 and res["failed"] == 0 and calls["closes"] == []
