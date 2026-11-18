"""METAR-PEAK COMPOUND STAKE SIMULASYONU (2026-08-19).

Kullanici: "kazandikca bet artirdigimiz backtest nasildi" — eski bankroll
scripti 3-esik spread idi ve terkedildi; bu, BUGUNKU tek-esik METAR-peak
stratejisine compound stake kurali uygular.

Kurallar (eski bankroll plani 2026-08-12, bugunku stratejiye uyarlandi):
  - Baslangic 100$. Stake = bankroll x 0.01 (compound). Min 0.50$, max 3.00$ (stake tavan).
  - METAR 1-dusus kilidi ile peak tespit (mevcut kurallar: yerel 13+ kapisi).
  - Giris = kilit anindan sonraki ilk orderbook best_ask (look-ahead YOK).
  - Kazaninca stake buyur (bankroll x %1), kaybedince kuculur.
  - Gunluk -%15 -> ertesi gun mola; -%30 -> stake %50; +%20 -> stake %50 azalt.

Karsilastirma: sabit $3 stake (mevcut) vs compound $3 tavanli.

Kullanim: python -m scripts.analyze_metar_compound [--days N]
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

from database.db import get_session
from database.models import WeatherMarket
from scrapers.metar import city_utc_offset, detect_peak
import sys as _sys
from pathlib import Path as _Path
import os

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_data_dir, get_db_path  # noqa: E402


def _bucket(x: float) -> int:
    return int(x + 0.5) if x >= 0 else int(x - 0.5)


def main() -> int:
    import sys

    days_n = 19
    for a in sys.argv[1:]:
        if a.startswith("--days"):
            days_n = int(a.split("=", 1)[1])

    code2name: dict[str, str] = {}
    name2off: dict[str, float] = {}
    mk: dict[tuple[str, str, int], int] = {}  # (city, date, bucket) -> market db id
    with get_session() as session:
        for r in session.query(WeatherMarket).all():
            if r.target_date is None:
                continue
            if r.city_code and r.city:
                code2name[str(r.city_code)] = str(r.city)
            if r.city:
                name2off[str(r.city)] = city_utc_offset(
                    str(r.city_code or ""), str(r.target_date.date().isoformat()), r.longitude
                )
                mk[(str(r.city), str(r.target_date.date().isoformat()), _bucket(float(r.threshold or 0)))] = int(r.id)

    # METAR arsivi
    conn = sqlite3.connect(get_db_path(), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    metar: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for code, day, ot, t in conn.execute("SELECT city_code, day, obs_time, temp_c FROM metar_observations"):
        try:
            ep = datetime.fromisoformat(str(ot)).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        metar[(code2name.get(str(code), str(code)), str(day))].append((ep, float(t)))
    conn.close()

    # orderbook fiyatlari
    conn = sqlite3.connect(f"file:{os.path.join(get_data_dir(), 'orderbook.db')}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    prices: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for m_id, t, ask in conn.execute(
        "SELECT market_id, snapshot_time, best_ask FROM orderbook_snapshots WHERE best_ask > 0"
    ):
        try:
            ts = float(datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp())
            prices[int(m_id)].append((ts, float(ask)))
        except (ValueError, TypeError):
            continue
    conn.close()
    for k in prices:
        prices[k].sort()

    def first_price_after(m_id: int, t: float) -> float | None:
        for ts, p in prices.get(m_id, []):
            if ts >= t:
                return p
        return None

    # gunler
    days = sorted({d for (_, d) in metar})
    days = [d for d in days if d >= "2026-08-01"][-days_n:]

    def _run(stake_mode: str) -> tuple[float, int, int, float]:
        bankroll = 100.0
        n = wins = 0
        pnl = 0.0
        yesterday_loss15 = False
        cut_stake_half = False
        for day in days:
            if yesterday_loss15:
                yesterday_loss15 = False
                continue  # molali gun: bet yok
            day_pnl = 0.0
            for (city, d), rows in metar.items():
                if d != day:
                    continue
                if len(rows) < 10:
                    continue
                rows.sort()
                off = name2off.get(city, 0.0)
                peak, confirmed = detect_peak([(int(e), t) for e, t in rows], utc_offset_hours=off)
                if not confirmed or peak is None:
                    continue
                # kilit anindan sonraki ilk fiyat (orderbook)
                b = _bucket(peak)
                m_id = mk.get((city, day, b))
                if m_id is None:
                    continue
                # kilit zamani ~ peak obs + 10dk poll
                lock_t = None
                cmax = rows[0][1]
                for i in range(1, len(rows)):
                    ep, cur = rows[i]
                    if datetime.fromtimestamp(ep + off * 3600, tz=timezone.utc).hour < 13:
                        cmax = max(cmax, cur)
                        continue
                    if cur < cmax:
                        lock_t = ep + 10 * 60
                        break
                    cmax = max(cmax, cur)
                if lock_t is None:
                    continue
                p = first_price_after(m_id, lock_t)
                if p is None or not (0.05 <= p < 0.95):
                    continue
                # stake
                if stake_mode == "compound":
                    stake = min(3.0, max(0.5, bankroll * 0.01))
                    if cut_stake_half:
                        stake /= 2
                else:
                    stake = 3.0
                win = b == _bucket(max(t for _, t in rows))
                n += 1
                if win:
                    wins += 1
                    gain = stake * (1.0 - p)
                    bankroll += gain
                    day_pnl += gain
                    pnl += gain
                else:
                    bankroll -= stake
                    day_pnl -= stake
                    pnl -= stake
            # gun sonu risk kurallari
            if day_pnl <= -0.15 * bankroll:
                yesterday_loss15 = True
            cut_stake_half = bankroll <= 70.0
        return pnl, n, wins, bankroll

    pnl_c, n_c, w_c, br_c = _run("compound")
    pnl_f, n_f, w_f, br_f = _run("flat")

    print(f"METAR-peak compound sim ({days[0]}..{days[-1]}, {len(days)} gun):")
    print(f"{'mod':10s} {'bet':>4s} {'win%':>6s} {'PnL':>9s} {'final bankroll':>14s}")
    print(f"{'compound':10s} {n_c:4d} {w_c / n_c * 100 if n_c else 0:5.1f}% {pnl_c:8.2f} {br_c:12.2f}")
    print(f"{'flat $3':10s} {n_f:4d} {w_f / n_f * 100 if n_f else 0:5.1f}% {pnl_f:8.2f} {br_f:12.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
