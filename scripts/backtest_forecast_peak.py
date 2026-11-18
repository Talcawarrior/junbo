"""Walk-forward Peak+1 backtest using weather forecasts.

Two changes vs backtest_peak_trail.py:
1. Entry bucket = ensemble forecast average (not METAR observation)
2. City selection rolls forward: each eval day uses the previous 20 days
   of METAR-based pair performance to pick the top cities.

Forecast data starts 2026-09-12; eval window is limited to available
forecast dates.
"""

from __future__ import annotations

import argparse
import bisect
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_db_path  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

sys.stdout.reconfigure(encoding="utf-8")

BOT_DB = Path(get_db_path())
BACKTEST_DB = REPO_ROOT / "data" / "backtest.db"
ACTUALS_DB = REPO_ROOT / "data" / "actuals.db"
ORDERBOOK_DB = REPO_ROOT / "data" / "orderbook.db"

FEE_RATE = 0.05
GAS_USD = 0.10
MIN_ENTRY = 0.05
MAX_ENTRY = 0.95

WEIGHTS = {
    "ecmwf_ifs025": 0.30,
    "ukmo_seamless": 0.08,
    "icon_global": 0.10,
    "gem_global": 0.08,
    "gfs_seamless": 0.08,
    "meteofrance_seamless": 0.05,
    "jma_seamless": 0.05,
    "cma_grapes_global": 0.03,
    "openweathermap": 0.08,
    "visual_crossing": 0.06,
    "weatherapi_com": 0.04,
    "nws_hourly": 0.03,
    "heat_mos": 0.02,
}

# bias-analizden cikarilan en iyi kaynak-sehir eslesmeleri
BEST_SOURCE_BY_CITY = {
    "Tel Aviv": "weatherapi_com",
    "Seoul": "visual_crossing",
    "Wellington": "meteofrance_seamless",
    "Chongqing": "visual_crossing",
    "Wuhan": "ecmwf_ifs025",
    "Buenos Aires": "visual_crossing",
    "Istanbul": "gem_global",
    "Amsterdam": "visual_crossing",
    "Dallas": "ecmwf_ifs025",
    "Moscow": "gem_global",
}


