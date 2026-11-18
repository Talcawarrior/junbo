"""TUM SESSION ozeti — bastan itibaren tum detaylar MD dosyasi."""

import sqlite3
import sys
from datetime import datetime, timezone
import sys as _sys
from pathlib import Path as _Path
import os

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_data_dir, get_db_path  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

now = datetime.now(timezone.utc).replace(tzinfo=None)
out = []

out.append("# JUNBO SESSION OZETI (TUMU)")
out.append(f"**Olusturma:** {now} UTC | **Dosya:** SESSION_OZET_2026-08-17.md")
out.append("")
out.append(
    "Bu dosya bot'un bastan itibaren (2026-08-04'ten beri) tum detayini icerir: veri hacmi, tum betler, "
    "strateji kararlari, duzeltilen bug'lar, backtest sonuclari, yarinki yapilacaklar."
)
out.append("")

db = sqlite3.connect(get_db_path())
db.row_factory = sqlite3.Row
ob = sqlite3.connect(os.path.join(get_data_dir(), "orderbook.db"))
ob.row_factory = sqlite3.Row

# ============ 1. VERI HACMI ============
out.append("## 1. VERI HACMI")
out.append("")
out.append("### bot.db")
out.append("")
rows = [
    ("weather_markets", "SELECT COUNT(*) FROM weather_markets"),
    ("weather_markets (farkli gun)", "SELECT COUNT(DISTINCT substr(target_date,1,10)) FROM weather_markets"),
    ("weather_forecasts", "SELECT COUNT(*) FROM weather_forecasts"),
    ("analyses", "SELECT COUNT(*) FROM analyses"),
    ("market_snapshots", "SELECT COUNT(*) FROM market_snapshots"),
    ("historical_calibrations", "SELECT COUNT(*) FROM historical_calibrations"),
    ("metar_observations", "SELECT COUNT(*) FROM metar_observations"),
    ("bets (toplam)", "SELECT COUNT(*) FROM bets"),
]
for label, sql in rows:
    out.append(f"- **{label}:** {db.execute(sql).fetchone()[0]:,}")
out.append("")
r = db.execute("SELECT MIN(target_date), MAX(target_date) FROM weather_markets").fetchone()
out.append(f"- weather_markets tarih araligi: {r[0][:10]} .. {r[1][:10]}")
out.append("")
out.append("### orderbook.db")
out.append("")
out.append(f"- **orderbook_snapshots:** {ob.execute('SELECT COUNT(*) FROM orderbook_snapshots').fetchone()[0]:,} satir")
_q_market = "SELECT COUNT(DISTINCT market_id) FROM orderbook_snapshots"
out.append(f"- **farkli market:** {ob.execute(_q_market).fetchone()[0]:,}")
r = ob.execute(
    "SELECT MIN(substr(snapshot_time,1,10)), MAX(substr(snapshot_time,1,10)) FROM orderbook_snapshots"
).fetchone()
out.append(f"- orderbook tarih araligi: {r[0]} .. {r[1]}")
out.append("")
out.append("> 500.000+ token/orderbook kaydi, 5.725+ market, 12 gun fiyat gecmisi.")
out.append("")

# ============ 2. STRATEJI KARARLARI ============
out.append("## 2. STRATEJI KARARLARI (kronolojik)")
out.append("")
out.append("| Tarih | Karar | Detay |")
out.append("|---|---|---|")
decisions = [
    (
        "2026-08-10",
        "7'li spread KALDIRILDI",
        "RANGE marketlerde sadece 1 esik kazanir; 7'den 6'si matematiksel kayip. Tek esik = merkez.",
    ),
    (
        "2026-08-11",
        "KAYAN PENCERE KAPALI",
        "Merkez kayarsa bile acilan betler settlement'a kadar TUTULUR. Backtest: kaydirma her config'de zarar.",
    ),
    ("2026-08-11", "Canli yes_price kullanimi", "Giris fiyati = CANLI yes_price (bayat snapshot degil)."),
    ("2026-08-12", "Sehir top-N'den dusse bile bet KAPATILMAZ", "Erken kapanis kazanan esikleri bile satiyordu."),
    ("2026-08-12", "Fair-value + 0.10-0.20 olum bolge filtreleri", "Sonradan KALDIRILDI (16 Agu)."),
    ("2026-08-14", "TEK ESIK (radius=0) + bias-top 12", "Backtest: radius0 +$41.9, radius3 -$317."),
    ("2026-08-15", "3 esige indir (radius=1)", "Kisa sureligine; sonra degisti."),
    (
        "2026-08-16",
        "0.01-0.95 arasi ilk 40 markete ac",
        "spread_max_entry=0.95, max_bets=40, fair-value+olum-bolge KALDIRILDI.",
    ),
    (
        "2026-08-16",
        "METAR-peak YEREL saat mantigi",
        "Sehirin boylamindan UTC offset; kilitlenme yerel 13+ sonrasi. Dogu sehirleri artik yakalanir.",
    ),
    (
        "2026-08-16",
        "YANLIS BUCKET BETLERINI KAPAT (3. adim)",
        "Peak kilitlenince kazanan bucket disindaki tum acik betler canli fiyattan satilir.",
    ),
    ("2026-08-16", "3 ESIK (radius=1) + peak'te komsu satisi", "T-2'de 3 esige gir, peak'te komsulari hemen sat."),
    ("2026-08-17", "GUNLUK LIMIT 120", "Kullanici: 'Toplam 120'. 40 iken 19 Agu'ya hic bet acilamiyordu."),
]
for d in decisions:
    out.append(f"| {d[0]} | **{d[1]}** | {d[2]} |")
