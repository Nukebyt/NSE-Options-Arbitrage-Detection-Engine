"""Polling loop: pulls tracked option chains and logs snapshots to SQLite.

NIFTY and BANKNIFTY are tracked by expiry *count*, not a fixed time horizon --
NIFTY has weekly expiries, BANKNIFTY only monthly (FOUNDATIONS.md S3), so "3
expiries" spans very different real time windows for each.
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import date, datetime, timezone

from db import get_connection, insert_snapshots, rupees_to_paise
from fetch_option_chain import BANKNIFTY, NIFTY, get_market_status, get_option_chain, list_expiries

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Only NORMAL_OPEN has continuously-moving quotes representative of normal
# trading -- pre-open/closing phases run auction mechanics with different
# microstructure, not what this project's checks are modeling. Polling
# outside NORMAL_OPEN just re-writes the same frozen last-known-state on
# every cycle, which silently corrupts Phase 3's persistence statistics (a
# frozen violation looks like it "persisted" for hours) -- caught by
# actually diffing consecutive rows for one instrument, not assumed.
MARKET_OPEN_STATUS = "NORMAL_OPEN"
CLOSED_RECHECK_SECONDS = 300

TRACKED = {
    NIFTY: {"label": "NIFTY", "n_expiries": 3},
    BANKNIFTY: {"label": "BANKNIFTY", "n_expiries": 2},
}


def future_expiries(instrument_key: str, n: int) -> list[str]:
    today = date.today().isoformat()
    expiries = [e for e in list_expiries(instrument_key) if e > today]
    return expiries[:n]


def chain_to_snapshot_rows(underlying_label: str, expiry: str, chain: list[dict], timestamp_utc: str) -> list[dict]:
    rows = []
    for strike_row in chain:
        spot_paise = rupees_to_paise(strike_row["underlying_spot_price"])
        strike_paise = rupees_to_paise(strike_row["strike_price"])
        for option_type, leg_key in (("CE", "call_options"), ("PE", "put_options")):
            leg = strike_row.get(leg_key)
            if not leg:
                continue
            market_data = leg.get("market_data") or {}
            greeks = leg.get("option_greeks") or {}
            # A strike needs a REAL two-sided quote to be usable at all -- a one-
            # sided quote (e.g. bid=2.05, ask=null/missing, oi=0) is common on
            # illiquid deep strikes and is NOT safely treated as "ask=0" (see
            # BUGS.md BUG-6): `... or 0` below would coerce a missing ask into a
            # literal zero, which the vertical spread check then reads as an
            # absurdly cheap dominant leg, cascading into hundreds of false
            # violations against every other strike. Require BOTH sides present
            # and strictly positive -- no real option is ever quoted at exactly
            # Rs 0.00 on either side.
            if not market_data.get("bid_price") or not market_data.get("ask_price"):
                continue
            rows.append(
                {
                    "underlying": underlying_label,
                    "expiry": expiry,
                    "strike_paise": strike_paise,
                    "option_type": option_type,
                    "instrument_key": leg.get("instrument_key", ""),
                    "timestamp_utc": timestamp_utc,
                    "bid_paise": rupees_to_paise(market_data.get("bid_price") or 0),
                    "ask_paise": rupees_to_paise(market_data.get("ask_price") or 0),
                    "ltp_paise": rupees_to_paise(market_data.get("ltp") or 0),
                    "oi": float(market_data.get("oi") or 0),
                    "volume": float(market_data.get("volume") or 0),
                    "iv": greeks.get("iv"),
                    "underlying_spot_paise": spot_paise,
                }
            )
    return rows


def poll_once(conn) -> int:
    timestamp_utc = datetime.now(timezone.utc).isoformat()
    total = 0
    for instrument_key, cfg in TRACKED.items():
        try:
            expiries = future_expiries(instrument_key, cfg["n_expiries"])
        except Exception:
            log.exception("Failed to list expiries for %s, skipping this cycle", cfg["label"])
            continue

        for expiry in expiries:
            try:
                chain = get_option_chain(instrument_key, expiry)
            except Exception:
                log.exception("Failed to fetch chain for %s %s, skipping", cfg["label"], expiry)
                continue
            rows = chain_to_snapshot_rows(cfg["label"], expiry, chain, timestamp_utc)
            if rows:
                insert_snapshots(conn, rows)
                total += len(rows)

    return total


def market_is_open() -> bool:
    try:
        status = get_market_status()
    except Exception:
        # Fail open: a spurious status-check failure shouldn't stop real
        # data collection during actual trading hours. Worst case on a
        # false positive is a few wasted rows, not a gap in coverage.
        log.exception("Could not check market status, polling anyway")
        return True
    return status == MARKET_OPEN_STATUS


def run(interval_seconds: int, iterations: int | None) -> None:
    conn = get_connection()
    count = 0
    was_closed = False
    while iterations is None or count < iterations:
        if not market_is_open():
            if not was_closed:
                log.info("Market closed -- pausing polling until it reopens (rechecking every %ds)", CLOSED_RECHECK_SECONDS)
                was_closed = True
            time.sleep(CLOSED_RECHECK_SECONDS)
            continue

        if was_closed:
            log.info("Market open again -- resuming polling")
            was_closed = False

        n = poll_once(conn)
        log.info("Logged %d snapshot rows", n)
        count += 1
        if iterations is None or count < iterations:
            time.sleep(interval_seconds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=60, help="seconds between polls")
    parser.add_argument("--iterations", type=int, default=None, help="stop after N polls (default: run forever)")
    args = parser.parse_args()
    run(args.interval, args.iterations)
