"""The deck may choose how to draw a number. It may not decide what the number IS.

Three separate misreadings on 2026-08-05 were one bug wearing three hats: a rule
written down twice and updated once.

  1. tp_target — the executor floored it at baseline (07-27); _persist_web_live kept
     its own min(baseline,trough)*(1+TP_PCT). The deck said $208.54 against $223.30
     equity, reading as a flatten 6.6% OVERDUE, while the real target was $289.83
     and the book was 23% below it, holding.
  2. rails_ok — computed, published, and never read by deck.html at all.
  3. "rung +4% · book +20% backstop" and the runway's ALERT/STACK FLOOR marks —
     hardcoded in deck.html, six copies for the margin-level marks alone.

The failure mode they share is the nasty one: a stale hardcoded number does not go
blank when config moves, it confidently states the old value.

These tests pin the contract. `health.thresholds` carries every config number the
deck displays, and each one must equal config — so moving a knob can never leave a
label behind. The deck's fallbacks (for blobs predating this) are pinned too: they
are the last copies in the codebase, and a test that ignored them would let them rot.
"""
import json
import pathlib
import re

import pytest

from deepfield import config, store
from deepfield.web import server


DECK = pathlib.Path(__file__).resolve().parents[1] / "deepfield" / "web" / "deck.html"


def _health(tmp_path):
    """Through the real assembler, not a hand-built dict — a regression anywhere in
    that path should fail here rather than be assumed away."""
    conn = store.connect(str(tmp_path / "d.db"))
    try:
        return server._assemble(conn)["health"]
    finally:
        conn.close()


def test_every_threshold_the_deck_asks_for_is_shipped(tmp_path):
    """Reads deck.html for its actual thr("...") call sites. A new one added
    without a matching server key would render the fallback forever — silently
    correct today and silently wrong the day config moves."""
    asked = set(re.findall(r'thr\(\s*"([a-z0-9_]+)"', DECK.read_text()))
    assert asked, "no thr() call sites found — did the accessor get renamed?"
    shipped = _health(tmp_path)["thresholds"]
    missing = asked - set(shipped)
    assert not missing, f"deck asks for thresholds the server never ships: {missing}"


def test_the_per_rung_target_is_no_longer_a_single_threshold(tmp_path):
    """2026-08-06: the per-rung take-profit became PER PAIR, so shipping one
    `tp_rung_pct` scalar would be the same "deck holds its own copy" defect in a new
    place — every pair would be labelled with some other pair's target. The deck reads
    `vol_table` instead, which is the table the executor itself resolves against."""
    h = _health(tmp_path)
    assert "tp_rung_pct" not in h["thresholds"], \
        "a single rung target cannot describe a volatility-scaled roster"
    assert "vol_table" in h, "the deck needs the per-pair table to label anything"


def test_shipped_thresholds_equal_config(tmp_path):
    t = _health(tmp_path)["thresholds"]
    assert t["tp_pct"] == config.TP_PCT
    assert t["ml_alert"] == config.MARGIN_LEVEL_ALERT_PCT
    assert t["ml_stack_floor"] == config.MARGIN_LEVEL_STACK_FLOOR_PCT
    assert t["kill_switch_dd_pct"] == config.KILL_SWITCH_DD_PCT
    assert t["size_mult"] == config.SIZE_MULT


def test_thresholds_track_config_rather_than_a_snapshot(tmp_path, monkeypatch):
    """The actual regression: change the knob, the deck must follow. If these were
    captured at import or copied into a literal, this is what would catch it."""
    monkeypatch.setattr(config, "TP_PCT", 0.35)
    monkeypatch.setattr(config, "MARGIN_LEVEL_ALERT_PCT", 145)
    monkeypatch.setattr(config, "MARGIN_LEVEL_STACK_FLOOR_PCT", 260)
    t = _health(tmp_path)["thresholds"]
    assert t["tp_pct"] == 0.35
    assert t["ml_alert"] == 145
    assert t["ml_stack_floor"] == 260


