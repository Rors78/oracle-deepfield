"""The respend pre-gate and the authoritative respend check must agree.

One rule, two implementations — the recurring defect class in this codebase, and
this is the money-adjacent instance of it. `_respend_would_refuse` is a cheap
estimate of what `_respend_budget_ok` will decide on the real sizing. Whenever the
estimate is OPTIMISTIC, the caller narrates an order it will not place and then
swallows the refusal, because the refusal used to log at DEBUG and DEBUG is off in
production (the live 15MB log contains zero DEBUG lines).

The specific drift, found 2026-08-06 by reading the live log rather than by any
test: the pre-gate applied `* (1 - LADDER_STEP_PCT)` internally. That is correct
for the reladder caller, whose rung really does price one step below the anchor
fill — and wrong for the seed caller, which opens a chain AT live and takes no step
at all. So every seed's bound came out a full ladder step (1%) under its true cost.

Why a 1% error produced a 72% failure rate rather than a rare one: the bucket is
chronically starved, so tokens approach any threshold from BELOW. The pre-gate
fired the instant tokens crossed the LOW bound, while the authoritative check 1%
higher still refused. At $5/hr, 1% of a ~$6.30 order is ~45 seconds of accrual, and
the exec loop polls every ~8s — so the crossing was sampled inside the dead band
essentially every time. Measured on the live log over 2026-07-25..08-06: 198 of 275
seed announcements (72%) placed nothing and logged no reason at all. DOT, AVAX and
RENDER each tried to open a chain all night and never once reached the exchange.

These tests pin the contract directly — pre-gate says proceed => the real check
must fund it — by sweeping the bucket across the boundary rather than asserting one
hand-picked value, since the bug lived entirely in a narrow band a point sample
walks straight past.
"""
import json
import logging
import time

import pytest

from deepfield import config, store, executor as ex_mod

SYM = "BTC/USD"


def _conn(tmp_path, ordermin=0.1, costmin=0.5, lot_dec=8):
    conn = store.connect(str(tmp_path / "r.db"))
    store.upsert_pair(conn, "XXBTZUSD", SYM, "BTC", ordermin, costmin, lot_dec)
    return conn


def _exec(conn, mode="live"):
    e = ex_mod.Executor(conn)
    e.mode = mode
    return e


def _bucket(conn, tokens):
    store.meta_set(conn, "respend_bucket",
                   json.dumps({"tokens": tokens, "updated": time.time()}))


def _governor(monkeypatch, rate=5.0, burst=40.0, step=0.01, smult=2.0):
    monkeypatch.setattr(config, "RESPEND_BUDGET_USD_PER_HR", rate)
    monkeypatch.setattr(config, "RESPEND_BURST_USD", burst)
    monkeypatch.setattr(config, "LADDER_STEP_PCT", step)
    monkeypatch.setattr(config, "SIZE_MULT", smult)


def _real_notional(e, price):
    """What the authoritative path will actually meter: size() at the SAME price the
    caller hands the pre-gate. Not re-derived here — calling the real sizer is the
    point, so a change in sizing shows up as a parity failure instead of being
    mirrored into this file and silently agreed with."""
    s = e.size(SYM, entry=price, stop=price * 0.9, leverage=10, equity=1000.0, card=None)
    assert s is not None
    return s["notional"]


# ── the contract ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("price", [99.0, 100.0, 6.4060, 0.8155, 211.98])
def test_pregate_never_passes_what_the_real_check_refuses(tmp_path, monkeypatch, price):
    """THE invariant. Swept across the boundary in fine steps: at every token level
    where the pre-gate says "proceed", the authoritative check must fund the order.

    A pre-gate that is too STRICT is merely a missed opportunity (and is caught by
    the companion test below). A pre-gate that is too LOOSE is the bug: it produces a
    narrated order that never exists."""
    conn = _conn(tmp_path)
    _governor(monkeypatch)
    e = _exec(conn)
    need = _real_notional(e, price)
    for i in range(240):
        tokens = need * (0.90 + i * 0.001)          # sweep 90% -> 114% of the true cost
        _bucket(conn, tokens)
        if e._respend_would_refuse(SYM, price):
            continue                                 # skipped — allowed, checked below
        _bucket(conn, tokens)                        # the refuse path rewrites meta
        ok, why, _debit = e._respend_budget_ok(need)
        assert ok, (f"pre-gate passed at ${tokens:.4f} but the real check refused "
                    f"${need:.4f} ({why}) — a dangling announcement")
    conn.close()


