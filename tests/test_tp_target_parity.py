"""The dashboard's T/P target must BE the executor's T/P target.

2026-08-05, from the operator's own screen. The deck read:

    t/p at $208.54 · -6.6% away          equity $223.30

which says a whole-book flatten is overdue. The executor's real target was
$289.83 and the book was 23% BELOW it, holding — the log said so in as many
words ("target now $289.83 (baseline $289.83 stays the profit yardstick)") and
no T/P fired. A 39% discrepancy, and worse than wrong: it inverted the meaning,
turning "deep in a drawdown" into "about to bank a cycle".

Cause: ONE RULE, TWO IMPLEMENTATIONS. The 07-27 baseline floor was added to the
closure inside `_check_take_profit`, while `_persist_web_live` kept its own
`min(baseline, trough) * (1 + TP_PCT)`. Nothing misfired — the money path was
correct throughout. The console was simply describing a different bot.

`executor.tp_target` is now the single definition. These tests exist so a future
change to the rule cannot land in one caller and not the other.
"""
import pytest

from deepfield import app, config, store
from deepfield import executor as ex_mod


def _blob_target(conn):
    """The number the deck will show, taken the long way round — through the real
    blob writer, so a regression anywhere in that path is caught rather than the
    helper being tested against itself."""
    import json

    class _State:
        pairs, links, started_ts = {}, None, 0.0
        exec = {}
    app._persist_web_live(conn, _State(), 223.30, 3.64, 219.0, None)
    return json.loads(store.meta_get(conn, "web_live"))["tp_target"]


@pytest.fixture
def conn(tmp_path):
    c = store.connect(str(tmp_path / "tp.db"))
    yield c
    c.close()


def test_the_incident_deck_said_208_executor_said_289(conn, monkeypatch):
    """The exact live state at 20:55 on 2026-08-05."""
    monkeypatch.setattr(config, "TP_TARGET_FLOOR_BASELINE", True)
    monkeypatch.setattr(config, "TP_PCT", 0.20)
    store.meta_set(conn, "tp_baseline", 289.8299)
    store.meta_set(conn, "tp_trough", 173.7825)

    assert ex_mod.tp_target(289.8299, 173.7825) == pytest.approx(289.83, abs=0.01)
    # The old deck formula, kept here as the thing that must NEVER come back.
    assert min(289.8299, 173.7825) * 1.20 == pytest.approx(208.54, abs=0.01)
    assert _blob_target(conn) == pytest.approx(289.83, abs=0.01)

    # And the meaning that was inverted: equity 223.30 is BELOW target, holding.
    assert 223.30 < _blob_target(conn)


def test_deck_and_executor_agree_across_the_state_space(conn, monkeypatch):
    monkeypatch.setattr(config, "TP_PCT", 0.20)
    cases = [
        ("trough under baseline (the ratchet's whole point)", 289.83, 173.78),
        ("trough equals baseline (freshly armed)", 200.00, 200.00),
        ("trough above baseline (clamped by the caller)", 200.00, 260.00),
        ("tiny book", 12.50, 9.00),
        ("large book", 10000.0, 4000.0),
    ]
    for floor in (True, False):
        monkeypatch.setattr(config, "TP_TARGET_FLOOR_BASELINE", floor)
        for label, baseline, trough in cases:
            store.meta_set(conn, "tp_baseline", baseline)
            store.meta_set(conn, "tp_trough", trough)
            want = ex_mod.tp_target(baseline, trough)
            got = _blob_target(conn)
            assert got == pytest.approx(round(want, 2), abs=0.01), \
                f"floor={floor} {label}: deck {got} vs executor {want}"


def test_floor_means_a_fire_can_never_bank_a_loss(monkeypatch):
    """What the floor is FOR (operator 2026-07-27): the ratchet may lower the target
    toward a bounce off the low, but never below baseline, so the worst a fire can
    settle at is breakeven. Jul-26 fired off a trough 21% under baseline and locked
    -$15.09; that is the cycle this prevents."""
    monkeypatch.setattr(config, "TP_PCT", 0.20)
    monkeypatch.setattr(config, "TP_TARGET_FLOOR_BASELINE", True)
    for baseline, trough in ((289.83, 173.78), (220.0, 100.0), (50.0, 1.0)):
        assert ex_mod.tp_target(baseline, trough) >= baseline

    # Explicitly opted out, the old red-cycle behaviour returns — unchanged.
    monkeypatch.setattr(config, "TP_TARGET_FLOOR_BASELINE", False)
    assert ex_mod.tp_target(289.83, 173.78) == pytest.approx(208.54, abs=0.01)


def test_no_baseline_means_no_target_not_a_zero(conn, monkeypatch):
    """Unarmed T/P must render "—", never $0.00 — which on a gauge measuring
    distance-to-target would read as "fire immediately"."""
    monkeypatch.setattr(config, "TP_TARGET_FLOOR_BASELINE", True)
    store.meta_set(conn, "tp_baseline", "")
    store.meta_set(conn, "tp_trough", "")
    assert _blob_target(conn) is None


def test_missing_trough_falls_back_to_baseline(conn, monkeypatch):
    """Pre-ratchet DBs have no tp_trough. The target is then simply
    baseline * (1 + TP_PCT) — not a target computed against zero, which with the
    floor off would be $0.00 and with it on would silently mask the bug."""
    monkeypatch.setattr(config, "TP_PCT", 0.20)
    store.meta_set(conn, "tp_baseline", 200.0)
    store.meta_set(conn, "tp_trough", "")
    for floor in (True, False):
        monkeypatch.setattr(config, "TP_TARGET_FLOOR_BASELINE", floor)
        assert _blob_target(conn) == pytest.approx(240.0, abs=0.01)
