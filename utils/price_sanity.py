"""Price validation helpers for binary Polymarket markets."""


def is_valid_binary_price(yes_price: float, no_price: float) -> bool:
    """Validate binary market prices."""
    if yes_price is None or no_price is None:
        return False
    try:
        y = float(yes_price)
        n = float(no_price)
    except (TypeError, ValueError):
        return False
    if not (0.01 <= y <= 0.99):
        return False
    if not (0.01 <= n <= 0.99):
        return False
    s = y + n
    if not (0.50 <= s <= 1.50):
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
