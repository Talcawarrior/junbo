"""Rolling-window spread backtest — gercek bot davranisinin birebir simulasyonu.

Bot ne yapiyor (executor/spread_placer.py):
  1. Bir (city, day) grubu acildiginda en son meteo tahmininin merkezine +/- 3
     esik (7 ayak) CANLI fiyattan acilir.
  2. Meteo tahmini GUNCELLENDIKCE (run_fetch_weather saatlik) merkez kayabilir.
     Kayan pencere: yeni pencerenin DISINDA kalan esikler O ANKI fiyattan
     kapatilir (close_bet_for_rotation), yeni pencereye giren eksik esikler acilir.
  3. Settlement'ta kalan esikler gercek sonuca gore cozulur (actuals.db).

Bu script snapshot (30dk fiyat gecmisi) + forecast (fetched_at gecmisi) verisini
zaman ekseninde birlestirip ayni davranisi simule eder.

Kullanici 2026-08-11: "pencere kaydirma yapmiyor muyuz? merkez kayinca spreadin
uclarindaki betleri kapatip yeni centera gore bazilarini acmiyor muyuz?"
Eski backtest_early_spread.py bunu YAPMIYORDU (ilk tahmin + ilk fiyat + settlement).
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import sqlite3  # noqa: E402

STAKE = 2.0

BOT_DB = os.path.join(_REPO_ROOT, "data", "bot.db")
ACTUALS_DB = os.path.join(_REPO_ROOT, "data", "actuals.db")

# Guncel bot model agirliklari (config/settings.py ile senkron, 2026-08-11).
WEIGHTS = {
    "ecmwf_ifs025": 0.45,
    "ukmo_seamless": 0.10,
    "icon_global": 0.12,
    "gem_global": 0.10,
    "gfs_seamless": 0.10,
    "meteofrance_seamless": 0.05,
    "jma_seamless": 0.05,
    "cma_grapes_global": 0.03,
}


def _price_at(series: list[tuple[str, float]], ts: str) -> float | None:
    """series = [(snapshot_time, yes_price)] zaman sirali. ts'ye en yakin onceki
    fiyati dondur (o anki piyasa fiyati)."""
    best = None
    for st, p in series:
        if st <= ts:
            best = p
        else:
            break
    return best


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Rolling-window spread backtest")
    parser.add_argument("--spread", type=int, default=3)
    parser.add_argument("--max-entry", type=float, default=0.99)
    parser.add_argument("--min-bets", type=int, default=0)
    parser.add_argument(
        "--strict-7",
        action="store_true",
        help="tam-7 zorunlu: (city,day) acilisinda center+/-spread esiklerinin TAMAMI "
        "0<fiyat<max_entry olmali; degilse sehir elenir. Pencere kaydiginda da yeni "
        "pencere tam-7 degilse pozisyonlar kapanir, sehir atlanir (2026-08-11).",
    )
    parser.add_argument(
        "--weighted",
        action="store_true",
        help="model_weight agirlikli ensemble (bot ile ayni; varsayilan: esit agirlik)",
    )
    args = parser.parse_args()

    adb = sqlite3.connect(ACTUALS_DB)
    actuals = {}
    for r in adb.execute("SELECT city, date, temperature_2m_max, temperature_2m_min FROM actual_temperatures"):
        actuals[(r[0], str(r[1])[:10])] = (r[2], r[3])
    adb.close()

    db = sqlite3.connect(BOT_DB)
    cur = db.cursor()

    # city_code -> city adi
    cur.execute("SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''")
    code_name = {}
    for c, code in cur.fetchall():
        if code and c:
            code_name.setdefault(code, c)

    # forecast gecmisi: (code, day, metric) -> [(fetched_at, source, value)]
    cur.execute(
        "SELECT city, target_date, metric, source, predicted_value, fetched_at "
        "FROM weather_forecasts WHERE predicted_value IS NOT NULL ORDER BY fetched_at ASC"
    )
    fc_history: dict[tuple, list] = defaultdict(list)
    for code, tdate, metric, source, pval, fetched in cur.fetchall():
        td = str(tdate)[:10] if tdate else None
        if not td or not code or not metric or not source:
            continue
        fc_history[(code, td, metric)].append((str(fetched), source, float(pval)))

    # snapshot fiyat gecmisi: (city_name, day, metric, thr) -> [(snapshot_time, yes_price)]
    cur.execute(
        "SELECT city, metric, target_date, threshold, snapshot_time, yes_price "
        "FROM market_snapshots ORDER BY snapshot_time ASC"
    )
    price_series: dict[tuple, list] = defaultdict(list)
    for city, metric, tdate, thr, stime, yp in cur.fetchall():
        td = str(tdate)[:10] if tdate else None
        if not td or thr is None:
            continue
        try:
            p = float(yp)
            if 0 < p < 1:
                price_series[(city, td, metric, float(thr))].append((str(stime), p))
        except (TypeError, ValueError):
            continue
    # her seriyi zaman sirali oldugundan emin ol
    for k in price_series:
        price_series[k].sort(key=lambda x: x[0])

    db.close()

    # ---------------- simulasyon ----------------
    pnl_total = 0.0
    total = won = lost = closed_early = 0
    per_day = defaultdict(lambda: {"bets": 0, "win": 0, "loss": 0, "closed": 0, "pnl": 0.0})
    per_city = defaultdict(lambda: {"bets": 0, "win": 0, "loss": 0, "closed": 0, "pnl": 0.0})

    for (code, td, metric), entries in fc_history.items():
        city_name = code_name.get(code)
        if not city_name:
            continue
        # forecast guncellemelerini zaman sirasina diz
        by_time: dict[str, dict] = {}
        for fetched, source, pval in entries:
            by_time.setdefault(fetched, {})[source] = pval
        fc_times = sorted(by_time)

        act = actuals.get((city_name, td))
        if act is None:
            continue
        is_min = "min" in (metric or "")
        actual = act[1] if is_min else act[0]
        if actual is None:
            continue

        # acik pozisyonlar: {thr: entry_price}
        open_pos: dict[float, float] = {}

        for ft in fc_times:
            # bu zaman icin merkez
            models = by_time[ft]
            if args.weighted:
                # BOT ILE AYNI: model_weight agirlikli ortalama (settings model_weights)
                wsum = 0.0
                acc = 0.0
                for src, v in models.items():
                    w = float(WEIGHTS.get(src, 1.0))
                    acc += v * w
                    wsum += w
                fval = acc / wsum if wsum > 0 else None
            else:
                fval = sum(models.values()) / len(models) if models else None
            if fval is None:
                continue
            center = round(fval)
            targets = set(range(center - args.spread, center + args.spread + 1))

            # --- TAM-7 ZORUNLU: yeni pencere icindeki 7 esigin tamaminin fiyati
            #    0 < fiyat < max_entry olmali. Acilista saglanmazsa sehir hic
            #    acilmaz; pencere kaydiginda saglanmazsa acik pozisyonlar kapanir
            #    ve sehir atlanir (bot davranisi: spread_placer tam-7 kurali).
            if args.strict_7:
                ok7 = True
                for thr in sorted(targets):
                    p = _price_at(price_series.get((city_name, td, metric, thr), []), ft)
                    if p is None or not (0 < p < args.max_entry):
                        ok7 = False
                        break
                if not ok7:
                    # acik pozisyonlari o anki fiyattan kapat (rotasyon kapanisi)
                    for thr in list(open_pos.keys()):
                        p = _price_at(price_series.get((city_name, td, metric, thr), []), ft)
                        if p is None:
                            p = open_pos[thr]
                        entry = open_pos[thr]
                        shares = STAKE / entry
                        pnl = shares * p - STAKE
                        pnl_total += pnl
                        closed_early += 1
                        per_day[td]["closed"] += 1
                        per_day[td]["pnl"] += pnl
                        per_city[city_name]["closed"] += 1
                        per_city[city_name]["pnl"] += pnl
                        del open_pos[thr]
                    # bu (city,day) icin is bitir (tam-7 saglanmiyor)
                    open_pos.clear()
                    break

            # --- KAYAN PENCERE: yeni pencere disinda kalan esikleri kapat ---
            for thr in list(open_pos.keys()):
                if thr not in targets:
                    # o anki fiyattan kapat (snapshot <= ft)
                    p = _price_at(price_series.get((city_name, td, metric, thr), []), ft)
                    if p is None:
                        p = open_pos[thr]  # fiyat yoksa entry ile kapat (tarafsiz)
                    entry = open_pos[thr]
                    shares = STAKE / entry
                    proceeds = shares * p
                    pnl = proceeds - STAKE  # entry fee yok sayildi
                    pnl_total += pnl
                    closed_early += 1
                    per_day[td]["closed"] += 1
                    per_day[td]["pnl"] += pnl
                    per_city[city_name]["closed"] += 1
                    per_city[city_name]["pnl"] += pnl
                    del open_pos[thr]

            # --- yeni penceredeki eksik esikleri AÇ ---
            for thr in sorted(targets):
                if thr in open_pos:
                    continue
                p = _price_at(price_series.get((city_name, td, metric, thr), []), ft)
                if p is None or not (0 < p < args.max_entry):
                    continue
                open_pos[thr] = p
                total += 1
                per_day[td]["bets"] += 1
                per_city[city_name]["bets"] += 1

        # --- SETTLEMENT: kalan pozisyonlar ---
        for thr, entry in open_pos.items():
            hit = (actual >= thr) if not is_min else (actual <= thr)
            total += 1
            per_day[td]["bets"] += 1
            per_city[city_name]["bets"] += 1
            if hit:
                gain = (1.0 - entry) * STAKE / entry if entry > 0 else -STAKE
                won += 1
                per_day[td]["win"] += 1
                per_city[city_name]["win"] += 1
            else:
                gain = -STAKE
                lost += 1
                per_day[td]["loss"] += 1
                per_city[city_name]["loss"] += 1
            pnl_total += gain
            per_day[td]["pnl"] += gain
            per_city[city_name]["pnl"] += gain

    # ---------------- rapor ----------------
    wlabel = "WEIGHTED" if args.weighted else "EQUAL"
    label = f"rolling-window spread={args.spread} max_entry<{args.max_entry} {wlabel}"
    print(f"\n=== BACKTEST: {label} ===")
    print(f"bets={total} win={won} loss={lost} closed_early={closed_early} win-rate={won / max(total, 1) * 100:.1f}%")
    print(f"TOTAL PnL (${STAKE}/bet): ${pnl_total:.2f}  avg=${pnl_total / max(total, 1):.3f}/bet")

    print("\nper-day:")
    for d in sorted(per_day):
        pd = per_day[d]
        if pd["bets"] < args.min_bets:
            continue
        wr = pd["win"] / pd["bets"] * 100 if pd["bets"] else 0
        print(
            f"  {d}: bets={pd['bets']:>3} win={pd['win']:>3} loss={pd['loss']:>3} "
            f"closed={pd['closed']:>3} wr={wr:4.1f}% PnL=${pd['pnl']:9.2f}"
        )

    print("\nper-city (top 10 by PnL):")
    for c in sorted(per_city, key=lambda c: -per_city[c]["pnl"])[:10]:
        pc = per_city[c]
        wr = pc["win"] / pc["bets"] * 100 if pc["bets"] else 0
        print(f"  {c:<16} bets={pc['bets']:>3} wr={wr:4.1f}% closed={pc['closed']:>3} PnL=${pc['pnl']:8.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
