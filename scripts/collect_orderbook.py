#!/usr/bin/env python3
"""Orderbook depth collector for ALL active weather markets.

Fetches market list from Polymarket Gamma API (with proper headers to get
clobTokenIds), then fetches orderbook from CLOB API for each market, and
stores depth metrics in data/orderbook.db. Detached from bot bet state —
bu script TUM acik weather marketlerin orderbook'unu toplar (sadece betli
olanlar degil), boylece backtest icin tam fiyat gecmisi birikir.

Kullanim: python scripts/collect_orderbook.py [--loop] [--interval 900]
  --loop       sonsuz dongu (bot entegrasyonu icin)
  --interval   dongu araligi saniye (varsayilan 900 = 15 dk)

Does NOT touch bot.db — fully independent data collection.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Paths ─────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ORDERBOOK_DB = ROOT / "data" / "orderbook.db"
LOCK_FILE = ROOT / "data" / ".orderbook_collect.lock"
LOCK_MAX_AGE = 30 * 60  # saniye; bayat lock = cokmus run, calmak guvenli
LOG_DIR = ROOT / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ORDERBOOK] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "collect_orderbook.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("collect_orderbook")


def acquire_lock() -> bool:
    """Tek seferde tek collector calissin diye lock dosyasi.

    2026-08-21: serial collect ~20dk surdugu icin watchdog'un sonraki
    tick'lerinde ust uste collect'ler basliyordu (orderbook.db 'database is
    locked' -> rc=1). Lock, cakisan run'lari daha baslamadan durdurur; bayat
    lock (cokmus run) calmak guvenli.
    """
    if LOCK_FILE.exists():
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
            if age < LOCK_MAX_AGE:
                logger.info("Baska bir collector calisiyor (lock age=%.0fs) - atlaniyor", age)
                return False
            logger.warning("Bayat lock (age=%.0fs) - caliniyor", age)
        except OSError:
            pass
    try:
        LOCK_FILE.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass
    return True


def release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


# ── Constants ─────────────────────────────────────────────────────────────
TIMEOUT = 60  # seconds per HTTP request
MAX_RETRIES = 3  # retry count on failure
RETRY_DELAY = 20  # seconds between retries
MAX_WORKERS = 15  # paralel HTTP cekim (CLOB fetch dar bogaz: ~2000 market)
CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
}


def get_proxies():
    """Polymarket SOCKS proxy (POLY_PROXY) — geo-block bypass."""
    try:
        from config.settings import bot_config

        return bot_config.polymarket.get_proxies()
    except Exception:
        return None


PROXIES = get_proxies()


# ── Database setup ────────────────────────────────────────────────────────
def init_orderbook_db() -> None:
    """Create orderbook.db and orderbook_snapshots table if not exists."""
    conn = sqlite3.connect(str(ORDERBOOK_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orderbook_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            token_id TEXT NOT NULL,
            city TEXT,
            metric TEXT,
            target_date TEXT,
            threshold REAL,
            bid_depth_usd REAL DEFAULT 0.0,
            ask_depth_usd REAL DEFAULT 0.0,
            best_bid REAL,
            best_ask REAL,
            spread REAL,
            num_bid_levels INTEGER DEFAULT 0,
            num_ask_levels INTEGER DEFAULT 0,
            snapshot_time TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # eski tabloya threshold kolonu ekle (yoksa)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(orderbook_snapshots)")]
    if "threshold" not in cols:
        conn.execute("ALTER TABLE orderbook_snapshots ADD COLUMN threshold REAL")
    conn.commit()
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_orderbook_market_time
        ON orderbook_snapshots (market_id, snapshot_time)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_orderbook_city
        ON orderbook_snapshots (city, snapshot_time)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_orderbook_token
        ON orderbook_snapshots (token_id)
    """)
    conn.commit()
    conn.close()
    logger.info("orderbook.db initialized")


