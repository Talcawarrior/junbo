#!/usr/bin/env python3
"""TEK backtest komut dosyasi (2026-08-18 konsolidasyon).

Onceden scripts/ altinda 20+ ayri backtest_*.py vardi. Aktif kullanilan 5
backtest tek dosyada birlestirildi; tum eski varyantlar backtest_archive/
altina tasindi. Arsiv envanteri ve gerekcesi: `backtest_archive/README.md`.

Subkomutlar:
  gunluk               - gun gun gercekci backtest (botun SU ANKI modu)
  orderbook            - orderbook tabanli gercekci backtest (ham vs kalibre)
  metar_peak           - METAR-peak gercekci backtest (actual vs clairvoyant)
  metar_vs_settlement  - METAR bucket vs GERCEK Polymarket kapanisi dogrulama
  walk_forward         - walk-forward (look-ahead'siz) model dogrulama

Kullanim:
  python scripts/backtest.py gunluk --days 2026-08-16,2026-08-17 [--detail]
  python scripts/backtest.py orderbook [--spread 3] [--bias-top 15] [--fill first_ask]
  python scripts/backtest.py metar_peak [--hours-before 6] [--stake 3.0]
  python scripts/backtest.py metar_vs_settlement [--min-day 2026-08-13] [--max-day 2026-08-17]
  python scripts/backtest.py walk_forward
"""

from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from utils.market_outcome import parse_resolved_outcome  # noqa: E402
from utils.probability import normal_cdf, estimate_probability_empirical  # noqa: E402
from scrapers.metar import city_utc_offset  # noqa: E402  (M3: gercek saat dilimi)

BOT_DB = os.path.join(_REPO_ROOT, "data", "bot.db")
OB_DB = os.path.join(_REPO_ROOT, "data", "orderbook.db")
BP_DB = os.path.join(_REPO_ROOT, "data", "backtest_prices.db")

# Tum gercekci backtestlerde ortak maliyet modeli (ayni degerler).
FEE_RATE = 0.05
GAS = 0.10
MAX_ENTRY = 0.95


def ts(s) -> float | None:
    """DB zaman damgasini epoch'a cevir (basarisizsa None)."""
    s = str(s).replace("T", " ").replace("+00:00", "").strip()
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


# =====================================================================
# 1) GUNLUK — botun su anki modu (spread radius=0 + METAR-peak) gun gun
# =====================================================================
# Kaynak: scripts/backtest_gunluk.py (merge edildi, tasindi -> backtest_archive/)

SPREAD_STAKE = 2.0
PEAK_STAKE = 3.0
PEAK_MIN_ENTRY = 0.05  # 2026-08-18 E config (kullanici karari, backtest +$820.96)
BIAS_TOP = 15  # spread_max_cities (spread_placer.py)
# METAR-PEAK sehir filtresi 2026-08-18'de KALDIRILDI (kullanici: "bias a gerek
# yok, nasil olsa peak tespit edilmis oluyor") -> TUM sehirler.
MIN_HOURS_BEFORE_CLOSE = 0.0  # 2026-08-18 E config: kapanisa kadar bet acilir
CLOSE_WINDOW_SEC = 6 * 3600  # peak zamanina en yakin ask icin arama penceresi


def ask_at_or_after(series, t, window_sec=CLOSE_WINDOW_SEC) -> float | None:
    """t zamanindan itibaren, window icindeki ilk ask (bot anlik girer)."""
    for s, a in series:
        if s < t:
            continue
        if s - t > window_sec:
            break
        return a
    return None


def first_ask_below(series, max_entry) -> float | None:
    """Botun ilk giris ani: fiyat < max_entry oldugu ilk snapshot ask'i."""
    for _t, a in series:
        if 0 < a < max_entry:
            return a
    return None


def peak_lock(rows: list[tuple[float, float]], utc_off: float, min_hour: int = 13) -> tuple[float | None, float | None]:
    """KILITLI METAR peak + kilitlenme epoch'u — scrapers/metar.py detect_peak
    ile BIREBIRE ayni kural. 2026-08-18 kullanici: 1 dusus YETERLI (20 21 22
    22 21 -> 22 kilitlenir, ikinci dusus beklenmez); zirve asilirsa kapat +
    yeni zirveye ac (aktar). (peak_temp, lock_epoch) ya da (None, None).
    """
    if len(rows) < 3:
        return (None, None)
    cummax = rows[0][1]
    for epoch, cur in rows[1:]:
        local_dt = datetime.fromtimestamp(epoch + utc_off * 3600, tz=timezone.utc)
        if local_dt.hour < min_hour:
            cummax = max(cummax, cur)
            continue
        if cur > cummax:
            cummax = cur
        elif cur < cummax:
            return (cummax, epoch)
    return (None, None)


def peak_break(rows: list[tuple[float, float]], locked_peak: float, after_epoch: float) -> tuple[float, float] | None:
    """Kilitli peak'i ASAN ilk gozlem (epoch, temp) — kilit bozulma ani.

    2026-08-18 canli kural (jobs/metar_peak.py): kilitli zirve asilirsa 2 dusus
    beklenmeden yanlis bucket betleri derhal kapatilir (Milan: kilit 31, sonra
    32 geldi). after_epoch sonrasi ilk aşan gozlem doner; yoksa None.
    """
    for epoch, temp in rows:
        if epoch <= after_epoch:
            continue
        if temp > locked_peak:
            return (float(epoch), float(temp))
    return None


def trough_lock(
    rows: list[tuple[float, float]], utc_off: float, min_hour: int = 6
) -> tuple[float | None, float | None]:  # noqa: E501
    """KILITLI METAR dip (gunun EN DUSUK sicakligi) — max kuralinin simetrigi:
    yerel saat >= min_hour (gundogumu sonrasi) 2 ardisik YUKSELIS -> dip teyit.
    (2026-08-18 kullanici: "low temperature da acmiyoruz, ona bakalim".)
    """
    if len(rows) < 3:
        return (None, None)
    cummin = rows[0][1]
    rise_count = 0
    for epoch, cur in rows[1:]:
        local_dt = datetime.fromtimestamp(epoch + utc_off * 3600, tz=timezone.utc)
        if local_dt.hour < min_hour:
            cummin = min(cummin, cur)
            rise_count = 0
            continue
        if cur < cummin:
            cummin = cur
            rise_count = 0
        elif cur > cummin:
            rise_count += 1
            if rise_count >= 2:
                return (cummin, epoch)
        else:
            rise_count = 0
    return (None, None)


def trough_break(rows: list[tuple[float, float]], locked_min: float, after_epoch: float) -> tuple[float, float] | None:
    """Kilitli dip'in ALTINA inen ilk gozlem (kilit bozulma ani, min icin)."""
    for epoch, temp in rows:
        if epoch <= after_epoch:
            continue
        if temp < locked_min:
            return (float(epoch), float(temp))
    return None


def cost_of(stake: float, entry: float) -> float:
    fee = stake * FEE_RATE * (1.0 - entry)
    return stake + fee + GAS


