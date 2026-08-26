"""Phase 4: event-driven no-arbitrage detection via Upstox's WebSocket feed.

Protobuf-encoded (verified against Upstox's real .proto schema at
assets.upstox.com/feed/market-data-feed/v3/MarketDataFeed.proto, not just
docs prose -- BUG-6 in BUGS.md caught two doc inaccuracies before this was
written, and the doc's own JSON example used mode "full" when the real
RequestMode enum only has full_d5/full_d30/ltpc/option_greeks).

Reference data (which instrument_key maps to which underlying/expiry/strike/
option_type) comes from one REST call at startup; the WebSocket only ever
sends instrument_key + price/greeks. This mirrors the "static reference data
via REST, hot path via WS deltas" pattern from the Kalshi-phase groundwork
(now archived, kalshi_archive/ws_stream.py) -- same idea, independently
re-derived for a different feed protocol.

STATUS 2026-08-25: auth + connect verified live. Full tick-processing path
is written and unit-tested against synthetic protobuf messages
(tests/test_ws_stream.py), but NSE is closed at the time of writing, so no
live tick has actually been decoded end-to-end yet -- see the run log noted
in ROADMAP.md Phase 4 for what was and wasn't observed live.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "models"))

from MarketDataFeed_pb2 import FeedResponse  # noqa: E402

from auth import get_ws_authorized_url  # noqa: E402
from consistency import (  # noqa: E402
    OptionQuote,
    check_convexity,
    check_put_call_parity,
    check_vertical_spread,
    implied_forward_paise,
)
from fetch_option_chain import BANKNIFTY, NIFTY, get_option_contracts, list_expiries  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

RISK_FREE_RATE = 0.065
TRACKED = {NIFTY: {"label": "NIFTY", "n_expiries": 2}, BANKNIFTY: {"label": "BANKNIFTY", "n_expiries": 1}}


def build_reference_table() -> tuple[dict[str, dict], dict[str, str]]:
    """Returns (option_reference, index_instrument_keys).
    option_reference: instrument_key -> {underlying, expiry, strike_paise, option_type}
    index_instrument_keys: underlying_key -> label (for tracking spot price)
    """
    option_reference: dict[str, dict] = {}
    index_keys: dict[str, str] = {}
    today = date.today().isoformat()

    for instrument_key, cfg in TRACKED.items():
        index_keys[instrument_key] = cfg["label"]
        expiries = [e for e in list_expiries(instrument_key) if e > today][: cfg["n_expiries"]]
        for expiry in expiries:
            for c in get_option_contracts(instrument_key, expiry):
                option_reference[c["instrument_key"]] = {
                    "underlying": cfg["label"],
                    "expiry": expiry,
                    "strike_paise": round(c["strike_price"] * 100),
                    "option_type": c["instrument_type"],
                }
    return option_reference, index_keys


class LiveBook:
    def __init__(self, option_reference: dict[str, dict]):
        self.reference = option_reference
        self.quotes: dict[str, OptionQuote] = {}
        self.spot_paise: dict[str, int] = {}  # underlying label -> spot

    def apply_index_feed(self, underlying_label: str, ltp: float) -> None:
        if ltp > 0:
            self.spot_paise[underlying_label] = round(ltp * 100)

    def apply_option_feed(self, instrument_key: str, feed) -> tuple[str, str] | None:
        """Returns the (underlying, expiry) group key that changed, or None
        if this instrument/message carries nothing usable."""
        meta = self.reference.get(instrument_key)
        if meta is None:
            return None

        which = feed.fullFeed.WhichOneof("FullFeedUnion")
        if which != "marketFF":
            return None
        market = feed.fullFeed.marketFF
        if not market.marketLevel.bidAskQuote:
            return None
        top = market.marketLevel.bidAskQuote[0]
        # BUG-5's lesson applies just as much here: a one-sided or missing
        # quote must never be treated as a real, tradeable zero price.
        if top.bidP <= 0 or top.askP <= 0:
            return None

        self.quotes[instrument_key] = OptionQuote(
            instrument_key=instrument_key,
            option_type=meta["option_type"],
            bid_paise=round(top.bidP * 100),
            ask_paise=round(top.askP * 100),
        )
        return (meta["underlying"], meta["expiry"])

    def group(self, underlying: str, expiry: str) -> tuple[dict[int, OptionQuote], dict[int, OptionQuote]]:
        calls: dict[int, OptionQuote] = {}
        puts: dict[int, OptionQuote] = {}
        for key, quote in self.quotes.items():
            meta = self.reference[key]
            if meta["underlying"] == underlying and meta["expiry"] == expiry:
                (calls if quote.option_type == "CE" else puts)[meta["strike_paise"]] = quote
        return calls, puts


def check_group(underlying: str, expiry: str, calls: dict, puts: dict, spot_paise: int | None) -> list[tuple[str, object]]:
    today = date.today()
    expiry_date = date.fromisoformat(expiry)
    years_to_expiry = max((expiry_date - today).days, 0) / 365.0

    violations: list[tuple[str, object]] = []
    for label, book in (("CE", calls), ("PE", puts)):
        for v in check_vertical_spread(list(book.items()), label):
            violations.append(("vertical_spread", v))

    common_strikes = sorted(set(calls) & set(puts))
    if common_strikes and spot_paise:
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

    return violations


def build_subscribe_message(instrument_keys: list[str], mode: str = "full_d5") -> bytes:
    """The doc page's prose says the request must be sent in binary format,
    but only ever shows a JSON-shaped payload -- best-effort read (not
    independently confirmed against a real successful subscribe yet, since
    that requires a live NSE trading-hours connection): UTF-8 encode the
    JSON and send it as a binary WebSocket frame rather than a text frame.
    Flagged clearly rather than asserted as certain -- verify the moment a
    live subscribe actually succeeds."""
    message = {"guid": str(uuid.uuid4()), "method": "sub", "data": {"mode": mode, "instrumentKeys": instrument_keys}}
    return json.dumps(message).encode("utf-8")


async def _connect_and_stream(option_reference: dict, index_keys: dict, book: "LiveBook") -> None:
    """One connection attempt. Raises (connection-level exceptions propagate
    to the caller) rather than swallowing errors -- reconnect/backoff policy
    lives in run_forever(), not here, so this stays a single, testable
    responsibility: get a fresh authorized URL, connect, subscribe, process
    messages until the connection drops."""
    import websockets

    url = get_ws_authorized_url()  # fetched fresh every attempt -- the embedded code is single-use
    all_keys = list(option_reference.keys()) + list(index_keys.keys())

    async with websockets.connect(url) as ws:
        log.info("Connected. Subscribing to %d instruments...", len(all_keys))
        await ws.send(build_subscribe_message(all_keys))

        # Heartbeat counters: without these, silence is ambiguous -- "no
        # violations found" and "not receiving any messages at all" look
        # identical in the logs otherwise. Logged periodically rather than
        # per-message so this doesn't drown out real VIOLATION lines.
        messages_received = 0
        option_updates_processed = 0
        violations_found = 0
        HEARTBEAT_EVERY = 50

        async for raw in ws:
            messages_received += 1
            if isinstance(raw, str):
                log.warning("Received unexpected text frame: %s", raw[:200])
                continue

            response = FeedResponse()
            response.ParseFromString(raw)

            for instrument_key, feed in response.feeds.items():
                if instrument_key in index_keys:
                    if feed.fullFeed.WhichOneof("FullFeedUnion") == "indexFF":
                        book.apply_index_feed(index_keys[instrument_key], feed.fullFeed.indexFF.ltpc.ltp)
                    continue

                group_key = book.apply_option_feed(instrument_key, feed)
                if group_key is None:
                    continue
                option_updates_processed += 1

                underlying, expiry = group_key
                calls, puts = book.group(underlying, expiry)
                violations = check_group(underlying, expiry, calls, puts, book.spot_paise.get(underlying))
                for vtype, v in violations:
                    violations_found += 1
                    log.info("VIOLATION [%s %s %s]: %s", underlying, expiry, vtype, v)

            if messages_received % HEARTBEAT_EVERY == 0:
                log.info(
                    "Heartbeat: %d messages received, %d option updates processed, %d violations found so far",
                    messages_received, option_updates_processed, violations_found,
                )


async def run(max_reconnects: int | None = None) -> None:
    """Reconnect-on-drop loop with exponential backoff (capped), matching
    the same discipline as the Kalshi-phase groundwork's retry/backoff
    (FOUNDATIONS.md S27) and BUG-2's REST connection-reuse lesson -- a live
    feed WILL drop occasionally (network blips, server-side resets), and a
    single-shot client that just dies on the first disconnect isn't a
    real-time detector, it's a demo. Reference data (option_reference,
    index_keys) is built once and reused across reconnects since strikes/
    expiries don't change intraday -- only the WS URL needs refreshing per
    attempt (BUG-6: its embedded code is single-use).

    max_reconnects=None means retry forever; pass a number to cap it (e.g.
    for testing, or a bounded demo run).
    """
    # Explicit submodule import, not a bare `import websockets` -- that alone
    # does NOT reliably make `websockets.exceptions` accessible as an
    # attribute in this library version (it's lazily loaded), which would
    # make the except clause below raise AttributeError instead of catching
    # a real connection drop. Caught by a test simulating an actual drop,
    # not by inspection -- the bare-import version looked correct on read.
    import websockets.exceptions

    option_reference, index_keys = build_reference_table()
    book = LiveBook(option_reference)
    log.info("Reference table built: %d option legs, %d indices", len(option_reference), len(index_keys))

    backoff_seconds = 1.0
    max_backoff_seconds = 60.0
    attempt = 0

    while max_reconnects is None or attempt < max_reconnects:
        attempt += 1
        try:
            await _connect_and_stream(option_reference, index_keys, book)
            # A clean return (server closed normally) is still a drop from a
            # detection standpoint -- reconnect rather than exit silently.
            log.warning("WebSocket closed cleanly; reconnecting")
            backoff_seconds = 1.0  # a clean run resets backoff -- this wasn't a repeated failure
        except (websockets.exceptions.ConnectionClosed, OSError) as e:
            log.warning("Connection dropped (%s: %s), reconnecting in %.0fs", type(e).__name__, e, backoff_seconds)
            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, max_backoff_seconds)


if __name__ == "__main__":
    asyncio.run(run())
