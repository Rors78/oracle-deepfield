"""Web console server — 2026-07-13 audit remediation (display layer).

Covers the server side of W1 (freshness-gated prices + data_stale/data_age_s),
W2 (real /api/health), W3 (per-fill stop + stop_txid), W4 (per-fill stop arrays
the swept math reads), W5 (recon per_pair + dated stamp), W6 (HOLD vs BUY),
W7 (per-pair STALE from tick_ages), W10 (ghost pairs), W12 (per-pair lev
default) — and the TUI side of W11 (no lower bound on proximity; BELOW STOP).
"""
import json
import os
import time

import pytest

from deepfield import config, store, ui
from deepfield.web import server


NOW = None  # set per-test


def _mkdb(tmp_path, monkeypatch, blob=None, orders=(), candles=(), meta=()):
    db = str(tmp_path / "web.db")
    # Start each build clean: tests call _mkdb several times on the same tmp_path,
    # and a reused file duplicates candle rows (UNIQUE pair,interval,ts) and keeps a
    # prior web_live blob a blob=None call means to clear. Drop the WAL sidecars too.
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except FileNotFoundError:
            pass
    conn = store.connect(db)
    for pair, interval, ts, c in candles:
        conn.execute(
            "INSERT INTO candles(pair,interval,ts,o,h,l,c,v,closed) "
            "VALUES(?,?,?,?,?,?,?,?,1)", (pair, interval, ts, c, c, c, c, 1.0))
    for row in orders:
        cols = ", ".join(row)
        conn.execute(f"INSERT INTO orders({cols}) VALUES({','.join('?'*len(row))})",
                     tuple(row.values()))
    if blob is not None:
        store.meta_set(conn, "web_live", json.dumps(blob))
    for k, v in meta:
        store.meta_set(conn, k, v)
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", db)
    return db


def _order(sym, entry, stop, stop_txid, vol=20.0, lev=10, status="open"):
    return {"ts": "2026-07-13T01:00:00+00:00", "symbol": sym, "side": "buy",
            "mode": "live", "entry": entry, "stop": stop, "volume": vol,
            "leverage": lev, "notional": entry * vol, "margin": entry * vol / lev,
            "score": 5, "required": 5, "txid": "T", "stop_txid": stop_txid,
            "status": status}


def _blob(now, age=0.0, **kw):
    d = {"equity": 100.0, "margin_used": 10.0, "free_margin": 90.0,
         "margin_level": 300, "capacity": 50, "links": [True, True],
         "mode": "live", "started": now - 5000, "updated": now - age,
         "prices": {"ADA/USD": 0.30}, "chg": {"ADA/USD": 99.9}}
    d.update(kw)
    return d


