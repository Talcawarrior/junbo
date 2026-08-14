"""METAR canli sicaklik scraper — NOAA aviationweather.gov (bedava, 30 dk guncelleme).

Polymarket weather marketleri Weather Underground istasyon verisinden cozulur.
WU ticari API'si ~$500/ay. Ama WU zaten NOAA/NWS istasyon verisini yayinlar;
aviationweather.gov (NOAA resmi METAR API) AYNI istasyon verisini BEDAVA verir.

Bu modul:
- fetch_metar_live(icao): o istasyonun son METAR gozlemini (anlik sicaklik) doner
- fetch_metar_day(icao, day): bir gunun (UTC) tum METAR gozlemlerini doner
- detect_peak(temps_ordered): gun icinde kumulatif max'i takip eder,
  sicaklik son 2 gozlemde max'tan dususe gectiyse zirve KILITLENDI dondurur.

METAR gozlemleri 30 dk'da bir yayinlanir (obsTime epoch, temp Celsius).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger("SCRAPER_METAR")

METAR_URL = "https://aviationweather.gov/api/data/metar"
HISTORY_URL = "https://aviationweather.gov/api/data/metar"

# 30dk'da bir METAR yayinlanir; istasyon basina son 30 saatlik gecmis cekilir.
HISTORY_HOURS = 30
REQUEST_TIMEOUT = 20

# In-process cache: (icao, day) -> [(epoch, temp_c)]
_CACHE: dict[tuple[str, str], list[tuple[int, float]]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 25 * 60  # 25 dk (METAR 30dk guncellenir)


def _cache_get(key):
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        value, expires = entry
        if time.monotonic() > expires:
            _CACHE.pop(key, None)
            return None
        return value


def _cache_set(key, value):
    with _CACHE_LOCK:
        _CACHE[key] = (value, time.monotonic() + _CACHE_TTL)


def _fetch_metar(icao: str, hours: int = HISTORY_HOURS) -> list[tuple[int, float]] | None:
    """aviationweather.gov'dan istasyonun son N saatlik METAR gozlemlerini ceker.

    Returns: [(epoch, temp_c), ...] sicakliga gore artan (zaman sirali), temp yoksa [].
    """
    try:
        resp = requests.get(
            METAR_URL,
            params={"ids": icao, "format": "json", "hours": str(hours)},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("METAR fetch fail %s: %s", icao, exc)
        return None
    rows = []
    for m in data:
        obs = m.get("obsTime")
        temp = m.get("temp")
        if obs is None or temp is None:
            continue
        try:
            rows.append((int(obs), float(temp)))
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda x: x[0])
    return rows


def fetch_metar_live(icao: str) -> Optional[float]:
    """Istasyonun SON anlik sicakligini doner (Celsius). Yoksa None."""
    rows = _fetch_metar(icao, hours=6)
    if not rows:
        return None
    return rows[-1][1]


def fetch_metar_day(icao: str, day: str) -> list[tuple[int, float]]:
    """Bir UTC gununun tum METAR gozlemlerini doner. Cache'li.

    day: 'YYYY-MM-DD' (UTC). Gozlemler zaman sirali (epoch, temp_c).
    """
    cache_key = (icao, day)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    rows = _fetch_metar(icao, hours=HISTORY_HOURS) or []
    # gun bazinda filtrele
    day_rows = []
    for epoch, temp in rows:
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        if dt.strftime("%Y-%m-%d") == day:
            day_rows.append((epoch, temp))
    day_rows.sort(key=lambda x: x[0])
    _cache_set(cache_key, day_rows)
    return day_rows


def detect_peak(day_rows: list[tuple[int, float]]) -> tuple[Optional[float], bool]:
    """Gun icinde kumulatif max'i takip eder, zirve KILITLI mi doner.

    Kural (kullanici 2026-08-14): sicaklik max'a cikar, sonra DUSER. Zirve
    kilitlenmesi = max olusan degerden sonra EN AZ 2 ardışık gozlem max'in
    altinda (dusus teyidi). O an kazanan bucket = round(cummax).

    Returns: (kilitli_max, is_confirmed). is_confirmed=False ise henuz zirve
    teyit edilmemis (hala yukselebilir).
    """
    if len(day_rows) < 3:
        return (day_rows[-1][1] if day_rows else None, False)
    cummax = day_rows[0][1]
    drop_count = 0
    confirmed_max = None
    for i in range(1, len(day_rows)):
        cur = day_rows[i][1]
        if cur > cummax:
            cummax = cur
            drop_count = 0
        elif cur < cummax:
            drop_count += 1
            if drop_count >= 2:
                confirmed_max = cummax
                break
        else:  # esit -> dusus sayilmaz
            drop_count = 0
    if confirmed_max is not None:
        return confirmed_max, True
    return cummax, False


def metar_live_check() -> bool:
    """METAR API erisilebilir mi (health check)."""
    try:
        rows = _fetch_metar("RJTT", hours=1)
        return bool(rows)
    except Exception:  # noqa: BLE001
        return False


def archive_metar_observations(icao: str, city: str, day_rows: list[tuple[int, float]]) -> int:
    """METAR gozlemlerini kalici arsive kaydeder (gecmis backtest icin).

    aviationweather.gov sadece son 30 saat tutar; bu arsiv her gozlemi saklar.
    Ayni (city_code, obs_time) kaydi tekrarlanmaz.
    """
    from database.db import get_session
    from database.models import MetarObservation
    from datetime import datetime, timezone

    if not day_rows:
        return 0
    added = 0
    try:
        with get_session() as session:
            for epoch, temp in day_rows:
                obs_dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
                exists = (
                    session.query(MetarObservation)
                    .filter(
                        MetarObservation.city_code == icao,
                        MetarObservation.obs_time == obs_dt.replace(tzinfo=None),
                    )
                    .first()
                )
                if exists:
                    continue
                session.add(
                    MetarObservation(
                        city_code=icao,
                        city=city,
                        temp_c=temp,
                        obs_time=obs_dt.replace(tzinfo=None),
                        day=obs_dt.strftime("%Y-%m-%d"),
                    )
                )
                added += 1
            if added:
                session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("metar archive fail %s", icao)
        return 0
    return added
