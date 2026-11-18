import sqlite3
from datetime import datetime
from pathlib import Path
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_db_path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
db_path = get_db_path()
if "--yes" not in _sys.argv:
    print(f"Guvenlik: canli DB sifirlanacak -> {db_path}")
    print("Onay icin --yes ekle: python scripts/reset_bot.py --yes")
    _sys.exit(1)

conn = sqlite3.connect(db_path)
c = conn.cursor()

print("=== ONCE ===")
c.execute("SELECT COUNT(*) FROM bets")
print(f"Bets: {c.fetchone()[0]}")
c.execute("SELECT status, COUNT(*), SUM(realized_pnl) FROM bets GROUP BY status")
for row in c.fetchall():
    print(f"  {row[0]}: {row[1]} bets, pnl={row[2]}")
c.execute("SELECT COUNT(*) FROM weather_markets")
print(f"Markets: {c.fetchone()[0]}")
c.execute("SELECT status, COUNT(*) FROM weather_markets GROUP BY status")
for row in c.fetchall():
    print(f"  {row[0]}: {row[1]}")

# 1. Arsiv tablosu olustur
c.execute("""CREATE TABLE IF NOT EXISTS bets_archive AS SELECT * FROM bets WHERE 0""")
conn.commit()

# 2. Mevcut betleri arsivle
c.execute("INSERT INTO bets_archive SELECT * FROM bets")
archived = c.rowcount
print(f"\n Arsivlenen bet sayisi: {archived}")

# 3. Betleri sil
c.execute("DELETE FROM bets")
deleted = c.rowcount
print(f" Silinen bet sayisi: {deleted}")

# 4. Analizleri temizle
c.execute("DELETE FROM analyses")
print(f" Silinen analiz sayisi: {c.rowcount}")

# 5. Expired marketlari temizle
c.execute("DELETE FROM weather_markets WHERE status = 'expired'")
print(f" Silinen expired market sayisi: {c.rowcount}")

# 6. Portfolio'yu sifirla
c.execute(
    """UPDATE portfolio SET
    initial_value = 1000.0,
    current_value = 1000.0,
    cash_balance = 1000.0,
    total_value = 1000.0,
    total_realized_pnl = 0.0,
    total_won = 0,
    total_lost = 0,
    daily_pnl = 0.0,
    last_updated = ?
    WHERE id = 1""",
    (datetime.now().isoformat(),),
)
print(" Portfolio sifirlandi")

# 7. Market snapshots'i temizle (expired market'larin snapshot'lari)
c.execute("""DELETE FROM market_snapshots WHERE market_id NOT IN (SELECT id FROM weather_markets)""")
print(f" Temizlenen snapshot sayisi: {c.rowcount}")

conn.commit()

print("\n=== SONRA ===")
c.execute("SELECT COUNT(*) FROM bets")
print(f"Bets: {c.fetchone()[0]}")
c.execute("SELECT COUNT(*) FROM bets_archive")
print(f"Arsiv: {c.fetchone()[0]}")
c.execute("SELECT * FROM portfolio")
print(f"Portfolio: {c.fetchone()}")
c.execute("SELECT COUNT(*) FROM weather_markets")
print(f"Markets: {c.fetchone()[0]}")

conn.close()

print("\nReset tamamlandi!")
print("Bot yeniden baslatilmali.")
