"""Mevcut weather_markets.city_code'u resolutionSource'taki gercek cozum
istasyonuna gunceller.

Bug (2026-08-13): marketler UUWW/EGLC/LFPB gibi cozum istasyonlarindan
cozuluyor ama bot city_code (UUEE/EGLL/LFPG) ile meteo verisi cekiyordu.
Bu script mevcut marketlerin city_code + lat/lon'unu resolutionSource'taki
ICAO'ya esitler (config.ICAO_COORDS'ta varsa).

Kullanim:
    python scripts/fix_city_code_from_resolution.py          # dry-run
    python scripts/fix_city_code_from_resolution.py --apply  # DB'ye yaz
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import sqlite3  # noqa: E402

BOT_DB = os.path.join(_REPO_ROOT, "data", "bot.db")


def _rs_icao(raw_data: str) -> str:
    try:
        d = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
    except (json.JSONDecodeError, TypeError):
        return ""
    # 1) resolutionSource URL'indeki ICAO (orn. .../moscow/UUWW)
    rs = d.get("resolutionSource") or d.get("resolution_source") or ""
    if rs:
        parts = str(rs).rstrip("/").split("/")
        last = parts[-1] if parts else ""
        if len(last) == 4 and last.isalpha() and last.isupper():
            return last
    # 2) description'daki 'site=UUWW' (NOAA timeseries kaynagi)
    import re

    desc = d.get("description") or ""
    m = re.search(r"site=([A-Z]{4})", desc)
    if m:
        return m.group(1)
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="city_code'u cozum istasyonuna guncelle")
    parser.add_argument("--apply", action="store_true", help="DB'ye yaz (varsayilan dry-run)")
    args = parser.parse_args()

    from config.settings import config

    conn = sqlite3.connect(BOT_DB)
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT id, city_code, latitude, longitude, raw_data FROM weather_markets WHERE raw_data IS NOT NULL"
    ).fetchall()

    changed = 0
    examples = []
    for mid, code, lat, lon, raw in rows:
        icao = _rs_icao(raw)
        if not icao or icao == code:
            continue
        if icao not in config.ICAO_COORDS:
            continue
        new_lat, new_lon = config.ICAO_COORDS[icao]
        changed += 1
        if len(examples) < 10:
            examples.append((mid, code, icao, lat, lon, new_lat, new_lon))
        if args.apply:
            cur.execute(
                "UPDATE weather_markets SET city_code=?, latitude=?, longitude=? WHERE id=?",
                (icao, new_lat, new_lon, mid),
            )
    if args.apply:
        conn.commit()
        print(f"YAZILDI: {changed} market guncellendi.")
    else:
        print(f"DRY-RUN: {changed} market guncellenecek (--apply ile yaz).")
    for ex in examples:
        print(f"  {ex[0]} {ex[1]} -> {ex[2]}  coords ({ex[3]},{ex[4]}) -> ({ex[5]:.4f},{ex[6]:.4f})")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
