"""
Gercek backtest engine: Poly settlement + orderbook + bias duzeltmeli
Son 12 gun, tum kaynak*sehir kombinasyonlari, her kombinasyon ayri.
Kafadan sallama yok - DB'den gelen veriyle.
"""

import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from utils.db_paths import get_data_dir

# 2026-11-18: D: sabit varsayilani kaldirildi (canli veri C:\JUNBO data'ya tasindi;
# D: kopyasi donmus yedek). get_data_dir(): JUNBO_DATA_DIR > .env DB_PATH dizini.
DATA_DIR = Path(get_data_dir())
BOT_DB = Path(os.getenv("JUNBO_BOT_DB", str(DATA_DIR / "bot.db")))
OB_DB = Path(os.getenv("JUNBO_ORDERBOOK_DB", str(DATA_DIR / "orderbook.db")))
ACTUALS_DB = Path(os.getenv("JUNBO_ACTUALS_DB", str(DATA_DIR / "actuals.db")))

STAKE = 2.0
FEE_RATE = 0.02  # yaklasik


def _actuals_fallback(cutoff: str) -> dict:
    """`data/actuals.db`'den (sehir, gun) -> gercek tmax.

    `bets.outcome` NULL kaldiginda settlement kaynagi buradan gelir. Dosya
    yoksa/okunamiyorsa bos sozluk doner (backtest yine calisir).
    """
    if not ACTUALS_DB.exists():
        return {}
    out: dict = {}
    try:
        con = sqlite3.connect(f"file:{ACTUALS_DB}?mode=ro", uri=True, timeout=30)
        try:
            rows = con.execute(
                "SELECT city, date, temperature_2m_max FROM actual_temperatures "
                "WHERE temperature_2m_max IS NOT NULL AND date >= ?",
                (cutoff,),
            )
            for city, day, tmax in rows:
                if city and day:
                    out[(str(city), str(day)[:10])] = float(tmax)
        finally:
            con.close()
    except (sqlite3.Error, ValueError, TypeError):
        return {}
    return out


def load_data(cutoff_days: int = 12):  # noqa: C901
    bot = sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True, timeout=30)
    cur = bot.cursor()
    cutoff = (datetime.now() - timedelta(days=cutoff_days + 2)).strftime("%Y-%m-%d")
    cur.execute(
        "SELECT DISTINCT city, city_code FROM weather_markets "
        "WHERE city IS NOT NULL AND city_code IS NOT NULL AND city_code!=''"
    )
    city_icao = {c: ic for c, ic in cur.fetchall() if c and ic}
    cur.execute(
        "SELECT city, source, target_date, predicted_value FROM weather_forecasts "
        "WHERE metric='temperature_max' AND predicted_value IS NOT NULL "
        "AND target_date >= ?",
        (cutoff,),
    )
    fc = {}
    for icao, src, td, pv in cur.fetchall():
        try:
            fc[(icao, str(td)[:10], src)] = float(pv)
        except (ValueError, TypeError):
            continue
    cur.execute(
        "SELECT city, model, bias FROM historical_calibrations WHERE metric='temperature_max' AND bias IS NOT NULL"
    )
    bias_map = {}
    for city, model, b in cur.fetchall():
        try:
            bias_map[(city, model)] = float(b)
        except (ValueError, TypeError):
            continue
    # 2026-10-27 (S21): iki gercek kusur.
    # (1) `outcome` 2026-09-29 sonrasi betlerde NULL kaldi -> `actual` bos
    #     donuyor ve test_load_data_has_settlement "settlement az: 0" veriyordu.
    #     `data/actuals.db` (8.691 satir) DOLU; oradan yedeklenir.
    # (2) Gun secimi `settled_at` (yerlesim ANI) idi; UMA gec cozdugunde
    #     onceki gunun sicakligi yaziliyordu. Marketin `target_date`'i esastir
    #     (executor/settler.py ile ayni kural).
    cur.execute(
        "SELECT b.city, b.settled_at, b.outcome, m.target_date FROM bets b "
        "LEFT JOIN weather_markets m ON b.market_id = m.id "
        "WHERE b.settled_at IS NOT NULL AND b.settled_at >= ?",
        (cutoff,),
    )
    actual = {}
    for city, settled, outcome, target_date in cur.fetchall():
        if not city or not outcome:
            continue
        day = str(target_date)[:10] if target_date else str(settled)[:10]
        try:
            t = json.loads(outcome).get("settlement_temp_c")
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
            continue
        if t is not None:
            actual[(city, day)] = float(t)
    for key, value in _actuals_fallback(cutoff).items():
        actual.setdefault(key, value)
    cur.execute(
        "SELECT id, city_code, target_date, threshold FROM weather_markets "
        "WHERE threshold IS NOT NULL AND target_date IS NOT NULL"
    )
    mid_to_key = {}
    for mid, code, tdate, thr in cur.fetchall():
        try:
            mid_to_key[str(mid)] = (code, str(tdate)[:10], float(thr))
        except (ValueError, TypeError):
            continue
    bot.close()
    # Live collector writes orderbook.db in DELETE journal mode; a big commit
    # can hold an exclusive lock for minutes -> wait longer instead of failing.
    ob = sqlite3.connect(f"file:{OB_DB}?mode=ro", uri=True, timeout=120)
    ob.execute("PRAGMA busy_timeout=120000")
    oc = ob.cursor()
    oc.execute("SELECT market_id, best_ask, snapshot_time FROM orderbook_snapshots WHERE best_ask IS NOT NULL")
    ob_series: dict[str, list] = defaultdict(list)
    for mid, ask, st in oc.fetchall():
        try:
            a = float(ask)
            if 0 < a < 1:
                s = str(st).replace("T", " ").replace("+00:00", "").strip()
                try:
                    ts = datetime.fromisoformat(s).timestamp()
                except ValueError:
                    continue
                ob_series[str(mid)].append((ts, a))
        except (ValueError, TypeError):
            continue
    ob.close()
    for k in ob_series:
        ob_series[k].sort(key=lambda x: x[0])
    return city_icao, fc, bias_map, actual, mid_to_key, ob_series, cutoff


