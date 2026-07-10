"""Regression tests for the 2026-07-10 multi-agent bug-extermination sweep.
Each test pins one confirmed finding; the test is written to FAIL against the
pre-fix code (red -> green) so it guards the fix, not just documents it."""


# ── Rank 1: EXEC_MODE fail-open — non-canonical value must NOT arm execution ──
def test_exec_mode_is_fail_closed():
    from deepfield import config
    f = config._normalize_exec_mode
    # the four canonical modes pass through untouched
    for good in ("off", "paper", "validate", "live"):
        assert f(good) == good
    # ANYTHING else resolves to 'off' — never silently to a live/paper order path.
    # Includes case slips, trailing space (trivial in .env/systemd), and typos.
    for bad in ("Paper", "paper ", " live", "LIVE", "Live", "test", "sim",
                "dry", "on", "true", "", "liveish", None, 1):
        assert f(bad) == "off", f"{bad!r} must resolve to 'off', not arm execution"