@pytest.mark.parametrize("price", [99.0, 100.0, 6.4060])
def test_pregate_never_skips_what_the_real_check_would_fund(tmp_path, monkeypatch, price):
    """The other direction, which the pre-gate's docstring promises: it may only skip
    when the bucket provably cannot fund the smallest order. Otherwise the governor
    would throttle harder than RESPEND_BUDGET_USD_PER_HR configures, which is exactly
    the failure the 07-27 debit-accrual audit chased."""
    conn = _conn(tmp_path)
    _governor(monkeypatch)
    e = _exec(conn)
    need = _real_notional(e, price)
    for i in range(240):
        tokens = need * (0.90 + i * 0.001)
        _bucket(conn, tokens)
        skipped = e._respend_would_refuse(SYM, price)
        _bucket(conn, tokens)
        ok, _why, _debit = e._respend_budget_ok(need)
        if ok:
            assert not skipped, (f"pre-gate skipped ${tokens:.4f} that the real check "
                                 f"would have funded (${need:.4f}) — over-throttling")
    conn.close()


def test_the_seed_price_basis_is_live_not_a_ladder_step_down(tmp_path, monkeypatch):
    """The regression itself, stated as a number.

    A seed opens the chain at live; a rung sits one ladder step below its anchor.
    Hand the pre-gate the same reference and it must price those DIFFERENTLY — if it
    silently discounts a step, the seed's bound lands 1% light and the dead band
    reopens. Pinned as an inequality against the real sizer, so it fails if the
    discount is ever restored inside the pre-gate."""
    conn = _conn(tmp_path)
    _governor(monkeypatch)
    e = _exec(conn)
    live = 100.0
    seed_cost = _real_notional(e, live)                              # sized AT live
    rung_cost = _real_notional(e, live * (1 - config.LADDER_STEP_PCT))
    assert rung_cost < seed_cost, "a rung one step down must cost less than a seed"

    # Fund exactly the rung, a hair under the seed. The seed must be refused; if the
    # pre-gate still applied the step internally it would pass here — the live bug.
    _bucket(conn, rung_cost + 1e-6)
    assert e._respend_would_refuse(SYM, live) is True, \
        "seed pre-gate passed on a bucket that only funds a laddered-down rung"
    assert e._respend_would_refuse(SYM, live * (1 - config.LADDER_STEP_PCT)) is False, \
        "rung pre-gate must still pass on a bucket that funds the rung"
    conn.close()


# ── the silence ──────────────────────────────────────────────────────────────

def test_a_respend_refusal_is_visible_at_info(tmp_path, monkeypatch, caplog):
    """The refusal explains an intention already narrated at INFO, so it must be
    readable at INFO. At DEBUG it was invisible in production and the seed
    announcement read as an order that vanished into nothing."""
    conn = _conn(tmp_path)
    _governor(monkeypatch)
    monkeypatch.setattr(config, "EXCLUDED_PAIRS", ())
    e = _exec(conn)
    monkeypatch.setattr(e, "_accumulation_allowed", lambda: (True, ""))
    monkeypatch.setattr(e, "rails_ok", lambda equity: (True, ""))
    monkeypatch.setattr(e, "portfolio_value", lambda: 1000.0)
    _bucket(conn, 0.0)                                   # bucket empty — must refuse
    with caplog.at_level(logging.INFO, logger="deepfield.exec"):
        out = e.place_entry(SYM, 100.0, card=None, respend=True)
    assert out is None, "an unfunded entry must not be placed"
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert any("respend paced" in m for m in msgs), \
        f"the refusal must say why at INFO; got {msgs}"
    conn.close()


