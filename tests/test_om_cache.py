"""Tests for utils/om_cache.py — shared Open-Meteo cache."""

import hashlib
import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch


def _expected_key(url: str) -> str:
    """Independent key generation matching §2.2 rules (for test verification)."""
    import urllib.parse

    u = urllib.parse.urlsplit(url)
    ciftler = []
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


class TestOmKey:
    """§2.2 anahtar kurallari dogrulugu."""

    def test_basic_url(self):
        from utils.om_cache import _om_key

        url = "https://api.open-meteo.com/v1/forecast?latitude=41.28&longitude=28.75&daily=temperature_2m_max&forecast_days=14&models=gfs_seamless&timezone=auto"
        assert _om_key(url) == _expected_key(url)

    def test_latitude_longitude_rounded(self):
        from utils.om_cache import _om_key

        # 41.28499 -> 41.28 (round to 2 decimal)
        url1 = "https://api.open-meteo.com/v1/forecast?latitude=41.28&longitude=28.75&daily=temperature_2m_max&forecast_days=14&models=gfs_seamless&timezone=auto"
        url2 = "https://api.open-meteo.com/v1/forecast?latitude=41.28499&longitude=28.75123&daily=temperature_2m_max&forecast_days=14&models=gfs_seamless&timezone=auto"
        assert _om_key(url1) == _om_key(url2)

    def test_apikey_removed(self):
        from utils.om_cache import _om_key

        url1 = "https://api.open-meteo.com/v1/forecast?latitude=41.28&longitude=28.75&daily=temperature_2m_max&forecast_days=14&models=gfs_seamless&timezone=auto"
        url2 = "https://api.open-meteo.com/v1/forecast?latitude=41.28&longitude=28.75&daily=temperature_2m_max&forecast_days=14&models=gfs_seamless&timezone=auto&apikey=secret"
        assert _om_key(url1) == _om_key(url2)

    def test_models_order_matters(self):
        from utils.om_cache import _om_key

        url1 = "https://api.open-meteo.com/v1/forecast?latitude=41.28&longitude=28.75&daily=temperature_2m_max&forecast_days=14&models=a,b&timezone=auto"
        url2 = "https://api.open-meteo.com/v1/forecast?latitude=41.28&longitude=28.75&daily=temperature_2m_max&forecast_days=14&models=b,a&timezone=auto"
        assert _om_key(url1) != _om_key(url2)

    def test_hostname_lowercase_only(self):
        from utils.om_cache import _om_key

        # Port/netloc excluded — :443 in URL should NOT affect the key
        url1 = "https://api.open-meteo.com/v1/forecast?latitude=41.28&longitude=28.75&daily=t&forecast_days=14&m=g&timezone=auto"
        url2 = "https://api.open-meteo.com:443/v1/forecast?latitude=41.28&longitude=28.75&daily=t&forecast_days=14&m=g&timezone=auto"
        assert _om_key(url1) == _om_key(url2)
        # :443 should NOT be in key
        assert "443" not in url1.split("?")[0].lower()

    def test_params_sorted_ascii(self):
        from utils.om_cache import _om_key

        # "b=1&a=2" and "a=2&b=1" should produce same key (sorted)
        url1 = "https://api.open-meteo.com/v1/forecast?b=1&a=2&latitude=41.28&longitude=28.75"
        url2 = "https://api.open-meteo.com/v1/forecast?a=2&b=1&latitude=41.28&longitude=28.75"
        assert _om_key(url1) == _om_key(url2)


class TestBuildOmUrl:
    """Ortak URL olusturma."""

    def test_default_params(self):
        from utils.om_cache import SHARED_DAILY, SHARED_MODELS, build_om_url

        url = build_om_url(41.28, 28.75)
        assert "latitude=41.28" in url
        assert "longitude=28.75" in url
        assert f"daily={SHARED_DAILY}" in url
        assert "forecast_days=14" in url
        assert f"models={SHARED_MODELS}" in url
        assert "timezone=auto" in url
        assert "start_date" not in url  # ortak format kullanmaz
        assert "temperature_unit" not in url

    def test_models_alphabetically_sorted(self):
        from utils.om_cache import SHARED_MODELS

        models = SHARED_MODELS.split(",")
        assert models == sorted(models)

    def test_custom_models(self):
        from utils.om_cache import build_om_url

        url = build_om_url(41.28, 28.75, models="gfs_seamless,ecmwf_ifs025")
        assert "models=gfs_seamless,ecmwf_ifs025" in url


