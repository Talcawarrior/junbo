"""Shared Open-Meteo cache reader/writer (C:/JUNBO data/ortak/db/om_cache.sqlite).

Heat + ASIAbot + junbo ayni IP'den ayni istekleri yapmasin diye konulan
ortak yanit deposu. Bu modul salt-okuyucu DEGIL — junbo da yazabilir.

Key generation: bolum §2.2'deki kuralla BIREBIR ayni (om_anahtar).
TTL: varsayilan 1 saat (3600 sn). Cagiran karar verir.

Dosya: C:/JUNBO data/ortak/db/om_cache.sqlite (OM_CACHE_DB env ile degistirilebilir;
2026-11-18: runner'in start-runner.ps1 OM_CACHE_DB yolu ile ayni -- onceki D:
varsayilani runner tasindiktan sonra ikinci, bayat bir onbellek olusturuyordu).
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

OM_CACHE_DB = os.environ.get("OM_CACHE_DB", r"C:\JUNBO data\ortak\db\om_cache.sqlite")

# Shared request shape — junbo, Heat, ASIAbot hepsi ayni URL'i sorar.
# models alfabetik sirali, virgulle, bosluksuz.
SHARED_MODELS = ",".join(
    sorted(
        [
            "cma_grapes_global",
            "ecmwf_ifs025",
            "gem_global",
            "gfs_seamless",
            "icon_global",
            "jma_seamless",
            "meteofrance_seamless",
            "ukmo_seamless",
        ]
    )
)

SHARED_DAILY = "temperature_2m_max,temperature_2m_min"


def _om_key(url: str) -> str:
    """§2.2 — Heat'in omAnahtar() ile BIREBIR ayni.

    Python paritesi notlari:
    - urllib.parse.parse_qsl (keep_blank_values=True) == JS URLSearchParams
    - hostname lower(), port/netloc dahil DEGIL
    - latitude/longitude %.2f ile yuvarlanir
    - apikey/key cifti atilir
    - kalan ciftler (ad, deger) ASCII sirali
    - sha256 hex
    """
    u = urllib.parse.urlsplit(url)
    ciftler: list[tuple[str, str]] = []
    for ad, deger in urllib.parse.parse_qsl(u.query, keep_blank_values=True):
        ad = ad.lower()
        if ad in ("apikey", "key"):
            continue
        if ad in ("latitude", "longitude"):
            try:
                deger = f"{float(deger):.2f}"
            except ValueError:
                pass
        ciftler.append((ad, deger))
    ciftler.sort()
    sorgu = "&".join(f"{a}={d}" for a, d in ciftler)
    metin = f"{u.hostname.lower()}{u.path}?{sorgu}"
    return hashlib.sha256(metin.encode("utf-8")).hexdigest()


def _db_con(readonly: bool = True) -> sqlite3.Connection | None:
    """Open DB connection. None if file missing or DB locked."""
    if not os.path.exists(OM_CACHE_DB):
        return None
    try:
        uri = f"file:{OM_CACHE_DB}"
        if readonly:
            uri += "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=5)
        if not readonly:
            con.execute("PRAGMA busy_timeout = 5000")
        return con
    except Exception as e:
        logger.debug("om_cache DB acilamadi: %s", e)
        return None


def om_get(url: str, max_age_s: int = 3600) -> tuple[dict | None, str | None]:
    """Onbellekten oku. Taze ise (data, fetched_at_utc) doner; degilse (None, None).

    fetched_at_utc: verinin GERCEK cekim anidir (ilk ag cekimi). Cagiran
    bunu run_at_utc olarak kullanmali, datetime.now() degil (§3, kural 2).
    """
    con = _db_con(readonly=True)
    if con is None:
        return None, None
    try:
        key = _om_key(url)
        row = con.execute(
            "SELECT payload, fetched_at_utc, status_code FROM om_cache WHERE key = ?",
            (key,),
        ).fetchone()
    finally:
        con.close()

    if not row:
        return None, None

    payload, fetched_at, status = row
    if status != 200:
        return None, None

    # Yas kontrolu
    try:
        ts = datetime.fromisoformat(fetched_at.replace("Z", "+00:00")).timestamp()
        yas = time.time() - ts
        if yas > max_age_s:
            return None, None
    except Exception:
        return None, None

    try:
        data = gzip.decompress(payload)
        return __import__("json").loads(data), fetched_at
    except Exception as e:
        logger.debug("om_cache gzip/json hatasi: %s", e)
        return None, None


def _parse_url_meta(url: str) -> tuple[str, str, float, float]:
    """URL'den aile, model, lat_r, lon_r cikar (Heat raporu §5.7 bug 4)."""
    u = urllib.parse.urlsplit(url)
    # aile: hostname'den turet
    host = u.hostname or ""
    if host.startswith("ensemble-api."):
        aile = "ensemble"
    elif host.startswith("archive-api."):
        aile = "archive"
    elif host.startswith("api."):
        aile = "forecast"
    else:
        aile = "diger"
    # lat/lon/model: query'den cek
    lat_r = 0.0
    lon_r = 0.0
    model = ""
    for ad, deger in urllib.parse.parse_qsl(u.query, keep_blank_values=True):
        ad = ad.lower()
        if ad == "latitude":
            try:
                lat_r = round(float(deger), 2)
            except ValueError:
                pass
        elif ad == "longitude":
            try:
                lon_r = round(float(deger), 2)
            except ValueError:
                pass
        elif ad == "models":
            model = deger
    return aile, model, lat_r, lon_r