def test_seed_does_not_announce_an_order_it_cannot_fund(tmp_path, monkeypatch, caplog):
    """End-to-end, through the real _seed_chains loop: an empty bucket must produce
    NO "starting ladder with a post-only bid" line. That sentence is a statement that
    an order is being placed, and 198 times in 12 days it was not true."""
    conn = _conn(tmp_path)
    _governor(monkeypatch)
    monkeypatch.setattr(config, "SEED_PAIRS", (SYM,))
    e = _exec(conn)
    monkeypatch.setattr(e, "_armed", lambda: True)
    monkeypatch.setattr(e, "_stack_margin_ok", lambda: (True, ""))
    monkeypatch.setattr(e, "_last_local_price", lambda sym: 100.0)
    placed = []
    monkeypatch.setattr(e, "place_entry",
                        lambda *a, **k: placed.append(a) or None)
    # A fresh tick would cost a REST call; the pre-gate must refuse before that.
    monkeypatch.setattr(e, "_live_last",
                        lambda sym: pytest.fail("ticker fetched despite an empty bucket"))
    _bucket(conn, 0.0)
    with caplog.at_level(logging.INFO, logger="deepfield.exec"):
        e._seed_chains()
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("no chain working" in m for m in msgs), \
        f"seed announced a bid it could not fund: {msgs}"
    assert not placed
    conn.close()


def test_seed_announces_and_places_when_the_bucket_can_fund_it(tmp_path, monkeypatch, caplog):
    """The converse — otherwise a pre-gate that refuses everything would pass every
    test above while silently switching accumulation off."""
    conn = _conn(tmp_path)
    _governor(monkeypatch)
    monkeypatch.setattr(config, "SEED_PAIRS", (SYM,))
    e = _exec(conn)
    monkeypatch.setattr(e, "_armed", lambda: True)
    monkeypatch.setattr(e, "_stack_margin_ok", lambda: (True, ""))
    monkeypatch.setattr(e, "_last_local_price", lambda sym: 100.0)
    monkeypatch.setattr(e, "_live_last", lambda sym: 100.0)
    placed = []
    monkeypatch.setattr(e, "place_entry", lambda *a, **k: placed.append(a) or 1)
    _bucket(conn, 40.0)                                  # burst-full: funds any min order
    with caplog.at_level(logging.INFO, logger="deepfield.exec"):
        e._seed_chains()
    assert any("no chain working" in r.getMessage() for r in caplog.records)
    assert placed, "a funded seed must actually reach place_entry"
    conn.close()


def test_a_stale_local_price_cannot_produce_a_dangling_announcement(tmp_path, monkeypatch, caplog):
    """The staleness half of the same hole. The cheap gate prices off a 15m candle
    close that can be a quarter-hour old; if price ran up since, the real sizing costs
    more than the bound just cleared. The seed re-checks on the fresh tick — already
    paid for — before it narrates."""
    conn = _conn(tmp_path)
    _governor(monkeypatch)
    monkeypatch.setattr(config, "SEED_PAIRS", (SYM,))
    e = _exec(conn)
    monkeypatch.setattr(e, "_armed", lambda: True)
    monkeypatch.setattr(e, "_stack_margin_ok", lambda: (True, ""))
    monkeypatch.setattr(e, "_last_local_price", lambda sym: 100.0)   # stale, cheap
    monkeypatch.setattr(e, "_live_last", lambda sym: 130.0)          # ran up 30%
    placed = []
    monkeypatch.setattr(e, "place_entry", lambda *a, **k: placed.append(a) or 1)
    # Funds the stale-priced min order but not the fresh-priced one.
    _bucket(conn, _real_notional(e, 100.0) + 1e-6)
    with caplog.at_level(logging.INFO, logger="deepfield.exec"):
        e._seed_chains()
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("no chain working" in m for m in msgs), \
        f"announced on a stale price the fresh tick cannot fund: {msgs}"
    assert not placed
    conn.close()
