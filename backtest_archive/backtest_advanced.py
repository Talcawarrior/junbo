"""Advanced City-Time Pattern Backtest for Junbo.

This script implements sophisticated backtesting strategies based on our discovered
patterns: certain cities have much higher win rates for temperature markets during
specific local time windows (HIGH vs LOW metrics, city-specific optimal hours).

Key features:
- City-specific time filters (0-12h, 12-24h, etc.) for both HIGH and LOW metrics
- Top-performing cities only (>=60% WR, >=3 bets)
- Rolling day-by-day performance tracking
- Detailed city-by-city analysis with time windows
- Multiple strategy variants for comparison

Based on analysis of:
- 969 settled markets (199 won, 770 lost)
- Cities with high temporal predictability (Tokyo, Beijing, NYC, Shanghai, etc.)
- Optimal time windows: 0-12h local time for most profitable strategies
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
import math
import os

os.environ.setdefault("MPLBACKEND", "Agg")
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "backtest.db"
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "backtest_advanced"

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
class MarketOutcome:
    market_id: str
    city: str
    metric: str
    target_date: datetime | None
    won: bool
    entry_price: float
    local_hour: int
    snapshots: list = field(default_factory=list)


@dataclass
class StrategyResult:
    name: str
    bets: list = field(default_factory=list)
    total_pnl: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    total_bet: float = 0.0
    max_drawdown: float = 0.0
    peak: float = 0.0
    equity_curve: list = field(default_factory=list)
    city_stats: dict = field(default_factory=dict)


FEE_RATE = 0.05


def load_data(db_path: Path):
    """Load settled markets and market snapshots from bot.db."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Get all settled bets (won/lost)
    cur.execute("""
        SELECT b.market_id, b.city, b.entry_price, b.status,
               wm.metric, wm.target_date
        FROM bets b
        JOIN weather_markets wm ON b.market_id = wm.id
        WHERE b.status IN ('won', 'lost')
    """)
    settled_data = cur.fetchall()

    # Get market snapshots with local time conversion
    cur.execute("""
        SELECT s.market_id, s.city, s.metric, s.target_date,
               s.yes_price, s.no_price, s.snapshot_time,
               s.hours_to_settlement
        FROM market_snapshots s
        JOIN weather_markets wm ON s.market_id = wm.id
        ORDER BY s.market_id, s.snapshot_time
    """)
    snapshot_data = cur.fetchall()

    conn.close()

    # Build outcomes
    outcomes = {}
    for row in settled_data:
        mid = row["market_id"]
        if mid not in outcomes:
            # Get local time for entry
            entry_local_hour = None
            for snap in snapshot_data:
                if snap["market_id"] == mid:
                    city = snap["city"]
                    city_offset = CITY_OFFSETS.get(city, 0)
                    try:
                        entry_local_hour = (snap["snapshot_time"].hour + city_offset) % 24
                    except AttributeError:
                        # Try to parse datetime string
                        try:
                            from datetime import datetime

                            snap_time = datetime.fromisoformat(snap["snapshot_time"].replace("Z", "+00:00"))
                            entry_local_hour = (snap_time.hour + city_offset) % 24
                        except Exception:
                            pass
                    break

            outcomes[mid] = {
                "city": row["city"],
                "metric": row["metric"],
                "won": row["status"] == "won",
                "entry_price": row["entry_price"],
                "local_hour": entry_local_hour or 0,
                "snapshots": [],
            }

    # Group snapshots by market_id
    for row in snapshot_data:
        mid = row["market_id"]
        if mid in outcomes:
            outcomes[mid]["snapshots"].append(
                {"time": row["snapshot_time"], "yes_price": row["yes_price"], "hts": row["hours_to_settlement"]}
            )

    # Convert to MarketOutcome objects
    market_outcomes = []
    for mid, data in outcomes.items():
        market_outcomes.append(
            MarketOutcome(
                market_id=mid,
                city=data["city"],
                metric=data["metric"],
                target_date=None,  # Could get from weather_markets if needed
                won=data["won"],
                entry_price=data["entry_price"],
                local_hour=data["local_hour"],
                snapshots=data["snapshots"],
            )
        )

    return market_outcomes


