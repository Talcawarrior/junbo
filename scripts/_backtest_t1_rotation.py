"""T-1 rotasyon backtesti (2026-08-21, kullanici istegi).

Kullanici: "iki gun once actik, sonra 1 gun sonra tahmine tekrar baktik ve
bet kaybedecek bu durumda satip 1 gun onceki tahmine gore alirsak kar
edermiyiz".

Strateji: 2-gun-oncesi acilan spread bet icin, settlement'tan 1 gun once
(T-1) en guncel ensemble tahminine tekrar bakilir.
  - Yeni merkez Y = round(fmean_T1) bet bucket'indan FARKLI ise:
      bet T-1 fiyatindan satilir (cash = shares * satis_fiyat),
      Y bucket marketine T-1 fiyatindan alinir (y_shares = cash / alis_fiyat),
      Y kazanirsa y_shares, kaybederse 0.
  - Y == bucket ise islem yok (tut).
  - T-1 forecast yok / Y marketi yok / satis veya alis fiyati 0.01 alti
    (likit degil) -> rotasyon YAPILAMAZ, hold kabul edilir.

Karsilastirma: her bette Hold (gercek sonuc) vs Rotasyon PNL'i.
Sadece won/lost (settle olmus) betler; stake yoksa $2 varsayimi.

Kullanim: python scripts/_backtest_t1_rotation.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_data_dir, get_db_path  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT_DB = get_db_path()
PRICE_DB = os.path.join(get_data_dir(), "backtest_prices.db")

STALE_STAKE = 2.0  # eski betlerde stake None ise


def _t1_price(pcur, market_id: str, t_day: datetime) -> float | None:
    """T-1 gunu (UTC) icin marketin son YES fiyati (price_history epoch)."""
    lo = datetime(t_day.year, t_day.month, t_day.day, 0, 0, 0)
    hi = datetime(t_day.year, t_day.month, t_day.day, 23, 59, 59, 999999)
    lo_ep = lo.replace(tzinfo=timezone.utc).timestamp()
    hi_ep = hi.replace(tzinfo=timezone.utc).timestamp()
    r = pcur.execute(
        "SELECT price FROM price_history WHERE market_id=? AND ts>=? AND ts<=? ORDER BY ts DESC LIMIT 1",
        (market_id, lo_ep, hi_ep),
    ).fetchone()
    return float(r[0]) if r else None


def _t1_forecast(bcur, city_code: str, target_day: datetime) -> float | None:
    """T-1 gunu sonuna kadarki en son ensemble cekiminin equal-weight ortalamasi."""
    day_key = target_day.strftime("%Y-%m-%d")
    t1_hi = (target_day - timedelta(days=1)).strftime("%Y-%m-%dT23:59:59")
    r = bcur.execute(
        "SELECT MAX(fetched_at) FROM weather_forecasts "
        "WHERE city=? AND metric='temperature_max' "
        "AND substr(target_date,1,10)=? AND fetched_at<=?",
        (city_code, day_key, t1_hi),
    ).fetchone()
    if not r or r[0] is None:
        return None
    latest = r[0]
    rows = bcur.execute(
        "SELECT predicted_value FROM weather_forecasts "
        "WHERE city=? AND metric='temperature_max' "
        "AND substr(target_date,1,10)=? AND fetched_at=? "
        "AND predicted_value IS NOT NULL",
        (city_code, day_key, latest),
    ).fetchall()
    vals = [x[0] for x in rows]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _y_market(bcur, city: str, target_day: datetime, y_thr: int) -> str | None:
    """(city, threshold=Y, hedef gun) RANGE market id'si."""
    day_key = target_day.strftime("%Y-%m-%d")
    r = bcur.execute(
        "SELECT id FROM weather_markets WHERE city=? AND metric='temperature_max' "
        "AND threshold=? AND market_type='RANGE' AND substr(target_date,1,10)=?",
        (city, float(y_thr), day_key),
    ).fetchone()
    return r[0] if r else None


