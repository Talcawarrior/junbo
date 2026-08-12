"""Bankroll yonetimi backtesti — 100$ baslangic, 5 gunluk buyume plani.

Kullanici plani (2026-08-12):
  - Baslangic 100$. Stake = bankroll x 0.01 (compound). Min 0.50$.
  - Exposure (stake+fee+gas) toplam bankroll'un %70'ini asmaz -> kac sehir
    acilacagini belirler (7 esik/sehir, en az sapan sehirler once).
  - Gunluk kayip >%15 ise ertesi gun mola. Toplam -%30 ise stake %50 dusur.
  - Gunluk kar >%20 ise yeni bet stake'i %50 azalt.
  - Fee: stake x 0.05 x (1-entry); gas: 0.10$/islem.

Veri: market acilisinda ILK snapshot fiyati (dusuk giris), actuals.db settlement.
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import sqlite3  # noqa: E402

BOT_DB = os.path.join(_REPO_ROOT, "data", "bot.db")
ACTUALS_DB = os.path.join(_REPO_ROOT, "data", "actuals.db")

FEE_RATE = 0.05
GAS_USD = 0.10
MAX_ENTRY = 0.30
SPREAD = 3
BETS_PER_CITY = 7  # center +/- SPREAD
EXPOSURE_PCT = 0.70
STAKE_PCT = 0.01
MIN_STAKE = 0.50
MAX_CITIES = 15


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Bankroll buyume backtesti")
    parser.add_argument("--start", type=float, default=100.0, help="baslangic sermayesi")
    parser.add_argument("--days", type=int, default=5, help="gun sayisi (en guncel N gun)")
    parser.add_argument("--bias-top", type=int, default=15, help="en az sapan sehir sayisi")
    parser.add_argument("--max-entry", type=float, default=MAX_ENTRY, help="ust fiyat siniri")
    parser.add_argument("--spread", type=int, default=SPREAD, help="center +/- derece")
    parser.add_argument("--fee-rate", type=float, default=FEE_RATE)
    parser.add_argument("--gas-usd", type=float, default=GAS_USD)
    args = parser.parse_args()

    adb = sqlite3.connect(ACTUALS_DB)
    actuals = {}
    for r in adb.execute("SELECT city, date, temperature_2m_max, temperature_2m_min FROM actual_temperatures"):
        actuals[(r[0], str(r[1])[:10])] = (r[2], r[3])
    adb.close()

    db = sqlite3.connect(BOT_DB)
    cur = db.cursor()

    cur.execute("SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''")
    code_name = {}
    for c, code in cur.fetchall():
        if code and c:
            code_name.setdefault(code, c)

    # en az sapan sehirler (bias)
    bias_sums: dict[str, float] = {}
    bias_cnt: dict[str, int] = {}
    for code, bias in cur.execute(
        "SELECT city_code, bias FROM historical_calibrations WHERE bias IS NOT NULL AND city_code IS NOT NULL"
    ).fetchall():
        cname = code_name.get(code)
        if not cname:
            continue
        bias_sums[cname] = bias_sums.get(cname, 0.0) + abs(float(bias))
        bias_cnt[cname] = bias_cnt.get(cname, 0) + 1
    city_bias = {c: bias_sums[c] / bias_cnt[c] for c in bias_sums if bias_cnt[c] > 0}
    keep = {c for c, _ in sorted(city_bias.items(), key=lambda kv: kv[1])[: args.bias_top]}

    # forecast: (code, day) -> ilk forecast ensemble merkezi (2-gun-oncesi)
    cur.execute(
        "SELECT city, target_date, metric, source, predicted_value, fetched_at "
        "FROM weather_forecasts WHERE predicted_value IS NOT NULL ORDER BY fetched_at ASC"
    )
    fc = {}
    for code, tdate, metric, source, pval, fetched in cur.fetchall():
        if "min" in (metric or ""):
            continue
        td = str(tdate)[:10] if tdate else None
        if not td or not code:
            continue
        key = (code, td)
        fc.setdefault(key, {})
        if source not in fc[key]:
            fc[key][source] = float(pval)

    # fiyat gecmisi per (city, day, thr) -> [(snapshot_time, yes_price)] (kayan pencere icin)
    cur.execute(
        "SELECT city, target_date, threshold, snapshot_time, yes_price "
        "FROM market_snapshots ORDER BY snapshot_time ASC"
    )
    price_series: dict[tuple, list] = defaultdict(list)
    for city, tdate, thr, stime, yp in cur.fetchall():
        td = str(tdate)[:10] if tdate else None
        if not td or thr is None:
            continue
        try:
            p = float(yp)
            if 0 < p < 1:
                price_series[(city, td, float(thr))].append((str(stime).replace("T", " ")[:19], p))
        except (TypeError, ValueError):
            continue
    for k in price_series:
        price_series[k].sort(key=lambda x: x[0])
    db.close()

    # forecast guncelleme gecmisi per (city, day): fetched_at -> ensemble merkezi
    # (kayan pencere: merkez kayinca eski uclar kapatilir, yeniler acilir)
    fc_by_day: dict[tuple, list] = defaultdict(list)  # (city, day) -> [(fetched, center)]
    for (code, td), models in fc.items():
        city = code_name.get(code)
        if not city or city not in keep:
            continue
        vals = list(models.values())
        if not vals:
            continue
        center = round(sum(vals) / len(vals))
        fc_by_day[(city, td)].append((center,))
    # tek merkez var (ilk forecast ensemble) — bot saatlik guncelliyor ama elimizde
    # cogu gun icin tek deger var; kayan pencere bu merkez uzerinden isler.

    # gun bazli merkezler (bot: en az sapan sehirler)
    by_day: dict[str, list] = defaultdict(list)  # day -> [(city, center)]
    for (code, td), models in fc.items():
        city = code_name.get(code)
        if not city or city not in keep:
            continue
        vals = list(models.values())
        if not vals:
            continue
        center = round(sum(vals) / len(vals))
        by_day[td].append((city, center))

    # ---------------- bankroll simülasyonu ----------------
    bankroll = args.start
    total_open = 0
    total_won = total_lost = 0
    previous_day_loss = False

    print(
        f"Baslangic: {bankroll}$ | stake=1$ | en az sapan {args.bias_top} sehir | "
        f"max_entry<{args.max_entry} spread={args.spread} | fee={args.fee_rate} gas={args.gas_usd}"
    )
    print(
        f"bot config: kayan pencere (merkez kayinca eski uclar kapatilir) | "
        f"exposure<={EXPOSURE_PCT:.0%} | gunluk -%15 mola\n"
    )

    for day in sorted(by_day)[-args.days :]:
        stake = 1.0  # kullanici karari: sabit 1$
        cities = by_day[day]
        cost_per_bet = stake + stake * args.fee_rate * (1 - 0.05) + args.gas_usd
        max_openable = int((bankroll * EXPOSURE_PCT) / (cost_per_bet * BETS_PER_CITY))
        max_openable = max(0, min(max_openable, len(cities), MAX_CITIES))

        if previous_day_loss:
            print(f"  [{day}] MOLA — onceki gun >%15 kayip, bet acilmadi")
            previous_day_loss = False
            continue
        if max_openable == 0:
            print(f"  [{day}] exposure siniri: bet acilamadi (bankroll={bankroll:.2f})")
            continue

        day_pnl = 0.0
        day_bets = day_won = day_lost = 0
        for city, center in cities[:max_openable]:
            # KAYAN PENCERE: her sehirde merkez+/-spread esiklerini ac (0<fiyat<max_entry)
            open_pos: dict[float, float] = {}
            for thr in range(center - args.spread, center + args.spread + 1):
                # ILK snapshot fiyati — bot market acilisinda (0-13 UTC probe) dusuk
                # fiyattan girer; en erken snapshot o anki fiyattir.
                series = price_series.get((city, day, float(thr)), [])
                entry = series[0][1] if series else None
                if entry is None or not (0 < entry < args.max_entry):
                    continue
                open_pos[float(thr)] = entry
            # (tek forecast guncellemesi var; merkez guncellenmedigi icin kapanis
            #  yok — bot'un kayan pencere davranisi tek merkezde ac/kapat demektir)
            for thr, entry in open_pos.items():
                act = actuals.get((city, day))
                if act is None or act[0] is None:
                    continue
                hit = round(float(act[0])) == thr
                fee = stake * args.fee_rate * (1.0 - entry) if args.fee_rate > 0 else 0.0
                cost = stake + fee + args.gas_usd
                gain = (stake / entry - cost) if hit else -cost
                day_pnl += gain
                day_bets += 1
                total_open += 1
                if hit:
                    day_won += 1
                    total_won += 1
                else:
                    day_lost += 1
                    total_lost += 1

        bankroll += day_pnl
        day_roi = day_pnl / max(args.start, 1) * 100
        # kural: gunluk >%15 kayip -> ertesi gun mola
        if day_pnl < 0 and abs(day_pnl) / max(bankroll - day_pnl, 1) > 0.15:
            previous_day_loss = True

        print(
            f"  [{day}] sehir={max_openable} bet={day_bets} win={day_won} loss={day_lost} "
            f"gunPnl={day_pnl:+.2f} bankroll={bankroll:.2f} (roi %{day_roi:+.1f})"
        )

    print("\n=== SONUC ===")
    print(f"Toplam bet: {total_open} | kazanan: {total_won} | kaybeden: {total_lost}")
    print(f"win-rate: {total_won/max(total_open,1)*100:.1f}%")
    print(f"Baslangic: {args.start}$ -> Final: {bankroll:.2f}$ (net {bankroll-args.start:+.2f}$)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