def om_put(url: str, data: dict, fetched_at_utc: str, kaynak: str = "junbo") -> None:
    """Yaniti onbellege yaz (gzip, status=200 varsayilir).

    kaynak: yazan proje adi (Heat/ASIAbot/junbo).
    fetched_at_utc: GERCEK ag cekim zamani (datetime.now() degil).
    Hit/son_hit_utc korunur (INSERT OR REPLACE yerine ON CONFLICT DO UPDATE).
    """
    con = _db_con(readonly=False)
    if con is None:
        return
    try:
        key = _om_key(url)
        raw = __import__("json").dumps(data, separators=(",", ":")).encode("utf-8")
        compressed = gzip.compress(raw)
        now = fetched_at_utc or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        aile, model, lat_r, lon_r = _parse_url_meta(url)
        con.execute(
            """INSERT INTO om_cache
               (key, url, aile, model, lat_r, lon_r, fetched_at_utc,
                status_code, payload, payload_bayt, hit, son_hit_utc, kaynak)
               VALUES (?, ?, ?, ?, ?, ?, ?, 200, ?, ?, 0, NULL, ?)
               ON CONFLICT(key) DO UPDATE SET
                 url=excluded.url, aile=excluded.aile, model=excluded.model,
                 lat_r=excluded.lat_r, lon_r=excluded.lon_r,
                 fetched_at_utc=excluded.fetched_at_utc, status_code=excluded.status_code,
                 payload=excluded.payload, payload_bayt=excluded.payload_bayt,
                 kaynak=excluded.kaynak""",
            (key, url, aile, model, lat_r, lon_r, now, compressed, len(raw), kaynak),
        )
        # gunluk sayac
        gun = now[:10]
        con.execute(
            "INSERT INTO om_cache_gun (gun, kurtarilan, yazilan) VALUES (?, 0, 1) "
            "ON CONFLICT(gun) DO UPDATE SET yazilan = yazilan + 1",
            (gun,),
        )
        con.commit()
        logger.debug("om_cache yazildi: %s (%d bayt -> %d bayt)", key, len(raw), len(compressed))
    except Exception as e:
        logger.debug("om_cache yazma hatasi: %s", e)
    finally:
        con.close()


def build_om_url(
    lat: float,
    lon: float,
    *,
    forecast_days: int = 14,
    models: str | None = None,
    daily: str = SHARED_DAILY,
) -> str:
    """Ortak sekilde Open-Meteo URL'i olustur.

    Ornek:
        https://api.open-meteo.com/v1/forecast
          ?latitude=41.28&longitude=28.75
          &daily=temperature_2m_max,temperature_2m_min
          &forecast_days=14
          &models=cma_grapes_global,ecmwf_ifs025,...
          &timezone=auto
    """
    params = {
        "latitude": f"{lat:.2f}",
        "longitude": f"{lon:.2f}",
        "daily": daily,
        "forecast_days": str(forecast_days),
        "models": models or SHARED_MODELS,
        "timezone": "auto",
    }
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return f"https://api.open-meteo.com/v1/forecast?{qs}"
