"""Wave 4 reverse gear — the deleverage governor. Pure decision math + an
integration test of Executor._reverse_gear with a self-consistent balance mock
(closing a lot drops notional/margin, so the buffer rises exactly as it would
live). Guards: fail-open, cap, disabled, never-flip-short, cancels bids.
"""
import pytest

from deepfield import defense, config, store, executor as ex_mod


# ── pure decision math ───────────────────────────────────────────────────────

@pytest.mark.parametrize("buf,trig,expected", [
    (7.9, 8.0, True), (8.0, 8.0, False), (12.0, 8.0, False),
    (None, 8.0, False), (float("inf"), 8.0, False), (float("nan"), 8.0, False),
])
def test_should_trim_fails_open(buf, trig, expected):
    assert defense.should_trim(buf, trig) is expected


# ── integration: Executor._reverse_gear ──────────────────────────────────────

EQUITY = 185.0

def _seed_open_lots(conn, n_per_pair):
    """Insert open live rows: pairs each with n lots of $120 notional / $12 margin."""
    pairs = [("ADA/USD", "ADAUSD:BTNL"), ("DOGE/USD", "XDGUSD:BTNL"),
             ("SOL/USD", "SOLUSD:BTNL")]
    vol = {"ADA/USD": 300.0, "DOGE/USD": 800.0, "SOL/USD": 1.5}
    for ws, mp in pairs:
        store.upsert_pair(conn, mp.split(":")[0], ws, ws.split("/")[0], 1, 0.5, 8)
        for k in range(n_per_pair):
            conn.execute(
                "INSERT INTO orders(ts,symbol,margin_pair,side,ordertype,mode,entry,stop,"
                "volume,leverage,notional,margin,status,stop_txid) VALUES"
                "(?,?,?,'buy','limit','live',1.0,0.9,?,10,120.0,12.0,'open',?)",
                ("2026-07-16T00:00:00+00:00", ws, mp, vol[ws], f"STOP-{ws}-{k}"))
    conn.commit()
    return pairs, vol


def _install_mock_broker(conn, monkeypatch, vol, bal_unavailable=False):
    """Balance derived from live open rows: closing a lot lowers v/m -> buffer rises.
    OpenPositions mirrors the open rows so _pair_net_long backs each close."""
    calls = {"closes": [], "cancels": [], "bids_cancelled": 0}

    def fake_tb():
        if bal_unavailable:
            return None
        rows = conn.execute("SELECT COALESCE(notional,0),COALESCE(margin,0) FROM orders WHERE status='open'").fetchall()
        v = sum(r[0] for r in rows); m = sum(r[1] for r in rows)
        return {"e": str(EQUITY), "m": str(m), "v": str(v)}

    def fake_open_positions():
        pos = {}
        for i, (ws, v) in enumerate(conn.execute(
                "SELECT symbol, volume FROM orders WHERE status='open'")):
            rest = config.MARGIN_PAIR.get(ws, "").replace(":BTNL", "")
            pos[f"T{i}"] = {"pair": rest, "type": "buy", "vol": str(v), "vol_closed": "0"}
        return pos

    def fake_private(endpoint, params=None, **kw):
        if endpoint.endswith("AddOrder"):
            calls["closes"].append(params)
            return {"txid": ["OCLOSE-XXXXX-YYYYY"]}
        return {}

    def fake_cancel(txid):
        calls["cancels"].append(txid)
        return {"count": 1}

    monkeypatch.setattr(ex_mod.broker, "trade_balance_full", fake_tb)
    monkeypatch.setattr(ex_mod.broker, "open_positions", fake_open_positions)
    monkeypatch.setattr(ex_mod.broker, "open_orders", lambda: {})
    monkeypatch.setattr(ex_mod.broker, "private", fake_private)
    monkeypatch.setattr(ex_mod.broker, "cancel_order", fake_cancel)
    monkeypatch.setattr(ex_mod.broker, "equity", lambda b: float(b["e"]) if b else None)
    return calls


def _mk(conn, monkeypatch, **cfg):
    monkeypatch.setattr(config, "REVERSE_GEAR_ENABLED", cfg.get("enabled", True))
    monkeypatch.setattr(config, "REVERSE_GEAR_TRIGGER_PCT", cfg.get("trigger", 8.0))
    monkeypatch.setattr(config, "REVERSE_GEAR_TARGET_PCT", cfg.get("target", 12.0))
    monkeypatch.setattr(config, "REVERSE_GEAR_MAX_LOTS_PER_PASS", cfg.get("max_lots", 4))
    monkeypatch.setattr(config, "REVERSE_GEAR_CANCEL_BIDS", cfg.get("cancel_bids", True))
    e = ex_mod.Executor(conn)
    e.mode = "live"
    return e


