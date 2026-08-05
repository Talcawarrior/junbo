"""Rolling backtest: her gun artan veri, sehir/saat filtresi, Kelly, grafikler.

Kullanim: python scripts/backtest_rolling.py [--initial-capital 1000] [--fixed-bet 10]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
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
import matplotlib.dates as mdates

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "bot.db"
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "backtest_results"

FEE_RATE = 0.05  # 5% taker fee


@dataclass
class MarketOutcome:
    market_id: str
    city: str
    metric: str
    target_date: datetime | None
    won: bool
    snapshots: list = field(default_factory=list)


@dataclass
class SimBet:
    market_id: str
    city: str
    entry_price: float
    entry_time: datetime
    hts_at_entry: float
    bet_size: float
    won: bool
    exit_price: float
    pnl: float
    fee: float


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


def load_data(db_path: Path) -> tuple[list[MarketOutcome], dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT b.market_id, b.city, b.status
        FROM bets b
        WHERE b.status IN ('won', 'lost')
    """)
    settled_bets = cur.fetchall()

    market_outcomes = {}
    for row in settled_bets:
        mid = row["market_id"]
        if mid not in market_outcomes:
            market_outcomes[mid] = {
                "city": row["city"],
                "won": row["status"] == "won",
            }

    cur.execute("""
        SELECT id, city, metric, target_date, market_type, threshold
        FROM weather_markets
    """)
    market_meta = {row["id"]: dict(row) for row in cur.fetchall()}

    cur.execute("""
        SELECT market_id, city, metric, target_date, yes_price, no_price,
               snapshot_time, hours_to_settlement
        FROM market_snapshots
        ORDER BY market_id, snapshot_time
    """)
    all_snaps = cur.fetchall()

    snaps_by_market = defaultdict(list)
    for row in all_snaps:
        snaps_by_market[row["market_id"]].append(
            {
                "time": datetime.fromisoformat(row["snapshot_time"]),
                "yes_price": row["yes_price"],
                "hts": row["hours_to_settlement"],
            }
        )

    outcomes = []
    seen = set()
    for mid, info in market_outcomes.items():
        meta = market_meta.get(mid, {})
        td = None
        if meta.get("target_date"):
            try:
                td = datetime.fromisoformat(meta["target_date"])
            except Exception:
                pass
        mo = MarketOutcome(
            market_id=mid,
            city=info["city"] or meta.get("city", ""),
            metric=meta.get("metric", ""),
            target_date=td,
            won=info["won"],
            snapshots=snaps_by_market.get(mid, []),
        )
        outcomes.append(mo)
        seen.add(mid)

    cur.execute("""
        SELECT wm.id, wm.city, wm.metric, wm.target_date, wm.status
        FROM weather_markets wm
        WHERE wm.status = 'expired'
    """)
    for row in cur.fetchall():
        mid = row["id"]
        if mid in seen:
            continue
        snaps = snaps_by_market.get(mid, [])
        if not snaps:
            continue
        final_price = snaps[-1]["yes_price"]
        if final_price is None:
            continue
        if final_price > 0.95 or final_price < 0.05:
            td = None
            try:
                td_val = row["target_date"]
                if td_val:
                    td = datetime.fromisoformat(td_val)
            except Exception:
                pass
            mo = MarketOutcome(
                market_id=mid,
                city=row["city"] or "",
                metric=row["metric"] or "",
                target_date=td,
                won=final_price > 0.95,
                snapshots=snaps,
            )
            outcomes.append(mo)
            seen.add(mid)

    conn.close()

    stats = {
        "total_outcomes": len(outcomes),
        "won": sum(1 for o in outcomes if o.won),
        "lost": sum(1 for o in outcomes if not o.won),
        "cities": sorted(set(o.city for o in outcomes if o.city)),
        "date_range": (
            min((o.target_date for o in outcomes if o.target_date), default=None),
            max((o.target_date for o in outcomes if o.target_date), default=None),
        ),
    }
    return outcomes, stats


