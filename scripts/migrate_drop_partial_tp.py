"""DB migration: bets.partial_tp_done kolonunu kaldir.

Bug (2026-08-14): SL/TP temizliginde `database/models.py`'den partial_tp_done
kaldirildi ama DB semasinda NOT NULL kolon olarak kaldi. spread_placer yeni bet
acarken bu kolonu set etmiyor -> NOT NULL constraint hatasi -> BOT BET ACAMIYOR.

SQLite ALTER TABLE DROP COLUMN (3.35+) ile kaldirilir. Veri kaybi yok (kolon
zaten kullanilmiyor, tum degerler default False).

Kullanim:
    python scripts/migrate_drop_partial_tp.py
"""
import os
import sqlite3
import shutil
import sys
from datetime import datetime

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT_DB = os.path.join(_REPO_ROOT, "data", "bot.db")


def main() -> int:
    if not os.path.exists(BOT_DB):
        print("HATA: bot.db yok")
        return 1

    # yedek al (guvenlik)
    backup = os.path.join(_REPO_ROOT, "data", "backups", f"bot_pre_droptp_{datetime.now():%Y%m%d_%H%M%S}.db")
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    shutil.copy2(BOT_DB, backup)
    print(f"yedek: {backup}")

    db = sqlite3.connect(BOT_DB)
    cur = db.cursor()

    # kolon var mi?
    cols = [r[1] for r in cur.execute("PRAGMA table_info(bets)")]
    if "partial_tp_done" not in cols:
        print("partial_tp_done kolonu zaten yok")
        db.close()
        return 0

    # foreign key'leri gecici devre disi birak (DROP COLUMN oncesi)
    cur.execute("PRAGMA foreign_keys=OFF")
    cur.execute("BEGIN")
    try:
        cur.execute("ALTER TABLE bets DROP COLUMN partial_tp_done")
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"DROP COLUMN hatasi: {exc}")
        print("Alternatif: kolonu nullable yap (default False)")
        db.close()
        return 1

    # dogrula
    cols2 = [r[1] for r in cur.execute("PRAGMA table_info(bets)")]
    if "partial_tp_done" in cols2:
        print("KOLON SILINEMEDI")
        db.close()
        return 1

    # test insert (spread_placer gibi)
    try:
        cur.execute(
            "INSERT INTO bets (market_id, city, city_code, side, amount, stake_amount, entry_price, "
            "shares, current_price, status, price, placed_at) "
            "VALUES ('test_mig', 'Testville', 'TEST', 'YES', 2.0, 2.0, 0.5, 4.0, 0.5, 'placed', 0.5, "
            "datetime('now'))"
        )
        db.commit()
        cur.execute("DELETE FROM bets WHERE market_id='test_mig'")
        db.commit()
        print("test insert OK — partial_tp_done artik yok, bet acilabilir")
    except Exception as exc:  # noqa: BLE001
        print(f"test insert HATA: {exc}")
        db.close()
        return 1

    db.close()
    print("MIGRATION BASARILI: bets.partial_tp_done kaldirildi")
    return 0


if __name__ == "__main__":
    sys.exit(main())
