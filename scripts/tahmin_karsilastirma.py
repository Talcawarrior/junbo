"""Tahmin karsilastirma (2026-08-21, kullanici).

Kullanici: "senin erken giris tahminlerinle karsilastir weather api yi ve
sakla bakalim gecenin sonunda ne kadar tutturuyor".

Iki tahmin kaynagini karsilastirir (dogrulama icin saklanir):
  1. metar_peak erken giris tahmini = bugunku METAR gozlem max'i (peak_watch
     durumu ile birlikte). Kaynak: gercek istasyon gozlemi, gun bitmemis.
  2. WeatherAPI.com bugunluk max tahmini (maxtemp_c). Kaynak: model tahmini.

Gece sonu (kapanis 24:00 UTC sonrasi) gercek max ile karsilastirilir:
  - gercek WU max = Polymarket kazanan bucket (settled_win market threshold)
  - METAR max = metar_observations MAX(temp_c) (referans, WU'dan sapabilir)

Kullanim:
  python scripts/tahmin_karsilastirma.py kaydet      # snapshot al + JSON sakla
  python scripts/tahmin_karsilastirma.py karsilastir # gece sonu dogrulama
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import ssl
import sys
import time
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_data_dir, get_db_path  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = get_db_path()
OUT_DIR = os.path.join(get_data_dir(), "analysis_results")


class _SystemStoreAdapter(HTTPAdapter):
    """Windows sistem root store (Avast TLS MITM cozumu, weather_ensemble ile ayni)."""

    def __init__(self):
        self.ctx = ssl.create_default_context()
        super().__init__()

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self.ctx
        return super().init_poolmanager(*args, **kwargs)


def _load_weatherapi_key() -> str:
    env = open(os.path.join(ROOT, ".env"), encoding="utf-8").read()
    key = re.search(r"WEATHERAPI_KEY=(\S+)", env)
    if not key:
        sys.exit("WEATHERAPI_KEY .env'de yok")
    return key.group(1)


def _session() -> requests.Session:
    s = requests.Session()
    s.mount("https://", _SystemStoreAdapter())
    return s


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def _metar_today_max(conn, city_code: str, today: str) -> float | None:
    r = conn.execute(
        "SELECT MAX(temp_c) FROM metar_observations WHERE city_code=? AND day=?",
        (city_code, today),
    ).fetchone()
    return float(r[0]) if r and r[0] is not None else None


def _peak_watch_row(conn, city: str) -> dict | None:
    r = conn.execute("SELECT status, peak FROM peak_watch WHERE city=? LIMIT 1", (city,)).fetchone()
    if not r:
        return None
    return {"status": r[0], "peak": float(r[1]) if r[1] is not None else None}


def _cmd_kaydet(_args) -> int:
    """Bugunku acik sehirlerin tahmin snapshot'ini JSON'a ekler (append)."""
    s = _session()
    key = _load_weatherapi_key()
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    f4_ep = int(now.timestamp()) + 4 * 3600

    conn = _db()
    cities = conn.execute(
        """
        SELECT city, city_code, latitude, longitude FROM weather_markets
        WHERE status='open' AND target_date LIKE ? AND metric='temperature_max'
        GROUP BY city, city_code ORDER BY city
        """,
        (today + "%",),
    ).fetchall()
    prices: dict[str, list] = {}
    for row in conn.execute(
        """
        SELECT city, threshold, yes_price FROM weather_markets
        WHERE status='open' AND target_date LIKE ? AND metric='temperature_max'
          AND market_type='RANGE' AND yes_price IS NOT NULL
        """,
        (today + "%",),
    ).fetchall():
        prices.setdefault(row[0], []).append((float(row[1]), float(row[2])))

    sehirler = []
    for city, code, lat, lon in cities:
        rec: dict = {"city": city, "city_code": code}
        rec["metar_bugun_max"] = _metar_today_max(conn, code, today)
        rec["peak_watch"] = _peak_watch_row(conn, city)
        rec["polymarket"] = [{"esik": t, "fiyat": p} for t, p in sorted(prices.get(city, []))]
        try:
            r = s.get(
                "https://api.weatherapi.com/v1/forecast.json",
                params={"key": key, "q": f"{lat},{lon}", "days": 1, "aqi": "no"},
                timeout=15,
            )
            if r.status_code == 200:
                d = r.json()
                fd = d["forecast"]["forecastday"][0]
                hours = fd.get("hour", [])
                cur = min(
                    hours,
                    key=lambda h: abs((h.get("time_epoch") or 0) - int(now.timestamp())),
                    default=None,
                )
                f4 = min(hours, key=lambda h: abs((h.get("time_epoch") or 0) - f4_ep), default=None)
                rec["weatherapi"] = {
                    "max_tahmin": fd["day"].get("maxtemp_c"),
                    "simdi": cur["temp_c"] if cur else None,
                    "plus4sa": f4["temp_c"] if f4 else None,
                }
            else:
                rec["weatherapi"] = {"max_tahmin": None, "hata": r.status_code}
        except requests.RequestException as e:
            rec["weatherapi"] = {"max_tahmin": None, "hata": type(e).__name__}
        time.sleep(0.4)
        sehirler.append(rec)
    conn.close()

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"tahminler_{today}.json")
    snap = {"kayit_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "sehirler": sehirler}
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        data.append(snap)
    else:
        data = [snap]
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    print(f"{len(sehirler)} sehir kaydedildi -> {out}")
    print(f"  kayit_utc={snap['kayit_utc']} (dosyada {len(data)} snapshot)")
    return 0


def _settled_win_bucket(conn, city: str, today: str) -> float | None:
    """Polymarket kazanan bucket (WU gercek max'in round'u)."""
    r = conn.execute(
        """
        SELECT threshold FROM weather_markets
        WHERE city=? AND target_date LIKE ? AND metric='temperature_max'
          AND market_type='RANGE' AND status='settled_win'
        LIMIT 1
        """,
        (city, today + "%"),
    ).fetchone()
    return float(r[0]) if r and r[0] is not None else None


def _cmd_karsilastir(_args) -> int:
    """Kayitli tahminleri gercek max ile karsilastirir (gece sonu)."""
    # dosya adindan gun: tahminler_2026-08-21.json -> 2026-08-21
    gun = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join(OUT_DIR, f"tahminler_{gun}.json")
    if not os.path.exists(path):
        sys.exit(f"kayit yok: {path} (once 'kaydet' calistir)")
    # gun icin daha erken bir dosya varsa onu kullan (kapanis sonrasi ertesi gun)
    mevcut = sorted(f for f in os.listdir(OUT_DIR) if f.startswith("tahminler_") and f.endswith(".json"))
    if not mevcut:
        sys.exit(f"kayit yok: {path}")
    path = os.path.join(OUT_DIR, mevcut[-1])
    gun = os.path.basename(path).replace("tahminler_", "").replace(".json", "")
    with open(path, encoding="utf-8") as f:
        snaps = json.load(f)

    conn = _db()
    rows = []
    for snap in snaps:
        for sh in snap["sehirler"]:
            city = sh["city"]
            wa = sh.get("weatherapi") or {}
            metar_obs = sh.get("metar_bugun_max")
            gercek_metar = _metar_today_max(conn, sh["city_code"], gun)
            gercek_wu = _settled_win_bucket(conn, city, gun)
            rows.append(
                {
                    "city": city,
                    "snapshot_utc": snap["kayit_utc"],
                    "metar_peak_tahmin": metar_obs,
                    "weatherapi_tahmin": wa.get("max_tahmin"),
                    "gercek_metar": gercek_metar,
                    "gercek_wu_bucket": gercek_wu,
                }
            )
    conn.close()

    # her sehir icin en son snapshot (erken giris tahmini) kullan
    by_city: dict[str, dict] = {}
    for r in rows:
        by_city[r["city"]] = r  # dosya sirasi kronolojik -> son yazar

    print(f"{'sehir':<15}{'metarPeak':>10}{'weathAPI':>10}{'gercekM':>9}{'WUbucket':>9}  hataMP hataWA")
    done = 0
    for city, r in sorted(by_city.items()):
        if r["gercek_metar"] is None and r["gercek_wu_bucket"] is None:
            continue
        done += 1
        err_mp = err_wa = "?"
        if r["gercek_wu_bucket"] is not None:
            if r["metar_peak_tahmin"] is not None:
                err_mp = f"{round(r['metar_peak_tahmin'] + 0.5) - r['gercek_wu_bucket']:+.0f}"
            if r["weatherapi_tahmin"] is not None:
                err_wa = f"{round(r['weatherapi_tahmin'] + 0.5) - r['gercek_wu_bucket']:+.0f}"
        mp = f"{r['metar_peak_tahmin']:.1f}" if r["metar_peak_tahmin"] is not None else "-"
        wa = f"{r['weatherapi_tahmin']:.1f}" if r["weatherapi_tahmin"] is not None else "-"
        gm = f"{r['gercek_metar']:.1f}" if r["gercek_metar"] is not None else "-"
        wu = f"{r['gercek_wu_bucket']:.0f}" if r["gercek_wu_bucket"] is not None else "-"
        print(f"{city:<15}{mp:>10}{wa:>10}{gm:>9}{wu:>9}  {err_mp:>6} {err_wa:>6}")
    if done == 0:
        print("Hicbir sehir henuz gerceklesmedi (kapanis sonrasi tekrar calistir).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Tahmin karsilastirma (metar_peak vs WeatherAPI)")
    p.add_argument(
        "komut",
        choices=["kaydet", "karsilastir"],
        help="kaydet: snapshot al; karsilastir: gece sonu dogrula",
    )
    args = p.parse_args()
    return _cmd_kaydet(args) if args.komut == "kaydet" else _cmd_karsilastir(args)


if __name__ == "__main__":
    sys.exit(main())
