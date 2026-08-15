"""The $24 that tripped the kill switch (2026-08-15), and the -$72.11 that the
first fix invented. Two incidents, one afternoon, both pinned here:

1. A prop-eval fee purchase (ledger type 'transfer', subtype
   proptradingplanpurchase, ZUSD -24) was invisible to the deposit/withdrawal
   walk — the equity drop read as pure drawdown and latched the kill switch.

2. The first extension walked once per type and passed 'spend'/'receive' —
   values Kraken's Ledgers API does not recognize. Kraken IGNORES an unknown
   type filter and returns the WHOLE window, so the fee was counted three times
   and rollover FEES leaked in as flows: net -$72.11 against a true -$24.00,
   over-shifting the kill-switch peak and T/P baseline. The rewrite walks ONCE,
   unfiltered, and classifies client-side.

The fake Kraken here reproduces the venue's actual filter behavior (unknown
type -> unfiltered results), so the multi-type design FAILS these tests and the
single-walk design passes — the tests pin the mechanism, not a tautology.
"""
import datetime as dt

import pytest

from deepfield import broker, store
from deepfield import app as app_mod

# The real 2026-08-15 window, shrunk: the fee transfer + internal entries that
# must be classified OUT (their fees are the rollover poll's business).
WINDOW = {
    "LF1": {"type": "rollover", "asset": "ZUSD", "amount": "0.0000", "fee": "0.0020"},
    "LF2": {"type": "transfer", "subtype": "proptradingplanpurchase",
            "asset": "ZUSD", "amount": "-24.0000", "fee": "0.0000"},
    "LF3": {"type": "rollover", "asset": "ZUSD", "amount": "0.0000", "fee": "0.0047"},
    "LF4": {"type": "margin", "asset": "ZUSD", "amount": "0.0000", "fee": "0.0274"},
    "LF5": {"type": "trade", "asset": "ZUSD", "amount": "-12.3400", "fee": "0.0100"},
}


def _kraken_like_private(requested):
    """Faithful on the property that caused the bug: an unknown or absent type
    filter returns the whole window; a known type returns only that type."""
    known = ("deposit", "withdrawal", "trade", "margin", "rollover", "transfer",
             "credit", "settled", "staking")

    def fake_private(path, params, **kw):
        assert path == "/0/private/Ledgers"
        typ = params.get("type")
        requested.append(typ)
        if int(params.get("ofs", 0)) > 0:
            return {"ledger": {}}
        if typ is None or typ not in known:
            return {"ledger": dict(WINDOW)}          # Kraken ignores the filter
        return {"ledger": {k: v for k, v in WINDOW.items() if v["type"] == typ}}
    return fake_private


def test_eval_fee_transfer_nets_exactly_minus_24(monkeypatch):
    """The incident window must net -24.00: the transfer counted ONCE, rollover
    and margin fees NOT counted, the trade entry NOT counted."""
    requested = []
    monkeypatch.setattr(broker, "private", _kraken_like_private(requested))
    monkeypatch.setattr(broker, "LEDGERS_PAGE_PACE_SECS", 0)
    net, count, complete = broker.external_flows_since(1786829700)
    assert net == pytest.approx(-24.0), (
        f"window netted {net}, not -24.00 — either the transfer is invisible "
        f"(the original gap) or entries were double-counted / internal fees "
        f"leaked in (the -$72.11 bug)")
    assert count == 1 and complete
    assert len(requested) == 1 and requested[0] is None, (
        f"walked {len(requested)} typed queries {requested} — the multi-type walk "
        f"is what triple-counted the fee when Kraken ignored unknown filters")


