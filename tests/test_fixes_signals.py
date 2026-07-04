"""Named regression tests per F-item — signal-level (F1, F2, F4, F13). SPEC §7."""
from deepfield import signals as sg
from deepfield import engine
from deepfield.signals import FIRED, NOT, NA
from deepfield.profiles import COMPAT, FULL


# ── F1: sig5 requires 3 CONSECUTIVE lower closes, not v4.4's loose OR ────────
def test_F1_sig5_tightened():
    # up-close after only a broken downtrend: compat fires, full does not.
    wc = [10, 12, 9, 11, 10, 12]   # wc[-1]=12>10; wc[-3]=11>wc[-4]=9 breaks run
    assert sg.sig5_first_up_close(wc, COMPAT).state == FIRED
    assert sg.sig5_first_up_close(wc, FULL).state == NOT
    assert sg.sig5_first_up_close(wc, COMPAT).name == "W-First Higher High"
    assert sg.sig5_first_up_close(wc, FULL).name == "W-First Up Close"


def test_F1_sig5_three_consecutive_fires_both():
    wc = [20, 14, 12, 10, 8, 9]    # 8<10<12<14 then up to 9
    assert sg.sig5_first_up_close(wc, FULL).state == FIRED
    assert sg.sig5_first_up_close(wc, COMPAT).state == FIRED


# ── F2: divergence pivots need prominence >= 1.5% + spacing >= 3 ────────────
def _f2_vectors():
    closes = [105, 104, 103, 102, 101, 100, 100.5, 101, 102, 103,
              104, 103, 102, 101, 100, 99, 99.5, 100, 101, 102,
              103, 104, 105, 106, 107]
    rsi = [50, 48, 46, 44, 42, 30, 40, 42, 44, 46,
           48, 46, 44, 42, 40, 35, 45, 46, 47, 48,
           49, 50, 51, 52, 53]
    return closes, rsi


def test_F2_shallow_pivots_rejected():
    closes, rsi = _f2_vectors()
    # compat: shallow lower-low + RSI higher-low -> divergence fires
    assert sg.sig4_drsi_divergence(closes, rsi, COMPAT).state == FIRED
    # full: pivots are ~0.5% prominent (<1.5%) -> filtered -> no divergence
    assert sg.sig4_drsi_divergence(closes, rsi, FULL).state == NOT


# ── F4: monthly RSI sampling is END-anchored (keeps latest weekly) ──────────
def test_F4_monthly_end_anchored():
    wc = list(range(63))           # (63-1)%4 == 2 -> last sample is wc[-1]
    assert engine.monthly_closes(wc, end_anchored=True)[-1] == wc[-1]
    assert engine.monthly_closes(wc, end_anchored=False)[-1] != wc[-1]


# ── F13: provisional sig6 pace-adjust + <15% floor (RULINGS Q4) ─────────────
def _prov_weekly(forming_green=True):
    n = 24
    wo = [100.0] * n
    wc = [101.0] * n            # green: close>open
    if not forming_green:
        wc[-1] = 99.0
    wh = [102.0] * n
    wl = [98.0] * n
    wvol = [100.0] * (n - 1) + [60.0]   # forming raw vol below average
    return (wo, wh, wl, wc, wvol)


def test_F13_provisional_below_15pct_is_na():
    weekly = _prov_weekly()
    daily = ([float(i) for i in range(1, 40)],)
    card = engine.evaluate("X/USD", weekly, daily, FULL, provisional=True, elapsed_fraction=0.10)
    sig6 = [r for r in card.results if r.slot == 6][0]
    assert sig6.state == NA and "~" in sig6.reason


def test_F13_provisional_pace_adjust_fires_when_confirmed_would_not():
    weekly = _prov_weekly()
    daily = ([float(i) for i in range(1, 40)],)
    # confirmed: raw forming vol (60) below SMA -> NOT
    confirmed = engine.evaluate("X/USD", weekly, daily, FULL, provisional=False)
    assert [r for r in confirmed.results if r.slot == 6][0].state == NOT
    # provisional at 50% elapsed: 60/0.5 = 120 > SMA -> FIRED
    prov = engine.evaluate("X/USD", weekly, daily, FULL, provisional=True, elapsed_fraction=0.5)
    assert [r for r in prov.results if r.slot == 6][0].state == FIRED
