"""Yeni sehir oto-kesif: Polymarket'te beliren bilinmeyen sehri haritala.

Kullanici karari 2026-09-13:
  1. Eski veriden HEMEN bias: backfill zaten gunluk tum sehirleri kapsar;
     gecmis forecast yoksa ilk gun GLOBAL-ortalama bias fallback kullanilir
     (spread yeni sehri atlamaz).
  2. Yeni sehir + koordinat OTOMATIK yazilir: config/city_overrides.json
     (settings.py degistirilmez; get_live_city_map her dongude okur).

Yontem (harici DB yok):
  - Bilinmeyen sehirli marketlerde resolution ICAO dogrudan market verisinde
    olur (resolutionSource URL veya description'da site=XXXX).
  - ICAO -> aviationweather stationinfo (site adi + lat/lon).
  - Open-Meteo geocoding ile site adi dogrulanir (<150km).
"""

import json
import logging
import math
import os

import requests

logger = logging.getLogger("NEW_CITY_JOB")

_MARKER_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", ".last_new_city_run")
_TIMEOUT = 15
_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
_STATION_URL = "https://aviationweather.gov/api/data/stationinfo"


def _today() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def should_run() -> bool:
    try:
        with open(_MARKER_PATH, encoding="utf-8") as fh:
            return fh.read().strip() != _today()
    except Exception:
        return True


def _write_marker() -> None:
    try:
        os.makedirs(os.path.dirname(_MARKER_PATH), exist_ok=True)
        with open(_MARKER_PATH, "w", encoding="utf-8") as fh:
            fh.write(_today())
    except Exception as exc:
        logger.warning("new_city marker yazilamadi: %s", exc)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def geocode_city(name: str) -> dict | None:
    """Open-Meteo geocoding: ilk yerlesim sonucu (lat/lon/ulke)."""
    try:
        r = requests.get(
            _GEO_URL,
            params={"name": name, "count": 3, "language": "en", "format": "json"},
            timeout=_TIMEOUT,
            proxies={"http": None, "https": None, "all": None},  # Poly disi: direkt (08-16 sizinti + ALL_PROXY)
        )
        if r.status_code != 200:
            return None
        results = (r.json() or {}).get("results") or []
        for res in results:
            try:
                return {
                    "name": res.get("name", name),
                    "country": res.get("country", ""),
                    "lat": float(res["latitude"]),
                    "lon": float(res["longitude"]),
                }
            except Exception:
                continue
    except Exception as exc:
        logger.warning("geocode hatasi %s: %s", name, exc)
    return None


def station_info(icao: str) -> dict | None:
    """aviationweather stationinfo: site adi + koordinat."""
    try:
        r = requests.get(
            _STATION_URL,
            params={"ids": icao, "format": "json"},
            timeout=_TIMEOUT,
            proxies={"http": None, "https": None, "all": None},  # Poly disi: direkt (08-16 sizinti + ALL_PROXY)
        )
        if r.status_code != 200:
            return None
        rows = r.json() or []
        if not rows:
            return None
        row = rows[0]
        return {
            "icao": str(row.get("station_id") or row.get("icaoId") or icao).upper(),
            "site": str(row.get("site") or row.get("stationName") or ""),
            "lat": float(row.get("lat") or row.get("latitude")),
            "lon": float(row.get("lon") or row.get("longitude")),
        }
    except Exception as exc:
        logger.warning("stationinfo hatasi %s: %s", icao, exc)
    return None


def _load_overrides() -> dict:
    from config.settings import CITY_OVERRIDES_PATH

    try:
        with open(CITY_OVERRIDES_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("aliases", {})
            data.setdefault("coords", {})
            return data
    except Exception:
        pass
    return {"aliases": {}, "coords": {}}


def _save_overrides(data: dict) -> None:
    from config.settings import CITY_OVERRIDES_PATH

    with open(CITY_OVERRIDES_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def discover_unknown_cities() -> list[dict]:
    """Bilinmeyen sehirli marketler -> [{city_token, icao}] (cozum istasyonlu)."""
    from scrapers.polymarket import PolymarketScraper

    scraper = PolymarketScraper()
    try:
        raw_markets = scraper._fetch_raw_markets() or []
    except Exception as exc:
        logger.warning("ham market cekilemedi: %s", exc)
        return []
    found: dict[str, str] = {}
    for raw in raw_markets:
        try:
            if scraper._extract_city(raw.get("question", "") or raw.get("title", "")):
                continue  # bilinen sehir
            icao = scraper._extract_resolution_icao(raw) or ""
            if not icao:
                continue
            title = (raw.get("title", "") or raw.get("question", "") or "").strip()
            if title and icao not in found.values():
                found[title] = icao
        except Exception:
            continue
    return [{"title": t, "icao": i} for t, i in found.items()]


def _city_token_from_title(title: str) -> str:
    """'Highest temperature in X on ...' -> X (kaba ama guvenli taraf)."""
    import re

    m = re.search(r"in\s+([A-Za-z][A-Za-z .'\-]{1,40}?)\s+on\s+", title, re.I)
    if m:
        return m.group(1).strip()
    # fallback: ilk 3 kelime disi (tarih/sicaklik icerenler atilir)
    words = [w for w in re.split(r"\s+", title) if w and not re.search(r"\d", w)]
    return " ".join(words[:3]).strip(" ,.-")


def run() -> dict:
    """Gunluk kesif: bilinmeyen sehir -> overrides.json. Idempotent."""
    from config.settings import get_live_city_map

    result: dict = {"checked": 0, "added": [], "skipped": []}
    live_map = get_live_city_map()
    candidates = discover_unknown_cities()
    result["checked"] = len(candidates)
    if not candidates:
        return result
    overrides = _load_overrides()
    for cand in candidates:
        title, icao = cand["title"], cand["icao"]
        token = _city_token_from_title(title).lower()
        if not token or token in live_map or token in overrides["aliases"]:
            result["skipped"].append(title[:60])
            continue
        st = station_info(icao)
        if not st or not st["site"]:
            result["skipped"].append(title[:60])
            continue
        geo = geocode_city(st["site"])
        if not geo or _haversine_km(geo["lat"], geo["lon"], st["lat"], st["lon"]) > 150:
            logger.warning("yeni sehir dogrulama basarisiz: %s (%s)", title[:60], icao)
            result["skipped"].append(title[:60])
            continue
        overrides["aliases"].setdefault(token, icao)
        overrides["aliases"].setdefault(st["site"].lower(), icao)
        overrides["coords"].setdefault(icao, [st["lat"], st["lon"]])
        result["added"].append({"city": token, "icao": icao, "station": st["site"]})
        logger.info("yeni sehir eklendi: %s -> %s (%s)", token, icao, st["site"])
    if result["added"]:
        _save_overrides(overrides)
        try:
            from utils.activity_log import log_event

            for a in result["added"]:
                log_event("new_city", a["city"], f"{a['icao']} ({a['station']}) oto-harita")
        except Exception:
            pass
    return result


def maybe_run() -> dict | None:
    if not should_run():
        return None
    try:
        out = run()
    except Exception as exc:
        logger.error("new_city job hatasi: %s", exc, exc_info=True)
        return {"error": str(exc)[:200]}
    _write_marker()
    return out


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(maybe_run(), indent=2, ensure_ascii=False))
