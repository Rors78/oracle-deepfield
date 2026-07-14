"""Audit 2026-07-13 ingest fixes.

FIX 1 — coincident daily+weekly border (Thu 00:00 UTC): the single fire=True
recompute must see BOTH just-completed bars closed=1 (weekly bars are
epoch-anchored, so every weekly border is also a daily border; the old code
flipped only the first-arriving event's bar, so weekly-driven signals evaluated
a week stale). The ed74968 double-fire dedup must survive: still exactly ONE
fire per close instant per symbol.

FIX 2 — REST-confirm-or-defer at clock closes: a failed REST confirm (outage)
must NOT flip the bar closed or fire the order path on partial WS data; the
watchdog retries. After REST_CONFIRM_DEFER_SECS the close proceeds loudly
anyway (no-blockers: a permanent outage can't silence closes forever).
"""
import time

from deepfield import store, events
from deepfield.ingest import Ingest, REST_CONFIRM_DEFER_SECS
from deepfield.state import AppState
from deepfield.profiles import FULL

SYM = "TEST/USD"

WEEK = 7 * 86400
DAY = 86400


def _last_weekly_border(now):
    """Most recent epoch-anchored weekly border (Thu 00:00 UTC) at or before now."""
    return (int(now) // WEEK) * WEEK


def _seed_coincident_border(conn, border):
    """Closed history ending just before `border`, plus BOTH forming bars that
    complete exactly AT `border` (daily ts=border-DAY, weekly ts=border-WEEK)."""
    for k in range(27, 1, -1):
        store.upsert_candle(conn, SYM, 10080, border - k * WEEK,
                            10.0, 11.0, 9.0, 10.0, 50.0, closed=1)
        store.upsert_candle(conn, SYM, 1440, border - k * DAY,
                            10.0, 11.0, 9.0, 10.0, 50.0, closed=1)
    store.upsert_candle(conn, SYM, 1440, border - DAY,
                        10.0, 10.5, 9.5, 10.2, 40.0, closed=0)
    store.upsert_candle(conn, SYM, 10080, border - WEEK,
                        10.0, 10.9, 9.1, 10.2, 300.0, closed=0)
    conn.commit()


def _closed(conn, interval, ts):
    row = conn.execute(
        "SELECT closed FROM candles WHERE pair=? AND interval=? AND ts=?",
        (SYM, interval, ts)).fetchone()
    return None if row is None else row[0]


def _spy_recompute(ing, conn, border, calls):
    """Wrap _recompute_confirmed to record (fire, daily_closed, weekly_closed)
    AT THE MOMENT the recompute runs — i.e. the exact closed-state the engine's
    series load will see."""
    orig = ing._recompute_confirmed

    def spy(symbol, fire=True):
        calls.append((fire,
                      _closed(conn, 1440, border - DAY),
                      _closed(conn, 10080, border - WEEK)))
        return orig(symbol, fire=fire)

    ing._recompute_confirmed = spy


# ── FIX 1: coincident border ────────────────────────────────────────────────

def test_coincident_border_daily_first_flips_weekly_before_recompute(tmp_path):
    """Watchdog order (INTERVALS iterates 1440 first): the daily CandleClosed
    arrives first, gets fire=True, and must flip the just-completed WEEKLY bar
    too before the recompute — and the weekly event that follows must NOT fire
    again (ed74968 dedup preserved)."""
    conn = store.connect(str(tmp_path / "t.db"))
    border = _last_weekly_border(time.time())
    _seed_coincident_border(conn, border)
    ing = Ingest(conn, AppState(), profile=FULL)
    calls = []
    _spy_recompute(ing, conn, border, calls)

    ing.handle_candle_closed(events.CandleClosed(SYM, 1440, border - DAY))
    assert calls == [(True, 1, 1)], (
        "fire=True recompute must run with BOTH the daily and the just-completed "
        f"weekly bar closed=1, got {calls}")

    ing.handle_candle_closed(events.CandleClosed(SYM, 10080, border - WEEK))
    assert len(calls) == 2
    assert calls[1][0] is False, "second coincident event must NOT fire (dedup)"
    assert sum(1 for c in calls if c[0]) == 1, "exactly ONE fire per close instant"
    conn.close()


def test_coincident_border_weekly_first_flips_daily_symmetrically(tmp_path):
    """Symmetric: if the weekly event happens to arrive first, it must flip the
    just-completed DAILY bar before the recompute; the daily event then dedups."""
    conn = store.connect(str(tmp_path / "t.db"))
    border = _last_weekly_border(time.time())
    _seed_coincident_border(conn, border)
    ing = Ingest(conn, AppState(), profile=FULL)
    calls = []
    _spy_recompute(ing, conn, border, calls)

    ing.handle_candle_closed(events.CandleClosed(SYM, 10080, border - WEEK))
    assert calls == [(True, 1, 1)]

    ing.handle_candle_closed(events.CandleClosed(SYM, 1440, border - DAY))
    assert calls[1][0] is False
    assert sum(1 for c in calls if c[0]) == 1
    conn.close()


def test_non_coincident_daily_close_leaves_forming_weekly_alone(tmp_path):
    """A plain daily border (not Thu 00:00) must NOT flip the forming weekly bar
    — the coincident flip only applies when the close instant is a border of the
    other interval."""
    conn = store.connect(str(tmp_path / "t.db"))
    border = _last_weekly_border(time.time())
    _seed_coincident_border(conn, border)
    # Rebuild the daily forming bar one day later so its close instant
    # (border + DAY) is NOT weekly-aligned. Close the original daily bar first
    # so there's only one forming daily bar.
    store.upsert_candle(conn, SYM, 1440, border - DAY, 10.0, 10.5, 9.5, 10.2, 40.0, closed=1)
    store.upsert_candle(conn, SYM, 1440, border, 10.2, 10.6, 9.9, 10.3, 30.0, closed=0)
    conn.commit()
    ing = Ingest(conn, AppState(), profile=FULL)

    ing.handle_candle_closed(events.CandleClosed(SYM, 1440, border))
    assert _closed(conn, 1440, border) == 1
    assert _closed(conn, 10080, border - WEEK) == 0, (
        "non-coincident daily close must not flip the forming weekly bar")
    conn.close()


def test_missing_other_row_at_coincident_border_does_not_crash(tmp_path):
    """If the other interval's row isn't in the DB yet (gap), the coincident flip
    logs at debug and proceeds — gap-heal covers it, the fire still happens."""
    conn = store.connect(str(tmp_path / "t.db"))
    border = _last_weekly_border(time.time())
    _seed_coincident_border(conn, border)
    conn.execute("DELETE FROM candles WHERE pair=? AND interval=10080 AND ts=?",
                 (SYM, border - WEEK))
    conn.commit()
    ing = Ingest(conn, AppState(), profile=FULL)
    calls = []
    _spy_recompute(ing, conn, border, calls)

    ing.handle_candle_closed(events.CandleClosed(SYM, 1440, border - DAY))
    assert len(calls) == 1 and calls[0][0] is True
    assert calls[0][2] is None  # weekly row absent — fine, reconciler's job
    conn.close()


# ── FIX 2: REST-confirm-or-defer ────────────────────────────────────────────

def _seed_overdue_daily(conn, now):
    """Closed daily/weekly history plus ONE forming daily bar whose border has
    passed (the clock-close case). Returns the forming ts."""
    d0 = (int(now) // DAY) * DAY          # today's daily border (already past)
    for k in range(27, 0, -1):
        store.upsert_candle(conn, SYM, 10080, d0 - k * WEEK,
                            10.0, 11.0, 9.0, 10.0, 50.0, closed=1)
        store.upsert_candle(conn, SYM, 1440, d0 - (k + 1) * DAY,
                            10.0, 11.0, 9.0, 10.0, 50.0, closed=1)
    forming_ts = d0 - DAY                 # closes exactly at d0 <= now
    store.upsert_candle(conn, SYM, 1440, forming_ts,
                        10.0, 10.4, 9.6, 10.1, 20.0, closed=0)
    conn.commit()
    return forming_ts


def test_rest_confirm_failure_defers_no_flip_no_fire(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    now = time.time()
    forming_ts = _seed_overdue_daily(conn, now)
    ing = Ingest(conn, AppState(), profile=FULL)
    fires = []
    orig = ing._recompute_confirmed
    ing._recompute_confirmed = lambda s, fire=True: fires.append(fire) or orig(s, fire=fire)

    ing.apply_rest_confirm(SYM, 1440, forming_ts, None)   # REST outage

    assert _closed(conn, 1440, forming_ts) == 0, "failed confirm must NOT flip the bar"
    assert fires == [], "failed confirm must NOT fire the recompute/order path"
    assert (SYM, 1440, forming_ts) in ing._rest_defer, "deferral must be tracked for retry"

    # A second failed pass within the ceiling: still deferred, first-seen unchanged.
    first_seen = ing._rest_defer[(SYM, 1440, forming_ts)]
    ing.apply_rest_confirm(SYM, 1440, forming_ts, None)
    assert _closed(conn, 1440, forming_ts) == 0 and fires == []
    assert ing._rest_defer[(SYM, 1440, forming_ts)] == first_seen
    conn.close()


def test_rest_confirm_failure_past_ceiling_flips_and_fires_once(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    now = time.time()
    forming_ts = _seed_overdue_daily(conn, now)
    ing = Ingest(conn, AppState(), profile=FULL)
    fires = []
    orig = ing._recompute_confirmed
    ing._recompute_confirmed = lambda s, fire=True: fires.append(fire) or orig(s, fire=fire)

    ing.apply_rest_confirm(SYM, 1440, forming_ts, None)   # first failure — deferred
    assert fires == []
    # Backdate the first-seen so the ceiling is exceeded on the next pass.
    ing._rest_defer[(SYM, 1440, forming_ts)] = time.time() - REST_CONFIRM_DEFER_SECS - 1

    ing.apply_rest_confirm(SYM, 1440, forming_ts, None)   # still failing → ceiling hit

    assert _closed(conn, 1440, forming_ts) == 1, "ceiling hit must flip on clock authority"
    assert fires.count(True) == 1, "ceiling hit must fire exactly once"
    assert (SYM, 1440, forming_ts) not in ing._rest_defer, "entry must be pruned once fired"
    conn.close()


def test_rest_confirm_success_after_deferral_clears_and_fires(tmp_path):
    """Outage recovers before the ceiling: the successful confirm proceeds
    normally (authoritative bar upserted, flip+fire) and drops the deferral."""
    conn = store.connect(str(tmp_path / "t.db"))
    now = time.time()
    forming_ts = _seed_overdue_daily(conn, now)
    ing = Ingest(conn, AppState(), profile=FULL)
    fires = []
    orig = ing._recompute_confirmed
    ing._recompute_confirmed = lambda s, fire=True: fires.append(fire) or orig(s, fire=fire)

    ing.apply_rest_confirm(SYM, 1440, forming_ts, None)   # deferred
    assert fires == []

    rows = [(forming_ts, "10.0", "10.5", "9.5", "10.3", "10.1", "25.0", 100)]
    ing.apply_rest_confirm(SYM, 1440, forming_ts, rows)   # REST back

    assert _closed(conn, 1440, forming_ts) == 1
    assert fires.count(True) == 1
    assert (SYM, 1440, forming_ts) not in ing._rest_defer
    row = conn.execute("SELECT c, v FROM candles WHERE pair=? AND interval=1440 AND ts=?",
                       (SYM, forming_ts)).fetchone()
    assert row == (10.3, 25.0), "REST values are authoritative over the partial WS bar"
    conn.close()


def test_ws_close_during_deferral_prunes_the_entry(tmp_path):
    """A late WS CandleClosed for a bar under deferral closes it via the normal
    path — and must drop the deferral entry (no unbounded growth, no stale retry)."""
    conn = store.connect(str(tmp_path / "t.db"))
    now = time.time()
    forming_ts = _seed_overdue_daily(conn, now)
    ing = Ingest(conn, AppState(), profile=FULL)

    ing.apply_rest_confirm(SYM, 1440, forming_ts, None)   # deferred
    assert (SYM, 1440, forming_ts) in ing._rest_defer

    ing.handle_candle_closed(events.CandleClosed(SYM, 1440, forming_ts))
    assert _closed(conn, 1440, forming_ts) == 1
    assert (SYM, 1440, forming_ts) not in ing._rest_defer
    conn.close()