def cmd_gunluk(args) -> int:
    days = [d.strip() for d in args.days.split(",") if d.strip()]

    db = sqlite3.connect(BOT_DB, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")

    # city_code -> city
    code_name: dict[str, str] = {}
    for c, code in db.execute(
        "SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''"
    ):
        if code and c:
            code_name.setdefault(code, c)

    # bias-top sehirler (en az sapan |bias|, METAR-aligned). SPREAD bias-top
    # 15 (spread_max_cities), METAR-PEAK bias-top 40 (BIAS_TOP_CITIES).
    bs, bc = {}, {}
    for code, bias in db.execute("SELECT city_code, bias FROM historical_calibrations WHERE bias IS NOT NULL"):
        cn = code_name.get(code)
        if not cn:
            continue
        bs[cn] = bs.get(cn, 0) + abs(float(bias))
        bc[cn] = bc.get(cn, 0) + 1
    cb = {c: bs[c] / bc[c] for c in bs if bc[c] > 0}
    ordered = [c for c, _ in sorted(cb.items(), key=lambda kv: kv[1])]
    keep = {c for c in ordered[:BIAS_TOP]}

    # market: (code, day, thr) -> (mid, target_ts, outcome, market_type)
    # SADECE temperature_max (spread + metar-peak max bucket'ina bet acar);
    # min marketleri ayni threshold'ta eslesip yanlis bet uretmesin diye filtrelenir.
    market: dict[tuple[str, str, float], tuple[str, float | None, bool | None, str]] = {}
    for r in db.execute(
        "SELECT id, city_code, threshold, target_date, raw_data, market_type FROM weather_markets "
        "WHERE threshold IS NOT NULL AND target_date IS NOT NULL AND raw_data IS NOT NULL "
        "AND metric = 'temperature_max'"
    ):
        code, thr, day = r[1], r[2], str(r[3])[:10]
        if thr is None or not code or day not in days:
            continue
        o = parse_resolved_outcome(r[4])
        t = ts(r[3])
        # half-up: US sehirlerinde esikler float C'dir (F'den donusturulur),
        # int() truncate yanlis bucket uretir (Austin 35.9C -> bucket 36).
        market[(code, day, int(float(thr) + 0.5))] = (str(r[0]), t, o, r[5])

    # METAR KILITLI peak (look-ahead YOK): bot final max'i bilmez, `detect_peak`
    # (yerel saat >= 13 + 2 ardısık dusus) kilitlediginde girer. Final max ile
    # giris yapmak gecen gunun sonucunu "bilirdi" — yanlis yuksek winrate uretir.
    lon: dict[str, float] = {}
    for code, lg in db.execute(
        "SELECT DISTINCT city_code, longitude FROM weather_markets "
        "WHERE city_code IS NOT NULL AND longitude IS NOT NULL"
    ):
        try:
            lon.setdefault(code, float(lg))
        except (TypeError, ValueError):
            pass
    day_rows: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for code, tmax, obs in db.execute(
        "SELECT city_code, temp_c, obs_time FROM metar_observations WHERE temp_c IS NOT NULL AND obs_time IS NOT NULL"
    ):
        day = str(obs)[:10]
        t = ts(obs)
        if code and day in days and t is not None:
            day_rows[(code, day)].append((t, float(tmax)))
    metar_peak_: dict[tuple[str, str], tuple[float, float]] = {}
    for (code, day), rows in day_rows.items():
        rows.sort(key=lambda x: x[0])
        # 2026-08-18 audit fix (M3): gercek saat dilimi (zoneinfo + DST) —
        # round(lon/15) China/Seoul/London icin 1 saat yanlis veriyordu.
        utc_off = city_utc_offset(code, day, lon.get(code))
        pk_, lock_epoch = peak_lock(rows, utc_off)
        if pk_ is not None and lock_epoch is not None:
            metar_peak_[(code, day)] = (float(pk_), lock_epoch)

    # tahminler: (code, day) -> model -> predicted_value (max)
    # GERCEKCI KAYNAK (2026-08-18 duzeltme): bot.db weather_forecasts yalnizca
    # son ~5 gunun fetch'lerini tutar (retention); 05-13 Aug hedefli satirlar
    # "14-Aug'da backfill edilmis" damgasiyla durur. O veriyi gunluk backtest'te
    # kullanmak LOOK-AHEAD'di — bot o batch'i o gun goremezdi. Gercek gunluk
    # fetch arsivi backtest.db'de (02-18 Aug, her gun ayri batch). Bot
    # (spread_placer.py) karar aninda func.max(fetched_at) batch'ini kullanir;
    # backtest de aynisini yapar AMA yalnizca kapanis ONCESI fetch edilmis
    # batch'lerden (kapanis = target_date + 12h, look-ahead yok).
    fc: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    if os.path.exists(WF_BACKTEST_DB):
        fc_db = sqlite3.connect(WF_BACKTEST_DB, timeout=30)

        def _settle_ts(d: str) -> float:
            # market kapanisi ~ target_date(23:59:59) + 12h (PEAK_CLOSE_HOURS=12)
            t = ts(f"{d} 23:59:59")
            return (t + 12 * 3600) if t is not None else 0.0

        usable: list[tuple[str, str, str, str, float]] = []
        for code, tdate, src, f_at, pv in fc_db.execute(
            "SELECT city, target_date, source, fetched_at, predicted_value FROM weather_forecasts "
            "WHERE predicted_value IS NOT NULL AND metric LIKE '%max%'"
        ):
            day = str(tdate)[:10]
            if day not in days or not code or f_at is None:
                continue
            t = ts(f_at)
            if t is None or t > _settle_ts(day):
                continue  # kapanis sonrasi fetch = look-ahead, kullanilamaz
            usable.append((code, day, src, str(f_at), float(pv)))
        latest_batch: dict[tuple[str, str], str] = {}
        for code, day, src, f_at_s, pv in usable:
            key = (code, day)
            if key not in latest_batch or f_at_s > latest_batch[key]:
                latest_batch[key] = f_at_s
        for code, day, src, f_at_s, pv in usable:
            if f_at_s == latest_batch.get((code, day)):
                fc[(code, day)].setdefault(src, pv)
        fc_db.close()

    # Botun marketi ilk ne zaman gorebilecegi (scan oncesi bet yok).
    first_seen: dict[str, float] = {}
    for mid, fs in db.execute("SELECT id, first_seen FROM weather_markets WHERE first_seen IS NOT NULL"):
        t = ts(fs)
        if t is not None:
            first_seen[str(mid)] = t

    # Gercek fiyat serisi: CLOB prices-history (Polymarket'in kendi islem/quote
    # gecmisi, en yetkili kaynak) + orderbook best_ask (botun 5dk okumalari).
    # market_snapshots.yes_price KULLANILMAZ: market fonde olmadan once botun
    # yes_price okumasi 0'a yakin artefakt uretiyor (Seoul 27C: snapshot 0.015,
    # gercek CLOB fiyati 0.19) — boyle bir fiyattan emir dolmaz, giris sahte
    # WIN/PnL uretir. Giris fiyati her zaman GERCEK piyasa fiyatidir.
    price_series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    ob = sqlite3.connect(OB_DB, timeout=30)
    ob.execute("PRAGMA busy_timeout=30000")
    try:
        ob_rows = list(
            ob.execute("SELECT market_id, best_ask, snapshot_time FROM orderbook_snapshots WHERE best_ask IS NOT NULL")
        )
    except sqlite3.OperationalError:
        import shutil
        import tempfile

        snap = os.path.join(tempfile.gettempdir(), "ob_gunluk.db")
        shutil.copy2(OB_DB, snap)
        ob.close()
        ob = sqlite3.connect(snap)
        ob_rows = list(
            ob.execute("SELECT market_id, best_ask, snapshot_time FROM orderbook_snapshots WHERE best_ask IS NOT NULL")
        )
    for mid, ask, st in ob_rows:
        t = ts(st)
        if t is None:
            continue
        try:
            a = float(ask)
            if 0 < a <= 1:
                price_series[str(mid)].append((t, a))
        except (TypeError, ValueError):
            pass
    ob.close()

    # CLOB prices-history (backfill_price_history.py): marketin TUM fiyat
    # gecmisi — ANA kaynak. orderbook onu tamamlar (CLOB bos ama bot okumus).
    if os.path.exists(BP_DB):
        bp = sqlite3.connect(BP_DB, timeout=30)
        try:
            bp_rows = list(bp.execute("SELECT market_id, ts, price FROM price_history WHERE price > 0 AND price <= 1"))
        except sqlite3.OperationalError:
            bp_rows = []
        bp.close()
        for mid, t, p in bp_rows:
            try:
                price_series[str(mid)].append((float(t), float(p)))
            except (TypeError, ValueError):
                pass
    for k in price_series:
        price_series[k].sort(key=lambda x: x[0])

    # GERCEK-FILL icin: botun markette GERCEKTEN doldurdugu entry'ler
    # (ideal ilk-ask yerine gercek fill ile kiyaslama icin).
    real_entries: dict[str, list[float]] = defaultdict(list)
    for mid, entry, status in db.execute("SELECT market_id, entry_price, status FROM bets WHERE entry_price > 0"):
        if status in ("won", "lost", "closed", "closed_early"):
            real_entries[str(mid)].append(float(entry))

    db.close()

    # ---- simülasyon ----
    spread: list[dict] = []
    peak: list[dict] = []

    for (code, day), models in fc.items():
        city = code_name.get(code)
        if not city or city not in keep or len(models) < 2:
            continue
        vals = list(models.values())
        # 2026-08-18 audit fix (C2): banker's round() yerine half-up (bot ile ayni)
        center = int(sum(vals) / len(vals) + 0.5)
        m = market.get((code, day, center))
        if m is None:
            continue
        mid, tgt, outcome, _ = m
        seri = price_series.get(mid)
        if not seri:
            continue
        # bot marketi first_seen'den once goremez -> o oncesi fiyat noktalari
        # giris icin kullanilamaz (market acilmis ama bot kesfetmemis).
        fs = first_seen.get(mid)
        if fs is not None:
            seri = [pt for pt in seri if pt[0] >= fs]
        if not seri:
            continue
        # veri penceresi: ilk snapshot settlement sonrasi ise canli bet penceresi
        # gozlemlenememis -> giris fiyati yaniltici (0.001 cozum-sonrasi artefakti)
        if tgt is None or seri[0][0] >= tgt:
            continue
        entry = first_ask_below(seri, MAX_ENTRY)
        if entry is None:
            continue
        stake = SPREAD_STAKE
        cost = cost_of(stake, entry)
        shares = stake / entry

        pk = metar_peak_.get((code, day))
        exit_tip = "hold_settlement"
        pnl = 0.0
        won = None
        if pk is not None:
            p_bucket, pk_t = pk
            P = int(p_bucket + 0.5)  # audit C2: half-up
            # 2026-08-18 canli kural: kilit bozulduysa (kilitli peak asildi)
            # kazanan asilma degerinin bucket'idir ve kapatma ASILMA aninda
            # yapilir; bozulmadiysa kilitli bucket + kilit ani (eski davranis).
            bk = peak_break(day_rows.get((code, day), []), p_bucket, pk_t)
            winner_bucket = int(bk[1] + 0.5) if bk is not None else P
            close_t = bk[0] if bk is not None else pk_t
            if winner_bucket != center:
                # yanlis bucket: canli fiyattan satilir (kilit ya da asilma aninda)
                pk_ask = ask_at_or_after(seri, close_t) if close_t else None
                if pk_ask is not None and 0 < pk_ask <= 1:
                    exit_tip = "sold_peak"
                    # kapanis maliyeti: satis tarafinda fee = stake*0.05*(1-ask) + gas
                    pnl = (pk_ask - entry) * shares - stake * FEE_RATE * (1.0 - pk_ask) - GAS
                    won = pk_ask > entry
                else:
                    exit_tip = "hold_settlement_nopk_ask"
        if exit_tip == "hold_settlement" or exit_tip == "hold_settlement_nopk_ask":
            if outcome is None:
                continue  # cozum yok -> bet sonuclanamaz
            exit_tip = "hold_settlement"
            won = outcome
            pnl = (stake / entry - cost) if outcome else -cost
        if won is None:
            continue
        spread.append(
            {
                "day": day,
                "city": city,
                "code": code,
                "bucket": center,
                "mid": mid,
                "entry": entry,
                "stake": stake,
                "pnl": pnl,
                "won": won,
                "exit": exit_tip,
                "exit_ask": pk_ask if exit_tip == "sold_peak" else None,
            }
        )

    # METAR-PEAK legi: TUM sehirler (bias filtresi 2026-08-18'de kaldirildi),
    # KILITLI peak bucket'i, SADECE RANGE. Kilit bozulursa (peak asilirsa)
    # canli yeni kural: bet acilir AMA asilma aninda canli fiyattan kapatilir.
    for (code, day), (peak_temp, lock_epoch) in metar_peak_.items():
        if day not in days:
            continue
        city = code_name.get(code)
        if not city:
            continue
        B = int(peak_temp + 0.5)  # audit C2: half-up
        m = market.get((code, day, B))
        # 2026-08-18 kullanici karari: peak bet'i SADECE RANGE marketlerine
        if m is None or m[3] != "RANGE":
            continue
        mid, tgt, outcome, _ = m
        # MIN_HOURS_BEFORE_CLOSE=2: kapanisa <2sa kala peak kilitlenirse bet yok
        if tgt is None or tgt - lock_epoch < MIN_HOURS_BEFORE_CLOSE * 3600:
            continue
        seri = price_series.get(mid)
        if not seri:
            continue
        fs = first_seen.get(mid)
        if fs is not None:
            seri = [pt for pt in seri if pt[0] >= fs]
        if not seri:
            continue
        entry = ask_at_or_after(seri, lock_epoch)
        if entry is None or not (PEAK_MIN_ENTRY <= entry < MAX_ENTRY):
            continue
        if outcome is None:
            continue
        stake = PEAK_STAKE
        cost = cost_of(stake, entry)
        shares = stake / entry
        # 2026-08-18 canli kural: kilitli peak asildiysa bet asilma aninda
        # kapatilir (Milan: kilit 31 -> 32 geldi, fiyat cokmeden sat).
        bk = peak_break(day_rows.get((code, day), []), peak_temp, lock_epoch)
        if bk is not None:
            bk_t = bk[0]
            bk_ask = ask_at_or_after(seri, bk_t)
            if bk_ask is not None and 0 < bk_ask <= 1:
                exit_tip = "sold_broken_lock"
                pnl = (bk_ask - entry) * shares - stake * FEE_RATE * (1.0 - bk_ask) - GAS
                won = bk_ask > entry
            else:
                exit_tip = "hold_settlement"
                won = outcome
                pnl = (stake / entry - cost) if outcome else -cost
        else:
            exit_tip = "hold_settlement"
            won = outcome
            pnl = (stake / entry - cost) if outcome else -cost
        peak.append(
            {
                "day": day,
                "city": code_name.get(code, code),
                "code": code,
                "bucket": B,
                "mid": mid,
                "entry": entry,
                "stake": stake,
                "pnl": pnl,
                "won": won,
                "exit": exit_tip,
                "exit_ask": bk_ask if exit_tip == "sold_broken_lock" else None,
            }
        )
        # 2026-08-18 kullanici aktar: kilit bozulduysa (bk) eski bet satildi;
        # yeni zirvenin bucket'ina DUSUS BEKLEMEDEN yeniden bet acilir
        # ("23 e ciktiginda 1 adet dusmesini beklemeyecek hemen acacak").
        if bk is not None and exit_tip == "sold_broken_lock":
            B2 = int(bk[1] + 0.5) if bk[1] >= 0 else int(bk[1] - 0.5)
            m2 = market.get((code, day, B2))
            if m2 is not None and m2[3] == "RANGE" and m2[1] is not None and m2[2] is not None:
                mid2, tgt2, out2, _ = m2
                if tgt2 is not None and tgt2 - bk[0] >= MIN_HOURS_BEFORE_CLOSE * 3600:
                    seri2 = price_series.get(mid2)
                    if seri2:
                        fs2 = first_seen.get(mid2)
                        if fs2 is not None:
                            seri2 = [pt for pt in seri2 if pt[0] >= fs2]
                    e2 = ask_at_or_after(seri2, bk[0]) if seri2 else None
                    if e2 is not None and PEAK_MIN_ENTRY <= e2 < MAX_ENTRY:
                        shares2 = stake / e2
                        cost2 = cost_of(stake, e2)
                        bk2 = peak_break(day_rows.get((code, day), []), bk[1], bk[0])
                        if bk2 is not None:
                            b2ask = ask_at_or_after(seri2, bk2[0])
                            if b2ask is not None and 0 < b2ask <= 1:
                                exit2 = "sold_broken_lock"
                                pnl2 = (b2ask - e2) * shares2 - stake * FEE_RATE * (1.0 - b2ask) - GAS
                                won2 = b2ask > e2
                            else:
                                exit2 = "hold_settlement"
                                won2 = out2
                                pnl2 = (stake / e2 - cost2) if out2 else -cost2
                        else:
                            exit2 = "hold_settlement"
                            won2 = out2
                            pnl2 = (stake / e2 - cost2) if out2 else -cost2
                        peak.append(
                            {
                                "day": day,
                                "city": code_name.get(code, code),
                                "code": code,
                                "bucket": B2,
                                "mid": mid2,
                                "entry": e2,
                                "stake": stake,
                                "pnl": pnl2,
                                "won": won2,
                                "exit": exit2,
                                "exit_ask": b2ask if exit2 == "sold_broken_lock" else None,
                            }
                        )

    # ---- GERCEK-FILL duzeltmesi (2026-08-18) ----
    # Sim ideal ilk-ask fiyatindan girer; botun ayni markette GERCEKTEN
    # doldurdugu entry'ler varsa onlarla degistirilir. Gercek entry >= MAX_ENTRY
    # ise o fiyattan bet acilamazdi -> bet DUSURULUR (ideal-ask artefakti).
    def _real_pnl(b: dict, entry: float) -> float:
        if b["exit"].startswith("hold_settlement"):
            return (b["stake"] / entry - cost_of(b["stake"], entry)) if b["won"] else -cost_of(b["stake"], entry)
        ea = b["exit_ask"]  # sold_peak
        return (ea - entry) * (b["stake"] / entry) - b["stake"] * FEE_RATE * (1.0 - ea) - GAS

    if args.real_entry:
        n_real_fill = 0
        n_dropped = 0
        for b in spread + peak:
            rs = real_entries.get(b["mid"])
            if not rs:
                continue
            real_e = sum(rs) / len(rs)
            # config sinirlari GERCEK fill'e de uygulanir: bugunku config
            # (spread 0<e<0.95, peak MIN_ENTRY<=e<0.95) disinda kalan bir fill
            # bugunku stratejiyle acilamazdi -> bet dusurulur. Aksi halde eski
            # config'in longshot'lari (0.01) kazanan markette sahte PnL uretir.
            if b in peak:
                ok = PEAK_MIN_ENTRY <= real_e < MAX_ENTRY
            else:
                ok = 0 < real_e < MAX_ENTRY
            if not ok:
                b["drop"] = True
                n_dropped += 1
                continue
            b["entry"] = real_e
            b["pnl"] = _real_pnl(b, real_e)
            b["fill"] = "real"
            n_real_fill += 1
        if args.detail or args.real_entry:
            print(f"  [GERCEK-FILL] eslesen bet={n_real_fill}  dusurulen(gercek entry>=max)={n_dropped}")

    # ---- rapor ----
    print("=== GUN GUN BACKTEST (botun 2026-08-18 su anki modu, gercek orderbook + gercek cozum) ===")
    print(
        f"  SPREAD: bias-top {BIAS_TOP}, radius=0 (tek esik), stake=${SPREAD_STAKE:.0f}, "
        f"max_entry={MAX_ENTRY}, fair-filter YOK; kapanis: METAR-peak yanlis bucket satisi + settlement"
    )
    print(
        f"  METAR-PEAK: TUM sehirler (bias yok), stake=${PEAK_STAKE:.0f}, MIN_ENTRY={PEAK_MIN_ENTRY}, "
        f"bucket=round(KILITLI METAR peak), kapanisa<{MIN_HOURS_BEFORE_CLOSE:.0f}sa YOK"
    )
    print(f"  fee=%{FEE_RATE * 100:.0f} gas=${GAS:.2f}  |  gunler: {', '.join(days)}")
    print()

    clean = {name: [b for b in bets if not b.get("drop")] for name, bets in (("SPREAD", spread), ("METAR-PEAK", peak))}
    for leg_name, bets in clean.items():
        for day in days:
            db_ = [b for b in bets if b["day"] == day]
            if not db_:
                print(f"  [{leg_name:<10}] {day}: bet yok")
                continue
            n = len(db_)
            won = sum(1 for b in db_ if b["won"])
            pnl = sum(b["pnl"] for b in db_)
            staked = sum(b["stake"] for b in db_)
            fees = sum(b["stake"] * FEE_RATE * (1.0 - b["entry"]) for b in db_) + n * GAS
            print(
                f"  [{leg_name:<10}] {day}: bet={n:>3} kazandi={won:>3} "
                f"winrate=%{won / n * 100:>5.1f}  yatirilan=${staked:>7.2f}+fees ${fees:>6.2f} "
                f"NET=${pnl:>+8.2f} ROI=%{pnl / staked * 100:>+7.1f}"
            )
        n_all = len(bets)
        if n_all:
            won = sum(1 for b in bets if b["won"])
            pnl = sum(b["pnl"] for b in bets)
            staked = sum(b["stake"] for b in bets)
            print(
                f"  [{leg_name:<10}] TOPLAM:  bet={n_all:>3} kazandi={won:>3} "
                f"winrate=%{won / n_all * 100:>5.1f}  yatirilan=${staked:>7.2f} "
                f"NET=${pnl:>+8.2f} ROI=%{pnl / staked * 100:>+7.1f}"
            )
        print()

    if args.detail:
        print("--- bet detay ---")
        for b in sorted(spread + peak, key=lambda x: (x["day"], x["city"])):
            print(
                f"  {b['day']} {b['city']:<14} {'S' if b in spread else 'P'} "
                f"bucket={b['bucket']:<3} entry={b['entry']:.3f} "
                f"{'WIN' if b['won'] else 'LOSS':<4} pnl=${b['pnl']:+.2f} exit={b['exit']}"
            )
        print()

    comb = [b for b in spread + peak if not b.get("drop")]
    pnl = sum(b["pnl"] for b in comb)
    staked = sum(b["stake"] for b in comb)
    won = sum(1 for b in comb if b["won"])
    print(
        f"  [BIRLESIK    ] bet={len(comb):>3} kazandi={won:>3} winrate=%{won / len(comb) * 100:>5.1f} "
        f"yatirilan=${staked:>7.2f} NET=${pnl:>+8.2f} ROI=%{pnl / staked * 100:>+7.1f}"
    )
    # 2026-08-18 kullanici: gunluk birlesik tablo — yatirilan/kazanilan/
    # winrate/ROI/fee+gas hepsi tek satirda (sabit rapor).
    print()
    print("  GUNLUK BIRLESIK (spread + peak):")
    print(
        f"  {'gun':10s} {'bet':>4s} {'kazan':>6s} {'win%':>6s} "
        f"{'stake$':>8s} {'fee+gas$':>9s} {'NET$':>9s} {'ROI%':>8s}"
    )
    for day in days:
        db_ = [b for b in comb if b["day"] == day]
        if not db_:
            continue
        n = len(db_)
        w = sum(1 for b in db_ if b["won"])
        stk = sum(b["stake"] for b in db_)
        fees = sum(b["stake"] * FEE_RATE * (1.0 - b["entry"]) + GAS for b in db_)
        pnl_day = sum(b["pnl"] for b in db_)
        roi = pnl_day / stk * 100 if stk else 0.0
        print(
            f"  {day:10s} {n:>4d} {w:>6d} %{w / n * 100:>5.1f} "
            f"${stk:>7.2f} ${fees:>8.2f} ${pnl_day:>+8.2f} %{roi:>+7.1f}"
        )
    if args.real_entry:
        n_ideal = sum(1 for b in comb if not b.get("fill") == "real")
        n_real = sum(1 for b in comb if b.get("fill") == "real")
        pnl_ideal = sum(b["pnl"] for b in comb if not b.get("fill") == "real")
        pnl_real = sum(b["pnl"] for b in comb if b.get("fill") == "real")
        print(
            f"  [GERCEK-FILL ] ideal-entry bet={n_ideal:>3} NET=${pnl_ideal:>+8.2f}"
            f"  | gercek-entry bet={n_real:>3} NET=${pnl_real:>+8.2f}"
        )

    if spread:
        ex = defaultdict(int)
        for b in spread:
            ex[b["exit"]] += 1
        print("  spread kapanis dagilimi:", dict(ex))
    return 0


# =====================================================================
# 2) ORDERBOOK — orderbook best_ask ile ham vs kalibreli fair-value
# =====================================================================
# Kaynak: scripts/backtest_orderbook.py (merge edildi, tasindi -> backtest_archive/)
# C1 bulgusu: 'ilk snapshot fiyati' gercek fill'den %103 sapiyor. Bu modul
# orderbook.db'deki GERCEK best_ask serisini kullanir: bet acilis fiyati =
# marketin en erken goruldugu andaki best_ask (veya median/vwap), settlement =
# GERCEK Polymarket outcome, maliyet = stake + fee + gas.

OB_STAKE = 2.0


def cmd_orderbook(args) -> int:
    db = sqlite3.connect(BOT_DB)
    cur = db.cursor()

    # city_code -> city
    code_name = {}
    for c, code in cur.execute(
        "SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''"
    ):
        if code and c:
            code_name.setdefault(code, c)

    # bias-top N (en az sapan)
    bs, bc = {}, {}
    for code, bias in cur.execute("SELECT city_code, bias FROM historical_calibrations WHERE bias IS NOT NULL"):
        cn = code_name.get(code)
        if not cn:
            continue
        bs[cn] = bs.get(cn, 0) + abs(float(bias))
        bc[cn] = bc.get(cn, 0) + 1
    cb = {c: bs[c] / bc[c] for c in bs if bc[c] > 0}
    keep = {c for c, _ in sorted(cb.items(), key=lambda kv: kv[1])[: args.bias_top]}

    # market -> (city, thr, target_date, metric)
    markets = {}
    for r in cur.execute(
        "SELECT id, city, city_code, threshold, target_date, metric, raw_data, status "
        "FROM weather_markets WHERE threshold IS NOT NULL AND target_date IS NOT NULL"
    ):
        mid, city, code, thr, tdate, metric, raw, status = r
        if "max" not in (metric or ""):
            continue
        markets[str(mid)] = {
            "city": city,
            "code": code,
            "thr": float(thr),
            "day": str(tdate)[:10],
            "metric": metric,
            "raw": raw,
            "status": status,
        }

    # GERCEK outcome: market -> YES kazandi mi
    outcome = {}
    for mid, m in markets.items():
        o = parse_resolved_outcome(m["raw"]) if m["raw"] else None
        if o is not None:
            outcome[mid] = o

    # orderbook ask serisi: market -> [(t, best_ask)]
    ob = sqlite3.connect(OB_DB)
    oc = ob.cursor()
    ask_series = defaultdict(list)
    for mid, ask, st in oc.execute(
        "SELECT market_id, best_ask, snapshot_time FROM orderbook_snapshots WHERE best_ask IS NOT NULL"
    ):
        t = ts(st)
        if t is None:
            continue
        try:
            a = float(ask)
            if 0 < a <= 1:
                ask_series[str(mid)].append((t, a))
        except (TypeError, ValueError):
            pass
    ob.close()
    for k in ask_series:
        ask_series[k].sort(key=lambda x: x[0])

    # tahminler: (code, day) -> {model: predicted_value} (son cekim)
    fc = {}
    for code, tdate, src, pv in cur.execute(
        "SELECT city, target_date, source, predicted_value FROM weather_forecasts "
        "WHERE predicted_value IS NOT NULL AND metric LIKE '%max%'"
    ):
        day = str(tdate)[:10]
        if day < args.min_date:
            continue
        fc.setdefault((code, day), {})
        fc[(code, day)].setdefault(src, float(pv))

    # C2/C3 seffaflik: eksik veri kontrolu (sessiz bos sonuc YERINE acik rapor)
    n_ob = len(ask_series)
    n_markets = len(markets)
    n_fc = len(fc)
    n_out = len(outcome)
    n_overlap = sum(1 for mid in markets if mid in ask_series and mid in outcome)
    print(
        f"[veri kontrol] orderbook_market={n_ob}, eslesen_market={n_markets}, "
        f"forecast_sehir_gun={n_fc}, cozumlenmis_market={n_out}, "
        f"orderbook+outcome ortusme={n_overlap}"
    )
    if n_overlap == 0:
        print("HATA: orderbook ile outcome arasinda eslesme YOK — backtest gecersiz.")
        return 1
    if n_overlap < 100:
        print(f"UYARI: cok az eslesme ({n_overlap}) — sonuc guvenilmez olabilir.")

    # kalibrasyon: (code, model) -> bias (tum gecmis, walk-forward yerine statik-onceki)
    cal = defaultdict(dict)
    for code, model, b in cur.execute(
        "SELECT city_code, model, AVG(bias) FROM historical_calibrations "
        "WHERE bias IS NOT NULL GROUP BY city_code, model"
    ):
        cal[code][model] = float(b)
    db.close()

    # ---------------- simulasyon ----------------
    def run(mode):
        pnl = 0.0
        total = won = 0
        peak = 0.0
        max_dd = 0.0
        for (code, day), models in fc.items():
            city = code_name.get(code)
            if not city or city not in keep or len(models) < 2:
                continue
            vals = list(models.values())
            mean = sum(vals) / len(vals)
            std = (max(vals) - min(vals)) / 2.0
            center = round(mean)
            for thr in range(center - args.spread, center + args.spread + 1):
                # bu esigin marketini bul (orderbook'ta gorulen)
                found = None
                for mid, m in markets.items():
                    if (
                        m["city"] == city
                        and m["thr"] == float(thr)
                        and m["day"] == day
                        and mid in ask_series
                        and mid in outcome
                    ):
                        found = mid
                        break
                if found is None:
                    continue
                o = outcome[found]
                # acilis fiyati: first_ask / median_ask / vwap (ilk 20 snapshot)
                seri = ask_series[found]
                if args.fill == "first_ask":
                    entry = seri[0][1]
                elif args.fill == "median_ask":
                    asks = [a for _, a in seri[:20]]
                    entry = sorted(asks)[len(asks) // 2]
                else:  # vwap: zaman agirlikli ortalama (ilk 20 snapshot)
                    pts = seri[:20]
                    wsum = 0.0
                    vsum = 0.0
                    for i, (t, a) in enumerate(pts):
                        w = 1.0 if i == 0 else (t - pts[i - 1][0])
                        vsum += a * w
                        wsum += w
                    entry = vsum / wsum if wsum > 0 else pts[0][1]
                if not (0 < entry < args.max_entry):
                    continue
                # fair value (kalibrasyonlu veya ham)
                if mode == "ham":
                    fmean = mean
                else:
                    kvals = [p - cal.get(code, {}).get(m, 0.0) for m, p in models.items()]
                    fmean = sum(kvals) / len(kvals)
                tsd = max(math.sqrt(std**2 + 1.0), 1.0)
                if args.fair == "empirical":
                    fair = estimate_probability_empirical(fmean, float(thr), "HIGH", "temperature_max", lag_hours=48)
                else:
                    fair = max(0.01, min(0.99, 1.0 - normal_cdf((thr - fmean) / tsd)))
                if entry >= fair - args.gap:
                    continue
                fee = OB_STAKE * FEE_RATE * (1.0 - entry)
                cost = OB_STAKE + fee + GAS
                gain = (OB_STAKE / entry) - cost if o else -cost
                pnl += gain
                total += 1
                if o:
                    won += 1
                if pnl > peak:
                    peak = pnl
                dd = peak - pnl
                if dd > max_dd:
                    max_dd = dd
        return pnl, total, won, max_dd

    # C2: seffaflik — veri donemi ve kapsam
    db2 = sqlite3.connect(OB_DB)
    ob_range = db2.execute("SELECT MIN(snapshot_time), MAX(snapshot_time) FROM orderbook_snapshots").fetchone()
    ob_markets = db2.execute("SELECT COUNT(DISTINCT market_id) FROM orderbook_snapshots").fetchone()[0]
    db2.close()
    resolved = sum(1 for v in outcome.values() if v is not None)
    unresolved = len(markets) - resolved
    print(
        f"=== ORDERBOOK BACKTEST (fill={args.fill}, spread={args.spread}, "
        f"max_entry={args.max_entry}, gap={args.gap}, bias-top={args.bias_top}) ==="
    )
    print(
        f"  veri donemi: {str(ob_range[0])[:16]} .. {str(ob_range[1])[:16]} | "
        f"orderbook market={ob_markets}, eslesen market={len(markets)}, "
        f"cozumlenmis={resolved}, cozumlenmemis={unresolved}"
    )
    print(f"  fill varsayimi: {args.fill} (first_ask=market ilk goruldugu an, median_ask=ilk 20 snapshot medyani)")
    print(f"  fee={FEE_RATE} gas=${GAS} stake=${OB_STAKE}")
    for mode in ["ham", "kal"]:
        pnl, n, w, mdd = run(mode)
        print(
            f"  {mode.upper():<6} bet={n:>4} won={w:>3} winrate={w / max(n, 1) * 100:>5.1f}% "
            f"PnL=${pnl:>9.2f} max_drawdown=${mdd:>8.2f}"
        )
    return 0


# =====================================================================
# 3) METAR-PEAK GERCEKCI — tahmin=round(actual) vs clairvoyant (ust sinir)
# =====================================================================
# Kaynak: scripts/backtest_metar_peak_realistic.py (merge edildi, tasindi -> backtest_archive/)
# Neden: eski backtest_metar_peak.py 'winrate %100 / ROI %286' veriyordu —
# kazanan bucket'i GERCEK cozumden aliyordu (clairvoyant). Bu modul
# tahmini CANLI METAR/actual'dan yapar; cozum Polymarket gercek cozumudur.

PEAK_CLOSE_HOURS = 12


def _load_orderbook(ob_path: str):
    """market_id -> [(t, ask)] - kilitliyse kopya al (bot calisirken guvenli)."""
    try:
        db = sqlite3.connect(ob_path, timeout=20)
        db.execute("PRAGMA busy_timeout=20000")
        series = defaultdict(list)
        for mid, ask, st in db.execute(
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
        db.close()
        for k in series:
            series[k].sort(key=lambda x: x[0])
        return series
    except sqlite3.OperationalError as exc:
        print(f"[ob] canli okunamadi ({exc}); kopya deneniyor...")
        import shutil
        import tempfile

        snap = os.path.join(tempfile.gettempdir(), "ob_snap.db")
        shutil.copy2(ob_path, snap)
        j = ob_path + "-journal"
        if os.path.exists(j):
            shutil.copy2(j, snap + "-journal")
        return _load_orderbook(snap)


def price_at(series, bet_ts, window_sec=6 * 3600):
    """bet_ts oncesi, window icindeki son ask. Yoksa None."""
    best = None
    for t, a in series:
        if t > bet_ts:
            break
        if bet_ts - t <= window_sec:
            best = a
    return best


def cmd_metar_peak(args) -> int:
    stake = args.stake

    ask_series = _load_orderbook(OB_DB)

    db = sqlite3.connect(BOT_DB, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")

    code_name = {}
    for c, code in db.execute(
        "SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''"
    ):
        if code and c:
            code_name.setdefault(code, c)

    # (code, day, thr) -> (mid, target_ts, outcome)
    market = {}
    for r in db.execute(
        "SELECT id, city_code, threshold, target_date, raw_data FROM weather_markets "
        "WHERE threshold IS NOT NULL AND target_date IS NOT NULL AND raw_data IS NOT NULL"
    ):
        code, thr, day = r[1], float(r[2]) if r[2] is not None else None, str(r[3])[:10]
        if thr is None or not code:
            continue
        if not (args.min_day <= day <= args.max_day):
            continue
        o = parse_resolved_outcome(r[4])
        t = ts(r[3])
        market[(code, day, int(thr + 0.5))] = (str(r[0]), t, o)

    # Gercek cozumden kazanan bucket: (code, day) -> thr (YES cozulen en yuksek esik)
    winner = {}
    for (code, day, thr), (_, _, o) in market.items():
        if o is True:
            cur = winner.get((code, day))
            if cur is None or thr > cur:
                winner[(code, day)] = thr

    # Gercek max sicaklik (Open-Meteo archive / historical_calibrations)
    actuals = {}
    for r in db.execute(
        "SELECT city_code, date, actual_value FROM historical_calibrations "
        "WHERE metric='temperature_max' AND actual_value IS NOT NULL"
    ):
        code, day = r[0], str(r[1])[:10]
        if code:
            actuals.setdefault((code, day), float(r[2]))
    db.close()

    # AYNI evrende iki mod:
    #   "actual" -> bucket = round(actual)   (METAR detektoru proxy'si, ~%30 dogru)
    #   "clair"  -> bucket = gercek cozum    (look-ahead UST SINIR, %100 dogru)
    actual_bet = []
    clair_bet = []
    for (code, day), pred in actuals.items():
        if winner.get((code, day)) is None:
            continue
        w = winner[(code, day)]

        for label, thr in (("actual", int(round(pred))), ("clair", w)):
            m = market.get((code, day, thr))
            if m is None:
                continue
            mid, tgt, o = m
            if tgt is None:
                continue
            bet_ts = tgt + (PEAK_CLOSE_HOURS - args.hours_before) * 3600
            series = ask_series.get(mid)
            entry = price_at(series, bet_ts) if series else None
            if entry is None or not (0.01 <= entry < MAX_ENTRY):
                continue
            fee = stake * FEE_RATE * (1.0 - entry)
            cost = stake + fee + GAS
            win = thr == w
            gain = (stake / entry - cost) if win else -cost
            bucket = (code, day)
            if label == "actual":
                actual_bet.append((bucket, entry, win, gain))
            else:
                clair_bet.append((bucket, entry, win, gain))

    def summarize(bets):
        n = len(bets)
        wins = sum(1 for b in bets if b[2])
        pnl = sum(b[3] for b in bets)
        return n, wins, pnl

    def print_mode(name, bets):
        n, wins, pnl = summarize(bets)
        stk = n * stake
        print(
            f"  [{name}] bet={n}, winrate=%{wins / max(n, 1) * 100:.1f}, "
            f"NET ${pnl:+.2f}, ROI %{pnl / max(stk, 1) * 100:.1f}, ort ${pnl / max(n, 1):+.2f}/bet"
        )
        return pnl

    print("=== METAR-PEAK GERCEKCI backtest (ayni evren, iki senaryo) ===")
    print(f"  kapanisa {args.hours_before} saat kala, stake=${stake:g}, kandidat (code,day)={len(actuals)}")
    pnl_actual = print_mode("tahmin=round(actual) (~%30 dogru, METAR proxy'si)", actual_bet)
    pnl_clair = print_mode("clairvoyant   (cozumden %100 dogru, UST SINIR)", clair_bet)

    # Gercek detektor ~%71: p*(clair) + (1-p)*(actual) yaklasimi
    p = 0.71
    blend = p * pnl_clair + (1 - p) * pnl_actual
    print(
        f"  GERCEKCI KARISIM (detektor %{p * 100:.0f} dogru): "
        f"0.71*clair + 0.29*actual = ${blend:+.2f} (ayni bet seti uzerinde)"
    )
    print(
        f"  bucket dogrulugu (round(actual) vs cozum): "
        f"%{sum(1 for b in actual_bet if b[2]) / max(len(actual_bet), 1) * 100:.0f}"
    )
    return 0


# =====================================================================
# 3.5) METAR-PEAK LIVE — sadece orderbook + METAR (forecast/bias YOK)
# =====================================================================
# Kullanici istegi 2026-08-18: "sadece order book ve metar ile backtest yap,
# hic 2 gun onceden bet acma, metar ile peak takibi yap ve tespit ettiginde
# 3 usd bet ac sehire ve bir adet." Botun canli METAR-peak akisinin birebir
# tekrari: detect_peak kilitlenince (yerel saat >= 13 + 2 ardisik dusus) o
# sehrin o gunu icin TEK YES bet ($3) kazanan bucket'a; giris = kilitlenme
# sonrasi ilk gercek ask + slippage. Sehir secimi YOK (bias-top uygulanmaz).


def cmd_metar_peak_live(args) -> int:
    ask_series = _load_orderbook(OB_DB)
    # CLOB prices-history de ekle (gunluk backtest ile ayni kaynak birligi;
    # orderbook'un dar kapsamini CLOB'un ~1700 market/gun gecmisi tamamlar).
    if os.path.exists(BP_DB):
        try:
            bp = sqlite3.connect(BP_DB, timeout=30)
            for mid, t, p in bp.execute(
                "SELECT market_id, ts, price FROM price_history WHERE price > 0 AND price <= 1"
            ):
                try:
                    ask_series[str(mid)].append((float(t), float(p)))
                except (TypeError, ValueError):
                    pass
            bp.close()
        except sqlite3.OperationalError:
            pass
    for k in ask_series:
        ask_series[k].sort(key=lambda x: x[0])

    db = sqlite3.connect(BOT_DB, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")

    code_name: dict[str, str] = {}
    for c, code in db.execute(
        "SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''"
    ):
        if code and c:
            code_name.setdefault(code, c)

    # --bias-top N: en az sapan N sehir (botun canli sehir secimi ile ayni
    # tablodan). NOT: skorlar TUM gecmisten hesaplanir (bot canlida da aynisini
    # yapar; look-ahead kullanici istegiyle bilincli olarak simule edilir).
    keep_bias: set[str] | None = None
    if args.bias_top and args.bias_top > 0:
        bs: dict[str, float] = {}
        bc: dict[str, int] = {}
        for code, bias in db.execute("SELECT city_code, bias FROM historical_calibrations WHERE bias IS NOT NULL"):
            cn = code_name.get(code)
            if not cn:
                continue
            bs[cn] = bs.get(cn, 0) + abs(float(bias))
            bc[cn] = bc.get(cn, 0) + 1
        cb = {c: bs[c] / bc[c] for c in bs if bc[c] > 0}
        ordered_bias = [c for c, _ in sorted(cb.items(), key=lambda kv: kv[1])]
        keep_bias = {c for c in ordered_bias[: args.bias_top]}

    # market: (code, day, thr) -> (mid, target_ts, outcome, market_type)
    # --metric max (varsayilan): temperature_max; min: temperature_min
    # (2026-08-18 kullanici: "low temperature da acmiyoruz, ona bakalim").
    is_min = getattr(args, "metric", "max") == "min"
    is_high = getattr(args, "market_type", "range") == "high"
    m_metric = "temperature_min" if is_min else "temperature_max"
    m_mtype = "HIGH" if is_high else "RANGE"
    market: dict[tuple[str, str, int], tuple[str, float | None, bool | None, str]] = {}
    for r in db.execute(
        "SELECT id, city_code, threshold, target_date, raw_data, market_type FROM weather_markets "
        "WHERE threshold IS NOT NULL AND target_date IS NOT NULL AND raw_data IS NOT NULL "
        "AND metric = ? AND market_type = ?",
        (m_metric, m_mtype),
    ):
        code, thr, day = r[1], r[2], str(r[3])[:10]
        if not code:
            continue
        try:
            thi = int(float(thr) + 0.5)  # half-up: US esikleri float C (35.9 -> bucket 36)
        except (TypeError, ValueError):
            continue
        o = parse_resolved_outcome(r[4])
        t = ts(r[3])
        market[(code, day, thi)] = (str(r[0]), t, o, r[5])

    lon: dict[str, float] = {}
    for code, lg in db.execute(
        "SELECT DISTINCT city_code, longitude FROM weather_markets "
        "WHERE city_code IS NOT NULL AND longitude IS NOT NULL"
    ):
        try:
            lon.setdefault(code, float(lg))
        except (TypeError, ValueError):
            pass

    # METAR gunluk serisi: (code, day) -> [(epoch, temp)]
    day_rows: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for code, tmax, obs in db.execute(
        "SELECT city_code, temp_c, obs_time FROM metar_observations WHERE temp_c IS NOT NULL AND obs_time IS NOT NULL"
    ):
        day = str(obs)[:10]
        t = ts(obs)
        if code and t is not None:
            day_rows[(code, day)].append((t, float(tmax)))
    db.close()

    # Gun araligi: --min-day/--max-day verilmediyse fiyat verisinin araligi
    # (kullanici: "orderbook kac gunluk varsa yap").
    days_all = sorted({k[1] for k in day_rows})
    if args.min_day:
        days_all = [d for d in days_all if d >= args.min_day]
    if args.max_day:
        days_all = [d for d in days_all if d <= args.max_day]
    if not args.min_day and not args.max_day:
        t_min, t_max = None, None
        for lst in ask_series.values():
            if not lst:
                continue
            t0, t1 = lst[0][0], lst[-1][0]
            t_min = t0 if t_min is None else min(t_min, t0)
            t_max = t1 if t_max is None else max(t_max, t1)
        if t_min is not None and t_max is not None:
            lo = datetime.fromtimestamp(t_min).strftime("%Y-%m-%d")
            hi = datetime.fromtimestamp(t_max).strftime("%Y-%m-%d")
            days_all = [d for d in days_all if lo <= d <= hi]
    days_set = set(days_all)

    stake = args.stake
    slippage = args.slippage
    min_entry = args.min_entry

    bets: list[dict] = []
    for (code, day), rows in day_rows.items():
        if day not in days_set:
            continue
        city = code_name.get(code)
        if city and keep_bias is not None and city not in keep_bias:
            continue  # bias-top N disinda kalan sehir
        rows.sort(key=lambda x: x[0])
        utc_off = city_utc_offset(code, day, lon.get(code))
        # max: zirve kilidi (yerel 13:00+ + 2 dusus); min: dip kilidi
        # (yerel 06:00+ + 2 yukselis) — 2026-08-18 low-temperature deneyi.
        if is_min:
            pk, lock_epoch = trough_lock(rows, utc_off, getattr(args, "min_lock_hour", 6))
        else:
            pk, lock_epoch = peak_lock(rows, utc_off)
        if pk is None or lock_epoch is None:
            continue  # zirve/dip henuz kilitlenmemis -> bet yok
        B = int(pk + 0.5) if pk >= 0 else int(pk - 0.5)  # half-up (C2)
        neighbor = getattr(args, "neighbor", "none")

        def _pick_mkt(code: str, day: str, bk: int):
            """Kilitli bucket marketi; yoksa komsu esige iner/cikar.

            HIGH: alt esikler kesin kazanir (max >= kilitli >= esik).
            RANGE --neighbor lower/upper (2026-08-18 kullanici: "peakden ilk
            dusuk sicaklik aldigimizda bet acalim"): bucket marketi yoksa
            en yakin alt/ust esige bet acilir.
            """
            m = market.get((code, day, bk))
            if m is not None:
                return m, bk
            if is_high or neighbor == "lower":
                rng = range(bk - 1, bk - 8, -1)
            elif neighbor == "upper":
                rng = range(bk + 1, bk + 8)
            else:
                return None, bk
            for thr_cand in rng:
                cand = market.get((code, day, thr_cand))
                if cand is not None:
                    return cand, thr_cand
            return None, bk

        m, B = _pick_mkt(code, day, B)
        if m is None:
            continue
        mid, tgt, outcome, mtype = m
        if mtype != m_mtype:
            continue  # canli bot kurali: tam bucket yalnizca RANGE (HIGH deneyi haric)
        if tgt is None or outcome is None:
            continue
        # Kapanisa <2 sa kala kilitlendi -> bet acilmaz (canli bot kurali).
        if tgt - lock_epoch < MIN_HOURS_BEFORE_CLOSE * 3600:
            continue
        seri = ask_series.get(mid)
        if not seri:
            continue
        entry = ask_at_or_after(seri, lock_epoch)
        if entry is None or not (min_entry <= entry < MAX_ENTRY):
            continue
        entry_eff = entry + slippage
        if entry_eff >= 1.0:
            continue  # kaydirilmis fiyatla alinamaz
        # 2026-08-18 canli kural: kilitli deger ASILDIYSA (max: ustune,
        # min: altina) bet asilma aninda kapatilir (Milan senaryosu).
        # HIGH haric: HIGH'da YES = "max >= esik" — kilit bozulmasi (max
        # daha da yukselmesi) kazanma sansini ARTIRIR, kapatma yok, tutulur.
        bk = None if is_high else (trough_break if is_min else peak_break)(rows, pk, lock_epoch)
        if bk is not None:
            bk_ask = ask_at_or_after(seri, bk[0])
            if bk_ask is not None and 0 < bk_ask <= 1:
                per = (bk_ask - entry_eff) / entry_eff - FEE_RATE * (1.0 - bk_ask)
                won = bk_ask > entry_eff
            else:
                per = (
                    (1.0 / entry_eff - 1.0 - FEE_RATE * (1.0 - entry_eff))
                    if outcome
                    else (-1.0 - FEE_RATE * (1.0 - entry_eff))
                )
                won = outcome
        else:
            per = (
                (1.0 / entry_eff - 1.0 - FEE_RATE * (1.0 - entry_eff))
                if outcome
                else (-1.0 - FEE_RATE * (1.0 - entry_eff))
            )
            won = outcome
        pnl = stake * per - GAS
        bets.append(
            {
                "day": day,
                "city": code_name.get(code, code),
                "code": code,
                "bucket": B,
                "peak": pk,
                "entry": entry,
                "entry_eff": entry_eff,
                "stake": stake,
                "pnl": pnl,
                "won": won,
                "per": per,  # gas haric dolar basina net (kelly icin)
            }
        )
        # 2026-08-18 kullanici: "23 e ciktiginda 1 adet dusmesini beklemeyecek
        # hemen acacak, cunku 21 den 23 e cikti" — kilit bozulduysa bk aninda
        # eski bet KAPATILIR (yukarida) VE yeni zirvenin bucket'ina TEKRAR bet
        # acilir (dusus beklenmeden, bk degeri kazanan sayilir; zincir 1 adim).
        if bk is not None and not is_min and not is_high:
            B2 = int(bk[1] + 0.5)
            m2, _ = _pick_mkt(code, day, B2)
            if m2 is not None:
                mid2, tgt2, out2, _mt2 = m2
                if tgt2 is not None and out2 is not None and tgt2 - bk[0] >= MIN_HOURS_BEFORE_CLOSE * 3600:
                    seri2 = ask_series.get(mid2)
                    if seri2:
                        e2 = ask_at_or_after(seri2, bk[0])
                        if e2 is not None and min_entry <= e2 < MAX_ENTRY:
                            e2eff = e2 + slippage
                            if e2eff < 1.0:
                                bk2 = peak_break(rows, bk[1], bk[0])
                                if bk2 is not None:
                                    b2ask = ask_at_or_after(seri2, bk2[0])
                                    if b2ask is not None and 0 < b2ask <= 1:
                                        per2 = (b2ask - e2eff) / e2eff - FEE_RATE * (1.0 - b2ask)
                                        won2 = b2ask > e2eff
                                    else:
                                        per2 = (
                                            (1.0 / e2eff - 1.0 - FEE_RATE * (1.0 - e2eff))
                                            if out2
                                            else (-1.0 - FEE_RATE * (1.0 - e2eff))
                                        )
                                        won2 = out2
                                else:
                                    per2 = (
                                        (1.0 / e2eff - 1.0 - FEE_RATE * (1.0 - e2eff))
                                        if out2
                                        else (-1.0 - FEE_RATE * (1.0 - e2eff))
                                    )
                                    won2 = out2
                                bets.append(
                                    {
                                        "day": day,
                                        "city": code_name.get(code, code),
                                        "code": code,
                                        "bucket": B2,
                                        "peak": bk[1],
                                        "entry": e2,
                                        "entry_eff": e2eff,
                                        "stake": stake,
                                        "pnl": stake * per2 - GAS,
                                        "won": won2,
                                        "per": per2,
                                    }
                                )

    print("=== METAR-PEAK BACKTEST (sadece orderbook + METAR; forecast/bias YOK) ===")
    print(
        f"  kural: METAR peak kilitlenince (yerel 13:00+ + 2 ardisik dusus) sehir basina TEK YES bet, stake=${stake:g}"
    )
    print(
        f"  giris: kilitlenme sonrasi ilk gercek ask; maliyet: slippage +${slippage:.2f}, "
        f"fee %{FEE_RATE * 100:.0f}, gas ${GAS:.2f}"
    )
    if days_all:
        print(f"  pencere: {min(days_all)} .. {max(days_all)}  (min-entry={min_entry:.2f}, max-entry={MAX_ENTRY})")
    print()

    print(
        f"  {'gun':10s} {'bet':>4s} {'kazandi':>8s} {'win%':>6s} {'stake$':>8s} "
        f"{'fee+gas$':>9s} {'NET$':>9s} {'ROI%':>8s}"
    )
    tot_stake = tot_cost = tot_pnl = tot_ideal = 0.0
    n_all = w_all = 0
    for day in sorted({b["day"] for b in bets}):
        dbets = [b for b in bets if b["day"] == day]
        n = len(dbets)
        w = sum(1 for b in dbets if b["won"])
        stk = sum(b["stake"] for b in dbets)
        fees = sum(b["stake"] * FEE_RATE * (1.0 - b["entry_eff"]) + GAS for b in dbets)
        pnl = sum(b["pnl"] for b in dbets)
        ideal = sum(
            ((b["stake"] / b["entry"]) - cost_of(b["stake"], b["entry"]))
            if b["won"]
            else -cost_of(b["stake"], b["entry"])
            for b in dbets
        )
        roi = pnl / stk * 100 if stk > 0 else 0.0
        print(
            f"  {day:10s} {n:>4d} {w:>8d} %{w / n * 100:>5.1f} ${stk:>7.2f} ${fees:>8.2f} ${pnl:>+8.2f} %{roi:>+7.1f}"
        )
        tot_stake += stk
        tot_cost += fees
        tot_pnl += pnl
        tot_ideal += ideal
        n_all += n
        w_all += w
    if n_all:
        roi_all = tot_pnl / tot_stake * 100 if tot_stake > 0 else 0.0
        print(
            f"  {'TOPLAM':10s} {n_all:>4d} {w_all:>8d} %{w_all / n_all * 100:>5.1f} "
            f"${tot_stake:>7.2f} ${tot_cost:>8.2f} ${tot_pnl:>+8.2f} %{roi_all:>+7.1f}"
        )
        print()
        print(f"  yatirilan stake       : ${tot_stake:.2f}")
        print(f"  fee + gas toplami     : ${tot_cost:.2f}")
        print(f"  NET (slippage dahil)  : ${tot_pnl:+.2f}")
        print(f"  [slippage etkisi]     : slippage'siz NET ${tot_ideal:+.2f}  ->  fark ${tot_ideal - tot_pnl:+.2f}")
        # ---- KELLY varyasyonu (2026-08-18 kullanici: "stake kelly tarzi") ----
        # f = p - (1-p)*entry/(1-entry); p = ayni fiyat araliginin ampirik
        # winrate'i. Ortalama stake flat baz'a esit kalacak sekilde olceklenir
        # (ayni sermaye, farkli dagilim); stake [0.5, 10] arasina klipslenir.
        if getattr(args, "stake_mode", "flat") == "kelly":
            edges = [0.10, 0.25, 0.45, 0.70, 0.95]
            for b in bets:
                e = b["entry_eff"]
                lo = max((lo for lo in edges if lo <= e), default=0.10)
                hi = min((hi for hi in edges if hi > e), default=0.95)
                same = [x for x in bets if lo <= x["entry_eff"] < hi]
                p = (sum(1 for x in same if x["won"]) / len(same)) if same else 0.5
                f = max(0.0, p - (1.0 - p) * e / (1.0 - e))
                b["kelly_f"] = f
            fmean = sum(b["kelly_f"] for b in bets) / len(bets) if bets else 0.0
            if fmean > 0:
                for b in bets:
                    st_k = stake * b["kelly_f"] / fmean
                    b["kelly_stake"] = max(0.5, min(10.0, st_k))
                    b["pnl_kelly"] = b["kelly_stake"] * b["per"] - GAS
            stk_k = sum(b.get("kelly_stake", 0.0) for b in bets)
            pnl_k = sum(b.get("pnl_kelly", 0.0) for b in bets)
            roi_k = pnl_k / stk_k * 100 if stk_k > 0 else 0.0
            print(
                f"  [KELLY stake       ] : stake=${stk_k:.2f} (flat ${tot_stake:.2f})  "
                f"NET=${pnl_k:+.2f}  ROI %{roi_k:+.1f}"
            )
    else:
        print("  bet yok (veri eksik ya da hicbir peak kilitlenmemis)")
    return 0


# =====================================================================
# 4) METAR vs SETTLEMENT — METAR bucket vs gercek Polymarket kapanisi
# =====================================================================
# Kaynak: scripts/test_metar_vs_settlement.py (merge edildi, tasindi -> backtest_archive/)
# Kullanici sorusu: "METAR sonuclari WU'dan aliyor, Polymarket da WU'dan aliyor,
# neden tutmuyor?"  ->  METAR-bucket vs kazanan-bucket, actual vs kazanan,
# METAR vs actual fark dagilimi + RANGE/HIGH ayri winrate + RANGE PnL tahmini.


def cmd_metar_vs_settlement(args) -> int:
    db = sqlite3.connect(BOT_DB, timeout=20)
    db.execute("PRAGMA busy_timeout=20000")
    cur = db.cursor()

    # 1) METAR arsivi: (city_code, day) -> max temp
    metar_max: dict[tuple[str, str], float] = {}
    try:
        for code, day, tmax in cur.execute(
            "SELECT city_code, day, MAX(temp_c) FROM metar_observations "
            "WHERE city_code IS NOT NULL AND day IS NOT NULL "
            "AND day >= ? AND day <= ? GROUP BY city_code, day",
            (args.min_day, args.max_day),
        ):
            if code and tmax is not None:
                metar_max[(code, day)] = float(tmax)
    except sqlite3.OperationalError as exc:
        print(f"metar_observations okunamadi: {exc}")
        return 1

    # 2) Gercek cozum: (city_code, day) -> kazanan bucket.
    #    SADECE temperature_max + RANGE (tam bucket) marketlerinden — botun
    #    hedefi bu. Lowest-temp veya HIGH/LOW marketleri karistirilmaz
    #    (2026-08-18 bugfix: HK 08-15 'kazanan 26' aslinda lowest-temp'ti,
    #    highest METAR 33 ile karismis -> yanlis fark +7C raporlaniyordu).
    code_name: dict[str, str] = {}
    winner: dict[tuple[str, str], int] = {}
    for city, code, thr, day, raw, mtype, metric in cur.execute(
        "SELECT city, city_code, threshold, target_date, raw_data, market_type, metric "
        "FROM weather_markets "
        "WHERE threshold IS NOT NULL AND target_date IS NOT NULL AND raw_data IS NOT NULL"
    ):
        if mtype != "RANGE" or metric != "temperature_max":
            continue
        d = str(day)[:10]
        if not (args.min_day <= d <= args.max_day):
            continue
        if code:
            code_name.setdefault(code, city)
        o = parse_resolved_outcome(raw)
        if o is None:
            continue
        try:
            t = int(float(thr) + 0.5)
        except (TypeError, ValueError):
            continue
        if o is True and (winner.get((code, d)) is None or winner.get((code, d), -999) < t):
            winner[(code, d)] = t

    # 3) Open-Meteo actual: (city_code, day) -> gercek max (kac modelden olursa, son deger)
    actual_val: dict[tuple[str, str], float] = {}
    for code, day, av in cur.execute(
        "SELECT city_code, date, actual_value FROM historical_calibrations "
        "WHERE metric='temperature_max' AND actual_value IS NOT NULL"
    ):
        d = str(day)[:10]
        if code and args.min_day <= d <= args.max_day:
            actual_val[(code, d)] = float(av)
    db.close()

    print("=== METAR bucket vs GERCEK Polymarket kapanisi ===")
    print(
        f"  pencere: {args.min_day} .. {args.max_day} | metar_cities={len(metar_max)} "
        f"resolved_cities={len(winner)} actual_cities={len(actual_val)}"
    )
    print(f"{'city':12s} {'day':10s} {'METARmax':>8s} {'METARbkt':>7s} {'WIN':>3s} {'ACT':>5s} {'tutuyor':>7s}")

    metar_ok = act_ok = n = 0
    metar_vs_act_diff: list[tuple[float | None, float | None]] = []
    mismatches = []
    for code, day in sorted(winner):
        city = code_name.get(code, code)
        w = winner[(code, day)]
        mb = None
        if (code, day) in metar_max:
            mb = int(round(metar_max[(code, day)]))
        ab = None
        if (code, day) in actual_val:
            ab = int(round(actual_val[(code, day)]))
            mx = metar_max[(code, day)] if (code, day) in metar_max else None
            metar_vs_act_diff.append((mx, actual_val[(code, day)]))
        hit = "?" if mb is None else ("TUTUYOR" if mb == w else "X")
        if mb is not None:
            n += 1
            if mb == w:
                metar_ok += 1
            else:
                mismatches.append((city, day, metar_max[(code, day)], mb, w))
        if ab is not None:
            act_ok += int(ab == w)
        row = f"{city:12s} {day:10s}"
        row += f" {metar_max.get((code, day), float('nan')):8.1f}" if (code, day) in metar_max else f" {'-':>8s}"
        row += f" {mb if mb is not None else '-':>7d}" if mb is not None else f" {'-':>7s}"
        row += f" {w:>3d}"
        row += f" {ab if ab is not None else '-':>5d}" if ab is not None else f" {'-':>5s}"
        row += f" {hit:>7s}"
        print(row)

    print()
    if n:
        print(
            f"METAR bucket dogrulugu (esitlik): {metar_ok}/{n} = %{metar_ok / max(n, 1) * 100:.0f} "
            f"(round(METAR max) == Polymarket kazanan bucket)"
        )
    na = len(actual_val)
    print(
        f"Open-Meteo actual dogrulugu (esitlik): {act_ok}/{na} = %{act_ok / max(na, 1) * 100:.0f} "
        f"(round(actual) == kazanan bucket)"
    )

    # IKI MARKET TIPI (kullanici 2026-08-18 duzeltmesi):
    #   RANGE (tam bucket, "exactly 32C"): YES sadece round(gercek max) == b ise.
    #   HIGH (or-above, ">= 32C"): YES gercek max >= b ise (b <= kazanan bucket).
    print("\n--- IKI MARKET TIPI ICIN AYRI WINRATE ---")
    print(
        f"  RANGE (tam bucket 'exactly bC'): %{metar_ok / max(n, 1) * 100:.0f} ({metar_ok}/{n})  <- botun ana bet tipi"
    )
    bets = wins = 0
    loss_list = []
    for (code, day), w in winner.items():
        mx = metar_max.get((code, day))
        if mx is None:
            continue
        b = int(round(mx))
        bets += 1
        if b <= w:
            wins += 1
        else:
            loss_list.append((code_name.get(code, code), day, mx, b, w, b - w))
    print(f"  HIGH  (or-above '>= bC'): %{wins / max(bets, 1) * 100:.0f} ({wins}/{bets})  <- daha kolay kazanan tip")
    print("\n  --- HIGH icin kayiplar (METAR abartti: b > kazanan) ---")
    for city, day, mx, b, w, over in sorted(loss_list, key=lambda x: -x[5]):
        print(f"    {city:12s} {day}  METARmax={mx:5.1f}->{b}C  KAZANAN={w}C  abartma={over}C")

    # METAR vs actual fark dagilimi (istasyon uyusmazligi)
    pairs = [d for d in metar_vs_act_diff if d[0] is not None and d[1] is not None]
    if pairs:
        diffs: list[float] = []
        for a, b in pairs:
            if a is not None and b is not None:
                diffs.append(round(float(a) - float(b), 1))
        over = sum(1 for x in diffs if x >= 1.5)
        under = sum(1 for x in diffs if x <= -1.5)
        avg = sum(diffs) / len(diffs) if diffs else 0.0
        print(
            f"METAR max vs Open-Meteo actual fark: n={len(diffs)} ort={avg:+.1f}C "
            f"(METAR>=actual+1.5C: {over}, METAR<=actual-1.5C: {under})"
        )

    if mismatches:
        print("\n--- UYUSMAYANLAR (METAR yanlis bucket dedi) ---")
        for city, day, mx, mb, w in mismatches:
            print(f"  {city:12s} {day}  METARmax={mx:.1f}->bucket{mb}  KAZANAN={w}  (fark {mx - (w):+.1f}C)")

    # Cozum kurali dogrulamasi: kazanan bucket == round(gercek max)?
    print("\n--- Cozum kurali: kazanan bucket == round(actual max)? ---")
    rule_n = rule_ok = 0
    for (code, day), w in winner.items():
        av = actual_val.get((code, day))
        if av is None:
            continue
        rule_n += 1
        if int(round(av)) == w:
            rule_ok += 1
    print(f"  round(actual)==kazanan: {rule_ok}/{rule_n} = %{rule_ok / max(rule_n, 1) * 100:.0f}")

    # PnL tahmini: RANGE (tam bucket) marketi, orderbook giris fiyati ile.
    print("\n--- RANGE (tam bucket) PnL tahmini (orderbook giris, son 3 gun) ---")
    _pnl_hours = 6
    _stake = 3.0
    _fee = 0.05
    _gas = 0.10

    ob_series = None
    try:
        import sqlite3 as _sq

        ob = _sq.connect(OB_DB, timeout=15)
        ob.execute("PRAGMA busy_timeout=15000")
        ob_series = defaultdict(list)
        for mid, ask, st in ob.execute(
            "SELECT market_id, best_ask, snapshot_time FROM orderbook_snapshots WHERE best_ask IS NOT NULL"
        ):
            t = ts(st)
            if t is None:
                continue
            try:
                a = float(ask)
                if 0 < a <= 1:
                    ob_series[str(mid)].append((t, a))
            except (TypeError, ValueError):
                pass
        for k in ob_series:
            ob_series[k].sort(key=lambda x: x[0])
        ob.close()
    except Exception as exc:
        print(f"  (orderbook okunamadi: {exc})")

    market_range = {}  # (code, day, thr) -> (mid, target_ts)
    _db2 = sqlite3.connect(BOT_DB, timeout=20)
    for r in _db2.execute(
        "SELECT id, city_code, threshold, target_date, market_type, metric FROM weather_markets "
        "WHERE threshold IS NOT NULL AND target_date IS NOT NULL AND market_type='RANGE' AND metric='temperature_max'"
    ):
        mid, code, thr, tdate = r[0], r[1], r[2], r[3]
        d = str(tdate)[:10]
        if code and args.min_day <= d <= args.max_day:
            t = ts(tdate)
            if t is not None:
                market_range[(code, d, int(float(thr) + 0.5))] = (str(mid), t)
    _db2.close()

    def _price_at(series, bet_ts, window_sec=8 * 3600):
        best = None
        if not series:
            return None
        for t, a in series:
            if t > bet_ts:
                break
            if bet_ts - t <= window_sec:
                best = a
        return best

    if ob_series is not None:
        pnl_bets = []
        for (code, day), mx in metar_max.items():
            if winner.get((code, day)) is None:
                continue
            b = int(round(mx))
            w = winner[(code, day)]
            m = market_range.get((code, day, b))
            if m is None:
                continue
            mid, tgt = m
            if tgt is None:
                continue
            bet_ts = tgt + (12 - _pnl_hours) * 3600
            entry = _price_at(ob_series.get(mid), bet_ts)
            if entry is None or not (0.01 <= entry < 0.95):
                continue
            fee = _stake * _fee * (1.0 - entry)
            cost = _stake + fee + _gas
            win = b == w
            gain = (_stake / entry - cost) if win else -cost
            pnl_bets.append((code_name.get(code, code), day, b, entry, win, gain))
        n_b = len(pnl_bets)
        wins = sum(1 for p in pnl_bets if p[4])
        net = sum(p[5] for p in pnl_bets)
        stk = n_b * _stake
        print(
            f"  bet={n_b}  win=%{wins / max(n_b, 1) * 100:.0f}  "
            f"NET ${net:+.2f}  ROI %{net / max(stk, 1) * 100:.1f}  (stake=${_stake:g}, kapanisa {_pnl_hours}h kala)"
        )
        print("  bet detay (ilk 15):")
        for city, day, b, entry, win, gain in pnl_bets[:15]:
            print(f"    {city:12s} {day}  bkt={b:3d}  entry={entry:6.3f}  {'WIN' if win else 'LOST'}  ${gain:+.2f}")
    return 0


# =====================================================================
# 5) WALK-FORWARD — look-ahead'siz, zaman-tabanli fold dogrulama
# =====================================================================
# Kaynak: scripts/walk_forward_backtest.py (merge edildi, tasindi -> backtest_archive/)
# Uyari (2026-08-18): model secimi yalnizca onceki gunlerin kalibrasyonundan
# gelmelidir; bu modul in-sample vs out-of-sample secim yanliligini aciga cikarir.

import numpy as np  # noqa: E402

WF_BACKTEST_DB = os.path.join(_REPO_ROOT, "data", "backtest.db")
WF_OUTPUT_DIR = Path(_REPO_ROOT) / "data" / "backtest_results" / "walk_forward"

TRAIN_DAYS = 2
TEST_DAYS = 1
STEP_DAYS = 1
MIN_TRAIN_SAMPLES = 5

FLAT_BET = 10.0
MIN_EDGE = 0.05
MAX_ENTRY_PRICE = 0.90
MAX_HOURS_TO_SETTLEMENT = 24
MIN_HOURS_TO_SETTLEMENT = 1


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


def _wf_estimate_probability(mean: float, std: float, threshold: float, metric: str = "temperature_max") -> float:
    if std is None or std <= 0:
        std = 2.0
    z = (threshold - mean) / std
    if metric in ("temperature_max", "temperature_mean"):
        return 1.0 - _norm_cdf(z)
    else:
        return _norm_cdf(z)


def _wf_load_data():
    conn = sqlite3.connect(str(WF_BACKTEST_DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id as market_id, city, city_code, metric, threshold,
               target_date, yes_price, no_price, status, raw_data
        FROM weather_markets
        WHERE city IS NOT NULL AND target_date IS NOT NULL
    """
    )
    markets = [dict(r) for r in cur.fetchall()]

    cur.execute(
        """
        SELECT market_id, city, metric, target_date, yes_price, no_price,
               snapshot_time, hours_to_settlement
        FROM market_snapshots
        ORDER BY market_id, snapshot_time
    """
    )
    snapshots = [dict(r) for r in cur.fetchall()]

    cur.execute(
        """
        SELECT market_id, city, metric, target_date, source,
               predicted_value, confidence, model_weight, fetched_at
        FROM weather_forecasts
        ORDER BY market_id, fetched_at
    """
    )
    forecasts = [dict(r) for r in cur.fetchall()]

    conn.close()

    # 2026-08-18 audit fix (W1b): backtest.db sync'inde cozumler yalnizca
    # 05-Agu'ya kadar gelmis; bot.db'de 04-17 Agu arasi TAM. Outcome oncelikle
    # bot.db'den (guncel), eksikse backtest.db raw_data'sindan alinir.
    live_outcomes: dict[str, bool] = {}
    try:
        out_db = sqlite3.connect(BOT_DB, timeout=30)
        out_db.execute("PRAGMA busy_timeout=30000")
        for mid, raw in out_db.execute("SELECT id, raw_data FROM weather_markets WHERE raw_data IS NOT NULL"):
            o = parse_resolved_outcome(raw)
            if o is not None:
                live_outcomes[str(mid)] = o
        out_db.close()
    except sqlite3.OperationalError:
        pass

    for m in markets:
        if m.get("target_date"):
            m["target_date"] = datetime.fromisoformat(m["target_date"])
        if m.get("yes_price") is not None:
            m["yes_price"] = float(m["yes_price"])
        # 2026-08-18 audit fix (W1): sonuc kaynagi `bets` tablosu degil,
        # marketin GERCEK Polymarket cozumu. bets'te yalnizca botun kendi
        # gecmisi var (~44 cozumlu satir) -> walk-forward tek gun uretiyordu.
        m["outcome"] = live_outcomes.get(str(m["market_id"]))
        if m["outcome"] is None and m.get("raw_data"):
            m["outcome"] = parse_resolved_outcome(m["raw_data"])

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

    return markets, snapshots, forecasts


def _wf_forecast_index(forecasts: list) -> dict:
    """market_id -> fetched_at artan sirada forecast listesi.

    2026-08-18 audit fix (W4): eski kod her snapshot icin TUM forecast
    listesini lineer taradi (113k satir x 20k snapshot x 19 fold ~ milyarlarca
    karsilastirma -> dakikalar/saatler). Tek seferlik indeks ile her arama
    o marketin kendi kisa listesinde yapilir.
    """
    by_mid: dict[str, list] = defaultdict(list)
    for f in forecasts:
        by_mid[f["market_id"]].append(f)
    for lst in by_mid.values():
        lst.sort(key=lambda f: f.get("fetched_at") or datetime.min)
    return by_mid


def _wf_get_available_forecast(forecasts_by_mid: dict, market_id: str, decision_time: datetime):
    """decision_time oncesindeki SON fetch (bot func.max(fetched_at) secimi)."""
    avail = forecasts_by_mid.get(market_id)
    if not avail:
        return None
    latest = None
    for f in avail:
        ft = f.get("fetched_at")
        if ft is None:
            continue
        if ft <= decision_time:
            latest = f
        else:
            break
    if latest is None:
        return None
    return {
        "mean": latest.get("predicted_value"),
        "std": latest.get("confidence") or 2.0,
        "weight": latest.get("model_weight") or 1.0,
    }


def _wf_simulate_decision(snap: dict, forecast, hours_to_settlement: float, threshold, entry_price):
    if hours_to_settlement > MAX_HOURS_TO_SETTLEMENT:
        return None
    if hours_to_settlement < MIN_HOURS_TO_SETTLEMENT:
        return None

    if entry_price is None or entry_price > MAX_ENTRY_PRICE or entry_price < 0.05:
        return None

    if forecast is None or forecast.get("mean") is None:
        return None

    # 2026-08-18 audit fix (W2): eski kod snap.get("threshold", 25)
    # kullaniyordu ama market_snapshots'ta threshold kolonu YOK -> her bet icin
    # sabit 25 varsayiliyor, P(max>=25)~1 -> model_prob 0.99'a kilitleniyordu
    # (sahte %100 winrate'in asil kaynagi). Esik artik MARKET kaydindan gelir.
    model_prob = _wf_estimate_probability(
        forecast["mean"], forecast["std"], threshold, snap.get("metric", "temperature_max")
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


def _wf_run_single_fold(
    market_lookup,
    snapshots,
    forecasts_by_mid,
    price_series,
    test_start,
    test_end,
    fold_id,
    seen,
):
    results = []

    test_snaps = [
        s for s in snapshots if s.get("snapshot_time") is not None and test_start <= s["snapshot_time"] < test_end
    ]

    if not test_snaps:
        return results

    # 2026-08-18 audit fix (W3): bet zamana gore ilk UYGUN snapshot'ta acilir
    # ve market basina TEK bet vardir (botun dup-guard'i ile ayni). Eski kod
    # her saatlik snapshot'ta ayni markete yeniden giriyordu (ayni gun 11 bet,
    # %100 winrate'in ikinci kaynagi).
    test_snaps.sort(key=lambda s: s["snapshot_time"])

    for snap in test_snaps:
        market_id = snap["market_id"]
        if market_id in seen:
            continue
        decision_time = snap["snapshot_time"]

        market = market_lookup.get(market_id)
        if market is None:
            continue

        hours_to_settlement = snap.get("hours_to_settlement")
        if hours_to_settlement is None:
            target = market.get("target_date")
            if target:
                hours_to_settlement = (target - decision_time).total_seconds() / 3600
            else:
                continue

        outcome = market.get("outcome")
        if outcome is None:
            continue  # cozumlenmemis market simule edilemez

        forecast = _wf_get_available_forecast(forecasts_by_mid, market_id, decision_time)
        if forecast is None:
            continue

        # 2026-08-18 audit fix (W5): giris fiyati market_snapshots.yes_price
        # DEGIL, gercek fiyat serisinden (orderbook + CLOB price_history) —
        # snapshot yes_price market fonlanmadan once ~0 artefakti uretiyordu
        # (gunluk backtest'in C1 kuralinin aynisi).
        seri = price_series.get(market_id)
        if not seri:
            continue
        # ask_at_or_after epoch (float) bekler; dosyadaki ts() ile ayni kural.
        entry_price = ask_at_or_after(seri, decision_time.timestamp())
        if entry_price is None:
            continue

        threshold = market.get("threshold")
        if threshold is None:
            continue

        decision = _wf_simulate_decision(snap, forecast, hours_to_settlement, threshold, entry_price)
        if decision is None:
            continue

        won = bool(outcome)

        if won:
            pnl = FLAT_BET * (1 / decision["entry_price"] - 1) * (1 - FEE_RATE)
        else:
            pnl = -FLAT_BET

        seen.add(market_id)
        results.append(
            {
                "market_id": market_id,
                "city": market.get("city", "unknown"),
                "metric": market.get("metric", "unknown"),
                "entry_time": decision_time,
                "entry_price": decision["entry_price"],
                "model_prob": decision["model_prob"],
                "edge": decision["edge"],
                "net_edge": decision["net_edge"],
                "hours_to_settlement": hours_to_settlement,
                "won": won,
                "pnl": pnl,
                "fold": fold_id,
            }
        )

    return results


def _wf_walk_forward(markets, snapshots, forecasts_by_mid, price_series):
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

    market_lookup = {m["market_id"]: m for m in markets}
    seen: set[str] = set()
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

        train_settled = sum(1 for m in train_markets if m.get("outcome") is not None)

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

        fold_results = _wf_run_single_fold(
            market_lookup,
            snapshots,
            forecasts_by_mid,
            price_series,
            test_start,
            test_end,
            fold_id,
            seen,
        )

        print(f"  -> {len(fold_results)} bet")
        all_results.extend(fold_results)

        current_train_start += timedelta(days=STEP_DAYS)
        fold_id += 1

        if fold_id > 100:
            break

    return all_results


def _wf_calculate_metrics(results):
    if not results:
        return {}

    total_bets = len(results)
    wins = sum(1 for r in results if r["won"])
    wr = wins / total_bets if total_bets > 0 else 0

    total_pnl = sum(r["pnl"] for r in results)
    total_staked = total_bets * FLAT_BET
    roi = total_pnl / total_staked if total_staked > 0 else 0

    pnl_by_date = defaultdict(float)
    for r in results:
        d = r["entry_time"].date()
        pnl_by_date[d] += r["pnl"]

    daily_pnls = list(pnl_by_date.values())
    sd = np.std(daily_pnls) if len(daily_pnls) > 1 else 0
    sharpe = (np.mean(daily_pnls) / sd) * np.sqrt(365) if sd > 0 else 0

    cum_pnl = np.cumsum([r["pnl"] for r in results])
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
        "avg_net_edge": round(float(np.mean([r["net_edge"] for r in results])), 4),
        "avg_hours_to_settlement": round(float(np.mean([r["hours_to_settlement"] for r in results])), 1),
    }


def cmd_walk_forward() -> int:
    WF_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("Walk-Forward Backtest basliyor...\n")

    markets, snapshots, forecasts = _wf_load_data()
    resolved = sum(1 for m in markets if m.get("outcome") is not None)
    print(f"Yuklenen: {len(markets)} market ({resolved} cozumlu), {len(snapshots)} snapshot, {len(forecasts)} forecast")

    # Tek seferlik indeksler (W4): forecast lineer taramasi kaldirildi.
    forecasts_by_mid = _wf_forecast_index(forecasts)

    # Gercek fiyat serisi (W5): orderbook best_ask + CLOB price_history,
    # gunluk backtest ile ayni kaynaklar.
    price_series = _load_orderbook(OB_DB)
    if os.path.exists(BP_DB):
        try:
            bp = sqlite3.connect(BP_DB, timeout=30)
            for mid, t, p in bp.execute(
                "SELECT market_id, ts, price FROM price_history WHERE price > 0 AND price <= 1"
            ):
                try:
                    price_series[str(mid)].append((float(t), float(p)))
                except (TypeError, ValueError):
                    pass
            bp.close()
        except sqlite3.OperationalError:
            pass
    for k in price_series:
        price_series[k].sort(key=lambda x: x[0])

    results = _wf_walk_forward(markets, snapshots, forecasts_by_mid, price_series)

    if not results:
        print("Sonuc yok.")
        return 0

    df_path = WF_OUTPUT_DIR / "walk_forward_trades.csv"
    with open(df_path, "w", encoding="utf-8") as f:
        f.write(
            "market_id,city,metric,entry_time,entry_price,model_prob,edge,net_edge,hours_to_settlement,won,pnl,fold\n"
        )
        for r in results:
            f.write(
                f"{r['market_id']},{r['city']},{r['metric']},"
                f"{r['entry_time'].isoformat()},{r['entry_price']:.4f},"
                f"{r['model_prob']:.4f},{r['edge']:.4f},{r['net_edge']:.4f},"
                f"{r['hours_to_settlement']:.1f},{r['won']},{r['pnl']:.2f},{r['fold']}\n"
            )
    print(f"\nTrade'ler kaydedildi: {df_path}")

    metrics = _wf_calculate_metrics(results)
    print("\n=== GENEL SONUC ===")
    for k, v in metrics.items():
        print(f"  {k:30}: {v}")

    fold_metrics = defaultdict(list)
    for r in results:
        fold_metrics[r["fold"]].append(r)

    print("\n=== FOLD BAZLI ===")
    print(f"  {'Fold':>5} | {'Bahis':>6} | {'WR%':>6} | {'Net Kar':>10} | {'ROI%':>7}")
    print(f"  {'-' * 5}-+-{'-' * 6}-+-{'-' * 6}-+-{'-' * 10}-+-{'-' * 7}")
    for fid in sorted(fold_metrics.keys()):
        fm = _wf_calculate_metrics(fold_metrics[fid])
        print(
            f"  {fid:5d} | {fm.get('total_bets', 0):6d} | "
            f"{fm.get('win_rate', 0) * 100:5.1f}% | "
            f"${fm.get('total_pnl', 0):9.2f} | "
            f"{fm.get('roi', 0) * 100:6.1f}%"
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
        "folds": {str(fid): _wf_calculate_metrics(fold_metrics[fid]) for fid in sorted(fold_metrics.keys())},
    }

    import json

    report_path = WF_OUTPUT_DIR / "walk_forward_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\nRapor: {report_path}")
    return 0


# =====================================================================
# Ana girisc
# =====================================================================


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Junbo backtest komut dosyasi (tek giris noktasi)")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gunluk", help="gun gun gercekci backtest (botun su anki modu)")
    g.add_argument("--days", default="2026-08-16,2026-08-17", help="virgullu target gunler")
    g.add_argument("--detail", action="store_true", help="bet-bazli detay tablosu")
    g.add_argument(
        "--real-entry",
        action="store_true",
        help="sim giris fiyati yerine botun ayni marketteki GERCEK fill'lerini kullan",
    )
    g.set_defaults(func=lambda a: cmd_gunluk(a))

    o = sub.add_parser("orderbook", help="orderbook tabanli gercekci backtest (ham vs kalibre)")
    o.add_argument("--spread", type=int, default=3)
    o.add_argument("--max-entry", type=float, default=0.95)
    o.add_argument("--gap", type=float, default=0.0, help="fair-value gap (0=market<fair)")
    o.add_argument("--min-date", default="2026-08-05", help="orderbook basladi")
    o.add_argument("--bias-top", type=int, default=15)
    o.add_argument(
        "--fill",
        default="first_ask",
        choices=["first_ask", "median_ask", "vwap"],
        help="acilis fiyati varsayimi: first_ask=market ilk goruldugu an, "
        "median_ask=ilk 20 snapshot medyani, vwap=ilk 20 snapshot agirlikli ort",
    )
    o.add_argument(
        "--fair",
        default="gaussian",
        choices=["gaussian", "empirical"],
        help="fair-value modeli: gaussian (eski) veya empirical CDF (kalin kuyruk)",
    )
    o.set_defaults(func=lambda a: cmd_orderbook(a))

    m = sub.add_parser("metar_peak", help="METAR-peak gercekci backtest (actual vs clairvoyant)")
    m.add_argument("--hours-before", type=int, default=6)
    m.add_argument("--stake", type=float, default=3.0)
    m.add_argument("--min-day", default="2026-08-05")
    m.add_argument("--max-day", default="2026-08-16")
    m.set_defaults(func=lambda a: cmd_metar_peak(a))

    v = sub.add_parser("metar_vs_settlement", help="METAR bucket vs GERCEK Polymarket kapanisi dogrulama")
    v.add_argument("--min-day", default="2026-08-13")
    v.add_argument("--max-day", default="2026-08-17")
    v.set_defaults(func=lambda a: cmd_metar_vs_settlement(a))

    pl = sub.add_parser("metar_peak_live", help="METAR-peak saf backtest (sadece orderbook+METAR, tek bet/sehir)")
    pl.add_argument("--min-day", default=None, help="baslangic gunu (default: fiyat verisi araligi)")
    pl.add_argument("--max-day", default=None, help="bitis gunu (default: fiyat verisi araligi)")
    pl.add_argument("--stake", type=float, default=3.0)
    pl.add_argument("--slippage", type=float, default=0.01, help="ask ustune eklenen fiyat kaymasi")
    pl.add_argument("--min-entry", type=float, default=0.05, help="MIN_ENTRY (0 = filtre yok)")
    pl.add_argument("--bias-top", type=int, default=0, help="en az sapan N sehir (0 = tum sehirler)")
    pl.add_argument(
        "--metric", default="max", choices=["max", "min"], help="max=zirve (varsayilan), min=dip (low temp)"
    )
    pl.add_argument(
        "--market-type",
        default="range",
        choices=["range", "high"],
        help="range=tam bucket (varsayilan), high=or-above (max >= esik, kapatma yok)",
    )
    pl.add_argument(
        "--neighbor",
        default="none",
        choices=["none", "lower", "upper"],
        help="bucket marketi yoksa komsu esige bet (kilit bozulursa yeni peak'e aktar)",
    )
    pl.add_argument(
        "--min-lock-hour", type=int, default=6, help="min kilidi icin yerel saat esigi (dip gundogumu sonrasi)"
    )
    pl.add_argument(
        "--stake-mode", default="flat", choices=["flat", "kelly"], help="flat=$3 sabit, kelly=fiyata gore olcekli"
    )
    pl.set_defaults(func=lambda a: cmd_metar_peak_live(a))

    w = sub.add_parser("walk_forward", help="walk-forward (look-ahead'siz) model dogrulama")
    w.set_defaults(func=lambda a: cmd_walk_forward())

    return p


def main() -> int:
    args = _build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