def _candles_ada(now):
    day = 86400
    t0 = int(now // day * day)
    return [("ADA/USD", 1440, t0 - 2 * day, 0.20), ("ADA/USD", 1440, t0 - day, 0.22)]


def _state(tmp_path, monkeypatch, **kw):
    _mkdb(tmp_path, monkeypatch, **kw)
    conn = server._ro_conn()
    try:
        return server._assemble(conn)
    finally:
        conn.close()


def _pair(st, sym):
    return next(p for p in st["pairs"] if p["sym"] == sym)


# ── W1: freshness-gate prices ─────────────────────────────────────────────────

def test_fresh_blob_serves_live_prices_and_age(tmp_path, monkeypatch):
    now = time.time()
    st = _state(tmp_path, monkeypatch, blob=_blob(now), candles=_candles_ada(now))
    assert st["data_stale"] is False
    assert st["data_age_s"] is not None and st["data_age_s"] < 5
    ada = _pair(st, "ADA")
    assert ada["price"] == 0.30 and ada["chg"] == 99.9


def test_stale_blob_falls_back_to_daily_close_and_flags(tmp_path, monkeypatch):
    """W1: a frozen blob must NOT serve frozen prices/chg as current."""
    now = time.time()
    st = _state(tmp_path, monkeypatch, blob=_blob(now, age=300),
                candles=_candles_ada(now))
    assert st["data_stale"] is True
    assert st["data_age_s"] == pytest.approx(300, abs=5)
    ada = _pair(st, "ADA")
    assert ada["price"] == 0.22                      # last daily close, not 0.30
    assert ada["chg"] != 99.9                        # frozen blob chg never served
    assert ada["chg"] == pytest.approx(10.0, abs=0.1)  # close-derived fallback
    assert st["health"]["equity"] is None            # existing gate still holds


def test_no_blob_at_all_is_stale_with_null_age(tmp_path, monkeypatch):
    now = time.time()
    st = _state(tmp_path, monkeypatch, blob=None, candles=_candles_ada(now))
    assert st["data_stale"] is True and st["data_age_s"] is None


# ── W2: real /api/health ──────────────────────────────────────────────────────

def test_health_ok_only_when_fresh_links_and_db(tmp_path, monkeypatch):
    now = time.time()
    _mkdb(tmp_path, monkeypatch, blob=_blob(now))
    h = server.build_health()
    assert h["ok"] is True and h["db_ok"] is True
    assert h["links"] == [True, True]
    assert h["data_age_s"] < 5


def test_health_fails_closed_on_stale_blob_or_down_link(tmp_path, monkeypatch):
    now = time.time()
    _mkdb(tmp_path, monkeypatch, blob=_blob(now, age=200))
    h = server.build_health()
    assert h["ok"] is False and h["db_ok"] is True    # DB fine, data frozen
    _mkdb(tmp_path, monkeypatch, blob=_blob(now, links=[True, False]))
    assert server.build_health()["ok"] is False       # a link down
    _mkdb(tmp_path, monkeypatch, blob=None)
    h = server.build_health()
    assert h["ok"] is False and h["data_age_s"] is None


def test_health_never_raises_on_broken_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "missing-dir" / "no.db"))
    h = server.build_health()
    assert h["ok"] is False and h["db_ok"] is False and "error" in h


# ── W3: /api/pair ships per-fill stop + stop_txid ─────────────────────────────

def test_pair_payload_ships_stop_and_stop_txid(tmp_path, monkeypatch):
    now = time.time()
    _mkdb(tmp_path, monkeypatch, blob=_blob(now), candles=_candles_ada(now),
          orders=[_order("ADA/USD", 0.174, 0.14821, "TXPROT"),
                  _order("ADA/USD", 0.166, 0.1475, "")])
    d = server.build_pair("ADA")
    assert len(d["fills"]) == 2
    assert d["fills"][0]["stop"] == 0.14821 and d["fills"][0]["stop_txid"] == "TXPROT"
    assert d["fills"][1]["stop"] == 0.1475 and d["fills"][1]["stop_txid"] == ""


# ── W4: per-fill stop arrays for the swept math ───────────────────────────────

def test_state_ships_divergent_per_fill_stops_and_protection(tmp_path, monkeypatch):
    now = time.time()
    st = _state(tmp_path, monkeypatch, blob=_blob(now), candles=_candles_ada(now),
                orders=[_order("ADA/USD", 0.174, 0.14821, "TXPROT"),
                        _order("ADA/USD", 0.166, 0.1475, "")])
    ada = _pair(st, "ADA")
    assert ada["fstops"] == [0.14821, 0.1475]        # per-chain stops DIVERGE
    assert ada["prot"] == [True, False]              # per-fill protection truth
    assert ada["stops"] == 1 and ada["fills"] == 2   # count feeds the coherence badge
    # (A swept-figure assert that compared a sum to itself re-spelled was deleted
    # 2026-08-08 — the payload assertions above are the real pins.)


# ── W6: HOLD vs BUY ───────────────────────────────────────────────────────────