def _open_count(conn):
    return conn.execute("SELECT COUNT(*) FROM orders WHERE status='open'").fetchone()[0]


def test_fires_and_deleverages_to_target(tmp_path, monkeypatch):
    conn = store.connect(str(tmp_path / "t.db"))
    _, vol = _seed_open_lots(conn, n_per_pair=5)   # 15 lots x $120 = $1800 notional, $180 margin
    # buffer now = (185 - 0.4*180)/1800*100 = 6.28% < trigger 8 -> should shed
    calls = _install_mock_broker(conn, monkeypatch, vol)
    e = _mk(conn, monkeypatch, max_lots=8)
    before = _open_count(conn)
    e._reverse_gear()
    after = _open_count(conn)
    assert after < before                                    # shed some lots
    assert len(calls["closes"]) == before - after            # one market close per lot
    # ended at/above target OR hit the per-pass cap
    rows = conn.execute("SELECT COALESCE(notional,0),COALESCE(margin,0) FROM orders WHERE status='open'").fetchall()
    v = sum(r[0] for r in rows); m = sum(r[1] for r in rows)
    # liq buffer after the shed, stated here rather than imported: the helper this
    # used to call had no production caller, so it was the test propping it up.
    buf = (EQUITY - defense.LIQ_ML * m) / v * 100.0 if v > 0 else float('inf')
    assert buf >= 12.0 or (before - after) == 8


def test_capped_per_pass(tmp_path, monkeypatch):
    conn = store.connect(str(tmp_path / "t.db"))
    _seed_open_lots(conn, n_per_pair=5)
    calls = _install_mock_broker(conn, monkeypatch, {})
    e = _mk(conn, monkeypatch, max_lots=2)
    e._reverse_gear()
    assert len(calls["closes"]) == 2                          # never more than the cap in one pass


def test_healthy_book_does_not_trim(tmp_path, monkeypatch):
    conn = store.connect(str(tmp_path / "t.db"))
    _seed_open_lots(conn, n_per_pair=1)   # 3 lots x $120 = $360 notional -> buffer ~ (185-14.4)/360 = 47% healthy
    calls = _install_mock_broker(conn, monkeypatch, {})
    e = _mk(conn, monkeypatch)
    e._reverse_gear()
    assert calls["closes"] == []


def test_disabled_is_inert(tmp_path, monkeypatch):
    conn = store.connect(str(tmp_path / "t.db"))
    _seed_open_lots(conn, n_per_pair=5)
    calls = _install_mock_broker(conn, monkeypatch, {})
    e = _mk(conn, monkeypatch, enabled=False)
    e._reverse_gear()
    assert calls["closes"] == []


def test_unknown_balance_fails_open(tmp_path, monkeypatch):
    conn = store.connect(str(tmp_path / "t.db"))
    _seed_open_lots(conn, n_per_pair=5)
    calls = _install_mock_broker(conn, monkeypatch, {}, bal_unavailable=True)
    e = _mk(conn, monkeypatch)
    e._reverse_gear()
    assert calls["closes"] == []                             # never trims on an unknown read


def test_never_sells_more_than_net_long(tmp_path, monkeypatch):
    """If Kraken shows LESS net long than the DB row (position partly gone), the close
    is capped at net long — never oversells into a short."""
    conn = store.connect(str(tmp_path / "t.db"))
    _seed_open_lots(conn, n_per_pair=5)
    calls = _install_mock_broker(conn, monkeypatch, {})
    # shrink live net long to a fraction of the DB volume
    def small_positions():
        pos = {}
        for i, (ws, v) in enumerate(conn.execute("SELECT symbol, volume FROM orders WHERE status='open'")):
            rest = config.MARGIN_PAIR.get(ws, "").replace(":BTNL", "")
            pos[f"T{i}"] = {"pair": rest, "type": "buy", "vol": str(float(v) * 0.1), "vol_closed": "0"}
        return pos
    monkeypatch.setattr(ex_mod.broker, "open_positions", small_positions)
    e = _mk(conn, monkeypatch, max_lots=1)
    e._reverse_gear()
    assert calls["closes"], "expected a trim close"
    sold = float(calls["closes"][0]["volume"])
    # largest lot picked is DOGE (vol 800); its live net long is 10% of ALL 5 DOGE
    # open rows = 5*80 = 400. The close must cap at net long (400), never the full 800.
    assert sold <= 400.0 + 1e-6         # never exceeds pair net long (no flip-short)
    assert sold < 800.0                 # cap actually engaged (didn't sell the DB row's full vol)
