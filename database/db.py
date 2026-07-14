"""Database setup with WAL mode and custom transaction sessions."""

import logging
import os
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from config.settings import config
from database.models import Base

logger = logging.getLogger("DATABASE")
DB_PATH = config.DB_PATH

_DB_INITIALIZED = False


def get_engine():
    """Create database engine with optimized SQLite settings."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    eng = create_engine(
        f"sqlite:///{DB_PATH}",
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
        echo=config.DB_ECHO,
    )

    @event.listens_for(eng, "connect")
    def set_sqlite_pragma(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA cache_size=10000")
        cursor.close()

    return eng


engine = get_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db():
    """Initialize database tables and apply migrations. Idempotent."""
    global _DB_INITIALIZED
    if _DB_INITIALIZED:
        return
    Base.metadata.create_all(bind=engine)

    # Migration: add threshold_low / threshold_high columns (nullable, safe for SQLite)
    _migrate_add_column("weather_markets", "threshold_low", "FLOAT")
    _migrate_add_column("weather_markets", "threshold_high", "FLOAT")
    # Migration: add model_predictions column to analyses
    _migrate_add_column("analyses", "model_predictions", "TEXT")
    # Migration: add close_reason / closed_at columns to bets (pre-existing model fields)
    _migrate_add_column("bets", "close_reason", "VARCHAR")
    _migrate_add_column("bets", "closed_at", "DATETIME")
    # Migration: add raw_edge / slippage_pct to analyses (cost model v2)
    _migrate_add_column("analyses", "raw_edge", "FLOAT")
    _migrate_add_column("analyses", "slippage_pct", "FLOAT")
    # Migration: add city / bias columns to historical_calibrations
    _migrate_add_column("historical_calibrations", "city", "VARCHAR")
    _migrate_add_column("historical_calibrations", "bias", "FLOAT")
    # Migration: add entry_fee to bets (Polymarket taker fee at entry time)
    _migrate_add_column("bets", "entry_fee", "FLOAT")

    _DB_INITIALIZED = True
    logger.info("Database initialized at %s with WAL mode", DB_PATH)


def _ensure_db_init():
    """Lazy-init: call init_db() if it hasn't been called yet."""
    if not _DB_INITIALIZED:
        init_db()


def _migrate_add_column(table: str, column: str, col_type: str) -> None:
    """Idempotent ALTER TABLE ADD COLUMN for SQLite."""
    with engine.connect() as conn:
        row = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        existing = [r[1] for r in row]  # column name is at index 1
        if column not in existing:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
            conn.commit()
            logger.info("Migration: added column %s.%s (%s)", table, column, col_type)


@contextmanager
def get_session():
    """Her işlem kendi session'ını alır, hata olursa rollback yapar."""
    _ensure_db_init()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def get_session_or(existing=None):
    """Yield *existing* session if provided, otherwise create a fresh one.

    This allows callers to share a single session across a batch of
    operations (e.g. one bot cycle) while keeping backward compatibility
    for callers that don't pass a session.
    """
    if existing is not None:
        yield existing
    else:
        with get_session() as session:
            yield session


def get_db_session():
    """Fallback compatibility method for legacy code."""
    _ensure_db_init()
    return SessionLocal()


def get_db_session_factory():
    """Fallback compatibility method returning the raw sessionmaker factory."""
    _ensure_db_init()
    return SessionLocal


def ensure_initial_portfolio():
    """Create Portfolio(id=1) with INITIAL_PORTFOLIO values if it does not exist.

    Called by both the FastAPI lifespan (server mode) and run_cli() (CLI mode)
    so that Portfolio(id=1) is guaranteed to exist before any bet is placed.
    Idempotent - safe to call multiple times.
    """
    from config.settings import config
    from database.models import Portfolio

    with get_session() as session:
        portfolio = session.query(Portfolio).filter(Portfolio.id == 1).first()
        if not portfolio:
            portfolio = Portfolio(
                id=1,
                initial_value=config.INITIAL_PORTFOLIO,
                current_value=config.INITIAL_PORTFOLIO,
                cash_balance=config.INITIAL_PORTFOLIO,
                total_value=config.INITIAL_PORTFOLIO,
                total_realized_pnl=0.0,
                total_won=0,
                total_lost=0,
                daily_pnl=0.0,
            )
            session.add(portfolio)
            session.commit()
            logger.info("ensure_initial_portfolio: Portfolio(id=1) created")
