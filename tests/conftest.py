"""Shared test isolation.

The live HALT_ENTRIES file (operator kill-switch) lives at config.HALT_FILE in
the project root. When it's present, rails_ok() short-circuits to HALT — which
would masquerade as / suppress every execution test. Point HALT_FILE at a
nonexistent temp path for every test so the suite never depends on (or is
poisoned by) the real runtime file. Tests that WANT a halt monkeypatch it back.
"""
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
