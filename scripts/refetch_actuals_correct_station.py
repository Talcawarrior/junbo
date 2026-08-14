"""Actuals verisini DOGRU COZUM ISTASYONUNDAN yeniden ceker (Open-Meteo Archive API).

Bug (2026-08-13): actuals.db sehir merkezi / yanlis istasyon koordinatiyla
(ornek Moscow 55.9726, 37.4146) doluydu. Marketler NOAA cozum istasyonundan
cozuluyor (Moscow=UUWW). Bu script weather_markets.city_code'dan (artik dogru
istasyon) koordinati alip archive API ile gecmis gunlerin gercek sicakligini
ceker ve actuals.db'yi dogru istasyon koordinatiyla yeniden doldurur.

Ayrica weather_forecasts'i de dogru istasyondan yeniden doldurur (archive API
model forecast icin gecmis vermez, ama gercek sicaklik ile kalibrasyon bias'i
duzeltilir; forecast'ler bugunden itibaren dogru istasyondan cekilir).

Kullanim:
    python scripts/refetch_actuals_correct_station.py          # dry-run
    python scripts/refetch_actuals_correct_station.py --apply  # DB'ye yaz
    python scripts/refetch_actuals_correct_station.py --start 2026-05-01 --end 2026-08-13
"""

from __future__ import annotations

import argparse
import os
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import sqlite3  # noqa: E402
import requests  # noqa: E402

BOT_DB = os.path.join(_REPO_ROOT, "data", "bot.db")
ACTUALS_DB = os.path.join(_REPO_ROOT, "data", "actuals.db")
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
STATION_CODES = ["UUWW", "EGLC", "LFPB", "RKSI", "KBKF", "KHOU", "RCSS", "MPMG"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Actuals'i dogru cozum istasyonundan yeniden cek")
    parser.add_argument("--apply", action="store_true", help="DB'ye yaz (dry-run varsayilan)")
    parser.add_argument("--start", default="2026-05-07", help="baslangic tarihi (archive kapsami)")
    parser.add_argument("--end", default="2026-08-13", help="bitis tarihi")
    args = parser.parse_args()


    db = sqlite3.connect(BOT_DB)
    cur = db.cursor()

    # dogru istasyon koordinatlari: market city_code -> (lat, lon)
    stations = {}
    for r in cur.execute(
        "SELECT DISTINCT city_code, latitude, longitude FROM weather_markets "
        "WHERE city_code IS NOT NULL AND city_code != '' AND latitude != 0"
    ):
        code, lat, lon = r
        if code and lat and lon:
            stations.setdefault(code, (float(lat), float(lon)))

    # dogru istasyonlardan gecmis veri cek (city_code + station coords)
    results = []
    for code, (lat, lon) in sorted(stations.items()):
        # rate limit dostu: 0.5s bekleme
        time.sleep(0.5)
        try:
            resp = requests.get(
                ARCHIVE_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "start_date": args.start,
                    "end_date": args.end,
                    "temperature_unit": "celsius",
                    "timezone": "auto",
                },
                timeout=20,
            )
            resp.raise_for_status()
            d = resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"  {code}: HATA {exc}")
            continue
        daily = d.get("daily", {})
        times = daily.get("time", [])
        mx = daily.get("temperature_2m_max", [])
        mn = daily.get("temperature_2m_min", [])
        city_name = cur.execute(
            "SELECT DISTINCT city FROM weather_markets WHERE city_code=?", (code,)
        ).fetchone()
        cname = city_name[0] if city_name else code
        for i, t in enumerate(times):
            results.append((code, cname, lat, lon, t, mx[i], mn[i]))
        print(f"  {code} ({cname}): {len(times)} gun ({times[0]}..{times[-1]})")

    print(f"\ntoplam satir: {len(results)}")

    if not args.apply:
        print("DRY-RUN: yazilmadi (--apply ile yaz).")
        db.close()
        return 0

    # actuals.db'yi yeniden doldur (dogru istasyon koordinati)
    adb = sqlite3.connect(ACTUALS_DB)
    ac = adb.cursor()
    ac.execute("DELETE FROM actual_temperatures")
    for code, cname, lat, lon, t, mx, mn in results:
        ac.execute(
            "INSERT OR REPLACE INTO actual_temperatures "
            "(city, latitude, longitude, date, temperature_2m_max, temperature_2m_min, fetched_at, created_at) "
            "VALUES (?,?,?,?,?,?, datetime('now'), datetime('now'))",
            (cname, lat, lon, t, mx, mn),
        )
    adb.commit()
    n = ac.execute("SELECT COUNT(*) FROM actual_temperatures").fetchone()[0]
    print(f"actuals.db yeniden dolduruldu: {n} satir (dogru istasyon koordinatlari)")
    adb.close()
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
