import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "backtest"))

from analyze_violations import group_by_cycle, moneyness_bucket, scan_cycle


def _row(**kwargs):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (underlying, expiry, strike_paise, option_type, instrument_key, timestamp_utc, bid_paise, ask_paise, oi, volume, iv, underlying_spot_paise)")
    defaults = dict(
        underlying="NIFTY", expiry="2026-09-01", strike_paise=2450000, option_type="CE",
        instrument_key="X", timestamp_utc="2026-08-25T12:00:00+00:00", bid_paise=1000, ask_paise=1050,
        oi=100, volume=10, iv=12.0, underlying_spot_paise=2433455,
    )
    defaults.update(kwargs)
    conn.execute(
        "INSERT INTO t VALUES (:underlying,:expiry,:strike_paise,:option_type,:instrument_key,:timestamp_utc,:bid_paise,:ask_paise,:oi,:volume,:iv,:underlying_spot_paise)",
        defaults,
    )
    return conn.execute("SELECT * FROM t").fetchone()


def test_group_by_cycle_groups_by_timestamp():
    rows = [_row(timestamp_utc="t1"), _row(timestamp_utc="t1"), _row(timestamp_utc="t2")]
    cycles = group_by_cycle(rows)
    assert list(cycles.keys()) == ["t1", "t2"]
    assert len(cycles["t1"]) == 2
    assert len(cycles["t2"]) == 1


def test_scan_cycle_detects_vertical_spread_violation():
    rows = [
        _row(strike_paise=2450000, option_type="CE", instrument_key="C1", bid_paise=15000, ask_paise=15200, oi=500),
        _row(strike_paise=2460000, option_type="CE", instrument_key="C2", bid_paise=15300, ask_paise=15500, oi=300),
    ]
    results = scan_cycle(rows)
    types = {r["type"] for r in results}
    assert "vertical_spread" in types


def test_scan_cycle_vertical_violation_has_correct_legs_and_oi():
    rows = [
        _row(strike_paise=2450000, option_type="CE", instrument_key="C1", bid_paise=15000, ask_paise=15200, oi=500),
        _row(strike_paise=2460000, option_type="CE", instrument_key="C2", bid_paise=15300, ask_paise=15500, oi=300),
    ]
    v = next(r for r in scan_cycle(rows) if r["type"] == "vertical_spread")
    assert set(v["legs"]) == {("buy", 15200), ("sell", 15300)}  # buy dominant(C1) at ask, sell dominated(C2) at bid
    assert v["min_oi"] == 300  # min of the two legs' OI


def test_scan_cycle_no_violations_on_clean_data():
    rows = [
        _row(strike_paise=2450000, option_type="CE", instrument_key="C1", bid_paise=15000, ask_paise=15200),
        _row(strike_paise=2460000, option_type="CE", instrument_key="C2", bid_paise=9000, ask_paise=9200),
        _row(strike_paise=2450000, option_type="PE", instrument_key="P1", bid_paise=9000, ask_paise=9200),
        _row(strike_paise=2460000, option_type="PE", instrument_key="P2", bid_paise=14900, ask_paise=15100),
    ]
    results = scan_cycle(rows)
    assert all(r["type"] != "vertical_spread" for r in results)


def test_scan_cycle_handles_single_leg_gracefully():
    # only a call, no put -- parity check should be skipped, not crash
    rows = [_row(option_type="CE")]
    results = scan_cycle(rows)  # should not raise
    assert isinstance(results, list)


def test_scan_cycle_calendar_violation_across_expiries():
    rows = [
        _row(expiry="2026-09-01", strike_paise=2450000, option_type="CE", instrument_key="NEAR", bid_paise=16000, ask_paise=16200),
        _row(expiry="2026-09-08", strike_paise=2450000, option_type="CE", instrument_key="FAR", bid_paise=15500, ask_paise=15700),
    ]
    results = scan_cycle(rows)
    calendar = [r for r in results if r["type"] == "calendar"]
    assert len(calendar) == 1
    assert set(calendar[0]["legs"]) == {("sell", 16000), ("buy", 15700)}


# --- moneyness_bucket ---

def test_moneyness_bucket_near_atm():
    assert moneyness_bucket(2450000, 2450000) == "near_atm (<2%)"


def test_moneyness_bucket_mid():
    assert moneyness_bucket(2450000, 2400000) == "mid (2-5%)"  # ~2.08% away


def test_moneyness_bucket_far():
    assert moneyness_bucket(2700000, 2450000) == "far (>5%)"  # ~10.2% away
