"""Yerlesmis betlerin `outcome` alanini actuals.db'den geriye donuk doldurur.

2026-10-27 (S15): `executor/settler.py` gercek sicakligi yalnizca yerlesim ANINDA
yaziyordu; `data/actuals.db` 2026-09-28'de dondugu icin 2026-09-29 sonrasi
betlerin `outcome` alani NULL kaldi (~298 bet). Backtest ve
tests/test_settlement_gate.py bu alani okudugu icin oralar bos donuyordu.

Bu betik `bets.outcome` alanini, settler ile AYNI JSON semasinda doldurur:
    {"settlement_temp_c": float, "settlement_temp_f": float,
     "source": "wu_weather_underground", "collected_at": ISO8601}

Gun secimi: marketin `target_date`'i (yerlesim aninin tarihi DEGIL). UMA gec
cozdugunde onceki hal yanlis gunun sicakligini yaziyordu.

Kullanim:
    python scripts/backfill_bet_outcomes.py            # yaz
    python scripts/backfill_bet_outcomes.py --dry-run  # sadece raporla
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_data_dir, get_db_path  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT_DB = get_db_path()
ACTUALS_DB = os.path.join(get_data_dir(), "actuals.db")

SOURCE = "wu_weather_underground"


def _load_actuals() -> dict[tuple[str, str], float]:
    """(sehir, YYYY-MM-DD) -> temperature_2m_max."""
    con = sqlite3.connect(ACTUALS_DB)
    cur = con.cursor()
    cur.execute("SELECT city, date, temperature_2m_max FROM actual_temperatures WHERE temperature_2m_max IS NOT NULL")
    out = {}
    for city, day, temp in cur.fetchall():
        if city and day:
            out[(str(city), str(day)[:10])] = float(temp)
    con.close()
    return out


def _target_bets(con: sqlite3.Connection) -> list[tuple]:
    """outcome'i bos, yerlesmis betler + marketin hedef gunu."""
    cur = con.cursor()
    cur.execute(
        "SELECT b.id, b.city, b.settled_at, m.target_date "
        "FROM bets b LEFT JOIN weather_markets m ON b.market_id = m.id "
        "WHERE b.status IN ('won','lost') AND b.outcome IS NULL"
    )
    return cur.fetchall()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="yazma, sadece raporla")
    args = parser.parse_args()

    if not os.path.exists(BOT_DB):
        print(f"HATA: bot DB yok: {BOT_DB}")
        return 1
    if not os.path.exists(ACTUALS_DB):
        print(f"HATA: actuals DB yok: {ACTUALS_DB}")
        return 1

    actuals = _load_actuals()
    con = sqlite3.connect(BOT_DB)
    rows = _target_bets(con)
    print(f"outcome bos yerlesmis bet: {len(rows)}  |  actuals kaydi: {len(actuals)}")

    now_iso = datetime.now(timezone.utc).isoformat()
    filled = 0
    missing: dict[tuple[str, str], int] = {}

    for bet_id, city, settled_at, target_date in rows:
        # Market gunu esas; yoksa yerlesim aninin gunune dus (eski davranis).
        day = str(target_date)[:10] if target_date else None
        if not day and settled_at:
            day = str(settled_at)[:10]
        if not city or not day:
            missing[(str(city or "?"), str(day or "?"))] = missing.get((str(city or "?"), str(day or "?")), 0) + 1
            continue

        temp = actuals.get((str(city), day))
        if temp is None:
            key = (str(city), day)
            missing[key] = missing.get(key, 0) + 1
            continue

        payload = json.dumps(
            {
                "settlement_temp_c": temp,
                "settlement_temp_f": temp * 9 / 5 + 32,
                "source": SOURCE,
                "collected_at": now_iso,
            }
        )
        if not args.dry_run:
            con.execute("UPDATE bets SET outcome = ? WHERE id = ?", (payload, bet_id))
        filled += 1

    if not args.dry_run:
        con.commit()
    con.close()

    print(f"{'doldurulacak' if args.dry_run else 'dolduruldu'}: {filled}")
    print(f"eslesmeyen: {sum(missing.values())}")
    for (city, day), n in sorted(missing.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {city:20s} {day}  x{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
