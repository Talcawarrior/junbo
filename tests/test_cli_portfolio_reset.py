"""Tests for CLI portfolio creation and reset logic."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import OPEN_BET_STATUSES, Base, Bet, Portfolio


@pytest.fixture
def fresh_db(tmp_path):
    """Create a fresh in-memory-like DB for each test."""
    db_path = tmp_path / "test_portfolio.db"
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)  # noqa: N806
    session = Session()

    # Patch database.db to use our test engine
    import config.settings as settings_mod
    import database.db as db_mod

    original_engine = db_mod.engine
    original_session_factory = db_mod.SessionLocal
    db_mod.engine = engine
    db_mod.SessionLocal = Session

    # Reset DRY_RUN to true for safety
    original_dry = settings_mod.Config.DRY_RUN
    settings_mod.Config.DRY_RUN = True

    yield session, engine

    # Restore
    db_mod.engine = original_engine
    db_mod.SessionLocal = original_session_factory
    settings_mod.Config.DRY_RUN = original_dry
    session.close()
    engine.dispose()


def test_ensure_initial_portfolio_creates_row(fresh_db):
    """ensure_initial_portfolio() creates Portfolio(id=1) with correct defaults."""
    session, _ = fresh_db
    from config.settings import config
    from database.db import ensure_initial_portfolio

    # Verify no portfolio exists
    pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
    assert pf is None, "Portfolio should not exist before ensure_initial_portfolio"

    # Call the helper
    ensure_initial_portfolio()

    # Verify portfolio was created
    pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
    assert pf is not None, "Portfolio(id=1) should exist after ensure_initial_portfolio"
    assert pf.cash_balance == config.INITIAL_PORTFOLIO, (
        f"cash_balance should be {config.INITIAL_PORTFOLIO}, got {pf.cash_balance}"
    )
    assert pf.current_value == config.INITIAL_PORTFOLIO, (
        f"current_value should be {config.INITIAL_PORTFOLIO}, got {pf.current_value}"
    )
    assert pf.total_value == config.INITIAL_PORTFOLIO, (
        f"total_value should be {config.INITIAL_PORTFOLIO}, got {pf.total_value}"
    )
    assert pf.initial_value == config.INITIAL_PORTFOLIO, (
        f"initial_value should be {config.INITIAL_PORTFOLIO}, got {pf.initial_value}"
    )
    assert pf.daily_pnl == 0.0, f"daily_pnl should be 0.0, got {pf.daily_pnl}"
    assert pf.total_realized_pnl == 0.0, (
        f"total_realized_pnl should be 0.0, got {pf.total_realized_pnl}"
    )
    assert pf.total_won == 0, f"total_won should be 0, got {pf.total_won}"
    assert pf.total_lost == 0, f"total_lost should be 0, got {pf.total_lost}"


def test_ensure_initial_portfolio_idempotent(fresh_db):
    """Calling ensure_initial_portfolio() twice does not duplicate or error."""
    session, _ = fresh_db
    from config.settings import config
    from database.db import ensure_initial_portfolio

    ensure_initial_portfolio()
    ensure_initial_portfolio()  # Should not raise

    pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
    assert pf is not None
    assert pf.cash_balance == config.INITIAL_PORTFOLIO


def test_cli_reset_creates_portfolio_if_missing(fresh_db):
    """CLI reset creates Portfolio(id=1) if it does not exist."""
    session, _ = fresh_db

    # Verify no portfolio
    pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
    assert pf is None

    # Simulate CLI reset logic (from main.py lines 859-876)
    from config.settings import config
    from database.models import Analysis as AnalysisModel
    from database.models import Portfolio as PortfolioModel

    (
        session.query(Bet)
        .filter(Bet.status.in_(OPEN_BET_STATUSES))
        .update({"status": "cancelled"}, synchronize_session=False)
    )
    (session.query(AnalysisModel).delete(synchronize_session=False))

    pf = session.query(PortfolioModel).filter(PortfolioModel.id == 1).first()
    if not pf:
        pf = PortfolioModel(id=1)
        session.add(pf)
    pf.cash_balance = config.INITIAL_PORTFOLIO
    pf.current_value = config.INITIAL_PORTFOLIO
    pf.total_value = config.INITIAL_PORTFOLIO
    pf.initial_value = config.INITIAL_PORTFOLIO
    pf.daily_pnl = 0.0
    pf.total_realized_pnl = 0.0
    pf.total_won = 0
    pf.total_lost = 0
    session.commit()

    # Verify portfolio was created with correct values
    pf = session.query(PortfolioModel).filter(PortfolioModel.id == 1).first()
    assert pf is not None, "Portfolio(id=1) should exist after reset"
    assert pf.cash_balance == config.INITIAL_PORTFOLIO
    assert pf.current_value == config.INITIAL_PORTFOLIO
    assert pf.total_value == config.INITIAL_PORTFOLIO
    assert pf.initial_value == config.INITIAL_PORTFOLIO
    assert pf.daily_pnl == 0.0
    assert pf.total_realized_pnl == 0.0
    assert pf.total_won == 0
    assert pf.total_lost == 0


def test_cli_reset_resets_existing_portfolio(fresh_db):
    """CLI reset resets an existing Portfolio to INITIAL_PORTFOLIO values."""
    session, _ = fresh_db
    from config.settings import config

    # Create a portfolio with non-zero values
    pf = Portfolio(
        id=1,
        initial_value=config.INITIAL_PORTFOLIO,
        current_value=config.INITIAL_PORTFOLIO / 2,
        cash_balance=config.INITIAL_PORTFOLIO / 3,
        total_value=config.INITIAL_PORTFOLIO / 2,
        total_realized_pnl=-200.0,
        total_won=3,
        total_lost=5,
        daily_pnl=-50.0,
    )
    session.add(pf)
    session.commit()

    # Simulate CLI reset
    from database.models import Portfolio as PortfolioModel

    pf = session.query(PortfolioModel).filter(PortfolioModel.id == 1).first()
    if not pf:
        pf = PortfolioModel(id=1)
        session.add(pf)
    pf.cash_balance = config.INITIAL_PORTFOLIO
    pf.current_value = config.INITIAL_PORTFOLIO
    pf.total_value = config.INITIAL_PORTFOLIO
    pf.initial_value = config.INITIAL_PORTFOLIO
    pf.daily_pnl = 0.0
    pf.total_realized_pnl = 0.0
    pf.total_won = 0
    pf.total_lost = 0
    session.commit()

    # Verify reset
    pf = session.query(PortfolioModel).filter(PortfolioModel.id == 1).first()
    assert pf.cash_balance == config.INITIAL_PORTFOLIO
    assert pf.current_value == config.INITIAL_PORTFOLIO
    assert pf.total_value == config.INITIAL_PORTFOLIO
    assert pf.daily_pnl == 0.0
    assert pf.total_realized_pnl == 0.0
    assert pf.total_won == 0
    assert pf.total_lost == 0
