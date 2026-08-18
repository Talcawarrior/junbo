"""Kilitli peak'i olan ama bucket marketi acilmamis sehirleri loglar.

Kullanici (2026-08-18): "8'inde bucket marketi acilmamis — o sehirleri tekrar
goster yada geldikce bir yere kaydet bakalim yarin." Bu script o gunun
"kilitlendi AMA market yok" sehirlerini `reports/missing_markets_log.md`'ye
TARIH basligiyla EKLER (idempotent: ayni tarih yeniden yazilir). Kullanici
Polyden kontrol edip gercek pazar yapisini karsilastirabilir.

Kullanim:
    python scripts/log_missing_markets.py                # bugun
    python scripts/log_missing_markets.py --day 2026-08-18
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.backtest import ts, peak_lock  # noqa: E402
from scrapers.metar import city_utc_offset  # noqa: E402

LOG_PATH = os.path.join(_REPO_ROOT, "reports", "missing_markets_log.md")


def _collect(day: str) -> list[tuple[str, str, float, list[int]]]:
    """(code, city, kilitli_peak, mevcut esikler) — marketi olmayanlar."""
    db = sqlite3.connect(os.path.join(_REPO_ROOT, "data", "bot.db"), timeout=30)
    db.execute("PRAGMA busy_timeout=30000")

    city_of: dict[str, str] = {}
    thrs: dict[str, set[int]] = defaultdict(set)
    for code, city, thr in db.execute(
        "SELECT city_code, city, threshold FROM weather_markets "
        "WHERE metric='temperature_max' AND market_type='RANGE' "
        "AND target_date LIKE ? AND threshold IS NOT NULL",
        (day + "%",),
    ):
        if code:
            city_of.setdefault(code, city)
            try:
                thrs[code].add(int(float(thr) + 0.5))
            except (TypeError, ValueError):
                pass
    lon: dict[str, float] = {}
    for code, lg in db.execute(
        "SELECT DISTINCT city_code, longitude FROM weather_markets "
        "WHERE city_code IS NOT NULL AND longitude IS NOT NULL"
    ):
        try:
            lon.setdefault(code, float(lg))
        except (TypeError, ValueError):
            pass
    day_rows: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for code, tmax, obs in db.execute(
        "SELECT city_code, temp_c, obs_time FROM metar_observations WHERE temp_c IS NOT NULL AND obs_time LIKE ?",
        (day + "%",),
    ):
        t = ts(obs)
        if code and t is not None:
            day_rows[code].append((t, float(tmax)))
    db.close()

    out: list[tuple[str, str, float, list[int]]] = []
    for code, rows in day_rows.items():
        rows.sort(key=lambda x: x[0])
        pk, _lock = peak_lock(rows, city_utc_offset(code, day, lon.get(code)))
        if pk is None:
            continue
        B = int(pk + 0.5) if pk >= 0 else int(pk - 0.5)
        if B in thrs.get(code, set()):
            continue  # market VAR
        out.append((code, city_of.get(code, code), float(pk), sorted(thrs.get(code, set()))))
    out.sort(key=lambda x: x[0])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Market yok loglayici")
    parser.add_argument("--day", default=None, help="gun (default: bugun UTC)")
    args = parser.parse_args()
    day = args.day or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    rows = _collect(day)
    lines = [f"## {day} — kilitli peak AMA bucket marketi yok ({len(rows)} sehir)\n"]
    lines.append("| Sehir | ICAO | Kilitli METAR peak | Polydaki RANGE max esikleri |")
    lines.append("|---|---|---|---|")
    for code, city, pk, esikler in rows:
        esik_s = ", ".join(str(e) for e in esikler) if esikler else "-"
        lines.append(f"| {city} | {code} | {pk:.1f}C | {esik_s} |")
    if not rows:
        lines.append("| - | - | - | - |")
    lines.append("")

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    existing = ""
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, encoding="utf-8") as f:
            existing = f.read()
    header = f"## {day} "
    if header in existing:
        # ayni tarih yeniden yazilir (idempotent)
        idx = existing.index(header)
        nxt = existing.find("## ", idx + 1)
        existing = (existing[:idx] if idx > 0 else "") + (existing[nxt:] if nxt != -1 else "")
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write(existing + "\n".join(lines))

    print(f"{day}: {len(rows)} sehirde bucket marketi yok -> {LOG_PATH}")
    for code, city, pk, esikler in rows:
        print(f"  {city:16s} ({code}) kilitli={pk:.1f}C  esikler={esikler}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
