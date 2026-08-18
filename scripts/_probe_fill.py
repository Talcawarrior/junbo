#!/usr/bin/env python3
"""FILL-MODEL GERCEKLIK PROBU (2026-08-18).

Simulasyon (scripts/backtest.py gunluk) gercek betin entry fiyatini ne kadar
dogru yeniden uretiyor? Her gercek bet icin:
  real_entry   = bets.entry_price (botun POLYMARKET'te dolan gercek fiyati)
  data_ask     = price_series'in botun placed_at aninda gosterdigi ask
                (orderbook_snapshots.best_ask + backtest_prices.price_history,
                ask_at_or_after(placed_at) mantigi)

Eger data_ask ~ real_entry ise fill modeli gercekci; degilse sim giris fiyati
canlidan uzak demektir. Rapor: ortalama mutlak sapma + korelasyon + ornekler.
Sadece BIRLESTIRMEK icin yazildi, bot koduna dokunmaz.
"""

import os
import sqlite3
import sys
from collections import defaultdict
from statistics import mean

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.backtest import ts, BOT_DB, OB_DB, BP_DB, CLOSE_WINDOW_SEC


def _ask_at(series, t, window_sec=CLOSE_WINDOW_SEC):
    """t'den itibaren window icindeki ilk ask (backtest.py ile ayni mantik)."""
    for s, a in series:
        if s < t:
            continue
        if s - t > window_sec:
            break
        return a
    return None


def main():
    # 1) gercek betler
    db = sqlite3.connect(BOT_DB, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    rows = list(
        db.execute(
            "SELECT market_id, entry_price, placed_at, bet_type, city, strike_temp, status, pnl "
            "FROM bets WHERE entry_price IS NOT NULL AND placed_at IS NOT NULL "
            "AND entry_price > 0"
        )
    )
    db.close()

    # 2) fiyat serisi (orderbook + backtest_prices)
    series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    ob = sqlite3.connect(OB_DB, timeout=30)
    ob.execute("PRAGMA busy_timeout=30000")
    for mid, ask, st in ob.execute(
        "SELECT market_id, best_ask, snapshot_time FROM orderbook_snapshots WHERE best_ask IS NOT NULL"
    ):
        t = ts(st)
        if t is None:
            continue
        try:
            a = float(ask)
            if 0 < a <= 1:
                series[str(mid)].append((t, a))
        except (TypeError, ValueError):
            pass
    ob.close()
    if os.path.exists(BP_DB):
        bp = sqlite3.connect(BP_DB, timeout=30)
        for mid, t, p in bp.execute("SELECT market_id, ts, price FROM price_history WHERE price > 0 AND price <= 1"):
            series[str(mid)].append((float(t), float(p)))
        bp.close()
    for k in series:
        series[k].sort(key=lambda x: x[0])

    # 3) karsilastir
    pairs = []
    n_missing = 0
    for mid, real_entry, placed, bet_type, city, strike, status, pnl in rows:
        t = ts(placed)
        if t is None:
            continue
        seri = series.get(str(mid))
        if not seri:
            n_missing += 1
            continue
        data_ask = _ask_at(seri, t)
        if data_ask is None:
            n_missing += 1
            continue
        pairs.append((mid, real_entry, data_ask, t, bet_type, city, strike, status, pnl))

    if not pairs:
        print("Eslesen bet yok (fiyat verisi hicbir marketi kapsamiyor).")
        return

    n = len(pairs)
    diffs = [abs(r - a) for _, r, a, *_ in pairs]
    rel = [abs(r - a) / r for _, r, a, *_ in pairs if r > 0]
    # korelasyon (Pearson)
    mx = mean(r for _, r, _, *_ in pairs)
    my = mean(a for _, _, a, *_ in pairs)
    cov = mean((r - mx) * (a - my) for _, r, a, *_ in pairs)
    sx = mean((r - mx) ** 2 for _, r, a, *_ in pairs) ** 0.5
    sy = mean((a - my) ** 2 for _, r, a, *_ in pairs) ** 0.5
    corr = cov / (sx * sy) if sx and sy else 0.0

    print("=== FILL-MODEL GERCEKLIK PROBU ===")
    print(f"karsilastirilan bet: {n}  (fiyat verisi yok: {n_missing})")
    print(f"ortalama |real - data_ask| = {mean(diffs):.4f}")
    print(f"ortalama bagil sapma         = {mean(rel) * 100:.1f}%")
    print(f"korelasyon (real vs data)    = {corr:.3f}")
    print()

    def _row(x):
        mid, r, a, _t, bt, city, strike, _st, _pnl = x
        return (
            f"  {mid[:20]:20} real={r:6.3f} data={a:6.3f} d={r - a:+7.3f} "
            f"{bt or '?':12} {str(city)[:14]:14} strike={strike}"
        )

    print("en kotu 10 (buyuk sapma):")
    for x in sorted(pairs, key=lambda x: -abs(x[1] - x[2]))[:10]:
        print(_row(x))
    print()
    print("en iyi 10 (kucuk sapma):")
    for x in sorted(pairs, key=lambda x: abs(x[1] - x[2]))[:10]:
        print(_row(x))


if __name__ == "__main__":
    main()