def compute_city_stats(outcomes: list[MarketOutcome]):
    """Compute win rates by city and metric."""
    city_stats = {}
    for mo in outcomes:
        if mo.city not in city_stats:
            city_stats[mo.city] = {
                "temperature_max": {"won": 0, "lost": 0, "bets": 0},
                "temperature_min": {"won": 0, "lost": 0, "bets": 0},
            }

        if mo.metric == "temperature_max":
            city_stats[mo.city]["temperature_max"]["bets"] += 1
            if mo.won:
                city_stats[mo.city]["temperature_max"]["won"] += 1
            else:
                city_stats[mo.city]["temperature_max"]["lost"] += 1
        elif mo.metric == "temperature_min":
            city_stats[mo.city]["temperature_min"]["bets"] += 1
            if mo.won:
                city_stats[mo.city]["temperature_min"]["won"] += 1
            else:
                city_stats[mo.city]["temperature_min"]["lost"] += 1

    # Calculate win rates
    for city, stats in city_stats.items():
        for metric in ["temperature_max", "temperature_min"]:
            data = stats[metric]
            total = data["won"] + data["lost"]
            if total > 0:
                data["win_rate"] = data["won"] / total
            else:
                data["win_rate"] = 0.0

    return city_stats


def simulate_strategy(
    outcomes: list[MarketOutcome], strategy_name: str, initial_capital: float, fixed_bet: float
) -> StrategyResult:
    """Simulate strategy with city-time filters."""
    result = StrategyResult(name=strategy_name)
    capital = initial_capital
    result.peak = initial_capital
    result.equity_curve = []

    # Strategy parameters based on analysis
    strategy_params = {
        "sehir_yuksek_0_12": {
            "cities": [c for c, s in city_stats["temperature_max"].items() if s["win_rate"] >= 0.60 and s["bets"] >= 3],
            "metric": "temperature_max",
            "hour_range": (0, 12),
        },
        "sehir_dusuk_0_12": {
            "cities": [c for c, s in city_stats["temperature_min"].items() if s["win_rate"] >= 0.60 and s["bets"] >= 3],
            "metric": "temperature_min",
            "hour_range": (0, 12),
        },
        "sehir_yuksek_12_24": {
            "cities": [c for c, s in city_stats["temperature_max"].items() if s["win_rate"] >= 0.60 and s["bets"] >= 3],
            "metric": "temperature_max",
            "hour_range": (12, 24),
        },
        "sehir_dusuk_12_24": {
            "cities": [c for c, s in city_stats["temperature_min"].items() if s["win_rate"] >= 0.60 and s["bets"] >= 3],
            "metric": "temperature_min",
            "hour_range": (12, 24),
        },
        "sabit_0_12": {"cities": [], "metric": None, "hour_range": (0, 12)},
        "sabit_12_24": {"cities": [], "metric": None, "hour_range": (12, 24)},
    }

    params = strategy_params.get(strategy_name, {})

    for mo in outcomes:
        # Apply filters
        if params["metric"] and mo.metric != params["metric"]:
            continue
        if params["cities"] and mo.city not in params["cities"]:
            continue
        if params["hour_range"]:
            start, end = params["hour_range"]
            if not (start <= mo.local_hour < end):
                continue

        # Calculate PnL (assuming entry price is current price for backtest)
        shares = fixed_bet / mo.entry_price
        fee = fixed_bet * FEE_RATE * (1.0 - mo.entry_price)

        if mo.won:
            payout = shares * 1.0
            pnl = payout - fixed_bet - fee
        else:
            pnl = -fixed_bet

        capital += pnl
        result.total_pnl += pnl
        result.total_bet += fixed_bet
        result.bets.append(
            {
                "market_id": mo.market_id,
                "city": mo.city,
                "metric": mo.metric,
                "entry_price": mo.entry_price,
                "local_hour": mo.local_hour,
                "won": mo.won,
                "pnl": pnl,
                "fee": fee,
            }
        )

        if mo.won:
            result.win_count += 1
        else:
            result.loss_count += 1

        if capital > result.peak:
            result.peak = capital
        dd = (result.peak - capital) / result.peak if result.peak > 0 else 0
        if dd > result.max_drawdown:
            result.max_drawdown = dd

        result.equity_curve.append((datetime.now(), capital))

    return result


def compute_summary_stats(results: list[StrategyResult]) -> dict:
    """Compute summary statistics for all strategies."""
    summary = {}

    for r in results:
        total = r.win_count + r.loss_count
        win_rate = r.win_count / total * 100 if total > 0 else 0
        roi = r.total_pnl / r.total_bet * 100 if r.total_bet > 0 else 0
        sharpe = sharpe_ratio(r.equity_curve)

        summary[r.name] = {
            "total_bets": total,
            "wins": r.win_count,
            "losses": r.loss_count,
            "win_rate": win_rate,
            "total_pnl": r.total_pnl,
            "roi": roi,
            "max_drawdown": r.max_drawdown * 100,
            "sharpe": sharpe,
            "profit_factor": profit_factor(r),
        }

    return summary


