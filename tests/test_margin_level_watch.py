"""Margin-level watch (app._check_margin_level).

MARGIN_LEVEL_ALERT_PCT was written by audit 2026-07-13 #2 and then read by no code
at all, so its value was never pressure-tested against a real book. Wiring it
surfaced that 150 was the ratio twin of an all-pairs-10:1 stack: at the current 2-5x
mix the book runs at ml 130-199 in ordinary operation while the price-space liq
buffer reads NOMINAL, so 150 would have paged continuously and burned the channel.

These tests pin the two things that make the watch worth having: it is SILENT
through the observed normal band, and it FIRES through the band of the 2026-07-16
near-margin-call. They also pin fail-open on a bad read, since a watch that pages on
a garbage margin level is worse than no watch.

No network: the alerter is monkeypatched and the DB is in-memory.
"""
import sqlite3

import pytest

from deepfield import app, config, defense

SCHEMA = """
CREATE TABLE journal(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT,
    symbol TEXT, text TEXT);
"""


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    return c


@pytest.fixture(autouse=True)
def _reset_ratchet(monkeypatch):
    """The watch ratchets on the worst bucket paged since the last recovery above the
    floor (2026-07-24), which is module state by design — it has to outlive a poll
    cycle. Reset it per test so cases don't silence each other."""
    monkeypatch.setattr(app, "_ml_worst_bucket", None)


@pytest.fixture
def fired(monkeypatch):
    """Capture fire_safety calls as (kind, throttle_key, message, loud)."""
    calls = []
    monkeypatch.setattr(app.alerter, "fire_safety",
                        lambda kind, sym, msg, loud=None: calls.append((kind, sym, msg, loud)))
    monkeypatch.setattr(config, "MARGIN_LEVEL_ALERT_PCT", 120)
    return calls


def bal(ml):
    return {"ml": ml}


# ── silent through normal operation ────────────────────────────────────────

@pytest.mark.parametrize("ml", [130, 136, 161, 193, 199, 240])
def test_silent_through_the_observed_operating_band(conn, fired, ml):
    """Measured over the last 3000 samples at the current posture: min 130, p05 136,
    p50 161, max 199. None of that is news, and paging through it would train the
    operator to ignore the channel."""
    assert app._check_margin_level(conn, bal(ml)) == ml
    assert fired == []


def test_exactly_at_the_floor_is_not_an_alert(conn, fired):
    assert app._check_margin_level(conn, bal(120)) == 120
    assert fired == []


# ── fires on a real approach ───────────────────────────────────────────────

@pytest.mark.parametrize("ml", [119, 115, 110, 106, 95, 85])
def test_fires_below_the_floor(conn, fired, ml):
    """106-115 is the band the 2026-07-16 near-margin-call actually sat in."""
    assert app._check_margin_level(conn, bal(ml)) == ml
    assert len(fired) == 1
    kind, _key, msg, _loud = fired[0]
    assert kind == "margin-level"
    assert f"{ml}" in msg


def test_alert_is_journaled_too(conn, fired):
    app._check_margin_level(conn, bal(110))
    rows = conn.execute("SELECT kind, text FROM journal").fetchall()
    assert len(rows) == 1 and rows[0][0] == "defense"


def test_worsening_slide_mints_a_new_throttle_key(conn, fired):
    """fire_safety throttles per (kind, key) for 30 min. A 5-point bucket in the key
    means a book sliding toward the call keeps paging instead of going quiet after
    the first alert."""
    for ml in (118, 112, 107, 99):
        app._check_margin_level(conn, bal(ml))
    keys = [k for _kind, k, _msg, _loud in fired]
    assert len(set(keys)) == 4, keys


def test_small_moves_inside_one_bucket_reuse_the_key(conn, fired):
    """Noise within a 5-point band must NOT mint new keys — that would defeat the
    throttle and turn a hovering level into a siren."""
    for ml in (118, 117, 116):
        app._check_margin_level(conn, bal(ml))
    assert len({k for _kind, k, _msg, _loud in fired}) == 1


# ── the overnight-flapping regression (2026-07-24) ─────────────────────────

def test_oscillating_across_a_bucket_boundary_pages_once(conn, fired):
    """THE BUG THIS FIXES. On 2026-07-23 the level wobbled 110<->120 for nine hours.
    Each 5-point bucket carried its own throttle window, so alternating buckets paged
    a `-u critical` popup roughly every half hour all night for a condition that was
    not deteriorating. Only a WORSENING slide is news."""
    for ml in (112, 118, 111, 119, 112, 120, 111):
        app._check_margin_level(conn, bal(ml))
    assert len(fired) == 1, [f[1] for f in fired]


def test_recovering_above_the_floor_re_arms_the_watch(conn, fired):
    """The ratchet must not latch forever — a book that recovers and later slides back
    toward the call has to page again."""
    app._check_margin_level(conn, bal(110))
    app._check_margin_level(conn, bal(250))      # recovered well clear of the floor
    app._check_margin_level(conn, bal(110))
    assert len(fired) == 2


def test_touching_the_floor_does_not_re_arm(conn, fired):
    """Hysteresis. Re-arming at exactly the floor would make the ratchet decorative for
    the case it exists to fix — a level oscillating ACROSS the boundary."""
    app._check_margin_level(conn, bal(110))
    app._check_margin_level(conn, bal(121))      # over the floor, but not a recovery
    app._check_margin_level(conn, bal(110))
    assert len(fired) == 1


