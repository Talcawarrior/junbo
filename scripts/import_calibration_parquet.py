"""Arsiv parquet'lerindeki kalibrasyon verisini bot.db'ye yukler.

historical_calibrations_20260730.parquet (75 sehir, 112 gun) gibi arsiv
dosyalarini bot.db historical_calibrations tablosuna INSERT OR REPLACE eder.
Bu, bot.db'nin kendi 9 gunluk verisini ~120 gune cikarir (kalibrasyon
guclenir: uzun veriyle bias daha stabil).

Kullanim:
    python scripts/import_calibration_parquet.py --dry-run
    python scripts/import_calibration_parquet.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_db_path  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import sqlite3  # noqa: E402

ARCHIVE = os.path.join(_REPO_ROOT, "data", "archive")
BOT_DB = get_db_path()

DEFAULT_FILES = [
    "historical_calibrations_20260730.parquet",
    "historical_calibrations_20260630.parquet",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Arsiv kalibrasyon parquet'lerini bot.db'ye yukle")
    parser.add_argument("--apply", action="store_true", help="DB'ye yaz (varsayilan: dry-run)")
    parser.add_argument("--file", action="append", default=DEFAULT_FILES, help="parquet dosya adi")
    args = parser.parse_args()

    import pandas as pd

    conn = sqlite3.connect(BOT_DB)
    total_inserted = 0
    for fname in args.file:
        path = os.path.join(ARCHIVE, fname)
        if not os.path.exists(path):
            print(f"YOK: {fname}")
            continue
        df = pd.read_parquet(path)
        required = {"city_code", "city", "date", "metric", "model", "predicted_value", "actual_value", "bias"}
        missing = required - set(df.columns)
        if missing:
            print(f"ATLA {fname}: eksik kolonlar {missing}")
            continue
        rows = []
        for _, r in df.iterrows():
            rows.append(
                (
                    str(r["city_code"]),
                    str(r["city"]),
                    str(r["date"])[:10],
                    str(r["metric"]),
                    str(r["model"]),
                    float(r["predicted_value"]),
                    float(r["actual_value"]),
                    float(r["bias"]),
                )
            )
        print(
            f"{fname}: {len(rows)} satir, sehir={df['city_code'].nunique()}, "
            f"tarih={str(df['date'].min())[:10]}..{str(df['date'].max())[:10]}"
        )
        if args.apply and rows:
            conn.executemany(
                "INSERT OR REPLACE INTO historical_calibrations "
                "(city_code, city, date, metric, model, predicted_value, actual_value, bias, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                rows,
            )
            conn.commit()
            total_inserted += len(rows)

    if args.apply:
        n = conn.execute("SELECT COUNT(*) FROM historical_calibrations").fetchone()[0]
        dmin, dmax = conn.execute("SELECT MIN(date), MAX(date) FROM historical_calibrations").fetchone()
        cities = conn.execute("SELECT COUNT(DISTINCT city_code) FROM historical_calibrations").fetchone()[0]
        print(f"\nYAZILDI: {total_inserted} yeni satir.")
        print(f"toplam: {n} satir, {cities} sehir, tarih {str(dmin)[:10]}..{str(dmax)[:10]}")
    else:
        print("\nDRY-RUN (yazilmadi). --apply ile yaz.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