def test_usdt_transfer_valued_one_to_one(monkeypatch):
    monkeypatch.setattr(broker, "private", _kraken_like_private([]))
    monkeypatch.setattr(broker, "LEDGERS_PAGE_PACE_SECS", 0)
    win = {"L1": {"type": "transfer", "asset": "USDT", "amount": "-20.0", "fee": "0"}}
    monkeypatch.setattr(broker, "private",
                        lambda path, params, **kw: {"ledger": win if int(params.get("ofs", 0)) == 0 else {}})
    net, count, complete = broker.external_flows_since(1786829700)
    assert net == pytest.approx(-20.0), (
        f"USDT transfer contributed {net} — stables must value 1:1, not $0")


def test_non_stable_asset_still_contributes_zero(monkeypatch):
    win = {"L1": {"type": "transfer", "asset": "XXBT", "amount": "-0.0003", "fee": "0"}}
    monkeypatch.setattr(broker, "private",
                        lambda path, params, **kw: {"ledger": win if int(params.get("ofs", 0)) == 0 else {}})
    monkeypatch.setattr(broker, "LEDGERS_PAGE_PACE_SECS", 0)
    net, count, complete = broker.external_flows_since(1786829700)
    assert net == 0.0 and count == 1


class _StubBroker:
    """Poll-level stub: the walk already summed to -24 (the real fee), and the
    account's eb moved 205.39 -> 181.32 over the window (the real readings)."""
    flow = (-24.0, 1, True)

    @classmethod
    def external_flows_since(cls, start_ts):
        return cls.flow

    @staticmethod
    def trade_balance_full():
        return {"eb": "181.3194"}


def _seeded_conn(tmp_path, anchor="205.3862"):
    conn = store.connect(str(tmp_path / "flows_test.db"))
    store.meta_set(conn, "flows_cursor", "1786829700.0")
    store.meta_set(conn, "flows_eb_anchor", anchor)
    store.meta_set(conn, "peak_equity", "226.7062")
    store.meta_set(conn, "tp_baseline", "289.8299")
    store.meta_set(conn, "tp_trough", "173.7825")
    return conn


def test_poll_shifts_peak_by_transfer_flow(tmp_path):
    """The incident replay: -$24 external flow, reconciling against the account's
    own -$24.07 move, must shift the kill-switch peak (and baseline, and trough)
    so the switch measures trading drawdown only."""
    conn = _seeded_conn(tmp_path)
    app_mod._poll_external_flows(conn, _StubBroker, dt.datetime.now(dt.timezone.utc))
    assert float(store.meta_get(conn, "peak_equity")) == pytest.approx(202.7062), (
        "peak did not shift — a $24 purchase would still read as pure drawdown")
    assert float(store.meta_get(conn, "tp_baseline")) == pytest.approx(265.8299)
    assert float(store.meta_get(conn, "tp_trough")) == pytest.approx(149.7825)


class _TripleCountBroker(_StubBroker):
    """The db3a9a6 bug's exact numbers: ledger claims -72.11 across 27 entries
    while the account only moved -24.07."""
    flow = (-72.11, 27, True)


def test_triple_count_shift_refused(tmp_path, monkeypatch):
    """The permanent version of the hand-check that caught the incident: a ledger
    flow the account's own eb delta cannot corroborate (gap $48 > max($2, 5%))
    must be REFUSED — no yardstick moves, the cursor stays put for re-check, and
    the operator is paged."""
    from deepfield import alerter
    conn = _seeded_conn(tmp_path)
    paged = []
    monkeypatch.setattr(alerter, "fire_safety",
                        lambda kind, sym, msg, loud=None: paged.append((kind, msg)))
    app_mod._poll_external_flows(conn, _TripleCountBroker, dt.datetime.now(dt.timezone.utc))
    assert float(store.meta_get(conn, "peak_equity")) == pytest.approx(226.7062), (
        "peak shifted on an unreconcilable flow — the -$72.11 corruption again")
    assert float(store.meta_get(conn, "tp_baseline")) == pytest.approx(289.8299)
    assert float(store.meta_get(conn, "tp_trough")) == pytest.approx(173.7825)
    assert store.meta_get(conn, "flows_cursor") == "1786829700.0", (
        "cursor advanced past a refused window — the flow is now permanently lost")
    assert paged and paged[0][0] == "flow-mismatch" and "REFUSED" in paged[0][1]