def fetch_active_markets_from_gamma() -> list[dict[str, Any]]:
    """Fetch ALL active weather markets from bot.db (not Gamma events).

    NOT (2026-08-16): Gamma events?tag_slug=weather YANLIS kategoriler donuyor
    (April 2024 temperature increase gibi kapali iklim marketleri) — sehir
    bazli hava durumu degil. Bot.db'deki weather_markets zaten dogru sehir/
    tarih/threshold marketlerini icerir (run_fetch_markets ile proxy fix'ten
    beri cekiliyor). Burada DB'den acik marketler + clobTokenIds okunur.

    Returns: [{"id","token_id","city","city_code","metric","target_date",
                "threshold","question"}, ...]
    """
    import json

    import sqlite3

    db = sqlite3.connect(str(ROOT / "data" / "bot.db"))
    db.row_factory = sqlite3.Row
    markets = []

    def _yes_token(raw_data):
        if not raw_data:
            return None
        try:
            d = json.loads(raw_data)
        except Exception:
            return None
        toks = d.get("clobTokenIds")
        if isinstance(toks, str):
            try:
                toks = json.loads(toks)
            except Exception:
                return None
        if isinstance(toks, list) and toks:
            return str(toks[0])
        return None

    for r in db.execute(
        "SELECT id, city, city_code, threshold, target_date, raw_data FROM weather_markets WHERE status='open'"
    ):
        tok = _yes_token(r["raw_data"])
        if not tok:
            continue
        markets.append(
            {
                "id": str(r["id"]),
                "token_id": tok,
                "city": r["city"],
                "city_code": r["city_code"],
                "metric": "temperature_max",
                "threshold": r["threshold"],
                "target_date": (str(r["target_date"]) if r["target_date"] else ""),
                "question": "",
            }
        )
    db.close()
    logger.info("bot.db'den %d acik weather market (token'li) okundu", len(markets))
    return markets


def fetch_orderbook(token_id: str | None) -> dict[str, Any] | None:
    """Fetch live orderbook from CLOB API with retry logic + proxy.

    404 = kalici (token CLOB'ta yok) — retry YOK, direkt None. Diger hatalarda
    2 retry.
    """
    import requests

    url = f"{CLOB_BASE}/book?token_id={token_id}"
    max_tries = 2  # 404 icin retry yok, digerleri icin 2 deneme
    for attempt in range(1, max_tries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, proxies=PROXIES)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("CLOB attempt %d/%d failed for token %s: %s", attempt, max_tries, token_id[:16], exc)
            if attempt < max_tries:
                time.sleep(3)
    return None


def parse_orderbook(raw: dict[str, Any]) -> dict[str, float | None]:
    """Parse raw CLOB orderbook response into depth metrics."""
    bids = raw.get("bids", [])
    asks = raw.get("asks", [])

    bid_depth = 0.0
    best_bid = None
    for level in bids:
        try:
            price = float(level.get("price", 0))
            size = float(level.get("size", 0))
            if 0 < price < 1 and size > 0:
                bid_depth += price * size
                if best_bid is None or price > best_bid:
                    best_bid = price
        except (ValueError, TypeError):
            continue

    ask_depth = 0.0
    best_ask = None
    for level in asks:
        try:
            price = float(level.get("price", 0))
            size = float(level.get("size", 0))
            if 0 < price < 1 and size > 0:
                ask_depth += price * size
                if best_ask is None or price < best_ask:
                    best_ask = price
        except (ValueError, TypeError):
            continue

    spread = None
    if best_bid is not None and best_ask is not None:
        spread = best_ask - best_bid

    return {
        "bid_depth_usd": round(bid_depth, 2),
        "ask_depth_usd": round(ask_depth, 2),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": round(spread, 4) if spread is not None else None,
        "num_bid_levels": len(bids),
        "num_ask_levels": len(asks),
    }


