"""AKTIVITE AKISI KAYDEDICI (2026-08-19).

Kullanici: "botun calismasi / bet acilmasi-kapanmasini engelleyen TUM teknik
ve software hatalar aktivite akisinda gorulsun; peak bulunup acilamayan
betlerde neden acilamadigi yazilsin; bulunan peakler yazilsin."

Bu modul bot.db'ye `activity_events` tablosu yazar (uygulama katmani —
database/db.py'ye dokunulmaz). Kategoriler:
  peak_found   - METAR peak kilitlendi (sehir, bucket, saat)
  bet_opened   - bet acildi
  bet_closed   - yanlis bucket beti kapatildi (aktar)
  bet_blocked  - peak var ama bet acilamadi (detail = NEDEN)
  error        - teknik/software hata (detail = hata turu + sehir/url)

API (/api/health-check) son 50 olayi dashboard'a tasir.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger("UTILS_ACTIVITY")

_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bot.db")

_CREATE = """
CREATE TABLE IF NOT EXISTS activity_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    category TEXT NOT NULL,
    city TEXT,
    detail TEXT
)
"""
_INSERT = "INSERT INTO activity_events (ts, category, city, detail) VALUES (?, ?, ?, ?)"


def _ensure_table(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(_CREATE)
        conn.commit()
    except sqlite3.OperationalError:
        pass  # tablo zaten var ya da kilitli — sessiz gec


def log_event(category: str, city: str | None = None, detail: str = "") -> None:
    """Aktivite akisina bir olay yazar. Asla raise etmez (botu durdurmaz)."""
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        _ensure_table(conn)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(_INSERT, (now, category, city, detail[:500]))
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001 — aktivite logu botu durduramaz
        logger.debug("activity log yazilamadi: %s", exc)


def recent_events(limit: int = 200) -> list[dict]:
    """Son N olay (yeni once). Dashboard/API icin. 2026-08-19: 50 -> 200."""
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT ts, category, city, detail FROM activity_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [{"ts": r[0], "category": r[1], "city": r[2], "detail": r[3]} for r in rows]
    except Exception:  # noqa: BLE001
        return []


_PEAK_WATCH_CREATE = """
CREATE TABLE IF NOT EXISTS peak_watch (
    city TEXT PRIMARY KEY,
    cur REAL,
    prev REAL,
    direction TEXT,
    status TEXT,
    peak REAL,
    updated_at TEXT
)
"""


def update_peak_watch(rows: list[dict]) -> None:
    """Canli peak takibi durumu (sehir basina son durum, UPSERT).

    2026-08-19 kullanici: "hangi sehirler peak takibinde, su anda kac,
    bir oncekinden yuksek mu dusuk mu" — dashboard'da gorunur.
    """
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            conn.execute(_PEAK_WATCH_CREATE)
        except sqlite3.OperationalError:
            pass
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for r in rows:
            conn.execute(
                "INSERT INTO peak_watch (city, cur, prev, direction, status, peak, updated_at) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(city) DO UPDATE SET cur=excluded.cur, prev=excluded.prev, "
                "direction=excluded.direction, status=excluded.status, peak=excluded.peak, "
                "updated_at=excluded.updated_at",
                (r["city"], r["cur"], r["prev"], r["direction"], r["status"], r["peak"], now),
            )
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def peak_watch_list() -> list[dict]:
    """Tum takip edilen sehirlerin canli durumu (sicakliga gore azalan)."""
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            conn.execute(_PEAK_WATCH_CREATE)
        except sqlite3.OperationalError:
            pass
        rows = conn.execute(
            "SELECT city, cur, prev, direction, status, peak, updated_at "
            "FROM peak_watch ORDER BY COALESCE(cur, -999) DESC"
        ).fetchall()
        conn.close()
        return [
            {
                "city": r[0],
                "cur": r[1],
                "prev": r[2],
                "direction": r[3],
                "status": r[4],
                "peak": r[5],
                "updated_at": r[6],
            }
            for r in rows
        ]
    except Exception:  # noqa: BLE001
        return []


def log_daily_market_summary() -> None:
    """Gunde 1 kez: Poly'de kac market/sehir var vs bizim DB'de kac var.

    Kullanici 2026-08-19: "polyde kac market/sehir var, bizim db de kac var".
    Ayni gun zaten yazildiysa atlar (idempotent).
    """
    import sqlite3 as _sq

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        conn = _sq.connect(_DB_PATH, timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        _ensure_table(conn)
        already = conn.execute(
            "SELECT COUNT(*) FROM activity_events WHERE category='daily_summary' AND ts LIKE ?", (today + "%",)
        ).fetchone()[0]
        if already:
            conn.close()
            return
        # DB'deki market/sehir sayilari
        db_markets = conn.execute("SELECT COUNT(*) FROM weather_markets WHERE target_date >= date('now')").fetchone()[0]
        db_cities = conn.execute(
            "SELECT COUNT(DISTINCT city_code) FROM weather_markets WHERE target_date >= date('now')"
        ).fetchone()[0]
        conn.close()
        # Poly canli sayisi (Gamma public-search toplami — basarisizsa bilinmez)
        try:
            import requests

            from config.settings import bot_config

            proxies = bot_config.polymarket.get_proxies()
            r = requests.get(
                "https://gamma-api.polymarket.com/public-search",
                params={"q": "temperature", "limit_per_type": "100"},
                timeout=20,
                proxies=proxies,
            )
            evs = (r.json() or {}).get("events", []) if r.status_code == 200 else []
            poly_markets = sum(len(e.get("markets") or []) for e in evs)
        except Exception:  # noqa: BLE001
            poly_markets = -1
        log_event(
            "daily_summary",
            None,
            f"Poly(arama): {poly_markets} market | DB: {db_markets} market / {db_cities} sehir (bugun+gelecek)",
        )
    except Exception:  # noqa: BLE001
        pass
