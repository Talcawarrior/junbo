"""Settlement anında Polymarket/Weather Underground'dan gerçek max temp çekip kaydeder."""
import os, sys, time, json, logging, sqlite3, requests, threading
from datetime import datetime, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import bot_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("SETTLEMENT_TEMPS")

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bot.db")

# Weather Underground API (Polymarket'in baz aldığı kaynak)
WU_API_KEY = os.getenv("WEATHERAPI_KEY")  # .env'de var
WU_URL = "https://api.weatherapi.com/v1/history.json"

# Polymarket Gamma API
GAMMA_URL = "https://gamma-api.polymarket.com/markets"


def get_wu_max_temp(city: str, target_date: str) -> float | None:
    """Weather Underground'dan (weatherapi.com) gunluk max temp ceker."""
    if not WU_API_KEY:
        log.warning("WEATHERAPI_KEY yok")
        return None

    # Sehir kodunu coord'a cevir (basit map)
    city_coords = {
        "Los Angeles": (34.05, -118.24),
        "New York": (40.71, -74.01),
        "Chicago": (41.88, -87.63),
        "Houston": (29.76, -95.37),
        "Phoenix": (33.45, -112.07),
        "Philadelphia": (39.95, -75.17),
        "San Antonio": (29.42, -98.49),
        "San Diego": (32.72, -117.16),
        "Dallas": (32.78, -96.80),
        "San Jose": (37.34, -121.89),
        "Austin": (30.27, -97.74),
        "Jacksonville": (30.33, -81.66),
        "Fort Worth": (32.76, -97.33),
        "Columbus": (39.96, -82.99),
        "Charlotte": (35.23, -80.84),
        "San Francisco": (37.77, -122.42),
        "Indianapolis": (39.77, -86.16),
        "Seattle": (47.61, -122.33),
        "Denver": (39.74, -104.99),
        "Washington": (38.91, -77.04),
        "Boston": (42.36, -71.06),
        "El Paso": (31.78, -106.49),
        "Detroit": (42.33, -83.05),
        "Nashville": (36.16, -86.78),
        "Memphis": (35.15, -90.05),
        "Portland": (45.52, -122.68),
        "Oklahoma City": (35.47, -97.52),
        "Las Vegas": (36.17, -115.14),
        "Louisville": (38.25, -85.76),
        "Baltimore": (39.29, -77.04),
        "Milwaukee": (43.04, -87.91),
        "Albuquerque": (35.08, -106.65),
        "Tucson": (32.22, -110.93),
        "Fresno": (36.74, -119.77),
        "Sacramento": (38.58, -121.49),
        "Mesa": (33.42, -111.83),
        "Kansas City": (39.10, -94.58),
        "Atlanta": (33.75, -84.39),
        "Long Beach": (33.77, -118.19),
        "Colorado Springs": (38.83, -104.82),
        "Raleigh": (35.78, -78.64),
        "Miami": (25.76, -80.19),
        "Virginia Beach": (36.85, -75.98),
        "Omaha": (41.26, -95.94),
        "Oakland": (37.80, -122.27),
        "Minneapolis": (44.98, -93.27),
        "Tulsa": (36.15, -95.99),
        "Tampa": (27.95, -82.46),
        "Arlington": (32.74, -97.09),
        "New Orleans": (29.95, -90.08),
        "Wichita": (37.69, -97.34),
        "Cleveland": (41.50, -81.69),
        "Bakersfield": (35.37, -119.02),
        "Aurora": (39.73, -104.83),
        "Anaheim": (33.84, -117.91),
        "Honolulu": (21.31, -157.86),
        "Stockton": (37.96, -121.29),
        "Corpus Christi": (27.80, -97.40),
        "Riverside": (33.95, -117.40),
        "Lexington": (38.04, -84.50),
        "Henderson": (36.04, -114.98),
        "St. Paul": (44.95, -93.09),
        "St. Louis": (38.63, -90.20),
        "Cincinnati": (39.10, -84.51),
        "Pittsburgh": (40.44, -79.99),
        "Greensboro": (36.07, -79.79),
        "Anchorage": (61.22, -149.90),
        "Plano": (33.02, -96.70),
        "Newark": (40.74, -74.17),
        "Toledo": (41.66, -83.55),
        "Orlando": (28.54, -81.38),
        "Chula Vista": (32.64, -117.08),
        "Irvine": (33.68, -117.83),
        "Newark": (37.52, -121.88),
        "Durham": (35.99, -78.90),
        "Chandler": (33.31, -111.84),
        "Fort Wayne": (41.13, -85.13),
        "St. Petersburg": (27.77, -82.64),
        "Laredo": (27.51, -99.47),
        "Buffalo": (42.89, -78.88),
        "Jersey City": (40.73, -74.08),
        "Chula Vista": (32.64, -117.08),
        "Chandler": (33.31, -111.84),
        "Orlando": (28.54, -81.38),
        "St. Petersburg": (27.77, -82.64),
        "Irvine": (33.68, -117.83),
        "Durham": (35.99, -78.90),
        "Cape Town": (-33.92, 18.42),
        "Istanbul": (41.01, 28.98),
        "Shanghai": (31.23, 121.47),
        "Hong Kong": (22.32, 114.17),
        "Seoul": (37.57, 126.98),
        "Tokyo": (35.68, 139.69),
        "London": (51.51, -0.13),
        "Paris": (48.86, 2.35),
        "Singapore": (1.35, 103.82),
        "Dubai": (25.20, 55.27),
        "Sydney": (-33.87, 151.21),
        "Melbourne": (-37.81, 144.96),
        "Brisbane": (-27.47, 153.02),
        "Perth": (-31.95, 115.86),
        "Auckland": (-36.85, 174.76),
        "Wellington": (-41.29, 174.78),
        "Christchurch": (-43.53, 172.64),
        "Amsterdam": (52.37, 4.90),
        "Berlin": (52.52, 13.41),
        "Madrid": (40.42, -3.70),
        "Rome": (41.90, 12.50),
        "Vienna": (48.21, 16.37),
        "Warsaw": (52.23, 21.01),
        "Lisbon": (38.72, -9.14),
        "Budapest": (47.50, 19.04),
        "Prague": (50.08, 14.44),
        "Bucharest": (44.43, 26.10),
        "Sofia": (42.70, 23.32),
        "Athens": (37.98, 23.73),
        "Dublin": (53.35, -6.26),
        "Oslo": (59.91, 10.75),
        "Stockholm": (59.33, 18.07),
        "Copenhagen": (55.68, 12.57),
        "Helsinki": (60.17, 24.94),
        "Reykjavik": (64.15, -21.90),
        "Moscow": (55.76, 37.62),
        "St. Petersburg": (59.93, 30.31),
        "Kiev": (50.45, 30.52),
        "Warsaw": (52.23, 21.01),
        "Bucharest": (44.43, 26.10),
        "Belgrade": (44.79, 20.45),
        "Zagreb": (45.81, 15.98),
        "Ljubljana": (46.06, 14.51),
        "Bratislava": (48.15, 17.11),
        "Vilnius": (54.69, 25.28),
        "Riga": (56.95, 24.11),
        "Tallinn": (59.44, 24.75),
        # Eksik sehirler (son betlerde gecenler)
        "Jeddah": (21.49, 39.19),
        "Karachi": (24.86, 67.01),
        "Lucknow": (26.85, 80.95),
        "Kuala Lumpur": (3.14, 101.69),
        "Manila": (14.60, 120.98),
        "Beijing": (39.90, 116.41),
        "Toronto": (43.65, -79.38),
    }

    if city not in city_coords:
        log.warning(f"Coord yok: {city}")
        return None

    lat, lon = city_coords[city]
    params = {
        "key": WU_API_KEY,
        "q": f"{lat},{lon}",
        "dt": target_date,
    }

    try:
        r = requests.get(WU_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        max_c = data.get("forecast", {}).get("forecastday", [{}])[0].get("day", {}).get("maxtemp_c")
        if max_c is not None:
            return float(max_c)
    except Exception as e:
        log.warning(f"WU API hata {city} {target_date}: {e}")
    return None


def parse_bucket_from_question(q: str) -> tuple[float, float] | None:
    """Question'dan bucket cikarir. Iki format:
    - Range: 'between 74-75°F' -> (74.0, 75.0)
    - Single: 'be 28°C' veya 'be 29°C' -> threshold bazli (threshold, threshold+1)
    """
    import re
    # Range format: between 98-99°F or between 98-99F
    m = re.search(r"between\s+(\d+)\s*-\s*(\d+)\s*[°]?\s*[FC]", q, re.IGNORECASE)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    # Single temperature format: "be 28°C" or "be 29°C" or "be 28F"
    # Degree symbol ° veya sadece C/F
    m = re.search(r"be\s+(\d+(?:\.\d+)?)\s*[°]?\s*[FC]", q, re.IGNORECASE)
    if m:
        t = float(m.group(1))
        is_f = 'F' in q.upper() and 'C' not in q.upper()
        if is_f:
            return (t, t + 1)
        else:
            f = t * 9/5 + 32
            low = int(f // 2) * 2
            return (low, low + 2)
    return None


def get_polymarket_question(market_id: str) -> str | None:
    """Gamma API'den market sorusunu ceker (bucket araligi)."""
    try:
        r = requests.get(f"{GAMMA_URL}/{market_id}", timeout=10)
        r.raise_for_status()
        data = r.json()
        return data.get("question", "")
    except Exception as e:
        log.warning(f"Gamma API hata {market_id}: {e}")
    return None


def c_to_f_bucket(c: float) -> tuple[float, float]:
    """Celsius'tan Fahrenheit bucket araligi (2F genisliginde)"""
    f = c * 9/5 + 32
    low = int(f // 2) * 2
    return (low, low + 2)


def settle_temps_job(lookback_hours: int = 12):
    """Son lookback_hours icinde kapanan marketler icin gercek temp cek."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Henuz result_data'si olmayan, kapanmis betler
    since = datetime.utcnow() - timedelta(hours=lookback_hours)
    cur.execute("""
        SELECT b.id, b.market_id, b.city, b.strike_temp, b.placed_at, b.settled_at
        FROM bets b
        WHERE b.status IN ('won','lost')
        AND (b.result_data IS NULL OR b.result_data = '')
        AND b.settled_at >= ?
        ORDER BY b.settled_at DESC
    """, (since.isoformat(),))

    rows = cur.fetchall()
    log.info(f"{len(rows)} bet icin settlement temp cekilecek")

    updated = 0
    for row in rows:
        bid, mid, city, strike, placed, settled = row

        # 1. WU'dan gercek max temp
        settled_date = settled[:10] if settled else None
        wu_temp = get_wu_max_temp(city, settled_date) if settled_date else None

        # 2. Polymarket bucket araligi (question parse)
        bucket = None
        q = get_polymarket_question(mid)
        if q:
            bucket = parse_bucket_from_question(q)

        # 3. Kendi bucket hesaplamamiz
        strike_bucket = c_to_f_bucket(strike) if strike else None

        result = {
            "wu_temp_c": wu_temp,
            "polymarket_question": q,
            "polymarket_bucket_f": bucket,
            "our_strike_bucket_f": strike_bucket,
            "collected_at": datetime.utcnow().isoformat(),
        }

        cur.execute("""
            UPDATE bets SET result_data = ? WHERE id = ?
        """, (json.dumps(result), bid))
        updated += 1

        log.info(f"Bet {bid}: {city} strike={strike}C wu={wu_temp}C bucket={bucket} our_bucket={strike_bucket}")

        time.sleep(0.5)  # API rate limit

    conn.commit()
    conn.close()
    log.info(f"Guncellendi: {updated} bet")
    return updated


def main():
    log.info("Settlement temp collector basliyor...")
    settle_temps_job(lookback_hours=24)  # Son 24 saat
    log.info("Bitti")


if __name__ == "__main__":
    main()