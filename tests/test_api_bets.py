"""FastAPI TestClient tests for GET /api/bets endpoint + lazy init_db.

Uses the production DB (data/bot.db) like other integration tests, inserting
test bets and cleaning them up in fixtures.
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

# ── Module-scoped client (same pattern as test_api_integration.py) ─────


@pytest.fixture(scope="module")
def client():
    import main as app_module

    with TestClient(app_module.app) as c:
        yield c


# ── Helpers ────────────────────────────────────────────────────────────


def _clean_bets(session, prefix="test-api-bets-"):
    """Remove test bets with matching market_id prefix using raw SQL."""
    from sqlalchemy import text

    session.execute(
        text("DELETE FROM bets WHERE market_id LIKE :pat"),
        {"pat": f"{prefix}%"},
    )
    session.commit()


def _insert_bets(session, n=5, status="won", prefix="test-api-bets"):
    """Insert n test bets into the production DB."""
    from database.models import Bet

    for i in range(n):
        mkt_id = f"{prefix}-{status}-{i:03d}"
        b = Bet(
            market_id=mkt_id,
            city="Miami",
            side="YES",
            amount=50.0,
            entry_price=0.6,
            current_price=0.65,
            status=status,
            realized_pnl=10.0 if status == "won" else -10.0,
            unrealized_pnl=0.0,
            placed_at=datetime.now(timezone.utc) - timedelta(hours=i),
            settled_at=(
                datetime.now(timezone.utc) if status in ("won", "lost") else None
            ),
        )
        session.add(b)
    session.commit()


# ── Tests ──────────────────────────────────────────────────────────────


class TestApiBetsEndpoint:
    """3 tests using the production DB; test data is cleaned up after each test."""

    # Using autouse fixture to clean up any leftover test data
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        yield
        # After each test, clean up any test bets we created
        from database.db import get_session

        with get_session() as session:
            _clean_bets(session, "test-api-bets")

    def test_bets_endpoint_200(self, client):
        """GET /api/bets -> 200, 'bets' list and 'count' field present."""
        # Insert some test data first
        from database.db import get_session

        with get_session() as session:
            _insert_bets(session, n=3, status="won")

        resp = client.get("/api/bets")
        assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "bets" in data, f"Missing 'bets' in {list(data.keys())}"
        assert "count" in data, f"Missing 'count' in {list(data.keys())}"
        assert isinstance(data["bets"], list)
        # At least our 3 test bets
        assert data["count"] >= 3

        # Verify bet schema
        for b in data["bets"]:
            for key in (
                "id",
                "market_id",
                "city",
                "side",
                "amount",
                "entry_price",
                "current_price",
                "status",
                "realized_pnl",
                "unrealized_pnl",
                "placed_at",
            ):
                assert key in b, f"Missing '{key}' in bet: {list(b.keys())}"

    def test_bets_filter_status(self, client):
        """status=won returns only won bets; status=lost returns only lost."""
        from database.db import get_session

        with get_session() as session:
            _insert_bets(session, n=2, status="won")
            _insert_bets(session, n=3, status="lost")

        # Filter by won
        resp_won = client.get("/api/bets?status=won")
        assert resp_won.status_code == 200
        data_won = resp_won.json()
        for b in data_won["bets"]:
            assert b["status"] == "won", f"Expected won, got {b['status']}"
        assert data_won["count"] >= 2
        assert data_won["count"] <= data_won["total"]

        # Filter by lost
        resp_lost = client.get("/api/bets?status=lost")
        assert resp_lost.status_code == 200
        data_lost = resp_lost.json()
        for b in data_lost["bets"]:
            assert b["status"] == "lost", f"Expected lost, got {b['status']}"
        assert data_lost["count"] >= 3

        # Multi-status: won,lost
        resp_multi = client.get("/api/bets?status=won,lost")
        assert resp_multi.status_code == 200
        data_multi = resp_multi.json()
        assert data_multi["count"] >= 5

    def test_bets_pagination(self, client):
        """limit & offset slice correctly."""
        from database.db import get_session

        with get_session() as session:
            _insert_bets(session, n=10, status="won")

        # Get full list to know order
        resp_all = client.get("/api/bets")
        assert resp_all.status_code == 200
        all_ids = [b["id"] for b in resp_all.json()["bets"]]

        # limit=2, offset=1
        resp_page = client.get("/api/bets?limit=2&offset=1")
        assert resp_page.status_code == 200
        data = resp_page.json()
        assert data["count"] == 2
        assert data["total"] >= 10
        page_ids = [b["id"] for b in data["bets"]]
        # offset=1 means skip the first (most recent)
        assert page_ids == all_ids[1:3], f"{page_ids} != {all_ids[1:3]}"


class TestLazyInitDb:
    """Test that lazy init_db() prevents 'no such table' errors in isolation."""

    def test_fresh_db_no_table_error(self):
        """Empty temp DB gets lazy-inited by get_session → no error on query.

        Patches database.db internals (engine, SessionLocal, _DB_INITIALIZED)
        to point at a fresh temp DB, verifies get_session() triggers lazy
        init_db() automatically, then restores all state without polluting
        sys.modules — avoiding cross-test pollution (e.g. test_cli_portfolio_reset).
        """
        import config.settings as cfg_mod
        import database.db as db_mod

        _fd, _tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(_fd)

        # Save originals
        _orig_db_path = cfg_mod.config.DB_PATH
        _orig_engine = db_mod.engine
        _orig_session_factory = db_mod.SessionLocal
        _orig_db_init = db_mod._DB_INITIALIZED
        _orig_db_path_mod = db_mod.DB_PATH

        # Create fresh temp engine + sessionmaker
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        _fresh_engine = create_engine(f"sqlite:///{_tmp_path}")
        _fresh_session_factory = sessionmaker(bind=_fresh_engine)

        try:
            # Patch database.db to use the fresh engine
            cfg_mod.config.DB_PATH = _tmp_path
            db_mod.DB_PATH = _tmp_path
            db_mod._DB_INITIALIZED = False
            db_mod.engine = _fresh_engine
            db_mod.SessionLocal = _fresh_session_factory

            from database.db import get_session
            from database.models import Bet

            with get_session() as session:
                count = session.query(Bet).count()
                assert count == 0, f"Expected 0 bets, got {count}"
        finally:
            # Restore everything
            cfg_mod.config.DB_PATH = _orig_db_path
            db_mod.DB_PATH = _orig_db_path_mod
            db_mod._DB_INITIALIZED = _orig_db_init
            db_mod.engine = _orig_engine
            db_mod.SessionLocal = _orig_session_factory

            _fresh_engine.dispose()

            try:
                os.unlink(_tmp_path)
            except PermissionError:
                pass
