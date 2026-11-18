#!/usr/bin/env python3
"""Build and update a catalog of every data file JUNBO can use.

Scans the configured roots for SQLite, Parquet, CSV and JSONL files, records
their schema, row counts and date coverage, and writes:
  - <catalog>/data_catalog.db   (queryable catalog)
  - <catalog>/DATA_CATALOG.md   (human-readable index)
  - <catalog>/data_catalog.json (machine-readable export)

New files are discovered on every run. Existing rows keep first_seen_utc.
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_data_dir  # noqa: E402

import pandas as pd
import pyarrow.parquet as pq

DATA_DIR = Path(os.getenv("JUNBO_DATA_DIR", get_data_dir()))
CATALOG_DIR = DATA_DIR / "catalog"
CATALOG_DB = CATALOG_DIR / "data_catalog.db"
CATALOG_MD = CATALOG_DIR / "DATA_CATALOG.md"
CATALOG_JSON = CATALOG_DIR / "data_catalog.json"
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
CATALOG_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_ROOTS = [
    get_data_dir(),
    r"C:\junbo_offsite_backups",
    r"D:\Bot DATA backups.old",
    r"D:\ORTAK data.junction-old",
    r"C:\Users\fdemir\Documents\New project\Heat",
    r"C:\Users\fdemir\Documents\New project\ASIAbot",
    r"C:\Users\fdemir\Documents\New project\junbo",
]
EXTS = {".db", ".sqlite", ".sqlite3", ".parquet", ".csv", ".jsonl", ".ndjson"}
SKIP_PARTS = {"node_modules", ".git", "__pycache__", ".venv", "site-packages", "catalog", "logs"}
DATE_COLS = [
    "target_date",
    "date",
    "datetime",
    "timestamp",
    "snapshot_time",
    "fetched_at",
    "placed_at",
    "created_at",
    "analyzed_at",
    "recorded_at",
    "last_updated",
    "ts",
    "end_date",
    "start_date",
    "run_at_utc",
    "archived_at",
]

KEY_RANGES = [
    ("bot.db", "weather_markets", "target_date"),
    ("bot.db", "weather_forecasts", "target_date"),
    ("bot.db", "market_snapshots", "snapshot_time"),
    ("bot.db", "bets", "placed_at"),
    ("backtest.db", "weather_markets", "target_date"),
    ("backtest.db", "weather_forecasts", "target_date"),
    ("backtest.db", "market_snapshots", "snapshot_time"),
    ("kardesler.db", "junbo_weather_markets", "target_date"),
    ("kardesler.db", "junbo_weather_forecasts", "target_date"),
    ("kardesler.db", "junbo_market_snapshots", "snapshot_time"),
    ("kardesler.db", "junbo_orderbook_snapshots", "snapshot_time"),
    ("kardesler.db", "asi_weather_markets", "target_date"),
    ("kardesler.db", "asi_weather_forecasts", "target_date"),
    ("kardesler.db", "asi_forecast_archive", "target_date"),
    ("kardesler.db", "asi_historical_calibrations", "date"),
    ("kardesler.db", "junbo_historical_calibrations", "date"),
    ("orderbook.db", "orderbook_snapshots", "snapshot_time"),
    ("tempmarket-main.sqlite", "prediction_log", "target_date"),
    ("tempmarket-main.sqlite", "forecast_snapshots", "target_date"),
    ("tempmarket-main.sqlite", "settlements", "target_date"),
    ("tempmarket-main.sqlite", "observations", "target_date"),
    ("tempmarket-main.sqlite", "reports", "target_date"),
    ("tempmarket-edge.sqlite", "prediction_log", "target_date"),
    ("tempmarket-edge.sqlite", "forecast_snapshots", "target_date"),
    ("tempmarket-edge.sqlite", "settlements", "target_date"),
    ("tempmarket-edge.sqlite", "observations", "target_date"),
    ("tempmarket-edge.sqlite", "reports", "target_date"),
    ("actuals.db", "actual_temperatures", "date"),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DATA-CATALOG] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "data_catalog.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("data_catalog")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def role_for(path: Path) -> str:
    p = str(path).lower()
    name = path.name.lower()
    if "offsite" in p or "_eski" in p and name.endswith(".db"):
        return "bot DB backup"
    if name == "bot.db":
        return "live bot operational DB"
    if name == "backtest.db":
        return "backtest archive"
    if name == "orderbook.db":
        return "orderbook snapshots"
    if name == "actuals.db":
        return "Open-Meteo actuals"
    if name == "kardesler.db":
        return "Junbo+ASIA mirror"
    if "tempmarket-main" in name:
        return "Heat main DB"
    if "tempmarket-edge" in name:
        return "Heat edge DB"
    if "weather_forecasts" in name and name.endswith(".parquet"):
        return "forecast archive"
    if "historical_calibrations" in name and name.endswith(".parquet"):
        return "calibration archive"
    if name == "markets.parquet":
        return "unified markets"
    if name == "actuals.parquet":
        return "unified actuals"
    if name.startswith("analyses_") and name.endswith(".parquet"):
        return "analysis archive"
    if name.endswith(".parquet"):
        return "parquet dataset"
    if name.endswith((".csv", ".jsonl", ".ndjson")):
        return "flat data file"
    return "SQLite database"


def sqlite_connect(path: Path) -> sqlite3.Connection:
    uri = "file:" + str(path).replace("\\", "/").replace(" ", "%20") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def query_with_timeout(conn: sqlite3.Connection, sql: str, params: tuple = (), seconds: float = 5.0):
    start = time.monotonic()
    conn.set_progress_handler(lambda: 1 if time.monotonic() - start > seconds else 0, 10000)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.set_progress_handler(None, 0)


def sqlite_table_info(conn: sqlite3.Connection, table: str, fast: bool = False) -> dict:
    cols = [(r[1], r[2]) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
    row_count, approx = None, False
    if fast:
        approx = True
        try:
            rows = query_with_timeout(conn, f'SELECT MAX(rowid) FROM "{table}"', seconds=2.0)
            row_count = int(rows[0][0] or 0)
        except sqlite3.Error:
            row_count = None
        return {"row_count": row_count, "row_count_approx": approx, "columns": cols, "date_ranges": {}}
    try:
        rows = query_with_timeout(conn, f'SELECT COUNT(*) FROM "{table}"', seconds=3.0)
        row_count = int(rows[0][0])
    except sqlite3.Error:
        approx = True
        try:
            rows = query_with_timeout(conn, f'SELECT MAX(rowid) FROM "{table}"', seconds=2.0)
            row_count = int(rows[0][0] or 0)
        except sqlite3.Error:
            row_count = None
    date_ranges = {}
    names = [c[0] for c in cols]
    for dc in [c for c in DATE_COLS if c in names][:2]:
        try:
            rows = query_with_timeout(
                conn,
                f'SELECT MIN("{dc}"), MAX("{dc}") FROM "{table}" WHERE "{dc}" IS NOT NULL',
                seconds=3.0,
            )
            mn, mx = rows[0] if rows else (None, None)
        except sqlite3.Error:
            mn = mx = None
            try:
                lo = query_with_timeout(
                    conn,
                    f'SELECT "{dc}" FROM "{table}" WHERE "{dc}" IS NOT NULL ORDER BY rowid ASC LIMIT 1',
                    seconds=2.0,
                )
                hi = query_with_timeout(
                    conn,
                    f'SELECT "{dc}" FROM "{table}" WHERE "{dc}" IS NOT NULL ORDER BY rowid DESC LIMIT 1',
                    seconds=2.0,
                )
                mn = lo[0][0] if lo else None
                mx = hi[0][0] if hi else None
            except sqlite3.Error:
                pass
        date_ranges[dc] = [str(mn) if mn is not None else None, str(mx) if mx is not None else None]
    return {
        "row_count": row_count,
        "row_count_approx": approx,
        "columns": cols,
        "date_ranges": date_ranges,
    }


def scan_sqlite(path: Path, fast: bool = False) -> list[dict]:
    conn = sqlite_connect(path)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        out = []
        for t in tables:
            try:
                info = sqlite_table_info(conn, t, fast=fast)
                out.append({"table": t, **info, "error": None})
            except sqlite3.Error as exc:
                out.append(
                    {
                        "table": t,
                        "row_count": None,
                        "row_count_approx": False,
                        "columns": [],
                        "date_ranges": {},
                        "error": str(exc),
                    }
                )
        return out
    finally:
        conn.close()


def scan_parquet(path: Path) -> list[dict]:
    pf = pq.ParquetFile(path)
    names = list(pf.schema_arrow.names)
    rows = pf.metadata.num_rows
    date_ranges = {}
    small = path.stat().st_size < 250 * 1024 * 1024
    if small:
        for dc in [c for c in DATE_COLS if c in names][:3]:
            try:
                s = pd.read_parquet(path, columns=[dc])[dc]
                s = pd.to_datetime(s, errors="coerce")
                date_ranges[dc] = [str(s.min()), str(s.max())]
            except Exception:
                date_ranges[dc] = [None, None]
    return [
        {
            "table": path.stem,
            "row_count": int(rows),
            "row_count_approx": False,
            "columns": [(n, str(pf.schema_arrow.field(n).type)) for n in names],
            "date_ranges": date_ranges,
            "error": None,
        }
    ]


def init_catalog() -> None:
    conn = sqlite3.connect(str(CATALOG_DB))
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS files (
        path TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        size_bytes INTEGER,
        mtime_utc TEXT,
        first_seen_utc TEXT NOT NULL,
        last_seen_utc TEXT NOT NULL,
        role TEXT,
        status TEXT NOT NULL DEFAULT 'present',
        error TEXT
    );
    CREATE TABLE IF NOT EXISTS tables (
        path TEXT NOT NULL,
        table_name TEXT NOT NULL,
        row_count INTEGER,
        row_count_approx INTEGER DEFAULT 0,
        columns_json TEXT,
        date_ranges_json TEXT,
        checked_utc TEXT NOT NULL,
        error TEXT,
        PRIMARY KEY (path, table_name)
    );
    CREATE TABLE IF NOT EXISTS runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_utc TEXT,
        finished_utc TEXT,
        roots TEXT,
        files_seen INTEGER,
        files_new INTEGER,
        files_missing INTEGER,
        errors INTEGER
    );
    """)
    conn.commit()
    conn.close()


