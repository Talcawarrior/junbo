"""Price validation helpers for binary Polymarket markets."""


def is_valid_binary_price(yes_price: float, no_price: float) -> bool:
    """
    Validate binary market prices.

    Rules:
    - yes_price must be numeric and 0.01 <= yes_price <= 0.99
    - no_price must be numeric and 0.01 <= no_price <= 0.99
    - 0.50 <= yes_price + no_price <= 1.50 (allowing for spread/vig)
    """
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


def clamp_price(
    price: float, min_price: float = 0.01, max_price: float = 0.99
) -> float:
    """Clamp price to valid range."""
    if price is None:
        return min_price
    try:
        p = float(price)
    except (TypeError, ValueError):
        return min_price
    return max(min_price, min(max_price, p))


def safe_ev(model_prob: float, price: float, min_price: float = 0.01) -> float:
    """
    Calculate EV per $1 risked with price guard.

    Returns 0.0 if price <= min_price to avoid absurd EV values.
    """
    if price is None or model_prob is None:
        return 0.0
    try:
        p = float(price)
        prob = float(model_prob)
    except (TypeError, ValueError):
        return 0.0
    if p <= min_price:
        return 0.0
    # EV = prob * (1/p - 1) - (1 - prob)
    return prob * (1.0 / p - 1.0) - (1.0 - prob)


def validate_market_prices(market) -> tuple[bool, str]:
    """
    Validate a WeatherMarket object prices.

    Returns (is_valid, reason_if_invalid).
    """
    if market.yes_price is None:
        return False, "yes_price is None"
    if market.no_price is None:
        return False, "no_price is None"
    if not is_valid_binary_price(market.yes_price, market.no_price):
        return False, (
            f"invalid prices: yes={market.yes_price}, no={market.no_price}, sum={market.yes_price + market.no_price}"
        )
    return True, ""
