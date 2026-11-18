"""Peak+1 pair backtest: last 20 complete days, real orderbook only.

Pipeline (no lookahead):
1. Ranking window (20 days before evaluation): simulate the Peak+1 pair for
   every city/day, using only information available at decision time.
2. Select the top 10 cities by pair hit rate in the ranking window.
3. Evaluation window (last 20 complete days): run the same pair strategy only
   for the selected cities.

Entry: local average peak hour from prior METAR history. At the first
observation at/after that hour, open YES on the current-temp bucket (lower)
and the bucket above it (upper).

Direction rule: if temperature rises into the upper bucket, close the lower
leg; if it falls back to/below the entry temperature, close the upper leg.
With --close-mode move1c the legs close only on a full 1C move from entry;
with --close-mode hold both legs are kept until settlement.
Buys fill at best_ask, sells fill at best_bid. Remaining legs settle against
the actuals.db daily max bucket.

A pair counts as a hit when the actual max bucket is one of the two entry
buckets. Skipped pairs (missing market, observation, quote, or actual) are
reported, not silently dropped.
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

from scrapers.metar import city_utc_offset  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

BOT_DB = Path(get_db_path())
ACTUALS_DB = REPO_ROOT / "data" / "actuals.db"
ORDERBOOK_DB = REPO_ROOT / "data" / "orderbook.db"

FEE_RATE = 0.05
GAS_USD = 0.10
MIN_ENTRY = 0.05
MAX_ENTRY = 0.95


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
    """Open orderbook.db read-only; fall back to a backup copy when locked."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
        con.execute("PRAGMA busy_timeout=60000")
        con.execute("SELECT 1 FROM orderbook_snapshots LIMIT 1").fetchone()
        return con
    except sqlite3.OperationalError:
        print("orderbook live read locked; copying via backup API...", flush=True)
        tmp = Path(tempfile.gettempdir()) / "ob_peak1_copy.db"
        if tmp.exists():
            tmp.unlink()
        src = sqlite3.connect(path, timeout=60)
        src.execute("PRAGMA busy_timeout=60000")
        dst = sqlite3.connect(tmp, timeout=60)
        src.backup(dst)
        dst.close()
        src.close()
        print("orderbook copy ready.", flush=True)
        con = sqlite3.connect(tmp, timeout=60)
        con.execute("PRAGMA busy_timeout=60000")
        return con


