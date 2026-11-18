import sys

sys.path.insert(0, ".")
from datetime import datetime, date
from database.db import get_session
from database.models import HistoricalCalibration, WeatherForecast, WeatherMarket, Portfolio
from executor.spread_placer import _last_forecast_per_city_metric, _city_accuracy, _find_market


def debug_spread_placer():
    """Debug spread placer for test case"""
    target_day = date(2026, 10, 4)

    with get_session() as session:
        print("=== Test Data Check ===")

        # Check markets
        markets = (
            session.query(WeatherMarket)
            .filter(
                WeatherMarket.city == "Testville",
                WeatherMarket.target_date >= datetime(target_day.year, target_day.month, target_day.day, 0, 0, 0),
                WeatherMarket.target_date <= datetime(target_day.year, target_day.month, target_day.day, 23, 59, 59),
            )
            .all()
        )
        print(f"Markets found: {len(markets)}")
        for m in markets:
            print(f"  {m.id}: {m.city} {m.threshold}C {m.status} price={m.yes_price}")

        # Check forecasts
        forecasts = (
            session.query(WeatherForecast)
            .filter(
                WeatherForecast.city == "AAA",
                WeatherForecast.target_date >= datetime(target_day.year, target_day.month, target_day.day, 0, 0, 0),
                WeatherForecast.target_date <= datetime(target_day.year, target_day.month, target_day.day, 23, 59, 59),
            )
            .all()
        )
        print(f"Forecasts found: {len(forecasts)}")
        for f in forecasts:
            print(f"  {f.source}: {f.predicted_value}")

        # Check calibration
        calibrations = session.query(HistoricalCalibration).filter(HistoricalCalibration.city_code == "AAA").all()
        print(f"Calibrations found: {len(calibrations)}")
        for c in calibrations:
            print(f"  {c.model}: {c.bias}")

        # Check portfolio
        portfolio = session.query(Portfolio).filter(Portfolio.id == 1).first()
        print(f"Portfolio: {portfolio.cash_balance if portfolio else 'None'}")

        print("\n=== Spread Placer Logic ===")

        # Check city accuracy
        acc = _city_accuracy(session)
        print(f"City accuracy: {acc}")

        # Check forecasts processing
        forecasts_proc = _last_forecast_per_city_metric(session, target_day)
        print(f"Processed forecasts: {forecasts_proc}")

        # Check market finding
        for thr in range(28, 35):
            mkt = _find_market(session, "Testville", "temperature_max", target_day, thr)
            print(f"Market {thr}C: {'Found' if mkt else 'Not found'}")


if __name__ == "__main__":
    debug_spread_placer()
