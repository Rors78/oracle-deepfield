"""Shared test isolation.

The live HALT_ENTRIES file (operator kill-switch) lives at config.HALT_FILE in
the project root. When it's present, rails_ok() short-circuits to HALT — which
would masquerade as / suppress every execution test. Point HALT_FILE at a
nonexistent temp path for every test so the suite never depends on (or is
poisoned by) the real runtime file. Tests that WANT a halt monkeypatch it back.
"""
import types

import pytest

from deepfield import config


@pytest.fixture(autouse=True)
def _isolate_halt_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HALT_FILE", str(tmp_path / "NO_SUCH_HALT"))


@pytest.fixture(autouse=True)
def _isolate_operator_stack_knobs(monkeypatch):
    """The 2026-07-13 operator stack directives (3x sizing, seeded chains, equity
    take-profit) each add live behavior on top of the paths the legacy tests pin
    down. Default them INERT here so every existing test keeps testing exactly
    what it tested; the dedicated SIZE_MULT/seed/T-P tests monkeypatch them on."""
    monkeypatch.setattr(config, "SIZE_MULT", 1.0)
    monkeypatch.setattr(config, "SEED_PAIRS", ())
    monkeypatch.setattr(config, "TP_ENABLED", False)
    # Reverse gear (Wave 4) now defaults ARMED — like the knobs above it adds live
    # behavior to poll_fills; default it inert so legacy tests test exactly what they
    # tested. The dedicated test_reverse_gear.py monkeypatches it on.
    monkeypatch.setattr(config, "REVERSE_GEAR_ENABLED", False)


@pytest.fixture(autouse=True)
def _mute_alerter_desktop(monkeypatch):
    """Tests that walk recon-mismatch / unprotected / stop-fired paths fired REAL
    desktop alerts: executor._fire_safety lazy-imports the live alerter, and only
    test_safety_alert_routing ever mocked it. A few full-suite runs on 2026-07-31
    buried the operator's desktop in critical 'DEEPFIELD SAFETY: recon-mismatch'
    popups + paplay sirens that read as live incidents. Rebind alerter's OWN
    shutil/subprocess module references to inert stubs (which() finds no binary ->
    no sound tier, no notify-send; the real modules are untouched for everyone
    else) and blank the Telegram creds. play_alert()/_telegram() stay REAL —
    test_m5_ingest's bell-fallback and telegram-URL tests still exercise them,
    and the routing tests' own leaf mocks simply land on these stubs and win.
    _safety_last is the same module-level cross-test clock as the poll_fills
    dicts below — fresh per test."""
    from deepfield import alerter
    monkeypatch.setattr(alerter, "shutil", types.SimpleNamespace(which=lambda _n: None))
    monkeypatch.setattr(alerter, "subprocess", types.SimpleNamespace(
        run=lambda *a, **k: types.SimpleNamespace(returncode=1)))
    monkeypatch.setattr(alerter, "TG_TOKEN", None)
    monkeypatch.setattr(alerter, "TG_CHAT", None)
    monkeypatch.setattr(alerter, "_safety_last", {})


@pytest.fixture(autouse=True)
def _isolate_poll_fills_clocks(monkeypatch):
    """poll_fills gates its runtime exchange-truth sweep and the reladder/seed
    safety nets behind MODULE-level monotonic clocks (_recon_next, _reladder_next,
    _seed_next). In a fresh process _recon_next is 0.0, so the FIRST test to call
    poll_fills eats the one-shot runtime sweep — its broker fake receives an
    OpenPositions call it never faked, and pytest-randomly turned that into a
    roaming intermittent failure (whichever ladder test the shuffle put first).
    Default the sweep INERT and hand every test fresh backoff dicts. The runtime
    reconcile tests call verify_open_stops() directly (no clock involved), and the
    seed tests clear/assert _seed_next themselves — a fresh dict preserves both.

    The reladder safety net (_ensure_ladder_rungs) is the same story one layer
    down: clock-fresh it fires in EVERY live-mode poll_fills test and continues
    the chain a step below the lowest fill (test_ladder_dedupe_skips_owned_level
    only ever passed because an earlier test's clock stamp suppressed it). No test
    exercises it through poll_fills, so default it INERT like the other add-on
    behaviors above; a test that wants it monkeypatches the method back."""
    from deepfield import executor as ex_mod
    monkeypatch.setattr(config, "RUNTIME_RECON_SECS", 0)
    monkeypatch.setattr(ex_mod, "_recon_next", 0.0)
    monkeypatch.setattr(ex_mod, "_reladder_next", {})
    monkeypatch.setattr(ex_mod, "_seed_next", {})
    monkeypatch.setattr(ex_mod.Executor, "_ensure_ladder_rungs", lambda self: None)


@pytest.fixture(autouse=True)
def _bridge_query_orders_to_query_order(monkeypatch):
    """poll_fills batches its pending-order lookups through broker.query_orders
    (one 50-txid sweep per cycle — full-universe roster, 2026-07-19). The legacy
    tests fake per-txid broker.query_order; bridge the batch through it here so
    every existing fake keeps its exact intent ("any txid returns X"). Reads
    query_order DYNAMICALLY, so a test's own monkeypatch wins. Tests that fake
    query_orders itself simply override this (their setattr runs later). Absent
    result contract preserved: None from query_order -> key omitted -> unknown."""
    from deepfield import broker

    def _batched(txids):
        out = {}
        for t in txids or []:
            if not t:
                continue
            o = broker.query_order(t)
            if o is not None:
                out[t] = o
        return out

    monkeypatch.setattr(broker, "query_orders", _batched)