out.append("")

# ============ 3. DUZELTILEN BUG'LAR ============
out.append("## 3. DUZELTILEN BUG'LAR")
out.append("")
bugs = [
    (
        "2026-08-16",
        "Polymarket proxy yok",
        "PAC dosyasi SOCKS 40000'e yonlendiriyor; Python requests kullanmiyordu -> 10054. POLY_PROXY eklendi.",
    ),
    (
        "2026-08-16",
        "CITY_ICAO_MAP 7 sehir yanlis istasyon",
        "UUEE->UUWW, EGLL->EGLC, LFPG->LFPB, RKSS->RKSI, RCTP->RCSS, KDEN->KBKF, KIAH->KHOU.",
    ),
    ("2026-08-16", "detect_peak sabah kilitlenmesi", "Gecenin sicakligi zirve saniliyordu. Yerel saat esigi eklendi."),
    ("2026-08-16", "partial_tp_done NOT NULL", "Bot bet acamiyordu; migration ile kolon kaldirildi."),
    (
        "2026-08-16",
        "METAR/Open-Meteo proxy sizintisi",
        "Global env proxy aviationweather+open-meteo'yu da yakaliyordu -> 172 timeout. proxies={'all': None}.",
    ),
    (
        "2026-08-16",
        "orderbook sadece betli marketler",
        "clob_stream sadece betli marketleri dinliyordu. Tum acik marketlere genisletildi + collect_orderbook.py.",
    ),
    (
        "2026-08-16",
        "radius=0 falsy bug",
        "`int(getattr(s,'spread_radius',3) or 3)` -> 0'i default 3'e dusuruyordu. `is None` guard.",
    ),
    (
        "2026-08-17",
        "METAR timeout (peak kaciyordu)",
        "40 sehir tek tek cekiliyordu -> 60s timeout. ThreadPoolExecutor(8) ile paralel.",
    ),
    ("2026-08-17", "CLOB WebSocket baglanamiyor", "WARP SOCKS WS desteklemiyor. aiohttp_socks + REST /book yedegi."),
    ("2026-08-17", "Gunluk limit 40 19 Agu'yu blokluyor", "remaining = 40-46 = 0. Limit 120."),
    (
        "2026-08-17",
        "API 4.7MB / timeout (Chrome baglaniyor)",
        "analyses indexsiz + 15k missed-signal serialization. Indexler + limit=200.",
    ),
]
for b in bugs:
    out.append(f"- **{b[0]} — {b[1]}:** {b[2]}")
out.append("")

# ============ 4. TUM BETLER (ozet) ============
out.append("## 4. BET DURUMU (genel)")
out.append("")
r = db.execute("SELECT status, COUNT(*) n FROM bets GROUP BY status ORDER BY n DESC").fetchall()
for x in r:
    out.append(f"- **{x['status']}:** {x['n']}")
out.append("")
r = db.execute("SELECT COUNT(*), COALESCE(SUM(realized_pnl),0) FROM bets WHERE status IN ('won','lost')").fetchone()
out.append(f"- **Cozen (won+lost):** {r[0]} bet, toplam realized_pnl ${r[1]:+.2f}")
out.append("")

