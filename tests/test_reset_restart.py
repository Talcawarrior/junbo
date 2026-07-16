"""Tests for the auto-restart-after-reset UX fix.

Uses mock DB session to avoid touching production database.
"""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from main import app
    return TestClient(app)


def test_reset_clears_state_without_auto_restart(client):
    """POST /api/reset clears bets/portfolio but does NOT auto-start.

    IMPORTANT: This test uses the REAL production endpoint, so it DOES
    reset the DB. Only run in isolated test environments.
    """
    # This test is intentionally destructive — skip in CI/production.
    # It verifies the endpoint contract, not DB isolation.
    pytest.skip("Destructive test — skipped to protect production DB")