class TestOmCacheRoundtrip:
    """Cache yazma-okuma roundtrip (gecici DB)."""

    def _make_temp_db(self, tmp_path):
        """Create a temp DB with the om_cache schema."""
        db_path = str(tmp_path / "om_cache.sqlite")
        con = sqlite3.connect(db_path)
        con.execute("""
            CREATE TABLE om_cache (
                key TEXT PRIMARY KEY,
                url TEXT, aile TEXT, model TEXT,
                lat_r REAL, lon_r REAL,
                fetched_at_utc TEXT, status_code INTEGER,
                payload BLOB, payload_bayt INTEGER,
                hit INTEGER, son_hit_utc TEXT, kaynak TEXT
            )
        """)
        con.execute("""
            CREATE TABLE om_cache_gun (
                gun TEXT PRIMARY KEY,
                kurtarilan INTEGER, yazilan INTEGER
            )
        """)
        con.commit()
        con.close()
        return db_path

    def test_put_and_get(self, tmp_path):
        from utils.om_cache import om_get, om_put

        db_path = self._make_temp_db(tmp_path)
        url = "https://api.open-meteo.com/v1/forecast?latitude=41.28&longitude=28.75&daily=temperature_2m_max,temperature_2m_min&forecast_days=14&models=cma_grapes_global,ecmwf_ifs025,gem_global,gfs_seamless,icon_global,jma_seamless,meteofrance_seamless,ukmo_seamless&timezone=auto"
        data = {"daily": {"time": ["2026-09-20"], "temperature_2m_max": [25.0]}}
        # Use current time so TTL check passes
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        with patch("utils.om_cache.OM_CACHE_DB", db_path):
            om_put(url, data, now, kaynak="junbo")
            result, result_at = om_get(url, max_age_s=3600)

        assert result is not None
        assert result["daily"]["temperature_2m_max"][0] == 25.0
        assert result_at == now

    def test_cache_miss_expired(self, tmp_path):
        from utils.om_cache import om_get, om_put

        db_path = self._make_temp_db(tmp_path)
        url = "https://api.open-meteo.com/v1/forecast?latitude=41.28&longitude=28.75&daily=temperature_2m_max,temperature_2m_min&forecast_days=14&models=cma_grapes_global,ecmwf_ifs025,gem_global,gfs_seamless,icon_global,jma_seamless,meteofrance_seamless,ukmo_seamless&timezone=auto"
        data = {"daily": {"time": ["2026-09-20"]}}
        # Fetch 2 hours ago
        old_time = "2026-09-20T10:00:00Z"

        with patch("utils.om_cache.OM_CACHE_DB", db_path):
            om_put(url, data, old_time, kaynak="junbo")
            # TTL 1 hour -> should be expired
            result, _ = om_get(url, max_age_s=3600)

        assert result is None

    def test_cache_miss_not_found(self, tmp_path):
        from utils.om_cache import om_get

        db_path = self._make_temp_db(tmp_path)
        url = "https://api.open-meteo.com/v1/forecast?latitude=99.99&longitude=99.99&daily=t&forecast_days=14&m=g&timezone=auto"

        with patch("utils.om_cache.OM_CACHE_DB", db_path):
            result, _ = om_get(url)

        assert result is None

    def test_gzip_roundtrip(self, tmp_path):
        """Verify gzip compress/decompress works correctly."""
        from utils.om_cache import om_get, om_put

        db_path = self._make_temp_db(tmp_path)
        url = "https://api.open-meteo.com/v1/forecast?latitude=41.28&longitude=28.75&daily=temperature_2m_max,temperature_2m_min&forecast_days=14&models=cma_grapes_global,ecmwf_ifs025,gem_global,gfs_seamless,icon_global,jma_seamless,meteofrance_seamless,ukmo_seamless&timezone=auto"
        # Large data to test gzip
        data = {
            "daily": {
                "time": [f"2026-09-{i:02d}" for i in range(1, 15)],
                "temperature_2m_max": [20.0 + i for i in range(14)],
            }
        }
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        with patch("utils.om_cache.OM_CACHE_DB", db_path):
            om_put(url, data, now, kaynak="junbo")
            result, _ = om_get(url, max_age_s=3600)

        assert result is not None
        assert len(result["daily"]["time"]) == 14
        assert result["daily"]["temperature_2m_max"][0] == 20.0

    def test_db_missing_returns_none(self):
        """If DB file doesn't exist, om_get returns (None, None)."""
        from utils.om_cache import om_get

        with patch("utils.om_cache.OM_CACHE_DB", r"D:\nonexistent\path.sqlite"):
            result, _ = om_get("https://example.com?x=1")

        assert result is None