def scan_once(roots: list[str], max_seconds: float = 5.0) -> dict:
    init_catalog()
    started = utc_now()
    seen = set()
    files_new = errors = 0
    found = []

    for root in roots:
        rp = Path(root)
        if not rp.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(rp):
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_PARTS]
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix.lower() not in EXTS:
                    continue
                try:
                    seen.add(str(p.resolve()))
                    found.append(p)
                except OSError:
                    continue

    conn = sqlite3.connect(str(CATALOG_DB))
    cur = conn.cursor()
    now = utc_now()
    for p in found:
        key = str(p.resolve())
        kind = p.suffix.lower().lstrip(".")
        try:
            st = p.stat()
        except OSError:
            continue
        existing = cur.execute("SELECT first_seen_utc FROM files WHERE path=?", (key,)).fetchone()
        if existing is None:
            files_new += 1
            first_seen = now
        else:
            first_seen = existing[0]
        cur.execute(
            "INSERT INTO files(path, kind, size_bytes, mtime_utc, first_seen_utc, last_seen_utc, role, status, error) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
            "kind=excluded.kind, size_bytes=excluded.size_bytes, mtime_utc=excluded.mtime_utc, "
            "last_seen_utc=excluded.last_seen_utc, role=excluded.role, status='present', error=NULL",
            (
                key,
                kind,
                st.st_size,
                datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                first_seen,
                now,
                role_for(p),
                "present",
                None,
            ),
        )
        tables = []
        err = None
        fast = st.st_size > 300 * 1024 * 1024 or any(x in key.lower() for x in ("\\offsite", "_eski", "\\backups"))
        logger.info("Scanning %s (fast=%s, %.1f MB)", key, fast, st.st_size / 1048576)
        try:
            if kind in ("db", "sqlite", "sqlite3"):
                tables = scan_sqlite(p, fast=fast)
            elif kind == "parquet":
                tables = scan_parquet(p)
        except Exception as exc:
            err = str(exc)
            errors += 1
            cur.execute("UPDATE files SET error=? WHERE path=?", (err, key))
        cur.execute("DELETE FROM tables WHERE path=?", (key,))
        for t in tables:
            cur.execute(
                "INSERT OR REPLACE INTO tables(path, table_name, row_count, row_count_approx, "
                "columns_json, date_ranges_json, checked_utc, error) VALUES(?,?,?,?,?,?,?,?)",
                (
                    key,
                    t["table"],
                    t.get("row_count"),
                    int(bool(t.get("row_count_approx"))),
                    json.dumps(t.get("columns", []), ensure_ascii=True),
                    json.dumps(t.get("date_ranges", {}), ensure_ascii=True),
                    now,
                    t.get("error"),
                ),
            )
        conn.commit()

    # Mark previously seen files under the scanned roots as missing.
    if seen:
        qmarks = ",".join("?" for _ in seen)
        cur.execute(f"UPDATE files SET status='missing' WHERE path NOT IN ({qmarks})", tuple(seen))
        files_missing = cur.rowcount
        conn.commit()
    else:
        files_missing = 0

    finished = utc_now()
    cur.execute(
        "INSERT INTO runs(started_utc, finished_utc, roots, files_seen, files_new, files_missing, errors) "
        "VALUES(?,?,?,?,?,?,?)",
        (started, finished, json.dumps(roots), len(seen), files_new, files_missing, errors),
    )
    conn.commit()
    enrich_key_ranges(conn)
    _write_reports(conn)
    conn.close()
    summary = {"files_seen": len(seen), "files_new": files_new, "files_missing": files_missing, "errors": errors}
    logger.info("Catalog run: %s", summary)
    return summary


