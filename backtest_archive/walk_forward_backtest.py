#!/usr/bin/env python3
"""
Proper Walk-Forward Backtest for Junbo Weather Bot
---------------------------------------------------
- No look-ahead bias
- Time-based folds only
- Decision made with information available at that timestamp
- Settlement used only for evaluation
- min_edge=0.05 (matches bot config)
- Works with the actual data range available
"""

import sqlite3
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass
from collections import defaultdict
from typing import List, Dict, Optional, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BACKTEST_DB = Path(__file__).resolve().parent.parent / "data" / "backtest.db"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "backtest_results" / "walk_forward"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TRAIN_DAYS = 2
TEST_DAYS = 1
STEP_DAYS = 1
MIN_TRAIN_SAMPLES = 5

FLAT_BET = 10.0
FEE_RATE = 0.05
MIN_EDGE = 0.05
MAX_ENTRY_PRICE = 0.90
MAX_HOURS_TO_SETTLEMENT = 24
MIN_HOURS_TO_SETTLEMENT = 1


@dataclass
class BetResult:
    market_id: str
    city: str
    metric: str
    entry_time: datetime
    entry_price: float
    model_prob: float
    edge: float
    net_edge: float
    hours_to_settlement: float
    won: bool
    pnl: float
    fold: int


