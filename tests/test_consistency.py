import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "models"))

from consistency import (
    OptionQuote,
    check_calendar_monotonic,
    check_convexity,
    check_put_call_parity,
    check_vertical_spread,
    implied_forward_paise,
)


def q(key, option_type, bid, ask):
    return OptionQuote(instrument_key=key, option_type=option_type, bid_paise=bid, ask_paise=ask)


# --- Vertical spread (calls: lower strike dominates; puts: higher strike dominates) ---

def test_call_vertical_violation_detected():
    low = q("C24500", "CE", bid=15000, ask=15200)  # 150.00/152.00
    high = q("C24600", "CE", bid=15300, ask=15500)  # crossed: higher strike's bid > lower's ask
    violations = check_vertical_spread([(2450000, low), (2460000, high)], "CE")
    assert len(violations) == 1
    v = violations[0]
    assert v.dominant_key == "C24500"
    assert v.dominated_key == "C24600"
    # edge = dominated.bid - dominant.ask = 15300 - 15200
    assert v.edge_paise == 100


def test_call_vertical_no_violation_when_properly_ordered():
    low = q("C24500", "CE", bid=15000, ask=15200)
    high = q("C24600", "CE", bid=9000, ask=9200)
    assert check_vertical_spread([(2450000, low), (2460000, high)], "CE") == []


def test_put_vertical_violation_detected():
    # puts: higher strike dominates, so violation is LOW strike's bid > HIGH strike's ask
    low = q("P24500", "PE", bid=9300, ask=9500)
    high = q("P24600", "PE", bid=9000, ask=9200)  # crossed the other way vs calls
    violations = check_vertical_spread([(2450000, low), (2460000, high)], "PE")
    assert len(violations) == 1
    v = violations[0]
    assert v.dominant_key == "P24600"
    assert v.dominated_key == "P24500"
    assert v.edge_paise == 100  # 9300 - 9200


def test_put_vertical_no_violation_when_properly_ordered():
    low = q("P24500", "PE", bid=4000, ask=4200)
    high = q("P24600", "PE", bid=9000, ask=9200)
    assert check_vertical_spread([(2450000, low), (2460000, high)], "PE") == []


def test_vertical_spread_checks_nonadjacent_strikes():
    k1 = q("C1", "CE", bid=20000, ask=20200)
    k2 = q("C2", "CE", bid=19900, ask=20100)  # consistent with k1
    k3 = q("C3", "CE", bid=20500, ask=20700)  # inconsistent vs k1 despite being far away
    violations = check_vertical_spread([(100, k1), (200, k2), (300, k3)], "CE")
    keys = {(v.dominant_key, v.dominated_key) for v in violations}
    assert ("C1", "C3") in keys


def test_invalid_option_type_raises():
    try:
        check_vertical_spread([(100, q("X", "CE", 1, 2))], "XX")
        assert False, "expected ValueError"
    except ValueError:
        pass


# --- Put-call parity ---

def test_parity_no_violation_when_consistent():
    # S=2450000p, K=2450000p, r=0, T=0 -> theoretical_diff = 0
    call = q("C", "CE", bid=15000, ask=15200)
    put = q("P", "PE", bid=14900, ask=15100)
    # synthetic_long_cost = 15200-14900=300; theoretical=0 -> long_edge = -300, no violation
    # synthetic_short_proceeds = 15000-15100=-100; short_edge = -100-0 <0, no violation
    v = check_put_call_parity(call, put, implied_forward_paise=2450000, strike_paise=2450000, risk_free_rate=0.0, years_to_expiry=0.0)
    assert v is None


def test_parity_violation_synthetic_long_forward_cheap():
    # theoretical_diff = S - K = 2450000 - 2440000 = 10000 (r=0)
    # make synthetic long (buy call, sell put) very cheap: call.ask - put.bid small
    call = q("C", "CE", bid=15000, ask=15100)
    put = q("P", "PE", bid=5100, ask=5200)
    # synthetic_long_cost = 15100 - 5100 = 10000... need it BELOW theoretical (10000) to trigger; adjust
    put = q("P", "PE", bid=5200, ask=5300)
    # synthetic_long_cost = 15100 - 5200 = 9900 < 10000 -> long_edge = 100 > 0
    v = check_put_call_parity(call, put, implied_forward_paise=2450000, strike_paise=2440000, risk_free_rate=0.0, years_to_expiry=0.0)
    assert v is not None
    assert v.direction == "synthetic_long_forward_cheap"
    assert v.edge_paise == 100


def test_parity_violation_synthetic_short_forward_rich():
    # theoretical_diff = S - K = 2450000 - 2460000 = -10000 (r=0)
    call = q("C", "CE", bid=5300, ask=5400)
    put = q("P", "PE", bid=15000, ask=15100)
    # synthetic_short_proceeds = call.bid - put.ask = 5300 - 15100 = -9800 > theoretical(-10000) -> short_edge = 200
    v = check_put_call_parity(call, put, implied_forward_paise=2450000, strike_paise=2460000, risk_free_rate=0.0, years_to_expiry=0.0)
    assert v is not None
    assert v.direction == "synthetic_short_forward_rich"
    assert v.edge_paise == 200


