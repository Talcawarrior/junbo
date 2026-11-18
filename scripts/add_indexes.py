"""Bot.db'ye eksik indexleri ekle — API timeout'larini cozer.

Kok neden (2026-08-17): analyses 127k satir indexsiz + weather_markets/
weather_forecasts indexsiz -> /api/markets ve /api/city-bets 10s+ timeout
(Chrome 'baglaniyor'da kaliyor).

Not: bot calisiyorken index eklemek WAL ile guvenli; ama guvenli tarafta
kalmak icin bot durdurulmus olmali. Bu script kisa surer.
"""

import sqlite3
import sys
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_db_path  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

db = sqlite3.connect(get_db_path())
cur = db.cursor()

indexes = [
    ("ix_analyses_should_bet", "CREATE INDEX IF NOT EXISTS ix_analyses_should_bet ON analyses(should_bet)"),
    ("ix_analyses_market_id", "CREATE INDEX IF NOT EXISTS ix_analyses_market_id ON analyses(market_id)"),
    ("ix_wm_status", "CREATE INDEX IF NOT EXISTS ix_wm_status ON weather_markets(status)"),
    ("ix_wm_target_date", "CREATE INDEX IF NOT EXISTS ix_wm_target_date ON weather_markets(target_date)"),
    ("ix_wm_city_code", "CREATE INDEX IF NOT EXISTS ix_wm_city_code ON weather_markets(city_code)"),
    ("ix_wm_city", "CREATE INDEX IF NOT EXISTS ix_wm_city ON weather_markets(city)"),
    (
        "ix_wf_city_metric_target",
        "CREATE INDEX IF NOT EXISTS ix_wf_city_metric_target ON weather_forecasts(city, metric, target_date)",
    ),
    ("ix_wf_fetched_at", "CREATE INDEX IF NOT EXISTS ix_wf_fetched_at ON weather_forecasts(fetched_at)"),
    (
        "ix_cal_city_metric",
        "CREATE INDEX IF NOT EXISTS ix_cal_city_metric ON historical_calibrations(city_code, metric)",
    ),
    ("ix_metar_city_day", "CREATE INDEX IF NOT EXISTS ix_metar_city_day ON metar_observations(city_code, day)"),
    # 2026-08-22 audit: activity_events (33k satir) indexsizdi. Read pattern:
    #   dashboard ORDER BY id DESC (PK), category='daily_summary' count,
    #   city=? AND category='peak_found' AND ts<=? (peak_found lookup).
    (
        "ix_activity_events_category",
        "CREATE INDEX IF NOT EXISTS ix_activity_events_category ON activity_events(category)",
    ),
    ("ix_activity_events_ts", "CREATE INDEX IF NOT EXISTS ix_activity_events_ts ON activity_events(ts)"),
]

for name, sql in indexes:
    try:
        t0 = __import__("time").time()
        cur.execute(sql)
        print(f"  EKLENDI {name} ({__import__('time').time() - t0:.2f}s)")
    except Exception as e:
        print(f"  HATA {name}: {e}")

db.commit()

# dogrula
print()
print("=== indexler ===")
for r in cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_%' ORDER BY name"):
    print(f"  {r[0]}")

db.close()
print("\nTamam.")
