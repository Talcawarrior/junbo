"""Tests for the auto-restart-after-reset UX fix."""

from unittest.mock import AsyncMock, patch

import pytest

# Match the pattern used by tests/test_api_integration.py: the FastAPI
# TestClient depends on httpx (or the new httpx2 fork), which isn't in
# our minimal CI runner's install. Skip cleanly when it's missing
# instead of failing collection.
httpx = pytest.importorskip("httpx", reason="httpx not installed (needed for FastAPI TestClient)")
from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


def test_reset_clears_state_without_auto_restart(client):
    """POST /api/reset clears bets/portfolio but does NOT auto-start."""
    with patch("api.start_bot", new=AsyncMock(return_value={"status": "started"})) as mock_start:
        r = client.post("/api/reset")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "reset"
        # Reset does NOT auto-start (manual start required)
        assert not mock_start.called
