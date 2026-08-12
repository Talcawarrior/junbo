"""Bot stratejisine gore gun gun tablo: meteo merkez +/-3 spread, CLOB fiyatindan giris,
gercek outcome (parse_resolved_outcome). Kazanan kazanc, kaybeden -2.10, toplam kolon."""

import sqlite3
import shutil
import tempfile
import os
import sys

sys.path.insert(0, r"C:\Users\fdemir\Documents\New project\junbo")
from utils.market_outcome import parse_resolved_outcome
from collections import defaultdict

BOT_DB = r"C:\Users\fdemir\Documents\New project\junbo\data\bot.db"
STAKE = 2.0
FEE = 0.05
GAS = 0.10
SPREAD_R = 3
MAX_ENTRY = 0.95

db = sqlite3.connect(BOT_DB)
cur = db.cursor()

# city_code -> city
cur.execute("SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''")
code_name = {}
for c, code in cur.fetchall():
    if code and c:
        code_name.setdefault(code, c)

# resolved marketler + gercek outcome (code, day, thr) -> yes_won
markets = {}
for r in cur.execute(
    "SELECT city_code, metric, threshold, target_date, raw_data FROM weather_markets "
    "WHERE status='expired' AND raw_data IS NOT NULL"
).fetchall():
    code, metric, thr, tdate, raw = r
    day = str(tdate)[:10] if tdate else None
    if not day or not code or thr is None or "max" not in (metric or ""):
        continue
    outcome = parse_resolved_outcome(raw)
    if outcome is None:
        continue
    markets[(code, day, float(thr))] = outcome

# CLOB fiyati: market_id -> (code, day, thr) map + best_ask ilk
wm = {}
for r in cur.execute(
    "SELECT id, city_code, metric, threshold, target_date FROM weather_markets "
    "WHERE threshold IS NOT NULL AND target_date IS NOT NULL AND city_code IS NOT NULL"
).fetchall():
    mid, code, metric, thr, tdate = r
    day = str(tdate)[:10] if tdate else None
    if "max" not in (metric or ""):
        continue
    wm[str(mid)] = (code, day, float(thr))

# orderbook kopyala (kilitli)
fd, tmp = tempfile.mkstemp(suffix=".db")
os.close(fd)
shutil.copy2(r"C:\Users\fdemir\Documents\New project\junbo\data\orderbook.db", tmp)
ob = sqlite3.connect(tmp, timeout=10)
clob = {}  # (code, day, thr) -> best_ask (ilk gorulen)
for mid, ask, stime in ob.execute(
    "SELECT market_id, best_ask, snapshot_time FROM orderbook_snapshots WHERE best_ask IS NOT NULL "
    "ORDER BY snapshot_time ASC"
).fetchall():
    key = wm.get(str(mid))
    if key is None:
        continue
    try:
        a = float(ask)
        if 0 < a < 1:
            clob.setdefault(key, a)
    except (TypeError, ValueError):
        pass
ob.close()
os.unlink(tmp)

# forecast merkezi (ilk ensemble per code, day)
fc = {}
for r in cur.execute(
    "SELECT city, target_date, source, predicted_value, fetched_at FROM weather_forecasts "
    "WHERE predicted_value IS NOT NULL AND metric LIKE '%max%' ORDER BY fetched_at ASC"
).fetchall():
    code, tdate, src, pv, ft = r
    day = str(tdate)[:10] if tdate else None
    if not day:
        continue
    key = (code, day)
    fc.setdefault(key, {})
    if src not in fc[key]:
        fc[key][src] = float(pv)
db.close()

# gun gun: bot stratejisi = meteo merkez +/- 3, CLOB giris, gercek outcome
by_day = defaultdict(list)
for (code, day), models in fc.items():
    vals = list(models.values())
    if not vals:
        continue
    center = round(sum(vals) / len(vals))
    for thr in range(center - SPREAD_R, center + SPREAD_R + 1):
        key = (code, day, float(thr))
        if key not in markets or key not in clob:
            continue
        entry = clob[key]
        if not (0 < entry < MAX_ENTRY):
            continue
        yes_won = markets[key]
        fee = STAKE * FEE * (1.0 - entry) if FEE > 0 else 0.0
        cost = STAKE + fee + GAS
        gain = (STAKE / entry - cost) if yes_won else -cost
        by_day[day].append(
            {
                "city": code_name.get(code, code),
                "thr": thr,
                "entry": entry,
                "won": yes_won,
                "gain": gain,
            }
        )

for day in sorted(by_day):
    bets = by_day[day]
    day_pnl = sum(b["gain"] for b in bets)
    day_won = sum(1 for b in bets if b["won"])
    print(f"\n{'='*80}")
    print(
        f"=== {day} | bet={len(bets)} kazanan={day_won} kaybeden={len(bets)-day_won} "
        f"win-rate={day_won/max(len(bets),1)*100:.1f}% | TOPLAM=${day_pnl:.2f}"
    )
    print(f"{'='*80}")
    for b in sorted(bets, key=lambda x: x["thr"]):
        mark = "KAZANDI" if b["won"] else "kaybetti"
        print(f"  {b['city']:<16} esik={b['thr']:>5.1f}C giris={b['entry']:.4f}  {mark:8}  {b['gain']:+.2f}")