def test_position_without_live_buy_reads_hold(tmp_path, monkeypatch):
    now = time.time()
    st = _state(tmp_path, monkeypatch, blob=_blob(now), candles=_candles_ada(now),
                orders=[_order("ADA/USD", 0.174, 0.14821, "TX")])
    ada = _pair(st, "ADA")                           # no weekly candles -> no card -> not a live BUY
    assert ada["status"] == "HOLD" and ada["stStyle"] == "hold"
    assert ada["tier"] == "active"


def test_below_stop_is_the_loudest_state(tmp_path, monkeypatch):
    now = time.time()
    blob = _blob(now, prices={"ADA/USD": 0.10})      # price UNDER both stops
    st = _state(tmp_path, monkeypatch, blob=blob, candles=_candles_ada(now),
                orders=[_order("ADA/USD", 0.174, 0.14821, "TX")])
    ada = _pair(st, "ADA")
    assert ada["status"] == "BELOW-STOP" and ada["fault"] is True


# ── W7: per-pair STALE from tick_ages ─────────────────────────────────────────

def test_tick_ages_drive_stale_status_with_position(tmp_path, monkeypatch):
    now = time.time()
    blob = _blob(now, tick_ages={"ADA/USD": 500.0})
    st = _state(tmp_path, monkeypatch, blob=blob, candles=_candles_ada(now),
                orders=[_order("ADA/USD", 0.174, 0.14821, "TX")])
    ada = _pair(st, "ADA")
    assert ada["status"] == "STALE" and ada["fault"] is True   # money on stale data = fault
    assert ada["tier"] == "active"


def test_tick_ages_absent_degrades_silently(tmp_path, monkeypatch):
    now = time.time()
    st = _state(tmp_path, monkeypatch, blob=_blob(now), candles=_candles_ada(now),
                orders=[_order("ADA/USD", 0.174, 0.14821, "TX")])
    assert _pair(st, "ADA")["status"] == "HOLD"      # old blob: no stale branch, no crash


def test_stale_without_position_keeps_tier(tmp_path, monkeypatch):
    now = time.time()
    blob = _blob(now, tick_ages={"ADA/USD": 500.0})
    st = _state(tmp_path, monkeypatch, blob=blob, candles=_candles_ada(now))
    ada = _pair(st, "ADA")
    assert ada["status"] == "STALE" and ada["fault"] is False
    assert ada["tier"] in ("watch", "idle")          # not promoted without money on it


# ── W10: ghost pairs ──────────────────────────────────────────────────────────

def test_ghost_pairs_surface_in_state_and_health(tmp_path, monkeypatch):
    now = time.time()
    # NB: use a pair that can never be in the roster — XMR rejoined it in the
    # 2026-07-19 full-universe expansion, which un-ghosted the old fixture.
    st = _state(tmp_path, monkeypatch, blob=_blob(now), candles=_candles_ada(now),
                orders=[_order("GHOST/USD", 150.0, 120.0, "TX")])   # non-roster pair, open row
    assert st["health"]["ghost_pairs"] == ["GHOST/USD"]
    assert st["health"]["pos_count"] == 1            # counted — and now also NAMED
    h = server.build_health()
    assert h["ghost_pairs"] == ["GHOST/USD"]


# ── W5: recon per_pair + dated stamp ──────────────────────────────────────────

def test_recon_passes_per_pair_and_dated_time(tmp_path, monkeypatch):
    now = time.time()
    recon = {"ts": "2026-07-10T09:14:54+00:00", "all_ok": False,
             "per_pair": {"ADA/USD": {"rows": 23, "vol": 460.0, "stops": 22, "ok": False}}}
    st = _state(tmp_path, monkeypatch, blob=_blob(now), candles=_candles_ada(now),
                meta=[("last_recon", json.dumps(recon))])
    h = st["health"]
    assert h["recon_ok"] is False
    assert h["recon_per_pair"]["ADA/USD"]["stops"] == 22
    assert "07-10" in h["recon_time"]                # date, not just HH:MM


# ── W12: leverage default from config, not a hardcoded 10 ─────────────────────

