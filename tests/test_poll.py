import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "data"))

import poll
from poll import chain_to_snapshot_rows, market_is_open


def _strike_row(strike, spot=24334.55, call_market_data=None, put_market_data=None):
    return {
        "strike_price": strike,
        "underlying_spot_price": spot,
        "call_options": {
            "instrument_key": "NSE_FO|CALL",
            "market_data": call_market_data if call_market_data is not None else {"bid_price": 10.0, "ask_price": 10.5, "ltp": 10.2, "oi": 100, "volume": 50},
            "option_greeks": {"iv": 12.5},
        },
        "put_options": {
            "instrument_key": "NSE_FO|PUT",
            "market_data": put_market_data if put_market_data is not None else {"bid_price": 20.0, "ask_price": 20.5, "ltp": 20.2, "oi": 200, "volume": 60},
            "option_greeks": {"iv": 13.1},
        },
    }


def test_normal_strike_produces_two_rows_ce_and_pe():
    chain = [_strike_row(24500)]
    rows = chain_to_snapshot_rows("NIFTY", "2026-09-01", chain, "2026-08-25T12:00:00+00:00")
    assert len(rows) == 2
    types = {r["option_type"] for r in rows}
    assert types == {"CE", "PE"}


def test_prices_converted_to_integer_paise():
    chain = [_strike_row(24500)]
    rows = chain_to_snapshot_rows("NIFTY", "2026-09-01", chain, "2026-08-25T12:00:00+00:00")
    ce = next(r for r in rows if r["option_type"] == "CE")
    assert ce["bid_paise"] == 1000  # 10.0 rupees
    assert ce["ask_paise"] == 1050  # 10.5 rupees
    assert ce["strike_paise"] == 2450000  # 24500 rupees


def test_strike_with_no_quotes_on_one_side_is_skipped():
    chain = [_strike_row(30000, call_market_data={"bid_price": None, "ask_price": None, "ltp": 0})]
    rows = chain_to_snapshot_rows("NIFTY", "2026-09-01", chain, "2026-08-25T12:00:00+00:00")
    types = {r["option_type"] for r in rows}
    assert types == {"PE"}  # call leg dropped, put leg kept


def test_missing_leg_entirely_does_not_crash():
    chain = [{"strike_price": 24500, "underlying_spot_price": 24334.55, "call_options": None,
              "put_options": {"instrument_key": "NSE_FO|PUT", "market_data": {"bid_price": 5, "ask_price": 5.5, "ltp": 5.2, "oi": 10, "volume": 5}, "option_greeks": {"iv": 10.0}}}]
    rows = chain_to_snapshot_rows("NIFTY", "2026-09-01", chain, "2026-08-25T12:00:00+00:00")
    assert len(rows) == 1
    assert rows[0]["option_type"] == "PE"


def test_null_iv_preserved_not_coerced_to_zero():
    chain = [_strike_row(24500, call_market_data={"bid_price": 1.0, "ask_price": 1.5, "ltp": 1.2, "oi": 1, "volume": 1})]
    chain[0]["call_options"]["option_greeks"] = {"iv": None}
    rows = chain_to_snapshot_rows("NIFTY", "2026-09-01", chain, "2026-08-25T12:00:00+00:00")
    ce = next(r for r in rows if r["option_type"] == "CE")
    assert ce["iv"] is None


# --- Market-hours gating ---

def test_market_is_open_true_when_normal_open(monkeypatch):
    monkeypatch.setattr(poll, "get_market_status", lambda: "NORMAL_OPEN")
    assert market_is_open() is True


def test_market_is_open_false_for_other_statuses(monkeypatch):
    for status in ("CLOSING_END", "PRE_OPEN_START", "PRE_OPEN_END", "NORMAL_CLOSE", "CLOSING_START"):
        monkeypatch.setattr(poll, "get_market_status", lambda status=status: status)
        assert market_is_open() is False, f"expected closed for status={status}"


def test_market_is_open_fails_open_on_status_check_error(monkeypatch):
    def raise_error():
        raise RuntimeError("network blip")

    monkeypatch.setattr(poll, "get_market_status", raise_error)
    assert market_is_open() is True
