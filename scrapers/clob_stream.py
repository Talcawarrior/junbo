"""Polymarket CLOB market and authenticated user streams.

The stream is intentionally an opt-in primitive.  Callers own persistence and
can use the same class in dry-run tests with a fake websocket.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

logger = logging.getLogger("SCRAPER_CLOB_STREAM")

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


def _proxy_url() -> str | None:
    """Polymarket SOCKS proxy (POLY_PROXY) — geo-block bypass. WebSocket de
    ayni proxy'den gitmeli; yoksa direct -> getaddrinfo failed (2026-08-17).

    aiohttp_socks/python_socks sadece 'socks5://' sehemasini kabul eder;
    'socks5h://' (hostname'i proxy'de cozen) desteklenmez. aiohttp_socks
    zaten DNS'i proxy uzerinden yaptigi icin socks5 yeterli.
    """
    try:
        from config.settings import bot_config

        # 2026-08-17 BUGFIX: get_proxies() proxy_url bosken None doner -
        # None.get() AttributeError uretiyordu (try/except yutuyordu, sessizce
        # proxy'siz kaliniyordu). None-guard eklendi.
        proxies = bot_config.polymarket.get_proxies() or {}
        url = proxies.get("https")
        if url and url.startswith("socks5h://"):
            return "socks5://" + url[len("socks5h://") :]
        return url
    except Exception:
        return None


def _make_connector():
    """aiohttp_socks.ProxyConnector — SOCKS5 proxy ile WebSocket baglantisi."""
    from aiohttp_socks import ProxyConnector

    return ProxyConnector.from_url(_proxy_url())


def _event(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    # Some server messages are envelopes; keep the event type at the top level
    # while retaining the original fields for forward compatibility.
    return payload


class CLOBMarketStream:
    def __init__(
        self, asset_ids: list[str], on_event: Callable[[dict[str, Any]], Awaitable[None] | None], *, session=None
    ):
        self.asset_ids = [str(value) for value in asset_ids if value]
        self.on_event = on_event
        self.session = session

    async def run(self, stop: asyncio.Event | None = None, *, max_retries: int | None = None) -> None:
        stop = stop or asyncio.Event()
        retries = 0
        while not stop.is_set() and (max_retries is None or retries <= max_retries):
            try:
                await self._run_connection(stop)
                retries = 0
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as exc:
                retries += 1
                if stop.is_set():
                    break
                delay = min(30.0, 2.0 ** min(retries, 4))
                logger.warning("CLOB market stream disconnected: %s; retrying in %.1fs", exc, delay)
                await asyncio.sleep(delay)

    async def _run_connection(self, stop: asyncio.Event) -> None:
        owns_session = self.session is None
        session = self.session or aiohttp.ClientSession(connector=_make_connector() if _proxy_url() else None)
        try:
            async with session.ws_connect(MARKET_WS_URL, heartbeat=None) as ws:
                await ws.send_json({"assets_ids": self.asset_ids, "type": "market", "custom_feature_enabled": True})
                await self._consume(ws, stop)
        finally:
            if owns_session:
                await session.close()

    async def _consume(self, ws, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=10.0)
            except asyncio.TimeoutError:
                await ws.send_str("PING")
                continue
            if msg.type == aiohttp.WSMsgType.TEXT:
                if msg.data == "PONG":
                    continue
                try:
                    payload = _event(json.loads(msg.data))
                except json.JSONDecodeError:
                    logger.warning("Ignoring malformed CLOB market event")
                    continue
                if payload:
                    result = self.on_event(payload)
                    if asyncio.iscoroutine(result):
                        await result
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                raise ConnectionError("CLOB market websocket closed")


class CLOBUserStream(CLOBMarketStream):
    """Authenticated order/trade stream with authoritative reconnect recovery."""

    def __init__(
        self,
        auth: dict[str, str],
        on_event,
        *,
        markets=None,
        recover: Callable[[], Awaitable[None] | None] | None = None,
        session=None,
    ):
        super().__init__([], on_event, session=session)
        self.auth = auth
        self.markets = [str(value) for value in (markets or []) if value]
        self.recover = recover
        self._connected_once = False

    async def _run_connection(self, stop: asyncio.Event) -> None:
        owns_session = self.session is None
        session = self.session or aiohttp.ClientSession(connector=_make_connector() if _proxy_url() else None)
        try:
            async with session.ws_connect(USER_WS_URL, heartbeat=None) as ws:
                await ws.send_json({"auth": self.auth, "markets": self.markets, "type": "user"})
                if self._connected_once and self.recover:
                    result = self.recover()
                    if asyncio.iscoroutine(result):
                        await result
                self._connected_once = True
                await self._consume(ws, stop)
        finally:
            if owns_session:
                await session.close()
