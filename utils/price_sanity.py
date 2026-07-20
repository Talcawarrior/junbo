"""Price validation helpers for binary Polymarket markets."""


def is_valid_binary_price(yes_price: float, no_price: float) -> bool:
    """Validate binary market prices.

    Binary invariant: yes_price + no_price ≈ 1.0.
    Valid prices can be extreme (e.g. 0.0005 / 0.9995) — these are real
    Polymarket prices for near-certain outcomes. Only reject 0.0, negative,
    or sums wildly far from 1.0.
    """
    if yes_price is None or no_price is None:
        return False
    try:
        y = float(yes_price)
        n = float(no_price)
    except (TypeError, ValueError):
        return False
    if y < 0 or n < 0:
        return False
    if y > 1.0 or n > 1.0:
        return False
    s = y + n
    # Binary invariant: sum must be close to 1.0
    if abs(s - 1.0) > 0.05:
        return False
    return True


def safe_ev(model_prob: float, price: float, min_price: float = 0.01) -> float:
    """Calculate EV per $1 risked with price guard."""
    if price is None or model_prob is None:
        return 0.0
    try:
        p = float(price)
        prob = float(model_prob)
    except (TypeError, ValueError):
        return 0.0
    if p <= min_price:
        return 0.0
    return prob * (1.0 / p - 1.0) - (1.0 - prob)