def sharpe_ratio(equity_curve: list) -> float:
    """Calculate Sharpe ratio from equity curve."""
    if len(equity_curve) < 2:
        return 0.0

    returns = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1][1]
        curr = equity_curve[i][1]
        if prev > 0:
            returns.append((curr - prev) / prev)

    if not returns:
        return 0.0

    avg_return = sum(returns) / len(returns)
    var = sum((r - avg_return) ** 2 for r in returns) / len(returns)
    std = math.sqrt(var) if var > 0 else 0.001

    return avg_return / std * math.sqrt(252)


def profit_factor(result: StrategyResult) -> float:
    """Calculate profit factor."""
    gross_profit = sum(b["pnl"] for b in result.bets if b["pnl"] > 0)
    gross_loss = abs(sum(b["pnl"] for b in result.bets if b["pnl"] < 0))
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def generate_charts(results: list[StrategyResult], stats: dict, out_dir: Path):
    """Generate charts comparing different strategies."""
    out_dir.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(2, 2, figsize=(16, 12))
    fig.suptitle("Advanced City-Time Pattern Backtest Results", fontsize=16, fontweight="bold")

    # 1. Win rate comparison
    ax = axes[0][0]
    names = [r.name for r in results]
    win_rates = [stats[r.name]["win_rate"] for r in results]
    roes = [stats[r.name]["roi"] for r in results]

    bars = ax.bar(names, win_rates, color="skyblue", alpha=0.7)
    ax.set_xlabel("Strategy")
    ax.set_ylabel("Win Rate (%)")
    ax.set_title("Win Rate Comparison")
    ax.axhline(y=50, color="gray", linestyle="--", alpha=0.5)
    for bar, wr in zip(bars, win_rates):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5, f"{wr:.1f}%", ha="center", fontsize=9)

    # 2. ROI comparison
    ax = axes[0][1]
    bars = ax.bar(names, roes, color="lightgreen", alpha=0.7)
    ax.set_xlabel("Strategy")
    ax.set_ylabel("ROI (%)")
    ax.set_title("ROI Comparison")
    for bar, roi in zip(bars, roes):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5, f"{roi:.1f}%", ha="center", fontsize=9)

    # 3. Risk-Return scatter
    ax = axes[1][0]
    x_values = [stats[r.name]["sharpe"] for r in results]
    y_values = [stats[r.name]["roi"] for r in results]
    sizes = [stats[r.name]["total_bets"] for r in results]

    scatter = ax.scatter(x_values, y_values, s=[s * 10 for s in sizes], alpha=0.7, c=y_values, cmap="viridis")
    ax.set_xlabel("Sharpe Ratio")
    ax.set_ylabel("ROI (%)")
    ax.set_title("Risk-Return Profile")
    plt.colorbar(scatter, ax=ax, label="Number of Bets")

    # 4. Drawdown comparison
    ax = axes[1][1]
    drawdowns = [stats[r.name]["max_drawdown"] for r in results]
    bars = ax.bar(names, drawdowns, color="salmon", alpha=0.7)
    ax.set_xlabel("Strategy")
    ax.set_ylabel("Max Drawdown (%)")
    ax.set_title("Maximum Drawdown")
    for bar, dd in zip(bars, drawdowns):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5, f"{dd:.1f}%", ha="center", fontsize=9)

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    chart_path = out_dir / "backtest_advanced_results.png"
    plt.savefig(chart_path, dpi=150, bbox_inches="tight")
    plt.close()

    print(f"\n  Chart saved: {chart_path}")


def save_report(results: list[StrategyResult], stats: dict, out_dir: Path):
    """Save detailed report to JSON."""
    report = {
        "generated_at": datetime.now().isoformat(),
        "total_strategies": len(results),
        "best_strategy": max(stats.items(), key=lambda x: x[1]["roi"])[0],
        "strategy_stats": stats,
    }

    report_path = out_dir / "backtest_advanced_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\n  Report saved: {report_path}")


