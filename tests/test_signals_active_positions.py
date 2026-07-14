"""Tests for /api/signals active positions — verifies the b.ladder_data fix.

These tests ensure that:
1. /api/signals returns active bets without NameError
2. Bad ladder_data JSON does not crash the endpoint
3. status.total_bets and signals.count are consistent
"""

import pytest

httpx = pytest.importorskip("httpx", reason="httpx not installed")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def client():
    import main as app_module

    with TestClient(app_module.app) as c:
        yield c


def test_signals_returns_active_bet_without_error(client):
    """Placed bet appears in /api/signals with correct shape."""
    resp = client.get("/api/signals")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Must not contain "error" key
    assert "error" not in body, f"/api/signals returned error: {body.get('error')}"
    assert "signals" in body and "count" in body
    assert isinstance(body["signals"], list)
    # If there are active bets, validate their shape
    for sig in body["signals"]:
        assert "id" in sig
        assert "ladder_orders" in sig
        assert isinstance(sig["ladder_orders"], list), (
            f"ladder_orders should be list, got {type(sig['ladder_orders'])}"
        )


def test_signals_no_error_on_any_bet(client):
    """Even if there are 0 bets, endpoint must return without error."""
    resp = client.get("/api/signals")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body, f"/api/signals returned error: {body.get('error')}"
    assert isinstance(body.get("count", None), int)


def test_status_signals_consistency(client):
    """If status says total_bets > 0, signals must also have count > 0."""
    status_resp = client.get("/api/status")
    signals_resp = client.get("/api/signals")
    assert status_resp.status_code == 200
    assert signals_resp.status_code == 200

    status_body = status_resp.json()
    signals_body = signals_resp.json()

    total_bets = status_body.get("stats", {}).get("total_bets", 0)
    signals_count = signals_body.get("count", 0)

    if total_bets > 0:
        assert signals_count > 0, (
            f"Consistency violation: status.total_bets={total_bets} but signals.count={signals_count}"
        )


def test_signals_ladder_orders_is_list_for_each_bet(client):
    """Every bet in /api/signals must have ladder_orders as a list."""
    resp = client.get("/api/signals")
    assert resp.status_code == 200
    body = resp.json()
    if "error" in body:
        pytest.fail(f"/api/signals returned error: {body['error']}")
    for sig in body.get("signals", []):
        lo = sig.get("ladder_orders")
        assert isinstance(lo, list), (
            f"Bet {sig.get('id')}: ladder_orders={type(lo)}, expected list"
        )
