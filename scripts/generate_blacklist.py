"""Generate model + city blacklist from backtest data.

Usage: python scripts/generate_blacklist.py
"""
import json
import sqlite3
import sys; sys.path.insert(0, r"C:\Users\fdemir\Documents\New project\junbo")
from database.db import DB_PATH

MAE_THRESHOLD = 2.0  # exclude model if MAE > 2.0C for a city
CITY_MAE_THRESHOLD = 1.5  # exclude city if avg MAE > 1.5C (after exclusions)
MIN_MODELS = 2  # city needs at least this many usable models

conn = sqlite3.connect(DB_PATH)

# Get per-city per-model MAE for temperature_max (most important metric)
rows = conn.execute("""
    SELECT city_code, model,
           ROUND(AVG(ABS(bias)), 3) as mae,
           ROUND(AVG(bias), 3) as avg_bias,
           COUNT(*) as n
    FROM historical_calibrations
    WHERE metric = 'temperature_max'
    GROUP BY city_code, model
    ORDER BY city_code, mae
""").fetchall()

# Build per-city stats
city_data = {}
for city_code, model, mae, avg_bias, n in rows:
    if city_code not in city_data:
        city_data[city_code] = {"all_models": [], "blacklisted_models": []}
    city_data[city_code]["all_models"].append({
        "model": model, "mae": mae, "avg_bias": avg_bias, "samples": n,
        "blacklisted": mae > MAE_THRESHOLD
    })
    if mae > MAE_THRESHOLD:
        city_data[city_code]["blacklisted_models"].append(model)

# Build output
model_blacklist = {}
city_blacklist = []

for city_code, data in city_data.items():
    # List models to exclude
    bl_models = [m["model"] for m in data["all_models"] if m["blacklisted"]]
    if bl_models:
        model_blacklist[city_code] = sorted(bl_models)
    
    # Check city viability: remaining models = all - blacklisted
    remaining = [m for m in data["all_models"] if not m["blacklisted"]]
    if len(remaining) < MIN_MODELS:
        city_blacklist.append(city_code)
        print(f"CITY BLACKLIST: {city_code} — only {len(remaining)}/{len(data['all_models'])} models usable")
        continue
    
    avg_mae = sum(m["mae"] for m in remaining) / len(remaining)
    if avg_mae > CITY_MAE_THRESHOLD:
        city_blacklist.append(city_code)
        print(f"CITY BLACKLIST: {city_code} — avg MAE={avg_mae:.3f} (threshold={CITY_MAE_THRESHOLD})")
        continue

    if bl_models:
        print(f"MODEL BLACKLIST: {city_code} -> {bl_models}")

# Also add min temp blacklist
min_rows = conn.execute("""
    SELECT city_code, model,
           ROUND(AVG(ABS(bias)), 3) as mae
    FROM historical_calibrations
    WHERE metric = 'temperature_min'
    GROUP BY city_code, model
    ORDER BY city_code, mae
""").fetchall()

for city_code, model, mae in min_rows:
    if city_code not in model_blacklist:
        model_blacklist[city_code] = []
    # Only add min-temp specific blacklist if model not already blacklisted for max
    if mae > MAE_THRESHOLD and model not in model_blacklist.get(city_code, []):
        model_blacklist.setdefault(city_code, []).append(f"{model}_min")
        print(f"MODEL BLACKLIST (min): {city_code} -> {model} (MAE={mae:.3f})")

output = {
    "model_blacklist": model_blacklist,
    "city_blacklist": sorted(set(city_blacklist)),
    "thresholds": {
        "model_mae": MAE_THRESHOLD,
        "city_avg_mae": CITY_MAE_THRESHOLD,
        "min_models": MIN_MODELS,
    },
    "generated_at": "2026-07-26"
}

path = "data/model_blacklist.json"
with open(path, "w") as f:
    json.dump(output, f, indent=2)

print(f"\nWritten to {path}")
print(f"Model blacklist entries: {len(model_blacklist)} cities")
print(f"City blacklist: {len(city_blacklist)} cities: {sorted(set(city_blacklist))}")

conn.close()
