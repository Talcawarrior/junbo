"""Fix stale entry prices on open bets (2026-08-10 bug).

Gamma ``outcomePrices`` lagged the executable CLOB book, so several open
bets recorded entry prices that never existed on the real market (e.g.
Beijing 32C 0.18 in DB vs ~0.98 on the book). This script:

  1. Pulls each open bet's market CLOB price history.
  2. Finds the real price closest to ``placed_at``.
  3. If the recorded entry price diverges > 15% from that real price,
     rewrites the bet: entry_price, price, fair_value, shares,
     current_price, entry_fee, pnl (realized) and unrealized_pnl using
     the same formulas as the live bot.

Dry-run by default: pass ``--apply`` to write to the DB.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
import urllib.request

# Make repo-root imports work when run as `python scripts/fix_stale_entry_prices.py`
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from utils.formulas import bet_shares, polymarket_fee_from_stake, unrealized_pnl as compute_unrealized_pnl  # noqa: E402

STALE_DIVERGENCE_PCT = 0.15


def _token_for(raw_data: str | None) -> str | None:
    if not raw_data:
        return None
    try:
        raw = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    ids = raw.get("clobTokenIds")
    if isinstance(ids, str):
        try:
            ids = json.loads(ids)
        except (json.JSONDecodeError, TypeError):
            ids = None
    if isinstance(ids, list) and ids:
        return str(ids[0])
    return None


def _clob_price_at(token: str, ts_iso: str, window_s: int = 3600) -> tuple[int | None, float | None]:
    """Return (unix_ts, price) closest to ``ts_iso`` from CLOB price history."""
    try:
        dt = datetime.datetime.fromisoformat(ts_iso.replace(" ", "T").split(".")[0] + "+00:00")
        target = int(dt.timestamp())
        url = (
            f"https://clob.polymarket.com/prices-history?market={token}"
            f"&startTs={target - window_s}&endTs={target + window_s}&fidelity=1"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=30).read())
        best_ts, best_p, best_diff = None, None, None
        for p in data.get("history", []):
            if isinstance(p, dict):
                pt, pv = int(p["t"]), float(p["p"])
            else:
                pt, pv = int(p[0]), float(p[1])
            diff = abs(pt - target)
            if best_diff is None or diff < best_diff:
                best_diff, best_ts, best_p = diff, pt, pv
        return best_ts, best_p
    except Exception as exc:  # network / parse
        print(f"    [clob] error for {token}: {exc}", file=sys.stderr)
        return None, None


def main() -> int:
    parser = argparse.ArgumentParser(description="Fix stale entry prices on open bets")
    parser.add_argument("--apply", action="store_true", help="write changes to the DB (default: dry-run)")
    parser.add_argument("--market-id", help="only process this market id")
    args = parser.parse_args()

    from database.db import get_session
    from database.models import Bet

    from config.settings import bot_config

    fee_rate = bot_config.strategy.current_fee_rate

    changed = []
    with get_session() as session:
        open_bets = (
            session.query(Bet)
            .filter(Bet.status.in_(["placed", "partial_fill", "filled"]))
            .order_by(Bet.placed_at)
            .all()
        )
        print(f"open bets: {len(open_bets)}")
        for bet in open_bets:
            if args.market_id and bet.market_id != args.market_id:
                continue
            from database.models import WeatherMarket

            market = session.query(WeatherMarket).filter_by(id=bet.market_id).first()
            if market is None:
                print(f"  bet#{bet.id}: market {bet.market_id} not found - skip")
                continue
            token = _token_for(market.raw_data)
            if token is None:
                print(f"  bet#{bet.id} ({market.city}): no YES token - skip")
                continue
            ts, live_price = _clob_price_at(token, str(bet.placed_at))
            if live_price is None:
                print(f"  bet#{bet.id} ({market.city}): no CLOB quote - skip")
                time.sleep(1.0)
                continue
            entry = float(bet.entry_price or 0)
            divergence = abs(live_price - entry) / live_price if live_price > 0 else 999.0
            if divergence <= STALE_DIVERGENCE_PCT:
                print(f"  bet#{bet.id} ({market.city}): entry={entry:.4f} clob={live_price:.4f} ({divergence:.1%}) ok")
                time.sleep(1.0)
                continue

            amount = float(bet.amount or bet.stake or 0)
            shares = bet_shares(amount, live_price)
            fee = polymarket_fee_from_stake(amount, live_price, fee_rate)
            unrealized = round(compute_unrealized_pnl(shares, live_price, live_price) - fee, 2)
            print(
                f"  bet#{bet.id} ({market.city}): entry={entry:.4f} -> {live_price:.4f} "
                f"({divergence:.1%}) shares={bet.shares:.2f}->{shares:.2f} fee={fee:.4f}"
            )
            changed.append(
                {
                    "bet_id": bet.id,
                    "market_id": bet.market_id,
                    "city": market.city,
                    "old_entry": entry,
                    "new_entry": round(live_price, 4),
                    "old_shares": float(bet.shares or 0),
                    "new_shares": round(shares, 6),
                    "divergence_pct": round(divergence * 100, 1),
                }
            )
            if args.apply:
                bet.entry_price = round(live_price, 4)
                bet.price = round(live_price, 4)
                bet.fair_value = round(live_price, 4)
                bet.shares = round(shares, 6)
                bet.current_price = round(live_price, 4)
                bet.entry_fee = round(fee, 4)
                bet.unrealized_pnl = unrealized
            time.sleep(1.0)

        if args.apply:
            session.commit()
            print(f"\nAPPLIED {len(changed)} fixes")
        else:
            print(f"\nDRY-RUN: {len(changed)} bets would be fixed (use --apply)")

    if changed:
        print("\n  summary:")
        for c in changed:
            print(
                f"    bet#{c['bet_id']} {c['city']:<14} {c['old_entry']:.4f} -> "
                f"{c['new_entry']:.4f} ({c['divergence_pct']}%)"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