def enrich_key_ranges(catalog_conn: sqlite3.Connection) -> None:
    """Fill date ranges for key tables even when fast mode skipped them."""
    now = utc_now()
    for basename, table, col in KEY_RANGES:
        paths = catalog_conn.execute(
            "SELECT path FROM files WHERE path LIKE ? AND status='present'", (f"%{basename}",)
        ).fetchall()
        for (path,) in paths:
            low = path.lower()
            if "offsite" in low or "_eski" in low or "\\backups" in low:
                continue
            try:
                src = sqlite_connect(Path(path))
                try:
                    try:
                        rows = query_with_timeout(
                            src,
                            f'SELECT MIN("{col}"), MAX("{col}") FROM "{table}" WHERE "{col}" IS NOT NULL',
                            seconds=10.0,
                        )
                        mn, mx = rows[0] if rows else (None, None)
                    except sqlite3.Error:
                        lo = query_with_timeout(
                            src,
                            f'SELECT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL ORDER BY rowid ASC LIMIT 1',
                            seconds=5.0,
                        )
                        hi = query_with_timeout(
                            src,
                            f'SELECT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL ORDER BY rowid DESC LIMIT 1',
                            seconds=5.0,
                        )
                        mn = lo[0][0] if lo else None
                        mx = hi[0][0] if hi else None
                finally:
                    src.close()
                old = catalog_conn.execute(
                    "SELECT date_ranges_json FROM tables WHERE path=? AND table_name=?", (path, table)
                ).fetchone()
                ranges = json.loads(old[0]) if old and old[0] else {}
                ranges[col] = [str(mn) if mn is not None else None, str(mx) if mx is not None else None]
                catalog_conn.execute(
                    "UPDATE tables SET date_ranges_json=?, checked_utc=? WHERE path=? AND table_name=?",
                    (json.dumps(ranges, ensure_ascii=True), now, path, table),
                )
            except sqlite3.Error as exc:
                logger.debug("Key range failed for %s %s: %s", path, table, exc)
    catalog_conn.commit()


