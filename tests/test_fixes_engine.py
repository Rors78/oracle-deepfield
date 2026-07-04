"""Named regression tests per F-item — engine-level (F3, F5, F8, F9, F10, F11, F12)."""
import logging
from logging.handlers import RotatingFileHandler

import pytest

from deepfield import engine
from deepfield import VERSION, USER_AGENT
from deepfield.signals import FIRED, NOT, NA
from deepfield.profiles import COMPAT, FULL
from deepfield.logsetup import setup_logging, MAX_BYTES, BACKUPS


# ── F3: young listing -> sig1 N/A, denom shrinks, required recomputed ───────
def _young_series(weekly_n=166, daily_n=400):
    wc = [100.0 + i * 0.1 for i in range(weekly_n)]
    weekly = (list(wc), [v + 1 for v in wc], [v - 1 for v in wc], list(wc), [50.0] * weekly_n)
    daily = ([100.0 + i * 0.05 for i in range(daily_n)],)
    return weekly, daily


def test_F3_young_listing_na_and_dynamic_required():
    weekly, daily = _young_series()
    full = engine.evaluate("SUI/USD", weekly, daily, FULL)
    compat = engine.evaluate("SUI/USD", weekly, daily, COMPAT)
    sig1_full = [r for r in full.results if r.slot == 1][0]
    sig1_compat = [r for r in compat.results if r.slot == 1][0]
    assert sig1_full.state == NA and "200" in sig1_full.reason
    assert sig1_compat.state == NOT            # v4.4: counted, not excluded
    assert full.denom == 6 and full.required == 4        # round(5/7*6)=4
    assert compat.denom == 7 and compat.required == 5    # v4.4 fixed
    # invariant 4: N/A never inflates — full score cannot exceed compat score here
    assert full.score <= compat.score + 0  # same fired set on slots 2..7


# ── F5: freshness measured to bar CLOSE, not open ───────────────────────────
def test_F5_freshness_to_close():
    interval = 1440  # daily
    # bar OPENED 1 day ago and closed 100s ago
    now = 1_000_000.0
    ts = now - interval * 60 - 100
    assert abs(engine.candle_close_age(ts, interval, now=now) - 100) < 1e-6
    assert engine.is_stale(200, 180) is True
    assert engine.is_stale(100, 180) is False


# ── F8: tranche floor + quantization + post-asserts ─────────────────────────
def test_F8_tranche_ordermin_floor_and_conviction():
    # DOGE-like: ordermin dominates; costmin/price tiny.
    qty, mult = engine.tranche(5, 5, ordermin=50, costmin=0.5, lot_decimals=8, price=0.1)
    assert mult == 1.0 and qty == 50 and qty * 0.1 >= 0.5
    # +2 conviction doubles
    qty2, mult2 = engine.tranche(7, 5, ordermin=50, costmin=0.5, lot_decimals=8, price=0.1)
    assert mult2 == 2.0 and qty2 == 100
    # BTC-like: ordermin floor holds, cost satisfied
    qtyb, _ = engine.tranche(5, 5, ordermin=0.00005, costmin=0.5, lot_decimals=8, price=60000)
    assert qtyb >= 0.00005 and qtyb * 60000 >= 0.5


# ── F9: regime uses 4-bar slope + RECOVERY (a state compat cannot emit) ─────
def test_F9_recovery_state_diverges_from_compat():
    wc = [100.0] * 200 + [98.0, 98.0, 98.0, 103.0]   # dip below EMA then rebound above
    dc = [float(i) for i in range(1, 40)]
    full = engine.regime(wc, dc, FULL)
    compat = engine.regime(wc, dc, COMPAT)
    assert full.label == "RECOVERY"        # above & 4-bar slope <= 0
    assert compat.label != "RECOVERY"      # compat has no RECOVERY branch


# ── F10: cooldown ledger predicate (REALERT_HOURS) ──────────────────────────
def test_F10_cooldown():
    now = 1_000_000.0
    assert engine.should_alert(None, now) is True
    assert engine.should_alert(now - 23 * 3600, now, 24) is False
    assert engine.should_alert(now - 25 * 3600, now, 24) is True


# ── F11: single VERSION constant, used in the User-Agent ────────────────────
def test_F11_single_version_in_user_agent():
    assert VERSION in USER_AGENT
    assert USER_AGENT == f"OracleDeepfield/{VERSION}"


# ── F12: RotatingFileHandler 5 MB x 3 ───────────────────────────────────────
def test_F12_rotating_log(tmp_path):
    h = setup_logging(debug=False, log_dir=str(tmp_path))
    assert isinstance(h, RotatingFileHandler)
    assert h.maxBytes == MAX_BYTES == 5 * 1024 * 1024
    assert h.backupCount == BACKUPS == 3
    assert logging.getLogger().level == logging.INFO