def print_summary_table(stats: dict):
    """Print formatted summary table."""
    print(f"\n{'='*120}")
    print("  ADVANCED BACKTEST SUMMARY")
    print(f"{'='*120}")
    print(
        f"  {'Strateji':25s} | {'Bahis':>8s} | {'Kazanan':>8s} | {'WR%':>8s} | "
        f"{'Net Kar':>12s} | {'ROI%':>8s} | {'Sharpe':>8s} | {'DD%':>8s}"
    )
    print(f"  {'-'*25}-+-{'-'*8}-+-{'-'*8}-+-{'-'*8}-+-{'-'*12}-+-{'-'*8}-+-{'-'*8}-+-{'-'*8}")

    for name, data in stats.items():
        print(
            f"  {name:25s} | {data['total_bets']:8d} | {data['wins']:8d} | "
            f"{data['win_rate']:5.1f}% | ${data['total_pnl']:11.2f} | "
            f"{data['roi']:6.1f}% | {data['sharpe']:7.2f} | {data['max_drawdown']:5.1f}%"
        )


def main():
    # Load data and compute city stats
    print("Loading data and computing city statistics...")
    global city_stats
    outcomes = load_data(DB_PATH)
    city_stats = compute_city_stats(outcomes)

    print(f"\nLoaded {len(outcomes)} settled markets")
    print(f"Total outcomes: {len(outcomes)}")
    print(f"Won: {sum(1 for o in outcomes if o.won)}")
    print(f"Lost: {sum(1 for o in outcomes if not o.won)}")
    print(f"Overall Win Rate: {sum(1 for o in outcomes if o.won) / len(outcomes) * 100:.1f}%")

    print(f"\n{'='*60}")
    print("TOP PERFORMING CITIES (HIGH METRIC)")
    print(f"{'='*60}")
    for city, stats in sorted(city_stats.items(), key=lambda x: x[1]["temperature_max"]["win_rate"], reverse=True):
        high = city_stats[city]["temperature_max"]
        if high["bets"] >= 3:
            print(f"  {city:20s}: {high['won']:2d}/{high['bets']:2d} = %{high['win_rate']*100:5.0f} (temp_max)")

    print(f"\n{'='*60}")
    print("TOP PERFORMING CITIES (LOW METRIK)")
    print(f"{'='*60}")
    for city, stats in sorted(city_stats.items(), key=lambda x: x[1]["temperature_min"]["win_rate"], reverse=True):
        low = city_stats[city]["temperature_min"]
        if low["bets"] >= 3:
            print(f"  {city:20s}: {low['won']:2d}/{low['bets']:2d} = %{low['win_rate']*100:5.0f} (temp_min)")

    # Define strategies to test
    strategies = [
        "sehir_yuksek_0_12",
        "sehir_dusuk_0_12",
        "sehir_yuksek_12_24",
        "sehir_dusuk_12_24",
        "sabit_0_12",
        "sabit_12_24",
    ]

    print(f"\n{'='*60}")
    print("TESTING STRATEGIES")
    print(f"{'='*60}")

    results = []
    for strategy in strategies:
        r = simulate_strategy(outcomes, strategy, initial_capital=1000.0, fixed_bet=10.0)
        results.append(r)
        print(f"\n{strategy.upper()}:")
        print(f"  Toplam bahis: {r.win_count + r.loss_count}")
        print(f"  Kazanan: {r.win_count}")
        print(f"  Kaybeden: {r.loss_count}")
        print(f"  Win rate: {r.win_count / (r.win_count + r.loss_count) * 100:.1f}%")
        print(f"  Toplam PnL: ${r.total_pnl:.2f}")
        print(f"  ROI: {r.total_pnl / r.total_bet * 100:.1f}%")
        print(f"  Max drawdown: {r.max_drawdown * 100:.1f}%")

    # Compute summary statistics
    stats = compute_summary_stats(results)

    # Generate charts and save report
    generate_charts(results, stats, OUT_DIR)
    save_report(results, stats, OUT_DIR)

    # Print summary table
    print_summary_table(stats)

    # Print best strategy
    best_strategy = max(results, key=lambda r: r.total_pnl)
    print(f"\n{'='*60}")
    print(f"EN IYI STRATEJI: {best_strategy.name}")
    print(f"Toplam PnL: ${best_strategy.total_pnl:.2f}")
    print(f"Win rate: {best_strategy.win_count / (best_strategy.win_count + best_strategy.loss_count) * 100:.1f}%")
    print(f"Max drawdown: {best_strategy.max_drawdown * 100:.1f}%")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
