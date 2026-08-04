"""Central accounting API for all portfolio cash operations.

Every debit/credit to ``Portfolio.cash_balance`` must go through one of
the three functions in this module.  This guarantees auditability (every
mutation is logged with before/after values) and prevents double-spend
bugs.

Usage::

    from utils.accounting import debit_stake, credit_sale, credit_settlement
"""

import logging
from decimal import ROUND_HALF_UP, Decimal

from database.models import Portfolio

logger = logging.getLogger("ACCOUNTING")

# ── Helpers ─────────────────────────────────────────────────────────────────


def _get_portfolio(session) -> Portfolio:
    pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
    if pf is None:
        raise RuntimeError("Portfolio(id=1) not found — run init first")
    return pf


def _log(cash_before: float, cash_after: float, reason: str):
    logger.info(
        "ACCOUNTING: %s cash %.2f -> %.2f",
        reason,
        cash_before,
        cash_after,
    )


def _to_float(val) -> float:
    """Round to 2 decimals via Decimal to avoid floating pennies."""
    return float(Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ── Public API ──────────────────────────────────────────────────────────────


def debit_stake(session, amount: float, reason: str) -> float:
    """Deduct *amount* from portfolio cash for a new bet/ladder rung.

    Returns the new cash balance.
    Raises ``ValueError`` if the resulting balance would go negative.
    """
    pf = _get_portfolio(session)
    cash_before = _to_float(pf.cash_balance or 0.0)
    amount_r = _to_float(amount)
    cash_after = cash_before - amount_r
    if cash_after < 0:
        raise ValueError(
            f"Insufficient cash: have {cash_before:.2f}, need {amount_r:.2f} (reason={reason})"
        )
    cash_after = _to_float(cash_after)
    pf.cash_balance = cash_after
    _log(cash_before, cash_after, reason)
    return cash_after


def credit_sale(session, proceeds: float, reason: str) -> float:
    """Add *proceeds* (principal + PnL) from an early-exit sale.

    Returns the new cash balance.
    """
    pf = _get_portfolio(session)
    cash_before = _to_float(pf.cash_balance or 0.0)
    proceeds_r = _to_float(proceeds)
    cash_after = _to_float(cash_before + proceeds_r)
    pf.cash_balance = cash_after
    _log(cash_before, cash_after, reason)
    return cash_after


def credit_settlement(session, payout: float, fee: float, reason: str) -> float:
    """Add *payout - fee* from a settled winning bet.

    Returns the new cash balance.
    """
    pf = _get_portfolio(session)
    cash_before = _to_float(pf.cash_balance or 0.0)
    net = _to_float(payout - fee)
    cash_after = _to_float(cash_before + net)
    pf.cash_balance = cash_after
    _log(cash_before, cash_after, reason)
    return cash_after
