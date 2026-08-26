import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "data"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "data" / "proto"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "models"))

from MarketDataFeed_pb2 import Feed, FullFeed, IndexFullFeed, LTPC, MarketFullFeed, MarketLevel, Quote

import ws_stream
from ws_stream import LiveBook, build_subscribe_message, check_group


def _option_feed(bid, ask, bid_q=10, ask_q=10) -> Feed:
    feed = Feed()
    feed.fullFeed.marketFF.marketLevel.bidAskQuote.append(Quote(bidQ=bid_q, bidP=bid, askQ=ask_q, askP=ask))
    return feed


def _index_feed(ltp) -> Feed:
    feed = Feed()
    feed.fullFeed.indexFF.ltpc.ltp = ltp
    return feed


REFERENCE = {
    "NSE_FO|C1": {"underlying": "NIFTY", "expiry": "2026-09-01", "strike_paise": 2450000, "option_type": "CE"},
    "NSE_FO|C2": {"underlying": "NIFTY", "expiry": "2026-09-01", "strike_paise": 2460000, "option_type": "CE"},
    "NSE_FO|P1": {"underlying": "NIFTY", "expiry": "2026-09-01", "strike_paise": 2450000, "option_type": "PE"},
}


def test_apply_option_feed_stores_quote_and_returns_group_key():
    book = LiveBook(REFERENCE)
    group_key = book.apply_option_feed("NSE_FO|C1", _option_feed(bid=150.0, ask=152.0))
    assert group_key == ("NIFTY", "2026-09-01")
    assert book.quotes["NSE_FO|C1"].bid_paise == 15000
    assert book.quotes["NSE_FO|C1"].ask_paise == 15200


def test_apply_option_feed_unknown_instrument_returns_none():
    book = LiveBook(REFERENCE)
    assert book.apply_option_feed("NSE_FO|UNKNOWN", _option_feed(150.0, 152.0)) is None


def test_apply_option_feed_zero_sided_quote_skipped():
    """Same BUG-5 lesson, applied to the live feed path: a missing/zero side
    must never be treated as a real price."""
    book = LiveBook(REFERENCE)
    group_key = book.apply_option_feed("NSE_FO|C1", _option_feed(bid=0.0, ask=152.0))
    assert group_key is None
    assert "NSE_FO|C1" not in book.quotes


def test_apply_option_feed_no_depth_returns_none():
    feed = Feed()
    feed.fullFeed.marketFF.oi = 100  # touch marketFF without adding any depth
    book = LiveBook(REFERENCE)
    assert book.apply_option_feed("NSE_FO|C1", feed) is None


def test_apply_index_feed_stores_spot():
    book = LiveBook(REFERENCE)
    book.apply_index_feed("NIFTY", 24334.55)
    assert book.spot_paise["NIFTY"] == 2433455


def test_group_separates_calls_and_puts_by_underlying_expiry():
    book = LiveBook(REFERENCE)
    book.apply_option_feed("NSE_FO|C1", _option_feed(150.0, 152.0))
    book.apply_option_feed("NSE_FO|C2", _option_feed(90.0, 92.0))
    book.apply_option_feed("NSE_FO|P1", _option_feed(80.0, 82.0))
    calls, puts = book.group("NIFTY", "2026-09-01")
    assert set(calls.keys()) == {2450000, 2460000}
    assert set(puts.keys()) == {2450000}


def test_check_group_detects_vertical_violation_live():
    book = LiveBook(REFERENCE)
    book.apply_option_feed("NSE_FO|C1", _option_feed(bid=150.0, ask=152.0))
    book.apply_option_feed("NSE_FO|C2", _option_feed(bid=153.0, ask=155.0))  # crossed vs C1's ask
    calls, puts = book.group("NIFTY", "2026-09-01")
    violations = check_group("NIFTY", "2026-09-01", calls, puts, spot_paise=None)
    assert any(vtype == "vertical_spread" for vtype, _ in violations)


def test_check_group_skips_parity_without_spot():
    book = LiveBook(REFERENCE)
    book.apply_option_feed("NSE_FO|C1", _option_feed(150.0, 152.0))
    book.apply_option_feed("NSE_FO|P1", _option_feed(80.0, 82.0))
    calls, puts = book.group("NIFTY", "2026-09-01")
    violations = check_group("NIFTY", "2026-09-01", calls, puts, spot_paise=None)
    assert all(vtype != "put_call_parity" for vtype, _ in violations)


def test_build_subscribe_message_shape():
    raw = build_subscribe_message(["NSE_FO|C1", "NSE_INDEX|Nifty 50"], mode="full_d5")
    assert isinstance(raw, bytes)
    parsed = json.loads(raw.decode("utf-8"))
    assert parsed["method"] == "sub"
    assert parsed["data"]["mode"] == "full_d5"
    assert parsed["data"]["instrumentKeys"] == ["NSE_FO|C1", "NSE_INDEX|Nifty 50"]
    assert "guid" in parsed


# --- Reconnect-on-drop loop ---

def test_run_reconnects_after_drop_and_respects_max_reconnects(monkeypatch):
    monkeypatch.setattr(ws_stream, "build_reference_table", lambda: ({}, {}))

    attempts = []
    sleeps = []

    async def fake_connect_and_stream(option_reference, index_keys, book):
        attempts.append(1)
        raise OSError("simulated connection drop")

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(ws_stream, "_connect_and_stream", fake_connect_and_stream)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    asyncio.run(ws_stream.run(max_reconnects=3))

    assert len(attempts) == 3
    assert len(sleeps) == 3


def test_run_backoff_increases_and_caps(monkeypatch):
    monkeypatch.setattr(ws_stream, "build_reference_table", lambda: ({}, {}))

    sleeps = []

    async def always_drops(option_reference, index_keys, book):
        raise OSError("simulated connection drop")

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(ws_stream, "_connect_and_stream", always_drops)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    asyncio.run(ws_stream.run(max_reconnects=5))

    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0]  # doubling, hasn't hit the 60s cap yet


def test_run_clean_close_reconnects_and_resets_backoff(monkeypatch):
    monkeypatch.setattr(ws_stream, "build_reference_table", lambda: ({}, {}))

    call_count = {"n": 0}
    sleeps = []

    async def drop_then_close_cleanly(option_reference, index_keys, book):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("simulated connection drop")
        return None  # clean close, no exception

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(ws_stream, "_connect_and_stream", drop_then_close_cleanly)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    asyncio.run(ws_stream.run(max_reconnects=2))

    assert call_count["n"] == 2
    assert sleeps == [1.0]  # only the first (dropped) attempt slept; the clean close didn't
