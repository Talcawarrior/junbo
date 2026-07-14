"""Kelly criterion math, shared by engine.calculator and engine.strategy.

Two callers existed with the same formula but different parameter names
and slightly different edges (calculator returned a raw fraction, strategy
returned a dollar amount with min/max clamps). That was a code-review
finding (#5) and a maintenance trap: if you ever wanted to add a "min
odds for Kelly" or a different fraction formula, you had to remember to
fix it in two places.

The math (William Poundstone / Kelly 1956):
    f* = (b p - q) / b
    where b = net odds (decimal-odds minus 1), p = model probability of
    winning, q = 1 - p.

For a Polymarket binary market at price `m` (between 0 and 1, where 1 - m
is the implied win payout), the decimal odds on the YES side are
1 / m, so the net odds are b = (1 / m) - 1 = (1 - m) / m. We expose
both shapes via two pure functions:

    kelly_fraction(prob, price)         -> float in [0, 1]
        The pure f* fraction (no bankroll scaling, no min/max).

    kelly_bet_amount(portfolio, prob,
                     price, *,
                     fraction=0.15,
                     min_bet=1.0,
                     max_bet_pct=0.03) -> float dollars
        The strategy helper: portfolio_value * kelly_fraction,
        floored at min_bet, capped at max_bet_pct * portfolio_value.

Both functions are pure (no DB, no logging) and are safe to call from
either sync (calculator.analyze_market) or async (risk manager) paths.
"""

from __future__ import annotations


def kelly_fraction(prob: float, price: float) -> float:
    """Return the pure Kelly fraction f* for a binary bet.

    Parameters
    ----------
    prob : float
        Model probability of the bet winning (0, 1).
    price : float
        Current market price of the bet (0, 1). For YES this is the YES
        price; for NO use the NO price (1 - yes_price). The decimal odds
        are taken as 1 / price.

    Returns
    -------
    float
        The f* fraction of bankroll recommended. Returns 0.0 for any
        nonsensical input (negative or out-of-range prob/price) or for
        bets where Kelly says "do not bet" (f* <= 0).
    """
    if prob <= 0 or prob >= 1:
        return 0.0
    if price <= 0 or price >= 1:
        return 0.0

    # Net decimal odds on a $1 stake.
    b = (1.0 / price) - 1.0
    if b <= 0:
        return 0.0

    q = 1.0 - prob
    f_star = (b * prob - q) / b
    if f_star <= 0:
        return 0.0
    return f_star


def kelly_bet_amount(
    portfolio_value: float,
    prob: float,
    price: float,
    *,
    fraction: float = 0.15,
    min_bet: float = 1.0,
    max_bet_pct: float = 0.03,
) -> float:
    """Compute a Kelly-sized dollar bet for the given portfolio.

    Wraps :func:`kelly_fraction` with the safety bounds used by the
    engine (fractional Kelly + min bet + max bet cap). Returns 0.0 when
    Kelly says "no bet" or the input is invalid.

    Parameters
    ----------
    portfolio_value : float
        Current total portfolio value (cash + unrealized PnL) in dollars.
    prob, price, fraction, min_bet, max_bet_pct
        See :func:`kelly_fraction` plus the per-bet floor and cap.
    """
    if portfolio_value <= 0:
        return 0.0

    f_star = kelly_fraction(prob, price)
    if f_star <= 0:
        return 0.0

    fractional = f_star * fraction
    if fractional <= 0:
        return 0.0

    amount = portfolio_value * fractional
    amount = max(amount, min_bet)

    max_amount = portfolio_value * max_bet_pct
    amount = min(amount, max_amount)
    return round(amount, 2)
