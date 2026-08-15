"""The $20 that tripped the kill switch (2026-08-15): a prop-eval fee purchase
left the account as a ledger type the flow walk never queried, so the equity
drop read as pure drawdown and latched the switch. Operator-approved fix:
extend the typed Ledgers walk (transfer/spend/receive + USD-stable 1:1) and
recheck flows once before NEWLY latching a kill-switch block.

The fake Kraken here is faithful on the one property that matters: a typed
Ledgers query returns ONLY entries of that type. The old two-type walk never
asks for 'transfer', so under it the fee nets $0 and these tests FAIL — the
fix is what they pin, not a tautology.
"""
import datetime as dt

import pytest

from deepfield import broker, store
from deepfield import app as app_mod


def _fake_private_factory(entries_by_type, requested):
    def fake_private(path, params, **kw):
        assert path == "/0/private/Ledgers"
        typ = params["type"]
        requested.append(typ)
        if int(params.get("ofs", 0)) > 0:
            return {"ledger": {}}
        return {"ledger": entries_by_type.get(typ, {})}
    return fake_private


def test_transfer_entry_counts_as_external_flow(monkeypatch):
    """A -$20 ZUSD 'transfer' nets -20.0 — the exact shape of the eval fee."""
    requested = []
    monkeypatch.setattr(broker, "private", _fake_private_factory(
        {"transfer": {"L1": {"asset": "ZUSD", "amount": "-20.0000", "fee": "0.0000"}}},
        requested))
    monkeypatch.setattr(broker, "LEDGERS_PAGE_PACE_SECS", 0)
    net, count, complete = broker.external_flows_since(1786829700)
    assert net == pytest.approx(-20.0), (
        f"transfer entry contributed {net} — the typed walk never asked for it")
    assert count == 1 and complete
    assert set(requested) >= {"deposit", "withdrawal", "transfer", "spend", "receive"}, (
        f"walk only queried {set(requested)} — the type extension is missing")


def test_usdt_spend_valued_one_to_one(monkeypatch):
    """The fee may have routed through USDT — a stable 'spend' is a dollar out."""
    monkeypatch.setattr(broker, "private", _fake_private_factory(
        {"spend": {"L2": {"asset": "USDT", "amount": "-20.0000", "fee": "0.0000"}}}, []))
    monkeypatch.setattr(broker, "LEDGERS_PAGE_PACE_SECS", 0)
    net, count, complete = broker.external_flows_since(1786829700)
    assert net == pytest.approx(-20.0), (
        f"USDT spend contributed {net} — stables must value 1:1, not $0")


def test_non_stable_asset_still_contributes_zero(monkeypatch):
    monkeypatch.setattr(broker, "private", _fake_private_factory(
        {"transfer": {"L3": {"asset": "XXBT", "amount": "-0.0003", "fee": "0"}}}, []))
    monkeypatch.setattr(broker, "LEDGERS_PAGE_PACE_SECS", 0)
    net, count, complete = broker.external_flows_since(1786829700)
    assert net == 0.0 and count == 1


class _StubBroker:
    """Poll-level stub: the walk already summed to -20."""
    @staticmethod
    def external_flows_since(start_ts):
        return (-20.0, 1, True)


def _seeded_conn(tmp_path):
    conn = store.connect(str(tmp_path / "flows_test.db"))
    store.meta_set(conn, "flows_cursor", "1786829700.0")
    store.meta_set(conn, "peak_equity", "226.7062")
    store.meta_set(conn, "tp_baseline", "289.8299")
    store.meta_set(conn, "tp_trough", "173.7825")
    return conn


def test_poll_shifts_peak_by_transfer_flow(tmp_path):
    """The incident replay: -$20 external flow must shift the kill-switch peak
    (and baseline, and trough) so the switch measures trading drawdown only."""
    conn = _seeded_conn(tmp_path)
    app_mod._poll_external_flows(conn, _StubBroker, dt.datetime.now(dt.timezone.utc))
    assert float(store.meta_get(conn, "peak_equity")) == pytest.approx(206.7062), (
        "peak did not shift — a $20 purchase would still read as pure drawdown")
    assert float(store.meta_get(conn, "tp_baseline")) == pytest.approx(269.8299)
    assert float(store.meta_get(conn, "tp_trough")) == pytest.approx(153.7825)


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