def test_implied_forward_matches_real_nifty_example():
    """Regression test locking in the real numbers from BUGS.md DEC-4: NIFTY
    spot 24334.55, ATM strike 24350, call_mid 138.40, put_mid 90.45 -> the
    market-implied forward should land near 24397.95 (r~0 over 7 days makes
    the e^(rT) factor negligible here), NOT near raw spot -- an earlier
    version of this check used spot directly (implicitly assuming zero
    dividend yield) and it was off by about 33 points, enough to cause 44
    false "violations" on one real chain."""
    atm_call = OptionQuote("C", "CE", bid_paise=13830, ask_paise=13850)  # mid 138.40
    atm_put = OptionQuote("P", "PE", bid_paise=9035, ask_paise=9055)  # mid 90.45
    forward = implied_forward_paise(atm_call, atm_put, atm_strike_paise=2435000, risk_free_rate=0.065, years_to_expiry=7 / 365)
    assert abs(forward - 2439795) < 50  # within 50 paise (0.5 rupee) of the real observed value


def test_parity_no_violation_at_the_calibrating_strike_itself():
    """Checking parity AT the exact strike used to derive the implied forward
    must never itself produce a violation -- it's true by construction."""
    atm_call = OptionQuote("C", "CE", bid_paise=13830, ask_paise=13850)
    atm_put = OptionQuote("P", "PE", bid_paise=9035, ask_paise=9055)
    forward = implied_forward_paise(atm_call, atm_put, atm_strike_paise=2435000, risk_free_rate=0.065, years_to_expiry=7 / 365)
    v = check_put_call_parity(atm_call, atm_put, forward, strike_paise=2435000, risk_free_rate=0.065, years_to_expiry=7 / 365)
    assert v is None


# --- Convexity / butterfly ---

def test_convexity_violation_detected():
    low = q("K1", "CE", bid=20000, ask=21000)
    mid = q("K2", "CE", bid=16000, ask=16500)  # priced too high relative to wings
    high = q("K3", "CE", bid=9000, ask=9500)
    # cost = ask(low) + ask(high) - 2*bid(mid) = 21000 + 9500 - 32000 = -1500 -> edge = 1500 > 0
    v = check_convexity(low, mid, high, 24000_00, 24500_00, 25000_00)
    assert v is not None
    assert v.edge_paise == 1500


def test_convexity_no_violation_when_curve_is_convex():
    low = q("K1", "CE", bid=20000, ask=21000)
    mid = q("K2", "CE", bid=15000, ask=15200)
    high = q("K3", "CE", bid=9000, ask=9500)
    # cost = 21000 + 9500 - 30000 = 500 -> edge = -500, no violation
    assert check_convexity(low, mid, high, 24000_00, 24500_00, 25000_00) is None


def test_convexity_requires_equal_spacing():
    low = q("K1", "CE", 1, 2)
    mid = q("K2", "CE", 1, 2)
    high = q("K3", "CE", 1, 2)
    try:
        check_convexity(low, mid, high, 100, 250, 300)  # not equally spaced
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_convexity_requires_matching_option_types():
    low = q("K1", "CE", 1, 2)
    mid = q("K2", "PE", 1, 2)
    high = q("K3", "CE", 1, 2)
    try:
        check_convexity(low, mid, high, 100, 200, 300)
        assert False, "expected ValueError"
    except ValueError:
        pass


# --- Calendar monotonicity ---

def test_calendar_violation_detected():
    near = q("NEAR", "CE", bid=16000, ask=16200)
    far = q("FAR", "CE", bid=15500, ask=15700)  # crossed: near's bid > far's ask
    v = check_calendar_monotonic(near, "2026-09-01", far, "2026-09-08", strike_paise=2450000)
    assert v is not None
    assert v.near_key == "NEAR"
    assert v.far_key == "FAR"
    assert v.edge_paise == 300  # 16000 - 15700


def test_calendar_no_violation_when_far_priced_higher():
    near = q("NEAR", "CE", bid=15000, ask=15200)
    far = q("FAR", "CE", bid=16000, ask=16200)
    assert check_calendar_monotonic(near, "2026-09-01", far, "2026-09-08", strike_paise=2450000) is None


def test_calendar_requires_near_before_far():
    near = q("NEAR", "CE", 1, 2)
    far = q("FAR", "CE", 1, 2)
    try:
        check_calendar_monotonic(near, "2026-09-08", far, "2026-09-01", strike_paise=2450000)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_calendar_requires_matching_option_type():
    near = q("NEAR", "CE", 1, 2)
    far = q("FAR", "PE", 1, 2)
    try:
        check_calendar_monotonic(near, "2026-09-01", far, "2026-09-08", strike_paise=2450000)
        assert False, "expected ValueError"
    except ValueError:
        pass
