"""Edge gate test: should_bet must require net_edge >= effective_min_edge."""

import os

os.environ["JUNBO_ENV"] = "test"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import datetime

import pytest

from engine.calculator import Calculator
from database.models import Base, WeatherMarket, WeatherForecast, Portfolio


@pytest.fixture()
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _make_market(session, market_id, yes_price, market_type="HIGH"):
    m = WeatherMarket(
        id=market_id,
        question="Will temp exceed 30?",
        city="Ankara",
        city_code="ankara",
        metric="temperature_max",
        threshold=30.0,
        market_type=market_type,
        target_date=datetime.datetime.now() + datetime.timedelta(days=1),
        yes_price=yes_price,
        no_price=round(1 - yes_price, 4),
        liquidity=50000,
    )
    session.add(m)
    return m


def _make_forecast(session, market_id, value, source, weight=1.0):
    f = WeatherForecast(
        market_id=market_id,
        metric="temperature_max",
        source=source,
        predicted_value=value,
        model_weight=weight,
    )
    session.add(f)


@pytest.mark.parametrize(
    "yes_price, forecast_values, expected_bet",
    [
        (0.10, [35.0, 34.5, 35.5, 33.0, 34.0], True),  # high edge
        (0.50, [20.0, 21.0, 22.0, 21.5, 22.5], False),  # negative edge (fc far below threshold)
        (0.80, [20.0, 21.0, 22.0, 21.5, 22.5], False),  # negative edge (YES-side)
        (0.25, [20.0, 21.0, 22.0, 21.5, 22.5], False),  # negative edge
    ],
)
def test_edge_gate(db_session, yes_price, forecast_values, expected_bet):
    _make_market(db_session, "mkt_edge_gate_1", yes_price)
    db_session.add(Portfolio(id=1, cash_balance=1000.0, total_value=1000.0))
    for i, v in enumerate(forecast_values):
        _make_forecast(db_session, "mkt_edge_gate_1", v, f"src{i}")
    db_session.commit()

    calc = Calculator()
    analysis = calc.analyze_market("mkt_edge_gate_1", session=db_session)
    assert analysis is not None
    assert analysis.should_bet == expected_bet, (
        f"yes={yes_price}, fc={forecast_values} -> should_bet={analysis.should_bet}, "
        f"expected {expected_bet}, edge={analysis.edge:.4f}, raw={analysis.raw_edge:.4f}"
    )


def test_negative_edge_never_bets(db_session):
    """Forecast below market implies negative edge -> must NOT bet."""
    _make_market(db_session, "mkt_neg_edge", 0.05)
    db_session.add(Portfolio(id=1, cash_balance=1000.0, total_value=1000.0))
    for i, v in enumerate([20.0, 21.0, 22.0, 21.5, 22.5]):
        _make_forecast(db_session, "mkt_neg_edge", v, f"src{i}")
    db_session.commit()

    calc = Calculator()
    analysis = calc.analyze_market("mkt_neg_edge", session=db_session)
    assert analysis is not None
    assert analysis.edge < 0.05
    assert analysis.should_bet is False