def test_lev_default_is_per_pair_max(tmp_path, monkeypatch):
    now = time.time()
    st = _state(tmp_path, monkeypatch, blob=_blob(now), candles=_candles_ada(now))
    assert _pair(st, "CRV")["lev"] == config.PER_PAIR_LEVERAGE["CRV/USD"] == 5
    assert _pair(st, "BTC")["lev"] == 10
    st2 = _state(tmp_path, monkeypatch, blob=_blob(now), candles=_candles_ada(now),
                 orders=[_order("CRV/USD", 0.5, 0.4, "TX", lev=5)])
    assert _pair(st2, "CRV")["lev"] == 5             # fills keep the real order lev


# ── a bid with no fill behind it is a whole symbol — and must not 500 the deck ─

def test_pending_only_symbol_assembles(tmp_path, monkeypatch):
    """A pair whose last fill closed still has a resting bid. The ladder row it
    builds has no fills, so every counter the payload reads must still exist —
    a missing 'harvesting' key 500s /api/state for the ENTIRE deck, not one pair.
    """
    now = time.time()
    st = _state(tmp_path, monkeypatch, blob=_blob(now), candles=_candles_ada(now),
                orders=[_order("SHIB/USD", 4.59e-06, None, "", vol=770000.0,
                               status="pending")])
    shib = _pair(st, "SHIB")
    assert shib["harv"] == 0 and shib["stops"] == 0   # no fills → nothing to protect
    assert shib["bid"] == 4.59e-06                    # the bid itself still shows


# ── W11 (TUI): proximity has no lower bound; BELOW STOP is explicit ───────────

def _v(price, stop, fills=True):
    return {"ps": None, "card": None, "price": price, "stop": stop,
            "prox": ui._proximity(price, stop), "stale": False,
            "is_buy": False, "has_fills": fills}


def test_proximity_no_lower_bound():
    assert ui._proximity(1.02, 1.00) is True         # within 3% above
    assert ui._proximity(0.995, 1.00) is True        # just below
    assert ui._proximity(0.90, 1.00) is True         # >1% BELOW — the true emergency
    assert ui._proximity(1.04, 1.00) is False        # comfortably above


def test_strip_state_below_stop_is_loudest():
    label, style = ui._strip_state(_v(0.90, 1.00), time.time())
    assert "BELOW STOP" in label and "bold" in style
    label, _ = ui._strip_state(_v(1.02, 1.00), time.time())
    assert label == "NEAR-STOP"


# ── deck mk II: sparklines, interval candles, equity windows, skim series ─────

def test_state_ships_15m_sparkline(tmp_path, monkeypatch):
    now = time.time()
    t0 = int(now) - 3600
    spark_candles = [("ADA/USD", 15, t0 + i * 900, 0.29 + i * 0.001) for i in range(4)]
    st = _state(tmp_path, monkeypatch, blob=_blob(now),
                candles=_candles_ada(now) + spark_candles)
    ada = _pair(st, "ADA")
    assert ada["spark"] == [0.29, 0.291, 0.292, 0.293]


def test_state_spark_empty_when_no_15m_history(tmp_path, monkeypatch):
    now = time.time()
    st = _state(tmp_path, monkeypatch, blob=_blob(now), candles=_candles_ada(now))
    assert _pair(st, "ADA")["spark"] == []


def test_build_pair_intraday_interval(tmp_path, monkeypatch):
    now = time.time()
    t0 = int(now) - 7200
    hour_candles = [("ADA/USD", 60, t0 + i * 3600, 0.30 + i * 0.01) for i in range(2)]
    _mkdb(tmp_path, monkeypatch, blob=_blob(now),
          candles=_candles_ada(now) + hour_candles)
    d = server.build_pair("ADA", iv=60)
    assert d["iv"] == 60 and len(d["days"]) == 2
    assert d["closes"] == [0.30, 0.31]
    # bogus interval falls back to daily, never 500s
    d = server.build_pair("ADA", iv=7)
    assert d["iv"] == 1440 and len(d["days"]) == 2


