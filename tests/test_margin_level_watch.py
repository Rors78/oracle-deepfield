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

from deepfield import app, config

SCHEMA = """
CREATE TABLE journal(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT,
    symbol TEXT, text TEXT);
"""


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    return c


@pytest.fixture
def fired(monkeypatch):
    """Capture fire_safety calls as (kind, throttle_key, message)."""
    calls = []
    monkeypatch.setattr(app.alerter, "fire_safety",
                        lambda kind, sym, msg: calls.append((kind, sym, msg)))
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
    kind, _key, msg = fired[0]
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
    keys = [k for _kind, k, _msg in fired]
    assert len(set(keys)) == 4, keys


def test_small_moves_inside_one_bucket_reuse_the_key(conn, fired):
    """Noise within a 5-point band must NOT mint new keys — that would defeat the
    throttle and turn a hovering level into a siren."""
    for ml in (118, 117, 116):
        app._check_margin_level(conn, bal(ml))
    assert len({k for _kind, k, _msg in fired}) == 1


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