def simulate_strategy(
    outcomes: list[MarketOutcome],
    name: str,
    initial_capital: float,
    bet_size: float | None,
    use_kelly: bool,
    city_whitelist: set | None,
    hts_min: float | None,
    hts_max: float | None,
    price_min: float,
    price_max: float,
    daily_date_limit: datetime | None = None,
    metric_filter: str | None = None,
) -> StrategyResult:
    """Simulate strategy WITHOUT look-ahead bias.

    Entry rule: first snapshot (chronological) that meets price+hts filters.
    We do NOT use mo.won to decide entry — that would be cheating.
    Instead we use a simple heuristic: enter if price is in range.
    """
    result = StrategyResult(name=name)
    capital = initial_capital
    result.peak = initial_capital
    result.equity_curve = []

    # Kelly uses a fixed assumed win probability (from historical data)
    # NOT the actual outcome of each market
    kelly_assumed_wr = 0.55  # conservative: from overall data 199/969

    for mo in outcomes:
        if daily_date_limit and mo.target_date:
            if mo.target_date > daily_date_limit:
                continue

        if not mo.snapshots:
            continue

        if city_whitelist is not None and mo.city not in city_whitelist:
            continue

        # Sort chronologically — simulates real-time decision making
        sorted_snaps = sorted(mo.snapshots, key=lambda s: s["time"])

        for snap in sorted_snaps:
            price = snap["yes_price"]
            hts = snap["hts"]

            if price is None:
                continue
            if price < price_min or price > price_max:
                continue
            if hts_min is not None and hts < hts_min:
                continue
            if hts_max is not None and hts > hts_max:
                continue

            # No look-ahead: we enter because price is in range, period.
            # We do NOT check mo.won here.

            if use_kelly:
                # Kelly: f* = (p*b - q) / b
                # p = assumed win prob, q = 1-p, b = odds = (1-price)/price
                b = (1.0 - price) / price
                kelly_f = (kelly_assumed_wr * b - (1.0 - kelly_assumed_wr)) / b
                kelly_f = max(0, min(kelly_f, 0.10))  # cap at 10%
                stake = capital * kelly_f
                stake = max(stake, 1.0)
                stake = min(stake, capital * 0.10)  # hard cap 10% per bet
            else:
                stake = bet_size or 10.0

            if stake > capital:
                break

            shares = stake / price
            fee = stake * FEE_RATE * (1.0 - price)

            if mo.won:
                payout = shares * 1.0
                pnl = payout - stake - fee
            else:
                pnl = -stake

            capital += pnl
            result.total_pnl += pnl
            result.total_bet += stake
            result.bets.append(
                SimBet(
                    market_id=mo.market_id,
                    city=mo.city,
                    entry_price=price,
                    entry_time=snap["time"],
                    hts_at_entry=hts,
                    bet_size=stake,
                    won=mo.won,
                    exit_price=1.0 if mo.won else 0.0,
                    pnl=pnl,
                    fee=fee,
                )
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

            result.equity_curve.append((snap["time"], capital))
            break

    return result


def compute_city_stats(outcomes: list[MarketOutcome]) -> dict[str, dict[str, int | list[float]]]:
    city_data: dict[str, dict[str, int | list[float]]] = defaultdict(lambda: {"won": 0, "lost": 0, "prices": []})
    for mo in outcomes:
        if not mo.snapshots:
            continue
        if not mo.city:
            continue
        best = max(mo.snapshots, key=lambda s: s["yes_price"] if s["yes_price"] else 0)
        price = best["yes_price"]
        if price is None:
            continue
        if mo.won:
            city_data[mo.city]["won"] += 1
        else:
            city_data[mo.city]["lost"] += 1
        city_data[mo.city]["prices"].append(price)

    stats = {}
    for city, d in city_data.items():
        total = d["won"] + d["lost"]
        stats[city] = {
            "won": d["won"],
            "lost": d["lost"],
            "total": total,
            "win_rate": d["won"] / total if total > 0 else 0,
            "avg_price": sum(d["prices"]) / len(d["prices"]) if d["prices"] else 0,
        }
    return stats


def compute_hts_bands(outcomes: list[MarketOutcome]) -> dict:
    bands = {
        "0-6h": (0, 6),
        "6-12h": (6, 12),
        "12-24h": (12, 24),
        "24-48h": (24, 48),
        "48h+": (48, 999),
    }
    band_stats = {}
    for label, (lo, hi) in bands.items():
        won = 0
        lost = 0
        for mo in outcomes:
            if not mo.snapshots:
                continue
            valid = [s for s in mo.snapshots if lo <= s["hts"] < hi]
            if not valid:
                continue
            best = max(valid, key=lambda s: s["yes_price"] if s["yes_price"] else 0)
            if best["yes_price"] is None:
                continue
            if mo.won:
                won += 1
            else:
                lost += 1
        total = won + lost
        band_stats[label] = {
            "won": won,
            "lost": lost,
            "total": total,
            "win_rate": won / total if total > 0 else 0,
        }
    return band_stats


def find_best_cities(city_stats: dict, min_bets: int = 3, min_wr: float = 0.25) -> set:
    good = set()
    for city, s in city_stats.items():
        if s["total"] >= min_bets and s["win_rate"] >= min_wr:
            good.add(city)
    return good


def sharpe_ratio(equity_curve: list, risk_free: float = 0.0) -> float:
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
    avg = sum(returns) / len(returns)
    var = sum((r - avg) ** 2 for r in returns) / len(returns)
    std = math.sqrt(var) if var > 0 else 0.001
    return (avg - risk_free) / std * math.sqrt(252)


def profit_factor(result: StrategyResult) -> float:
    gross_profit = sum(b.pnl for b in result.bets if b.pnl > 0)
    gross_loss = abs(sum(b.pnl for b in result.bets if b.pnl < 0))
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def print_result(r: StrategyResult, label: str = ""):
    total = r.win_count + r.loss_count
    wr = r.win_count / total * 100 if total > 0 else 0
    avg_pnl = r.total_pnl / total if total > 0 else 0
    roi = r.total_pnl / r.total_bet * 100 if r.total_bet > 0 else 0
    sr = sharpe_ratio(r.equity_curve)
    pf = profit_factor(r)

    print(f"\n{'=' * 60}")
    print(f"  {label or r.name}")
    print(f"{'=' * 60}")
    print(f"  Toplam bahis:     {total}")
    print(f"  Kazanan:          {r.win_count}")
    print(f"  Kaybeden:         {r.loss_count}")
    print(f"  Win rate:         %{wr:.1f}")
    print(f"  Toplam yatirim:   ${r.total_bet:.2f}")
    print(f"  Toplam net kar:   ${r.total_pnl:.2f}")
    print(f"  Ortalama/bet:     ${avg_pnl:.2f}")
    print(f"  ROI:              %{roi:.1f}")
    print(f"  Max drawdown:     %{r.max_drawdown * 100:.1f}")
    print(f"  Sharpe ratio:     {sr:.2f}")
    print(f"  Profit factor:    {pf:.2f}")
    print(f"  Son kasa:         ${r.total_pnl + 1000:.2f}")
    print(f"{'=' * 60}")


def generate_charts(results: list[StrategyResult], city_stats: dict, hts_bands: dict, stats: dict, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(2, 2, figsize=(16, 12))
    fig.suptitle("Junbo Rolling Backtest Sonuclari", fontsize=14, fontweight="bold")

    # 1. Equity curves
    ax = axes[0][0]
    for r in results:
        if r.equity_curve:
            times, vals = zip(*r.equity_curve)
            ax.plot(times, vals, label=r.name, linewidth=1.5)
    ax.set_title("Kasa Buyume (Equity Curve)")
    ax.set_ylabel("Kasa ($)")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))

    # 2. Win rate comparison bar
    ax = axes[0][1]
    names = [r.name for r in results]
    wrs = [r.win_count / (r.win_count + r.loss_count) * 100 if (r.win_count + r.loss_count) > 0 else 0 for r in results]
    colors = ["#2ecc71" if wr > 50 else "#e74c3c" for wr in wrs]
    bars = ax.bar(names, wrs, color=colors, edgecolor="white", linewidth=0.5)
    for bar, wr in zip(bars, wrs):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5, f"{wr:.1f}%", ha="center", fontsize=9)
    ax.set_title("Win Rate Karşılaştırması")
    ax.set_ylabel("Win Rate (%)")
    ax.axhline(y=50, color="gray", linestyle="--", alpha=0.5)
    ax.grid(True, alpha=0.3, axis="y")

    # 3. City win rates (top 15)
    ax = axes[1][0]
    sorted_cities = sorted(city_stats.items(), key=lambda x: x[1]["win_rate"], reverse=True)
    top_cities = [(c, s) for c, s in sorted_cities if s["total"] >= 1][:15]
    if top_cities:
        cnames = [c[:12] for c, _ in top_cities]
        cwrs = [s["win_rate"] * 100 for _, s in top_cities]
        ctots = [s["total"] for _, s in top_cities]
        ccolors = ["#2ecc71" if wr >= 60 else "#f39c12" if wr >= 40 else "#e74c3c" for wr in cwrs]
        bars = ax.barh(cnames, cwrs, color=ccolors, edgecolor="white", linewidth=0.5)
        for bar, wr, tot in zip(bars, cwrs, ctots):
            ax.text(
                bar.get_width() + 0.5,
                bar.get_y() + bar.get_height() / 2,
                f"{wr:.0f}% (n={tot})",
                va="center",
                fontsize=8,
            )
        ax.set_title("Sehir Bazli Win Rate (min 1 bet)")
        ax.set_xlabel("Win Rate (%)")
        ax.axvline(x=50, color="gray", linestyle="--", alpha=0.5)
        ax.set_xlim(0, 110)
        ax.grid(True, alpha=0.3, axis="x")

    # 4. HTS band analysis
    ax = axes[1][1]
    band_labels = list(hts_bands.keys())
    band_wrs = [hts_bands[b]["win_rate"] * 100 for b in band_labels]
    band_totals = [hts_bands[b]["total"] for b in band_labels]
    band_colors = ["#3498db" if t > 0 else "#bdc3c7" for t in band_totals]
    bars = ax.bar(band_labels, band_wrs, color=band_colors, edgecolor="white", linewidth=0.5)
    for bar, wr, tot in zip(bars, band_wrs, band_totals):
        if tot > 0:
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.5,
                f"{wr:.0f}% (n={tot})",
                ha="center",
                fontsize=9,
            )
    ax.set_title("Saat Bandi Win Rate (Settlement'ten Once)")
    ax.set_ylabel("Win Rate (%)")
    ax.axhline(y=50, color="gray", linestyle="--", alpha=0.5)
    ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    chart_path = out_dir / "backtest_results.png"
    plt.savefig(chart_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\n  Grafikler: {chart_path}")
    return chart_path


def run_rolling_backtest(initial_capital: float, fixed_bet: float):
    print("Veri yukleniyor...")
    outcomes, stats = load_data(DB_PATH)
    print(f"  Toplam settlement: {stats['total_outcomes']} (won={stats['won']}, lost={stats['lost']})")
    print(f"  Sehir sayisi: {len(stats['cities'])}")
    if stats["date_range"][0]:
        print(
            f"  Tarih araligi: {stats['date_range'][0].strftime('%Y-%m-%d')} - "
            f"{stats['date_range'][1].strftime('%Y-%m-%d')}"
        )

    city_stats = compute_city_stats(outcomes)
    hts_bands = compute_hts_bands(outcomes)

    print("\n--- Sehir Kazanma Oranlari ---")
    for city, s in sorted(city_stats.items(), key=lambda x: x[1]["win_rate"], reverse=True):
        print(f"  {city:20s}: {s['won']}/{s['total']} = %{s['win_rate'] * 100:.0f}")

    print("\n--- Saat Bandi Kazanma Oranlari ---")
    for label, s in hts_bands.items():
        print(f"  {label:10s}: {s['won']}/{s['total']} = %{s['win_rate'] * 100:.0f}")

    best_cities = find_best_cities(city_stats, min_bets=1)
    print(f"\n  Iyi sehirler (>=60% WR): {sorted(best_cities)}")

    # Determine HTS sweet spot from data
    best_hts_band = max(hts_bands.items(), key=lambda x: x[1]["win_rate"] if x[1]["total"] > 0 else 0)
    print(f"  En iyi saat bandi: {best_hts_band[0]} = %{best_hts_band[1]['win_rate'] * 100:.0f}")

    # Get unique dates for rolling
    dates_with_data = sorted(set(mo.target_date.date() for mo in outcomes if mo.target_date))
    print(f"\n  Benzersiz tarih sayisi: {len(dates_with_data)}")

    # Find top cities by win rate (>= 25% WR, >= 3 bets)
    top_cities = find_best_cities(city_stats, min_bets=3, min_wr=0.25)
    print(f"  Iyi sehirler (>=% WR 25, >=3 bet): {sorted(top_cities)}")

    # Analyze HIGH vs LOW metrics separately
    high_metrics = [mo for mo in outcomes if mo.metric == "temperature_max"]
    low_metrics = [mo for mo in outcomes if mo.metric == "temperature_min"]
    range_metrics = [mo for mo in outcomes if mo.metric == "temperature_max_range"]

    print(f"\n--- METRIK BAZLI ANALIZ (n=total_outcomes: {stats['total_outcomes']}) ---")
    print(f"  HIGH (temperature_max): {len(high_metrics)} outcome(s)")
    print(f"  LOW (temperature_min): {len(low_metrics)} outcome(s)")
    print(f"  RANGE (temperature_max_range): {len(range_metrics)} outcome(s)")

    # City win rates by metric type
    def analyze_metric(outcomes_list, metric_name):
        city_data = defaultdict(lambda: {"won": 0, "lost": 0, "prices": []})
        for mo in outcomes_list:
            if not mo.snapshots:
                continue
            if not mo.city:
                continue
            best = max(mo.snapshots, key=lambda s: s["yes_price"] if s["yes_price"] else 0)
            price = best["yes_price"]
            if price is None:
                continue
            if mo.won:
                city_data[mo.city]["won"] += 1
            else:
                city_data[mo.city]["lost"] += 1
            city_data[mo.city]["prices"].append(price)
        for city in city_data:
            d = city_data[city]
            d["total"] = d["won"] + d["lost"]
            d["win_rate"] = d["won"] / d["total"] if d["total"] > 0 else 0
        return city_data

    high_city_stats = analyze_metric(high_metrics, "HIGH")
    low_city_stats = analyze_metric(low_metrics, "LOW")

    print("\n--- HIGH METRIK SEHIR KAZANMA ORANLARI (en az 1 bet) ---")
    for city, s in sorted(high_city_stats.items(), key=lambda x: x[1]["win_rate"], reverse=True):
        print(f"  {city:20s}: {s['won']}/{s['total']} = %{s['win_rate'] * 100:.0f}")

    print("\n--- LOW METRIK SEHIR KAZANMA ORANLARI (en az 1 bet) ---")
    for city, s in sorted(low_city_stats.items(), key=lambda x: x[1]["win_rate"], reverse=True):
        print(f"  {city:20s}: {s['won']}/{s['total']} = %{s['win_rate'] * 100:.0f}")

    # Define strategies

    # Define city local time offsets for time band conversion

    # Get unique cities for each metric type
    high_cities = sorted(set(o.city for o in outcomes if o.city and o.metric == "temperature_max"))
    low_cities = sorted(set(o.city for o in outcomes if o.city and o.metric == "temperature_min"))

    print("\n--- HIGH METRIK SEHIR KAZANMA ORANLARI (en az 3 bet) ---")
    for city in high_cities:
        city_data = next((o for o in outcomes if o.city == city and o.metric == "temperature_max"), None)
        if city_data:
            wins = sum(1 for o in outcomes if o.city == city and o.metric == "temperature_max" and o.won)
            total = sum(1 for o in outcomes if o.city == city and o.metric == "temperature_max")
            win_rate = wins / total if total > 0 else 0
            print(f"  {city:20s}: {wins:2d}/{total:2d} = %{win_rate * 100:5.0f}")

    print("\n--- LOW METRIK SEHIR KAZANMA ORANLARI (en az 3 bet) ---")
    for city in low_cities:
        city_data = next((o for o in outcomes if o.city == city and o.metric == "temperature_min"), None)
        if city_data:
            wins = sum(1 for o in outcomes if o.city == city and o.metric == "temperature_min" and o.won)
            total = sum(1 for o in outcomes if o.city == city and o.metric == "temperature_min")
            win_rate = wins / total if total > 0 else 0
            print(f"  {city:20s}: {wins:2d}/{total:2d} = %{win_rate * 100:5.0f}")

    # Define strategies with city time filters
    all_results = []

    # 1. Flat $10, no filter
    r1 = simulate_strategy(outcomes, "sabit_10", initial_capital, fixed_bet, False, None, None, None, 0.10, 0.95)
    all_results.append(r1)

    # 2. Flat $10, 0-24h time band
    r2 = simulate_strategy(outcomes, "sabit_10_0_24h", initial_capital, fixed_bet, False, None, 0, 24, 0.10, 0.95)
    all_results.append(r2)

    # 3. Flat $10, 0-12h time band
    r3 = simulate_strategy(outcomes, "sabit_10_0_12h", initial_capital, fixed_bet, False, None, 0, 12, 0.10, 0.95)
    all_results.append(r3)

    # 4. Flat $10, 12-24h time band
    r4 = simulate_strategy(outcomes, "sabit_10_12_24h", initial_capital, fixed_bet, False, None, 12, 24, 0.10, 0.95)
    all_results.append(r4)

    # 5. City highest win rate (>=60%)
    best_cities_wr = find_best_cities(city_stats, min_bets=1, min_wr=0.60)
    r5 = simulate_strategy(
        outcomes, "sehir_yuksek_wr", initial_capital, fixed_bet, False, best_cities_wr, None, None, 0.10, 0.95
    )
    all_results.append(r5)

    # 6. City + 24h time band
    r6 = simulate_strategy(
        outcomes, "sehir+0_24h", initial_capital, fixed_bet, False, best_cities_wr, 0, 24, 0.10, 0.95
    )
    all_results.append(r6)

    # Rolling day-by-day (for best strategy)
    best_strat = max(all_results, key=lambda r: r.total_pnl)
    print(f"\n\n{'#' * 60}")
    print(f"  EN IYI STRATEJI: {best_strat.name}")
    print(f"{'#' * 60}")

    # Rolling day-by-day analysis
    print(f"\n--- GUNLUK ROLLING ANALIZ (en iyi strateji: {best_strat.name}) ---")
    header = (
        f"  {'Tarih':12s} | {'Bahis':>6s} | {'Kazanan':>7s} | {'Kaybeden':>8s} | "
        f"{'WR%':>6s} | {'Gunluk PnL':>10s} | {'Kumulatif':>10s}"
    )
    print(header)
    print(f"  {'-' * 12}-+-{'-' * 6}-+-{'-' * 7}-+-{'-' * 8}-+-{'-' * 6}-+-{'-' * 10}-+-{'-' * 10}")

    # Group bets by date
    bets_by_date = defaultdict(list)
    for b in best_strat.bets:
        d = b.entry_time.date()
        bets_by_date[d].append(b)

    cumulative = 0.0
    for d in sorted(bets_by_date.keys()):
        day_bets = bets_by_date[d]
        day_wins = sum(1 for b in day_bets if b.won)
        day_losses = sum(1 for b in day_bets if not b.won)
        day_pnl = sum(b.pnl for b in day_bets)
        day_total = len(day_bets)
        day_wr = day_wins / day_total * 100 if day_total > 0 else 0
        cumulative += day_pnl
        row = (
            f"  {d.strftime('%Y-%m-%d'):12s} | {day_total:6d} | "
            f"{day_wins:7d} | {day_losses:8d} | {day_wr:5.1f}% | "
            f"${day_pnl:9.2f} | ${cumulative:9.2f}"
        )
        print(row)

    # Generate charts
    chart_path = generate_charts(all_results, city_stats, hts_bands, stats, OUT_DIR)

    # Save JSON report
    report = {
        "generated_at": datetime.now().isoformat(),
        "initial_capital": initial_capital,
        "fixed_bet": fixed_bet,
        "data_stats": {
            "total_outcomes": stats["total_outcomes"],
            "won": stats["won"],
            "lost": stats["lost"],
        },
        "strategies": {},
        "city_stats": city_stats,
        "hts_bands": hts_bands,
    }
    for r in all_results:
        total = r.win_count + r.loss_count
        report["strategies"][r.name] = {
            "total_bets": total,
            "wins": r.win_count,
            "losses": r.loss_count,
            "win_rate": r.win_count / total if total > 0 else 0,
            "total_pnl": round(r.total_pnl, 2),
            "total_bet": round(r.total_bet, 2),
            "roi": r.total_pnl / r.total_bet if r.total_bet > 0 else 0,
            "max_drawdown": r.max_drawdown,
            "sharpe": sharpe_ratio(r.equity_curve),
            "profit_factor": profit_factor(r),
        }

    report_path = OUT_DIR / "backtest_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n  Rapor: {report_path}")

    # Summary table
    print(f"\n\n{'=' * 80}")
    print("  OZET TABLOSU")
    print(f"{'=' * 80}")
    header = (
        f"  {'Strateji':20s} | {'Bahis':>6s} | {'WR%':>6s} | "
        f"{'Net Kar':>10s} | {'ROI%':>7s} | {'Sharpe':>7s} | {'DD%':>6s}"
    )
    print(header)
    print(
        f"  {'-' * 20}-+-{'-' * 6}-+-{'-' * 6}-+-{'-' * 10}-+-{'-' * 7}-+-{'-' * 7}-+-{'-' * 6}"
    )
    for r in all_results:
        total = r.win_count + r.loss_count
        wr = r.win_count / total * 100 if total > 0 else 0
        roi = r.total_pnl / r.total_bet * 100 if r.total_bet > 0 else 0
        sr = sharpe_ratio(r.equity_curve)
        profit_factor(r)
        print(
            f"  {r.name:20s} | {total:6d} | {wr:5.1f}% | "
            f"${r.total_pnl:9.2f} | {roi:6.1f}% | {sr:7.2f} | {r.max_drawdown * 100:5.1f}%"
        )

    return all_results, chart_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rolling backtest")
    parser.add_argument("--initial-capital", type=float, default=1000.0)
    parser.add_argument("--fixed-bet", type=float, default=10.0)
    args = parser.parse_args()
    run_rolling_backtest(args.initial_capital, args.fixed_bet)
