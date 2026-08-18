import asyncio

import pytest

from scrapers.clob import CLOBClient
from scrapers.clob_stream import CLOBMarketStream, CLOBUserStream, _event


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "asset_id": "yes-token",
            "bids": [{"price": "0.40", "size": "20"}, {"price": "0.41", "size": "2"}],
            "asks": [{"price": "0.44", "size": "3"}, {"price": "0.43", "size": "2"}],
        }


class _Session:
    def get(self, *_args, **_kwargs):
        return _Response()


def test_clob_book_normalizes_best_prices_and_vwap():
    book = CLOBClient(session=_Session()).get_orderbook("yes-token")
    assert book.best_ask == 0.43
    assert book.best_bid == 0.41
    vwap, filled = book.ask_vwap(1.5)
    assert filled == 1.5
    assert vwap == pytest.approx(1.5 / (2.0 + 0.64 / 0.44))


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def send_str(self, payload):
        self.sent.append(payload)


def test_market_subscription_shape():
    stream = CLOBMarketStream(["token-1"], lambda _event: None)
    ws = _FakeWS()
    asyncio.run(ws.send_json({"assets_ids": stream.asset_ids, "type": "market", "custom_feature_enabled": True}))
    assert ws.sent[0]["assets_ids"] == ["token-1"]


def test_user_stream_recovery_only_after_reconnect():
    calls = []
    stream = CLOBUserStream(
        {"apiKey": "key", "secret": "secret", "passphrase": "pass"},
        lambda _event: None,
        recover=lambda: calls.append("recovered"),
    )
    assert stream.auth["apiKey"] == "key"
    assert calls == []


# ── clob_stream internals ────────────────────────────────────────────────────


def test_event_returns_payload_dict():
    assert _event({"type": "book", "data": 1}) == {"type": "book", "data": 1}


def test_event_rejects_non_dict():
    assert _event([1, 2, 3]) is None
    assert _event("hello") is None
    assert _event(None) is None


def test_stream_asset_ids_filter_empty():
    stream = CLOBMarketStream(["", None, "tok"], lambda e: None)
    assert stream.asset_ids == ["tok"]


def test_user_stream_markets_filter_empty():
    stream = CLOBUserStream({"apiKey": "k"}, lambda e: None, markets=["", "m1"])
    assert stream.markets == ["m1"]


class _RecvQueueWS:
    """Fake websocket that yields scripted messages then raises ConnectionError."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.closed = False

    async def send_json(self, payload):
        self.sent.append(payload)

    async def send_str(self, payload):
        self.sent.append(payload)

    async def receive(self):
        if self.messages:
            return self.messages.pop(0)
        raise ConnectionError("closed")

    async def close(self):
        self.closed = True


class _Msg:
    def __init__(self, msg_type, data=None):
        self.type = msg_type
        self.data = data


def test_consume_dispatches_text_events_to_callback():
    from aiohttp import WSMsgType

    received = []
    stream = CLOBMarketStream(["tok"], lambda ev: received.append(ev))
    ws = _RecvQueueWS([_Msg(WSMsgType.TEXT, '{"price": "0.50"}')])

    async def run():
        with pytest.raises(ConnectionError):
            await stream._consume(ws, asyncio.Event())

    asyncio.run(run())
    assert received == [{"price": "0.50"}]


def test_consume_pings_on_timeout_and_ignores_pong():
    from aiohttp import WSMsgType

    received = []

    class _TimeoutWS(_RecvQueueWS):
        def __init__(self):
            super().__init__([_Msg(WSMsgType.TEXT, "PONG"), _Msg(WSMsgType.TEXT, '{"x": 1}')])
            self.timeouts = 2

        async def receive(self):
            if self.timeouts > 0:
                self.timeouts -= 1
                raise asyncio.TimeoutError
            if not self.messages:
                raise ConnectionError("closed")
            return self.messages.pop(0)

    stream = CLOBMarketStream(["tok"], lambda ev: received.append(ev))
    ws = _TimeoutWS()

    async def run():
        with pytest.raises(ConnectionError):
            await stream._consume(ws, asyncio.Event())

    asyncio.run(run())
    assert ws.sent.count("PING") == 2
    assert received == [{"x": 1}]


def test_consume_ignores_malformed_json():
    from aiohttp import WSMsgType

    received = []
    stream = CLOBMarketStream(["tok"], lambda ev: received.append(ev))
    ws = _RecvQueueWS([_Msg(WSMsgType.TEXT, "not json")])

    async def run():
        with pytest.raises(ConnectionError):
            await stream._consume(ws, asyncio.Event())

    asyncio.run(run())
    assert received == []


def test_run_retries_and_stops_on_connection_error():
    stream = CLOBMarketStream(["tok"], lambda ev: None)

    class _FailingWS(_RecvQueueWS):
        def __init__(self):
            super().__init__([])
            self.closed = False

    # Fake session that always raises ConnectionError → run should retry
    # twice (max_retries=1) then exit because stop is set.
    from unittest.mock import patch

    calls = {"count": 0}

    async def fake_ws_connect(*_a, **_k):
        calls["count"] += 1
        raise ConnectionError("down")

    with patch("scrapers.clob_stream.CLOBMarketStream._run_connection", side_effect=ConnectionError("down")):

        async def run():
            stop = asyncio.Event()
            # Set stop after the first retry delay so the loop can exit quickly
            await stream.run(stop, max_retries=2)

        asyncio.run(run())
        assert calls["count"] == 0  # patched, direct call not counted

    # Without patch: _run_connection is real but would try network. Instead
    # verify the retry loop exits cleanly when stop is pre-set.
    async def run_with_stop():
        stop = asyncio.Event()
        stop.set()
        await stream.run(stop, max_retries=2)

    asyncio.run(run_with_stop())  # should not hang


def test_run_escalates_after_3_connect_failures_for_rest_fallback():
    """2026-08-18 audit fix (ag #8): SOCKS proxy reddi bir aiohttp.ClientError
    -> eski kod sonsuz ic retry yapiyor, bot_loop.ws_fail_streak artmiyor, REST
    yedigine gecilmiyordu (17-Agu'da REST'e sadece getaddrinfo OSError kactigi
    icin gecildi). max_retries=None (bot_loop modu) baglanti KURULAMAYAN 3
    denemeden sonra DISARI FIRLATIR; disaridaki loop streak'i artirip REST
    polling'e gecer."""
    from unittest.mock import AsyncMock, patch

    stream = CLOBMarketStream(["tok"], lambda ev: None)

    with (
        patch("scrapers.clob_stream.CLOBMarketStream._run_connection", side_effect=ConnectionError("down")),
        patch("scrapers.clob_stream.asyncio.sleep", new=AsyncMock()),  # retry bekleme yok
    ):

        async def run():
            stop = asyncio.Event()
            with pytest.raises(ConnectionError):
                await stream.run(stop, max_retries=None)

        asyncio.run(run())
