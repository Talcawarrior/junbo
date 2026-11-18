"""Analyze city time patterns for high vs low temperature markets.

Analyzes which cities have higher win rates for HIGH (max) vs LOW (min)
temperature markets at different local times of day.

This helps identify:
1. Which cities are more predictable for high temperature markets
2. Which cities are more predictable for low temperature markets
3. Optimal local time windows for betting
4. Whether high vs low metrics have different temporal patterns
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_db_path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

DB_PATH = Path(get_db_path())
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "analysis_results"

CITY_OFFSETS = {
    "Wellington": 12,
    "Seoul": 9,
    "Tokyo": 9,
    "Hong Kong": 8,
    "Shanghai": 8,
    "Beijing": 8,
    "Shenzhen": 8,
    "Singapore": 8,
    "Kuala Lumpur": 8,
    "Manila": 8,
    "Chongqing": 8,
    "Chengdu": 8,
    "Guangzhou": 8,
    "Taipei": 8,
    "Qingdao": 8,
    "Wuhan": 8,
    "London": 1,
    "Paris": 2,
    "Amsterdam": 2,
    "Madrid": 2,
    "Moscow": 3,
    "Helsinki": 3,
    "Warsaw": 2,
    "Milan": 2,
    "Munich": 2,
    "Istanbul": 3,
    "Ankara": 3,
    "Tel Aviv": 3,
    "New York": -4,
    "Toronto": -4,
    "Miami": -4,
    "Chicago": -5,
    "Houston": -5,
    "Dallas": -5,
    "Atlanta": -4,
    "Denver": -6,
    "Los Angeles": -7,
    "Seattle": -7,
    "San Francisco": -7,
    "Sao Paulo": -3,
    "Buenos Aires": -3,
    "Cape Town": 2,
}


@dataclass
class CityTimeStats:
    city: str
    metric: str
    hour: int
    bets: int
    wins: int
    win_rate: float
    avg_price: float


def load_data(db_path: Path):
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT b.market_id, b.city, b.status, wm.metric
        FROM bets b
        JOIN weather_markets wm ON b.market_id = wm.id
        WHERE b.status IN ('won', 'lost')
    """)
    settled_data = cur.fetchall()

    outcomes = []
    for row in settled_data:
        outcomes.append(
            {"market_id": row["market_id"], "city": row["city"], "metric": row["metric"], "won": row["status"] == "won"}
        )

    cur.execute("""
        SELECT market_id, city, metric, target_date, yes_price, no_price,
               snapshot_time, hours_to_settlement
        FROM market_snapshots
        ORDER BY market_id, snapshot_time
    """)
    all_snaps = cur.fetchall()

    conn.close()

    snapshots_by_market = defaultdict(list)
    for row in all_snaps:
        market_id = row["market_id"]
        snapshots_by_market[market_id].append(
            {
                "time": datetime.fromisoformat(row["snapshot_time"]),
                "yes_price": row["yes_price"],
                "hts": row["hours_to_settlement"],
            }
        )

    return {"outcomes": outcomes, "snapshots_by_market": snapshots_by_market}


def analyze_city_time_patterns(data):
    city_time_stats = defaultdict(list)

    for outcome in data["outcomes"]:
        market_id = outcome["market_id"]
        city = outcome["city"]
        metric = outcome["metric"]
        won = outcome["won"]

        for snap in data["snapshots_by_market"][market_id]:
            price = snap["yes_price"]
            snap_time = snap["time"]

            if price is None:
                continue

            city_offset = CITY_OFFSETS.get(city, 0)
            local_hour = (snap_time.hour + city_offset) % 24

            city_time_stats[(city, metric, local_hour)].append(
                {
                    "price": price,
                    "won": won,
                    "time": snap_time,
                }
            )

    stats = []
    for (city, metric, hour), entries in city_time_stats.items():
        if not entries:
            continue

        bets = len(entries)
        wins = sum(1 for e in entries if e["won"])
        win_rate = wins / bets if bets > 0 else 0

        prices = [e["price"] for e in entries]
        avg_price = sum(prices) / len(prices)

        stats.append(
            CityTimeStats(
                city=city,
                metric=metric,
                hour=hour,
                bets=bets,
                wins=wins,
                win_rate=win_rate,
                avg_price=avg_price,
            )
        )

    return stats


def print_analysis(stats):
    print("=" * 100)
    print(f"{'City':20s} {'Metric':8s} {'Hour':6s} {'Bets':6s} {'Wins':6s} {'WR%':8s} {'Avg Price':10s}")
    print("=" * 100)

    for s in sorted(stats, key=lambda x: (x.metric, x.hour)):
        print(
            f"{s.city:20s} {s.metric:8s} {s.hour:6d} {s.bets:6d} {s.wins:6d} "
            f"{s.win_rate * 100:6.1f}% {s.avg_price:10.3f}"
        )


def print_top_cities_by_metric(stats):
    metrics = {"temperature_max": [], "temperature_min": []}
    for s in stats:
        if s.metric in metrics:
            metrics[s.metric].append(s)

    for metric, stat_list in metrics.items():
        print(f"\n--- Top Cities for {metric.upper()} ---")
        print(f"{'City':20s} {'Hour':6s} {'Bets':6s} {'WR%':8s} {'Avg Price':10s}")
        print("-" * 60)

        for s in sorted(stat_list, key=lambda x: x.win_rate, reverse=True)[:10]:
            print(f"{s.city:20s} {s.hour:6d} {s.bets:6d} {s.win_rate * 100:6.1f}% {s.avg_price:10.3f}")


