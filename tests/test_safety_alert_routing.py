"""Safety-alert quiet/loud routing (alerter.fire_safety).

Operator, 2026-07-24: "calm the notify-send alerts down, they are annoying." The
channel had been routing EVENTS and STATES identically — every kind got a `-u
critical` popup plus a sound — so a condition that simply persisted (margin level
under the alert floor) re-paged on a repeat timer all night.

The failure mode this guards against is NOT noise for its own sake: it is a channel
the operator learns to ignore, so the one alert that matters (a naked leveraged long)
arrives into a habit of dismissing popups. These tests pin that the loud path stays
loud, that silence is opt-IN, and that a quiet alert is still fully observable in the
log and the journal.

No sound, no popup, no network: every side-effecting call is monkeypatched.
"""
import pytest

from deepfield import alerter, config


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    """Capture the three side-effect channels instead of firing them."""
    played, notified, telegrammed = [], [], []
    monkeypatch.setattr(alerter, "play_alert", lambda: played.append(1) or "paplay")
    monkeypatch.setattr(alerter.shutil, "which", lambda _n: "/usr/bin/notify-send")
    monkeypatch.setattr(alerter.subprocess, "run",
                        lambda *a, **k: notified.append(a) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(alerter, "_telegram", lambda _t: telegrammed.append(1) or None)
    monkeypatch.setattr(alerter, "_safety_last", {})      # fresh throttle state per test
    return {"played": played, "notified": notified, "telegram": telegrammed}


def test_loud_kind_makes_noise(_no_side_effects):
    """A naked leveraged long is the whole reason this channel exists."""
    r = alerter.fire_safety("unprotected", "SOL/USD", "stop-rest FAILED")
    assert r["sound"] == "paplay" and r["notify"] is True
    assert len(_no_side_effects["played"]) == 1
    assert len(_no_side_effects["notified"]) == 1


@pytest.mark.parametrize("kind", ["margin-level", "trim", "tp"])
def test_quiet_kinds_are_silent(kind, _no_side_effects):
    r = alerter.fire_safety(kind, "*", "chronic state")
    assert r["quiet"] is True and r["sound"] is None and r["notify"] is False
    assert _no_side_effects["played"] == []
    assert _no_side_effects["notified"] == []


def test_silence_is_opt_in_for_unknown_kinds(_no_side_effects):
    """A new event kind must default to LOUD. Inheriting silence is how a real alert
    goes missing years later."""
    r = alerter.fire_safety("some-brand-new-kind", "BTC/USD", "?")
    assert r["sound"] == "paplay" and r["notify"] is True


def test_loud_override_escalates_a_quiet_kind(_no_side_effects):
    """The margin-level watch uses this at the exchange seizure band."""
    r = alerter.fire_safety("margin-level", "ml95", "near the call", loud=True)
    assert r["sound"] == "paplay" and r["notify"] is True


def test_quiet_override_silences_a_loud_kind(_no_side_effects):
    r = alerter.fire_safety("unprotected", "SOL/USD", "…", loud=False)
    assert r["quiet"] is True
    assert _no_side_effects["played"] == []


def test_quiet_alert_is_still_logged_at_warning(caplog):
    """Quiet must mean 'no speaker', never 'no record' — the operator reads the
    console feed and greps the log; both still have to show it."""
    with caplog.at_level("WARNING", logger=alerter.log.name):
        alerter.fire_safety("margin-level", "ml110", "margin level 110% below floor")
    assert any("margin level 110%" in r.getMessage() for r in caplog.records)


def test_quiet_kinds_use_the_longer_throttle(monkeypatch, _no_side_effects):
    """A log-only line can afford a long window; a repeat inside it is suppressed."""
    monkeypatch.setattr(config, "SAFETY_ALERT_QUIET_THROTTLE_SECS", 21600)
    assert alerter.fire_safety("margin-level", "ml110", "first") is not None
    assert alerter.fire_safety("margin-level", "ml110", "again") is None


def test_loud_and_quiet_throttles_are_independent_knobs(monkeypatch, _no_side_effects):
    """Turning the quiet window up must not lengthen the loud one."""
    monkeypatch.setattr(config, "SAFETY_ALERT_QUIET_THROTTLE_SECS", 21600)
    monkeypatch.setattr(config, "SAFETY_ALERT_THROTTLE_SECS", 0)   # 0 = never throttle
    assert alerter.fire_safety("unprotected", "SOL/USD", "a") is not None
    assert alerter.fire_safety("unprotected", "SOL/USD", "b") is not None


def test_never_raises_into_the_money_path(monkeypatch):
    monkeypatch.setattr(alerter, "play_alert",
                        lambda: (_ for _ in ()).throw(RuntimeError("no audio device")))
    monkeypatch.setattr(alerter, "_safety_last", {})
    assert alerter.fire_safety("unprotected", "SOL/USD", "…") is None
