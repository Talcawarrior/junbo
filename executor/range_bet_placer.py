"""Range betting: YES-only, fixed $10, temperature range (T-2..T+2)."""

import logging
from datetime import datetime, timezone, timedelta

from config.settings import bot_config
from database.db import get_session
from database.models import OPEN_BET_STATUSES, Bet, Portfolio, WeatherMarket, WeatherForecast

logger = logging.getLogger("RANGE_BET")

_TARGET_DAY_OFFSET = 2


def _get_forecast_temp(city: str, metric: str, target_date: datetime) -> float | None:
    """Get latest temperature forecast for a city/metric/date."""
    with get_session() as s:
        forecasts = (
            s.query(WeatherForecast)
            .filter(
                WeatherForecast.city.ilike(city),
                WeatherForecast.metric == metric,
                WeatherForecast.target_date == target_date,
                WeatherForecast.source.isnot(None),
            )
            .order_by(WeatherForecast.fetched_at.desc())
            .all()
        )
        if not forecasts:
            return None
        latest_by_source = {}
        for f in forecasts:
            if f.source not in latest_by_source:
                latest_by_source[f.source] = f.predicted_value
        values = list(latest_by_source.values())
        if not values:
            return None
        return sum(values) / len(values)


def _find_market(city: str, threshold: int, metric: str, target_date: datetime) -> WeatherMarket | None:
    """Find market for a city/threshold/metric/date combination."""
    with get_session() as s:
        return (
            s.query(WeatherMarket)
            .filter(
                WeatherMarket.city.ilike(city),
                WeatherMarket.metric == metric,
                WeatherMarket.threshold == float(threshold),
                WeatherMarket.target_date == target_date,
                WeatherMarket.status == "open",
            )
            .first()
        )


def _existing_bet(market_id: str) -> bool:
    """Check if a bet already exists for this market."""
    with get_session() as s:
        return s.query(Bet).filter(Bet.market_id == market_id, Bet.status != "rejected").first() is not None


def _city_open_count(city: str) -> int:
    """Count open bets for a city."""
    with get_session() as s:
        return (
            s.query(Bet)
            .join(WeatherMarket, Bet.market_id == WeatherMarket.id)
            .filter(
                Bet.status.in_(OPEN_BET_STATUSES),
                WeatherMarket.city.ilike(city),
            )
            .count()
        )


def place_range_bets() -> list[str]:
    """Place YES-only range bets for configured cities."""
    cities = bot_config.strategy.range_bet_cities
    if not cities:
        logger.info("Range betting: no cities configured")
        return []
    if not bot_config.strategy.range_bet_enabled:
        logger.info("Range betting: disabled")
        return []

    bet_amount = bot_config.strategy.range_bet_amount
    spread = bot_config.strategy.range_bet_spread
    target_date = (datetime.now(timezone.utc) + timedelta(days=_TARGET_DAY_OFFSET)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    results = []
    for city in cities:
        temp = _get_forecast_temp(city, "temperature_max", target_date)
        if temp is None:
            logger.info("Range: no forecast for %s", city)
            continue

        base_temp = round(temp)

        for offset in range(-spread, spread + 1):
            threshold = base_temp + offset
            market = _find_market(city, threshold, "temperature_max", target_date)
            if not market:
                logger.info("Range: no market for %s %dC", city, threshold)
                continue
            if _existing_bet(str(market.id)):
                continue

            yes_price = float(market.yes_price or 0.5)
            entry_fee = round(bet_amount * bot_config.strategy.current_fee_rate * (1 - yes_price), 4)
            shares = round(bet_amount / yes_price, 4) if yes_price > 0 else 0

            now_ts = int(datetime.now(timezone.utc).timestamp())
            bet = Bet(
                market_id=str(market.id),
                city=market.city,
                city_code=market.city_code or "",
                side="YES",
                amount=bet_amount,
                stake_amount=bet_amount,
                price=yes_price,
                entry_price=yes_price,
                shares=shares,
                current_price=yes_price,
                status="placed",
                order_id=f"range_{market.id}_{now_ts}",
                placed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                entry_fee=entry_fee,
                ladder_data="[]",
                potential_payout=bet_amount / yes_price if yes_price > 0 else 0,
            )

            with get_session() as s:
                s.add(bet)
                m = s.query(WeatherMarket).filter(WeatherMarket.id == market.id).first()
                if m:
                    m.status = "bet_placed"
                pf = s.query(Portfolio).filter(Portfolio.id == 1).first()
                if pf:
                    open_exposure = s.query(Bet.amount).filter(Bet.status.in_(OPEN_BET_STATUSES)).all()
                    total_open = sum(float(a[0] or 0) for a in open_exposure)
                    pf.total_value = (pf.cash_balance or 0) + total_open
                    pf.last_updated = datetime.now(timezone.utc).replace(tzinfo=None)
                s.commit()

            msg = f"{city} {threshold}C YES ${bet_amount:.0f}"
            logger.info("Range bet: %s", msg)
            results.append(msg)

    if results:
        logger.info("Range betting complete: %d bets placed", len(results))
    else:
        logger.info("Range betting: no bets placed")
    return results
