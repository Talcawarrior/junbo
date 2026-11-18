"""Polymarket CLOB prices-history ile gecmis fiyat toplama (backtest icin).

Sorun: market_snapshots (30dk) ve orderbook (5dk) bazi marketlerde
settlement-oncesi veri kapsamiyor; bot canli giris fiyatini bu verilerden
okudugu icin backtest eksik kalabiliyor. CLOB `/prices-history?interval=max`
marketin TUM fiyat gecmisini verir (her islem/snapshot noktasi).

Bu script bot.db weather_markets'teki `clobTokenIds[0]` (YES token) ile her
marketin fiyat gecmisini ceker ve `data/backtest_prices.db` `price_history`
tablosuna yazar. Botun POLY_PROXY (WARP socks5h) uzerinden cekilir;
direct cekim Cloudflare'da 403/10054 veriyor (kontrol edildi 2026-08-18).

Kullanim:
    python scripts/backfill_price_history.py --days 2026-08-16,2026-08-17
    python scripts/backfill_price_history.py --days 2026-08-16,2026-08-17 --limit 5
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_data_dir, get_db_path  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from config.settings import bot_config  # noqa: E402

BOT_DB = get_db_path()
OUT_DB = os.path.join(get_data_dir(), "backtest_prices.db")

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"


def _opener():
    proxy = getattr(bot_config.polymarket, "proxy", "") or os.environ.get("POLY_PROXY", "")
    if not proxy:
        proxy = "socks5h://127.0.0.1:40000"
    op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    op.addheaders = [("User-Agent", _UA), ("Accept", "application/json")]
    return op


def fetch_history(opener, token_id: str, timeout: float = 25.0) -> list[tuple[float, float]]:
    """interval=max: marketin tum fiyat gecmisi [(ts_sec, yes_price), ...]."""
    url = f"https://clob.polymarket.com/prices-history?market={token_id}&interval=max"
    with opener.open(url, timeout=timeout) as r:
        d = json.loads(r.read())
    out: list[tuple[float, float]] = []
    for row in d.get("history", []) or []:
        try:
            t = float(row["t"])
            p = float(row["p"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 < p <= 1:
            out.append((t, p))
    out.sort(key=lambda x: x[0])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="CLOB prices-history backfill")
    parser.add_argument("--days", default="2026-08-16,2026-08-17", help="virgullu target gunler")
    parser.add_argument("--metric", default="", help="bos = tum metricler; 'temperature_max' gibi")
    parser.add_argument("--delay", type=float, default=0.25, help="istek arasi saniye")
    parser.add_argument("--limit", type=int, default=0, help="test: yalnizca ilk N market")
    parser.add_argument("--reset", action="store_true", help="hedef gunlerin eski kayitlarini sil")
    args = parser.parse_args()
    days = [d.strip() for d in args.days.split(",") if d.strip()]

    db = sqlite3.connect(BOT_DB, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    q = "SELECT id, city, metric, raw_data FROM weather_markets WHERE raw_data IS NOT NULL"
    if days:
        like = " OR ".join(["target_date LIKE ?"] * len(days))
        q += f" AND ({like})"
    params = [f"{d}%" for d in days]
    if args.metric:
        q += " AND metric = ?"
        params.append(args.metric)
    markets = list(db.execute(q, params))
    db.close()

    out = sqlite3.connect(OUT_DB)
    out.execute(
        "CREATE TABLE IF NOT EXISTS price_history "
        "(market_id INTEGER NOT NULL, ts REAL NOT NULL, price REAL NOT NULL, "
        "PRIMARY KEY (market_id, ts))"
    )
    if args.reset and days:
        like = " OR ".join(["target_date LIKE ?"] * len(days))
        m_ids = [r[0] for r in markets]
        if m_ids:
            out.executemany("DELETE FROM price_history WHERE market_id = ?", [(m,) for m in m_ids])
            out.commit()

    opener = _opener()
    fetched, skipped, failed = 0, 0, 0
    total = len(markets)
    for i, (mid, city, metric, raw) in enumerate(markets, 1):
        try:
            rd = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            skipped += 1
            continue
        toks_raw = rd.get("clobTokenIds")
        if not toks_raw:
            skipped += 1
            continue
        # clobTokenIds raw_data'da CIKTI JSON dizisi olarak saklanir (string);
        # gercek liste icin yeniden parse edilir.
        if isinstance(toks_raw, str):
            try:
                toks = json.loads(toks_raw)
            except (json.JSONDecodeError, TypeError):
                skipped += 1
                continue
        else:
            toks = toks_raw
        if not toks:
            skipped += 1
            continue
        token_id = str(toks[0])  # YES token
        if args.limit and i > args.limit:
            break
        rows = []
        for attempt in range(3):
            try:
                rows = fetch_history(opener, token_id)
                break
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt == 2:
                    print(f"  FAIL {mid} {city} ({metric}): {type(e).__name__} {e}")
                    failed += 1
                    rows = []
                else:
                    time.sleep(1.5 * (attempt + 1))
        if rows:
            out.executemany(
                "INSERT OR REPLACE INTO price_history (market_id, ts, price) VALUES (?, ?, ?)",
                [(mid, t, p) for t, p in rows],
            )
            out.commit()
            fetched += 1
        if i % 50 == 0 or i == total:
            print(f"  [{i}/{total}] fetched={fetched} skipped={skipped} failed={failed}")
        time.sleep(args.delay)

    out.close()
    print(f"\nDONE: fetched={fetched} skipped={skipped} failed={failed} (total={total})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
