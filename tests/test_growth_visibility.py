"""The growth gates must be as visible as the rails — and no more alarming.

2026-08-07 audit, finding R1. The respend bucket held $0.0154 of a $40 burst, every
seed and every ladder rung was being refused, and the deck read

    CLEAR · clear to buy — all rails green

because the refusals were log.debug and no gate outside the four rails was ever
shipped to the page. Same failure class as 2026-08-05 (kill-switch freeze, healthy
deck), different gate. The fix is `executor.growth_detail` -> blob `growth` ->
deck `railgrowth`, and these tests pin the three properties that make it honest:

  1. TRUTH TRAVELS. Each gate's verdict reaches the blob through the real writer
     (`_persist_web_live`), the same long way round test_tp_target_parity takes —
     a regression anywhere in the path is caught, not just in the helper.
  2. ONE FORMULA. The display's "paced" verdict and the governor's real refusal
     read the bucket through the same accrual code (_respend_tokens_now). The
     drift guard here is the analog of test_rails_detail_agrees_with_rails_ok:
     if they ever disagree, the console is describing a different bot.
  3. NEVER CRY WOLF. PACED is informational — the governor doing its job. A
     missing measurement (no price, no pairs) must never render as a verdict,
     and posture gates (regime, stack) must outrank pacing on the page.
"""
import json

import pytest

from .conftest import pin_vol
from deepfield import app, config, store
from deepfield import executor as ex_mod

SYM = "BTC/USD"


@pytest.fixture
def conn(tmp_path):
    c = store.connect(str(tmp_path / "growth.db"))
    store.upsert_pair(c, "XXBTZUSD", SYM, "BTC", 0.00005, 0.5, 8)
    c.execute("INSERT INTO candles(pair,interval,ts,o,h,l,c,v,closed) "
              "VALUES(?,15,1786000000,100000,100000,100000,100000,1,1)", (SYM,))
    c.commit()
    yield c
    c.close()


@pytest.fixture
def ex(conn, monkeypatch):
    # The REAL roster shape — a list of dicts, exactly like config.py builds it.
    # The first draft patched PAIRS to a tuple of strings and thereby masked a
    # production TypeError (dict membership test against EXCLUDED_PAIRS) that
    # silently degraded the whole respend gate to None. A fixture that feeds the
    # code a more convenient world than production is how vacuous tests start.
    monkeypatch.setattr(config, "PAIRS", [
        {"rest": "XXBTZUSD", "wsname": "XBT/USD", "ws": SYM, "display": "BTC",
         "ordermin": 0.00005, "costmin": 0.5}])
    monkeypatch.setattr(config, "EXCLUDED_PAIRS", frozenset())
    monkeypatch.setattr(config, "RESPEND_BUDGET_USD_PER_HR", 5.0)
    monkeypatch.setattr(config, "RESPEND_BURST_USD", 40.0)
    monkeypatch.setattr(config, "SIZE_MULT", 2)
    e = ex_mod.Executor(conn)
    e.mode = "live"
    return e


def _bucket(conn, tokens):
    import time
    store.meta_set(conn, "respend_bucket",
                   json.dumps({"tokens": tokens, "updated": time.time()}))
    conn.commit()


def _blob_growth(conn, growth):
    """The dict the deck will read, taken through the real blob writer."""
    class _State:
        pairs, links, started_ts = {}, None, 0.0
        exec = {"growth_detail": growth}
    app._persist_web_live(conn, _State(), 223.30, 3.64, 219.0, None)
    return json.loads(store.meta_get(conn, "web_live"))["growth"]


# ── 1. truth travels: each gate reaches the blob ─────────────────────────────
# BTC min lot 0.00005 x $100000 = $5, x SIZE_MULT 2 = $10 min fundable notional.

def test_empty_bucket_ships_paced_through_the_real_writer(ex, conn):
    _bucket(conn, 0.01)
    g = _blob_growth(conn, ex.growth_detail())
    r = g["respend"]
    assert r["enabled"] and r["paced"] is True
    assert r["min_notional"] == 10.0
    assert r["eta_secs"] > 0, "a paced verdict must say when it unblocks"


def test_full_bucket_ships_not_paced(ex, conn):
    _bucket(conn, 40.0)
    g = _blob_growth(conn, ex.growth_detail())
    assert g["respend"]["paced"] is False


def test_bull_regime_ships_a_blocked_regime_gate(ex, conn, monkeypatch):
    monkeypatch.setattr(config, "ACCUMULATE_ONLY_IN_BEAR", True)
    monkeypatch.setattr(config, "NO_ACCUMULATE_REGIMES", ("BULL",))
    store.meta_set(conn, "regime", "BULL")
    _bucket(conn, 40.0)
    g = _blob_growth(conn, ex.growth_detail())
    assert g["regime"]["ok"] is False
    assert "BULL" in g["regime"]["reason"]


def test_bear_regime_ships_an_open_gate(ex, conn, monkeypatch):
    monkeypatch.setattr(config, "ACCUMULATE_ONLY_IN_BEAR", True)
    store.meta_set(conn, "regime", "BEAR")
    g = _blob_growth(conn, ex.growth_detail())
    assert g["regime"]["ok"] is True


