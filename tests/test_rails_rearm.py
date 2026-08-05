"""Rails re-arm + ladder-era recalibration (2026-07-30 — operator: "no more off
the rails, do it your way").

Pins the re-arm commit's contract:
  1. RAILS_ENABLED defaults ON, with MAX_OPEN_POSITIONS recalibrated to ladder
     scale (the 07-04 value of 15 would have been a permanent freeze against a
     ~115-committed-row book, with every check behind it short-circuited dead).
  2. The kill-switch peak RESETS at T/P cycle settle — banked profit is off the
     table, so a deliberate flatten must never read as a 20% drawdown.
  3. The kill-switch peak SHIFTS with external deposit/withdrawal flows, same
     ledger truth as tp_baseline — a deposit is not profit, a withdrawal is not
     a drawdown.
  4. The L_eff ceiling (stress_state telemetry) now GATES growth via
     _stack_margin_ok — fail-open on missing/stale, entry-gating only.
  5. USDC/USD (pegged) is out of the seed roster: +4% harvest can never print,
     the 5%-min stop can never trigger, a filled lot is pure rollover bleed.
"""
import json
import time
import types
import datetime

from deepfield import store, config, executor as ex_mod
from deepfield import app as app_mod

# Captured at import time — BEFORE conftest's autouse fixture blanks SEED_PAIRS
# per test, so this is the real shipped roster.
_REAL_SEED_PAIRS = config.SEED_PAIRS

SYM = "BTC/USD"


