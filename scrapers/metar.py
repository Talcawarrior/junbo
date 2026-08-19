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
import ssl
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger("SCRAPER_METAR")

METAR_URL = "https://aviationweather.gov/api/data/metar"
HISTORY_URL = "https://aviationweather.gov/api/data/metar"

# 30dk'da bir METAR yayinlanir; istasyon basina son 30 saatlik gecmis cekilir.
HISTORY_HOURS = 30
# 2026-08-19: 20 -> 12 sn + retry 4: aksamlari aviationweather yavasliyor;
# uzun timeout 3 denemede 60+ sn surep donguyu (60s _FETCH_TIMEOUT) tasiriyordu.
# Kisa timeout + hizli retry: en kotu ~48 sn.
REQUEST_TIMEOUT = 12

# Avast Web/Mail Shield TLS intercept: Windows sistem kok deposunu yukle
# (weather_ensemble.py ile ayni cozum, 2026-08-19 metar.py'ye de tasindi),
# dogrulama ACIK kalir. 17 Agu'da SSLCertVerificationError goruldu; requests
# certifi Avast kokunu bilmiyor, sistem store biliyor.
_SYSTEM_TLS = ssl.create_default_context()


class _SystemStoreAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):  # type: ignore[override]
        kwargs["ssl_context"] = _SYSTEM_TLS
        return super().init_poolmanager(*args, **kwargs)