def test_tp_flatten_ships_a_blocked_stack_gate(ex, conn):
    store.meta_set(conn, "tp_flatten_active", "1")
    g = _blob_growth(conn, ex.growth_detail())
    assert g["stack"]["ok"] is False
    assert "flatten" in g["stack"]["reason"].lower()


# ── 2. one formula: display paced == governor refusal ────────────────────────

def test_paced_verdict_agrees_with_the_governor_across_bucket_levels(ex, conn):
    """THE drift guard. For the minimum fundable notional ($10 here), the deck's
    `paced` and `_respend_budget_ok`'s refusal must agree at every bucket level —
    including both sides of the threshold. They share _respend_tokens_now, so the
    only way this fails is someone giving the display its own copy of the math,
    which is the exact defect (one rule, two implementations) this repo keeps
    paying for."""
    for tokens in (0.0, 4.99, 9.99, 10.01, 25.0, 40.0):
        _bucket(conn, tokens)
        paced = ex.growth_detail()["respend"]["paced"]
        _bucket(conn, tokens)          # budget_ok stamps the bucket on refusal
        ok, _why, _debit = ex._respend_budget_ok(10.0)
        assert paced == (not ok), (
            f"tokens={tokens}: deck says paced={paced}, governor says ok={ok} — "
            f"the console is describing a different bot")


# ── 3. never cry wolf ────────────────────────────────────────────────────────

def test_no_price_means_no_paced_verdict(ex, conn):
    """No local price -> min notional unknowable -> PACED must not be claimed,
    even with a bone-dry bucket. A missing measurement never renders as a
    verdict (rails_detail's own rule)."""
    conn.execute("DELETE FROM candles")
    conn.commit()
    _bucket(conn, 0.0)
    r = ex.growth_detail()["respend"]
    assert r["paced"] is False
    assert r["min_notional"] is None


def test_governor_off_ships_enabled_false(ex, conn, monkeypatch):
    monkeypatch.setattr(config, "RESPEND_BUDGET_USD_PER_HR", 0)
    assert ex.growth_detail()["respend"] == {"enabled": False}


def test_growth_never_raises_into_the_writer(ex, conn, monkeypatch):
    """growth_detail is display plumbing on the app loop — a failure inside any
    gate read must degrade to None for that gate, not kill the cycle."""
    monkeypatch.setattr(ex_mod.Executor, "_respend_tokens_now",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    g = ex.growth_detail()
    assert g["respend"] is None          # the broken gate degrades
    assert g["regime"] is not None       # the others still report


def test_server_ships_growth_verbatim(ex, conn, monkeypatch):
    """The server is transport, not derivation: whatever growth dict the blob
    holds must arrive in the page payload unmodified — including through a STALE
    blob, same reasoning as rails (a stale PACED beats silence)."""
    from deepfield.web import server
    _bucket(conn, 0.01)
    g = ex.growth_detail()
    _blob_growth(conn, g)                       # writes web_live via the real writer
    out = server._assemble(conn)
    assert out["health"]["growth"] == json.loads(json.dumps(g)), \
        "server dropped or re-derived the growth section"


# ── 4. the deck renders, never recomputes ────────────────────────────────────

def test_deck_reads_growth_and_does_not_do_bucket_math():
    """Grep guard in the house style: the page may format growth values, never
    derive them. Any appearance of the accrual inputs (rate/3600, burst cap
    arithmetic) in deck.html is a second implementation of the governor."""
    import os
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "deepfield", "web", "deck.html")).read()
    assert "h.growth" in src, "the deck never reads the growth section"
    assert "railgrowth" in src, "there is nowhere to render it"
    assert "respend_bucket" not in src, \
        "the page is reading the raw bucket — that is the governor's private state"
    assert "rate_per_hr" not in src.replace("g.respend.rate_per_hr", ""), \
        "accrual arithmetic on rate_per_hr has leaked into the page"
    assert "eta_secs" in src, "the ETA must come from the executor, not the page"


def test_deck_stale_fallback_is_last_equity_not_peak():
    """R4: with a stale blob the headline equity must fall back to the LAST
    RECORDED equity sample, not peak_equity — peak is a ratcheted all-time high,
    guaranteed wrong in a drawdown, which is the one moment the fallback shows."""
    import os, re
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "deepfield", "web", "deck.html")).read()
    m = re.search(r"const eq=eqLive\?h\.equity:([^;]+);", src)
    assert m, "renderCapital's fallback expression has moved — re-pin it"
    fb = m.group(1)
    assert "es" in fb and fb.index("es") < fb.index("peak"), \
        f"stale fallback is {fb!r} — the series tail must be tried before peak"
    # ...and `es` must actually be the series tail, defined from h.equity_series
    # in the same function, not some other alias.
    fn = src[src.index("function renderCapital"):src.index("const eq=eqLive")]
    assert "h.equity_series" in fn, \
        "the `es` fallback no longer reads h.equity_series — re-pin the source"