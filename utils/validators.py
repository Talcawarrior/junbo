"""Data validation utilities."""


def validate_market(market: dict) -> list[str]:
    """Validate Polymarket raw data fields."""
    errors = []
    if not market.get("market_id"):
        errors.append("Market ID is missing")
    if not market.get("question"):
        errors.append("Question is missing")
    return errors


def validate_forecast(value: float, metric: str) -> bool:
    """Validate forecast temperature ranges."""
    ranges = {
        "temperature_max": (-60, 60),
        "temperature_min": (-80, 50),
    }
    if metric in ranges:
        low, high = ranges[metric]
        return low <= value <= high
    return True
