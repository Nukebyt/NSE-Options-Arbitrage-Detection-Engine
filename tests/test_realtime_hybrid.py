import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "data"))

import realtime_hybrid
from realtime_hybrid import Debouncer, build_subscribe_message, check_group_via_rest


# --- Debouncer ---

def test_debouncer_allows_first_trigger():
    d = Debouncer(2.0)
    assert d.should_trigger(("NIFTY", "2026-09-01"), now=100.0) is True


def test_debouncer_blocks_within_window():
    d = Debouncer(2.0)
    d.should_trigger(("NIFTY", "2026-09-01"), now=100.0)
    assert d.should_trigger(("NIFTY", "2026-09-01"), now=101.0) is False  # only 1s later


def test_debouncer_allows_after_window_elapses():
    d = Debouncer(2.0)
    d.should_trigger(("NIFTY", "2026-09-01"), now=100.0)
    assert d.should_trigger(("NIFTY", "2026-09-01"), now=102.5) is True  # 2.5s later


def test_debouncer_tracks_keys_independently():
    d = Debouncer(2.0)
    d.should_trigger(("NIFTY", "2026-09-01"), now=100.0)
    # a different group should not be blocked by NIFTY's debounce window
    assert d.should_trigger(("BANKNIFTY", "2026-09-29"), now=100.1) is True


# --- build_subscribe_message ---

def test_build_subscribe_message_uses_ltpc_mode():
    import json

    raw = build_subscribe_message(["NSE_FO|1", "NSE_FO|2"])
    parsed = json.loads(raw.decode("utf-8"))
    assert parsed["data"]["mode"] == "ltpc"
    assert parsed["data"]["instrumentKeys"] == ["NSE_FO|1", "NSE_FO|2"]


# --- check_group_via_rest (mocked REST, no network) ---

def _fake_chain():
    def leg(strike, ce_bid, ce_ask, pe_bid, pe_ask, ce_key, pe_key):
        return {
            "strike_price": strike,
            "underlying_spot_price": 24350.0,
            "call_options": {"instrument_key": ce_key, "market_data": {"bid_price": ce_bid, "ask_price": ce_ask}},
            "put_options": {"instrument_key": pe_key, "market_data": {"bid_price": pe_bid, "ask_price": pe_ask}},
        }

    # Wide-spread, monotonic, parity-consistent (verified by actually running
    # check_group_via_rest against it, not just eyeballed -- a first, tighter-
    # spread attempt at this fixture left a genuine 5-paise parity "violation"
    # from real discounting math, not a bug in the check, which is exactly
    # the kind of thing worth widening the spread to clear rather than
    # weakening the test's assertion).
    return [
        leg(24300, 175.0, 181.0, 85.0, 91.0, "C1", "P1"),
        leg(24350, 145.0, 151.0, 105.0, 111.0, "C2", "P2"),
        leg(24400, 115.0, 121.0, 130.0, 136.0, "C3", "P3"),
    ]


def test_check_group_via_rest_clean_data_no_violations(monkeypatch):
    monkeypatch.setattr(realtime_hybrid, "get_option_chain", lambda index_key, expiry: _fake_chain())
    violations, latency = check_group_via_rest("NIFTY", "2026-09-01")
    assert violations == []
    assert latency >= 0


def test_check_group_via_rest_detects_vertical_violation(monkeypatch):
    def crossed_chain(index_key, expiry):
        chain = _fake_chain()
        # make the 24400 call's bid exceed the 24300 call's ask (181) -- a real crossing
        chain[2]["call_options"]["market_data"] = {"bid_price": 190.0, "ask_price": 195.0}
        return chain

    monkeypatch.setattr(realtime_hybrid, "get_option_chain", crossed_chain)
    violations, latency = check_group_via_rest("NIFTY", "2026-09-01")
    assert any(vtype == "vertical_spread" for vtype, _ in violations)


def test_check_group_via_rest_empty_chain_returns_no_violations(monkeypatch):
    monkeypatch.setattr(realtime_hybrid, "get_option_chain", lambda index_key, expiry: [])
    violations, latency = check_group_via_rest("NIFTY", "2026-09-01")
    assert violations == []
