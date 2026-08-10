"""Shared test isolation.

The live HALT_ENTRIES file (operator kill-switch) lives at config.HALT_FILE in
the project root. When it's present, rails_ok() short-circuits to HALT — which
would masquerade as / suppress every execution test. Point HALT_FILE at a
nonexistent temp path for every test so the suite never depends on (or is
poisoned by) the real runtime file. Tests that WANT a halt monkeypatch it back.
"""
import types

import pytest

from deepfield import config, vol


def pin_vol(conn, tp=4.0, sl=10.0, rung=1.0, symbols=None):
    """Pin the per-pair distance table to fixed percentages for a test.

    Since the 2026-08-06 volatility ruling there is no flat LADDER_STEP_PCT /
    TP_RUNG_PCT / STOP_*_PCT to monkeypatch — every distance resolves per pair from
    deepfield/vol.py. Tests that need a KNOWN geometry write a table here instead of
    patching a constant, so they still exercise the real resolver rather than a
    test-only branch inside it.

    Defaults reproduce the pre-ruling flat geometry (4% target, 10% stop, 1% rung), so
    a legacy test pinned to those numbers keeps asserting exactly what it always did."""
    syms = symbols or [p["ws"] for p in config.PAIRS]
    vol.save_table(conn, {s: {"atr_pct": None, "tp_pct": tp, "sl_pct": sl,
                              "rung_pct": rung, "source": "test",
                              "liq_pct": None, "liq_clamped": False,
                              "tp_floored": False, "tp_capped": False,
                              "rr": (tp / sl if sl else None),
                              "leverage": config.PER_PAIR_LEVERAGE.get(s)}
                          for s in syms})


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
    # The 2026-08-06 volatility cutover is a ONE-SHOT pass at the top of poll_fills
    # that cancels every resting rung and re-rests every stop. Left armed it would
    # rewrite the book out from under every poll_fills test in the suite. Inert by
    # default, exactly like the knobs above; test_vol_distances turns it on.
    monkeypatch.setattr(config, "VOL_MIGRATE_ENABLED", False)


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
def _no_real_kraken(monkeypatch):
    """HARD WALL: no test may reach the real Kraken API. The rate limit is
    per-ACCOUNT, so a test process's calls throttle the LIVE bot blind (it
    happened 2026-07-19 from a scratch verification walk, and again 2026-07-30
    from this very suite — TradeBalance/QueryOrders/OpenPositions storms while
    the bot was mid-cycle). Both broker.private and rest_client.fetch_json go
    through urllib.request.urlopen, resolved at call time, so one guard covers
    the public and private surfaces. A test that trips this must mock
    broker/rest_client at the layer it exercises — never widen this guard."""
    import urllib.request

    real_urlopen = urllib.request.urlopen

    def _guarded(req, *a, **kw):
        url = req if isinstance(req, str) else getattr(req, "full_url", str(req))
        if "kraken.com" in url:
            raise AssertionError(
                f"test tried to reach the REAL Kraken API ({url}) — the rate "
                "limit is per-account and throttles the live bot; mock "
                "broker/rest_client instead")
        return real_urlopen(req, *a, **kw)

    monkeypatch.setattr(urllib.request, "urlopen", _guarded)

    # Blocking a call must not cost wall-clock: fetch_json retries through
    # 2s/4s backoff sleeps before giving up, which taxed every on-fill
    # _live_last at ~6s per ladder test once the wall went up. Rebind
    # rest_client's OWN time reference so its sleeps are free; the real time
    # module (and every other module's sleeps) are untouched.
    import time as _time

    from deepfield import rest_client
    monkeypatch.setattr(rest_client, "time", types.SimpleNamespace(
        sleep=lambda _s: None, time=_time.time, monotonic=_time.monotonic))


@pytest.fixture(autouse=True)
def _isolate_executor_module_state(monkeypatch):
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
    monkeypatch.setattr(ex_mod, "_stack_pause_since", None)
    monkeypatch.setattr(ex_mod, "_tp_trough_noted", None)
    # 2026-08-10: _FLOOR_SEEN/_SURPLUS_SEEN moved to module level (the per-cycle
    # Executor rebuild was resetting them in production — the floor dedup never
    # engaged, the adoption clock never aged). Fresh dicts per test, or the floor
    # keys and adoption clocks of one test leak into the next — the exact roaming
    # cross-test contamination this fixture already prevents for _recon_next.
    monkeypatch.setattr(ex_mod, "_FLOOR_SEEN", {})
    monkeypatch.setattr(ex_mod, "_SURPLUS_SEEN", {})
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
