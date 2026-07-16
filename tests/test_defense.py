"""Wave 1 defense engine — buffer math, tiers, and the edge-triggered escalation
state machine. Pure-function tests (no DB/API); the live-poll wiring is proven
separately by the --once runtime + grep checks in the fix dispatch.
"""
import math

import pytest

from deepfield import defense


# ── buffer math ─────────────────────────────────────────────────────────────

def test_buffers_match_live_audit_numbers():
    """The exact live pull from DEEPFIELD_AUDIT_EVIDENCE.md §A:
    e=200.6894, m=151.1935, v=1500.5541 -> liq 9.34%, call 5.31%, book 7.48x."""
    c = defense.compute(200.6894, 151.1935, 1500.5541)
    assert c["buffer_liq_pct"] == pytest.approx(9.34, abs=0.02)
    assert c["buffer_call_pct"] == pytest.approx(5.31, abs=0.02)
    assert c["eff_leverage"] == pytest.approx(7.48, abs=0.01)


def test_flat_book_is_safest_not_a_crash():
    """v=0 (no positions) -> +inf buffers, 0 leverage. Never divides by zero and
    never maps the default case to a scarier number than real data."""
    c = defense.compute(200.0, 0.0, 0.0)
    assert math.isinf(c["buffer_liq_pct"]) and c["buffer_liq_pct"] > 0
    assert math.isinf(c["buffer_call_pct"]) and c["buffer_call_pct"] > 0
    assert c["eff_leverage"] == 0.0


def test_unknown_inputs_map_to_none_not_a_number():
    assert defense.compute(None, 100.0, 1000.0) is None       # equity unknown
    assert defense.compute(0.0, 100.0, 1000.0) is None        # non-positive equity
    assert defense.compute(-5.0, 100.0, 1000.0) is None
    assert defense.compute(200.0, None, 1000.0) is None       # margin unknown
    assert defense.compute("x", 100.0, 1000.0) is None


def test_lower_buffer_means_higher_leverage_monotonic():
    lo = defense.compute(200.0, 100.0, 1000.0)   # 5x
    hi = defense.compute(200.0, 150.0, 1500.0)   # 7.5x
    assert hi["eff_leverage"] > lo["eff_leverage"]
    assert hi["buffer_liq_pct"] < lo["buffer_liq_pct"]


# ── tiers ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("buf,expected", [
    (20.0, defense.TIER_NOMINAL),
    (12.0, defense.TIER_NOMINAL),     # boundary: >= nominal
    (11.9, defense.TIER_CAUTION),
    (6.0, defense.TIER_CAUTION),      # boundary: >= critical
    (5.9, defense.TIER_CRITICAL),
    (0.0, defense.TIER_CRITICAL),
    (-3.0, defense.TIER_CRITICAL),    # already past liq on paper
    (math.inf, defense.TIER_NOMINAL), # flat book
    (None, defense.TIER_UNKNOWN),
    (float("nan"), defense.TIER_UNKNOWN),
])
def test_tier_boundaries(buf, expected):
    assert defense.tier_for(buf, 12.0, 6.0) == expected


# ── escalation state machine ────────────────────────────────────────────────

def _c(buf):
    return {"buffer_liq_pct": buf, "buffer_call_pct": buf - 4, "eff_leverage": 7.0}


def test_boot_into_nominal_emits_one_baseline_then_quiet():
    st, ev = defense.step(None, _c(15.0), defense.TIER_NOMINAL, now=1.0)
    assert [e["kind"] for e in ev] == ["transition"]
    assert ev[0]["from"] is None and ev[0]["to"] == defense.TIER_NOMINAL
    # next identical cycle: no event (edge-triggered — this is the anti-spam law)
    st2, ev2 = defense.step(st, _c(15.0), defense.TIER_NOMINAL, now=2.0)
    assert ev2 == []


def test_escalation_transitions_emit_once_each():
    st, _ = defense.step(None, _c(15.0), defense.TIER_NOMINAL, now=1.0)
    st, ev = defense.step(st, _c(9.0), defense.TIER_CAUTION, now=2.0)
    assert [e["to"] for e in ev] == [defense.TIER_CAUTION]
    st, ev = defense.step(st, _c(9.0), defense.TIER_CAUTION, now=3.0)   # unchanged
    assert ev == []
    st, ev = defense.step(st, _c(4.0), defense.TIER_CRITICAL, now=4.0)
    assert [e["to"] for e in ev] == [defense.TIER_CRITICAL]
    assert st["alert_buffer"] == pytest.approx(4.0)


def test_critical_re_alerts_only_on_2pct_further_decay():
    st = {"tier": defense.TIER_CRITICAL, "since": 1.0, "alert_buffer": 5.0}
    # <2% further decay: no re-alert
    st, ev = defense.step(st, _c(4.0), defense.TIER_CRITICAL, now=2.0)
    assert ev == []
    assert st["alert_buffer"] == pytest.approx(5.0)   # last-alert level unchanged
    # >=2% further decay: re-alert, and the level advances
    st, ev = defense.step(st, _c(2.9), defense.TIER_CRITICAL, now=3.0)
    assert [e["kind"] for e in ev] == ["recritical"]
    assert st["alert_buffer"] == pytest.approx(2.9)


def test_recovery_to_nominal_emits_transition():
    st = {"tier": defense.TIER_CRITICAL, "since": 1.0, "alert_buffer": 3.0}
    st, ev = defense.step(st, _c(14.0), defense.TIER_NOMINAL, now=9.0)
    assert [e["to"] for e in ev] == [defense.TIER_NOMINAL]
    assert ev[0]["from"] == defense.TIER_CRITICAL


def test_unknown_retains_prior_tier_and_never_alerts():
    st = {"tier": defense.TIER_CAUTION, "since": 1.0, "alert_buffer": 9.0}
    st2, ev = defense.step(st, None, defense.TIER_UNKNOWN, now=5.0)
    assert ev == []
    assert st2["tier"] == defense.TIER_CAUTION   # a missing read can't fake recovery


def test_flat_book_buffer_is_not_tracked_as_a_number():
    """+inf buffer must not be stored/compared as a numeric alert_buffer."""
    st, ev = defense.step(None, _c(math.inf), defense.TIER_NOMINAL, now=1.0)
    assert st["alert_buffer"] is None


# ── formatting never crashes ─────────────────────────────────────────────────

def test_format_line_handles_inf_none_and_real():
    assert "CAUTION" in defense.format_line(_c(9.0), defense.TIER_CAUTION)
    assert defense.format_line(None, None)                       # no crash
    assert defense.format_line(_c(math.inf), defense.TIER_NOMINAL)  # no crash
