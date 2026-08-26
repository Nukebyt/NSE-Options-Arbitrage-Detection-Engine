"""Upstox option chain REST client.

instrument_key values confirmed live 2026-08-25: NIFTY = "NSE_INDEX|Nifty 50",
BANKNIFTY = "NSE_INDEX|Nifty Bank". Other plausible-looking guesses
("NSE_INDEX|Bank Nifty", "NSE_INDEX|NIFTY BANK") return 200 with silently
empty data rather than an error -- don't trust an unverified instrument_key
or expiry_date just because the call didn't fail.
"""
from __future__ import annotations

import time

import requests

from auth import get_auth_headers

BASE_URL = "https://api.upstox.com/v2"

NIFTY = "NSE_INDEX|Nifty 50"
BANKNIFTY = "NSE_INDEX|Nifty Bank"

# A single connection-reusing session + retry, same reasoning as the
# Kalshi-phase BUG-2 fix (kalshi_archive/fetch_markets.py).
_session = requests.Session()
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0


def _get(path: str, params: dict) -> dict:
    headers = get_auth_headers()
    for attempt in range(MAX_RETRIES):
        try:
            resp = _session.get(f"{BASE_URL}{path}", headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))


def get_option_contracts(instrument_key: str, expiry_date: str | None = None) -> list[dict]:
    params = {"instrument_key": instrument_key}
    if expiry_date:
        params["expiry_date"] = expiry_date
    data = _get("/option/contract", params)
    return data.get("data", [])


def get_option_chain(instrument_key: str, expiry_date: str) -> list[dict]:
    data = _get("/option/chain", {"instrument_key": instrument_key, "expiry_date": expiry_date})
    return data.get("data", [])


def get_historical_daily_closes(instrument_key: str, from_date: str, to_date: str) -> list[tuple[str, float]]:
    """Daily closing prices for the underlying, oldest first -- for
    realized volatility (FOUNDATIONS.md S31). Confirmed live 2026-08-25:
    GET /v2/historical-candle/{instrument_key}/day/{to_date}/{from_date}
    (instrument_key must be URL-quoted, it contains '|'), response candles
    are [timestamp, open, high, low, close, volume, oi], newest first.
    """
    import urllib.parse

    quoted_key = urllib.parse.quote(instrument_key, safe="")
    data = _get(f"/historical-candle/{quoted_key}/day/{to_date}/{from_date}", {})
    candles = data.get("data", {}).get("candles", [])
    closes = [(c[0], c[4]) for c in candles]
    return list(reversed(closes))  # oldest first


def get_market_status(exchange: str = "NSE") -> str:
    """Live exchange-reported status (e.g. NORMAL_OPEN, CLOSING_END,
    PRE_OPEN_START) -- confirmed live 2026-08-25 via GET /v2/market/status/NSE.
    Preferred over a hardcoded trading-hours window: it accounts for
    holidays and special sessions automatically, the same way relying on
    Upstox's own instrument data beat hardcoding lot sizes (FOUNDATIONS.md S3).
    """
    data = _get(f"/market/status/{exchange}", {})
    return data["data"]["status"]


def list_expiries(instrument_key: str) -> list[str]:
    contracts = get_option_contracts(instrument_key)
    return sorted({c["expiry"] for c in contracts})


if __name__ == "__main__":
    expiries = list_expiries(NIFTY)
    print(f"NIFTY expiries found ({len(expiries)} total): {expiries[:6]}")

    # today's expiry (if any) may have very few strikes left -- use the next one
    from datetime import date

    today = date.today().isoformat()
    future_expiries = [e for e in expiries if e > today] or expiries
    target_expiry = future_expiries[0]

    print(f"\nFetching chain for {target_expiry}...")
    chain = get_option_chain(NIFTY, target_expiry)
    spot = chain[0]["underlying_spot_price"] if chain else "n/a"
    print(f"{len(chain)} strikes returned. Underlying spot: {spot}\n")

    atm_index = min(range(len(chain)), key=lambda i: abs(chain[i]["strike_price"] - float(spot))) if chain else 0
    for row in chain[max(0, atm_index - 3) : atm_index + 3]:
        call = row.get("call_options", {}).get("market_data", {})
        put = row.get("put_options", {}).get("market_data", {})
        print(
            f"  strike={row['strike_price']:<10} "
            f"call_bid={call.get('bid_price', '-'):<8} call_ask={call.get('ask_price', '-'):<8} "
            f"put_bid={put.get('bid_price', '-'):<8} put_ask={put.get('ask_price', '-'):<8}"
        )