def save_snapshot(
    conn: sqlite3.Connection,
    market_id: str,
    token_id: str,
    city: str | None,
    metric: str | None,
    target_date: str | None,
    metrics: dict[str, float | None],
    snapshot_time: str,
    threshold: float | None = None,
) -> None:
    """Insert orderbook snapshot into orderbook.db."""
    conn.execute(
        """
        INSERT INTO orderbook_snapshots
            (market_id, token_id, city, metric, target_date, threshold,
             bid_depth_usd, ask_depth_usd, best_bid, best_ask, spread,
             num_bid_levels, num_ask_levels, snapshot_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            market_id,
            token_id,
            city,
            metric,
            target_date,
            threshold,
            metrics["bid_depth_usd"],
            metrics["ask_depth_usd"],
            metrics["best_bid"],
            metrics["best_ask"],
            metrics["spread"],
            metrics["num_bid_levels"],
            metrics["num_ask_levels"],
            snapshot_time,
        ),
    )


def _fetch_one(market: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Bir marketin orderbook'unu cek. (market, raw_or_None) doner."""
    return market, fetch_orderbook(market["token_id"])


def collect_once(workers: int = MAX_WORKERS) -> int:
    """Run one collection cycle. Returns number of markets processed.

    2026-08-21 PARALEL: HTTP fetch (dar bogaz) ThreadPoolExecutor ile es
    zamanli; SQLite yazimi ana thread'de siralidir (tek conn, thread-safe).
    Seri cekim ~2000 market icin ~20dk suruyordu -> watchdog'un 45dk esigi
    icinde bitmiyor, sonraki tick'lerde DB kilitli gorunup ust uste collect
    birikiyordu. Paralel ile dongu ~3-4dk'ya iner.
    """
    markets = fetch_active_markets_from_gamma()
    if not markets:
        logger.info("No open weather markets in bot.db")
        return 0

    logger.info("Found %d weather markets, fetching orderbooks (%d workers)...", len(markets), workers)
    snapshot_time = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(str(ORDERBOOK_DB))
    processed = 0
    errors = 0

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_fetch_one, m) for m in markets]
            for fut in as_completed(futures):
                market, raw = fut.result()
                if raw is None:
                    errors += 1
                    continue

                metrics = parse_orderbook(raw)
                save_snapshot(
                    conn,
                    market["id"],
                    market["token_id"],
                    market.get("city"),
                    market.get("metric"),
                    market.get("target_date"),
                    metrics,
                    snapshot_time,
                    threshold=market.get("threshold"),
                )
                processed += 1

                if processed <= 10 or processed % 500 == 0:
                    logger.info(
                        "  [%d/%d] %s: bid=$%.1f ask=$%.1f spread=%.4f",
                        processed,
                        len(markets),
                        (market.get("city") or market["id"][:12]),
                        metrics["bid_depth_usd"],
                        metrics["ask_depth_usd"],
                        metrics["spread"] or 0,
                    )
    finally:
        conn.commit()
        conn.close()
    logger.info("Collection complete: %d/%d markets processed, %d errors", processed, len(markets), errors)
    return processed


def main() -> None:
    """Main entry point. --loop ile sonsuz dongu (bot entegrasyonu)."""
    parser = argparse.ArgumentParser(description="Collect orderbook for ALL active weather markets")
    parser.add_argument("--loop", action="store_true", help="run forever (for bot integration)")
    parser.add_argument("--interval", type=int, default=900, help="loop interval seconds (default 900)")
    parser.add_argument(
        "--workers",
        type=int,
        default=MAX_WORKERS,
        help="paralel HTTP worker sayisi (default %(default)s)",
    )
    args = parser.parse_args()

    logger.info(
        "=== Orderbook collection started (loop=%s, interval=%ds, workers=%d) ===",
        args.loop,
        args.interval,
        args.workers,
    )

    if not acquire_lock():
        return

    try:
        init_orderbook_db()

        if not args.loop:
            count = collect_once(args.workers)
            logger.info("=== Done: %d markets ===", count)
            return

        while True:
            try:
                count = collect_once(args.workers)
                logger.info("=== Cycle done: %d markets, next in %ds ===", count, args.interval)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Cycle error: %s", exc)
            time.sleep(args.interval)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
