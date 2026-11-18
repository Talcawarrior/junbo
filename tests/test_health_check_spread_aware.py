"""Health-check verdict must separate rotation closes from settlement results.

The old logic counted every rotation close as a loss and looked only at the
loss count, so a profitable spread book could still be reported as
"critical". These tests lock in the net-PnL based verdict and the new
rotation/settlement breakdown.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from api import app
from database.db import get_session
from database.models import Bet


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def _delete_all_bets():
    with get_session() as session:
        session.query(Bet).delete()
        session.commit()


@pytest.fixture(autouse=True)
def _clean_bets():
    _delete_all_bets()
    yield
    _delete_all_bets()


def _seed(rotation: int, wins: int, losses: int) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with get_session() as session:
        for i in range(rotation):
            session.add(
                Bet(
                    market_id=f"test-health-rot-{i}",
                    status="closed",
                    close_reason="rotation",
                    realized_pnl=-1.0,
                    pnl=-1.0,
                    amount=2.0,
                    placed_at=now,
                    closed_at=now,
                )
            )
        for i in range(wins):
            session.add(
                Bet(
                    market_id=f"test-health-win-{i}",
                    status="won",
                    realized_pnl=3.0,
                    pnl=3.0,
                    amount=2.0,
                    placed_at=now,
                    settled_at=now,
                )
            )
        for i in range(losses):
            session.add(
                Bet(
                    market_id=f"test-health-loss-{i}",
                    status="lost",
                    realized_pnl=-1.0,
                    pnl=-1.0,
                    amount=2.0,
                    placed_at=now,
                    settled_at=now,
                )
            )
        session.commit()


def test_profitable_rotation_book_is_not_critical(client):
    """Many small rotation losses + positive net PnL => not critical."""
    _seed(rotation=10, wins=8, losses=2)

    data = client.get("/api/health-check").json()

    assert data["recent_48h"]["total"] == 20
    assert data["recent_48h"]["rotation_count"] == 10
    assert data["recent_48h"]["settlement_count"] == 10
    assert data["recent_48h"]["net_pnl"] == pytest.approx(12.0)
    assert data["recent_48h"]["rotation_pnl"] == pytest.approx(-10.0)
    assert data["recent_48h"]["settlement_pnl"] == pytest.approx(22.0)
    assert data["summary_all"]["rotation"]["count"] == 10
    assert data["summary_all"]["rotation"]["pnl"] == pytest.approx(-10.0)
    assert data["summary_all"]["settlement"]["count"] == 10
    assert data["summary_all"]["settlement"]["wins"] == 8
    assert data["summary_all"]["settlement"]["losses"] == 2
    assert data["summary_all"]["settlement"]["win_rate_pct"] == 80.0
    assert data["verdict"] != "critical"


def test_negative_net_pnl_is_critical(client):
    """Same loss count, negative net PnL => critical."""
    _seed(rotation=10, wins=2, losses=8)

    data = client.get("/api/health-check").json()

    assert data["recent_48h"]["total"] == 20
    assert data["recent_48h"]["net_pnl"] == pytest.approx(-12.0)
    assert data["verdict"] == "critical"


def test_daily_timeline_includes_rotation_closes(client):
    """Daily PnL must include rotation closes, matching the equity curve."""
    _seed(rotation=1, wins=1, losses=0)

    data = client.get("/api/health-check").json()

    now = datetime.now(timezone.utc)
    today = f"{now.day}/{now.month}"
    day = next((d for d in data["daily_pnl_timeline"] if d["date"] == today), None)
    assert day is not None
    assert day["total"] == 2
    assert day["pnl"] == pytest.approx(2.0)


def test_edge_metrics_are_marked_unavailable_without_analyses(client):
    """Spread mode has no Analysis rows; the API must say so explicitly."""
    _seed(rotation=1, wins=1, losses=0)

    data = client.get("/api/health-check").json()

    assert data["strategy_mode"] in ("spread", "edge", "metar")
    assert data["edge_metrics_available"] is False
    assert data["edge_distribution"]["available"] is False


def test_strategy_mode_reports_metar_when_spread_disabled(client):
    """SPREAD_ENABLED=false iken etkin mod 'metar' olarak raporlanir."""
    from config.settings import bot_config

    old_enabled = bot_config.strategy.spread_enabled
    old_strategy = bot_config.strategy.betting_strategy
    bot_config.strategy.spread_enabled = False
    bot_config.strategy.betting_strategy = "spread"
    try:
        _seed(rotation=1, wins=1, losses=0)
        data = client.get("/api/health-check").json()
        assert data["strategy_mode"] == "metar"
    finally:
        bot_config.strategy.spread_enabled = old_enabled
        bot_config.strategy.betting_strategy = old_strategy