def load_data() -> Tuple[list, list, list, list]:
    conn = sqlite3.connect(str(BACKTEST_DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT id as market_id, city, city_code, metric, threshold,
               target_date, yes_price, no_price, status
        FROM weather_markets
        WHERE city IS NOT NULL AND target_date IS NOT NULL
    """)
    markets = [dict(r) for r in cur.fetchall()]

    cur.execute("""
        SELECT market_id, city, metric, target_date, yes_price, no_price,
               snapshot_time, hours_to_settlement
        FROM market_snapshots
        ORDER BY market_id, snapshot_time
    """)
    snapshots = [dict(r) for r in cur.fetchall()]

    cur.execute("""
        SELECT market_id, city, metric, target_date, source,
               predicted_value, confidence, model_weight, fetched_at
        FROM weather_forecasts
        ORDER BY market_id, fetched_at
    """)
    forecasts = [dict(r) for r in cur.fetchall()]

    cur.execute("""
        SELECT id as bet_id, market_id, city, outcome, stake_amount,
               entry_price, shares, status, placed_at, settled_at
        FROM bets
        WHERE status IN ('won', 'lost')
        ORDER BY placed_at
    """)
    resolved_bets = [dict(r) for r in cur.fetchall()]

    conn.close()

    for m in markets:
        if m.get("target_date"):
            m["target_date"] = datetime.fromisoformat(m["target_date"])
        if m.get("yes_price") is not None:
            m["yes_price"] = float(m["yes_price"])

    for s in snapshots:
        if s.get("snapshot_time"):
            s["snapshot_time"] = datetime.fromisoformat(s["snapshot_time"])
        if s.get("target_date"):
            s["target_date"] = datetime.fromisoformat(s["target_date"])
        if s.get("yes_price") is not None:
            s["yes_price"] = float(s["yes_price"])
        if s.get("hours_to_settlement") is not None:
            s["hours_to_settlement"] = float(s["hours_to_settlement"])

    for f in forecasts:
        if f.get("fetched_at"):
            f["fetched_at"] = datetime.fromisoformat(f["fetched_at"])
        if f.get("target_date"):
            f["target_date"] = datetime.fromisoformat(f["target_date"])
        if f.get("predicted_value") is not None:
            f["predicted_value"] = float(f["predicted_value"])
        if f.get("confidence") is not None:
            f["confidence"] = float(f["confidence"])
        if f.get("model_weight") is not None:
            f["model_weight"] = float(f["model_weight"])

    for b in resolved_bets:
        if b.get("placed_at"):
            b["placed_at"] = datetime.fromisoformat(b["placed_at"])
        if b.get("settled_at"):
            b["settled_at"] = datetime.fromisoformat(b["settled_at"])

    return markets, snapshots, forecasts, resolved_bets


def estimate_probability(mean: float, std: float, threshold: float, metric: str = "temperature_max") -> float:
    if std is None or std <= 0:
        std = 2.0
    z = (threshold - mean) / std
    if metric in ("temperature_max", "temperature_mean"):
        return 1.0 - _norm_cdf(z)
    else:
        return _norm_cdf(z)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + _erf(x / np.sqrt(2.0)))


def _erf(x: float) -> float:
    a1, a2, a3, a4, a5 = (0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429)
    p = 0.3275911
    sign = 1.0 if x >= 0 else -1.0
    x = abs(x)
    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * np.exp(-x * x)
    return sign * y


def get_available_forecast(forecasts: list, market_id: str, decision_time: datetime) -> Optional[Dict]:
    available = [
        f
        for f in forecasts
        if f["market_id"] == market_id and f.get("fetched_at") is not None and f["fetched_at"] <= decision_time
    ]
    if not available:
        return None
    available.sort(key=lambda f: f["fetched_at"])
    latest = available[-1]
    return {
        "mean": latest.get("predicted_value"),
        "std": latest.get("confidence") or 2.0,
        "weight": latest.get("model_weight") or 1.0,
    }


def simulate_decision(snap: dict, forecast: Optional[Dict], hours_to_settlement: float) -> Optional[Dict]:
    if hours_to_settlement > MAX_HOURS_TO_SETTLEMENT:
        return None
    if hours_to_settlement < MIN_HOURS_TO_SETTLEMENT:
        return None

    entry_price = snap.get("yes_price")
    if entry_price is None or entry_price > MAX_ENTRY_PRICE or entry_price < 0.05:
        return None

    if forecast is None or forecast.get("mean") is None:
        return None

    model_prob = estimate_probability(
        forecast["mean"], forecast["std"], snap.get("threshold", 25), snap.get("metric", "temperature_max")
    )
    model_prob = max(0.01, min(0.99, model_prob))

    edge = model_prob - entry_price
    net_edge = edge - FEE_RATE * entry_price * (1 - entry_price)

    if net_edge < MIN_EDGE:
        return None

    return {
        "model_prob": model_prob,
        "edge": edge,
        "net_edge": net_edge,
        "entry_price": entry_price,
    }


def resolve_outcome_from_bets(resolved_bets: list, market_id: str) -> Optional[bool]:
    for b in resolved_bets:
        if b["market_id"] == market_id:
            return b["status"] == "won"
    return None


def run_single_fold(
    markets: list,
    snapshots: list,
    forecasts: list,
    resolved_bets: list,
    train_start: datetime,
    train_end: datetime,
    test_start: datetime,
    test_end: datetime,
    fold_id: int,
) -> List[BetResult]:
    results = []

    test_snaps = [
        s for s in snapshots if s.get("snapshot_time") is not None and test_start <= s["snapshot_time"] < test_end
    ]

    if not test_snaps:
        return results

    market_lookup = {m["market_id"]: m for m in markets}

    for snap in test_snaps:
        market_id = snap["market_id"]
        decision_time = snap["snapshot_time"]
        hours_to_settlement = snap.get("hours_to_settlement")

        if hours_to_settlement is None:
            target = market_lookup.get(market_id, {}).get("target_date")
            if target:
                hours_to_settlement = (target - decision_time).total_seconds() / 3600
            else:
                continue

        market = market_lookup.get(market_id)
        if market is None:
            continue

        forecast = get_available_forecast(forecasts, market_id, decision_time)
        if forecast is None:
            continue

        decision = simulate_decision(snap, forecast, hours_to_settlement)
        if decision is None:
            continue

        won = resolve_outcome_from_bets(resolved_bets, market_id)
        if won is None:
            continue

        if won:
            pnl = FLAT_BET * (1 / decision["entry_price"] - 1) * (1 - FEE_RATE)
        else:
            pnl = -FLAT_BET

        results.append(
            BetResult(
                market_id=market_id,
                city=market.get("city", "unknown"),
                metric=market.get("metric", "unknown"),
                entry_time=decision_time,
                entry_price=decision["entry_price"],
                model_prob=decision["model_prob"],
                edge=decision["edge"],
                net_edge=decision["net_edge"],
                hours_to_settlement=hours_to_settlement,
                won=won,
                pnl=pnl,
                fold=fold_id,
            )
        )

    return results


def walk_forward(markets: list, snapshots: list, forecasts: list, resolved_bets: list) -> List[BetResult]:
    all_times = []
    for s in snapshots:
        if s.get("snapshot_time"):
            all_times.append(s["snapshot_time"])
    for m in markets:
        if m.get("target_date"):
            all_times.append(m["target_date"])

    if not all_times:
        return []

    min_time = min(all_times)
    max_time = max(all_times)
    print(f"Veri araligi: {min_time} -> {max_time}")

    all_results = []
    fold_id = 0
    current_train_start = min_time

    while True:
        train_end = current_train_start + timedelta(days=TRAIN_DAYS)
        test_start = train_end
        test_end = test_start + timedelta(days=TEST_DAYS)

        if test_end > max_time + timedelta(days=1):
            break

        train_markets = [m for m in markets if m.get("target_date") is not None and m["target_date"] <= train_end]

        train_settled = sum(
            1 for m in train_markets if resolve_outcome_from_bets(resolved_bets, m["market_id"]) is not None
        )

        print(f"\n=== Fold {fold_id} ===")
        print(
            f"  Train: {current_train_start.date()} -> {train_end.date()} "
            f"({len(train_markets)} markets, {train_settled} settled)"
        )
        print(f"  Test : {test_start.date()} -> {test_end.date()}")

        if train_settled < MIN_TRAIN_SAMPLES:
            current_train_start += timedelta(days=STEP_DAYS)
            fold_id += 1
            continue

        fold_results = run_single_fold(
            markets,
            snapshots,
            forecasts,
            resolved_bets,
            current_train_start,
            train_end,
            test_start,
            test_end,
            fold_id,
        )

        print(f"  -> {len(fold_results)} bet")
        all_results.extend(fold_results)

        current_train_start += timedelta(days=STEP_DAYS)
        fold_id += 1

        if fold_id > 100:
            break

    return all_results


def calculate_metrics(results: List[BetResult]) -> Dict:
    if not results:
        return {}

    total_bets = len(results)
    wins = sum(1 for r in results if r.won)
    wr = wins / total_bets if total_bets > 0 else 0

    total_pnl = sum(r.pnl for r in results)
    total_staked = total_bets * FLAT_BET
    roi = total_pnl / total_staked if total_staked > 0 else 0

    pnl_by_date = defaultdict(float)
    for r in results:
        d = r.entry_time.date()
        pnl_by_date[d] += r.pnl

    daily_pnls = list(pnl_by_date.values())
    sd = np.std(daily_pnls) if len(daily_pnls) > 1 else 0
    sharpe = (np.mean(daily_pnls) / sd) * np.sqrt(365) if sd > 0 else 0

    cum_pnl = np.cumsum([r.pnl for r in results])
    peak = np.maximum.accumulate(cum_pnl)
    dd = cum_pnl - peak
    max_dd = float(np.min(dd)) if len(dd) > 0 else 0.0

    return {
        "total_bets": total_bets,
        "wins": wins,
        "win_rate": round(wr, 4),
        "total_pnl": round(total_pnl, 2),
        "total_staked": round(total_staked, 2),
        "roi": round(roi, 4),
        "sharpe": round(sharpe, 2),
        "max_drawdown": round(max_dd, 2),
        "avg_net_edge": round(np.mean([r.net_edge for r in results]), 4),
        "avg_hours_to_settlement": round(np.mean([r.hours_to_settlement for r in results]), 1),
    }


def main():
    print("Walk-Forward Backtest basliyor...\n")

    markets, snapshots, forecasts, resolved_bets = load_data()
    print(
        f"Yuklenen: {len(markets)} market, {len(snapshots)} snapshot, "
        f"{len(forecasts)} forecast, {len(resolved_bets)} resolved bet"
    )

    results = walk_forward(markets, snapshots, forecasts, resolved_bets)

    if not results:
        print("Sonuc yok.")
        return

    df_path = OUTPUT_DIR / "walk_forward_trades.csv"
    with open(df_path, "w", encoding="utf-8") as f:
        f.write(
            "market_id,city,metric,entry_time,entry_price,model_prob,edge,net_edge,hours_to_settlement,won,pnl,fold\n"
        )
        for r in results:
            f.write(
                f"{r.market_id},{r.city},{r.metric},"
                f"{r.entry_time.isoformat()},{r.entry_price:.4f},"
                f"{r.model_prob:.4f},{r.edge:.4f},{r.net_edge:.4f},"
                f"{r.hours_to_settlement:.1f},{r.won},{r.pnl:.2f},{r.fold}\n"
            )
    print(f"\nTrade'ler kaydedildi: {df_path}")

    metrics = calculate_metrics(results)
    print("\n=== GENEL SONUC ===")
    for k, v in metrics.items():
        print(f"  {k:30}: {v}")

    fold_metrics = defaultdict(list)
    for r in results:
        fold_metrics[r.fold].append(r)

    print("\n=== FOLD BAZLI ===")
    print(f"  {'Fold':>5} | {'Bahis':>6} | {'WR%':>6} | {'Net Kar':>10} | {'ROI%':>7}")
    print(f"  {'-'*5}-+-{'-'*6}-+-{'-'*6}-+-{'-'*10}-+-{'-'*7}")
    for fid in sorted(fold_metrics.keys()):
        fm = calculate_metrics(fold_metrics[fid])
        print(
            f"  {fid:5d} | {fm.get('total_bets', 0):6d} | "
            f"{fm.get('win_rate', 0)*100:5.1f}% | "
            f"${fm.get('total_pnl', 0):9.2f} | "
            f"{fm.get('roi', 0)*100:6.1f}%"
        )

    report = {
        "config": {
            "train_days": TRAIN_DAYS,
            "test_days": TEST_DAYS,
            "step_days": STEP_DAYS,
            "min_edge": MIN_EDGE,
            "max_hours": MAX_HOURS_TO_SETTLEMENT,
            "flat_bet": FLAT_BET,
            "fee_rate": FEE_RATE,
        },
        "overall": metrics,
        "folds": {str(fid): calculate_metrics(fold_metrics[fid]) for fid in sorted(fold_metrics.keys())},
    }

    report_path = OUTPUT_DIR / "walk_forward_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\nRapor: {report_path}")


if __name__ == "__main__":
    main()
