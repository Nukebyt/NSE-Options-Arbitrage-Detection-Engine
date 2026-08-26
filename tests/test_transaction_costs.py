import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "models"))

from transaction_costs import LOT_SIZES, leg_cost_paise, trade_cost_paise


def test_leg_cost_includes_brokerage_always():
    cost = leg_cost_paise(premium_paise_per_unit=100, side="buy", lot_size=1)
    assert cost >= 2000  # at least the flat Rs 20 brokerage


def test_sell_side_incurs_stt_buy_side_does_not():
    # same premium/lot_size, only side differs
    buy_cost = leg_cost_paise(premium_paise_per_unit=100000, side="buy", lot_size=65)
    sell_cost = leg_cost_paise(premium_paise_per_unit=100000, side="sell", lot_size=65)
    assert sell_cost > buy_cost  # STT (0.15% of a large turnover) should dominate the difference


def test_hand_computed_example_nifty_lot():
    # premium = Rs 100.00/unit = 10000 paise, NIFTY lot_size=65 -> turnover = 650000 paise
    # sell side: STT = 650000*0.0015=975, exch=650000*0.0003553=230.945->231,
    # sebi=650000*1e-7=0.065->0, ipft=650000*5e-6=3.25->3
    # gst = round((2000+231+3)*0.18) = round(402.12) = 402
    # total = 2000+975+231+0+0+3+402 = 3611
    cost = leg_cost_paise(premium_paise_per_unit=10000, side="sell", lot_size=65)
    assert cost == 3611


def test_invalid_side_raises():
    try:
        leg_cost_paise(100, "short", 1)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_trade_cost_sums_all_legs():
    legs = [("buy", 10000), ("sell", 10000)]
    total = trade_cost_paise(legs, lot_size=65)
    assert total == leg_cost_paise(10000, "buy", 65) + leg_cost_paise(10000, "sell", 65)


def test_trade_cost_counts_repeated_leg_twice():
    # butterfly's middle strike sold twice -- should cost roughly double a single sell
    single = trade_cost_paise([("sell", 5000)], lot_size=65)
    doubled = trade_cost_paise([("sell", 5000), ("sell", 5000)], lot_size=65)
    assert doubled == 2 * single


def test_lot_sizes_confirmed_values():
    assert LOT_SIZES["NIFTY"] == 65
    assert LOT_SIZES["BANKNIFTY"] == 30