_SESSION = requests.Session()
_SESSION.mount("https://", _SystemStoreAdapter())

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

    2026-08-18: 76 "Read timed out" gunluk hata — tek deneme yetersizdi; 3
    deneme + backoff eklendi (timeout'lar gecici, retry ile kurtariliyor).
    2026-08-19: deneme 3 -> 4, timeout 12 sn (aksam yavasligi dongu 60s
    limitini asmasin diye).
    """
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            # 2026-08-16: config/settings.py global olarak HTTP_PROXY/HTTPS_PROXY env
            # set ediyor (Polymarket SOCKS). aviationweather.gov geo-block'lu DEGIL ve
            # proxy uzerinden 20s timeout (172 hata) -> bu istek DIRECT gider.
            resp = _SESSION.get(
                METAR_URL,
                params={"ids": icao, "format": "json", "hours": str(hours)},
                timeout=REQUEST_TIMEOUT,
                proxies={"http": None, "https": None, "all": None},  # type: ignore[dict-item]
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    else:
        logger.warning("METAR fetch fail %s (3 deneme): %s", icao, last_exc)
        try:
            from utils.activity_log import log_event

            log_event("error", icao, f"METAR fetch fail (3 deneme): {type(last_exc).__name__} {str(last_exc)[:120]}")
        except Exception:  # noqa: BLE001
            pass
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


def fetch_metar_day(icao: str, day: str, utc_offset_hours: float = 0.0) -> list[tuple[int, float]]:
    """Bir YEREL gunun tum METAR gozlemlerini doner. Cache'li.

    day: 'YYYY-MM-DD'. 2026-08-19 kullanici: "NY cok batida nasil kitledi,
    bu dunun mu" — UTC gun filtresi yanlisti (NY icin UTC 00:00-04:00 =
    dunun yerel aksami; dunun peak'i bugunun kilidi saniliyordu). Artik
    pencere yerel gune gore kaydirilir: yerel 00:00 = UTC 00:00 - offset.
    """
    cache_key = (icao, day, utc_offset_hours)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    rows = _fetch_metar(icao, hours=HISTORY_HOURS) or []
    # YEREL gun bazinda filtrele
    day_rows = []
    for epoch, temp in rows:
        dt = datetime.fromtimestamp(epoch + utc_offset_hours * 3600, tz=timezone.utc)
        if dt.strftime("%Y-%m-%d") == day:
            day_rows.append((epoch, temp))
    day_rows.sort(key=lambda x: x[0])
    _cache_set(cache_key, day_rows)
    return day_rows


# 2026-08-18 audit fix (M3): sehrin gercek saat dilimi. Eski round(lon/15)
# nominal offset'i verir ama politik sinirlar + DST ile yanlis olabilir:
#   - China/Singapore/Malaysia lon/15=+7  -> gercek UTC+8
#   - Seoul lon/15=+8                     -> gercek UTC+9
#   - London lon/15=+0 (BST)              -> gercek UTC+1 (DST)
#   - Toronto lon/15=-5 (EDT)             -> gercek UTC-4 (DST)
#   - Lucknow (VILK) lon/15=+5            -> gercek UTC+5:30
# zoneinfo (IANA tz DB, Windows 10+ dahili) gercek offset + DST verir.
_CITY_TZ: dict[str, str] = {
    "CYYZ": "America/Toronto",
    "EDDM": "Europe/Berlin",
    "EFHK": "Europe/Helsinki",
    "EGLC": "Europe/London",
    "EHAM": "Europe/Amsterdam",
    "EPWA": "Europe/Warsaw",
    "FACT": "Africa/Johannesburg",
    "KATL": "America/New_York",
    "KAUS": "America/Chicago",
    "KBKF": "America/Denver",
    "KDAL": "America/Chicago",
    "KHOU": "America/Chicago",
    "KLAX": "America/Los_Angeles",
    "KLGA": "America/New_York",
    "KMIA": "America/New_York",
    "KORD": "America/Chicago",
    "KSEA": "America/Los_Angeles",
    "KSFO": "America/Los_Angeles",
    "LEMD": "Europe/Madrid",
    "LFPB": "Europe/Paris",
    "LIMC": "Europe/Rome",
    "LLBG": "Asia/Jerusalem",
    "LTAC": "Europe/Istanbul",
    "LTFM": "Europe/Istanbul",
    "MMMX": "America/Mexico_City",
    "MPMG": "America/Panama",
    "NZWN": "Pacific/Auckland",
    "OEJN": "Asia/Riyadh",
    "OPKC": "Asia/Karachi",
    "RCSS": "Asia/Taipei",
    "RJTT": "Asia/Tokyo",
    "RKPK": "Asia/Seoul",
    "RKSI": "Asia/Seoul",
    "RPLL": "Asia/Manila",
    "SAEZ": "America/Argentina/Buenos_Aires",
    "SBGR": "America/Sao_Paulo",
    "UUWW": "Europe/Moscow",
    "VHHH": "Asia/Hong_Kong",
    "VILK": "Asia/Kolkata",
    "WMKK": "Asia/Kuala_Lumpur",
    "WSSS": "Asia/Singapore",
    "ZBAA": "Asia/Shanghai",
    "ZGGG": "Asia/Shanghai",
    "ZGSZ": "Asia/Shanghai",
    "ZHHH": "Asia/Shanghai",
    "ZSPD": "Asia/Shanghai",
    "ZSQD": "Asia/Shanghai",
    "ZUCK": "Asia/Shanghai",
    "ZUUU": "Asia/Shanghai",
}


def city_utc_offset(city_code: str, day: str, fallback_lon: Optional[float] = None) -> float:
    """Sehrin UTC offset'ini saat cinsinden verir (target gun, DST dahil).

    Bilinen sehirlerde zoneinfo tz DB'sinden gercek offset okunur; bilinmeyen
    sehirde lon/15 nominal degerine duser. day: 'YYYY-MM-DD'.
    """
    import zoneinfo

    tz_name = _CITY_TZ.get(city_code)
    if tz_name:
        try:
            tz = zoneinfo.ZoneInfo(tz_name)
            y, m, d = int(day[:4]), int(day[5:7]), int(day[8:10])
            base = datetime(y, m, d, 0, 0, 0, tzinfo=timezone.utc)
            off = base.astimezone(tz).utcoffset()
            if off is not None:
                return off.total_seconds() / 3600.0
        except Exception as exc:  # noqa: BLE001 — tz DB eksikse fallback
            logger.warning("metar tz %s (%s) cozulemedi: %s", city_code, tz_name, exc)
    try:
        return round(float(fallback_lon) / 15.0) if fallback_lon is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def detect_peak(
    day_rows: list[tuple[int, float]], min_local_hour: int = 13, utc_offset_hours: float = 0.0
) -> tuple[Optional[float], bool]:
    """Gun icinde kumulatif max'i takip eder, zirve KILITLI mi doner.

    Kural (kullanici 2026-08-14): sicaklik max'a cikar, sonra DUSER. Zirve
    kilitlenmesi = max olusan degerden sonra EN AZ 2 ardısık gozlem max'in
    altinda (dusus teyidi). O an kazanan bucket = round(cummax).

    BUGFIX (2026-08-15): sabahin gece sicakligi (00:00'da 25C) zirve
    saniliyordu -> gercel max oglen sonrasi 31C iken 25C'ye bet acildi.
    Cozum: zirve ancak YEREL saat >= min_local_hour (varsayilan 13:00, gunduz
    max'in olustugu dilim) olduktan sonra kilitlenir.

    BUGFIX (2026-08-16): kullanici "benim saatimle degil, sehirin YEREL
    saatine gore gir" dedi. Sabit UTC saat kistiri sehirlerin gercek max
    saatine uymuyordu (Wellington 03:00 UTC, Hong Kong 07:00 UTC max yapiyor
    ama UTC>=15 kisti peak'i kaciriyordu). Artik kilitlenme kurali YEREL
    saat uzerinden: utc_offset_hours ile epoch'u sehir yerel saatine cevir,
    yerel saat >= min_local_hour ise peak say.

    Returns: (kilitli_max, is_confirmed). is_confirmed=False ise henuz zirve
    teyit edilmemis (hala yukselebilir).
    """
    if len(day_rows) < 3:
        return (day_rows[-1][1] if day_rows else None, False)
    cummax = day_rows[0][1]
    for i in range(1, len(day_rows)):
        epoch, cur = day_rows[i]
        # Yerel saat: UTC epoch + sehir offset
        local_dt = datetime.fromtimestamp(epoch + utc_offset_hours * 3600, tz=timezone.utc)
        # Saat esigi: sabah/gece dususu zirve sayilmaz (yerel gunduz max olusur)
        if local_dt.hour < min_local_hour:
            cummax = max(cummax, cur)
            continue
        if cur > cummax:
            cummax = cur
        elif cur < cummax:
            # 2026-08-18 kullanici karari: 1 dusus YETERLI — 20 21 22 22 21
            # orneginde 22'yi kilitler, ikinci dusus beklenmez. Erken giris:
            # fiyat daha 0.99'a oturmadan girilir; zirve asilirsa kapat +
            # yeni zirveye ac (jobs/metar_peak.py aktar mantigi).
            return cummax, True
        else:  # esit -> dusus sayilmaz
            pass
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
