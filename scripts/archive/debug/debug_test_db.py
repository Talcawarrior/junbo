import sys
import os

sys.path.insert(0, ".")

# Import test configuration to use temp DB
from tests.conftest import _TMP_DB_PATH

print(f"Temp DB path: {_TMP_DB_PATH}")

if _TMP_DB_PATH and os.path.exists(_TMP_DB_PATH):
    print(f"Temp DB exists: {os.path.getsize(_TMP_DB_PATH)} bytes")

    # Check what's in the temp DB
    import sqlite3

    con = sqlite3.connect(_TMP_DB_PATH)
    cur = con.cursor()

    print("\n=== Temp DB Content ===")

    # Check tables
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = cur.fetchall()
    print(f"Tables: {[t[0] for t in tables]}")

    # Check WeatherMarket table
    cur.execute("SELECT COUNT(*) FROM weather_markets")
    market_count = cur.fetchone()[0]
    print(f"WeatherMarket count: {market_count}")

    if market_count > 0:
        cur.execute("SELECT id, city, threshold, status FROM weather_markets LIMIT 5")
        markets = cur.fetchall()
        print("Sample markets:")
        for m in markets:
            print(f"  {m}")

    # Check WeatherForecast table
    cur.execute("SELECT COUNT(*) FROM weather_forecasts")
    forecast_count = cur.fetchone()[0]
    print(f"WeatherForecast count: {forecast_count}")

    # Check HistoricalCalibration table
    cur.execute("SELECT COUNT(*) FROM historical_calibrations")
    cal_count = cur.fetchone()[0]
    print(f"HistoricalCalibration count: {cal_count}")

    con.close()
else:
    print("No temp DB found - test may not have run yet")