def test_reconciliation_tolerance_scales_with_flow(tmp_path, monkeypatch):
    """A large flow is allowed 5% slack: -$500 ledger vs -$490 account (gap $10
    < $25 tol) passes; the same $10 gap on a -$30 flow (tol $2) refuses."""
    from deepfield import alerter
    monkeypatch.setattr(alerter, "fire_safety", lambda *a, **k: None)

    class BigFlow(_StubBroker):
        flow = (-500.0, 2, True)

        @staticmethod
        def trade_balance_full():
            return {"eb": str(205.3862 - 490.0)}
    conn = _seeded_conn(tmp_path)
    app_mod._poll_external_flows(conn, BigFlow, dt.datetime.now(dt.timezone.utc))
    # the shift APPLIED: a withdrawal bigger than the peak clears it to 0 by the
    # documented floor (executor re-seeds from live equity) — the gate let it through
    assert float(store.meta_get(conn, "peak_equity")) == 0.0

    class SmallFlowBigGap(_StubBroker):
        flow = (-30.0, 1, True)

        @staticmethod
        def trade_balance_full():
            return {"eb": str(205.3862 - 20.0)}
    conn2 = store.connect(str(tmp_path / "flows_test2.db"))
    store.meta_set(conn2, "flows_cursor", "1786829700.0")
    store.meta_set(conn2, "flows_eb_anchor", "205.3862")
    store.meta_set(conn2, "peak_equity", "226.7062")
    store.meta_set(conn2, "tp_baseline", "289.8299")
    app_mod._poll_external_flows(conn2, SmallFlowBigGap, dt.datetime.now(dt.timezone.utc))
    assert float(store.meta_get(conn2, "peak_equity")) == pytest.approx(226.7062)


class _StubEx:
    """rails_detail flips to ok once the (stubbed) poll has shifted the peak."""
    def __init__(self):
        self.calls = 0

    def rails_detail(self, equity):
        self.calls += 1
        return {"ok": True, "reason": "ok"}


def test_prelatch_recheck_polls_once_and_unlatches(tmp_path, monkeypatch):
    conn = _seeded_conn(tmp_path)
    polled = []
    monkeypatch.setattr(app_mod, "_poll_external_flows",
                        lambda c, b, now: polled.append(now))
    ex = _StubEx()
    blocked = {"ok": False, "reason": "KILL SWITCH: equity $180.10 < 80% of peak $226.71"}
    fresh = app_mod._kill_switch_flow_recheck(conn, ex, 180.10, blocked)
    assert polled, "newly-latching kill switch did not trigger the immediate flow poll"
    assert fresh["ok"], "flow-explained trip still latched"


def test_prelatch_recheck_skips_standing_block_and_other_reasons(tmp_path, monkeypatch):
    conn = _seeded_conn(tmp_path)
    polled = []
    monkeypatch.setattr(app_mod, "_poll_external_flows",
                        lambda c, b, now: polled.append(now))
    ex = _StubEx()
    # standing block: rails_block_since already set -> no poll (one walk per trip)
    store.meta_set(conn, "rails_block_since", "2026-08-15T21:40:27+00:00")
    blocked = {"ok": False, "reason": "KILL SWITCH: equity $180.10 < 80% of peak $226.71"}
    out = app_mod._kill_switch_flow_recheck(conn, ex, 180.10, blocked)
    assert not polled and out is blocked
    # non-kill-switch reason -> no poll (flows cannot explain a MAX_OPEN block)
    store.meta_set(conn, "rails_block_since", "")
    other = {"ok": False, "reason": "max open positions (300/300)"}
    out = app_mod._kill_switch_flow_recheck(conn, ex, 180.10, other)
    assert not polled and out is other
