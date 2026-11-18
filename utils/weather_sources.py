"""Free / cheap weather sources for forecast + settlement verification.

Verified 2026-08-25 against live APIs:
- IEM asos.py: ORD/LGA MAX via max(tmpf) -> VC ile 0.4C ort (KORD test)
- Visual Crossing timeline: 32 havalani x7 gun OK, tempmax/tempmin var, 1000/gun free
- WeatherAPI history: 1008 limited >7 gun, sadece 7 gun
- OGIMET GSOD: 07115 Unknown station (yeni havalimanlari yok)
- NWS / AviationWeather: key yok, 100 req/dk
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Optional

import requests

from config.settings import bot_config

logger = logging.getLogger("WEATHER_SOURCES")


def _vc_key() -> str:
    try:
        return bot_config.meteo.next_vc_key()  # type: ignore[attr-defined]
    except Exception:
        return bot_config.meteo.vc_api_key


def _vc_rotate() -> str:
    try:
        return bot_config.meteo.rotate_vc_key()  # type: ignore[attr-defined]
    except Exception:
        return bot_config.meteo.vc_api_key


def _vc_keys_available() -> bool:
    keys = getattr(bot_config.meteo, "vc_api_keys", None) or []
    return bool(keys) or bool(bot_config.meteo.vc_api_key)


IEM_URL = bot_config.meteo.iem_url
NWS_URL = bot_config.meteo.nws_url
AVIATION_URL = bot_config.meteo.aviation_url
VC_URL = bot_config.meteo.vc_url

# Simple in-memory cache for settlement_actual / VC daily to avoid 429
_VC_CACHE: dict[tuple[float, float, str], dict] = {}
_VC_CACHE_TS: dict[tuple[float, float, str], float] = {}
_CACHE_TTL_SEC = 3600  # 1 hour


# Visual Crossing
def vc_daily(lat: float, lon: float, target: date) -> Optional[dict]:
    """VC timeline single day -> {tmax, tmin, source}. None if 401/limit."""
    if not _vc_keys_available():
        return None
    key = (round(lat, 4), round(lon, 4), str(target))
    now_ts = time.time()
    if key in _VC_CACHE and now_ts - _VC_CACHE_TS.get(key, 0) < _CACHE_TTL_SEC:
        return _VC_CACHE[key]
    url = f"{VC_URL}/{lat},{lon}/{target}/{target}"
    vc_params = {"key": None, "unitGroup": "metric", "include": "days", "contentType": "json"}
    try:
        vc_params["key"] = _vc_key()
        r = requests.get(url, params=vc_params, timeout=15)
        if r.status_code == 429:
            logger.warning("VC 429 rate-limit %s - rotating key", target)
            time.sleep(1)
            vc_params["key"] = _vc_rotate()
            r = requests.get(url, params=vc_params, timeout=15)
            if r.status_code == 429:
                return _VC_CACHE.get(key)
        if r.status_code != 200:
            logger.warning("VC %s %s: %s", lat, lon, r.status_code)
            return _VC_CACHE.get(key)
        days = r.json().get("days", [])
        if not days:
            return None
        d = days[0]
        val = {"tmax": float(d["tempmax"]), "tmin": float(d["tempmin"]), "source": d.get("source", "fcst")}
        _VC_CACHE[key] = val
        _VC_CACHE_TS[key] = now_ts
        time.sleep(0.3)  # throttle to avoid 429
        return val
    except Exception as e:
        logger.warning("VC daily fail %s: %s", target, e)
        return _VC_CACHE.get(key)


def vc_range(lat: float, lon: float, start: date, end: date) -> dict[str, dict]:
    """VC range -> {date_str: {tmax, tmin}}. One call for 7-15 days."""
    if not _vc_keys_available():
        return {}
    url = f"{VC_URL}/{lat},{lon}/{start}/{end}"
    vc_params = {"key": None, "unitGroup": "metric", "include": "days", "contentType": "json"}
    try:
        vc_params["key"] = _vc_key()
        r = requests.get(url, params=vc_params, timeout=30)
        if r.status_code == 429:
            logger.warning("VC range 429 %s - rotating key", start)
            time.sleep(1)
            vc_params["key"] = _vc_rotate()
            r = requests.get(url, params=vc_params, timeout=30)
        if r.status_code != 200:
            logger.warning("VC range %s: %s", start, r.status_code)
            return {}
        out: dict[str, dict] = {}
        for d in r.json().get("days", []):
            out[d["datetime"]] = {
                "tmax": float(d["tempmax"]),
                "tmin": float(d["tempmin"]),
                "source": d.get("source", ""),
            }
        time.sleep(0.3)  # throttle
        return out
    except Exception as e:
        logger.warning("VC range fail: %s", e)
        return {}


# Iowa Mesonet (IEM) - US only, free, 90d+
def iem_daily_max(station_3: str, target: date) -> Optional[float]:
    """IEM asos.py max(tmpf) -> C. station_3 = LGA/ORD (K'siz). None if no data."""
    y, m, d = str(target).split("-")
    for sta in (station_3, "K" + station_3):
        params = {
            "station": sta,
            "data": "tmpf",
            "year1": y,
            "month1": m,
            "day1": d,
            "year2": y,
            "month2": m,
            "day2": d,
            "format": "comma",
            "latlon": "no",
            "direct": "no",
        }
        try:
            r = requests.get(IEM_URL, params=params, timeout=15)
            if r.status_code != 200:
                continue
            lines = [ln for ln in r.text.split("\n") if ln and not ln.startswith("#") and not ln.startswith("station")]
            vals: list[float] = []
            for ln in lines:
                parts = ln.split(",")
                if len(parts) >= 3:
                    v = parts[2].strip()
                    if v not in ("M", "", "null"):
                        try:
                            vals.append(float(v))
                        except ValueError:
                            pass
            if vals:
                return round((max(vals) - 32) * 5 / 9, 1)
        except Exception:
            continue
    return None


# NWS API - US grid forecast (7d)
def nws_forecast_max(lat: float, lon: float) -> Optional[float]:
    """NWS /points -> forecast daily max (C). US only. None if outside US."""
    try:
        r = requests.get(f"{NWS_URL}/points/{lat},{lon}", headers={"User-Agent": "Junbo/1.0"}, timeout=10)
        if r.status_code != 200:
            return None
        forecast_url = r.json()["properties"]["forecast"]
        r2 = requests.get(forecast_url, headers={"User-Agent": "Junbo/1.0"}, timeout=10)
        if r2.status_code != 200:
            return None
        periods = r2.json()["properties"]["periods"]
        # First period with isDaytime true has temperature (F)
        for p in periods:
            if p.get("isDaytime"):
                f = float(p["temperature"])
                return round((f - 32) * 5 / 9, 1)
        return None
    except Exception as e:
        logger.debug("NWS fail %s,%s: %s", lat, lon, e)
        return None


# AviationWeather METAR - running high today
def metar_running_high(icao: str) -> Optional[float]:
    """Latest METAR temp (C) for intraday running high. US+global."""
    try:
        r = requests.get(f"{AVIATION_URL}/metar", params={"ids": icao, "format": "json"}, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        if not data:
            return None
        # Most recent report
        temp = data[0].get("temp")
        if temp is not None:
            return float(temp)
        return None
    except Exception:
        return None


# Settlement verification helper
def settlement_actual(
    icao: str,
    lat: float,
    lon: float,
    target: date,
) -> tuple[Optional[float], str]:
    """Best-effort actual MAX for settlement date.

    Priority (free, same ASOS):
    1. IEM (US) -> most accurate, WU ile birebir
    2. VC timeline -> global, 0.2-0.4C IEM ile (NY test)
    Returns (tmax_c, source).
    """
    # IEM for US K*** -> try 3-letter
    if icao.startswith("K") and len(icao) == 4:
        station3 = icao[1:]
        v = iem_daily_max(station3, target)
        if v is not None:
            return v, "IEM"
    # VC fallback (global)
    vc = vc_daily(lat, lon, target)
    if vc and vc["tmax"] is not None:
        return vc["tmax"], "VC"
    return None, "none"


def _today_utc() -> date:
    return date.today()


# Small CLI for manual 15-source check
if __name__ == "__main__":
    tgt = date.today() - timedelta(days=1)
    print("VC LTFM", vc_daily(41.2753, 28.7519, tgt))
    print("IEM LGA", iem_daily_max("LGA", tgt))
    print("NWS KLGA", nws_forecast_max(40.7769, -73.8740))
    print("METAR KLGA", metar_running_high("KLGA"))