# ── quiet vs loud routing (operator 2026-07-24) ────────────────────────────

def test_under_the_floor_is_quiet(conn, fired, monkeypatch):
    """Tight-but-recoverable is a dashboard fact. It logs and journals; it must not
    make noise, or the channel gets ignored by the time it matters."""
    monkeypatch.setattr(config, "MARGIN_LEVEL_LOUD_PCT", 100)
    app._check_margin_level(conn, bal(110))
    assert fired[0][3] is False


def test_the_seizure_band_is_loud(conn, fired, monkeypatch):
    """Below the loud threshold Kraken is close to taking the book — that wakes the
    operator regardless of how chronic the state is."""
    monkeypatch.setattr(config, "MARGIN_LEVEL_LOUD_PCT", 100)
    app._check_margin_level(conn, bal(92))
    assert fired[0][3] is True


def test_zero_loud_threshold_never_escalates(conn, fired, monkeypatch):
    monkeypatch.setattr(config, "MARGIN_LEVEL_LOUD_PCT", 0)
    app._check_margin_level(conn, bal(50))
    assert fired[0][3] is False


# ── fail open ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("balance", [None, {}, {"ml": None}, {"ml": ""},
                                     {"ml": "junk"}, {"ml": 0}, {"ml": -5},
                                     {"ml": float("nan")}])
def test_unknown_or_garbage_level_never_pages(conn, fired, balance):
    assert app._check_margin_level(conn, balance) is None
    assert fired == []


def test_zero_threshold_disables(conn, fired, monkeypatch):
    monkeypatch.setattr(config, "MARGIN_LEVEL_ALERT_PCT", 0)
    assert app._check_margin_level(conn, bal(50)) == 50
    assert fired == []


def test_never_raises_into_the_exec_loop(conn, monkeypatch):
    monkeypatch.setattr(config, "MARGIN_LEVEL_ALERT_PCT", 120)
    monkeypatch.setattr(app.alerter, "fire_safety",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert app._check_margin_level(conn, bal(100)) is None


# ── calibration drift guard ────────────────────────────────────────────────
#
# These read the REAL config values (no monkeypatch) on purpose. The 150 that sat
# here until 2026-07-22 was not wrong when it was written — it was the ratio twin of
# an all-pairs-10:1 book. It went wrong silently when the posture moved underneath it
# (SIZE_MULT 16->8->2, stack floor 120->160->200 over 07-16..07-21) and nothing failed,
# because a bare constant has no relationship anything can check.
#
# So don't pin the measured band — that would rot exactly the same way. Pin the
# RELATIONSHIP to the two levels that define the operating envelope. Both sides move
# on their own when the posture changes, so the window re-derives itself and a
# now-miscalibrated alert floor fails here instead of in production.

_ALERT = float(config.MARGIN_LEVEL_ALERT_PCT or 0)
_FLOOR = float(config.MARGIN_LEVEL_STACK_FLOOR_PCT or 0)
_CALL = defense.CALL_ML * 100        # Kraken margin-calls at ml <= 80%

# The book OPERATES AT the stack floor (config.py says so where the floor is set):
# seeds pause below it, the book waits, ml recovers. Routine dips under the floor are
# normal running, so the alert has to sit clear of that dip band or it pages at nothing.
# 0.70 is the empirical shape — at floor 200 the book bottomed near ml 130 (0.65x).
_DIP_BAND = 0.70

# An alert that only fires once Kraken has already called you is not a warning.
_WARN_MARGIN = 1.25


@pytest.mark.skipif(not _ALERT, reason="alert threshold disabled (0)")
def test_alert_floor_sits_below_the_routine_dip_band():
    """Fires when the alert floor drifts up into normal operation — the exact failure
    that made the old 150 unusable, and that wiring it as-is would have shipped."""
    assert _ALERT < _FLOOR * _DIP_BAND, (
        f"MARGIN_LEVEL_ALERT_PCT={_ALERT:.0f} is inside the routine operating band for a "
        f"stack floor of {_FLOOR:.0f} (the book runs at and below the floor, so anything "
        f"above {_FLOOR * _DIP_BAND:.0f} pages during normal running). Recalibrate it "
        f"against the actual ml distribution, don't just lower the floor to suit it."
    )


@pytest.mark.skipif(not _ALERT, reason="alert threshold disabled (0)")
def test_alert_floor_still_warns_before_the_exchange_acts():
    assert _ALERT > _CALL * _WARN_MARGIN, (
        f"MARGIN_LEVEL_ALERT_PCT={_ALERT:.0f} is too close to Kraken's margin call at "
        f"ml {_CALL:.0f}. By the time it pages there is no room to act."
    )


@pytest.mark.skipif(not _ALERT, reason="alert threshold disabled (0)")
def test_the_calibration_window_is_not_empty():
    """If the floor and the call level ever squeeze together there is no honest value
    to pick, and silently keeping the old one would be the worst outcome. Fail loudly
    and make it a decision instead."""
    lo, hi = _CALL * _WARN_MARGIN, _FLOOR * _DIP_BAND
    assert lo < hi, (
        f"No valid alert threshold exists between Kraken's call at {_CALL:.0f} and a "
        f"stack floor of {_FLOOR:.0f}: the window is [{lo:.0f}, {hi:.0f}]. The stack "
        f"floor is too low for the margin-level watch to warn about anything."
    )