def to_epoch(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def half_up(x: float) -> int:
    return int(x + 0.5)


def iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def open_orderbook(path: Path) -> sqlite3.Connection:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
        con.execute("PRAGMA busy_timeout=60000")
        con.execute("SELECT 1 FROM orderbook_snapshots LIMIT 1").fetchone()
        return con
    except sqlite3.OperationalError:
        print("orderbook locked; copying via backup...", flush=True)
        tmp = Path(tempfile.gettempdir()) / "ob_fc_peak.db"
        if tmp.exists():
            tmp.unlink()
        src = sqlite3.connect(path, timeout=60)
        src.execute("PRAGMA busy_timeout=60000")
        dst = sqlite3.connect(tmp, timeout=60)
        src.backup(dst)
        dst.close()
        src.close()
        print("copy ready.", flush=True)
        con = sqlite3.connect(tmp, timeout=60)
        con.execute("PRAGMA busy_timeout=60000")
        return con


def quote_at_or_after(con, mid: str, ts_epoch: float, side: str, window_sec: int):
    bound = iso_utc(ts_epoch)
    rows = con.execute(
        "SELECT best_bid, best_ask, bid_depth_usd, ask_depth_usd, snapshot_time "
        "FROM orderbook_snapshots WHERE market_id=? AND snapshot_time>=? "
        "ORDER BY snapshot_time LIMIT 40",
        (str(mid), bound),
    ).fetchall()
    for bid, ask, bid_depth, ask_depth, stime in rows:
        t = to_epoch(stime)
        if t is None or t - ts_epoch > window_sec:
            break
        if side == "ask":
            try:
                price = float(ask)
            except (TypeError, ValueError):
                continue
            if 0 < price < 1:
                return price, ask_depth, t
        else:
            try:
                price = float(bid)
            except (TypeError, ValueError):
                continue
            if 0 < price < 1:
                return price, bid_depth, t
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Walk-forward forecast Peak+1 backtest")
    parser.add_argument("--stake", type=float, default=5.0)
    parser.add_argument("--rank-days", type=int, default=20)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument(
        "--entry-offset-min",
        type=float,
        default=-120.0,
        help="minutes before avg peak hour to enter (default -120 = 2h before)",
    )
    parser.add_argument(
        "--forecast-offset", type=float, default=1.0, help="add this many C above ensemble avg for upper bucket"
    )
    parser.add_argument("--obs-window-min", type=float, default=45.0)
    parser.add_argument("--quote-window-min", type=float, default=45.0)
    parser.add_argument("--matrix", action="store_true", help="source x city matrix: simulate each source separately")
    parser.add_argument(
        "--matrix-sources", default="", help="comma-separated sources for matrix mode (default: top 8 by MAE)"
    )
    parser.add_argument("--matrix-days", type=int, default=30, help="eval days for matrix mode (last N forecast days)")
    args = parser.parse_args()

    stake = float(args.stake)
    obs_window = float(args.obs_window_min) * 60.0
    quote_window = int(float(args.quote_window_min) * 60.0)
    entry_offset = float(args.entry_offset_min) * 60.0
    fc_offset = float(args.forecast_offset)

    # --- load data ---
    con_actuals = sqlite3.connect(f"file:{ACTUALS_DB}?mode=ro", uri=True, timeout=60)
    actuals: dict[tuple[str, str], float] = {}
    for city, day, vmax in con_actuals.execute(
        "SELECT city, date, temperature_2m_max FROM actual_temperatures WHERE temperature_2m_max IS NOT NULL"
    ):
        actuals[(str(city), str(day)[:10])] = float(vmax)
    con_actuals.close()

    con_bot = sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True, timeout=60)
    con_bot.execute("PRAGMA busy_timeout=60000")

    city_names: dict[str, str] = {}
    longitudes: dict[str, float | None] = {}
    for mid, city, code, lon in con_bot.execute(
        "SELECT DISTINCT id, city, city_code, longitude FROM weather_markets "
        "WHERE metric='temperature_max' AND market_type='RANGE' "
        "AND city_code IS NOT NULL AND city_code != ''"
    ):
        city_names.setdefault(str(code), str(city))
        if lon is not None:
            longitudes.setdefault(str(code), float(lon))

    # markets: (code, day, bucket) -> [market_ids]
    markets: dict[tuple[str, str, int], list[str]] = defaultdict(list)
    for mid, code, thr, tdate in con_bot.execute(
        "SELECT id, city_code, threshold, target_date FROM weather_markets "
        "WHERE metric='temperature_max' AND market_type='RANGE' "
        "AND threshold IS NOT NULL AND target_date IS NOT NULL "
        "AND city_code IS NOT NULL AND city_code != ''"
    ):
        day = str(tdate)[:10]
        try:
            bucket = half_up(float(thr))
        except (TypeError, ValueError):
            continue
        markets[(str(code), day, bucket)].append(str(mid))

    # metar observations
    raw_metar = con_bot.execute(
        "SELECT city_code, day, obs_time, temp_c FROM metar_observations "
        "WHERE day >= '2026-08-01' ORDER BY city_code, day, obs_time"
    ).fetchall()

    # forecasts: (code, day) -> {source: latest_value} from backtest.db
    forecasts: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    bt = sqlite3.connect(f"file:{BACKTEST_DB}?mode=ro", uri=True, timeout=60)
    for code, tdate, src, pv, fetched in bt.execute(
        "SELECT city, target_date, source, predicted_value, fetched_at "
        "FROM weather_forecasts WHERE predicted_value IS NOT NULL "
        "AND metric LIKE '%max%' ORDER BY fetched_at ASC"
    ):
        day = str(tdate)[:10]
        if not code or not day:
            continue
        forecasts[(str(code), day)][str(src)] = float(pv)
    bt.close()
    con_bot.close()

    # --- timezone offsets ---
    from scrapers.metar import city_utc_offset

    lon_of = {c: longitudes.get(c) for c in city_names}
    offset_cache: dict[tuple[str, str], float] = {}

    def offset_for(code: str, utc_day: str) -> float:
        key = (code, utc_day)
        if key not in offset_cache:
            offset_cache[key] = float(city_utc_offset(code, utc_day, lon_of.get(code)))
        return offset_cache[key]

    # --- METAR by local day ---
    obs_by_local: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for code, _day, obs_time, temp in raw_metar:
        epoch = to_epoch(obs_time)
        if epoch is None or temp is None:
            continue
        try:
            temp_f = float(temp)
        except (TypeError, ValueError):
            continue
        utc_day = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")
        off = offset_for(str(code), utc_day)
        local_dt = datetime.fromtimestamp(epoch + off * 3600, tz=timezone.utc)
        obs_by_local[(str(code), local_dt.strftime("%Y-%m-%d"))].append((epoch, temp_f))
    for rows in obs_by_local.values():
        rows.sort(key=lambda r: r[0])

    # --- peak hours from METAR ---
    peak_hours: dict[tuple[str, str], float] = {}
    for (code, day), rows in obs_by_local.items():
        if len(rows) < 3:
            continue
        peak_epoch, peak_temp = max(rows, key=lambda r: r[1])
        off = offset_for(code, datetime.fromtimestamp(peak_epoch, tz=timezone.utc).strftime("%Y-%m-%d"))
        local_dt = datetime.fromtimestamp(peak_epoch + off * 3600, tz=timezone.utc)
        peak_hours[(code, day)] = local_dt.hour + local_dt.minute / 60.0

    hours_by_code: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for (code, day), hour in peak_hours.items():
        hours_by_code[code].append((day, hour))
    for code in hours_by_code:
        hours_by_code[code].sort(key=lambda r: r[0])

    def avg_peak_hour(code: str, target_day: str) -> float | None:
        rows = hours_by_code.get(code, [])
        days = [d for d, _ in rows]
        idx = bisect.bisect_left(days, target_day)
        prior = [h for _, h in rows[:idx]]
        if len(prior) < 3:
            return None
        return sum(prior) / len(prior)

    # --- ensemble forecast ---
    def ensemble_forecast(code: str, day: str, force_source: str | None = None) -> float | None:
        models = forecasts.get((code, day), {})
        if not models:
            return None
        if force_source and force_source in models:
            return float(models[force_source])
        city_name = city_names.get(code)
        best_src = BEST_SOURCE_BY_CITY.get(city_name)
        if best_src and best_src in models:
            return float(models[best_src])
        wsum = 0.0
        acc = 0.0
        for src, val in models.items():
            w = WEIGHTS.get(src, 0.05)
            acc += val * w
            wsum += w
        return acc / wsum if wsum > 0 else None

    # --- determine eval window from available forecasts ---
    fc_dates = sorted({d for (_, d) in forecasts if forecasts[(_, d)]})
    if not fc_dates:
        print("No forecast data available.")
        return 1

    con_ob = open_orderbook(ORDERBOOK_DB)
    skips: dict[str, int] = defaultdict(int)

    # --- METAR-based pair simulator for ranking ---
    def metar_pair_pnl(code: str, day: str, with_quotes: bool = True) -> dict | None:
        """Simulate a METAR-based Peak+1 pair for ranking purposes."""
        avg_h = avg_peak_hour(code, day)
        if avg_h is None:
            return None
        off = offset_for(code, day)
        y, m, d = int(day[:4]), int(day[5:7]), int(day[8:10])
        local_midnight = datetime(y, m, d, tzinfo=timezone.utc).timestamp()
        entry_utc = local_midnight - off * 3600 + avg_h * 3600 + entry_offset
        rows = obs_by_local.get((code, day), [])
        if len(rows) < 2:
            return None
        epochs = [e for e, _ in rows]
        idx = bisect.bisect_left(epochs, entry_utc)
        if idx >= len(rows) or rows[idx][0] - entry_utc > obs_window:
            return None
        entry_epoch, entry_temp = rows[idx]
        lower = half_up(entry_temp)
        upper = lower + 1
        low_mids = markets.get((code, day, lower), [])
        high_mids = markets.get((code, day, upper), [])
        if not low_mids or not high_mids:
            return None
        actual = actuals.get((city_names.get(code, ""), day))
        if actual is None:
            return None
        if not with_quotes:
            actual_bucket = half_up(actual)
            return {"pair_hit": actual_bucket in (lower, upper), "actual_bucket": actual_bucket}
        entry_quotes = {}
        for label, bucket, mids in (("low", lower, low_mids), ("high", upper, high_mids)):
            found = None
            for mid in mids:
                q = quote_at_or_after(con_ob, mid, entry_epoch, "ask", quote_window)
                if q is None:
                    continue
                price, depth, t = q
                if not (MIN_ENTRY <= price < MAX_ENTRY):
                    continue
                found = (mid, price)
                break
            if found is None:
                return None
            entry_quotes[label] = found
        actual_bucket = half_up(actual)
        pair_hit = actual_bucket in (lower, upper)
        return {"pair_hit": pair_hit, "actual_bucket": actual_bucket}

    # --- forecast-based pair simulator ---
    def forecast_pair(code: str, day: str, force_source: str | None = None) -> dict | None:
        fc = ensemble_forecast(code, day, force_source)
        if fc is None:
            skips["no_forecast"] += 1
            return None
        avg_h = avg_peak_hour(code, day)
        if avg_h is None:
            skips["no_peak_history"] += 1
            return None
        off = offset_for(code, day)
        y, m, d = int(day[:4]), int(day[5:7]), int(day[8:10])
        local_midnight = datetime(y, m, d, tzinfo=timezone.utc).timestamp()
        entry_utc = local_midnight - off * 3600 + avg_h * 3600 + entry_offset
        rows = obs_by_local.get((code, day), [])
        if len(rows) < 2:
            skips["no_metar"] += 1
            return None

        lower = half_up(fc)
        upper = lower + 1
        low_mids = markets.get((code, day, lower), [])
        high_mids = markets.get((code, day, upper), [])
        if not low_mids or not high_mids:
            skips["no_market"] += 1
            return None
        actual = actuals.get((city_names.get(code, ""), day))
        if actual is None:
            skips["no_actual"] += 1
            return None

        entry_quotes = {}
        for label, bucket, mids in (("low", lower, low_mids), ("high", upper, high_mids)):
            found = None
            for mid in mids:
                q = quote_at_or_after(con_ob, mid, entry_utc, "ask", quote_window)
                if q is None:
                    continue
                price, depth, t = q
                if not (MIN_ENTRY <= price < MAX_ENTRY):
                    continue
                found = (mid, price, t)
                break
            if found is None:
                skips["no_entry_quote"] += 1
                return None
            entry_quotes[label] = found

        entry_temp_at_fc = None
        epochs = [e for e, _ in rows]
        idx = bisect.bisect_left(epochs, entry_utc)
        if idx < len(rows) and rows[idx][0] - entry_utc <= obs_window:
            entry_temp_at_fc = rows[idx][1]

        return {
            "code": code,
            "day": day,
            "fc": fc,
            "lower": lower,
            "upper": upper,
            "rows": rows,
            "entry_epoch": entry_utc,
            "actual": actual,
            "actual_bucket": half_up(actual),
            "entry_quotes": entry_quotes,
            "entry_temp_at_fc": entry_temp_at_fc,
        }

    def leg_cost(entry: float) -> tuple[float, float, float]:
        fee = stake * FEE_RATE * (1.0 - entry)
        cost = stake + fee + GAS_USD
        return stake / entry, fee, cost

    def simulate_forecast_pair(pair: dict) -> dict:
        legs = {}
        for label, bucket, mids in (
            ("low", pair["lower"], markets.get((pair["code"], pair["day"], pair["lower"]), [])),
            ("high", pair["upper"], markets.get((pair["code"], pair["day"], pair["upper"]), [])),
        ):
            mid, entry, _t = pair["entry_quotes"][label]
            shares, _fee, cost = leg_cost(entry)
            legs[label] = {
                "bucket": bucket,
                "mid": mid,
                "entry": entry,
                "shares": shares,
                "cost": cost,
                "open": True,
                "pnl": 0.0,
                "hit": False,
                "exit": None,
            }
        fc = pair["fc"]
        entry_epoch = pair["entry_epoch"]
        for epoch, temp in pair["rows"]:
            if epoch <= entry_epoch:
                continue
            if legs["low"]["open"] and temp >= entry_epoch and temp >= fc + fc_offset:
                q = quote_at_or_after(con_ob, legs["low"]["mid"], epoch, "bid", quote_window)
                if q is not None:
                    bid, _d, _t = q
                    legs["low"]["open"] = False
                    legs["low"]["pnl"] = legs["low"]["shares"] * bid - legs["low"]["cost"]
                    legs["low"]["exit"] = ("fc_move_up", bid, epoch)
            elif legs["high"]["open"] and temp < fc:
                q = quote_at_or_after(con_ob, legs["high"]["mid"], epoch, "bid", quote_window)
                if q is not None:
                    bid, _d, _t = q
                    legs["high"]["open"] = False
                    legs["high"]["pnl"] = legs["high"]["shares"] * bid - legs["high"]["cost"]
                    legs["high"]["exit"] = ("fc_move_down", bid, epoch)
        actual_bucket = pair["actual_bucket"]
        for leg in legs.values():
            if leg["open"]:
                leg["hit"] = leg["bucket"] == actual_bucket
                leg["pnl"] = (leg["shares"] - leg["cost"]) if leg["hit"] else -leg["cost"]
        pnl = legs["low"]["pnl"] + legs["high"]["pnl"]
        pair_hit = actual_bucket in (pair["lower"], pair["upper"])
        leg_hits = int(legs["low"]["hit"]) + int(legs["high"]["hit"])
        return {"pnl": pnl, "pair_hit": pair_hit, "leg_hits": leg_hits}

    # --- walk-forward evaluation ---
    if args.matrix:
        if args.matrix_sources.strip():
            matrix_sources = [s.strip() for s in args.matrix_sources.split(",") if s.strip()]
        else:
            matrix_sources = [
                "visual_crossing",
                "ecmwf_ifs025",
                "icon_global",
                "ukmo_seamless",
                "gem_global",
                "gfs_seamless",
                "openweathermap",
                "weatherapi_com",
            ]
        eval_dates = fc_dates[-args.matrix_days :]
        print("=" * 80)
        print("SOURCE x CITY MATRIX (real orderbook, forecast entry)")
        print(f"sources: {', '.join(matrix_sources)}")
    else:
        matrix_sources = [None]
        eval_dates = fc_dates
        print("=" * 80)
        print("WALK-FORWARD FORECAST Peak+1 BACKTEST (real orderbook)")
    print(f"forecast+{fc_offset:.0f}C entry, {args.entry_offset_min:+.0f}min before peak")
    print(f"ranking: previous {args.rank_days} days METAR-based pair hit rate")
    print(f"eval: {eval_dates[0]}..{eval_dates[-1]} ({len(eval_dates)} days)")
    print(f"stake per leg: ${stake:.2f}")
    print("=" * 80)

    total_pnl = 0.0
    total_pairs = 0
    total_pair_hits = 0
    total_legs = 0
    total_leg_hits = 0
    total_stake = 0.0
    eval_day: dict[str, dict] = defaultdict(lambda: {"pairs": 0, "hits": 0, "pnl": 0.0})
    eval_city: dict[str, dict] = defaultdict(
        lambda: {"pairs": 0, "hits": 0, "legs": 0, "leg_hits": 0, "pnl": 0.0, "stake": 0.0}
    )
    matrix_cell: dict[tuple[str, str], dict] = defaultdict(lambda: {"pairs": 0, "hits": 0, "pnl": 0.0, "stake": 0.0})

    for day in eval_dates:
        # rolling ranking: previous 20 days METAR-based
        day_date = datetime.strptime(day, "%Y-%m-%d").date()
        rank_stats: dict[str, dict] = defaultdict(lambda: {"n": 0, "hits": 0})
        for rd_offset in range(args.rank_days, 0, -1):
            rd = (day_date - timedelta(days=rd_offset)).isoformat()
            for code in set(c for (c, d, _b) in markets if d == rd):
                res = metar_pair_pnl(code, rd, with_quotes=False)
                if res is None:
                    continue
                st = rank_stats[code]
                st["n"] += 1
                st["hits"] += 1 if res["pair_hit"] else 0

        # select top N
        ranked = sorted(
            rank_stats.items(),
            key=lambda item: (
                -item[1]["hits"] / item[1]["n"] if item[1]["n"] else 0,
                -item[1]["n"],
                item[0],
            ),
        )
        top_codes = [c for c, s in ranked[: args.top_n] if s["n"] >= 3]
        if not top_codes:
            top_codes = [c for c, _ in ranked[: args.top_n]]

        for code in top_codes:
            for force_src in matrix_sources:
                pair = forecast_pair(code, day, force_source=force_src)
                if pair is None:
                    continue
                res = simulate_forecast_pair(pair)
                city_name = city_names.get(code, code)
                c = eval_city[city_name if force_src is None else f"{city_name}"]
                c["pairs"] += 1
                c["hits"] += 1 if res["pair_hit"] else 0
                c["legs"] += 2
                c["leg_hits"] += res["leg_hits"]
                c["pnl"] += res["pnl"]
                c["stake"] += 2 * stake
                if force_src is not None:
                    cell = matrix_cell[(force_src, city_name)]
                    cell["pairs"] += 1
                    cell["hits"] += 1 if res["pair_hit"] else 0
                    cell["pnl"] += res["pnl"]
                    cell["stake"] += 2 * stake
                d = eval_day[day]
                d["pairs"] += 1
                d["hits"] += 1 if res["pair_hit"] else 0
                d["pnl"] += res["pnl"]
                total_pnl += res["pnl"]
                total_pairs += 1
                total_pair_hits += 1 if res["pair_hit"] else 0
                total_legs += 2
                total_leg_hits += res["leg_hits"]
                total_stake += 2 * stake

    con_ob.close()

    if args.matrix:
        print()
        print(f"{'source':<22s} {'city':<16s} {'pairs':>6s} {'hit%':>6s} {'PnL':>10s} {'ROI%':>7s}")
        for src, city in sorted(matrix_cell, key=lambda k: -matrix_cell[k]["pnl"]):
            m = matrix_cell[(src, city)]
            hit = m["hits"] / m["pairs"] * 100 if m["pairs"] else 0.0
            roi = m["pnl"] / m["stake"] * 100 if m["stake"] else 0.0
            print(f"{src:<22s} {city:<16s} {m['pairs']:>6d} {hit:>5.1f}% {m['pnl']:>+9.2f} {roi:>+6.1f}%")
        print()
        print("per-source totals:")
        src_tot: dict[str, dict] = defaultdict(lambda: {"pairs": 0, "pnl": 0.0, "stake": 0.0})
        for (src, _city), m in matrix_cell.items():
            src_tot[src]["pairs"] += m["pairs"]
            src_tot[src]["pnl"] += m["pnl"]
            src_tot[src]["stake"] += m["stake"]
        for src in sorted(src_tot, key=lambda s: -src_tot[s]["pnl"]):
            t = src_tot[src]
            roi = t["pnl"] / t["stake"] * 100 if t["stake"] else 0.0
            print(f"  {src:<22s} pairs={t['pairs']:>4d} PnL={t['pnl']:>+9.2f} ROI={roi:>+6.1f}%")

    print()
    print(f"{'Eval city':<18s} {'pairs':>6s} {'hits':>5s} {'hit%':>6s} {'legs':>5s} {'PnL':>10s} {'ROI%':>7s}")
    for city in sorted(eval_city, key=lambda c: -eval_city[c]["pnl"]):
        c = eval_city[city]
        hit = c["hits"] / c["pairs"] * 100 if c["pairs"] else 0.0
        roi = c["pnl"] / c["stake"] * 100 if c["stake"] else 0.0
        print(
            f"{city:<18s} {c['pairs']:>6d} {c['hits']:>5d} {hit:>5.1f}% {c['legs']:>5d} {c['pnl']:>+9.2f} {roi:>+6.1f}%"
        )

    print()
    pair_rate = total_pair_hits / total_pairs * 100 if total_pairs else 0.0
    leg_rate = total_leg_hits / total_legs * 100 if total_legs else 0.0
    roi = total_pnl / total_stake * 100 if total_stake else 0.0
    print(f"TOTAL pairs={total_pairs} pair_hits={total_pair_hits} pair_hit={pair_rate:.1f}%")
    print(f"TOTAL legs={total_legs} leg_hits={total_leg_hits} leg_hit={leg_rate:.1f}%")
    print(f"TOTAL PnL=${total_pnl:+.2f} stake=${total_stake:.2f} ROI={roi:+.1f}%")

    print()
    print("per-day:")
    for day in eval_dates:
        d = eval_day.get(day)
        if not d:
            continue
        rate = d["hits"] / d["pairs"] * 100 if d["pairs"] else 0.0
        print(f"  {day}: pairs={d['pairs']:>3d} hits={d['hits']:>3d} hit={rate:>5.1f}% PnL=${d['pnl']:+.2f}")

    if skips:
        print()
        print("skipped:")
        for reason in sorted(skips):
            print(f"  {reason}: {skips[reason]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
