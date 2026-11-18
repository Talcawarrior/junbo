"""Gecmis METAR gozlemlerini aviationweather.gov'dan toplayip arsive ekler.

Sorun: metar_observations arsivi SADECE 2026-08-14'ten itibaren dolu (bot o
tarihte METAR arsivlemeye basladi). 1-15 Agu backtesti icin 06-13 Agu
METAR'larinin geriye donuk toplanmasi gerekir (peak-kilitlenmesi modeli
METAR gozlemlerine dayanir, look-ahead yok).

aviationweather.gov `/api/data/metar?date=<gun+1>&hours=24` bir ONCEKI gunun
tam gozlemlerini doner (dogrulandi: date=08-11 hours=24 -> 08-10 48 obs).
Bu script her (sehir, gun) icin tek istek atar ve `archive_metar_observations`
(uygulama katmani, idempotent) ile arsive yazar. Bot CALISIYOR olsa da guvenli:
mevcut kayitlar degismez, yalnizca gecmis gunlerin eksik gozlemleri eklenir.

Kullanim:
    python scripts/backfill_metar_history.py --start 2026-08-06 --end 2026-08-13
    python scripts/backfill_metar_history.py --start 2026-08-06 --end 2026-08-13 --limit 3
"""

from __future__ import annotations

import argparse
import os
import ssl
import sys
import time
from datetime import date as _date, datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_db_path  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

METAR_URL = "https://aviationweather.gov/api/data/metar"
REQUEST_TIMEOUT = 25

# Avast Web/Mail Shield TLS intercept: Windows sistem kok deposunu yukle
# (weather_ensemble.py ile ayni cozum), dogrulama ACIK kalir.
_SYSTEM_TLS = ssl.create_default_context()


class _SystemStoreAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):  # type: ignore[override]
        kwargs["ssl_context"] = _SYSTEM_TLS
        return super().init_poolmanager(*args, **kwargs)


_SESSION = requests.Session()
_SESSION.mount("https://", _SystemStoreAdapter())


def fetch_metar_day(icao: str, day: str) -> list[tuple[int, float]]:
    """Bir UTC gununun tum METAR gozlemleri. date=<gun+1> onceki gunu verir."""
    dt = _date.fromisoformat(day)
    nxt = (dt + timedelta(days=1)).isoformat()
    resp = _SESSION.get(
        METAR_URL,
        params={"ids": icao, "format": "json", "date": nxt, "hours": "24"},
        timeout=REQUEST_TIMEOUT,
        proxies={"http": None, "https": None, "all": None},  # type: ignore[dict-item]
    )
    resp.raise_for_status()
    rows = []
    for m in resp.json():
        obs = m.get("obsTime")
        temp = m.get("temp")
        if obs is None or temp is None:
            continue
        try:
            t = int(obs)
            if datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d") != day:
                continue
            rows.append((t, float(temp)))
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda x: x[0])
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Gecmis METAR backfill")
    parser.add_argument("--start", default="2026-08-06", help="ilk gun (UTC)")
    parser.add_argument("--end", default="2026-08-13", help="son gun (UTC)")
    parser.add_argument("--delay", type=float, default=0.2, help="istek arasi saniye")
    parser.add_argument("--limit", type=int, default=0, help="test: ilk N sehir")
    args = parser.parse_args()

    from database.db import get_session
    from database.models import MetarObservation

    start = _date.fromisoformat(args.start)
    end = _date.fromisoformat(args.end)
    days = []
    d = start
    while d <= end:
        days.append(d.isoformat())
        d += timedelta(days=1)

    with get_session() as session:
        codes = [
            r[0] for r in session.query(MetarObservation.city_code).distinct().order_by(MetarObservation.city_code)
        ]
    # Arsivde HIC gozlem olmayan sehirler de gerekli (backtest butun sehirleri
    # tarar) -> weather_markets'teki tum sehir kodlariyla birlestir.
    import sqlite3

    db = sqlite3.connect(get_db_path(), timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    mk = [
        r[0]
        for r in db.execute(
            "SELECT DISTINCT city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''"
        )
    ]
    db.close()
    all_codes = sorted(set(codes) | set(mk))
    if args.limit:
        all_codes = all_codes[: args.limit]

    added_total = 0
    seen_total = 0
    fail = 0
    total = len(all_codes) * len(days)
    done = 0
    for icao in all_codes:
        for day in days:
            try:
                rows = fetch_metar_day(icao, day)
                if rows:
                    from scrapers.metar import archive_metar_observations

                    added = archive_metar_observations(icao, icao, rows)
                    added_total += added
                    seen_total += len(rows)
            except Exception as exc:  # noqa: BLE001
                fail += 1
                if fail <= 5:
                    print(f"  FAIL {icao} {day}: {type(exc).__name__} {exc}")
            done += 1
            if done % 100 == 0:
                print(f"  [{done}/{total}] eklenen={added_total} toplam={seen_total} hata={fail}")
            time.sleep(args.delay)
    print(f"\nDONE: {done}/{total} istek, {added_total} yeni gozlem eklendi, {seen_total} gozlem toplandi, {fail} hata")
    return 0


if __name__ == "__main__":
    sys.exit(main())
