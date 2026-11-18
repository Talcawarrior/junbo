#!/usr/bin/env python3
"""Query the JUNBO data catalog.

Examples:
    python scripts/query_data_catalog.py --search weather_forecasts
    python scripts/query_data_catalog.py --search "D:\\JUNBO data"
    python scripts/query_data_catalog.py --coverage market_snapshots
"""

import argparse
import os
import sqlite3
from pathlib import Path
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_data_dir  # noqa: E402

DATA_DIR = Path(os.getenv("JUNBO_DATA_DIR", get_data_dir()))
CATALOG_DB = Path(os.getenv("JUNBO_CATALOG_DB", str(DATA_DIR / "catalog" / "data_catalog.db")))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--search", default=None, help="Match path, role or table name")
    ap.add_argument("--coverage", default=None, help="Show date ranges for a table name")
    args = ap.parse_args()

    if not CATALOG_DB.exists():
        raise SystemExit(f"Catalog not found: {CATALOG_DB}. Run scripts/build_data_catalog.py first.")

    conn = sqlite3.connect(f"file:{CATALOG_DB}?mode=ro", uri=True)
    cur = conn.cursor()

    if args.coverage:
        rows = cur.execute(
            "SELECT f.path, f.role, t.table_name, t.row_count, t.date_ranges_json, t.error "
            "FROM tables t JOIN files f ON f.path=t.path WHERE t.table_name LIKE ? "
            "ORDER BY f.path",
            (f"%{args.coverage}%",),
        ).fetchall()
        for path, role, table, count, ranges, err in rows:
            print(f"{path}\n  role={role} table={table} rows={count} ranges={ranges} error={err}")
        return

    pattern = f"%{args.search}%" if args.search else "%"
    rows = cur.execute(
        "SELECT DISTINCT f.path, f.role, f.status, f.size_bytes, t.table_name, t.row_count, t.date_ranges_json "
        "FROM files f LEFT JOIN tables t ON f.path=t.path "
        "WHERE f.path LIKE ? OR f.role LIKE ? OR t.table_name LIKE ? "
        "ORDER BY f.path, t.table_name",
        (pattern, pattern, pattern),
    ).fetchall()
    for path, role, status, size, table, count, ranges in rows:
        print(f"{path}")
        print(
            f"  role={role} status={status} size_mb={(size or 0) / 1048576:.1f} table={table} rows={count} ranges={ranges}"  # noqa: E501
        )
    conn.close()


if __name__ == "__main__":
    main()
