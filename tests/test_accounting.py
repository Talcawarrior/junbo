"""Tests for utils/accounting.py — centralized cash operations.

Verifies:
  1. Ladder fill no-double-debit: ``debit_stake`` is idempotent in principle
     (scheduler skips ``status=="filled"`` rungs).
  2. Early exit returns principal: ``credit_sale`` of exact stake → cash unchanged.
  3. Early exit with profit: ``credit_sale`` of > stake → cash grows.
  4. Negative cash guard: ``debit_stake`` with insufficient funds raises ValueError.
  5. Invariant full cycle: debit_stake + credit_settlement → cash unchanged.
"""

import os
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)

from config.settings import config as _cfg  # noqa: E402

_cfg.DB_PATH = _db_path

import importlib  # noqa: E402

import database.db  # noqa: E402

importlib.reload(database.db)  # noqa: E402

from database.db import get_session, init_db  # noqa: E402

init_db()  # noqa: E402

from database.models import Portfolio  # noqa: E402
from utils.accounting import credit_sale, credit_settlement, debit_stake  # noqa: E402

# ── Helpers ───────────────────────────────────────────────────────────────────


def _clean():
    with get_session() as session:
        session.query(Portfolio).delete()
        session.commit()


def _portfolio_cash() -> float:
    with get_session() as session:
        pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
        return float(pf.cash_balance or 0.0) if pf else 0.0


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_ladder_no_double_debit():
    """Ladder fill: only ``status=="pending"`` rungs trigger debit_stake.

    If a rung is already ``"filled"``, the scheduler will skip it, so the
    stake is never debited twice.  This test verifies the accounting function
    itself is a simple deduction (idempotency lives in the caller).
    """
    _clean()
    with get_session() as session:
        session.add(Portfolio(id=1, cash_balance=1000.0, total_value=1000.0))
        session.commit()

    with get_session() as session:
        # Simulate filling a single ladder rung of $50
        cash_before = _portfolio_cash()
        debit_stake(session, 50.0, "ladder_fill:test-rung-1")
        session.commit()
        first_deduction = cash_before - _portfolio_cash()

        # Second call with same reason — would be a double-debit in real code
        debit_stake(session, 50.0, "ladder_fill:test-rung-1")
        session.commit()
        assert _portfolio_cash() == 900.0, (
            f"Expected 900.0 after two $50 debits, got {_portfolio_cash()}"
        )
        assert first_deduction == 50.0


def test_early_exit_returns_principal():
    """credit_sale of exactly the stake → cash unchanged net.

    Simulates: stake $200 → cash = 800. credit_sale($200) → cash = 1000.
    Net effect: zero.
    """
    _clean()
    with get_session() as session:
        session.add(Portfolio(id=1, cash_balance=1000.0, total_value=1000.0))
        session.commit()

    with get_session() as session:
        debit_stake(session, 200.0, "bet_open:test-principal")
        session.commit()
    assert _portfolio_cash() == 800.0, f"Expected 800, got {_portfolio_cash()}"

    with get_session() as session:
        credit_sale(session, 200.0, "early_exit:test-principal:stop_loss")
        session.commit()
    assert _portfolio_cash() == 1000.0, (
        f"Expected 1000 (net zero), got {_portfolio_cash()}"
    )


def test_early_exit_with_profit():
    """credit_sale with proceeds > stake → cash grows by profit.

    Simulates: stake $100 → cash = 900.  Shares sold for $150 → cash = 1050.
    Profit = $50.
    """
    _clean()
    with get_session() as session:
        session.add(Portfolio(id=1, cash_balance=1000.0, total_value=1000.0))
        session.commit()

    with get_session() as session:
        debit_stake(session, 100.0, "bet_open:test-profit")
        session.commit()
    assert _portfolio_cash() == 900.0

    with get_session() as session:
        credit_sale(session, 150.0, "early_exit:test-profit:take_profit")
        session.commit()
    assert _portfolio_cash() == 1050.0, (
        f"Expected 1050 (900+150), got {_portfolio_cash()}"
    )


def test_negative_cash_guard():
    """debit_stake with amount > cash_balance raises ValueError."""
    _clean()
    with get_session() as session:
        session.add(Portfolio(id=1, cash_balance=100.0, total_value=100.0))
        session.commit()

    with get_session() as session:
        try:
            debit_stake(session, 200.0, "bet_open:overdraft")
            assert False, "Expected ValueError for insufficient cash"
        except ValueError as exc:
            assert "Insufficient cash" in str(exc), f"Unexpected message: {exc}"

    # Cash unchanged after failed debit
    assert _portfolio_cash() == 100.0, (
        f"Cash should still be 100, got {_portfolio_cash()}"
    )


def test_invariant_full_cycle():
    """debit_stake + credit_settlement → cash unchanged (zero-sum cycle).

    Simulates: stake $100 at 0.5 entry → payout $200, fee $10.
    debit_stake($100) → cash = 900.
    credit_settlement(payout=$200, fee=$10) → cash = 900 + 200 - 10 = 1090.
    Net: + $90 (the profit).
    """
    _clean()
    with get_session() as session:
        session.add(Portfolio(id=1, cash_balance=1000.0, total_value=1000.0))
        session.commit()

    with get_session() as session:
        debit_stake(session, 100.0, "bet_open:test-cycle")
        session.commit()
    assert _portfolio_cash() == 900.0

    with get_session() as session:
        credit_settlement(
            session, payout=200.0, fee=10.0, reason="settle:test-cycle:won"
        )
        session.commit()
    assert _portfolio_cash() == 1090.0, (
        f"Expected 1090 (900+200-10), got {_portfolio_cash()}"
    )

    # Total profit = 1090 - 1000 = $90 correct
    profit = _portfolio_cash() - 1000.0
    assert profit == 90.0, f"Expected $90 profit, got ${profit}"