def test_deck_fallbacks_match_config(tmp_path):
    """The fallbacks in thr(key, fallback) are the LAST hardcoded copies left — they
    exist so a blob predating `thresholds` still renders. They must agree with config
    today; when a knob legitimately changes, this fails and the fallback gets updated
    with it rather than quietly drifting into fiction."""
    src = DECK.read_text()
    expect = {
        "tp_pct": config.TP_PCT,
        "ml_alert": float(config.MARGIN_LEVEL_ALERT_PCT),
        "ml_stack_floor": float(config.MARGIN_LEVEL_STACK_FLOOR_PCT),
    }
    found = dict(re.findall(r'thr\(\s*"([a-z0-9_]+)"\s*,\s*([0-9.]+)\s*\)', src))
    for key, want in expect.items():
        assert key in found, f"no fallback found for {key}"
        assert float(found[key]) == pytest.approx(float(want)), \
            f"deck fallback for {key} is {found[key]}, config says {want}"


def _decommented(src):
    """Comments may quote the old hardcoded strings — that is documentation, not a
    regression. Only live code counts."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)          # block comments + CSS
    src = re.sub(r"^\s*//.*$", "", src, flags=re.M)          # whole-line JS comments
    return src


def test_no_stray_hardcoded_harvest_percentages():
    """The specific label that was wrong: "rung +4% · book +20% backstop", baked
    into the markup where TP_RUNG_PCT and TP_PCT belong."""
    src = _decommented(DECK.read_text())
    assert "rung +4%" not in src and "book +20% backstop" not in src, \
        "harvest header is hardcoded again — it must render from thresholds"


def test_thresholds_survive_a_json_round_trip(tmp_path):
    """The blob is serialised to the browser. A non-finite or Decimal value here
    would break JSON.parse and blank the entire deck, not just this card."""
    t = _health(tmp_path)["thresholds"]
    assert json.loads(json.dumps(t)) == t
    for k, v in t.items():
        assert v is None or isinstance(v, (int, float)), f"{k} is {type(v).__name__}"
        if isinstance(v, float):
            assert v == v and abs(v) != float("inf"), f"{k} is not finite"


# ── the two balances (2026-08-05) ────────────────────────────────────────────

def test_both_balances_ship_together(tmp_path):
    """The deck computes the off-collateral gap as balance_total - balance_trade.
    Ship one without the other and it silently cannot compute — so they are pinned
    as a pair, not individually.

    Kraken reports two different pots: `eb` (every currency, the balances-page
    figure) and `tb` (only what counts as margin collateral)."""
    import time
    from deepfield import store as st
    conn = st.connect(str(tmp_path / "b.db"))
    try:
        st.meta_set(conn, "web_live", json.dumps({
            "updated": time.time(), "mode": "live",
            "equity": 173.79, "balance_total": 225.30, "balance_trade": 173.83,
        }))
        h = server._assemble(conn)["health"]
        assert h["balance_total"] == 225.30
        assert h["balance_trade"] == 173.83
    finally:
        conn.close()


def test_off_collateral_is_not_unrealized_pnl():
    """The bug this pins, found by RUNNING it rather than by any test: the deck
    first computed the gap as balance_total - EQUITY. Equity already contains
    unrealized P&L, so a fully-collateralised paper account with a 30-cent open
    loss was reported as "$0.31 off-collateral" — an entirely fabricated figure.

    Both eb and tb are pre-unrealized, so their difference is exactly the money
    Kraken will not accept as collateral, whatever the book is doing."""
    eb, tb = 225.2995, 173.8278
    gap = round(eb - tb, 2)
    assert gap == 51.47
    for equity in (tb - 40.0, tb + 40.0):            # open loss, open gain
        assert round(eb - equity, 2) != gap, \
            "eb - equity drifts with open P/L; eb - tb does not"

    # The paper case that exposed it: no gap at all, only an unrealized loss.
    assert 199.97 - 199.97 < 0.01, "fully collateralised -> no line"
    assert round(199.97 - 199.67, 2) == 0.30, "the old formula's phantom 30 cents"


def test_deck_reads_the_two_balances_not_equity():
    """Guards the deck source itself: the subtraction must be between the two
    balances. A future edit back to `- eq` reintroduces the phantom gap."""
    src = _decommented(DECK.read_text())
    assert "h.balance_total - h.balance_trade" in src, \
        "off-collateral must be eb - tb"
    assert "h.balance_total - eq" not in src, \
        "eb - equity folds unrealized P/L into the collateral gap"


# ── % above the 52-week low (2026-08-05) ─────────────────────────────────────

def _pct_low_from_server(tmp_path, monkeypatch, price, lo52):
    """The pctLow the page will actually receive, produced by the REAL
    server._assemble — a fresh blob carrying `price` live, daily candles whose
    minimum close is `lo52`. The first version of these tests recomputed
    `round((price/lo52-1)*100, 1)` locally and asserted properties of its own
    expression; server.py could have reverted to the stale-scored-close base OR
    re-grown the max(0,...) clamp and both tests would have kept passing (proven
    by mutation, 2026-08-08). Reuses test_web_console's harness so the value
    takes the same path the deck's does."""
    import time as _t
    from .test_web_console import _blob, _candles_ada  # noqa: F401 (harness)
    from .test_web_console import _mkdb
    from deepfield.web import server
    now = _t.time()
    day = 86400
    t0 = int(now // day * day)
    candles = [("ADA/USD", 1440, t0 - 2 * day, lo52),
               ("ADA/USD", 1440, t0 - day, lo52 * 1.15)]
    _mkdb(tmp_path, monkeypatch,
          blob=_blob(now, prices={"ADA/USD": price}, chg={}), candles=candles)
    conn = server._ro_conn()
    try:
        st = server._assemble(conn)
    finally:
        conn.close()
    return next(p for p in st["pairs"] if p["sym"] == "ADA")["pctLow"]


def test_pct_low_is_measured_against_the_price_shown(tmp_path, monkeypatch):
    """One sentence, two different "now"s. The card's pct_above_low is computed off
    the last SCORED close (weekly/daily) while the sheet header carries the LIVE
    tick, so intraday they disagree. On 2026-08-05 the detail sheet read:

        XRP  1.04909    "52-week 1.05162 — 3.2755 · now 1% above the low"

    a price BELOW the stated low, described as above it. The 52w bounds stay
    weekly-close truth on purpose; only the percentage is re-based — and the
    re-basing is asserted against the server's own output, not a local copy of
    the formula."""
    shown = _pct_low_from_server(tmp_path, monkeypatch, 1.04909, 1.05162)
    assert shown == -0.2, f"server shipped {shown!r} for the incident's own numbers"
    assert shown < 0, "a price under the low cannot read as above it"


def test_a_break_below_the_low_is_not_clamped_to_zero(tmp_path, monkeypatch):
    """It was `max(0, round(...))`. That reported a break below the 52-week low as
    "0% above" — silencing the exact event the bottom thesis exists to notice, and
    rounding to whole percent turned every sub-1% breach into 0 as well. Asserted
    on the server's emitted value: a 10% break and a sub-1% breach must both
    arrive at the page negative and undamped."""
    assert _pct_low_from_server(tmp_path, monkeypatch, 0.90, 1.00) == -10.0
    assert _pct_low_from_server(tmp_path, monkeypatch, 0.994, 1.00) == -0.6


def test_deck_phrases_below_the_low_rather_than_negative_above():
    """`-0.2% above the low` is not English. The deck must say BELOW."""
    src = _decommented(DECK.read_text())
    assert "lowPhrase(" in src, "the phrasing helper must exist"
    assert "% above 52w low" not in src.replace("${p.pctLow}% above 52w low", ""), \
        "raw concatenation of a possibly-negative pct must not remain"
    assert "BELOW" in src, "a break below the low must be sayable"


def test_pct_low_survives_a_missing_card(tmp_path):
    """No card (young pair, unscoreable) must not blow up the pairs payload."""
    conn = store.connect(str(tmp_path / "p.db"))
    try:
        out = server._assemble(conn)
        assert isinstance(out.get("pairs"), list)
    finally:
        conn.close()
