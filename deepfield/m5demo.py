"""M5 proof demo. SPEC §13 M5: 'Provisional scores updating · simulated
candle-close (injected event) → confirmed recompute → ledger row → cooldown
suppression on repeat.'

Runs against a SCRATCH DB (never the production deepfield.db) so the synthetic
TEST/USD pair never pollutes real state. Every step uses the real ingest/
engine/alerter code paths — nothing here is mocked business logic, only the
WS transport is replaced by directly-constructed events.
"""
import os
import sys
import time
import logging

from . import store
from . import engine
from . import events
from . import ingest as ingest_mod
from .state import AppState
from .profiles import FULL

SYM = "TEST/USD"


def _setup_console_log():
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(name)-14s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))
    root = logging.getLogger()
    root.handlers = [h]
    root.setLevel(logging.INFO)


def _make_buy_series():
    """Deterministic synthetic series designed to fire sig1/2/5/6/7 (denom=7,
    required=5, score=5 -> BUY). Verified by assertion below, not assumed."""
    n = 210
    wc = [200.0 - i * 0.8 for i in range(n)]          # steady decline -> new 52w low, below EMA200
    wc[-4] = wc[-5] - 1.0                              # 3 consecutive lower closes...
    wc[-3] = wc[-4] - 1.0
    wc[-2] = wc[-3] - 1.0
    wc[-1] = wc[-2] + 3.0                              # ...then the up close (sig5)
    wo = [c + 0.5 for c in wc]
    wo[-1] = wc[-1] - 2.0                              # last bar green: open < close (sig6)
    wh = [max(o, c) + 0.5 for o, c in zip(wo, wc)]
    wl = [min(o, c) - 0.5 for o, c in zip(wo, wc)]
    wvol = [100.0] * (n - 1) + [500.0]                 # last-bar volume spike >> SMA20 (sig6)
    # sig7 compares the DAILY close against the WEEKLY-derived 52w low, so the
    # daily series must land near that same level (weekly low here is 33.0 —
    # wc[-4], the deepest point of the down-run) for "near 52w low" to be true.
    dc = [200.0 - i * (167.0 / 399.0) for i in range(400)]
    dc[-1] = dc[-2] + 1.0
    return (wo, wh, wl, wc, wvol), (dc,)


def _seed_history(conn, weekly, daily):
    """Seed all bars EXCEPT the last of each series as closed=1. The last bar
    of each is added later via injected CandleUpdate events (forming), then
    closed via an injected CandleClosed — mirroring the real WS lifecycle."""
    wo, wh, wl, wc, wvol = weekly
    now = int(time.time())
    week_ts0 = now - (len(wc) - 1) * 7 * 86400
    weekly_ts = [week_ts0 + i * 7 * 86400 for i in range(len(wc))]
    for i in range(len(wc) - 1):
        store.upsert_candle(conn, SYM, 10080, weekly_ts[i], wo[i], wh[i], wl[i], wc[i], wvol[i], closed=1)

    (dc,) = daily
    day_ts0 = now - (len(dc) - 1) * 86400
    daily_ts = [day_ts0 + i * 86400 for i in range(len(dc))]
    for i in range(len(dc) - 1):
        store.upsert_candle(conn, SYM, 1440, daily_ts[i], dc[i], dc[i] + 1, dc[i] - 1, dc[i], 100.0, closed=1)
    conn.commit()
    return weekly_ts[-1], daily_ts[-1]


def run_demo(db_path):
    _setup_console_log()
    log = logging.getLogger("deepfield.m5demo")

    if os.path.exists(db_path):
        os.remove(db_path)
    conn = store.connect(db_path)

    weekly, daily = _make_buy_series()
    wo, wh, wl, wc, wvol = weekly
    (dc,) = daily
    last_week_ts, last_day_ts = _seed_history(conn, weekly, daily)
    log.info("seeded %d closed weekly + %d closed daily bars for %s", len(wc) - 1, len(dc) - 1, SYM)

    appstate = AppState()
    ing = ingest_mod.Ingest(conn, appstate, profile=FULL)

    # ── Step 1: forming bars arrive via CandleUpdate (still mid-interval) ───
    ing.handle_candle_update(events.CandleUpdate(SYM, 10080, last_week_ts, wo[-1], wh[-1], wl[-1], wc[-1], wvol[-1]))
    ing.handle_candle_update(events.CandleUpdate(SYM, 1440, last_day_ts, dc[-1], dc[-1] + 1, dc[-1] - 1, dc[-1], 100.0))
    prov = appstate.pair(SYM).provisional
    log.info("PROVISIONAL after forming-bar updates: score=%s/%s status=%s fired=%s",
             prov.score, prov.denom, prov.status, prov.fired)
    assert prov is not None, "provisional recompute did not run"

    # ── Step 2: the weekly bar closes (injected CandleClosed) ──────────────
    log.info(">>> injecting CandleClosed for the weekly bar (simulated close) <<<")
    ing.handle_candle_closed(events.CandleClosed(SYM, 10080, last_week_ts))
    card = appstate.pair(SYM).confirmed
    log.info("CONFIRMED after close #1: score=%s/%s required=%s status=%s fired=%s",
             card.score, card.denom, card.required, card.status, card.fired)
    assert card.status == "BUY", f"demo series did not score BUY: {card.status} {card.score}/{card.denom}"

    n_confirmed = conn.execute("SELECT COUNT(*) FROM alerts WHERE symbol=? AND kind='confirmed'", (SYM,)).fetchone()[0]
    log.info("confirmed alert rows after close #1: %d (expect 1)", n_confirmed)
    assert n_confirmed == 1

    # ── Step 3: the SAME close event fires again (duplicate / replay) ──────
    log.info(">>> injecting the SAME CandleClosed again — expect cooldown suppression <<<")
    ing.handle_candle_closed(events.CandleClosed(SYM, 10080, last_week_ts))

    n_confirmed_2 = conn.execute("SELECT COUNT(*) FROM alerts WHERE symbol=? AND kind='confirmed'", (SYM,)).fetchone()[0]
    log.info("confirmed alert rows after close #2 (repeat): %d (expect still 1 — suppressed)", n_confirmed_2)
    assert n_confirmed_2 == 1, "cooldown failed to suppress the duplicate alert"

    # ── Step 4: full alert-chain exercise (--test-alert equivalent) ────────
    from . import alerter
    result = alerter.test_alert(conn)
    log.info("test-alert chain result: %s", result)

    log.info("=== FULL alerts table ===")
    for row in conn.execute("SELECT id, ts, symbol, price, score, denom, signals, kind FROM alerts ORDER BY id"):
        log.info("  %s", row)

    total = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    log.info("DEMO RESULT: %s (total alert rows=%d, expect 2: 1 confirmed + 1 test)",
             "✅ PASS" if total == 2 else "❌ FAIL", total)
    conn.close()
    return total == 2


if __name__ == "__main__":
    ok = run_demo(sys.argv[1] if len(sys.argv) > 1 else "/tmp/deepfield_m5demo.db")
    sys.exit(0 if ok else 1)
