"""weather_forecasts tablosunu DOGRU COZUM ISTASYONUNDAN yeniden doldurur.

Bug (2026-08-13): forecast'ler EGLL/LFPG/RKSS/UUEE (yanlis istasyon) ile kayitli,
market'ler EGLC/LFPB/RKSI/UUWW (dogru istasyon) ile cozuluyor. Bu script
Open-Meteo archive API'den (model ensemble) dogru istasyon koordinatinda
gecmis forecast'leri ceker ve weather_forecasts'i yeniden doldurur.

Kullanim:
    python scripts/refetch_forecasts_correct_station.py          # dry-run
    python scripts/refetch_forecasts_correct_station.py --apply  # DB'ye yaz
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import sqlite3  # noqa: E402
import requests  # noqa: E402

BOT_DB = os.path.join(_REPO_ROOT, "data", "bot.db")
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# Open-Meteo model adi -> bot source adi (weather_forecasts.source)
MODEL_SOURCES = {
    "ecmwf_ifs025": "ecmwf_ifs025",
    "gfs_seamless": "gfs_seamless",
    "icon_global": "icon_global",
    "gem_global": "gem_global",
    "jma_seamless": "jma_seamless",
    "cma_grapes_global": "cma_grapes_global",
    "ukmo_seamless": "ukmo_seamless",
    "meteofrance_seamless": "meteofrance_seamless",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Forecast'leri dogru istasyondan yeniden cek")
    parser.add_argument("--apply", action="store_true", help="DB'ye yaz (dry-run varsayilan)")
    parser.add_argument("--start", default="2026-08-03", help="baslangic tarihi")
    parser.add_argument("--end", default=None, help="bitis tarihi (default bugun)")
    args = parser.parse_args()
    if args.end is None:
        args.end = datetime.now().strftime("%Y-%m-%d")

    db = sqlite3.connect(BOT_DB)
    cur = db.cursor()

    # dogru istasyon koordinatlari
    stations = {}
    for r in cur.execute(
        "SELECT DISTINCT city_code, latitude, longitude FROM weather_markets "
        "WHERE city_code IS NOT NULL AND city_code != '' AND latitude != 0"
    ):
        code, lat, lon = r
        if code and lat and lon:
            stations.setdefault(code, (float(lat), float(lon)))

    results = []
    for code, (lat, lon) in sorted(stations.items()):
        time.sleep(0.5)
        try:
            resp = requests.get(
                ARCHIVE_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "daily": "temperature_2m_max",
                    "start_date": args.start,
                    "end_date": args.end,
                    "temperature_unit": "celsius",
                    "timezone": "auto",
                    "models": ",".join(MODEL_SOURCES.keys()),
                },
                timeout=25,
            )
            resp.raise_for_status()
            d = resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"  {code}: HATA {exc}")
            continue
        daily = d.get("daily", {})
        times = daily.get("time", [])
        city_name = cur.execute(
            "SELECT DISTINCT city FROM weather_markets WHERE city_code=?", (code,)
        ).fetchone()
        cname = city_name[0] if city_name else code
        for i, t in enumerate(times):
            for model, source in MODEL_SOURCES.items():
                key = f"temperature_2m_max_{model}"
                vals = daily.get(key, [])
                if i < len(vals) and vals[i] is not None:
                    results.append((code, cname, t, source, float(vals[i])))
        print(f"  {code} ({cname}): {len(times)} gun, {len(results)} satir")

    print(f"\ntoplam forecast satiri: {len(results)}")

    if not args.apply:
        print("DRY-RUN: yazilmadi (--apply ile yaz).")
        db.close()
        return 0

    # weather_forecasts'i yeniden doldur (dogru istasyon)
    cur.execute("DELETE FROM weather_forecasts")
    for code, cname, t, source, pval in results:
        lat, lon = stations[code]
        cur.execute(
            "INSERT INTO weather_forecasts "
            "(city, lat, lon, target_date, metric, source, predicted_value, model_weight, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,1.0, datetime('now'))",
            (code, lat, lon, t + " 00:00:00", "temperature_max", source, pval),
        )
    db.commit()
    n = cur.execute("SELECT COUNT(*) FROM weather_forecasts").fetchone()[0]
    print(f"weather_forecasts yeniden dolduruldu: {n} satir (dogru istasyon)")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