def _write_reports(conn: sqlite3.Connection) -> None:
    files = conn.execute(
        "SELECT path, kind, size_bytes, mtime_utc, role, status, error FROM files ORDER BY path"
    ).fetchall()
    lines = [
        "# JUNBO Data Catalog",
        "",
        f"Generated: {utc_now()}",
        "",
        "This file is generated by `scripts/build_data_catalog.py`. Query the SQLite catalog for details:",
        "",
        f"- `{CATALOG_DB}`",
        f"- `{CATALOG_JSON}`",
        "",
        "## Files",
        "",
        "| Role | Path | Size MB | Tables | Last seen | Status |",
        "|---|---|---:|---:|---|---|",
    ]
    export = []
    for path, kind, size, mtime, role, status, err in files:
        tables = conn.execute(
            "SELECT table_name, row_count, row_count_approx, date_ranges_json, error "
            "FROM tables WHERE path=? ORDER BY table_name",
            (path,),
        ).fetchall()
        table_desc = ", ".join(f"{t[0]}={t[1]}{'~' if t[2] else ''}" for t in tables[:6])
        lines.append(
            "| %s | `%s` | %.2f | %s | %s | %s |"
            % (role or "", path, (size or 0) / (1024 * 1024), table_desc, mtime or "", status)
        )
        export.append(
            {
                "path": path,
                "kind": kind,
                "size_bytes": size,
                "mtime_utc": mtime,
                "role": role,
                "status": status,
                "error": err,
                "tables": [
                    {
                        "table": t[0],
                        "row_count": t[1],
                        "approx": bool(t[2]),
                        "date_ranges": json.loads(t[3] or "{}"),
                        "error": t[4],
                    }
                    for t in tables
                ],
            }
        )
    lines += [
        "",
        "## Coverage notes",
        "",
        "- `bot.db` is the live source; its `weather_forecasts` is a hot window and is pruned.",
        "- `archive/weather_forecasts_*.parquet` and `archive/forecasts_snapshot_*.parquet` are the forecast history.",
        "- `market_snapshots` is the entry-price history; `orderbook_snapshots` is the depth history.",
        "- `unified/markets.parquet` and `unified/actuals.parquet` are the cleaned join layer.",
        "- Run `python scripts/build_data_catalog.py` after adding new DB files.",
    ]
    CATALOG_MD.write_text("\n".join(lines), encoding="utf-8")
    CATALOG_JSON.write_text(json.dumps(export, indent=1, ensure_ascii=True), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--roots", nargs="*", default=DEFAULT_ROOTS)
    ap.add_argument("--max-seconds", type=float, default=5.0)
    args = ap.parse_args()
    scan_once(args.roots, max_seconds=args.max_seconds)


if __name__ == "__main__":
    main()