def analyze_best_hours(stats):
    print(f"\n{'=' * 60}")
    print("BEST HOURS FOR WIN RATE")
    print(f"{'=' * 60}")
    print(f"{'Hour':6s} {'Total Bets':8s} {'Wins':8s} {'WR%':8s} {'Avg Price':10s}")
    print("-" * 65)

    hour_stats = defaultdict(list)
    for s in stats:
        hour_stats[s.hour].append(s)

    for hour in range(24):
        entries = hour_stats.get(hour, [])
        if not entries:
            continue

        total_bets = sum(e.bets for e in entries)
        total_wins = sum(e.wins for e in entries)
        avg_wr = total_wins / total_bets if total_bets > 0 else 0
        avg_price = sum(e.avg_price * e.bets for e in entries) / total_bets

        print(f"{hour:6d} {total_bets:8d} {total_wins:8d} {avg_wr * 100:6.1f}% {avg_price:10.3f}")


def generate_charts(stats):
    out_dir = OUT_DIR / "city_time_analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    metrics = {"temperature_max": [], "temperature_min": []}
    for s in stats:
        if s.metric in metrics:
            metrics[s.metric].append(s)

    if len(metrics["temperature_max"]) > 0 or len(metrics["temperature_min"]) > 0:
        fig, axes = plt.subplots(2, 1, figsize=(14, 12))
        fig.suptitle("City Time Pattern Analysis: High vs Low Temperature Markets", fontsize=16, fontweight="bold")

        for idx, metric in enumerate(["temperature_max", "temperature_min"]):
            metric_name = metric
            metric_stats = metrics[metric_name]
            ax = axes[idx]

            hours = sorted(set(s.hour for s in metric_stats))
            win_rates = []
            avg_prices = []

            for hour in hours:
                hour_entries = [s for s in metric_stats if s.hour == hour]
                if not hour_entries:
                    continue

                total_bets = sum(e.bets for e in hour_entries)
                total_wins = sum(e.wins for e in hour_entries)
                wr = total_wins / total_bets if total_bets > 0 else 0

                weighted_price = sum(e.avg_price * e.bets for e in hour_entries) / total_bets

                win_rates.append(wr * 100)
                avg_prices.append(weighted_price)

            color = "red" if metric == "temperature_max" else "blue"
            bars = ax.bar(hours, win_rates, color=color, alpha=0.7, edgecolor="black", linewidth=0.5)

            ax.set_xlabel("Local Hour (UTC)", fontsize=12)
            ax.set_ylabel("Win Rate (%)" if idx == 0 else "Average Entry Price", fontsize=12)
            ax.set_title(f"{metric_name.upper()} Markets", fontsize=14, fontweight="bold")
            ax.set_ylim(0, max(100, max(win_rates) * 1.2) if win_rates else 100)
            ax.grid(True, alpha=0.3)

            if idx == 0:
                for bar, wr in zip(bars, win_rates):
                    height = bar.get_height()
                    ax.text(
                        bar.get_x() + bar.get_width() / 2,
                        height + 1,
                        f"{wr:.1f}%",
                        ha="center",
                        va="bottom",
                        fontsize=9,
                    )
            else:
                ax2 = ax.twinx()
                ax2.plot(hours, avg_prices, color="green", marker="o", linewidth=2, markersize=6)
                ax2.set_ylabel("Average Entry Price", color="green", fontsize=12)
                ax2.tick_params(axis="y", labelcolor="green")

        plt.tight_layout(rect=[0, 0, 1, 0.96])

        chart_path = out_dir / f"city_time_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        plt.savefig(chart_path, dpi=150, bbox_inches="tight")
        plt.close()

        print(f"\n  Chart saved: {chart_path}")


def save_report(stats, out_dir):
    report = {
        "generated_at": datetime.now().isoformat(),
        "city_time_stats": [],
        "best_hours": {},
        "top_cities": {},
    }

    for s in stats:
        report["city_time_stats"].append(
            {
                "city": s.city,
                "metric": s.metric,
                "hour": s.hour,
                "bets": s.bets,
                "wins": s.wins,
                "win_rate": s.win_rate,
                "avg_price": s.avg_price,
            }
        )

    metrics = {"temperature_max": [], "temperature_min": []}
    for s in stats:
        if s.metric in metrics:
            metrics[s.metric].append(s)

    for metric, stat_list in metrics.items():
        report["top_cities"][metric] = sorted(
            [{"city": s.city, "win_rate": s.win_rate, "bets": s.bets, "avg_price": s.avg_price} for s in stat_list],
            key=lambda x: x["win_rate"],
            reverse=True,
        )[:10]

    hour_stats = defaultdict(list)
    for s in stats:
        hour_stats[s.hour].append(s)

    for hour in range(24):
        entries = hour_stats.get(hour, [])
        if entries:
            total_bets = sum(e.bets for e in entries)
            total_wins = sum(e.wins for e in entries)
            report["best_hours"][hour] = {
                "bets": total_bets,
                "wins": total_wins,
                "win_rate": total_wins / total_bets if total_bets > 0 else 0,
            }

    report_path = out_dir / "city_time_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\n  Report saved: {report_path}")


def main():
    print("City Time Pattern Analysis for High vs Low Temperature Markets")
    print("=" * 70)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    data = load_data(DB_PATH)
    print(f"\nLoaded {len(data['outcomes'])} settled markets")

    city_time_stats = analyze_city_time_patterns(data)

    print_analysis(city_time_stats)
    print_top_cities_by_metric(city_time_stats)
    analyze_best_hours(city_time_stats)

    generate_charts(city_time_stats)
    save_report(city_time_stats, OUT_DIR)

    print(f"\n{'=' * 60}")
    print("Analysis complete. Files saved to:")
    print(f"  {OUT_DIR / 'city_time_analysis_*.png'}")
    print(f"  {OUT_DIR / 'city_time_report.json'}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
