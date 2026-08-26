import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "data"))

import fetch_option_chain
from fetch_option_chain import get_historical_daily_closes


def test_get_historical_daily_closes_reverses_to_oldest_first(monkeypatch):
    # Upstox returns candles newest-first: [timestamp, open, high, low, close, volume, oi]
    canned = {
        "data": {
            "candles": [
                ["2026-08-24T00:00:00+05:30", 100, 105, 99, 104.0, 0, 0],
                ["2026-08-21T00:00:00+05:30", 98, 101, 97, 100.0, 0, 0],
                ["2026-08-20T00:00:00+05:30", 95, 99, 94, 98.0, 0, 0],
            ]
        }
    }
    monkeypatch.setattr(fetch_option_chain, "_get", lambda path, params: canned)

    closes = get_historical_daily_closes("NSE_INDEX|Nifty 50", "2026-08-01", "2026-08-25")

    assert [c[1] for c in closes] == [98.0, 100.0, 104.0]  # oldest first
    assert closes[0][0] == "2026-08-20T00:00:00+05:30"


def test_get_historical_daily_closes_empty_response(monkeypatch):
    monkeypatch.setattr(fetch_option_chain, "_get", lambda path, params: {"data": {"candles": []}})
    closes = get_historical_daily_closes("NSE_INDEX|Nifty 50", "2026-08-01", "2026-08-25")
    assert closes == []
