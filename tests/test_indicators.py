"""Indicator math vs fixed vectors (Appendix A). SPEC §7 / M2."""
from deepfield import indicators as ind


def test_sma_fixed_vector():
    assert ind.calc_sma([1, 2, 3, 4, 5], 3) == [0.0, 0.0, 2.0, 3.0, 4.0]


def test_ema_sma_seeded_fixed_vector():
    # period=2: seed=out[1]=SMA(1,2)=1.5; then EMA k=2/3.
    out = ind.calc_ema([1, 2, 3, 4], 2)
    assert out[0] == 0.0
    assert abs(out[1] - 1.5) < 1e-12
    assert abs(out[2] - (3 * 2 / 3 + 1.5 * 1 / 3)) < 1e-12   # 2.5
    assert abs(out[3] - (4 * 2 / 3 + 2.5 * 1 / 3)) < 1e-12   # 3.5


def test_rsi_all_gains_is_100():
    series = list(range(1, 20))  # strictly increasing -> avg_loss 0 -> 100
    out = ind.calc_rsi(series, 14)
    assert out[14] == 100.0
    assert out[-1] == 100.0


def test_rsi_insufficient_data_is_zero():
    assert ind.calc_rsi([1, 2, 3], 14) == [0.0, 0.0, 0.0]


def test_macd_shapes_and_histogram_identity():
    series = [float(i) for i in range(60)]
    macd, sig, hist = ind.calc_macd(series)
    assert len(macd) == len(sig) == len(hist) == 60
    for i in range(60):
        assert abs(hist[i] - (macd[i] - sig[i])) < 1e-12


def test_clean_replaces_non_finite():
    assert ind.clean([1.0, float("nan"), "x", 3]) == [1.0, 0.0, 0.0, 3.0]
