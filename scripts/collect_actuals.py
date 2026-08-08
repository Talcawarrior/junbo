#!/usr/bin/env python3
"""Daily/6-hourly actual temperatures collector from Open-Meteo Archive API.

Fetches ground-truth temperatures for all cities in bot.db and stores in data/actuals.db.
Supports backfill on first run and incremental updates on subsequent runs.
"""

import json
import logging
import ssl
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ── Paths ─────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
BOT_DB = ROOT / "data" / "bot.db"
ACTUALS_DB = ROOT / "data" / "actuals.db"
LOG_DIR = ROOT / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ACTUALS] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "collect_actuals.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("collect_actuals")

# ── Constants ─────────────────────────────────────────────────────────────
TIMEOUT = 180  # 3 minutes per HTTP request
MAX_RETRIES = 5  # retry count on failure
RETRY_DELAY = 60  # seconds between retries
ARCHIVE_API = "https://archive-api.open-meteo.com/v1/archive"
DEFAULT_VARIABLES = [
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",
    "precipitation_sum",
    "wind_speed_10m_max",
]
BACKFILL_DAYS = 90  # how many days back to fetch on first run

# SSL context for systems with expired certs
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


# ── Database setup ────────────────────────────────────────────────────────
def init_actuals_db() -> None:
    """Create actuals.db and actual_temperatures table if not exists."""
    conn = sqlite3.connect(str(ACTUALS_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS actual_temperatures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            date TEXT NOT NULL,              -- YYYY-MM-DD
            temperature_2m_max REAL,
            temperature_2m_min REAL,
            temperature_2m_mean REAL,
            precipitation_sum REAL,
            wind_speed_10m_max REAL,
            fetched_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(city, date)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_actuals_city_date
        ON actual_temperatures (city, date)
    """)
    conn.commit()
    conn.close()
    logger.info("actuals.db initialized")


def get_cities() -> list[dict[str, Any]]:
    """Query unique cities with coordinates from bot.db (read-only)."""
    conn = sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("""
        SELECT DISTINCT city, latitude, longitude
        FROM weather_markets
        WHERE city IS NOT NULL
          AND latitude IS NOT NULL
          AND longitude IS NOT NULL
          AND city != ''
    """)
    cities = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return cities


def get_last_fetched_date(city: str) -> str | None:
    """Get the most recent date already stored for a city."""
    conn = sqlite3.connect(str(ACTUALS_DB))
    cursor = conn.execute(
        "SELECT MAX(date) FROM actual_temperatures WHERE city = ?",
        (city,),
    )
    result = cursor.fetchone()
    conn.close()
    return result[0] if result and result[0] else None


def fetch_archive_actuals(
    latitude: float,
    longitude: float,
    start_date: str,
    end_date: str,
) -> dict[str, Any] | None:
    """Fetch historical actuals from Open-Meteo Archive API with retry logic."""
    import urllib.request
    import urllib.error
    import urllib.parse

    vars_str = ",".join(DEFAULT_VARIABLES)
    url = (
        f"{ARCHIVE_API}?latitude={latitude}&longitude={longitude}"
        f"&start_date={start_date}&end_date={end_date}"
        f"&daily={vars_str}&timezone=auto"
    )

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl_ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            logger.warning(
                "Attempt %d/%d failed for lat=%.2f lon=%.2f: %s",
                attempt,
                MAX_RETRIES,
                latitude,
                longitude,
                exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    logger.error(
        "All %d attempts failed for lat=%.2f lon=%.2f",
        MAX_RETRIES,
        latitude,
        longitude,
    )
    return None


def parse_archive_response(raw: dict[str, Any], city: str, latitude: float, longitude: float) -> list[dict[str, Any]]:
    """Parse Open-Meteo archive response into list of daily records."""
    daily = raw.get("daily", {})
    dates = daily.get("time", [])
    if not dates:
        return []

    records = []
    fetched_at = datetime.now(timezone.utc).isoformat()

    for i, date_str in enumerate(dates):
        record = {
            "city": city,
            "latitude": latitude,
            "longitude": longitude,
            "date": date_str,
            "temperature_2m_max": daily.get("temperature_2m_max", [None])[i],
            "temperature_2m_min": daily.get("temperature_2m_min", [None])[i],
            "temperature_2m_mean": daily.get("temperature_2m_mean", [None])[i],
            "precipitation_sum": daily.get("precipitation_sum", [None])[i],
            "wind_speed_10m_max": daily.get("wind_speed_10m_max", [None])[i],
            "fetched_at": fetched_at,
        }
        # Convert None to actual None for SQLite
        for k, v in record.items():
            if v == "null" or v == "":
                record[k] = None
        records.append(record)

    return records


def upsert_records(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> int:
    """Upsert records into actual_temperatures table. Returns count of new/updated rows."""
    if not records:
        return 0

    count = 0
    for rec in records:
        try:
            conn.execute(
                """
                INSERT INTO actual_temperatures
                    (city, latitude, longitude, date,
                     temperature_2m_max, temperature_2m_min, temperature_2m_mean,
                     precipitation_sum, wind_speed_10m_max, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(city, date) DO UPDATE SET
                    temperature_2m_max = excluded.temperature_2m_max,
                    temperature_2m_min = excluded.temperature_2m_min,
                    temperature_2m_mean = excluded.temperature_2m_mean,
                    precipitation_sum = excluded.precipitation_sum,
                    wind_speed_10m_max = excluded.wind_speed_10m_max,
                    fetched_at = excluded.fetched_at,
                    created_at = datetime('now')
            """,
                (
                    rec["city"],
                    rec["latitude"],
                    rec["longitude"],
                    rec["date"],
                    rec["temperature_2m_max"],
                    rec["temperature_2m_min"],
                    rec["temperature_2m_mean"],
                    rec["precipitation_sum"],
                    rec["wind_speed_10m_max"],
                    rec["fetched_at"],
                ),
            )
            count += 1
        except sqlite3.Error as exc:
            logger.error("Failed to upsert %s %s: %s", rec["city"], rec["date"], exc)

    return count


def collect_once() -> int:
    """Run one collection cycle. Returns total records upserted."""
    cities = get_cities()
    if not cities:
        logger.info("No cities found in bot.db")
        return 0

    logger.info("Found %d cities, fetching actuals...", len(cities))

    total_upserted = 0
    end_date = datetime.now(timezone.utc).date().isoformat()

    for city_info in cities:
        city = city_info["city"]
        lat = city_info["latitude"]
        lon = city_info["longitude"]

        # Determine start date
        today = datetime.now(timezone.utc).date().isoformat()
        last_date = get_last_fetched_date(city)
        if last_date:
            if last_date >= today:
                # Bugunku veri zaten var -> ayni gunu tekrar cek (gun ici
                # guncelleme; archive API kismi saatler dondurur). Onceki
                # mantik (last+1) start'i bugunu asiyordu -> Open-Meteo
                # 400 Bad Request, veri 00:13'teki kismi haliyle kaliyordu.
                start_date = today
            else:
                # Incremental: fetch from next day after last successful date
                start_date = (datetime.fromisoformat(last_date) + timedelta(days=1)).date().isoformat()
        else:
            # First run: backfill 90 days
            start_date = (datetime.now(timezone.utc).date() - timedelta(days=BACKFILL_DAYS)).isoformat()

        logger.info("  %s: fetching %s to %s", city, start_date, end_date)

        # 2026-08-08 bugfix: incremental start (last_date+1) bugunu asabilir
        # ("fetching 2026-08-09 to 2026-08-08" -> Open-Meteo 400 Bad Request,
        #  "All 5 attempts failed"). Zaten guncel sehirde fetch YOK.
        if start_date > end_date:
            logger.info("  %s: already up to date (start=%s > end=%s), skip", city, start_date, end_date)
            continue

        raw = fetch_archive_actuals(lat, lon, start_date, end_date)
        if raw is None:
            logger.warning("  %s: fetch failed, skipping", city)
            continue

        records = parse_archive_response(raw, city, lat, lon)
        if not records:
            logger.warning("  %s: no data returned", city)
            continue

        conn = sqlite3.connect(str(ACTUALS_DB))
        upserted = upsert_records(conn, records)
        conn.commit()
        conn.close()

        logger.info("  %s: %d records upserted", city, upserted)
        total_upserted += upserted

        # Rate limit: be nice to Open-Meteo
        time.sleep(0.25)

    logger.info("Collection complete: %d total records upserted", total_upserted)
    return total_upserted


def main() -> None:
    """Main entry point."""
    logger.info("=== Actuals collection started ===")
    init_actuals_db()
    count = collect_once()
    logger.info("=== Done: %d records ===", count)


if __name__ == "__main__":
    main()
