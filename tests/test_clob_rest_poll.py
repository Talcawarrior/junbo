"""CLOB REST yedegi + arsiv kilidi regresyonlari (2026-11-18).

1. _clob_rest_poll_once: marketler get_session() icinde ORM nesnesi olarak
   yukleniyor, raw_data session kapandiktan sonra okunuyordu ->
   DetachedInstanceError yutuluyor, HIC HTTP istegi atilmiyordu.
2. _archive_clob_prices: tek baglanti + tek transaction ile JUNBO_DATA_DIR
   altindaki orderbook.db'ye yazar.
3. WS _on_event: arsiv yazimi event loop DISINDA (asyncio.to_thread).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from database.db import get_session
from database.models import WeatherMarket

_IDS = ("rest-poll-t1", "rest-poll-t2", "rest-poll-t3")


def _seed(ids, status="open"):
    tgt = datetime(2030, 1, 2, 0, 0, 0)
    with get_session() as s:
        for i, mid in enumerate(ids):
            s.add(
                WeatherMarket(
                    id=mid,
                    question=f"q-{mid}",
                    city="Testville",
                    metric="temperature_max",
                    threshold=10.0 + i,
                    target_date=tgt,
                    status=status,
                    yes_price=0.10,
                    last_updated=datetime(2030, 1, 1) - timedelta(hours=3),
                    raw_data=json.dumps({"clobTokenIds": json.dumps([f"tok-{mid}", f"no-{mid}"])}),
                )
            )


def _cleanup(ids):
    with get_session() as s:
        s.query(WeatherMarket).filter(WeatherMarket.id.in_(list(ids))).delete(synchronize_session=False)


@pytest.fixture
def seeded():
    _cleanup(_IDS)
    _seed(_IDS)
    yield _IDS
    _cleanup(_IDS)


class _Resp:
    status_code = 200

    def __init__(self, ask):
        self._ask = ask

    def json(self):
        return {"asks": [{"price": str(self._ask), "size": "10"}, {"price": "0.99", "size": "5"}], "bids": []}


def test_rest_poll_reaches_network_archives_and_refreshes_prices(seeded, monkeypatch):
    import requests

    import bot_loop

    urls = []

    def _fake_get(url, *a, **k):
        urls.append(url)
        return _Resp(0.42)

    archived = []
    monkeypatch.setattr(requests, "get", _fake_get)
    monkeypatch.setattr(bot_loop, "_archive_clob_prices", lambda items: archived.extend(items) or len(items))

    bot_loop._clob_rest_poll_once()

    hit = [u for u in urls if any(f"tok-{mid}" in u for mid in seeded)]
    assert len(hit) == len(seeded), "REST yedegi her acik market icin /book istegi atmali"
    assert {str(m.id) for m, _ask in archived} >= set(seeded)
    with get_session() as s:
        rows = s.query(WeatherMarket.id, WeatherMarket.yes_price).filter(WeatherMarket.id.in_(list(seeded))).all()
    assert all(abs(float(p) - 0.42) < 1e-9 for _id, p in rows), "yes_price canli ask ile tazelenmeli"


def test_archive_clob_prices_single_transaction_to_data_dir(tmp_path, monkeypatch):
    import bot_loop

    monkeypatch.setenv("JUNBO_DATA_DIR", str(tmp_path))
    con = sqlite3.connect(tmp_path / "orderbook.db")
    con.execute(
        """CREATE TABLE orderbook_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT, token_id TEXT, city TEXT, metric TEXT, target_date TEXT,
            threshold REAL, bid_depth_usd REAL, ask_depth_usd REAL, best_bid REAL,
            best_ask REAL, spread REAL, num_bid_levels INTEGER, num_ask_levels INTEGER,
            snapshot_time TEXT, created_at TEXT
        )"""
    )
    con.commit()
    con.close()

    items = [
        (
            SimpleNamespace(
                id=f"m{i}", city="X", metric="temperature_max", target_date=datetime(2030, 1, 2), threshold=float(i)
            ),
            0.1 * (i + 1),
        )
        for i in range(3)
    ]
    assert bot_loop._archive_clob_prices(items) == 3

    con = sqlite3.connect(tmp_path / "orderbook.db")
    rows = con.execute(
        "SELECT market_id, token_id, target_date, best_ask FROM orderbook_snapshots ORDER BY id"
    ).fetchall()
    con.close()
    assert [r[0] for r in rows] == ["m0", "m1", "m2"]
    assert {r[1] for r in rows} == {"0"}
    assert rows[0][2] == "2030-01-02 00:00:00"


@pytest.mark.asyncio
async def test_ws_archive_write_runs_off_event_loop(monkeypatch):
    import asyncio

    import bot_loop
    import scrapers.clob_stream as cs

    mid = "ws-arch-t1"
    _cleanup([mid])
    _seed([mid])
    state = SimpleNamespace(is_running=True)

    class _FakeStream:
        def __init__(self, assets, on_event, **_k):
            self.on_event = on_event

        async def run(self, stop=None, max_retries=None):
            await self.on_event({"market": mid, "price": 0.37})
            state.is_running = False

    offloaded = []

    async def _fake_to_thread(fn, *args, **kwargs):
        offloaded.append(fn)
        return None

    monkeypatch.setattr(cs, "CLOBMarketStream", _FakeStream)
    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    direct = []
    monkeypatch.setattr(bot_loop, "_archive_clob_prices", lambda items: direct.extend(items) or 0)
    try:
        await bot_loop.clob_stream_loop(state)
    finally:
        _cleanup([mid])

    assert bot_loop._archive_clob_price in offloaded, "arsiv yazimi to_thread ile event loop disina alinmali"
    assert direct == [], "arsiv event loop icinde senkron cagrilmamali"
