"""Rollover-fee accounting must never walk Kraken's Ledgers history.

The bug these pin down (fixed 2026-07-19): the first poll seeded fees_total by
walking Ledgers back to start_ts=0. Kraken held 6011 rollover entries (~121 pages)
against a rate limit shared with every other private call the bot makes, so the
seed was rejected mid-walk every time. The caller only banked its cursor on a
clean walk, so the cursor stayed unset and the NEXT poll re-walked from 0 — an
hourly burst of failing calls that also starved the money path, and fees_total sat
at None from the day the feature was wired.

The invariant is therefore not "the sum is right" but "no poll ever starts a walk
that cannot finish": the first poll anchors forward, and a truncated walk still
advances the cursor rather than deadlocking on a retry that will never succeed.
"""
import datetime as _dt

import pytest

from deepfield import app, broker, config, store

# Captured before the autouse fixture below shadows them, so the walk's own
# page/pacing behavior can still be exercised for real.
_REAL_WALK = broker.rollover_fees_since
_DEFAULT_PACE = broker.LEDGERS_PAGE_PACE_SECS


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "rollover.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    store.connect(path).close()
    return path


def _meta(path, key):
    c = store.connect(path)
    try:
        return store.meta_get(c, key, None)
    finally:
        c.close()


class _Recorder(list):
    """List of observed start_ts values, plus the walk result to hand back."""
    result = (0.0, 0, 0.0, True)

    def returns(self, result):
        self.result = result


@pytest.fixture(autouse=True)
def calls(monkeypatch):
    """Record every rollover_fees_since start_ts; return a complete empty walk.

    AUTOUSE on purpose: an unpatched poll reaches the real Kraken Ledgers endpoint,
    which is the very thing these tests exist to keep off the wire. Tests that want
    different walk behavior call calls.returns(...) rather than leaving a window
    where the real reader is live.
    """
    seen = _Recorder()

    def fake(start_ts):
        seen.append(float(start_ts or 0))
        r = seen.result
        return r(start_ts) if callable(r) else r

    monkeypatch.setattr(broker, "rollover_fees_since", fake)
    return seen


def test_first_poll_anchors_instead_of_walking_history(db, calls):
    """The regression: an unset cursor must NOT trigger a walk from 0."""
    app._poll_rollover_fees_threaded()

    # fees_day legitimately walks from UTC midnight; nothing may walk from 0.
    assert 0.0 not in calls, f"walked history from 0 — the exact bug: {calls}"
    midnight = _dt.datetime.now(_dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    assert calls == [midnight], "first poll should read fees_day only, then anchor"

    assert float(_meta(db, "fees_cursor")) > 0, "cursor must be anchored"
    assert _meta(db, "fees_epoch") is not None, "epoch must record when accounting began"
    assert float(_meta(db, "fees_total")) == 0.0


def test_second_poll_reads_only_the_window_since_the_cursor(db, calls):
    app._poll_rollover_fees_threaded()          # anchors
    anchored = float(_meta(db, "fees_cursor"))
    calls.clear()

    app._poll_rollover_fees_threaded()          # steady state

    assert 0.0 not in calls, "steady-state poll must never re-walk history"
    assert anchored in calls, "window must start at the banked cursor"
    assert float(_meta(db, "fees_cursor")) >= anchored, "cursor must advance forward"


def test_window_total_accumulates_across_polls(db, calls):
    calls.returns((0.25, 1, 0.0, True))
    app._poll_rollover_fees_threaded()          # anchors, banks nothing
    assert float(_meta(db, "fees_total")) == 0.0

    app._poll_rollover_fees_threaded()
    assert float(_meta(db, "fees_total")) == 0.25
    app._poll_rollover_fees_threaded()
    assert float(_meta(db, "fees_total")) == 0.50, "each window must bank onto the last"


def test_truncated_walk_still_advances_the_cursor(db, calls):
    """A capped walk under-counts, but holding the cursor back is the deadlock."""
    app._poll_rollover_fees_threaded()          # anchors
    anchored = float(_meta(db, "fees_cursor"))

    calls.returns((1.0, 500, 0.0, False))
    app._poll_rollover_fees_threaded()

    assert float(_meta(db, "fees_cursor")) > anchored, \
        "a truncated window must still advance — otherwise the walk only grows"
    assert float(_meta(db, "fees_total")) == 1.0


def test_api_failure_holds_the_cursor_for_retry(db, calls):
    app._poll_rollover_fees_threaded()          # anchors
    anchored = float(_meta(db, "fees_cursor"))

    calls.returns(None)
    app._poll_rollover_fees_threaded()

    assert float(_meta(db, "fees_cursor")) == anchored, \
        "a failed read must bank nothing and retry the same window"


def test_walk_is_page_bounded_and_paced(monkeypatch):
    """The walk itself stays bounded even if a caller hands it a wide window."""
    pages = []
    monkeypatch.setattr(broker, "LEDGERS_PAGE_PACE_SECS", 0)   # don't sleep the suite

    def fake_private(endpoint, params=None, **kw):
        pages.append(params["ofs"])
        return {"ledger": {f"L{len(pages)}-{i}": {"fee": "0.01", "time": 1.0}
                           for i in range(50)}}          # always a full page

    monkeypatch.setattr(broker, "private", fake_private)
    total, count, newest, complete = _REAL_WALK(0)

    assert len(pages) == broker.LEDGERS_MAX_PAGES, "walk must stop at the page ceiling"
    assert complete is False, "a ceiling-truncated walk must report itself incomplete"
    assert count == 50 * broker.LEDGERS_MAX_PAGES
    assert _DEFAULT_PACE > 0, "pages must be paced apart in production"
