import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "models"))

from fair_value import IVCurvePoint, find_iv_deviations, fit_iv_curve, realized_volatility


def p(strike, iv, key=None, option_type="CE"):
    return IVCurvePoint(strike_paise=strike, instrument_key=key or f"K{strike}", option_type=option_type, observed_iv=iv)


# --- fit_iv_curve ---

def test_fit_requires_at_least_four_distinct_strikes():
    points = [p(100, 15), p(200, 14), p(300, 13)]
    try:
        fit_iv_curve(points)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_fit_deduplicates_repeated_strikes():
    points = [p(100, 15), p(100, 15.5), p(200, 14), p(300, 13), p(400, 12)]
    # should not raise -- 4 distinct strikes after dedup (100 kept once)
    spline = fit_iv_curve(points)
    assert spline is not None


def test_fit_recovers_a_smooth_known_curve_closely():
    # a smooth, mildly curved synthetic "smile": IV = 15 + 0.000001*(strike-250)^2
    strikes = [100, 150, 200, 250, 300, 350, 400]
    points = [p(k, 15 + 0.000001 * (k - 250) ** 2) for k in strikes]
    spline = fit_iv_curve(points, smoothing=0)  # s=0 -> interpolate exactly through points
    for k in strikes:
        expected = 15 + 0.000001 * (k - 250) ** 2
        assert abs(float(spline(k)) - expected) < 0.01


# --- find_iv_deviations ---

def test_no_deviation_on_a_perfectly_smooth_curve():
    strikes = [100, 150, 200, 250, 300]
    points = [p(k, 15.0) for k in strikes]  # flat curve, no noise
    deviations = find_iv_deviations(points, threshold=1.0, smoothing=0)
    assert deviations == []


def test_single_outlier_strike_is_flagged():
    points = [p(100, 15.0), p(150, 15.0), p(200, 15.0), p(250, 30.0), p(300, 15.0), p(350, 15.0)]
    deviations = find_iv_deviations(points, threshold=2.0)
    flagged_strikes = {d.strike_paise for d in deviations}
    assert 250 in flagged_strikes


def test_outlier_does_not_falsely_flag_its_neighbors():
    """Regression test: a naive single-fit or plain leave-one-out approach
    both flagged the strikes NEXT TO a genuine outlier as false positives,
    because the outlier remained present in its neighbors' comparison
    curves even when excluded from its own. Only the two-pass approach
    (exclude confirmed outliers, refit clean, re-evaluate everyone) gets
    this right -- lock it in so the neighbor-contamination bug can't
    silently come back."""
    strikes = list(range(100, 100 + 40 * 10, 10))
    points = [p(k, 15 + 0.0005 * (k - 300), key=f"K{k}") for k in strikes]
    points = [p(pt.strike_paise, 30.0, key=pt.instrument_key) if pt.strike_paise == 300 else pt for pt in points]

    deviations = find_iv_deviations(points, threshold=2.0)
    flagged = {d.strike_paise for d in deviations}
    assert flagged == {300}  # only the real outlier -- not its neighbors at 290/310
    outlier = next(d for d in deviations if d.strike_paise == 300)
    assert abs(outlier.fitted_iv - 15.0) < 0.5  # recovers close to the true smooth baseline


def test_threshold_controls_sensitivity():
    points = [p(100, 15.0), p(150, 15.0), p(200, 15.0), p(250, 16.5), p(300, 15.0), p(350, 15.0)]
    # small (1.5-point) bump at 250 -- flagged with a loose threshold, not with a strict one
    loose = find_iv_deviations(points, threshold=0.5)
    strict = find_iv_deviations(points, threshold=5.0)
    assert any(d.strike_paise == 250 for d in loose)
    assert strict == []


# --- realized_volatility ---

def test_realized_volatility_returns_percentage_number_not_raw_fraction():
    """Regression test locking in a real bug: an earlier version returned a
    raw decimal fraction (0.0912) instead of a percentage number (9.12),
    which silently mismatched IVCurvePoint.observed_iv's convention and
    printed a real NIFTY realized vol as "0.09%" instead of the correct
    "9.12%" -- caught because 0.09% is an implausible annualized vol for an
    index, not from reading the code. These are the exact real NIFTY daily
    closes (fetched live, not reconstructed) that produced that number."""
    closes = [
        24187.7, 23996.25, 23869.6, 23767.45, 23995.95, 23985.35, 24250.2, 24317.15, 24383.6, 24774.3,
        24614.9, 24624.65, 24636.0, 24570.65, 24583.8, 24471.7, 24435.95, 24395.85, 24366.0, 24287.65,
        24154.9, 24078.3, 24231.85, 24252.0, 24219.05,
    ]
    result = realized_volatility(closes)
    assert abs(result - 9.122785188611807) < 0.01  # real percentage-scale number, not ~0.09


def test_realized_volatility_zero_for_constant_prices():
    closes = [100.0] * 10
    assert realized_volatility(closes) == 0.0


def test_realized_volatility_requires_at_least_two_closes():
    try:
        realized_volatility([100.0])
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_realized_volatility_positive_for_varying_prices():
    closes = [100.0, 102.0, 99.0, 103.0, 101.0, 104.0]
    vol = realized_volatility(closes)
    assert vol > 0


def test_realized_volatility_scales_with_trading_days_assumption():
    closes = [100.0, 105.0, 98.0, 103.0]
    vol_252 = realized_volatility(closes, trading_days_per_year=252)
    vol_365 = realized_volatility(closes, trading_days_per_year=365)
    assert vol_365 > vol_252  # sqrt(365) > sqrt(252), same daily vol scaled up more