def _conn(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.upsert_pair(conn, "XXBTZUSD", SYM, "BTC", 0.00005, 0.5, 8)
    return conn


def _exec(conn, mode="paper"):
    e = ex_mod.Executor(conn)
    e.mode = mode
    return e


# ── 1. armed by default, ladder-scale cap ────────────────────────────────────

def test_rails_armed_by_default_at_ladder_scale(tmp_path):
    assert config.RAILS_ENABLED is True
    assert config.MAX_OPEN_POSITIONS >= 150     # ladder era: ~90 open + ~25 pending
    conn = _conn(tmp_path)
    for i in range(120):                        # a normal-shape book clears the cap
        conn.execute("INSERT INTO orders(symbol,status,mode) VALUES(?,'open','live')",
                     (f"S{i}/USD",))
    conn.commit()
    ok, reason = _exec(conn, mode="live").rails_ok(1000.0)
    assert ok, reason
    conn.close()


def test_rails_cap_still_backstops_runaway(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 100)
    for i in range(100):
        conn.execute("INSERT INTO orders(symbol,status,mode) VALUES(?,'open','live')",
                     (f"S{i}/USD",))
    conn.commit()
    ok, reason = _exec(conn, mode="live").rails_ok(1000.0)
    assert not ok and "max open positions" in reason
    conn.close()


# ── 2. T/P settle resets the kill-switch peak ────────────────────────────────

def test_tp_settle_resets_kill_switch_peak(tmp_path, monkeypatch):
    """After a completed flatten, peak_equity == settled equity. Carrying the
    pre-flatten peak (here 500) across the settle would read the deliberate
    flatten as a drawdown and latch the rails through the restack."""
    monkeypatch.setattr(config, "TP_ENABLED", True)
    conn = _conn(tmp_path)
    store.meta_set(conn, "tp_baseline", 100.0)
    store.meta_set(conn, "tp_trough", 100.0)
    store.meta_set(conn, "peak_equity", 500.0)          # stale pre-flatten peak
    e = _exec(conn, mode="live")
    e.portfolio_value = lambda: 130.0                   # >= target 120 -> fire; settle read
    e._flatten_all = lambda: (True, True)               # ran, complete
    assert e._check_take_profit() is True
    assert float(store.meta_get(conn, "peak_equity")) == 130.0
    assert float(store.meta_get(conn, "tp_baseline")) == 130.0
    conn.close()


# ── 3. external flows shift the kill-switch peak ─────────────────────────────

def test_external_flows_shift_kill_switch_peak(tmp_path):
    conn = _conn(tmp_path)
    store.meta_set(conn, "flows_cursor", time.time() - 3600)   # anchored -> polls
    store.meta_set(conn, "tp_baseline", 100.0)
    store.meta_set(conn, "peak_equity", 200.0)
    fake = types.SimpleNamespace(external_flows_since=lambda cur: (40.0, 1, True))
    app_mod._poll_external_flows(conn, fake, datetime.datetime.now(datetime.timezone.utc))
    assert float(store.meta_get(conn, "peak_equity")) == 240.0  # deposit is not profit
    assert float(store.meta_get(conn, "tp_baseline")) == 140.0
    conn.close()


def test_withdrawal_below_zero_clears_peak(tmp_path):
    conn = _conn(tmp_path)
    store.meta_set(conn, "flows_cursor", time.time() - 3600)
    store.meta_set(conn, "tp_baseline", 100.0)
    store.meta_set(conn, "peak_equity", 30.0)
    fake = types.SimpleNamespace(external_flows_since=lambda cur: (-50.0, 1, True))
    app_mod._poll_external_flows(conn, fake, datetime.datetime.now(datetime.timezone.utc))
    # floored to 0 -> _update_peak re-seeds from the next live equity read
    assert float(store.meta_get(conn, "peak_equity")) == 0.0
    conn.close()


# ── 4. L_eff ceiling gates growth ────────────────────────────────────────────

def _stress(conn, lev, ceil, age_s=0.0):
    store.meta_set(conn, "stress_state", json.dumps(
        {"lev_now": lev, "lev_ceiling": ceil, "updated": time.time() - age_s}))


def test_leff_over_ceiling_pauses_growth(tmp_path):
    conn = _conn(tmp_path)
    _stress(conn, 5.0, 4.0)
    ok, why = _exec(conn, mode="live")._stack_margin_ok()
    assert not ok and "ceiling" in why
    conn.close()


def test_leff_under_ceiling_allows_growth(tmp_path):
    conn = _conn(tmp_path)
    _stress(conn, 1.8, 4.0)
    ok, _ = _exec(conn, mode="live")._stack_margin_ok()
    assert ok
    conn.close()


def test_leff_gate_fails_open_on_stale_missing_or_inf(tmp_path):
    conn = _conn(tmp_path)
    e = _exec(conn, mode="live")
    _stress(conn, 5.0, 4.0, age_s=700)                 # stale (> 600s) -> open
    assert e._stack_margin_ok()[0]
    store.meta_set(conn, "stress_state", "")           # missing -> open
    assert e._stack_margin_ok()[0]
    _stress(conn, 5.0, float("inf"))                   # flat book ceiling -> open
    assert e._stack_margin_ok()[0]
    store.meta_set(conn, "stress_state", "not json")   # unreadable -> open
    assert e._stack_margin_ok()[0]
    conn.close()


# ── 5. USDC out of the seed roster ───────────────────────────────────────────

def test_usdc_not_seeded_but_still_a_pair():
    """USDC is pegged, so it can never dip enough to seed a ladder — but it stays in
    the roster for display and signals.

    The count was `len(PAIRS) - 1` when USDC was the ONLY pull. Exclusions are now a
    named list (config.EXCLUDED_PAIRS — five stop-churn pairs joined it 2026-08-05),
    so the invariant is stated against that list rather than a hardcoded one. The
    property being pinned is unchanged: excluded pairs do not seed, and are NOT
    removed from the roster."""
    assert "USDC/USD" not in _REAL_SEED_PAIRS
    assert "USDC/USD" in config.EXCLUDED_PAIRS
    assert len(_REAL_SEED_PAIRS) == len(config.PAIRS) - len(config.EXCLUDED_PAIRS)
    assert any(p["ws"] == "USDC/USD" for p in config.PAIRS)  # display/signals stay
