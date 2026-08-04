"""Integration tests for the FastAPI app exposed by main.py.

These tests exercise the HTTP layer end-to-end (no mocks of the app itself)
against the live `data/bot.db` (SQLite, WAL mode, read-only open). The aim
is to lock down the JSON shape of the most-used endpoints (`/api/status`,
`/api/signals`, `/api/markets`) so the dashboard never breaks silently when
the schema drifts.

Why not a temp DB: `database.db.engine` is built at module-import time from
`config.settings.config.DB_PATH` and is reachable by every other module on
import. Patching the engine after import is racy; the cleanest path is to
read the real DB (WAL allows concurrent readers).

Why this file skips on missing httpx: the FastAPI TestClient needs httpx.
Local dev and the bot both install it transitively (via fastapi +
starlette >= 0.21), but minimal CI runners sometimes don't. We skip
gracefully rather than failing collection.
"""

import pytest

httpx = pytest.importorskip(
    "httpx", reason="httpx not installed (needed for FastAPI TestClient)"
)
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def client():
    """TestClient that talks to the real FastAPI app via in-process HTTP."""
    import main as app_module

    with TestClient(app_module.app) as c:
        yield c


def test_status_endpoint_returns_known_shape(client):
    """`/api/status` is the heart of the dashboard - its shape must be stable."""
    resp = client.get("/api/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    for key in ("is_running", "portfolio", "stats", "limits"):
        assert key in body, f"missing '{key}' in /api/status: {list(body.keys())}"

    p = body["portfolio"]
    for key in ("initial", "current"):
        assert key in p, f"missing portfolio.{key}"
    assert isinstance(p["initial"], (int, float))
    assert p["initial"] > 0


def test_signals_endpoint_returns_wrapped_list(client):
    """`/api/signals` returns `{"signals": [...], "count": N}`. Every item
    must have the contract the dashboard's signals table relies on."""
    resp = client.get("/api/signals")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "signals" in body and "count" in body
    assert isinstance(body["signals"], list)
    assert body["count"] == len(body["signals"])
    for bet in body["signals"]:
        for key in ("id", "city", "entry_price", "current_price", "stake_amount"):
            assert key in bet, f"missing '{key}' in bet: {list(bet.keys())}"
        # UX fix #6 (PR #6) split Edge into entry/live/move; legacy `edge`
        # key must remain as an alias for back-compat.
        for key in ("entry_edge", "live_edge", "move_pct", "edge"):
            assert key in bet, f"missing '{key}' in bet (UX fix #6 contract)"


def test_markets_endpoint_returns_wrapped_list(client):
    """`/api/markets` returns `{"markets": [...], "count": N}` with the
    contract the dashboard's markets table relies on."""
    resp = client.get("/api/markets")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "markets" in body and "count" in body
    assert isinstance(body["markets"], list)
    assert body["count"] == len(body["markets"])
    for m in body["markets"]:
        for key in ("id", "city", "current_yes_bid", "current_no_bid", "model_prob"):
            assert key in m, f"missing '{key}' in market: {list(m.keys())}"


def test_dashboard_html_served(client):
    """The dashboard SPA must be served at `/` and contain expected content."""
    resp = client.get("/")
    assert resp.status_code == 200, resp.text
    body = resp.text
    # Accept either the Next.js built page or the fallback message
    assert "Junbo" in body or "Dashboard" in body