# ============ 5. 17 AGUSTOS DETAY ============
out.append("## 5. 17 AGUSTOS — GUNUN DETAYI")
out.append("")
out.append("### Kapatilan 54 bet (rotation) — satis + PnL")
out.append("")
out.append("| bet# | sehir | esik | entry | shares | stake | realized_pnl |")
out.append("|---|---|---|---|---|---|---|")
closed = db.execute(
    "SELECT b.id, b.city, b.strike_temp, b.entry_price, b.shares, b.amount, b.realized_pnl "
    "FROM bets b JOIN weather_markets w ON w.id=b.market_id "
    "WHERE w.target_date LIKE '2026-08-17%' AND b.status='closed' AND b.close_reason='rotation' "
    "ORDER BY b.city, b.strike_temp"
).fetchall()
tot_pnl = 0.0
for r in closed:
    pnl = float(r["realized_pnl"] or 0)
    tot_pnl += pnl
    out.append(
        f"| {r['id']} | {r['city']} | {r['strike_temp']:.0f}C | {float(r['entry_price'] or 0):.3f} | "
        f"{float(r['shares'] or 0):.1f} | ${float(r['amount'] or 0):.2f} | {pnl:+.2f} |"
    )
out.append("")
out.append(f"**Kapatilan TOPLAM: {len(closed)} bet, NET PnL ${tot_pnl:+.2f}**")
out.append("")
out.append("### Acik metar-peak betler (17 Agu)")
out.append("")
out.append("| bet# | sehir | esik | entry | acilis |")
out.append("|---|---|---|---|---|")
mb = db.execute(
    "SELECT b.id, b.city, b.strike_temp, b.entry_price, substr(b.placed_at,12,5) pl FROM bets b "
    "JOIN weather_markets w ON w.id=b.market_id "
    "WHERE w.target_date LIKE '2026-08-17%' AND b.order_id LIKE 'metar_%' AND b.status IN ('placed','open') "
    "ORDER BY b.placed_at"
).fetchall()
for r in mb:
    out.append(
        f"| {r['id']} | {r['city']} | {r['strike_temp']:.0f}C | {float(r['entry_price'] or 0):.3f} | {r['pl']} |"
    )
out.append("")

# ============ 6. BACKTEST SONUCLARI ============
out.append("## 6. BACKTEST SONUCLARI")
out.append("")
out.append("### METAR-peak (kazanan bucket'a giris, orderbook gercek fiyat, $3 stake)")
out.append("")
out.append("| Config | Bet | NET | ROI |")
out.append("|---|---|---|---|")
out.append("| Kapanisa 6 saat kala | 28 | +$139.21 | %165.7 |")
out.append("| 8 saat kala | 32 | +$109.71 | %171 |")
out.append("| Gunluk ortalama | ~3 bet/gun | ~+$15.47/gun | - |")
out.append("")
out.append("### Spread tek esik (orderbook, $2)")
out.append("")
out.append("| Config | Bet | Winrate | PnL |")
out.append("|---|---|---|---|")
out.append("| bias-top 15 | 94 | %43.6 | +$74.05 |")
out.append("| bias-top 40 | 229 | %31.0 | -$10.42 |")
out.append("| bias-top 49 | 276 | %29.7 | -$28.25 |")
out.append("")
out.append("### 16 Agu gercek peak dogrulugu")
out.append("")
out.append("- Kilitlenen 42 sehirden 30'u dogru bucket (%71), 12 yanlis (%29)")
out.append("- ROI senaryolari: entry~0.30 -> %131, entry~0.50 -> %37, entry~0.67 -> %2")
out.append("")

# ============ 7. YAPILACAKLAR ============
out.append("## 7. YAPILACAKLAR (YARIN 18 AGU)")
out.append("")
out.append("- [ ] 17 Agu orderbook verisi (23.9k satir) ile komsu-satisi backtest'i")
out.append("- [ ] 3-esik + kazanan-tut + komsu-sat net PnL (kapatmasaydik vs kapatinca)")
out.append("- [ ] Komsu fiyatlar peak oncesi yukseliyor mu? (13:00 vs peak ani)")
out.append("- [ ] `_close_wrong_bucket_bets` satis pencere optimizasyonu")
out.append("- [ ] 18 Agu peak'lerinin canli takibi + bet acilis dogrulamasi")
out.append("")

out_path = r"C:\Users\fdemir\Documents\New project\junbo\SESSION_OZET_2026-08-17.md"
with open(out_path, "w", encoding="utf-8") as f:
    f.write("\n".join(out))

print(f"Yazildi: {out_path}")
print(f"{len(out)} satir, {sum(len(x) for x in out):,} karakter")