def test_build_equity_windows(tmp_path, monkeypatch):
    import sqlite3 as _sq
    now = int(time.time())
    db = _mkdb(tmp_path, monkeypatch, blob=_blob(now), candles=_candles_ada(now))
    conn = _sq.connect(db)
    conn.execute("INSERT INTO equity_history(ts,equity) VALUES(?,?)", (now - 3 * 86400, 150.0))
    conn.execute("INSERT INTO equity_history(ts,equity) VALUES(?,?)", (now - 600, 180.0))
    conn.commit(); conn.close()
    assert server.build_equity(24) == [[now - 600, 180.0]]
    assert server.build_equity(168) == [[now - 3 * 86400, 150.0], [now - 600, 180.0]]
    assert server.build_equity(0) == [[now - 3 * 86400, 150.0], [now - 600, 180.0]]


def _tp_rung_closed(sym, pnl, closed_iso, entry=0.20, vol=20.0):
    return {"ts": "2026-07-29T01:00:00+00:00", "symbol": sym, "side": "buy",
            "mode": "live", "entry": entry, "stop": 0.1, "volume": vol,
            "leverage": 3, "notional": entry * vol, "margin": entry * vol / 3,
            "score": 5, "required": 5, "txid": "T", "status": "closed",
            "error": json.dumps({"exit": "tp-rung", "pnl": pnl,
                                 "closed_ts": closed_iso})}


def test_rung_harvest_series_and_leaderboard(tmp_path, monkeypatch):
    now = time.time()
    st = _state(tmp_path, monkeypatch, blob=_blob(now), candles=_candles_ada(now),
                orders=[_tp_rung_closed("ADA/USD", 0.12, "2026-07-29T10:00:00+00:00"),
                        _tp_rung_closed("ADA/USD", 0.10, "2026-07-30T11:00:00+00:00"),
                        _tp_rung_closed("SHIB/USD", 0.50, "2026-07-30T12:00:00+00:00")])
    rh = st["rung_harvest"]
    # series is oldest-first, [ts, pnl, sym]
    assert [x[2] for x in rh["series"]] == ["ADA", "ADA", "SHIB"]
    assert rh["series"][0][1] == 0.12
    assert rh["series"][0][0] < rh["series"][1][0] < rh["series"][2][0]
    # leaderboard is total-desc: SHIB 0.50 beats ADA 0.22
    assert rh["by_sym"][0] == {"sym": "SHIB", "total": 0.50, "n": 1}
    assert rh["by_sym"][1] == {"sym": "ADA", "total": 0.22, "n": 2}


def test_pair_candles_keep_micro_price_resolution(tmp_path, monkeypatch):
    """Decimal-place rounding quantized SHIB/PEPE-class candles onto the 1e-06
    grid (a 4.69e-06 close became 5e-06; 90 days of candles collapsed to 4
    distinct values). Significant-digit rounding must preserve the shape."""
    now = time.time()
    day = 86400
    t0 = int(now // day * day)
    micro = [("SHIB/USD", 1440, t0 - 2 * day, 4.69e-06),
             ("SHIB/USD", 1440, t0 - day, 4.72e-06)]
    _mkdb(tmp_path, monkeypatch, blob=_blob(now), candles=micro)
    d = server.build_pair("SHIB")
    assert d["closes"] == [4.69e-06, 4.72e-06]     # NOT [5e-06, 5e-06]
    spark15 = [("SHIB/USD", 15, int(now) - 1800, 4.691e-06),
               ("SHIB/USD", 15, int(now) - 900, 4.712e-06)]
    _mkdb(tmp_path, monkeypatch, blob=_blob(now), candles=micro + spark15)
    conn = server._ro_conn()
    try:
        st = server._assemble(conn)
    finally:
        conn.close()
    shib = next(p for p in st["pairs"] if p["sym"] == "SHIB")
    assert shib["spark"] == [4.691e-06, 4.712e-06]
