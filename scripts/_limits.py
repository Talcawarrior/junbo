import sqlite3
import sys
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_db_path  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

db = sqlite3.connect(get_db_path())
db.row_factory = sqlite3.Row

# 17 Agu metar betleri
print("=== 17 Agu metar-peak betleri ===")
for r in db.execute(
    "SELECT b.id, b.city, b.strike_temp, b.entry_price, b.placed_at, b.status, w.target_date "
    "FROM bets b JOIN weather_markets w ON w.id=b.market_id "
    "WHERE w.target_date LIKE '2026-08-17%' AND b.order_id LIKE 'metar_%' "
    "ORDER BY b.placed_at"
):
    print(
        f"  bet#{r['id']} {r['city']:14s} {r['strike_temp']}C @{r['entry_price']:.3f} "
        f"at {str(r['placed_at'])[:19]} ({r['status']})"
    )

# METAR loop loglari son 2 saat
print("\n=== Son METAR loop loglari (hepsi) ===")
lines = open("logs/bot.log", encoding="utf-8", errors="replace").read().splitlines()
metar_logs = [
    ln
    for ln in lines
    if "metar" in ln.lower() and ("SCHEDULER_METAR" in ln or "metar_loop" in ln or "METAR poll" in ln)
]
for ln in metar_logs[-25:]:
    print(f"  {ln[:150]}")

# 17 Agu target'li marketlerde simdi peak kilitlenenler (TR sabah)
print("\n=== 17 Agu target'li, peak kilitlenen, bet acilmamis sehirler ===")
from collections import defaultdict  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from scrapers.metar import detect_peak  # noqa: E402

metar = defaultdict(list)
for r in db.execute("SELECT city_code, temp_c, day, obs_time FROM metar_observations"):
    try:
        dt = datetime.fromisoformat(str(r["obs_time"]))
        metar[(r["city_code"], r["day"])].append((dt.replace(tzinfo=timezone.utc).timestamp(), r["temp_c"]))
    except Exception:
        pass

# 17 Agu marketlerinde acik olan sehirler
markets = db.execute(
    "SELECT DISTINCT city_code FROM weather_markets "
    "WHERE target_date LIKE '2026-08-17%' AND status='open' AND city_code IS NOT NULL"
).fetchall()
# acik metar bet olan sehirler
bet_cities = set(
    r["city"]
    for r in db.execute(
        "SELECT DISTINCT b.city FROM bets b JOIN weather_markets w ON w.id=b.market_id "
        "WHERE w.target_date LIKE '2026-08-17%' AND b.order_id LIKE 'metar_%' AND b.status IN ('placed','open')"
    )
)

for m in markets:
    code = m["city_code"]
    series = sorted(metar.get((code, "2026-08-17"), []))
    if len(series) < 3:
        continue
    lon = None
    for r in db.execute("SELECT longitude FROM weather_markets WHERE city_code=? LIMIT 1", (code,)):
        lon = r["longitude"]
    offset = round(float(lon) / 15.0) if lon else 0.0
    peak, confirmed = detect_peak(series, utc_offset_hours=offset)
    if not confirmed or peak is None:
        continue
    city = None
    for r in db.execute("SELECT city FROM weather_markets WHERE city_code=? LIMIT 1", (code,)):
        city = r["city"]
    hit = city in bet_cities
    if not hit:
        print(f"  {code} ({city}): peak={peak:.0f}C KILITLI ama METAR bet YOK")
