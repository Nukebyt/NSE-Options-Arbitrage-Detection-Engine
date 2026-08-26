"""Phase 4, DEC-7 resolution: ltpc-as-trigger real-time detection.

`full_d5` (order-book depth) gets no response with either credential type
this project has tried (Analytics token, full OAuth2 App token -- see
BUGS.md DEC-7), so there's no live bid/ask stream to check directly.
`ltpc` mode DOES work -- it just carries last-traded-price only, which this
project's checks refuse to use (FOUNDATIONS.md S5's mid-price-leakage
discipline forbids computing an edge off anything but executable bid/ask).

The hybrid: subscribe via WebSocket in `ltpc` mode purely as a trigger --
the instant any tracked instrument trades, that's a signal "this
(underlying, expiry) group's book may have moved," which fires an
immediate REST call to `/option/chain` (confirmed real bid/ask depth,
working since Phase 1) for that one group, and Phase 2's checks run against
that REST snapshot, never against the WS tick's LTP itself. This is
event-driven in the sense that matters (triggered by real trade activity,
not a blind fixed interval) while keeping every check honest.

A debounce prevents re-fetching the same group on every single tick --
liquid strikes can trade many times a second, and re-fetching on all of
them would be both wasteful and pointless (the book won't have meaningfully
changed between two ticks a few hundred ms apart). REST rate limits
confirmed live: 50 req/s, 500 req/min for standard market-data GETs --
comfortable headroom even before debouncing, this project's 5 tracked
groups could poll every group every second and still use under 10% of the
per-second limit, but that would defeat the point of being trigger-driven.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "models"))

from MarketDataFeed_pb2 import FeedResponse  # noqa: E402

from auth import get_ws_authorized_url  # noqa: E402
from consistency import (  # noqa: E402
    _build_books,
    check_convexity,
    check_put_call_parity,
    check_vertical_spread,
    implied_forward_paise,
)
from fetch_option_chain import get_option_chain  # noqa: E402
from violations_db import get_connection as get_violations_db_connection  # noqa: E402
from violations_db import record_trigger  # noqa: E402
from ws_stream import TRACKED, build_reference_table  # noqa: E402 -- reuse, don't re-derive

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

RISK_FREE_RATE = 0.065
DEBOUNCE_SECONDS = 2.0

LABEL_TO_INDEX_KEY = {cfg["label"]: index_key for index_key, cfg in TRACKED.items()}


class Debouncer:
    """Tracks last-trigger time per key; should_trigger() returns True (and
    resets the clock) only if enough time has passed since the last trigger
    for that same key. Pure and independently testable -- no I/O."""

    def __init__(self, debounce_seconds: float):
        self.debounce_seconds = debounce_seconds
        self._last_trigger: dict[tuple, float] = {}

    def should_trigger(self, key: tuple, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        last = self._last_trigger.get(key)
        if last is None or (now - last) >= self.debounce_seconds:
            self._last_trigger[key] = now
            return True
        return False


def check_group_via_rest(underlying_label: str, expiry: str) -> tuple[list[tuple[str, object]], float]:
    """Fetches a fresh REST snapshot for one (underlying, expiry) group and
    runs Phase 2's checks against it. Returns (violations, fetch_latency_seconds)
    -- the latency this project's Phase 4 checklist wanted and couldn't
    measure while full_d5 was still the assumed path."""
    index_key = LABEL_TO_INDEX_KEY[underlying_label]

    t0 = time.monotonic()
    chain = get_option_chain(index_key, expiry)
    fetch_latency = time.monotonic() - t0

    if not chain:
        return [], fetch_latency

    spot_paise = round(chain[0]["underlying_spot_price"] * 100)
    calls, puts = _build_books(chain)

    violations: list[tuple[str, object]] = []
    for label, book in (("CE", calls), ("PE", puts)):
        for v in check_vertical_spread(list(book.items()), label):
            violations.append(("vertical_spread", v))

    common_strikes = sorted(set(calls) & set(puts))
    if common_strikes:
        expiry_date = date.fromisoformat(expiry)
        years_to_expiry = max((expiry_date - date.today()).days, 0) / 365.0
        atm_strike = min(common_strikes, key=lambda k: abs(k - spot_paise))
        forward = implied_forward_paise(calls[atm_strike], puts[atm_strike], atm_strike, RISK_FREE_RATE, years_to_expiry)
        for strike in common_strikes:
            v = check_put_call_parity(calls[strike], puts[strike], forward, strike, RISK_FREE_RATE, years_to_expiry)
            if v:
                violations.append(("put_call_parity", v))

    for label, book in (("CE", calls), ("PE", puts)):
        strikes = sorted(book)
        for i in range(len(strikes) - 2):
            k1, k2, k3 = strikes[i], strikes[i + 1], strikes[i + 2]
            if (k2 - k1) != (k3 - k2):
                continue
            v = check_convexity(book[k1], book[k2], book[k3], k1, k2, k3)
            if v:
                violations.append(("convexity", v))

    return violations, fetch_latency


def build_subscribe_message(instrument_keys: list[str]) -> bytes:
    message = {"guid": str(uuid.uuid4()), "method": "sub", "data": {"mode": "ltpc", "instrumentKeys": instrument_keys}}
    return json.dumps(message).encode("utf-8")


async def _connect_and_stream(option_reference: dict, debouncer: Debouncer, db_conn) -> None:
    import websockets

    url = get_ws_authorized_url()
    all_keys = list(option_reference.keys())

    async with websockets.connect(url) as ws:
        log.info("Connected. Subscribing to %d instruments in ltpc (trigger) mode...", len(all_keys))
        await ws.send(build_subscribe_message(all_keys))

        async for raw in ws:
            tick_received_at = time.monotonic()
            if isinstance(raw, str):
                continue

            response = FeedResponse()
            response.ParseFromString(raw)

            for instrument_key in response.feeds:
                meta = option_reference.get(instrument_key)
                if meta is None:
                    continue

                group_key = (meta["underlying"], meta["expiry"])
                if not debouncer.should_trigger(group_key, now=tick_received_at):
                    continue

                violations, fetch_latency = check_group_via_rest(*group_key)
                total_latency = time.monotonic() - tick_received_at
                log.info(
                    "Triggered by %s: %s %s -- REST fetch %.0fms, total tick-to-detection %.0fms, %d violations",
                    instrument_key, group_key[0], group_key[1], fetch_latency * 1000, total_latency * 1000, len(violations),
                )
                for vtype, v in violations:
                    log.info("  VIOLATION [%s %s %s]: %s", group_key[0], group_key[1], vtype, v)

                record_trigger(
                    db_conn,
                    detected_at_utc=datetime.now(timezone.utc).isoformat(),
                    underlying=group_key[0],
                    expiry=group_key[1],
                    trigger_instrument_key=instrument_key,
                    fetch_latency_ms=fetch_latency * 1000,
                    total_latency_ms=total_latency * 1000,
                    violations=violations,
                )


async def run(max_reconnects: int | None = None) -> None:
    """Same reconnect-with-backoff shape as ws_stream.run() (FOUNDATIONS.md
    S27) -- deliberately not shared code with ws_stream.py's version since
    the two scripts test genuinely different subscription modes and are
    meant to be run independently, not as variants of one entrypoint."""
    import websockets.exceptions

    option_reference, _ = build_reference_table()
    debouncer = Debouncer(DEBOUNCE_SECONDS)
    log.info("Reference table built: %d option legs. Debounce: %.1fs per group.", len(option_reference), DEBOUNCE_SECONDS)

    db_conn = get_violations_db_connection()
    log.info("Logging triggers/violations to %s (trigger_events, detected_violations)", db_conn.execute("PRAGMA database_list").fetchone()[2])

    backoff_seconds = 1.0
    max_backoff_seconds = 60.0
    attempt = 0

    try:
        while max_reconnects is None or attempt < max_reconnects:
            attempt += 1
            try:
                await _connect_and_stream(option_reference, debouncer, db_conn)
                log.warning("WebSocket closed cleanly; reconnecting")
                backoff_seconds = 1.0
            except (websockets.exceptions.ConnectionClosed, OSError) as e:
                log.warning("Connection dropped (%s: %s), reconnecting in %.0fs", type(e).__name__, e, backoff_seconds)
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, max_backoff_seconds)
    finally:
        db_conn.close()


if __name__ == "__main__":
    asyncio.run(run())
