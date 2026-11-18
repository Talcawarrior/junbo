"""R2 KURALININ PIYASA TESTI (2026-08-19).

R2 = sehrin klimatolojik peak saatinde (1 yillik mod, Open-Meteo arsivi)
o ana kadarki KUMULATIF max'in bucket'ina bet ac (istatistiksel isabet %87.3).

Burada: 1-19 Agu arasi, METAR arsivi (istasyon-dogru) + orderbook best_ask
fiyatlariyla R2'nin GERCEK piyasa karsiligi olculur: market bu istatistigi
fiyatliyor mu, yoksa peak saatinde hala ucuz mu?

Kullanim: python -m scripts.analyze_peak_hour_bet
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from database.db import get_session
from database.models import WeatherMarket
from scrapers.metar import city_utc_offset
import sys as _sys
from pathlib import Path as _Path
import os

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_data_dir, get_db_path  # noqa: E402


def _bucket(x: float) -> int:
    return int(x + 0.5) if x >= 0 else int(x - 0.5)


def main() -> int:
    # sehir mod saati: METAR arsivinden (istasyon-dogru) — OM grid'inin peak
    # saati 1-3 saat ERKEN (model gunduz dongusu), market istasyonla cozulur.
    code2name: dict[str, str] = {}
    name2off: dict[str, float] = {}
    mk: list[tuple[str, str, float, int]] = []  # (city, date, threshold, id)
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
                mk.append((str(r.city), str(r.target_date.date().isoformat()), float(r.threshold or 0), int(r.id)))

    mode_h: dict[str, int] = {}
    metar: dict[tuple[str, str], list[tuple[float, float]]] = {}
    conn = sqlite3.connect(get_db_path(), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    for code, day, ot, t in conn.execute("SELECT city_code, day, obs_time, temp_c FROM metar_observations"):
        name = code2name.get(str(code), str(code))
        try:
            ep = datetime.fromisoformat(str(ot)).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        metar.setdefault((name, str(day)), []).append((ep, float(t)))
    conn.close()
    _hrs_by_city: dict[str, list[int]] = {}
    for (city, day), rows in metar.items():
        if len(rows) < 10:
            continue
        off = name2off.get(city, 0.0)
        mx = max(t for _, t in rows)
        for e, t in rows:
            lt = datetime.fromtimestamp(e + off * 3600, tz=timezone.utc)
            if t == mx:
                _hrs_by_city.setdefault(city, []).append(lt.hour)
                break
    for city, hrs in _hrs_by_city.items():
        if hrs:
            mode_h[city] = max(set(hrs), key=hrs.count)

    # orderbook fiyatlari
    conn = sqlite3.connect(f"file:{os.path.join(get_data_dir(), 'orderbook.db')}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    prices: dict[int, list[tuple[float, float]]] = {}
    for m_id, t, ask in conn.execute(
        "SELECT market_id, snapshot_time, best_ask FROM orderbook_snapshots WHERE best_ask > 0"
    ):
        try:
            ts = float(datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp())
            prices.setdefault(int(m_id), []).append((ts, float(ask)))
        except (ValueError, TypeError):
            continue
    conn.close()
    for k in prices:
        prices[k].sort()

    # ---- sim ----
    stake = 3.0
    n = wins = 0
    pnl = 0.0
    pnl_wins = 0.0
    details = []
    for (city, day), rows in metar.items():
        mh = mode_h.get(city)
        if mh is None or day < "2026-08-05":
            continue
        rows.sort()
        off = name2off.get(city, 0.0)
        # mod saatin UTC karsiligi: yerel mh:00 = UTC (mh - off)
        t_cut = datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() + (mh - off) * 3600
        run = -999.0
        for e, t in rows:
            if e > t_cut + 30 * 60:
                break
            run = max(run, t)
        if run <= -999.0:
            continue
        daily_max = max(t for _, t in rows)
        b = _bucket(run)
        cand = [m for m in mk if m[0] == city and m[1] == day and _bucket(m[2]) == b]
        if not cand:
            continue
        # ilk orderbook fiyati >= t_cut (en fazla 45 dk sonra)
        p = None
        for ts, ask in prices.get(cand[0][3], []):
            if ts >= t_cut - 5 * 60:
                p = ask
                break
        if p is None:
            continue
        if not (0.05 <= p < 0.95):
            continue
        win = b == _bucket(daily_max)
        n += 1
        if win:
            wins += 1
            pnl += stake * (1.0 - p)
            pnl_wins += stake * (1.0 - p)
        else:
            pnl -= stake
        details.append((city, day, mh, p, b, _bucket(daily_max), win))

    print(f"R2 piyasa testi (5-19 Agu): {n} giris")
    print(f"isabet: {wins}/{n} = {wins / n * 100:.1f}% | PnL($3): ${pnl:+.2f}")
    if n:
        print(f"kazanan girislerin ort kazanci: ${pnl_wins / wins:.2f}" if wins else "")
    print()
    print("detay (en ucuz girisler):")
    for city, day, mh, p, b, fb, win in sorted(details, key=lambda r: r[3])[:20]:
        print(f"  {city:16s} {day} mh={mh:2d} fiyat={p:.3f} bet={b}C final={fb}C {'WIN' if win else 'LOSE'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