def evaluate_source_city(fc, bias_map, actual, city_icao, city, src, cutoff_days=12):
    icao = city_icao.get(city)
    if not icao:
        return None
    wins = losses = 0
    total_pnl = 0.0
    total_stake = 0.0
    details = []
    for (act_city, day), act_temp in actual.items():
        if act_city != city:
            continue
        raw = fc.get((icao, day, src))
        if raw is None:
            continue
        bias = bias_map.get((city, src), 0.0)
        calibrated = raw - bias
        strike = round(calibrated, 1)
        win = act_temp > strike
        if win:
            wins += 1
            pnl = (STAKE / 0.5) - STAKE - STAKE * FEE_RATE
        else:
            losses += 1
            pnl = -STAKE - STAKE * FEE_RATE
        total_pnl += pnl
        total_stake += STAKE
        details.append((day, raw, bias, calibrated, strike, act_temp, win, pnl))
    n = wins + losses
    if n == 0:
        return None
    return {
        "city": city,
        "src": src,
        "n": n,
        "w": wins,
        "l": losses,
        "wr": wins / n * 100,
        "pnl": total_pnl,
        "stake": total_stake,
        "roi": total_pnl / total_stake * 100 if total_stake else 0,
        "details": details,
    }


def evaluate_all(fc, bias_map, actual, city_icao, sources, cutoff_days=12):
    results = {}
    for src in sources:
        for city in city_icao:
            r = evaluate_source_city(fc, bias_map, actual, city_icao, city, src, cutoff_days)
            if r:
                results[(city, src)] = r
    return results


def load_source_offsets() -> dict:
    """HEAT-ours yerlesim offsetleri (config/source_offsets.json).

    2026-09-18 olcumu: ham ayni-bucket %25, offset-duzeltmeli %47.
    Capraz-kaynak backtest SARTI: HEAT degeri normalize edilmeden
    kullanilamaz (4 tur bosa kosuldu).
    """
    import json as _json

    try:
        with open(BOT_DB.parent.parent / "config" / "source_offsets.json", encoding="utf-8") as fh:
            return _json.load(fh)
    except Exception:
        return {}


def normalized_heat_settlement(city: str, heat_temp: float, offsets: dict | None = None) -> float:
    """HEAT yerlesimini bizim olcege cevir (sehir medyan offseti cikar)."""
    offsets = offsets if offsets is not None else load_source_offsets()
    try:
        off = float((offsets.get("cities", {}).get(city) or {}).get("median_offset_c", 0.0))
    except Exception:
        off = 0.0
    return float(heat_temp) - off


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    city_icao, fc, bias_map, actual, mid_to_key, ob_series, cutoff = load_data(12)
    print(f"cutoff {cutoff} cities {len(city_icao)} fc {len(fc)} bias {len(bias_map)} actual {len(actual)}")
