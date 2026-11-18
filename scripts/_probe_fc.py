import sqlite3
import sys
from collections import defaultdict
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_db_path  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

db = sqlite3.connect(get_db_path())
db.row_factory = sqlite3.Row

# 17 Agu'da kapatilan betler: satis fiyati, entry, shares, realized_pnl
print("=== 17 Agu KAPATILAN 54 bet — satis fiyati + PnL ===")
closed = db.execute(
    "SELECT b.id, b.city, b.strike_temp, b.entry_price, b.shares, b.amount, "
    "b.entry_fee, b.realized_pnl, b.close_reason, b.closed_at, "
    "w.yes_price as market_now, w.threshold "
    "FROM bets b JOIN weather_markets w ON w.id=b.market_id "
    "WHERE w.target_date LIKE '2026-08-17%' AND b.status='closed' AND b.close_reason='rotation' "
    "ORDER BY b.closed_at, b.city"
).fetchall()

print(
    f"{'bet#':>6s} {'sehir':14s} {'esik':>5s} {'entry':>7s} "
    f"{'shares':>7s} {'stake':>6s} {'realized_pnl':>12s} {'kapandigi':>10s}"
)
total_pnl = 0.0
total_stake = 0.0
by_city = defaultdict(lambda: {"n": 0, "pnl": 0.0, "stake": 0.0})
for r in closed:
    pnl = float(r["realized_pnl"] or 0)
    stake = float(r["amount"] or 0)
    total_pnl += pnl
    total_stake += stake
    by_city[r["city"]]["n"] += 1
    by_city[r["city"]]["pnl"] += pnl
    by_city[r["city"]]["stake"] += stake
    print(
        f"{r['id']:>6d} {r['city']:14s} {r['strike_temp']:>5.0f} "
        f"{float(r['entry_price'] or 0):>7.3f} {float(r['shares'] or 0):>7.1f} "
        f"{stake:>6.2f} {pnl:>+12.2f} {str(r['closed_at'])[11:16]:>10s}"
    )

print()
print(f"TOPLAM kapatilan: {len(closed)} bet, stake ${total_stake:.2f}, NET PnL ${total_pnl:+.2f}")
print(f"Stake basina ROI: %{total_pnl / max(total_stake, 1) * 100:.1f}")

print()
print("=== Sehir bazli ===")
for city, st in sorted(by_city.items(), key=lambda kv: kv[1]["pnl"]):
    print(f"  {city:14s} {st['n']:3d} bet, stake ${st['stake']:.2f}, PnL ${st['pnl']:+.2f}")