def main() -> int:
    bcur = sqlite3.connect(BOT_DB, timeout=10)
    pcur = sqlite3.connect(PRICE_DB, timeout=10)
    bcur.execute("PRAGMA busy_timeout=8000")

    # 1) 2-gun-oncesi acilan, settle olmus RANGE spread betler
    bets = bcur.execute(
        """
        SELECT b.id, b.market_id, w.city, b.city_code, w.threshold, b.status,
               ROUND(b.entry_price,4) AS entry, b.stake, w.target_date
        FROM bets b JOIN weather_markets w ON w.id=b.market_id
        WHERE w.market_type='RANGE' AND w.metric='temperature_max'
          AND julianday(w.target_date)-julianday(b.placed_at)>=1.5
          AND b.status IN ('won','lost')
        ORDER BY b.id
        """
    ).fetchall()

    # 2) Gercek kazanan bucket haritasi: (city, day) -> threshold
    win_rows = bcur.execute(
        """
        SELECT b.city, substr(w.target_date,1,10), w.threshold
        FROM bets b JOIN weather_markets w ON w.id=b.market_id
        WHERE b.status='won' AND w.market_type='RANGE' AND w.metric='temperature_max'
        GROUP BY b.city, substr(w.target_date,1,10), w.threshold
        """
    ).fetchall()
    winner: dict[tuple[str, str], float] = {}
    for city, day, thr in win_rows:
        winner[(city, day)] = thr

    rows = []
    for bid, mid, city, city_code, bucket, status, entry, stake, tdate in bets:
        if entry is None or entry <= 0.001:
            continue
        stake = float(stake) if stake else STALE_STAKE
        entry = float(entry)
        bucket = float(bucket)
        t_day = datetime.fromisoformat(str(tdate)[:10])
        t1_day = t_day - timedelta(days=1)
        day_key = t_day.strftime("%Y-%m-%d")
        w = winner.get((city, day_key))

        # Hold (A): gercek sonuc
        hold_win = status == "won"
        hold_pnl = stake * (1.0 / entry - 1.0) if hold_win else -stake

        # T-1 tahmini + rotasyon karari
        fmean = _t1_forecast(bcur, city_code, t_day)
        y_thr = int(fmean + 0.5) if fmean is not None else None
        rot_pnl = hold_pnl
        rot_ok = False
        satis = alis = None
        if y_thr is not None and y_thr != int(bucket):
            satis = _t1_price(pcur, mid, t1_day)
            y_mid = _y_market(bcur, city, t_day, y_thr)
            alis = _t1_price(pcur, y_mid, t1_day) if y_mid else None
            if satis is not None and alis is not None and satis >= 0.01 and alis >= 0.01:
                shares = stake / entry
                cash = shares * satis
                y_shares = cash / alis
                y_win = w is not None and y_thr == int(w)
                rot_pnl = y_shares - stake if y_win else -stake
                rot_ok = True
        rows.append(
            {
                "id": bid,
                "city": city,
                "bucket": int(bucket),
                "entry": entry,
                "day": day_key,
                "fmean": fmean,
                "y": y_thr,
                "hold_pnl": round(hold_pnl, 2),
                "rot_pnl": round(rot_pnl, 2),
                "rot_ok": rot_ok,
                "satis": satis,
                "alis": alis,
                "winner": int(w) if w is not None else None,
            }
        )

    # 3) Rapor
    n = len(rows)
    if not n:
        print("bet yok")
        return 1
    hold_t = sum(r["hold_pnl"] for r in rows)
    rot_t = sum(r["rot_pnl"] for r in rows)
    rot_n = sum(1 for r in rows if r["rot_ok"])
    hold_w = sum(1 for r in rows if r["hold_pnl"] > 0)
    rot_w = sum(1 for r in rows if r["rot_pnl"] > 0)
    print(f"2-gun-oncesi spread bet: {n}")
    print(f"  Hold  : toplam ${hold_t:+.2f} | kazancli {hold_w}/{n} (%{hold_w / n * 100:.1f})")
    print(f"  Rotasyon: toplam ${rot_t:+.2f} | kazancli {rot_w}/{n} (%{rot_w / n * 100:.1f})")
    print(f"  Fark  : ${rot_t - hold_t:+.2f}")
    print(f"  Rotasyon UYGULANAN: {rot_n} bet (farkli merkez + likit T-1)")

    # Rotasyon uygulanan betlerde detay karsilastirma
    if rot_n:
        sub = [r for r in rows if r["rot_ok"]]
        sh = sum(r["hold_pnl"] for r in sub)
        sr = sum(r["rot_pnl"] for r in sub)
        sw = sum(1 for r in sub if r["hold_pnl"] > 0)
        srw = sum(1 for r in sub if r["rot_pnl"] > 0)
        print(f"\n--- Rotasyon uygulanan {rot_n} betin kiyaslamasi ---")
        print(f"  Hold     : ${sh:+.2f} | {sw}/{rot_n} kazanci")
        print(f"  Rotasyon : ${sr:+.2f} | {srw}/{rot_n} kazanci")
        print(f"  Fark     : ${sr - sh:+.2f}")
        # hangileri ters sonuc verdi
        ters = [r for r in sub if (r["hold_pnl"] > 0) != (r["rot_pnl"] > 0)]
        print(f"  Ters sonuc veren: {len(ters)} bet")
        for r in sorted(ters, key=lambda x: x["rot_pnl"] - x["hold_pnl"])[:10]:
            print(
                f"    {r['city']:<10} T={r['day']} bucket={r['bucket']} "
                f"fmean={r['fmean']:.1f} Y={r['y']} kazanan={r['winner']} "
                f"hold={r['hold_pnl']:+.1f} rot={r['rot_pnl']:+.1f} "
                f"sat={r['satis']:.3f} al={r['alis']:.3f}"
            )

    # Rotasyon yapilamayanlar (neden)
    yok = [r for r in rows if not r["rot_ok"] and r["y"] is not None and r["y"] != r["bucket"]]
    neden = Counter()
    for r in yok:
        if r["satis"] is None:
            neden["satis fiyati yok"] += 1
        elif r["alis"] is None:
            neden["Y marketi/fiyati yok"] += 1
        else:
            neden["likidite yok (<0.01)"] += 1
    yok_f = [r for r in rows if r["fmean"] is None]
    print(f"\nRotasyon sinyali VAR ama yapilamayan: {len(yok)} {dict(neden)}")
    print(f"T-1 forecast verisi olmayan: {len(yok_f)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