def quote_at_or_after(con, mid: str, ts_epoch: float, side: str, window_sec: int):
    """First valid quote at/after ts_epoch inside the window.

    side 'ask' uses best_ask (buy), side 'bid' uses best_bid (sell).
    """
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
    parser = argparse.ArgumentParser(description="Peak+1 pair backtest, real orderbook only")
    parser.add_argument("--stake", type=float, default=5.0)
    parser.add_argument("--rank-days", type=int, default=20)
    parser.add_argument("--eval-days", type=int, default=20)
    parser.add_argument("--min-rank-days", type=int, default=3)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--obs-window-min", type=float, default=45.0)
    parser.add_argument("--quote-window-min", type=float, default=45.0)
    parser.add_argument("--min-entry", type=float, default=MIN_ENTRY)
    parser.add_argument("--max-entry", type=float, default=MAX_ENTRY)
    parser.add_argument("--min-depth", type=float, default=0.0)
    parser.add_argument(
        "--close-mode",
        default="direction",
        choices=("direction", "move1c", "hold"),
        help="direction: close on bucket touch/entry touch; move1c: close only "
        "on a full 1C move from entry; hold: never close early",
    )
    parser.add_argument(
        "--entry-offset-min",
        type=float,
        default=0.0,
        help="shift entry time vs avg peak hour in minutes (negative = earlier)",
    )
    args = parser.parse_args()

    stake = float(args.stake)
    obs_window = float(args.obs_window_min) * 60.0
    quote_window = int(float(args.quote_window_min) * 60.0)

    con_actuals = sqlite3.connect(f"file:{ACTUALS_DB}?mode=ro", uri=True, timeout=60)
    max_actual = con_actuals.execute("SELECT max(date) FROM actual_temperatures").fetchone()[0]
    today = datetime.now(timezone.utc).date()
    end_day = datetime.strptime(str(max_actual)[:10], "%Y-%m-%d").date()
    if end_day >= today:
        end_day = today - timedelta(days=1)
    eval_dates = [(end_day - timedelta(days=i)).isoformat() for i in range(args.eval_days - 1, -1, -1)]
    rank_dates = [
        (datetime.strptime(eval_dates[0], "%Y-%m-%d").date() - timedelta(days=i)).isoformat()
        for i in range(args.rank_days, 0, -1)
    ]
    rank_start = rank_dates[0]
    history_start = (datetime.strptime(rank_start, "%Y-%m-%d").date() - timedelta(days=45)).isoformat()

    actuals: dict[tuple[str, str], float] = {}
    for city, day, vmax in con_actuals.execute(
        "SELECT city, date, temperature_2m_max FROM actual_temperatures "
        "WHERE date>=? AND date<=? AND temperature_2m_max IS NOT NULL",
        (rank_start, eval_dates[-1]),
    ):
        actuals[(str(city), str(day)[:10])] = float(vmax)
    con_actuals.close()

    con_bot = sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True, timeout=60)
    con_bot.execute("PRAGMA busy_timeout=60000")
    markets: dict[tuple[str, str, int], list[str]] = defaultdict(list)
    city_names: dict[str, str] = {}
    longitudes: dict[str, float | None] = {}
    for mid, city, code, thr, tdate, lon in con_bot.execute(
        "SELECT id, city, city_code, threshold, target_date, longitude FROM weather_markets "
        "WHERE metric='temperature_max' AND market_type='RANGE' "
        "AND threshold IS NOT NULL AND target_date IS NOT NULL "
        "AND city_code IS NOT NULL AND city_code != '' "
        "AND substr(target_date,1,10)>=? AND substr(target_date,1,10)<=?",
        (rank_start, eval_dates[-1]),
    ):
        day = str(tdate)[:10]
        try:
            bucket = half_up(float(thr))
        except (TypeError, ValueError):
            continue
        markets[(str(code), day, bucket)].append(str(mid))
        city_names.setdefault(str(code), str(city))
        if lon is not None:
            try:
                longitudes.setdefault(str(code), float(lon))
            except (TypeError, ValueError):
                pass

    raw_metar = con_bot.execute(
        "SELECT city_code, day, obs_time, temp_c FROM metar_observations "
        "WHERE day>=? ORDER BY city_code, day, obs_time",
        (history_start,),
    ).fetchall()
    con_bot.close()

    lon_of = {c: longitudes.get(c) for c in city_names}
    offset_cache: dict[tuple[str, str], float] = {}

    def offset_for(code: str, utc_day: str) -> float:
        key = (code, utc_day)
        if key not in offset_cache:
            offset_cache[key] = float(city_utc_offset(code, utc_day, lon_of.get(code)))
        return offset_cache[key]

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

    con_ob = open_orderbook(ORDERBOOK_DB)
    skips: dict[str, int] = defaultdict(int)
    print("data loaded; starting ranking phase...", flush=True)

    def build_pair(code: str, day: str, with_quotes: bool):
        """Build the tradable pair for one city/day, or None with a skip reason."""
        avg_h = avg_peak_hour(code, day)
        if avg_h is None:
            skips["no_peak_history"] += 1
            return None
        off = offset_for(code, day)
        y, m, d = int(day[:4]), int(day[5:7]), int(day[8:10])
        local_midnight = datetime(y, m, d, tzinfo=timezone.utc).timestamp()
        entry_utc = local_midnight - off * 3600 + avg_h * 3600 + float(args.entry_offset_min) * 60.0
        rows = obs_by_local.get((code, day), [])
        if len(rows) < 2:
            skips["no_metar"] += 1
            return None
        epochs = [e for e, _ in rows]
        idx = bisect.bisect_left(epochs, entry_utc)
        if idx >= len(rows) or rows[idx][0] - entry_utc > obs_window:
            skips["no_entry_obs"] += 1
            return None
        entry_epoch, entry_temp = rows[idx]
        lower = half_up(entry_temp)
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
        entry_quotes = None
        if with_quotes:
            entry_quotes = {}
            for label, bucket, mids in (("low", lower, low_mids), ("high", upper, high_mids)):
                found = None
                for mid in mids:
                    q = quote_at_or_after(con_ob, mid, entry_epoch, "ask", quote_window)
                    if q is None:
                        continue
                    price, depth, t = q
                    if not (args.min_entry <= price < args.max_entry):
                        continue
                    if args.min_depth > 0:
                        try:
                            if float(depth or 0) < args.min_depth:
                                continue
                        except (TypeError, ValueError):
                            continue
                    found = (mid, price, depth, t)
                    break
                if found is None:
                    skips["no_entry_quote"] += 1
                    return None
                entry_quotes[label] = found
        return {
            "code": code,
            "day": day,
            "avg_h": avg_h,
            "entry_epoch": entry_epoch,
            "entry_temp": entry_temp,
            "lower": lower,
            "upper": upper,
            "rows": rows,
            "row_idx": idx,
            "actual": float(actual),
            "actual_bucket": half_up(float(actual)),
            "low_mids": low_mids,
            "high_mids": high_mids,
            "entry_quotes": entry_quotes,
        }

    def leg_cost(entry: float) -> tuple[float, float, float]:
        fee = stake * FEE_RATE * (1.0 - entry)
        cost = stake + fee + GAS_USD
        return stake / entry, fee, cost

    def exit_quote(mid: str, ts_epoch: float):
        return quote_at_or_after(con_ob, mid, ts_epoch, "bid", quote_window)

    def simulate(pair: dict) -> dict:
        legs = {}
        for label, bucket, mids in (
            ("low", pair["lower"], pair["low_mids"]),
            ("high", pair["upper"], pair["high_mids"]),
        ):
            mid, entry, _depth, _t = pair["entry_quotes"][label]
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
        entry_temp = pair["entry_temp"]
        upper_b = pair["upper"]
        close_mode = str(args.close_mode)
        for epoch, temp in pair["rows"][pair["row_idx"] + 1 :]:
            if close_mode == "direction":
                close_low = temp >= upper_b
                close_high = temp <= entry_temp
            elif close_mode == "move1c":
                close_low = temp >= entry_temp + 1.0
                close_high = temp <= entry_temp - 1.0
            else:
                close_low = False
                close_high = False
            if legs["low"]["open"] and close_low:
                q = exit_quote(legs["low"]["mid"], epoch)
                if q is not None:
                    bid, _d, _t = q
                    legs["low"]["open"] = False
                    legs["low"]["pnl"] = legs["low"]["shares"] * bid - legs["low"]["cost"]
                    legs["low"]["exit"] = ("temp_up", bid, epoch)
                else:
                    skips["no_exit_quote"] += 1
            elif legs["high"]["open"] and close_high:
                q = exit_quote(legs["high"]["mid"], epoch)
                if q is not None:
                    bid, _d, _t = q
                    legs["high"]["open"] = False
                    legs["high"]["pnl"] = legs["high"]["shares"] * bid - legs["high"]["cost"]
                    legs["high"]["exit"] = ("temp_down", bid, epoch)
                else:
                    skips["no_exit_quote"] += 1
        actual_bucket = pair["actual_bucket"]
        for leg in legs.values():
            if leg["open"]:
                leg["hit"] = leg["bucket"] == actual_bucket
                leg["pnl"] = (leg["shares"] - leg["cost"]) if leg["hit"] else -leg["cost"]
        pnl = legs["low"]["pnl"] + legs["high"]["pnl"]
        pair_hit = actual_bucket in (pair["lower"], pair["upper"])
        leg_hits = int(legs["low"]["hit"]) + int(legs["high"]["hit"])
        return {"pnl": pnl, "pair_hit": pair_hit, "leg_hits": leg_hits, "legs": legs}

    # Phase 1: rank every city/day in the ranking window.
    rank_stats: dict[str, dict] = defaultdict(lambda: {"n": 0, "hits": 0, "pnl": 0.0, "legs": 0})
    rank_pairs = 0
    for day in rank_dates:
        codes = sorted({code for (code, d, _b) in markets if d == day})
        for code in codes:
            pair = build_pair(code, day, with_quotes=True)
            if pair is None:
                continue
            res = simulate(pair)
            st = rank_stats[code]
            st["n"] += 1
            st["hits"] += 1 if res["pair_hit"] else 0
            st["pnl"] += res["pnl"]
            st["legs"] += 2
            rank_pairs += 1

    def rank_key(item):
        code, st = item
        rate = st["hits"] / st["n"] if st["n"] else 0.0
        return (-rate, -st["n"], code)

    eligible = [(c, s) for c, s in rank_stats.items() if s["n"] >= args.min_rank_days]
    if len(eligible) < args.top_n:
        eligible = list(rank_stats.items())
    eligible.sort(key=rank_key)
    top_codes = [c for c, _ in eligible[: args.top_n]]

    # Phase 2: evaluate only the selected cities in the evaluation window.
    print(f"ranking done ({rank_pairs} pairs); evaluating top {len(top_codes)} cities...", flush=True)
    eval_city: dict[str, dict] = defaultdict(
        lambda: {"pairs": 0, "hits": 0, "legs": 0, "leg_hits": 0, "pnl": 0.0, "stake": 0.0}
    )
    eval_day: dict[str, dict] = defaultdict(lambda: {"pairs": 0, "hits": 0, "pnl": 0.0})
    total_pnl = 0.0
    total_pairs = 0
    total_pair_hits = 0
    total_legs = 0
    total_leg_hits = 0
    total_stake = 0.0
    for day in eval_dates:
        for code in top_codes:
            pair = build_pair(code, day, with_quotes=True)
            if pair is None:
                continue
            res = simulate(pair)
            c = eval_city[code]
            c["pairs"] += 1
            c["hits"] += 1 if res["pair_hit"] else 0
            c["legs"] += 2
            c["leg_hits"] += res["leg_hits"]
            c["pnl"] += res["pnl"]
            c["stake"] += 2 * stake
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

    print("=" * 80)
    print("PEAK+1 PAIR BACKTEST (real orderbook: buy best_ask, sell best_bid)")
    print(f"ranking window: {rank_dates[0]}..{rank_dates[-1]} ({len(rank_dates)} days)")
    print(f"evaluation window: {eval_dates[0]}..{eval_dates[-1]} ({len(eval_dates)} days)")
    print(f"stake per leg: ${stake:.2f}, fee {FEE_RATE:.2f}, gas ${GAS_USD:.2f}")
    print(f"close mode: {args.close_mode}, entry offset: {float(args.entry_offset_min):+.0f} min")
    print("=" * 80)
    print()
    print(f"ranking pairs simulated: {rank_pairs}")
    print(f"{'Top city':<18s} {'days':>5s} {'hits':>5s} {'hit%':>6s} {'PnL':>10s}")
    for code in top_codes:
        st = rank_stats.get(code, {"n": 0, "hits": 0, "pnl": 0.0})
        rate = st["hits"] / st["n"] * 100 if st["n"] else 0.0
        print(f"{city_names.get(code, code):<18s} {st['n']:>5d} {st['hits']:>5d} {rate:>5.1f}% {st['pnl']:>+9.2f}")
    print()
    print(f"{'Eval city':<18s} {'pairs':>6s} {'hits':>5s} {'hit%':>6s} {'legs':>5s} {'PnL':>10s} {'ROI%':>7s}")
    for code in sorted(eval_city, key=lambda c: -eval_city[c]["pnl"]):
        c = eval_city[code]
        hit = c["hits"] / c["pairs"] * 100 if c["pairs"] else 0.0
        roi = c["pnl"] / c["stake"] * 100 if c["stake"] else 0.0
        print(
            f"{city_names.get(code, code):<18s} {c['pairs']:>6d} {c['hits']:>5d} "
            f"{hit:>5.1f}% {c['legs']:>5d} {c['pnl']:>+9.2f} {roi:>+6.1f}%"
        )
    print()
    pair_rate = total_pair_hits / total_pairs * 100 if total_pairs else 0.0
    leg_rate = total_leg_hits / total_legs * 100 if total_legs else 0.0
    roi = total_pnl / total_stake * 100 if total_stake else 0.0
    print(f"TOTAL pairs={total_pairs} pair_hits={total_pair_hits} pair_hit={pair_rate:.1f}%")
    print(f"TOTAL legs={total_legs} leg_hits={total_leg_hits} leg_hit={leg_rate:.1f}%")
    print(f"TOTAL PnL=${total_pnl:+.2f} stake=${total_stake:.2f} ROI={roi:+.1f}%")
    print()
    print("per-day evaluation:")
    for day in eval_dates:
        d = eval_day.get(day)
        if not d:
            continue
        rate = d["hits"] / d["pairs"] * 100 if d["pairs"] else 0.0
        print(f"  {day}: pairs={d['pairs']:>3d} hits={d['hits']:>3d} hit={rate:>5.1f}% PnL=${d['pnl']:+.2f}")
    if skips:
        print()
        print("skipped candidates:")
        for reason in sorted(skips):
            print(f"  {reason}: {skips[reason]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
