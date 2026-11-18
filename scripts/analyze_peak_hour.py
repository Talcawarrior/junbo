"""ISTATISTIKSEL PEAK SAATI MODELI (2026-08-19, kullanici sorusu).

"Her havaalaninin istatistiksel olarak en yuksek sicaklik saatleri yaz/kiş ayni
olur; o saate vardigimizda sicakliga bet acsak sansimiz artmaz mi?"

Open-Meteo arsiv API'sinden (saatlik, son 52 hafta) her sehrin GUNLUK MAX
sicakliginin yerel saate gore dagilimi cikarilir:
  1) mod saat (max'in en sik olustugu saat), median, P25-P75
  2) yaz (Haz-Agu) vs kis (Ara-Sub) karsilastirmasi
  3) KURAL TESTI: mod saatte bet acsak (o anki sicakligin bucket'ina)
     bucket isabeti ne olur? (bak: o saatte sicaklik zaten dustuyse isabet duser)
  4) kumulatif: yerel saate gore max'in %90'inin gerceklestigi saat

NOT: Open-Meteo sehrin grid noktasidir, havalimani degil — ama PEAK SAATI
guneslenme rejimiyle belirlenir, sehir/havalimani arasinda degismez (max
DEGERI farkli olabilir, SAATI ayni). Saat istatistigi gecerlidir.

Kullanim: python -m scripts.analyze_peak_hour [--refresh]
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from database.db import get_session
from database.models import WeatherMarket

# Avast TLS intercept: certifi degil Windows sistem store (metar.py cozumu)
from scrapers.metar import _SESSION as _HTTP, city_utc_offset

_CACHE = "data/om_hourly_52w.json"
_CHUNK_DAYS = 92


def _fetch(city: str, lat: float, lon: float, start: str, end: str) -> list[tuple[float, float]]:
    """Open-Meteo arsiv: [(epoch_utc, temp_c), ...] saatlik."""
    r = _HTTP.get(
        "https://archive-api.open-meteo.com/v1/archive",
        params={
            "latitude": lat,
            "longitude": lon,
            "start_date": start,
            "end_date": end,
            "hourly": "temperature_2m",
            "timezone": "UTC",
        },
        timeout=60,
        proxies={"http": None, "https": None, "all": None},
    )
    r.raise_for_status()
    d = r.json()["hourly"]
    out = []
    for t, v in zip(d["time"], d["temperature_2m"]):
        if v is None:
            continue
        out.append((float(datetime.fromisoformat(t).timestamp()), float(v)))
    return out


def main() -> int:
    cities: list[tuple[str, float, float]] = []
    with get_session() as session:
        seen = set()
        for r in session.query(WeatherMarket).all():
            if r.city and r.latitude and r.city not in seen:
                seen.add(str(r.city))
                cities.append((str(r.city), float(r.latitude), float(r.longitude)))
            if r.city and r.city_code:
                _CITY_CODE_BY_NAME[str(r.city)] = str(r.city_code)

    cache: dict[str, list[tuple[float, float]]] = {}
    if os.path.exists(_CACHE) and "--refresh" not in sys.argv:
        with open(_CACHE, encoding="utf-8") as f:
            cache = json.load(f)

    today = datetime.now(timezone.utc).date()
    chunks = []
    for i in range(4):
        end = today - timedelta(days=i * _CHUNK_DAYS)
        start = end - timedelta(days=_CHUNK_DAYS - 1)
        chunks.append((start.isoformat(), end.isoformat()))

    need = [c for c in cities if c[0] not in cache]
    if need:
        print(f"fetch: {len(need)} sehir x {len(chunks)} chunk (Open-Meteo arsiv)...")
        for n, (city, lat, lon) in enumerate(need, 1):
            rows: list[tuple[float, float]] = []
            for start, end in chunks:
                for attempt in range(3):
                    try:
                        rows += _fetch(city, lat, lon, start, end)
                        break
                    except Exception as exc:  # noqa: BLE001
                        if attempt == 2:
                            print(f"  FAIL {city}: {type(exc).__name__} {str(exc)[:80]}")
                        else:
                            time.sleep(2 * (attempt + 1))
            rows.sort()
            cache[city] = rows
            if n % 10 == 0:
                with open(_CACHE, "w", encoding="utf-8") as f:
                    json.dump(cache, f)
                print(f"  [{n}/{len(need)}]")
        with open(_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f)

    # ---- istatistik ----
    print()
    print(
        f"{'sehir':16s} {'n(gun)':>6s} {'mod':>5s} {'medyan':>6s} {'P25-75':>10s} "
        f"{'mod@isabet':>9s} {'bucket@mod':>11s} {'%90 saat':>8s} {'yaz/kis':>10s}"
    )
    overall_hits = overall_n = 0
    overall_r2_hit = overall_r2_n = 0
    all_hrs: list[tuple[str, int, int, float]] = []
    for city, _lat, _lon in cities:
        rows = cache.get(city) or []
        if not rows:
            continue
        off = city_utc_offset(_CITY_CODE_BY_NAME.get(city, ""), today.isoformat(), None)
        # gunluk max'in yerel saati
        day_max_hour: list[tuple[str, int, float, float]] = []  # (tarih, yerel saat, o saat temp, gunluk max)
        by_day: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for e, t in rows:
            lt = datetime.fromtimestamp(e + off * 3600, tz=timezone.utc)
            by_day[lt.strftime("%Y-%m-%d")].append((e, t))
        for d, rr in by_day.items():
            if len(rr) < 20:
                continue  # eksik gun atla
            mx = max(t for _, t in rr)
            mx_hr = None
            mx_temp_at_hr = None
            for e, t in rr:
                lt = datetime.fromtimestamp(e + off * 3600, tz=timezone.utc)
                if t == mx and mx_hr is None:
                    mx_hr = lt.hour
                    mx_temp_at_hr = t
            if mx_hr is not None:
                day_max_hour.append((d, mx_hr, mx_temp_at_hr or 0.0, mx))
        if len(day_max_hour) < 30:
            continue
        hrs = [h for _, h, _, _ in day_max_hour]
        hrs_sorted = sorted(hrs)
        mode_h = max(set(hrs), key=hrs.count)
        med = hrs_sorted[len(hrs_sorted) // 2]
        p25 = hrs_sorted[len(hrs_sorted) // 4]
        p75 = hrs_sorted[3 * len(hrs_sorted) // 4]
        # GERCEK KURAL TESTI: HER GUN mod saatteki sicakligin bucket'ina bet
        # ac (o gun peak mod saatte olmasa bile) — kac gun tutar?
        # (her gun icin mod saatteki sicaklik ayri diziden okunur)
        t_at_mod: list[tuple[str, float, float]] = []  # (tarih, mod saat temp, gunluk max)
        for d, rr in by_day.items():
            if len(rr) < 20:
                continue
            mx = max(t for _, t in rr)
            found = None
            for e, t in rr:
                lt = datetime.fromtimestamp(e + off * 3600, tz=timezone.utc)
                if lt.hour == mode_h:
                    found = t
            if found is not None:
                t_at_mod.append((d, found, mx))
        bucket_hit = sum(1 for d, t, m in t_at_mod if int(t + 0.5) == int(m + 0.5))
        rule_n = len(t_at_mod)
        # R2 (canli gercekci): mod saatte o ana kadarki KUMULATIF max'in
        # bucket'ina bet ac — tepe daha erken olduysa onu biliriz.
        r2_hit = r2_n = 0
        for d, rr in by_day.items():
            if len(rr) < 20:
                continue
            mx = max(t for _, t in rr)
            run = -999.0
            for e, t in rr:
                lt = datetime.fromtimestamp(e + off * 3600, tz=timezone.utc)
                if lt.hour > mode_h:
                    break
                run = max(run, t)
            if run > -999.0:
                r2_n += 1
                if int(run + 0.5) == int(mx + 0.5):
                    r2_hit += 1
        # %90 kumulatif saat
        cum = sorted(hrs)
        h90 = cum[int(len(cum) * 0.9)]

        # yaz/kis mod
        def _mode(hs):
            return max(set(hs), key=hs.count) if hs else -1

        summer = [h for d, h, _, _ in day_max_hour if d[5:7] in ("06", "07", "08")]
        winter = [h for d, h, _, _ in day_max_hour if d[5:7] in ("12", "01", "02")]
        overall_hits += bucket_hit
        overall_n += rule_n
        overall_r2_hit += r2_hit
        overall_r2_n += r2_n
        all_hrs.append((city, mode_h, med, h90))
        print(
            f"{city:16s} {len(day_max_hour):6d} {mode_h:5d} {med:6d} "
            f"{p25}-{p75:2d} {'':>9s} "
            f"{bucket_hit / rule_n * 100 if rule_n else 0:10.1f}% "
            f"{h90:8d} {_mode(summer)}/{_mode(winter):2d}"
        )
    if overall_n:
        print()
        print(
            f"TOPLAM R1 (mod saatte O ANKI sicakliga bet): {overall_hits}/{overall_n} = "
            f"{overall_hits / overall_n * 100:.1f}%"
        )
    if overall_r2_n:
        print(
            f"TOPLAM R2 (mod saatte KUMULATIF max'a bet — canli gercekci): "
            f"{overall_r2_hit}/{overall_r2_n} = {overall_r2_hit / overall_r2_n * 100:.1f}%"
        )
    return 0


# ICAO haritasi (peak saati icin yalnizca offset gerekli; lon fallback kullanilir)
_CITY_CODE_BY_NAME: dict[str, str] = {}


if __name__ == "__main__":
    raise SystemExit(main())
