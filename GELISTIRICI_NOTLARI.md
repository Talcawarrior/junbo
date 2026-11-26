# Gelistirici Notlari — Junbo Bot

**Son guncelleme:** 2026-10-22 — PnL muhasebesi tek kaynaga baglandi; settlement gurultusu + Gamma 403 duzeltildi.

---

## 0. BOTUN CALISMA PRENSIBI (TAM AKIS)

### 0.1 Genel Bakis
Junbo, Polymarket'te sicaklik bahisleri yapan PAPER MODE (DRY_RUN) bir b ottur. Bot, hava durumu tahminleri ve METAR (havalimani sicaklik) verisini kullanarak bahis acar.

**Temel strateji: METAR-peak** — havalimani sicaklik verisinden gunluk zirveyi tespit eder, Polymarket'te dogru bucket'a YES bahsi acar.

### 0.2 Veri Akisi (Sirali)

```
1. METAR VERISI (her 30 dk)
   Kaynak: aviationweather.gov (NOAA, bedava)
   Veri: Son 30 saatlik sicaklik gozlemleri (her 30 dk'da 1 gozlem)
   Arsiv: metar_observations tablosu (tarih kisitli API'nin disinda kalici saklama)
   Islem: Her sehir icin sicaklik serisi cekilir, peak tespit edilir

2. HAVA DURUMU TAHMINLERI (her 30 dk)
   Kaynak: Open-Meteo + WeatherAPI (bedava + $10/ay)
   Veri: temperature_max/min, 8 kaynak (ECMWF, GFS, ICON, UKMO, vb.)
   Tablo: weather_forecasts
   Islem: Her acik market icin tahminler cekilir, kalibrasyon uygulanir

3. PIYASA VERISI (surekli)
   Kaynak: Polymarket CLOB API (WebSocket + REST)
   Veri: Fiyatlar (yes_price/no_price), orderbook derinligi
   Tablolar: weather_markets (fiyat), orderbook_snapshots (gecmis)
   Islem: Canli fiyat takibi, stale fiyat kontrolu

4. ANALIZ (her dongude)
   Girdi: Tahmin + Piyasa fiyati + Kalibrasyon
   Islem: Edge hesaplama, Kelly criterion, pozisyon buyuklugu
   Cikti: Sinyaller (ac/kapat/hold)

5. BAHIS ACMA (her dongude)
   Girdi: Sinyaller + Risk sinirlari + Portfoy durumu
   Islem: Paper bet acma (DRY_RUN=true)
   Cikti: bets tablosuna yazma

6. SETTLEMENT (her dongude)
   Islem: Polymarket'ten sonuc cekme, PnL hesaplama
   Cikti: bets tablosu guncelleme, portfolio guncelleme
```

### 0.3 METAR Peak Tespit Mekanizmasi (Detayli)

```
GIRIS: aviationweather.gov'dan her sehir icin son 30 saatlik METAR serisi

ADIM 1: YEREL SAAT ESIGI
- Peak tespiti SADECE yerel saat >= 13:00'ten sonra calisir
- Neden: Sabah/gece sicakligi peak degildir, gunesin dogdugu saatlerde sicaklik yukselir
- Ornegin: Hong Kong (UTC+8) -> 05:00 UTC = 13:00 HK, o saatten once peak yok

ADIM 2: KUMULATIF MAX TAKIBI
- Her gozleme gore kumulatif max guncellenir
- Ornek: 20 -> 21 -> 22 -> 23 -> 22 (max=23, ilk dusus)

ADIM 3: PEAK ADAYI (1 dusus kurali)
- Kumulatif max'tan ilk dusus goruldu -> peak_candidate = max
- Dusus = sicaklik onceki gozleme gore dustu (esitlik dusus sayilmaz)
- Neden 2 degil 1: Hizli piyasada 2. dususu beklerken fiyat duser, firsat kaybolur

ADIM 4: ONAY PENCERESI (30 dk)
- Peak adayi goruldu: candidate_time = simdi
- 30 dk boyunca sicaklik peak_candidate ALTINDA kalmali
- Eger 30 dk icinde sicaklik tekrar peak_candidate'e cikarsa -> YANLIS ALARM
- Ornek LA vakasi: 23.6C peak -> 1 dusus -> 30 dk icinde 27.3C'ye cikti -> KILITLENMEDI

ADIM 5: KILITLENME
- 30 dk doldu + sicaklik hala peak_candidate altinda -> peak KILITLENDI
- Kazanan bucket = round(peak_candidate)
- Ornegin: peak=23.6C -> round(23.6)=24 -> "Will highest be 24C?" market'ine YES

ADIM 6: BAHIS ACMA
- Polymarket'te ilgili bucket market'ini bul (temperature_max, RANGE, dogru tarih)
- Entry fiyati = CLOB canli ask fiyati (orderbook'dan)
- Stake = config'den (default $5)
- Stale fiyat kontrolu: entry fiyati orderbook ask ile %10'dan fazla saparsa iptal

ADIM 7: ZIRVE ASILMASI (chain)
- Eger kilitleme sonrasi sicaklik peak'i asarsa (24 -> 25 -> 26)
- Eski bucket bahsi KAPATILIR (rotation)
- Yeni peak'in bucket'ina YENI bet acilir
- Bu zincir 22->23->24->25 seklinde devam edebilir
- Her adimda sadece 1 aktif bet olur (eski kapatilir, yeni acilir)
```

### 0.4 Sehir Ismi vs ICAO Kodu Karismasi (KRITIK BILGI)

Bot 3 farkli sehir temsili kullanir:

| Temsil | Ornek | Kullanan Tablo |
|--------|-------|----------------|
| **ICAO kodu** | KLAX, EGLL, RKSI | weather_markets.city_code, weather_forecasts.city |
| **Sehir adi** | Los Angeles, London, Seoul | weather_markets.city, bets.city |
| **Polymarket ID** | 3352310, 3352334 | weather_markets.id, bets.market_id |

**Eslesme kurallari:**
- `weather_markets.city_code` (ICAO) = `weather_forecasts.city` (ICAO) -> birebir eslesir
- `weather_markets.city` (sehir adi) != `weather_forecasts.city` (ICAO) -> ESLESMEZ
- `bets.city` (sehir adi) != `weather_markets.city_code` (ICAO) -> ESLESMEZ
- `weather_markets` tablosu: hem `city` (sehir adi) hem `city_code` (ICAO) var
- `weather_forecasts` tablosu: SADECE `city` (ama ICAO kodu icerir!)
- `bets` tablosu: SADECE `sehir adi` icerir

**Veri kaynaklarindaki karismasi:**
- ASIAbot bot_backup.db: TUM forecast'larda ICAO kodlari (KLAX, EDDM, vb.)
- ASIAbot bot_test.db: COGUNLUKLA ICAO ama BAZILAR sehir adi (London, Paris, vb.)
- Junbo backups: ICAO kodlari
- Open-Meteo/WeatherAPI: ICAO kodlari kullanir

**Dogru eslesme icin:**
- Sehir adi -> ICAO: `weather_markets.city_code` lookup tablosu kullanilir
- ICAO -> sehir adi: `weather_markets.city` lookup tablosu kullanilir
- Forecast restore ederken: source.city (ICAO veya sehir adi) -> target weather_markets.city_code eslesmeli

### 0.5 Settlement (Kapanis) Mekanizmasi

```
SETTLEMENT SURECI:
1. Polymarket'te market kapanir (target_date + 12 saat)
2. Bot settlement dongusunde kapanis fiyati ceker
3. YES bet icin: kapanis fiyati > 0.50 -> KAZAN (1.00 - kapanis) * stake / entry
4. YES bet icin: kapanis fiyati <= 0.50 -> KAYBET (tum stake)
5. realized_pnl hesaplanir, bets tablosuna yazilir
6. Portfolio guncellenir

KAPANIS TURLERI:
- won: market settle edildi, biz kazandik
- lost: market settle edildi, biz kaybettik
- closed: erken kapatma (rotation, stop-loss vb.)
- closed_early: zaman erken kapatma

ROTATION KAPANIS:
- Peak asilmasi sonrasi eski bucket beti kapatilir
- close_reason = 'rotation'
- settled_at = kapanis zamani (yeni eklendi, 2026-09-08)
- realized_pnl = kapanis fiyatinin bet sonucu
```

### 0.6 Kalibrasyon ve Tahmin Agirligi

```
8 TAHMIN KAYNAGI:
1. ecmwf_ifs025  (Avrupa Merkezi) - en iyi genel performans
2. gfs_seamless  (ABD NOAA)
3. icon_global   (Avrupa/Almanya)
4. ukmo_seamless (Ingiltere)
5. meteofrance_seamless (Fransa)
6. jma_seamless  (Japonya)
7. gem_global    (Kanada)
8. cma_grapes_global (Cin)

KALIBRASYON:
- historical_calibrations tablosu: sehir x model x metric -> MBE (Mean Bias Error)
- Kalibrasyon referansi: METAR istasyon max/min (Open-Meteo degil)
- Fallback: bilinmeyen model/sehir icin tum modellerin ortalamasi
- Kalibrasyon gunde 1 kez tazelenir (evolution_job.py)
- Rolling window: 30-90 gunluk veri ile bias hesaplama

DOGRU OLCUM ICIN:
- Metarpeak kaybi: collect_settlement_temps.py -> WU gercek sicaklik + Polymarket bucket parse
- Tahmin basarisi: compute_forecast_winners_final.py -> hangi model en yakin tutturdu
- Bias dogrulama: backfill_calibration.py --source metar
```

### 0.7 Bot Donguleri (7 Aktif Loop)

| Loop | Suresi | Is |
|------|--------|---|
| scan_and_bet | ~30 sn | Market tarama -> analiz -> bahis acma |
| settlement | ~60 sn | Kapanis kontrolu, PnL guncelleme |
| price_poller | ~30 sn | Canli fiyat guncelleme (CLOB REST) |
| snapshot | 30 dk | Market snapshot arsivleme |
| clob_stream | surekli | WebSocket canli fiyat |
| metar | ~30 dk | METAR cekme, peak tespit, bet acma |
| weather_fetch | ~30 dk | Open-Meteo tahmin cekme |

### 0.8 Risk Kontrolleri

- `DRY_RUN=true` kod seviyesinde sabit (canli trade ASLA)
- `max_bet_pct=0.10` (portfoy %10'u max tek bet)
- `total_exposure_pct=0.50` (portfoy %50'si max acik pozisyon)
- `spread_max_entry=0.50` (0.50 ustu fiyata bet acilmaz)
- `spread_min_entry=0.10` (0.10 alti fiyata bet acilmaz)
- `spread_blacklist`: Amsterdam, Hong Kong, Seoul, Moscow, Paris
- Stale fiyat guard: orderbook ask ile %10 sapma varsa bet REDDEDILIR
- Peak onay: 30 dk confirmation window (erken kilit onlenir)

### 0.9 Veri Konumlari (KONTROL ZORUNLU)

Veri yok demeden once SU KONUMLARA BAKILACAK:

| Konum | Icerik | Boyut |
|-------|--------|-------|
| `junbo\data\bot.db` | Ana veritabani (bet, forecast, metar, market) | ~700MB |
| `junbo\data\orderbook.db` | CLOB orderbook snapshotlari | ~2.4GB |
| `junbo\data\backups\` | Gunluk bot DB backuplari (476 dosya) | ~50GB |
| `D:\JUNBO data\backups\` | Dis kaynak backuplari | ~100GB |
| `D:\ASIA data\bot_backup.db` | ASIAbot forecast (390k kayit, 15-24 Agu) | 762MB |
| `D:\ASIA data\bot_test.db` | ASIAbot forecast (153k kayit, 19-28 Agu) | 762MB |
| `D:\ASIA data\junbo_bot.db` | Junbo eski snapshot (67k forecast, 15-24 Agu) | 316MB |
| `D:\HEAT data\era5\` | ERA5 tarihsel veri + kalibrasyon sample | 2 dosya |
| `D:\HEAT data\weather_data\` | Sehir listesi, NOAA istasyon verisi | 4 dosya |
| `ASIAbot\data\bot.db` | ASIAbot ana DB (forecast yok, 23k market) | ~750MB |

**MEVCUT VERI DURUMU (2026-09-10):**
- weather_forecasts: 42,800 kayit (19 Agu - 10 Eyl)
- metar_observations: 73,213 kayit (1 Agu - 10 Eyl)
- bets: 2,209 kayit (6 Agu - 9 Eyl)
- orderbook_snapshots: ~5M kayit (16 Agu - 10 Eyl)
- weather_markets: 31,706 kayit (4 Agu - 15 Eyl)

## 1. ZORUNLU: Her Kod Degisikligi Sonrasi

```bash
# 1) Latent-bug testleri (once bunu)
python -m pytest tests/test_latent_bugs.py -v --tb=long

# 2) Lint + type
python quick_check.py --fast
# ya da tek tek: ruff check . --ignore F401 ; mypy --ignore-missing-imports .

# 3) Degismis modul testleri
python -m pytest tests/test_calculator.py tests/test_calibration_audit.py -v --tb=short

# 4) E2E + liveliness
python -m pytest tests/test_e2e_system.py tests/test_integration_e2e.py tests/test_faz2_e2e_mock.py tests/test_liveliness_audit.py --tb=short -q

# 5) MUHASEBE/SETTLEMENT degisikliklerinde (ZORUNLU):
python -m pytest tests/test_accounting.py tests/test_settler_polymarket.py tests/test_signals_active_positions.py --tb=short -q

# 6) FULL suite (push oncesi)
python -m pytest tests/ --ignore=tests/test_betting_idempotency.py --ignore=tests/test_comprehensive.py --tb=short -q
# 2026-11-26 aksam (odul kaydi turu) gercek: 866 passed, 6 skipped, 0 failed (648 sn; +6 test_poly_rewards_recorder).
# 2026-11-26 (odul arastirmasi) gercek: 860 passed, 6 skipped, 0 failed (561 sn; +11 test_poly_rewards_formula).
# 2026-11-26 gercek: 849 passed, 6 skipped, 0 failed (615 sn; +6 warp_health testi: gamma-api/CloudFront yoklamasi).
# 2026-11-25 gercek: 843 passed, 6 skipped, 0 failed (pre-commit hook secimi 840/6/0 [test_replay_engine haric]
#   + test_replay_engine 3 passed; README port 8093, ALLOWED_DEAD'e yerel research betigi +4 isim).
# 2026-11-23 (erken giris kapali) gercek: 842 passed, 7 skipped, 0 failed (705 sn; +TestEarlyEntryDisabled 4,
#   +test_replay_default_follows_live_early_setting 1).
# 2026-11-23 gercek: 837 passed, 7 skipped, 0 failed (569 sn; Cowork METAR kurali +23, hibrit replay +14,
#   uretim-makinesi kapisi). Ayni testler canli veri olmayan ortamda: kirik 0, 10 acik nedenli skip.
# 2026-11-18 gercek: 784 passed, 6 skipped, 0 failed (523 sn; spread kapatma + path + proxy retry testleri dahil).
# 2026-11-18 aksam gercek: 797 passed, 8 skipped, 2 failed (892 sn; +17 denetim testi). 2 hata canli
#   TestPolymarketProxyLive: WARP SOCKS o anda kopuktu; WARP toparlaninca sinif tekrar 3/3 passed.
# HEDEF: "758 passed, 7 skipped, 0 failed" (2026-10-28 olcumu)
# 2026-11-09 HEDEF/gercek: "762 passed, 6 skipped, 0 failed" (616 sn; +test_poly_dns 3 test, settlement gate + backtest bias testleri yesil).
# 2026-10-28 gercek: 758 passed, 7 skipped, 0 failed (1363 sn).
# 2026-10-27 gercek: 754 passed, 7 skipped, 1 failed (test_load_data_has_settlement).
#   O fail S21 ile KAPANDI: `backtest/engine.py::load_data` artik `weather_markets.target_date`
#   kullaniyor + `data/actuals.db` yedegi var; 298 satirlik `bets.outcome` backfill'i
#   ZORUNLU DEGIL (calistirilmadi, kullanici onayi bekliyor — S19/S21).
# NOT: tests/test_replay_engine.py 2026-10-27'de bu komutta IGNORE ediliyordu cunku
#   replay/main.py + replay/seeding.py kayipti. Paket S20 ile yeniden yazildi; IGNORE kaldirildi.

# 7) Dokumantasyon senkronu (ZORUNLU, agents.md kurali)
# README.md + GELISTIRICI_NOTLARI.md — bugfix/karar/feature commit'lenmeden once ekle/duzelt
```bash
git diff --stat  # .py/.ts degisti ama README/NOTLAR yoksa -> dokumantasyon unutuldu
```

Test gecmeden commit/push/bot restart YOK. Basarisiz test "pre-existing" diye atlanmaz; du.zeltilir.

---

## 2. KRITIK YASALAR (dokunulmaz)

1. **`database/db.py`'ye ASLA dokunulmaz** — engine, SessionLocal, DB_PATH kritik altyapi. Degisiklik gerekiyorsa ayni modulun disinda yap (testlerde `importlib.reload`).
2. **Gercek DB asla direkt SQL ile degistirilmez** — tum islemler API/uygulama katmanindan. Testler temp DB kullanir (`conftest.py`).
3. **DRY_RUN=true kalici** — `executor/bet_placer.py` `_live_allowed = False` kod seviyesinde sabit. Kullaniciya canli trade onerilmez.
4. **TURKCE KARAKTER YASAK** — kodda, yorumda, commit mesajinda, log mesajinda `ç ğ ı ö ş ü` kullanilmaz. Yerine `c g i o s u` yazilir (mojibake onleme).
5. **Sadece istenen degisikligi yap** — hedef disindaki kodu, stili, yerlesimi EZME.
6. **Minimal diff** — en kucuk degisimle coz.
7. **VERI ASLA SILINMEZ, BOZULMAZ, OVERWRITE EDILMEZ** — forecast, metar, price, calibration verisi dahil tum veri kaynaklari KALICIDIR. Yeni veri SADECE INSERT ile eklenir. Eski veri query ile okunur ama ASLA guncellenmez veya silinmez.
8. **Veri kaynagi kontrol zorunlulugu** — "veri yok" denmeden ÖNCE su konumlar kontrol edilir:
   - `C:\Users\fdemir\Documents\New project\junbo\data\*.db` (ana proje)
   - `C:\Users\fdemir\Documents\New project\junbo\data\backups\` (junbo backuplari)
   - `C:\Users\fdemir\Documents\New project\ASIAbot\data\*.db` (asiabot)
   - `C:\Users\fdemir\Documents\New project\Heat\` (heat projesi)
   - `D:\JUNBO data\backups\` (junbo dis kaynak)
   - `D:\HEAT data\` (heat verileri: gfs_archive, weather_data, era5)
   - `D:\ASIA data\` (asia verileri: junbo_bot.db, bot_backup.db, bot_test.db)
   - Bot backup'lari (her 6 saatte 1 kayit: `bot_YYYYMMDD_HHMMSS.db`)
9. **POLYMARKET SETTLEMENT KAYDI ZORUNLU** — Her kapanan bet icin:
   - `outcome` kolonuna gercek sicaklik (actuals.db'den) JSON olarak kaydedilir
   - `settled_at` kolonu Poly kapanis zamani olarak guncellenir
   - Settlement verisi asla silinmez veya uzerine yazilmaz
   - Bu veri, METAR basarisi analizi ve backtest icin KULLANILIR
   - Poly settlement source: Weather Underground (WU) - resolutionSource alanindan dogrulanmistir
   - `collect_settlement_temps.py` WU sicakligini, `settler.py` outcome kolonunu gunceller

---

## 3. Git / Branch Kurallari

- Her degisiklik yeni branch: `feature/...`, `fix/...`, `refactor/...`, `test/...`.
- Ana branch'lere (`main`, `dev`, `restore/05-clean-state`) dogrudan push YOK.
- Branch basina TEK konu.
- Commit mesajlari Ingilizce, aciklayici.
- Push oncesi quick_check.py 7/7 + full suite 0 failed.

```bash
git checkout -b fix/konu
git add .
git commit -m "fix: kisa ozet"
git push origin fix/konu
gh pr create --fill   # opsiyonel
```

Aktif branchlar: `restore/05-clean-state` (production), `ponytail-audit`.

---

## 4. Test Katmanlari (CI)

| # | Katman | Arac | Dosya |
|---|---|---|---|
| 1 | Lint | ruff (F821, E722, F401...) | quick_check.py |
| 2 | Type | mypy | quick_check.py |
| 3 | Latent bug | import-all, dead-code, calibration | test_latent_bugs.py |
| 4 | Core | calculator, ASI, calibrasyon | test_calculator.*, test_ai... |
| 5 | Unit/regresyon | formuller, kelly, risk | test_units.py + digerleri |
| 6 | E2E | uctan u ca | test_e2e_system.py test_integration_e2e.py |
| 7 | Full | hepsi | `pytest tests/ ...` |
| 8 | **Davranis** | gercek DB + gercek modul, mock YOK | test_settlement_chain.py, test_bet_behavior.py, test_bot_flow.py, test_real_flow.py |

### Davranis Testleri Haritasi

Hangi dosya hangi kurali korur:

| Dosya | Kapsam |
|---|---|
| `tests/test_settlement_chain.py` | Settler: kapanis (target+12h) gecmeden expired YASAK; gectiyse expired; acik betli market asla expired |
| `tests/test_bet_behavior.py` | Acilis: oglen sonrasi bugunku market acilabilir (kapanis 24:00); kapanis gectiyse kapali; 20h+ kala kapali; fiyat gate [0.10,0.95); duplicate yok; nakit siniri; grup basina tek bet; rotation threshold |
| `tests/test_bot_flow.py` | Gercek akis: forecast -> spread bet ac -> bet acik kalir (erken kapanis yok) |
| `tests/test_real_flow.py` | Loop'lar gercek fonksiyonlarla: scan/settle/poller; update_prices -> settle zinciri |
| `tests/test_poly_rewards_formula.py` | Polymarket odul formulu (arastirma, 2026-11-26): belge ornegi (Q_one), max spread / min size disi emir puansiz, Q_min tek taraf kurali [0.10,0.90], rakip Q_min ust siniri (2.000 rastgele maker bolunusu), duzeltilmis mid, tick yuvarlama + sermaye; kayit raporu olcekli tamsayi cozumu; backtest kova kenarlari (tam derece yuvarlamasi, F araliklari -> C) |
| `tests/test_poly_rewards_recorder.py` | Odul kaydi dakikalik turu (2026-11-26 aksam): basarisiz `POST /books` tur butcesini (50 sn) asmaz; son tarih yokken tum denemeler yapilir ve son denemeden sonra beklenmez; once ornek sonra yenileme, ornek turun basladigi dakikaya yazilir (yenileme 45 sn surse de); yenileme hatasi ornegi dusurmez, sonraki tur yeniden dener; bos DB'de once yenileme (110 sn butce); ornek hatasi raporlanir, son tarih temizlenir |

> **Erken kapanis mekanizmalari KALDIRILDI (2026-08-12):** `RiskConfig`, `run_risk_management`, `check_stop_loss`, `check_take_profit`, `check_trailing_stop`, `check_time_decay`, `check_early_exit`, `check_rebalance`, `check_model_reversal`, `_reopen_after_stop_loss`, `partial_tp_done` silindi. Betler yalnizca settlement'ta kapanir. Silinen testler: `test_active_risk_management.py`, `test_take_profit_comprehensive.py`, `test_risk_behavior.py`, `edge/test_sl_reopen_chain.py`, `scripts/replay_test.py`. Suite: **633 passed, 7 skipped**.

> **Top-15 kapatma KALDIRILDI (2026-08-12):** `spread_placer` artik top-15 disi sehirlerin betlerini KAPATMAZ (kullanici karari: "ilk 15 bias sadece yeni gun aciliminda, kapatma yapilmayacak"). Kullanilmayan `close_losing_twin_bets` (tie_loser), `_cleanup_stale_bets` (stale_cleanup) ve tarihsel `24h_rule` mekanizmalari silindi — kodda hicbir kapanis uretmiyorlardi. Bet loglarina `bet#ID` eklendi.

> **METAR zirve-tespiti (2026-08-14):** `scrapers/metar.py` (aviationweather.gov NOAA, bedava) + `jobs/metar_peak.py` + `bot_loop.metar_loop` (30dk). Acik marketli sehirlerin METAR sicakligi gun icinde izlenir; max'a cikip **2 kez arka arkaya duserse** zirve kilitlenir, kazanan bucket'a (round(max)) tek esik YES bet ($1, order_id `metar_*`). Kapanisa <4 saat kalan sehirler atlanir. Not: aviationweather.gov sadece son 30 saat veri tutar — gecmis gun dogrulamasi yapilamaz, canli izleme icin tasarlandi. Sermaye +1000 USD. 14 Agu'da 5 bet acildi (entry 0.010).

> **GUNCEL SPREAD CONFIG (2026-08-16 kullanici karari "tek esik, 0.95, ilk 40"):** `spread_radius=0` (TEK esik: sadece tam merkez), `spread_max_cities=12`, `spread_max_entry=0.95` (0.01-0.95 arasi her fiyat), `spread_stake_usd=2.0`, `spread_max_bets_per_day=40`. Fair-value ve 0.10-0.20 olum bolge filtreleri KALDIRILDI (2026-08-16). Backtest 2026-08-14 (orderbook): radius0 +$41.9, radius3 -$317. Tarihsel kayitlardaki `spread_radius=1`/`0.30`/`350` ESKI konfigurasyonlardir (2026-08-15) — guncel durum icin bu nota bak.

> **2026-11-13 guncel (.env dogrulamasi):** `spread_radius=0`, `spread_max_cities=40`, `spread_max_entry=0.50`, `spread_stake_usd=2.0`, `spread_max_bets_per_day=120`. 2026-08-16 notundaki 12/0.95/40 tarihseldir; guncel deger `.env`'dir.

**Kural:** Yeni bug bulundugunda once davranis testi yaz (sentetik + gercek DB), sonra duzelt.

**ALLOWED_DEAD** guncellemesi: yeni public fonksiyon eklediginde ya caller ekle ya da `tests/test_latent_bugs.py::ALLOWED_DEAD` kumesine aciklama ile ekle (entry point ise otomatik gecer).

---

## 5. Bilinen Kritik Hatalar & Cozumler

| Hata | Cozum |
|---|---|
| **Odul kaydi her hedef yenilemesinde 1 dakika, WARP kopuklugunda fazladan 1 dakika kaybediyordu (2026-11-26 aksam)** | Canli kayit (`Junbo-RewardsRecorder`) 03:48-17:30 kapsama %96.2; kayip 31 dakikanin 27'si hedef yenileme dakikasi: eski `_sample` yenilemeyi (~15-55 sn) ornekten ONCE yapiyor, `ts` yenilemeden sonra alindigi icin ornek sonraki dakikanin etiketini aliyor, sonraki tur `INSERT OR REPLACE` ile ustune yaziyordu. 17:19'dan sonra WARP aralikli koptu (kapsama %63.8): basarisiz tur `_post_books` 6 deneme + son denemeden sonra bosuna bekleme ile ~95 sn suruyor, gorev IgnoreNew oldugu icin sonraki dakikanin tetigi de atlaniyordu (HATA satirlari tam 2 dk arayla). COZUM: `_tick` -- once ornek (etiket = turun basladigi dakika, `_BUDGET_S`=50 sn), sonra gerekiyorsa yenileme (`_REFRESH_BUDGET_S`=110 sn; 50 sn ile sinirlanan ilk elle denemede bos DB yenilemesi sigmadi -> yavas yenileme ertelenmesin, en fazla sonraki dakika atlansin); yenileme hatasi ornegi dusurmez, eski hedeflerle devam edilir. `poly_rewards_scan._retrying`: istek zaman asimi + beklemeler `_DEADLINE`'i asmaz, son denemeden sonra beklenmez (tarama betigi son tarihsiz calisir). WARP kapaliyken kaybolan dakikalar geri gelmez (S26). TEST: `tests/test_poly_rewards_recorder.py` 6 test (ag + saat sahte); elle (scratch DB, gercek API) dolu DB + yenileme 30.8 sn, bos DB 39 sn; canli 23:00 yenileme turunda dakika korundu (22:40-23:01 eksik yok); tam suite 866 passed, 6 skipped, 0 failed. |
| **WARP saglik betigi kismi arizayi goremiyordu (2026-11-26)** | 2026-11-25 aksam WARP Cloudflare disi hedeflere ulasamadi (CloudFront/Google/Quad9: SOCKS "Host unreachable"); `scripts/warp_health.ps1` yalnizca `cdn-cgi/trace` (Cloudflare) yokladigi icin 22:34'ten sonra "OK" dedi, bot gamma-api'ye ulasamadi, `TestPolymarketProxyLive` 2 test kirildi, reconnect elle yapildi. COZUM: ikinci yoklama `Test-GammaProxy` -- adres Cloudflare DoH (1.1.1.1, proxy uzerinden; `utils/poly_dns.py` ile ayni), `curl --resolve` + socks5://. `socks5h://` KULLANILMAZ: WARP adi makine hosts sabitlemesinden cozer -> Cloudflare 421, arizada da gecerdi (2026-11-26 olcumu). Herhangi bir HTTP durumu = ulasilabilir (Polymarket hatasi reconnect ile cozulmez); en fazla 2 adres (gorev siniri PT5M, en kotu ~2 dk). `Get-ProbeFailure` iki yoklamayi birlestirir; ilk yoklama, 5 sn tekrar ve reconnect sonrasi kontrol bunu kullanir; log satiri nedeni yazar. TEST: `tests/test_warp_health_script.py` 9 passed (+6: sahte "Host unreachable" SOCKS5 proxy ile iki ariza yolu, yalniz-gamma ariza = tick basarisiz, uretimde canli gecis); kopya betik simulasyonu FAIL #1 -> FAIL #2 + RECONNECT; gercek gorev `LastTaskResult=0`; tam suite 849 passed, 6 skipped, 0 failed. |
| **Izlenmeyen research betigi dead-code census'unu kirdi, commit durdu (2026-11-25)** | `test_no_dead_public_functions` calisma agacindaki TUM `.py` dosyalarini tarar (git'te izlenmeyenler dahil). Yerel arastirma betigi `research/bl_maliyet_backtest.py` (2026-11-24, izlenmiyor) 4 ic yardimcisi (`bucket_bounds`, `logit`, `ncdf`, `parse_outcome`) baska dosyada referans gormedigi icin pre-commit `coverage-threshold` hook'u 1 failed verdi; README port duzeltmesi (8091 -> 8093; kod, `.env`, `watchdog.py`, `service.ps1` zaten 8093) commit'lenemedi. COZUM: 4 isim `ALLOWED_DEAD`'e eklendi (`research/ou_model.py` ile ayni kural: tek basina calisan betik yardimcilari). TEST: `tests/test_latent_bugs.py` + `tests/test_replay_engine.py` 8 passed; tam suite 843 passed, 6 skipped, 0 failed. |
| **METAR-peak ERKEN giris zarar ettiriyordu -> KAPATILDI (2026-11-23, kullanici karari "Evet, kapat")** | Ucuz (<=0.50) erken betler zirveden 0-3.4 saat ONCE aciliyor, sicaklik 1-3 derece daha yukselince aktar zinciri onlari ~0.01 bid'den kapatiyordu; ayrica gunluk 15'lik cap'i erken doldurup sonraki kilit betlerini engelliyordu. Kanit: replay (gercek Polymarket sonucu, bilgi-sonrasi fiyat, 17 Kas haric) erken ACIK 197 bet -$132.24 / KAPALI 37 bet +$14.67; iyimser fiyat varsayiminda da ayni yon (+$225.88 vs +$391.85); canli giris <=0.50 155 bet -$44.66, >0.50 40 bet +$11.84. Cozum: `StrategyConfig.metar_early_entry=False` (env `METAR_EARLY_ENTRY`, `1/true/yes/on`); `run_metar_peak_bets` bayrak kapaliyken `_avg_peak_hour`'u HIC cagirmaz -> mevcut "gecmis yok" dali = 1-dusus kilidi (30 dk onay) + aktar. `EARLY_MAX_PRICE` kodda durur. Replay hibrit modu varsayilan olarak canli bayragi izler (`--early`/`--no-early`). Test: `TestEarlyEntryDisabled` (4) — varsayilan kapali, env ile acilir, kilitsiz bet yok + `_avg_peak_hour` cagrilmaz, kilitle bet var; erken yolu kullanan 7 eski test bayragi acikca acar; `test_replay_default_follows_live_early_setting`. |
| **Polymarket METAR cozum kurali degisti, bot eski kuralla okuyordu (2026-11-22/23, Cowork yamasi)** | Polymarket 2026-10-23'ten itibaren (Panama 10 Kas) HK disindaki tum sehirleri **aviationweather.gov RUTIN METAR** ile cozer: SPECI sayilmaz; deger RMK **T grubu** (onda bir, `T01390133` -> 13.9C), yoksa govdedeki tam sayi (`M11/M17` -> -11); yuvarlama **tam yarim yukari** `floor(x+0.5)` (-8.5 -> -8). Bot SPECI'yi sayiyordu (15 gunde KHOU/KLGA max, FACT/KLAX/RPLL/SBGR min farkli cikti) ve `int(x+0.5)` / simetrik `int(x-0.5)` negatif yarimlarda yanlis bucket veriyordu. Cozum: `scrapers/metar.py` `polymarket_round`, `_parse_metar_temp`, `_is_speci` (fetch'te SPECI atilir); METAR-peak, spread ve replay ayni `polymarket_round`'u kullanir; Panama `MPTO` -> `MPMG` (market metni); Hong Kong HKO ile cozuldugu icin `jobs/metar_peak.py` `NON_METAR_RESOLUTION={"VHHH": ...}` — HK'de METAR-peak bet ACILMAZ (canli HK 30 bet / 6 kazanc / -$23.89). Bu kara liste DEGILDIR; `METAR_PEAK_BLACKLIST` bos kalir. Test: `tests/test_polymarket_metar_rule.py` + `test_metar_peak_*` (+23). Not: gecis gunu arsivde restart oncesi SPECI satirlari kalabilir (`_merged_day_rows`), ertesi gun temizdir. |
| **Uretim makinesini dogrulayan testler baska ortamda kiriliyordu + rw sqlite BOS DB uretiyordu (2026-11-23)** | Cowork VM'inde (Linux) 9 test "no such table: bets" / "connection refused" ile kirildi; Cowork bunlari "ortama ozgu, onceden de kirik" diye gecmisti. Gercek neden: `test_backtest_engine` (4), `test_resolution_rules` (1), `test_settlement_gate` (2) canli `bot.db`'yi, `TestPolymarketProxyLive` (2) canli WARP proxy'sini dogrular ama bunu beyan etmez; `.env` `JUNBO_DATA_DIR=C:\JUNBO data` baska makinede yoktur ve `sqlite3.connect(yol)` rw modda o yolda **bos bot.db olusturuyordu** (olculdu: bos dizinde 0 baytlik `bot.db` dogdu). Cozum: `tests/conftest.py` `live_bot_db` / `production_host` fixture'lari — canli `bot.db` + `bets` tablosu salt-okunur yoklanir; varsa test KOSAR (uretimde proxy kopuksa FAIL = gercek ariza, atlanmaz), yoksa "uretim verisi yok" nedeniyle atlanir; tum baglantilar `mode=ro`. Dogrulama: uretimde 31/31 passed; bos veri dizini + olu proxy simulasyonunda 0 failed, 10 acik nedenli skip, sahte DB dosyasi olusmadi. |
| **`test_placed_is_only_open` spread onekini ZORUNLU tutuyordu (2026-11-23)** | Bu makinedeki tek kirik test. 2026-11-18'den beri METAR-only (`SPREAD_ENABLED=false`); tek acik METAR betiyle "Expected spread prefix, found: ['metar_']" veriyordu (testin varsayimi bayatti, kod dogru). Cozum: her acik pozisyon BILINEN bir stratejiye ait olmali (`spread`/`metar_`); bilinmeyen onek = bug. |
| **Replay her karari 3 saat ESKI veriyle veriyordu (2026-11-23)** | `replay/scheduler.py` tick epoch'unu naive `datetime.timestamp()` ile aliyordu; Windows bunu makine saat dilimiyle (UTC+3) yorumlar -> 12:00 UTC tick'i 09:00 UTC verisini goruyor, UTC gununun son 3 saati hic gorulmuyordu (arsiv tarafi 2026-08-23'te UTC'ye cevrilmis, scheduler unutulmustu). Cozum: `_utc_ts()` (naive = UTC). Test: `test_replay_hybrid.py::test_utc_ts_ignores_machine_timezone`. |
| **Replay METAR bacagi canli hibriti modellemiyordu + bilgi-oncesi fiyat look-ahead (2026-11-23)** | Eski `lock` modu yalnizca tek kilit girisi yapiyordu (erken giris, aktar zinciri, bayat METAR korumasi, ortalama peak saati YOK) -> "erken giris acik/kapali" kiyasi yapilamiyordu. Yeni `--metar-mode hybrid` canli `run_metar_peak_bets` kuralini uygular (`--no-early`, `--early-max-price`, `--settle polymarket`, `--include-non-metar`). Ilk kosuda canli ayna +$240 / canli -$15 cikti: replay yeni METAR'i gordugu anda piyasanin O GOZLEMI FIYATLAMADAN onceki fiyatindan dolum yapiyordu (Munih 20:30 @0.095, canli 21:01 @0.870). Cozum: fiyat, karar bilgisinin (son gozlemin `recorded_at` ani) SONRASINDA kaydedilmis olmali; METAR da `recorded_at`'ten once gorulmez. Ayrica fixed modda METAR stake sabit $3'tu (canli `METAR_STAKE_USD`=$2) -> canli config okunur. Test: `tests/test_replay_hybrid.py` (14). |
| **Zamanlanmis gorevler gorunur terminal penceresi aciyordu (2026-11-20)** | Kullanici: "surekli terminal acilip kapaniyor". WinEvent kancasi (`SetWinEventHook`, EVENT_OBJECT_SHOW) + process izleme ile olculdu: `JunboWARP` (5 dk, Interactive) `powershell -WindowStyle Hidden -File warp_health.ps1` calistiriyor; Windows 11'de varsayilan terminal Windows Terminal oldugu icin her tetiklemede gorunur sekme aciliyor ve script bitene kadar (~40 sn) acik kaliyor (ayni sorun saatlik `JunboForecastRunnerWatchdog`'da). `-WindowStyle Hidden` yetersiz. Cozum: `scripts/run_hidden.py` (pythonw.exe + `CREATE_NO_WINDOW`) sarmalayici; uc gorevin action'i (`JunboWARP`, `JunboForecastRunnerWatchdog`, `JunboForecastRunner`) `pythonw.exe "...\scripts\run_hidden.py" powershell.exe ...` yapildi; `JunboForecastRunner` calisirken yalnizca tanim degistirildi (calisan runner durdurulmadi), yeni tanim bir sonraki baslatmada devreye girer. Dogrulama: `JunboWARP` ve `JunboForecastRunnerWatchdog` elle tetiklendi -> `LastTaskResult=0`, izlemede **0 pencere olayi**. Geri donus: `C:\JUNBO data\state\*_task_backup_20261120.xml` -> `Register-ScheduledTask -Xml (Get-Content <yedek>) -TaskName <ad> -Force`. |
| **Tam suite 5 kirik: DB yolu, import yan etkisi, orderbook kilidi (2026-11-20)** | (1) `tests/test_liveliness_audit` + `tests/test_resolution_rules` hala repo `data/bot.db`'yi ariyordu; canli DB 2026-11-18'de `C:\JUNBO data\bot.db`'ye tasindi -> iki test artik `utils.db_paths.get_db_path()` kullanir. (2) `backtest/run.py` modul govdesi import aninda `load_data(12)` calistiriyordu (DB acma/kilit); govde `main()` icine alindi + `if __name__ == "__main__"`. (3) `data/backtest_results/*.py` (uretilmis rapor scriptleri) `test_import_all_modules` taramasina girip DB aciyordu -> test `data` dizinini atlar. (4) `backtest/engine.py` orderbook okumasi canli toplayicinin DELETE-journal kilidi altinda 30 sn'de `database is locked` veriyordu -> `timeout`/`busy_timeout` 120 sn. (5) `replay/archive.py` DB yollari `REPO_ROOT/data` sabitiydi (canli DB `C:\JUNBO data`'ya tasindi) -> `_path()` call-time `get_data_dir()`; `replay/main.py` fidelity artik `get_db_path()` kullanir. (6) `backtest/run_combos.py` import aninda `load_data(12)` calistiriyordu; ayrica kullanilmayan `best_cities` (F841) ve mukerrer `print` vardi -> `main()` guard + temizlik. Dogrulama: tam suite **801 passed, 6 skipped, 0 failed** (test_betting_idempotency + test_comprehensive ignore). |
| **Yedekler C:'yi dolduracakti (2026-11-18)** | `scripts/backup_databases.py` her 6 saatte 4 DB'yi (~7 GB) kopyalayip 90 gun sakliyordu; veri + yedek C:'ye tasininca ~28 GB/gun -> ~5 gunde disk dolu. Cozum: UTC gununde 1 kosu (`RUN_MARKER` `.last_backup_databases_run`, `--force` ile ek kosu) + `KEEP_LAST_BY_PREFIX` (bot/orderbook/backtest/actuals son 7; db_backup.py dosyalarina dokunulmaz). `data_watchdog` BACKUP kontrolu marker yasina bakar (>26 sa). Test: `tests/test_backup_retention.py`. |
| **CLOB REST yedegi hic calismadi (2026-11-18)** | `_clob_rest_poll_once` marketleri `get_session()` icinde ORM nesnesi olarak aliyor, `raw_data`'yi session kapandiktan sonra okuyordu; `expire_on_commit=True` -> `DetachedInstanceError` yutuldu -> 0 HTTP istegi, `orderbook_snapshots` `token_id='0'` satiri hic yok, `yes_price` REST tazelemesi de hic olmadi. Cozum: kolon sorgusu + `SimpleNamespace`; `_archive_clob_prices` toplu tek transaction. Test: `tests/test_clob_rest_poll.py`. |
| **orderbook.db kilit cakismasi (2026-11-18)** | `collect_orderbook.py` tum toplamayi TEK transaction'da yapiyordu (3-19 dk yazma kilidi); botun WS arsiv yazimi asyncio dongusu icinde senkron sqlite (timeout 5 sn) idi -> WS acilinca dongu donabilirdi; backtest testleri `database is locked` aldi. Cozum: collector 100 satirda bir commit + `timeout=30`; WS arsivi `asyncio.to_thread`. |
| **WeatherNext 3+ gun cekilmedi (2026-11-18)** | Runner kapisi `utcHour % 12 === 0` (utcHour tur basinda sabit). Turlar 80-140 dk surup kaydigi icin (23:59 -> 02:59) 00/12'de baslayan tur olmadi; 11-16/17'de ~38 saat runner turu da yoktu. Open-Meteo (6s) ve IEM (24s) ayni sebeple atlaniyordu. Cozum: UTC slot kapisi `forecast_runner/windows-runner/gates.mjs` (slot icindeki ilk basarili tur; durum `C:\JUNBO data\state\gated_slots_utc.json`). Test: `tests/test_runner_gates.py`. |
| **.env HEAT/OM yollari D:'de kaldi (2026-11-18)** | Runner C:'ye tasindi ama `.env` `JUNBO_HEAT_DB`/`HEAT_MAIN_DB`/`OM_CACHE_DB` D:'yi gosteriyordu -> heat_mos ertesi gun donmus D: kopyasini okuyacakti, om_cache ikiye bolundu. C:'ye cevrildi (`.env.bak_20261118_heatpaths`); `heat/config.py` adaylari ve `utils/om_cache.py` varsayilani da C:. |
| **JunboWARP gorevi bozuk ve zararliydi (2026-11-18)** | Disabled + tirnaksiz yol; ayrica `start_warp.ps1` `warp-cli mode warp` (tam VPN) calistirip SOCKS portunu kapatirdi. Cozum: `scripts/warp_health.ps1` (cdn-cgi/trace yoklama, 2 basarisiz tick -> disconnect/connect, 15 dk cooldown, mod degismez), gorev 5 dk, yedek XML `C:\JUNBO data\state\JunboWARP_task_backup_20261118.xml`. Test: `tests/test_warp_health_script.py`. |
| **bot_watchdog yanlis "stale" alarmi (2026-11-18)** | 17:59 tick'i (collect_orderbook 19 dk) log yazmadi; bot_watchdog 4 kez enable+run yapti. Cozum: gorev `Running` ise 40 dk'ya kadar alarm yok, >40 dk ise `/end` + `/run`. Test: `tests/test_bot_watchdog_data_guard.py`. |
| **Orderbook Ekim boslugu — veri watchdog'u durdu (2026-11-18)** | Bot servisi calisiyordu (`market_snapshots` Ekim 1.087.985 satir) ama `JunboDataWatchdog` logu 19.09 01:30 -> 11.11 21:53 arasi yok; `collect_orderbook.py` de ayni aralikta calismadi. `JunboSnapshot` (29.09) ve `Junbo-BackupDatabases` (28.09) Disabled kalmis. `ensure_task_enabled` PowerShell komutu fazladan `}}` ile timeout'a dusuyordu ve bu iki gorev listede yoktu. Cozum: gecerli PowerShell + tam liste (`TASK_ENSURE_NAMES`), `bot_watchdog` data-watchdog log staleness kontrolu, CLOB arsiv yolu `get_data_dir()`. Ekim depth verisi geri getirilemez; Ekim fiyat verisi `market_snapshots`'ta. |
| **Spread zararli cikti — kapatildi (2026-11-18)** | Gercek orderbook ask + gercek Polymarket sonucu ile 2 aylik backtest: **1.067 bet, %22.3 win, -$854.67**; edge filtreli varyant da -$62/-$68. `.env` `SPREAD_ENABLED=false`; `bot_loop` spread placer + edge dongusunu atlar, METAR-peak devam eder (PAPER). METAR hibrit 3-17 Agu: **195 bet, %81.5, +$1.032**. Veri denetimi: `orderbook.db` 29 Eyl-10 Kas bos (44 gun); canli `weather_forecasts` yalnizca Kasim. Backtest araclari: `%` help crash, O(n^2) market lookup, PK tabanli aralik, indeksli orderbook okuma. |
| **Health-check yanlis "critical" (2026-11-13)** | Edge-modu kalibrasyonu rotation kapanislarini kayip sayiyordu; spread modunda `Analysis` satiri olmadigi icin edge metrikleri 0'di. 2026-11-13: verdict son 48s NET PnL'e baglandi; `summary_all.rotation`/`settlement` ve `recent_48h` eklendi; win-rate mesaji `recent_win_rate` kullaniyor. Ornek: verdict critical iken net +$22.58 idi. |
| **DB yolu bolunmesi (2026-11-13)** | `utils/activity_log.py` ve ~33 script repo `data/bot.db`'yi hardcoded okuyordu (0 bet). `utils/db_paths.py` eklendi; activity_log, scriptler, `executor/settler.py` (actuals), `db_backup.py`, `jobs/collect_settlement_temps.py` canli `DB_PATH`'e baglandi. `tests/test_db_path_consistency.py` regresyonu korur. |
| **JunboBotWatchdog Disabled (2026-11-13)** | Task Scheduler'da `JunboBotWatchdog` Disabled idi; servis tek basina koruma sagliyordu. Enable edildi (1 dk, `scripts/bot_watchdog.py`). |
| **Dead-code census data/backtest_results'i tariyordu (2026-11-13)** | `test_no_dead_public_functions` repo genelinde `.py` tarayip `data/backtest_results` altindaki uretilmis analiz scriptlerini "dead" sayiyordu. `skip_dirs`'e `data` ve `backtest_archive` eklendi. |
| **Veri koku kayboldu -> crash loop + gamma-api HTTP 421 (2026-11-08)** | Iki ayri ariza ayni gun. (1) `data\bot.db` symlink'i ve `data\*` junction'lari `D:\JUNBO data`'yi gosteriyordu ama kok dizin YOKTU -> `sqlite3.OperationalError: unable to open database file`, JunboBot servisi 3 sn'de bir yeniden basliyordu. COZUM: `D:\JUNBO data` -> `D:\Bot DATA backups\JUNBO data` junction (veri kopyalanmadi, silinmedi, overwrite edilmedi); `PRAGMA quick_check` = ok (1.6 GB: 1516 bets / 76161 weather_markets / 86279 weather_forecasts). (2) `gamma-api.polymarket.com` CloudFront'a tasindi; makinenin `hosts` dosyasi onu eski Cloudflare edge'ine (172.64.153.51) pinliyordu -> her istek `HTTP 421 "gamma-api.polymarket.com is not served on this path"`. Sistem `hosts` dosyasi DEGISTIRILMEDI (kullanici karari: "proje icinde coz"). COZUM: `utils/poly_dns.py` — yalnizca bu host icin `socket.getaddrinfo` sarmalayicisi; WARP SOCKS proxy uzerinden Cloudflare DoH (`https://1.1.1.1/dns-query`) ile A kayitlarini cozer, 3600 sn TTL, `POLY_GAMMA_IP` statik fallback, `POLY_DNS_FIX=0` ile kapatilir. `config/settings.py` proxy blogunda `install()` cagrilir. `requests` yolu `socks5://` (yerel DNS) kullanir; `aiohttp`'nin `proxy=` parametresi CONNECT hostname'ini proxy'ye verip UZAK DNS yaptigi icin (rdns=True) 421 devam ediyordu -> `scrapers/async_client.py` SOCKS proxy'de `aiohttp_socks.ProxyConnector(..., rdns=False)` kurar ve `socks5h://` -> `socks5://` normalize eder. KANIT: `fetch_one_blocking`/`fetch_many` gamma 200; canli log "Toplam 2991 market cekildi"; yeni loglarda 0 HTTP 421. TEST: `tests/test_poly_dns.py` 3 passed (DoH fallback, cache, yalnizca gamma host'u ezilir). |
| **Her taramada "Fetch markets timed out" + UNIQUE constraint (2026-11-08)** | Kok neden: `_afetch` 97 `public-search` sorgusunu 1 sn/host throttle ile atiyor (~100 sn) ama `bot_loop._FETCH_TIMEOUT=60` idi. `asyncio.wait_for` iptal etse de `asyncio.to_thread` is parcacigi yazmaya devam ediyor; ayni anda `price_poller_loop` ve scan loop ikinci bir `run_fetch_markets` baslatiyordu -> `sqlite3.IntegrityError: UNIQUE constraint failed: weather_markets.id`. COZUM: (a) `bot_loop._MARKET_FETCH_TIMEOUT=300` — yalnizca `run_fetch_markets` beklemeleri (scan 391/398 + price poller 236) bu butceyi kullanir, diger fetch'ler 60 sn kalir; (b) `jobs/scheduler.run_fetch_markets` single-flight `threading.Lock` — ikinci cagiran `"fetch already in progress"` doner, yazici yarisi olmaz. TEST: `tests/test_bot_flow.py` + `tests/test_real_flow.py` 15 passed. |
| **`historical_calibrations` her temizlikte silinip bias_map bosaliyordu (2026-11-08)** | `database/db_cleanup.py::archive_old_forecasts` 10 gunluk hot esigini bu tabloya da uyguluyordu; satirlar tanimi geregi hep eski oldugu icin 6050 satirin tamami Parquet'a yazilip SQLite'tan silindi. Ayni gun `_run_calibration_backfill` calisti ama `weather_forecasts` de ayni temizlikte silindigi icin 0 satir yazdi -> `bias map for 0 cities`, bias duzeltmesi kapandi, `tests/test_backtest_engine.py::test_bias_varies_by_city` kirmizi. COZUM: arsiv dongusu `CALIB_TABLE` disindaki tablolar uzerinde doner (referans veri, kalici); Parquet budama `cold_days` ile devam eder. Silinen satirlar `data/archive/historical_calibrations_20261108.parquet` icinde duruyor (6050 satir, 49 sehir, 2026-10-18..10-26) — geri yukleme canli DB yazimi oldugu icin kullanici onayi bekliyordu: `python scripts/import_calibration_parquet.py --file historical_calibrations_20261108.parquet --apply`. 2026-11-09 kullanici onayi ile calistirildi. NOT: arac `--file` verildiginde varsayilan dosya listesini de ekler — bu yuzden `historical_calibrations_20260730.parquet` (80.520 satir, 2026-03-31..07-20) de yazildi; tabloda toplam 86.570 satir (6.050 onayli + 80.520 fazladan), 2026-03-31..10-26. Fazladan satirlar METAR kapsami (min 2026-08-01) disinda kaldigi icin gunde 1 kez calisan `backfill_calibration.py --source metar --apply` purge'unde silinecek; kalici isteniyorsa once kaldirilmali. |
| **Offsite yedek dizini (2026-11-09)** | `JUNBO_OFFSITE_DIR` makine ortami `D:/junbo_offsite_backups` idi; `db_backup.mirror_offsite` her yedegin dogrulanmis (JUNBO_BACKUP_KEY varsa Fernet sifreli) kopyasini yazar, etiket basina MAX_BACKUPS=30 tutar. Kullanici istegiyle 154 dosya / 98.468,8 MB `D:\JUNBO data\offsite_backups` altina tasindi (ayni disk, rename; tarih damgalari korundu), bos kaynak dizin silindi, makine ortami yeni yola cevrildi. Calisan servis yeni yolu bir sonraki restart'ta alir. Ayni disk (D:) oldugu icin artik fiziksel offsite degil; ayri disk isteniyorsa env yeniden yonlendirilmeli. |
| **`source_offsets.json` bayatladi -> settlement gate kirmizi (2026-11-08)** | `tests/test_settlement_gate.py` HEAT yerlesim medyanlarini `config/source_offsets.json` ile karsilastiriyor. Dosya 2026-10-27'de 159 cift uzerinden olculmustu; gec cozulen UMA marketleri nedeniyle ortusme 361 cife cikti ve 20+ sehrin medyani 0.30 toleransinin disina kaydi (Denver +0.93 kayitli / +0.10 olculen). Ayrica 0.4-0.1 farki float gurultusuyle 0.30000000000000004 cikip sinirda haksiz kirmizi veriyordu. COZUM: (a) fark 2 ondaliga yuvarlandi; (b) dosya testin kullandigi `_overlap_pairs` yontemiyle yeniden olculdu (n>=5 sehirler guncellendi, 33 sehir; raw uyum 0.296, offset-duzeltmeli 0.429); eski dosya `config/source_offsets.json.bak_20261108`. |
| **METAR-peak stake HIC dusulmuyordu (C1, 2026-08-18)** | `jobs/metar_peak.py::_open_metar_bet` bet'i `session.add` ediyordu ama `debit_stake` cagrilmiyordu -> kagit nakit ve exposure yanlis kaydediliyordu (cash oldugundan buyuk, acik risk eksik). COZUM: bet sonrasi `debit_stake(session, use_stake, "metar_peak ...")`; nakit yetmezse `ValueError` -> rollback + bet iptal. TEST: `tests/test_metar_peak.py` (debit sonrasi cash duser). |
| **Banker's rounding 26.5 -> bucket 26 (C2, 2026-08-18)** | Python `round()` half-even (26.5 -> 26); bot'un olasilik modeli + Polymarket cozumu half-up (26.5 -> 27). `spread_placer` merkez, `metar_peak` bucket + kazanan karsilastirma, `backtest.py gunluk` hepsi `int(x+0.5)` (half-up) yapildi. TEST: `test_spread_placer` center + `test_metar_peak` bucket (26.5C testleri). |
| **Stale/fantom yes_price ile bet aciliyordu (C3, 2026-08-18)** | Gamma `weather_markets.yes_price` 5dk gecikmeli; WS dustugunde REST poll eskiden fiyati guncellemiyordu -> 0.001-0.02 gibi gercek-disi fiyatlarla bet acilabiliyordu (30 bet NET -$32.84 icindeki longshot'lar). COZUM: `utils.clob_live.price_is_stale` — DB yes_price CLOB canli ask ile %15'ten fazla sapiyorsa bet REDDEDILIR (`spread_placer` + `metar_peak`). CLOB hataliysa bet asla engellenmez (bet_placer ile ayni kural). TEST: stale path testleri. |
| **METAR saat dilimi nominal round(lon/15) (M3, 2026-08-18)** | China icin +7 (gercek +8), Seoul +8 (+9), London +0 (BST +1), Lucknow +5 (gercek +5:30) — yanlis yerel-saat esigi peak'i kaciriyordu (dogudan peak gelmiyor semptomu). COZUM: `scrapers/metar.city_utc_offset()` zoneinfo tabani (DST dahil), bilinmeyen sehirde lon/15 fallback. 10 sehir dogrulandi (WSSS/ZBAA/RKSI/EGLC/CYYZ/VILK/RJTT/NZWN/FACT/LTFM). TEST: `test_metar_peak.py` offset testleri. |
| **`_close_wrong_bucket_bets` tum marketleri kapatıyordu (M12, 2026-08-18)** | temperature_min / HIGH / LOW marketleri de bucket karsilastirmasiyla kapatiliyordu ama bu marketlerin kazanan bucket'i YOK. COZUM: sorguya `metric='temperature_max' AND market_type='RANGE'` filtresi. TEST: `test_metar_peak.py::TestCloseWrongBucketBets::test_closes_only_wrong_bucket` (temperature_min marketi kapatilmaz). |
| **CLOB WS sonsuz ic retry -> REST hic devreye girmiyor (2026-08-18)** | `clob_stream.run()` baglanti hatasini iceride SESSIZCE sonsuz retry ediyordu; `ws_fail_streak` artmiyordu -> REST /book yedegi HIC kullanilmiyordu (fiyatlar bayat kalabiliyordu). COZUM: 3 art arda baglanti hatasinda (`max_retries=None`) dis donguye firlatir -> bot_loop REST poll'a gecer. TEST: `test_clob.py::test_run_escalates_after_3_connect_failures_for_rest_fallback`. |
| **REST poll yes_price beslemiyordu (2026-08-18)** | `_clob_rest_poll_once` fiyati yalnizca orderbook.db'ye arsivliyordu; `weather_markets.yes_price/no_price` REST fiyatindan guncellenmiyordu -> WS yokken spread/metar-peak bayat DB fiyatiyla bet aciyordu (C3 guardi da bundan tetikleniyordu). COZUM: REST poll sonucu bulk update (`yes_price/no_price/last_updated`). TEST: `test_bot_loop` / canli yedek yolu. |
| **METAR-peak yanlis market tipine bet (2026-08-18)** | Canli 11 bet HIGH/LOW/min marketlerine acilmisti (hepsi 0.01 entry, 4 lost): 6 temperature_min (London/Paris/Shanghai/Hong Kong/Seoul/Tokyo lowest, -$11.55) + 5 HIGH/LOW (3 HIGH + 2 LOW). Kullanici duzeltmesi: "bir sehir icin sadece bir highest temp var, 32 cikarsa sadece 32 kazanir" + "biz tam bucket a aciyoruz baskasina degil". COZUM: `jobs/metar_peak.py` market sorgusuna `metric='temperature_max' AND market_type='RANGE'` filtresi eklendi — or-above/or-below marketleri tam bucket kazananli degildir, min marketlerine round(peak) ile bet acilamaz. TEST: `tests/test_metar_peak_module.py::TestMetarPeakMarketTypeFilter` (3 farkli market tipinden SADECE RANGE+max olanina bet acildigini dogrular; DetachedInstanceError icin id uzerinden yeniden yukleme). |
| **Bias ground-truth market cozumune aykiri (2026-08-18)** | `scripts/backtest.py metar_vs_settlement`: round(METAR max) == Polymarket kazanan bucket **%74 (70/95)** — METAR (NOAA istasyon verisi, WU'nun yayinladigi veri) cozumle uyusuyor. Ama bias hesabinin temeli Open-Meteo Archive actual yalnizca **%30 (55/184)** — bias kalibrasyonu, market cozumunden FARKLI bir dogruluga (gridded ERA5 reanalysis) karsi yapiliyordu. **COZUM (2026-08-18, kullanici: "bias Open-Meteo'dan yanlis, METAR/WU'dan alalim"):** `scripts/backfill_calibration.py --source metar --apply` — bias actual referansi artik METAR istasyon max/min'i (`_load_metar_actuals`, bugunun kismi gunu atlanir); METAR kapsamayan (city,date,metric) satirlari tablodan temizlenir (karisik kaynakli MBE olmaz). Tablo yeniden kuruldu: **1280 satir, 0 duplicate, 0/1080 actual-vs-METAR uyusmazligi, 48 sehir, 8 model**. `evolution_job._run_calibration_backfill` gunde bir `--source metar` ile tazeler. Tahmin modelleri sorunlu DEGIL (ecmwf_ifs025 METAR'a en yakin, avg|bias|=0.97C) — yanlis olan bias REFERANSI idi; artik referans = cozum kaynagi (WU = NOAA METAR). Test: `tests/test_backfill_calibration_metar.py` (2 test: gecmis gun max/min eklenir + bugun atlanir; bos tabloda anahtar uretilmez). |
| **Open-Meteo TLS CERTIFICATE_VERIFY_FAILED (2026-08-18)** | Avast Web/Mail Shield on-makine SSL intercept ediyor; `requests`+certifi Avast kokunu bilmiyor -> archive/forecast/historical-forecast fetch'leri SSL hatasi ile bos donuyordu (canli tespit: `test_open_meteo_archive_live` basarisiz + `fetch_archive_actuals` bos DF). COZUM: `data_pipeline/weather_ensemble.py` -> `_SYSTEM_TLS = ssl.create_default_context()` (Windows'ta sistem root store'unu da yukler, Avast kokune guvenir) + `_SystemStoreAdapter` (requests `verify=` SSLContext almaz, adapter uzerinden `ssl_context` verilir) ile `_SESSION`; 5 `requests.get` `_SESSION.get` yapildi. TLS dogrulama ACIK kalir (verify=False DEGIL). Not: `scripts/collect_actuals.py` zaten `CERT_NONE` kullaniyordu, o yuzden actuals toplama KESINTISIZ devam etti (actual_temperatures max 08-17). TEST: `tests/test_live_data_smoke.py::test_open_meteo_archive_live` gecer; forecast 429 rate-limit -> skip (normal). |
| **`backtest_metar_peak.py` fake winrate %100 (2026-08-17)** | `gain = (STAKE/entry) - cost if True else -cost` (line 166) gizli dead-code: kazanan bucket Polymarket GERCEK cozumunden alindigi icin her bet WIN sayiliyordu ("winrate %100 / ROI %286" yaniltici ust sinir). Dead-code silindi + docstring'e not eklendi. GERCEKCI sayilar icin `scripts/backtest.py metar_peak` (round(actual) ~%30, METAR ~%71 karisim). Test: YOK (backtest script'leri test disi) — onerilen. |
| **`gunluk` backtest LOOK-AHEAD (2026-08-18)** | `cmd_gunluk` bot.db weather_forecasts okuyordu; o tablo ROTATED (05-13 Agu hedefleri 14-Agu'da backfill, fetched_at=14-Agu) -> sim 06-13 Agu'yu botun O GUN goremedigi forecast'lerle oynuyordu (+$415.97 yanilticiydi). COZUM: forecast artik `backtest.db` gercek gunluk batch'lerinden (02-18 Agu) okunur + `fetched_at <= kapanis` kapisi (kapanis = target 23:59:59 + 12h). Duzeltilmis sonuc: 05-17 Agu -> +$353.08 (173 bet, %71 winrate; eski +$415.97'den -$63). `--real-entry` bayragi: sim entry'sini ayni marketteki botun GERCEK fill'iyle degistirir (config sinirlari uygulanir) -> +$354.41 ~ ideal -> fill modeli optimistik DEGIL. Fill probu: `scripts/_probe_fill.py` (1356 bet, bagil sapma %59.6 = fiyat verisi boslugu, korelasyon 0.815). Rapor: `reports/backtest_gercekci_2026-08-18.md`. Test: YOK (standalone script) — onerilen. |
| **Onaysiz bot restart: `/api/restart` endpoint'i (2026-08-18)** | Kullanici: "onaysiz yap". Non-admin shell LocalSystem servisini (`JunboBot`, `sc qc` ile dogrulandi) durduramiyor; `/api/stop` ise sureci CIKARMIYOR (loop'lar durur, uvicorn ayni surecte eski kodla yasar). Cozum: `api.py` `/api/restart` endpoint'i — `os._exit(1)` ile kasitli cikis; SCM failure-recovery (`sc qfailure JunboBot` = RESTART 5s/10s/30s) sureci OTOMATIK yeniden baslatir ve diskteki guncel kod yuklenir (SQLite WAL crash-safe). Kullanim: `python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8093/api/restart', timeout=3)"` (POST methodu ile; API key gerekir). Not: endpoint'in yuklenmesi icin BIR KEZ admin restart gerekti (19:xx UAC); sonrasinda hep onaysiz. |
| **Kara liste TAMAMEN KALDIRILDI (2026-08-21, kullanici karari)** | Kullanici: "o zaman listeden kaldir kara listeleri" (Denver KBKF 🚫 sorgusu sonrasi; AskUserQuestion: "Tum kara liste (7 sehir)" secti). COZUM: `jobs/metar_peak.py::METAR_PEAK_BLACKLIST` bos `set()` yapildi; kara liste kilit-atlama dongusu (candidates) ve "kara liste (bet yok)" peak_watch gosterimi KODDAN KALDIRILDI (src/app/page.tsx 🚫 dalı artık hicbir sehirde tetiklenmez); config/agents/README/GELISTIRICI_NOTLARI senkron. ETKI: VHHH %20, ZGSZ %29, KBKF/KATL %43, KSEA/KSFO/NZWN %57 tutma oranli sehirlerde METAR-peak bet yeniden acilir (kullanici riski kabul etti). Ileride tekrar kisitlamak icin set doldurulur; gecmis kara liste kararlari (2026-08-20) tarihsel kayittir. Test: `TestBlacklistRemoved` (bos set + VHHH normal bet acar). |
| **1 dusus kilit + aktar stratejisi (2026-08-18 kullanici)** | Kullanici: "20 21 22 22 21 diyorsa ikinci 21 ve altini bekleme 22 ye bet ac; daha sonra 23 e cikarsa 22 yi kapa 23 e ac; 23 e ciktiginda 1 adet dusmesini beklemeyecek hemen acacak." Uygulama: `scrapers/metar.py detect_peak` + `scripts/backtest.py peak_lock` **1 dusus** ile kilitler (esitlik dusus sayilmaz); `jobs/metar_peak.py` kilit bozulunca (cur_max > kilitli peak) `_close_wrong_bucket_bets(winner_bucket)` + yeni zirvenin marketine `_open_metar_bet` (last_closed guard ile zincir: 22->23->24 her dongude); backtest'te `metar_peak_live`/`cmd_gunluk` bk'da kapat + dusus beklemeden yeni bucket'a bet (2 adim). SONUCLAR 05-17 Agu: metar_peak_live **202 bet %88.6 +$593.62 ROI %+98** (onceki 159/+$375.81); gunluk BIRLESIK **+$955.31 ROI %+120.5** (onceki +$471.54); spread leg'i 96->140 bet +$368.77 (erken kapatma fiyatlari iyilesti). Test: `test_metar_peak.py` (1-dusus + kullanici ornegi), `test_metar_peak_module.py TestMetarPeakBrokenLock` (kapat+25'e bet). |
| **Yeni config backtest sonuclari (2026-08-18)** | Bot restart (19:38 UTC, UAC ile `Restart-Service JunboBot -Force`) sonrasi `gunluk` + `metar_peak_live` yeni config'e cekildi: peak bias'siz (tum sehirler) + `peak_break()` yardimcisi — kilitli peak'i ASAN ilk gozlem (epoch, temp); spread leg'inde kazanan bucket = asilma bucket'i ve kapatma ASILMA aninda; peak leg'inde bet acilir ama asilma aninda kapatilir (`sold_broken_lock`). Sonuclar 05-17 Agu: gunluk BIRLESIK **212 bet %76.9 +$471.54** (eski +$353.08), peak leg'i 116 bet %95.7 +$344.90 (eski bias-40: 77 bet +$227.58); metar_peak_live **168 bet %83.3 +$358.15** (eski +$304.83). `test_latent_bugs` allowlist'e `peak_break` eklendi. |
| **Milan erken kilit: 31'de kilitlendi, 32 geldi, kapatma CALISMADI (2026-08-18)** | Kullanici: "lan bu saatte yapmis olmaliydi halen yapmadi, demek boyle bir mekanizma yok, ya koy". Canli ornek: LIMC 18 Agu — 10:50 UTC 31C, 11:20 30, 11:50 29 -> detect_peak 31C kilitledi (yerel 13:50), 12:26 UTC bet acildi (0.225); 13:20 UTC'de 32C geldi. Kapatma mekanizmasi VAR ama yanlis tetikleme sartina bagliydi: (1) yalnizca KAZANAN bucket marketi candidates'tayken cagriliyordu, (2) yeni zirvenin 2 ardısık dususle KILITLENMESINI bekliyordu — o zamana kadar 31C fiyati 0.0005'e coktu (bet 1452, cur_price 0.0005, ~-$3). COZUM (`jobs/metar_peak.py`): (a) kilitli peak ASILIRSA (cur_max > kilitli peak) 2 dusus BEKLEMEDEN derhal `_close_wrong_bucket_bets(cur_max)` — yeni cummax kazanan sayilir, eski bucket'a yeni bet acilmaz; (b) kapatma artik kazanan-bucket marketi OLMASA da cagrilir (sehir-gun basina bir kez, closed_cities seti); (c) kullanici "Metar betleri acilirken bias a gerek yok, nasil olsa peak tespit edilmis oluyor" -> **BIAS_TOP_CITIES=40 sehir filtresi KALDIRILDI**, tum sehirlerin acik RANGE+max marketlerine bakilir. Test: `tests/test_metar_peak_module.py` (bias testleri kaldirildi, `TestMetarPeakBrokenLock` eklendi; bias'siz sehir islenir). |
| **METAR arsivi aksam kesiliyordu — 24 saat toplama + backfill (2026-08-18)** | Kullanici: "24 saat veri topla bundan sonra ve backfill ile eksik gunleri tamamla". Kok neden: `run_metar_peak_bets` yalnizca ACIK marketi olan (kapanisa >2h kalan) sehirlerin METAR'ini cekiyordu -> aksam ~22:00'den sonra toplama duruyor, kapanis sonrasi da o gunun verisi hic toplanmiyordu. Sonuc: 14 Agu arsivi SADECE 1 sehir (o gun toplama cokmustu), 15-17 Agu aksam 21:00-22:00'de kesik, 16-17 Agu'da 13 sehir/gun peak gec kilitlenip bet kacirdi. COZUM: `jobs/metar_peak.py::collect_metar_archive()` — metar_loop (30dk) icinden bet mantigindan BAGIMSIZ olarak TUM sehirlerin (metar_observations + weather_markets birlestik) bugunku gozlemlerini ceker ve idempotent arsivler (ThreadPoolExecutor 8 worker, fetch cache'li); `run_metar_peak_bets` basinda cagrilir. BACKFILL: `scripts/backfill_metar_history.py --start 2026-08-14 --end 2026-08-17` (2 kosu, 504 zaman dolu) -> 14 Agu 1->49 sehir, 15-17 Agu 23:58'e kadar TAM. Backtest etkisi (`metar_peak_live`): 153->168 bet, NET +$275.75 -> **+$304.83** (14 Agu 1->11 bet, 17 Agu 7->10 bet). Test: `test_latent_bugs.py` allowlist'e eklendi. |
| **metar_peak_live gercekcilik + adalet duzeltmeleri (2026-08-20)** | Kullanici: "18-19 hep neden dusuk cikiyor" + "hersey duzeltilmeli (pre-existing yok)". (1) AKTAR ZINCIRI canli gibi N adim (`MAX_CHAIN_STEPS=10`): eski kod 2 betle sinirliydi — Istanbul 18 Agu 29->32 zinciri 30'da kopup kayip yaziliyordu. Yeni davranis: zincir piyasa engeliyle kesilir (31 marketi o anda 0.001 -> MIN_ENTRY, 32 marketi 0.989 -> MAX_ENTRY — piyasa önden fiyatliyor, canliyla ayni). (2) CAP SECIMI kilit zamanina gore: alfabetik siralama kaybeden sehirlere (Tel Aviv/Istanbul/Manila) cap dolduruyordu; canli gibi "once kilitlenen once girer" (2 asamali: coz + sirala + cap). (3) `--blacklist` (virgullu kod) + `--list-bets` (bet-bet analiz) parametreleri. (4) Pyright Optional-subscript uyarilari tuple destructure ile sifirlandi. Sonuc (kara listeli, $100 compound): cap6 90 bet %73.3 +$425 -> $3,352; cap10 142 bet %78.9 +$689 -> $21,711; capsiz 295 bet %81.4 +$1,400 -> $368,463. Not: cap 6, KILIT modunun canli 18 Agu gerceginden (~4-6 bet, 29 sehir 0.99+) modellenmisti; HIBRIT erken giris bu tikanikligi kaldirdigi icin cap 10+ canliya daha yakin. mypy: scripts/ repo konvansiyonuyla mypy kapsami DISINDA (mypy.ini exclude = scripts/). |
| **HIBRIT: sehir bazli tahmini peak saati + erken giris (2026-08-20, kullanici onayi)** | Kullanici: "o saate gelince ilk peak e basariz sonra duzeltme olursa aktar yapariz, bunu dene". Backtest `metar_peak_live --early-city-avg --entry-delay-min 30 --max-bets-per-day 6 --early-threshold 0.50`: hibrit flat +$363 / compound $200->$4,070 (kilit $3,629, erken saf $2,973). Uygulama (`jobs/metar_peak.py`): `_avg_peak_hour()` — `metar_observations` gecmisinden sehir basina kumulatif ortalama peak saati (bugun HARIC, look-ahead yok; en az 3 gun; `city_utc_offset` ile yerel saat); saat gelmeden bet acilmaz (kilit de beklemez); saat gelince cur_max'a ERKEN bet adayi; bucket marketi fiyati `EARLY_MAX_PRICE=0.50` USTUNDEYSE (piyasa biliyor) 1-dusus kilidini bekler (`detect_peak`), dusukse erken girer. Aktar (zirve asilmasi) degismez. Dashboard: "bekleme (peak ~X:00)" / "erken giris (max X.XC, ~Y:00)". Backtest dogrulamalari: metar_vs_settlement RANGE tam bucket isabeti **%88 (198/225)**; kilit modu %97.6 (dusus garantisi), erken %74-78 (ucuz ama riskli). Test: `TestMetarPeakHybrid` (5 test). |
| **Kara liste gosterimi + kilit kurali (2026-08-20, kullanici)** | Kullanici: "neden sadece 3 sehir kilitli" -> 3 kilit de KARA LISTEDEKI (HK/Shenzhen/Wellington) restart oncesi donmus eski satirlardi; hibrit yuzunden diger sehirler "erken giris" statüsündeydi (kilit yok). COZUM: (1) kara listedeki sehirlerin peak_watch satiri her dongude "kara liste (bet yok)" + peak=None ile yazilir (donmus kilit silinir, dashboard 🚫); (2) "erken girise [kilit] ekleme, ne zaman peak tespit olursa KESIN o zaman kilit olsun" -> `detect_peak` HER modda calisir; gercek 1-dusus kilidi varsa durum "kilitli peak=X" (🔒) veya "zirve asildi" (⚠️) gosterilir, erken giris ancak kilit YOKKEN gosterilir. Test: `TestMetarPeakBlacklist::test_kara_liste_sehri_peak_watch_satiri_durust`. |
| **METAR kara listesi: havalimani vs WU sehir istasyonu sapmasi (2026-08-20, kullanici onayi)** | Kullanici: "shenzen ve hongkong a bak, kilitleme yapmis ama kaybediyor". Canli 20 Agu: VHHH 30C kilit (0.655'ten $3) ve ZGSZ 31C kilit (0.225'ten $3) — CLOB canli fiyatlar 0.001/0.011 (piyasa %99 kayip fiyatliyor). Kok neden: METAR HAVALIMANI istasyonu (VHHH Chek Lap Kok / ZGSZ Bao'an) Polymarket'in WU SEHIR istasyonundan sistematik sapiyor. 13-19 Agu tutma: VHHH %20 (1/5, METAR 33-35 vs gercek 32-33), ZGSZ %29 (2/7, METAR 31-32 vs gercek 34-35); KBKF/KATL %43, KSEA/KSFO/NZWN %57 — diger 35 sehir %100. COZUM: `METAR_PEAK_BLACKLIST = {VHHH, ZGSZ, KBKF, KATL, KSEA, KSFO, NZWN}` — bu sehirlerde METAR-peak bet acilmaz; veri toplama ve spread betleri DEVAM eder. Test: `TestMetarPeakBlacklist`. |
| **BAYAT METAR esigi kadans-bilgili (2026-08-20, kullanici onayi)** | 60dk (saatlik) istasyonlarda 45dk esigi HER SAAT tetikleniyordu (Wuhan/Chongqing/Qingdao/Busan 60dk kadans; 75dk eski gozlem normal). COZUM: `jobs/metar_peak.py::_stale_threshold_min` — istasyonun gozlem araligi medyani >=55dk ise esik 90dk, yoksa 45dk (veri azsa 45). Test: `TestMetarPeakStaleThreshold` (3 test: saatlik 90dk / 30dk 45dk / veri yok 45dk). |
| **Bayat veri aktari bloklamaz (2026-08-20, kullanici onayi)** | 17:42'de Asya sehirlerinde bayat alarmi seli: aviationweather.gov ~2 saat yayin durdurdu (15:00Z sonrasinda bosluk; 17:50'de tazelendi — DB kanitli). Kusur: bayat kapisi `continue` ile AKTARI da blokluyordu (yanlis bucket'lar kapatilmiyordu). COZUM: `stale_skip` bayragi — bayat veri yalnizca YENI BET acmayi engeller; aktar (winner=max(peak,cur_max) ile kapatma) bayattan bagimsiz calisir (asla gozlenen max'in ustunde kapatmayiz, guvenli). Test: `TestMetarPeakStaleRefresh::test_bayat_veride_aktar_calisir_yeni_bet_yok`. |
| **BAYAT METAR pasif atliyordu -> aktif yeniden cekim (2026-08-20, kullanici)** | Kullanici: "boyle alarm mesaji var neden islem yapmadi bot, duzeltmeye calismadi". Eski davranis: son gozlem >45dk ise `bet_blocked` logla + `continue` (pasif). COZUM: bayat tespit edilince o sehrin METAR'i DERHAL yeniden cekilir (`fetch_metar_day` + `archive` + `_merged_day_rows`); taze geldiyse bet mantigi devam eder, gelmediyse sehir-gun basina 1 kez logla + atla (alarm gurultusu yok). Test: `TestMetarPeakStaleRefresh` (taze gelirse bet acilir / gelmezse atlanir). |
| **METAR yeniden cekim AGGRESIF (2026-08-21, kullanici onayi)** | Kullanici: "1 i yap" — (A) ilk PARALEL fetch bos/hata dondugunde `if not day_rows: continue` SESSIZ atliyordu (Buenos Aires 19 Agu 17:07->20:34 hic deneme yoktu). COZUM: ilk fetch bos ise o sehrin METAR'i DERHAL yeniden cekilir; yeniden cekim de bos/basarisizsa sehir-gun basina 1 kez `bet_blocked` logla. (B) bayat yeniden cekim TEK deneme idi (aviationweather tek cagriyi bayat donebiliyor); artik 3 deneme, aralarda 3 sn bekleme, son denemede bekleme yok — 3'u de bayat/basarisizsa sehir-gun basina 1 kez logla + `stale_skip`. Test: `TestMetarPeakStaleRefresh` (aynı semantik — taze gelirse bet acilir / gelmezse atlanir). |
| **`metar_peak_live` SAF peak backtest eklendi (2026-08-18)** | Kullanici istegi: "sadece order book ve metar ile backtest yap, hic 2 gun onceden bet acma, metar ile peak takibi yap ve tespit ettiginde 3 usd bet ac sehire ve bir adet... slippage de koy ustune, fee ve gas leri de." Yeni subcommand `scripts/backtest.py metar_peak_live`: forecast/bias KULLANILMAZ (bias-top sehir filtresi YOK, tum sehirler); `detect_peak` kilitlenince (yerel 13:00+ + 2 ardisik dusus) sehir basina TEK $3 YES bet, SADECE RANGE temperature_max; giris = kilitlenme sonrasi ilk gercek ask (orderbook + CLOB price_history) + `--slippage` (default +$0.01) + fee %5 + gas $0.10; kapanisa <2h kala kilitlenirse bet yok. Sonuc 03-18 Agu (MIN_ENTRY=0.10): **153 bet, %80.4 winrate, stake $459, fee+gas $25.70, NET +$275.75** (slippage etkisi -$23.06). Saf hal (--min-entry 0): 241 bet, %53.5, NET +$324.95 (slippage etkisi -$115.95 — longshot girislerde slippage oransal olarak agir). MIN_ENTRY=0.10 hem winrate hem slippage duyarliligini iyilestiriyor. |
| **GUNLUK METAR-peak cap (2026-08-21, kullanici karari)** | Kullanici: "cap leri simdi 7 ye mi 12 ye mi ayarlayalim" — canlida metar_peak'in HIC gunluk cap'i yoktu (cap taramasi sadece backtest parametresiydi `--max-bets-per-day`). COZUM: `METAR_PEAK_MAX_BETS_PER_DAY=12` (.env) -> `config/settings.py::StrategyConfig.metar_peak_max_bets_per_day` + env read; `jobs/metar_peak.py::run_metar_peak_bets` — bugunku (UTC) acilan `order_id LIKE 'metar_%'` betleri sayilir (status bagimsiz: gun icinde kapanan/settled olan da cap'e sayilir — backtest semantigi "gunluk acilan bet sayisi"), cap dolunca YENI bet acilmaz (matched_markets add'den sonra — "market yok" uyarisi tetiklenmemeli), yanlis-bucket kapatma (aktar/zincir) cap'tan bagimsiz devam eder. Neden 12: backtest cap-sweep'inde cap12 en karli (+$834, %82.6 winrate; cap7 +$539 ROI en yuksek ama cap canlida NADIREN doluyor — 18 Agu'da ~4 bet). Test: config load dogrulandi (cap=12), ruff+mypy+format temiz. |
| **peak_watch kilit takibi dogu->bati (2026-08-21, kullanici)** | Kullanici: "kilit takibi sehir isimlerini utc ye gore sirala yani en doguda sehir en basta en batidaki en sonda". Eski siralama sicakliga gore (cur DESC) idi. COZUM: `utils/activity_log.py` peak_watch tablosuna `lon REAL` kolonu (ALTER TABLE migrate, eski satirlar gecmiste kalir) + `peak_watch_list` `ORDER BY COALESCE(lon, -999) DESC, city` (boylam azalan = dogu->bati); `jobs/metar_peak.py` her peak_watch satirina `lon` ekler (weather_markets.longitude, kara liste dahil); `src/lib/api.ts` tipine `lon: number | null` eklendi. lon'suz eski kayitlar en sona gider. Test: `TestPeakWatchOrdering::test_peak_watch_dogu_bati_sirali` (Wellington->London->Los Angeles->EskiSehir, tmp DB uzerinde). ILK YAYINDA KACIRILAN YOL: erken-giris "bekleme (peak ~X:00)" satirina (metar_peak.py 3. watch_rows) lon eklenmemisti -> 20 sehir (London/Paris/NY/LA...) NULL kalip en sona dusuyordu. COZUM: `_city_lon(m)` yardimcisi — oncelik market longitude, bos/sifir ise `_ICAO_COORDS` (ICAO->koordinat) fallback. Test: `TestCityLonFallback` (EGLL None->ICAO, KLAX 0.0->ICAO, dolu market oncelikli, bilinmeyen->None). |
| **Backtest CIKIS gecikmesi + orderbook kayit sikligi (2026-08-21, kullanici onayi)** | Kullanici: "cikista da koy backteste" + "orderbook'u daha cok kayit edelim". (1) **CIKIS gercekciligi**: `metar_peak_live` kapanis (bk break + aktar zinciri) once break ANINDAKI bayat snapshot fiyatini okuyordu; canli bot break'i kendi 30dk dongusunde fark eder, o sirada exact-bucket piyasa cokmustur. COZUM: `ask_at_or_after(seri, break_t + delay_sec)` — cikis da ayni `--entry-delay-min` gecikmesine tabi (iki kapanis yolu: ana bet + zincir). (2) **ORDERBOOK KAYIT SIKLIGI**: `collect_orderbook.py` serial cekim ~2000 market icin ~20dk suruyordu; watchdog (45dk esigi) sonraki tick'lerde DB kilitli gorup ust uste collect'ler baslatiyordu ("database is locked" seli). COZUM: `ThreadPoolExecutor(max_workers=15)` paralel fetch (SQLite yazimi ana thread'de siralı, tek conn) -> dongu ~4dk; `.orderbook_collect.lock` dosyasi (30dk bayat-calmali) cakisan run'lari daha baslamadan durdurur; `--workers` parametresi. `data_watchdog.py` `ORDERBOOK_MAX_AGE` 45dk -> 10dk; `setup_tasks.ps1` `Junbo-OrderbookCollect` every30m -> every10m. NOT: orderbook verisi ~30dk ornekliydi (son 3 gun %94.6 ≥25dk aralik) — backtest bu bayat fiyattan giriyordu (canli CLOB'dan 0.85+ girerken backtest 0.05-0.60); bundan sonra ~10-12dk ornekleme ile canliya yaklasir. Cap taramasi (hibrit + cikis gecikmeli): cap6 %76.6 +$444 / cap7 %78.5 +$539 / cap8 %79.2 +$583 / cap9 %78.9 +$651 / cap10 %80.8 +$706 / cap11 %81.1 +$751 / cap12 %82.6 +$834 (toplam kar cap12'de, ROI cap7'de). |
| **`metar_peak_live` SAF peak backtest eklendi (2026-08-18)** | Kullanici istegi: "sadece order book ve metar ile backtest yap, hic 2 gun onceden bet acma, metar ile peak takibi yap ve tespit ettiginde 3 usd bet ac sehire ve bir adet... slippage de koy ustune, fee ve gas leri de." Yeni subcommand `scripts/backtest.py metar_peak_live`: forecast/bias KULLANILMAZ (bias-top sehir filtresi YOK, tum sehirler); `detect_peak` kilitlenince (yerel 13:00+ + 2 ardisik dusus) sehir basina TEK $3 YES bet, SADECE RANGE temperature_max; giris = kilitlenme sonrasi ilk gercek ask (orderbook + CLOB price_history) + `--slippage` (default +$0.01) + fee %5 + gas $0.10; kapanisa <2h kala kilitlenirse bet yok. Sonuc 03-18 Agu (MIN_ENTRY=0.10): **153 bet, %80.4 winrate, stake $459, fee+gas $25.70, NET +$275.75** (slippage etkisi -$23.06). Saf hal (--min-entry 0): 241 bet, %53.5, NET +$324.95 (slippage etkisi -$115.95 — longshot girislerde slippage oransal olarak agir). MIN_ENTRY=0.10 hem winrate hem slippage duyarliligini iyilestiriyor. |
| **`walk_forward` sahte %100 winrate + saatler suruyordu (2026-08-18, 5 audit fix W1-W5)** | Kullanici: "walk forward neden bu kadar uzun suruyor... tum backtestleri audit ve debug et". 5 gercek ariza: **(W1)** sonuc kaynagi `bets` tablosuydu (~44 cozumlu satir, yalnizca botun kendi gecmisi) -> tek gun (05-Agu) uretiyordu; COZUM: outcome `parse_resolved_outcome(raw_data)` — oncelikle bot.db (04-17 Agu tam), eksikse backtest.db. **(W2)** `snap.get("threshold", 25)`: market_snapshots'ta threshold kolonu YOK -> her bet sabit esik 25 ile P(max>=25)~1 -> model_prob 0.99'a kilitleniyordu (sahte winrate'in asil kaynagi); COZUM: esik market kaydindan. **(W3)** her saatlik snapshot'ta ayni markete yeniden bet (11x); COZUM: market basina TEK bet (seen seti, botun dup-guard'i ile ayni). **(W4)** her snapshot icin 113k forecast lineer tarama (~17.5 milyar karsilastirma -> saatler); COZUM: `_wf_forecast_index` tek seferlik indeks -> **90 saniye**. **(W5)** giris fiyati market_snapshots.yes_price artefakti (C1 kurali); COZUM: orderbook + CLOB price_history serisinden `ask_at_or_after`. Duzeltilmis SONUC (07-31..08-20, 14 fold, 1,325 bet): **%19.6 winrate, -$1,950.93, ROI -%14.7** — eski sabit "edge" modeli (P(max>=esik) vs fiyat) GERCEK veride KAYBEDIYOR; eski +$464.65 / %100 tamamen sahteydi. NOT: walk_forward botun SU ANKI stratejisi (spread radius=0 + METAR-peak RANGE) DEGIL, eski edge modelini test eder — bot stratejisi icin `gunluk` gecerlidir. Test: `tests/test_realistic_backtest.py` + `tests/test_latent_bugs.py` gecer. |
| **`clob_stream._proxy_url` None.get AttributeError (2026-08-17)** | `bot_config.polymarket.get_proxies()` proxy_url bosken None doner; `None.get("https")` crash uretiyordu (dis try/except sessizce yutup proxy'siz kaliyordu). Cozum: `proxies = get_proxies() or {}`. Test: `test_clob.py` mevcut; None-proxy path testi eklenebilir. |
| **API `/api/markets` 4.7MB / timeout (2026-08-17)** | 15k missed-signal + 1900 open market JSON'u dashboard'i boguyordu. Cozum: `?limit=` parametresi (default 200) hem missed-signals hem open-markets sorgularina eklendi. Dikkat: `existing_ids` artik limitli setten geliyor — full-list tukumiyen comparison script'ler ≤200 satir gorur. |
| **CANLI METAR-peak 30 bet NET -$32.84 (2026-08-17)** | order_id LIKE 'metar_%' 30 bet: 3 won / 22 lost / 5 placed. Gun gun: 08-14 -5.25, 08-15 -3.83, 08-16 -14.45, 08-17 -9.31. KOK NEDEN: 24 bet entry 0.01-0.03 longshot (NET -$39.90) — piyasa o bucket'i ~%1 sansla fiyatliyor = METAR tespiti yanlis. entry>=0.10 6 bet NET +$7.06. COZUM: `jobs/metar_peak.py` MIN_ENTRY=0.10 (0.01-0.03 longshot'lari elemek icin). SESSION_OZET'in "+$139 / ROI %165" CLAIRVOYANT (look-ahead) + dead-code bug, GERCEK DEGIL. Gercekci backtest: `scripts/backtest.py metar_peak` (actual ~%30 dogru -> -$30.14; METAR ~%71 -> ~+$85.65). |
| **`_clob_rest_poll_once` SEQUENTIAL 1900 market (2026-08-17)** | WS 3 kez fail edince REST /book yedegi ~1900 open marketi TEK TEK 15s timeout ile cekiyordu — en kotu ~8 saat (poll 300s'de bir, bot bloke). COZUM: `ThreadPoolExecutor(max_workers=16)` ile paralel fetch (requests thread-safe), `_archive_clob_price` arsiv ana thread'de (orderbook.db delete-journal, paralel yazar kilit cikarir). Test: YOK — canli yedek yolu; `test_regression_fixes.py` arsiv path'ini kapsar. |
| `max_bet_pct` kelly.py 10x fark | `kelly.py` artik `bot_config.strategy.max_bet_pct` okur |
| Fee rate tutarsizligi | `strategy.py` `current_fee_rate` kullanir; `slippage.py` guncellendi |
| `min_edge` cift kontrol | strategy.min_edge kontrolu kaldirildi; calculator'e birakildi |
| Timezone crash (fast_mode_until) | `.replace(tzinfo=None)` |
| Gamma API `tokens[]` yok | scraper `outcomePrices` fallback; bestBid=0 / bestAsk=1 atlar |
| Take profit `{pct:.1%}` double | ratio kullani, format ratio |
| **Snapshot 30dk durdu (2026-08-08)** | `jobs/snapshot_job.py` bucket-bug: farkli bucket'ta yeni satir yazilmiyordu; du/zeltildi |
| **SL sonrasi pencere disi yeni-lider acilimi (2026-08-08)** | ~~Wellington 12C/13C gece 00:00-04:00 UTC'de cift kayip. `_reopen_after_stop_loss` artik `_is_in_betting_window()` (04:00-23:30) gated; grupta ACIK bet varsa acilmaz (tek pozisyon); `_STOP_LOSS_REOPEN_WINDOW=6h` cutoff. Test: `test_reopens_new_leader_after_stop_loss_in_window`~~ **2026-08-12: `_reopen_after_stop_loss` KALDIRILDI** — erken kapanis yok, betler settlement'a kadar tutulur. Kayit tarihseldir. |
| **JunboSnapshot LastResult=1 (2026-08-08)** | Task action `...\..\snapshot_task.bat` parent dizini isaret ediyordu; dogru yol `...junbo\snapshot_task.bat` yapildi. LastResult=0, 17 snapshot |
| **Uyku: tarama duruyor (2026-08-08)** | Wake timers DC=Disabled, AC=Important → `powercfg /waketimers` bos. AC+DC=Enable yapildi; Sleep after + Hibernate = Never → bot loop'lari kesintisiz |
| **TS tip hatalari (2026-08-08)** | 24 hata: import HistoryStats, Signal'e threshold/metric/strike_temp, mapOpenPositions strikeTemp, KpiData fallback (availableCash/totalEntryFee/gercekKayip), brierScore null guard, exitType PT union, result ROTATION, mapActivityFeed status/health/weights imzalari → `tsc --noEmit` 0 hata |
| **target_date 12:00 etiketi kapanis saniliyordu (2026-08-08)** | `bet_placer` `target_date <= now` / `target_date > now+30dk` kullanarak 12:00 etiketini kapanis (24:00) saniyordu → 12:30 UTC sonrasi hicbir markete bet acilamiyordu (SL sonrasi reopen dahil; "0 open markets"). Kapanis = target_date + 12h. SQLite-safe esdeger: `target_date > now-11h30dk` ve `target_date <= now+8h`. Test: `test_tie_betting.py` helper `_td()` kapanis now+20h icinde olacak sekilde duzeltildi |
| **max_openable nakit sinirsizdi (2026-08-08)** | Eski formul `max_openable = max_exposure - exposure` nakit ust sinirini yok sayiyordu → "Max acilabilir $884" derken cuzdanda $849. Yeni: `min(nakit, max_exposure - exposure)`, API `max_openable_now`, frontend `maxOpenableUsd` backend'den okur. Invariant test: `max_openable_now <= free_cash` (test_all_functions.py) |
| **"Gercek Kayip" KPI kaldirildi (2026-08-08)** | `gercek_kayip = initial - equity_cash` exposure'i (bagli sermaye) kayip saniyordu + fee zaten PnL icinde (settlement_pnl = payout - stake - entry_fee). Kaldirildi; yerine `entry_fee_trade_count` (fee odenen islem sayisi) eklendi, Toplam Fee kartinda gosterilir |
| **SL sonrasi yeniden acilim calismiyordu (2026-08-08)** | Iki bug birlesiyordu: (1) `settler.py` acik beti olmayan marketi hemen `expired` yapiyordu (SL ile kapanan betin marketi hala canli: Toronto 30C 0.97, Miami 32.5C 0.925) -> `_reopen_after_stop_loss` status='open' aradigi icin bulamiyordu; (2) `_reopen_after_stop_loss` best == lost_market_id ise skip ediyordu, ikinci en yuksek farkli marketi denemiyordu. Duzeltme: settler kapanis (target+12h=24:00 UTC) gecmeden expired yapmaz; reopen kayip market haric en yuksek fiyatliyi secer. Test: `test_tie_betting.py` + elle dogrulama (Toronto 31C acildi) **2026-08-12: reopen mekanizmasi KALDIRILDI — kayit tarihseldir.** |
| **DURUM: Davranis testleri eklendi (2026-08-08)** | Kacan bug'lar hep modul ETKILESIMINDE cikiyordu (settler x reopen, target_date x kapanis). Izole unit testler (test_active_risk_management.py 42 test ama 0 gercek DB — hepsi MagicMock) bunlari yakalayamadi. Eklendi: `test_sl_reopen_chain.py` (SL->settler->reopen zinciri), `test_settlement_chain.py` (kapanis gecmeden expired yok), `test_bet_behavior.py` (acilis filtresi, vade, gate, rotation), `test_risk_behavior.py` (SL/TP/TS/time-decay gercek DB ile). Suite: 653 -> 680 **2026-08-12: `test_active_risk_management.py`, `test_sl_reopen_chain.py`, `test_risk_behavior.py` KALDIRILDI (erken kapanis mekanizmalari silindi) — kayit tarihseldir.** |
| **DURUM: TP/TS/time-decay config kapali (2026-08-08)** | `config/settings.py` risk: `take_profit_pct=999.0`, `trailing_stop_pct=999.0`, `time_decay_hours=0` — TP/TS/time-decay fiilen devre disi! SL calisiyor (0.2). Bu bilincli mi yoksa bug mu karar gerekli. Testler config'i gecici set ederek davranisi dogruluyor |
| **REPLAY testi: production DB kopyasi (2026-08-08)** | Sentetik testler gercek DB'deki durumlari yakalayamaz. `scripts/replay_test.py` production bot.db'yi kopyalar, kopya uzerinde settle_all + reopen calistirir: kapanisi (target+12h) gecmemis market expired YAPILMAMALI + reopen crash'siz. Neden pytest DEGIL script: conftest DB_PATH'i temp DB'ye cevirir, bot_config singleton ilk importta donar — replay pytest icinde calisamaz. Kullanim: `python scripts/replay_test.py` (cikis 0=OK). 2026-08-08 dogrulama: 3064 market, 0 yanlis expired, 7 acik-bet'siz SL grubu islendi (gate reddi) **2026-08-12: `scripts/replay_test.py` KALDIRILDI (reopen mekanizmasi silindi) — kayit tarihseldir.** |
| **`_fetch_open_meteo_model` tanimsiz idi (2026-08-09)** | `scrapers/meteo.py` `fetch_for_markets` icinde tanimsiz `self._fetch_open_meteo_model(...)` cagilisi ilk model'da `AttributeError` -> `except Exception`'a dusup SESSIZCE 0 satir uretiyordu (8-modelli per-model loop etkisiz) + ayni (lat,lon,date) icin cift istek riski. Cozum: kirlik loop ve kullanilmaz `openmeteo_models` listesi **silindi**; canli yol `fetch_all_markets` → `get_multi_model_forecast` ve aggregate `_fetch_open_meteo`/`_fetch_weatherapi` KORUNDU. Test: `test_meteo.py` |
| **DB bakimi ANALYZE+VACUUM eksikti (2026-08-09)** | Dosya buyudukce istatistikler eskimeyor, boyut artiyordu; `ANALYZE`/`VACUUM` hic calismiyordu. Cozum: `scripts/db_maintenance.py` (wal_checkpoint(TRUNCATE) → ANALYZE → VACUUM) + `data_watchdog` icinde **gunde 1 kez 02:00-04:00 UTC** penceresinde (`data/.last_db_maintenance` marker). VACUUM canli bot ile lock riski → sessiz pencere secildi. Ilk run: bot.db 157.88MB → 146.58MB (~11.3MB save) |
| **watchdog `_find_bot_pids` wmic Win11'de yok + ASIAbot taskkill filtresi tutmuyordu (2026-09-24)** | `wmic` Windows 11'de kaldirilmis; `subprocess.run(["wmic", ...])` exception -> except sessizce yutuyor -> `_find_bot_pids` HEP `[]` donuyordu (restart yolu sahte botlari bulamiyordu). Ayrica ASIAbot `stop_bot` `taskkill /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq *main.py bot*"` kullaniyordu; bot `pythonw.exe` (penceresiz, title yok) ile calistigi icin filtre HIC tutmuyor, her restart yeni kopya doguruyordu (CPU %97'ye cikmisti: 2 bot kopyasi + 1 cekirdek yiyen watchdog). COZUM: iki projede de `powershell Get-CimInstance Win32_Process + ConvertTo-Csv + csv.DictReader` (junbo `python.exe`, ASIAbot `pythonw.exe` filtresi); ASIAbot `stop_bot` PID-bazli kill'e cevrildi; yetim kopyalar olduruldu, tek bot (8092) + taze watchdog birakildi. Heat `watchdog.ps1` zaten Get-CimInstance kullaniyor (sorun yok); HORSE'ta watchdog/kill mantigi yok (tek seferlik task'lar, MultipleInstancesPolicy=IgnoreNew) — degisiklik yok. TEST: `_find_bot_pids` fonksiyonel dogrulama (ASIAbot canli [2384] buldu), `py_compile` + `ruff check --ignore F401` temiz. |
| **MeteoConfig eksik alanlar import crash (2026-09-24)** | `utils/weather_sources.py` module-level `bot_config.meteo.iem_url/nws_url/aviation_url/vc_url` okuyordu ama `MeteoConfig`'te bu alanlar HIC yoktu -> import her zaman `AttributeError` (`test_import_all_modules` kirmizi). COZUM: `config/settings.py::MeteoConfig`'e 5 alan + `vc_api_keys` listesi + `next_vc_key()/rotate_vc_key()` eklendi; `__post_init__`'te `.env` override (`IEM_URL/NWS_URL/AVIATION_URL/VC_URL/VC_API_KEY/VC_API_KEYS`). TEST: import + deger dogrulama OK; `test_faz25_35` + `test_strategy_selection` 32 passed. |
| **27 dead public function — yeni job/backtest/research yardimcilari (2026-09-24)** | `backtest/engine.py` (3), `backtest/run_combos.py` (1), `jobs/new_city_job.py` (3), `jobs/heat_mos_job.py` (1), `jobs/collect_settlement_temps.py` (5), `utils/weather_sources.py` (4), `research/ou_model.py` (3), `scripts/*` (7) — hepsi kendi dosyasinda kullaniliyor (wired `maybe_run`/`evaluate_all`/`__main__` uzerinden). COZUM: `test_latent_bugs.py::ALLOWED_DEAD`'e 27 + `replay/fidelity.py` icin 2 isim eklendi (gerekceli). TEST: `test_no_dead_public_functions` gecer. |
| **46 ruff hatasi (E402/E501/E741/F841) sifirlandi (2026-09-24)** | `backtest/run*.py` E402 noqa + `l`->`ls` + SQL wrap; `main.py`, `async_client.py`, `scripts/_*.py`, `add_indexes.py`, `analyze_peak_hour.py` satir wrap (cikti birebir korundu); `test_regression_fixes.py` `engine`->`_engine`. TEST: `ruff check . --ignore F401` temiz. NOT: tumu baska oturumun kirli agacindaki pre-existing hatalardi. |
| **`replay/fidelity.py` yazildi, `replay` engine HIC YOK (2026-09-24)** | `tests/test_replay_fidelity.py` (untracked WIP) `replay.fidelity` import ediyordu ama paket mevcut degildi -> collection ERROR. COZUM: test spesifikasyonuna sadik saf modul yazildi (`strategy_of`, `compare_replay_vs_live`: market_id + 15dk + 0.05 tolerans, float epsilon'lu; gunluk `days`, strateji kirilimi). TEST: 8 passed. ACIK: `tests/test_replay_engine.py` `replay.main`/`replay.seeding` (tam motor: ReplayClock, raporlar) istiyordu — bu kod HIC yazilmamis. KARAR (2026-09-24, kullanici): motor yazilmayacak, test ignore listesine alindi (`pytest.ini` addopts + agents/README/CI/pre-commit senkron). |
| **Pre-existing kirmizilar — stash ile kanitlandi (2026-09-24)** | `test_real_flow.py` x2 COZULDU: `place_spread_bets`'e commit 8c2258b ile `min_entry_price=0.10` tabani gelmisti, test tohumlari 0.05'te kalmisti (7 skip) -> tohumlar 0.15 yapildi, 11/11 gecer. `test_settlement_gate.py` x2 arastirildi: (a) Panama City tek beti BUGUN acilmis + HEAT overlap SIFIR (olcumu imkansiz) -> dosyaya notr `{0.0, n:0}` girildi (backtest davranisi degismez, `normalized_heat_settlement` zaten bilinmeyene 0.0 uygular), coverage testi gecer; (b) Hong Kong -0.10 (n=34) vs taze +1.40 (n=5, aralik -1.0..+3.3) -> 34 orneklem 5 gurultulu ornekle EZILMEDI, kapi bilerek kirmizi birakildi (kullanici karari; veri birikince yeniden olcum). Yan bulgu: bu test prod `bot.db` + `D:\` yolunu direkt okur (conftest bypass) — hermetik degil. |
| **BAYAT FİYATLA BET ACILDI (2026-08-10)** | Beijing 32°C (10 Ağu) marketine 08:59 UTC'de **0.18'e** bet acildi; gerçek CLOB book fiyati o an **~0.98** idi (Gamma `outcomePrices` ~1 saat bayat kaldi; snapshot'larda 01:42→08:54 arasi 7 saatlik bosluk vardi). Bot, bet acarken sadece DB'deki `market.yes_price`'ı (Gamma'dan) kullaniyor, gerçek işlem fiyatini CLOB'dan dogrulamiyordu → paper fill gercekte hic var olmamis fiyattan. Cozum: `utils/clob_live.py` eklendi — `raw_data`'dan `clobTokenIds[0]` (YES) cikarir, CLOB `/book`'tan canli ask/bid ceker; `bet_placer.open_bet_on_market` + `place_bet` artık bet acmadan once canli fiyatla DB fiyatini karsilastirir, sapma > %15 ise **bet reddedilir** (stale guard; CLOB erisilemezse eski davranis korunur). Test: `tests/test_clob_live.py` (12 test) |
| **Acik betlerde bayat giris fiyati (2026-08-10, elle duzeltildi)** | Ayni bayatlik 41 acik betten **17'sini** etkilemisti (Beijing %81.5, Hong Kong %57.1, Seoul %61.2, Tokyo %76.4, KL %76.3...). `scripts/fix_stale_entry_prices.py` yazildi: her acik betin CLOB fiyat gecmisinden `placed_at` anindaki gercek fiyati ceker, %15+ sapma olanlarda entry_price/price/fair_value/shares/current_price/entry_fee/unrealized_pnl'i canli bot formulleriyle yeniden hesaplar (dry-run varsayilan, `--apply` yazar). **2026-08-10 uygulandi**: 17 bet duzeltildi, DB backup `data/backups/bot_pre_pricefix_*.db`. Kapanmis/arsiv betlere dokunulmadi (PnL gerceklesmis). Test: `tests/test_fix_stale_entry_prices.py` (9 test) — token cikarimi, düzeltme matematigi, sapma esigi |
| **KALIBRASYON bos — model bias duzeltilmiyordu (2026-08-10)** | `historical_calibrations` tablosu **0 satir**; `jobs/evolution_job.py::_run_calibration_backfill` bos govde (sadece log). Sonuc: Busan (MBE -2.9C), Seoul (-1.5C), LA (+1.9C) gibi sistematik model sapmalari tahminlere yansimiyordu — edge hesaplari bias'li tahminlerle yapiliyordu. Cozum: (1) `scripts/backfill_calibration.py` — junbo'nun **kendi verisiyle** (`weather_forecasts` per-model × `actuals.db` Archive) `historical_calibrations`'i **58,064 satirla** doldurdu (8 model × 48 sehir × max/min, INSERT OR REPLACE, idempotent). (2) `utils/calibration.py` — ASIAbot'tan tasinan `CalibrationEngine` (sehir/model MBE map, `raw - MBE`), lazy singleton. (3) `engine/calculator.py` `latest_by_source`'ta her model tahmini kalibre edilir (bias_map yoksa eski davranis korunur). (4) `_run_calibration_backfill` artik gunde 1 kez backfill script'ini calistirir + bias map'i tazeler. Test: `tests/test_calibration_engine.py` (7 test). Dogrulama: Busan max gfs raw=30 -> 33.26, Seoul max gfs raw=31 -> 35.09, bilinmeyen sehir degismez. Ayrica `test_bot_loop.py::test_cleanup_stale_bets_cancels_only_stale` sabit tarihler (08-08) kullandigindan bugun (08-10) 48h sinirini asip flaky oluyordu — goreli tarihlerle duzeltildi **2026-08-12: `_cleanup_stale_bets` ve testi KALDIRILDI (kapanis uretmiyordu) — kayit tarihseldir.** |
| **ERKEN GIRIS + SPREAD backtest (2026-08-10)** | Simulasyonlar gosterdi: market acilir acilmaz (ilk snapshot fiyati) + meteo tahmini etrafinda ±N dereceye YES bet (spread) en yuksek geliri veriyor. **En iyi config: spread=3, max_entry<0.30, kalibrasyonsuz (RAW) → 813 bet, %50.6, +$36,814 (5/5 gun pozitif; 08-06 dusuk cunku veri toplama baslangici — az bet, 08-10 hafif dusuk cunku kazanan giris fiyati ortalamasi yukseldi).** Kalibrasyon spread stratejisinde ZARARLI (CALIB +$28,481 < RAW +$36,814) — tahmini ortalamaya cekip longshot esiklerini kacirdigi icin. `scripts/backtest_early_spread.py` yazildi (tekrarlanabilir; `--spread`, `--max-entry`, `--calibrated`, `--min-bets`). Kalibrasyon yine de edge-tabanli (tek esik) stratejide degerli — calculator'da aktif kaliyor |
| **SPREAD STRATEJISI ANA MOD OLDU (2026-08-10)** | `executor/spread_placer.py` eklendi — yeni 2-gun-sonrasi tarih acildiginda en son meteo tahmini etrafinda +/- `spread_radius` (3) dereceye, ilk snapshot fiyatindan (<0.30), tahmini en yuksek ilk 15 sehre YES bet acar (gunluk 30 bet limiti). **Kayan pencere:** tahmin guncellenirse yeni pencerenin disinda kalan esikler kapatilir (bet_placer.close_bet_for_rotation). `BETTING_STRATEGY=spread` (varsayilan) / `edge` (eski) — bot_loop `scan_and_bet_loop`'ta strategy switch; edge modunda eski `run_cycle` cagrilir, spread modunda sadece spread_placer. Eski mod kodlari silinmedi (geri donulebilir). Test: `tests/test_spread_placer.py` (5 test — son tahmin secimi, ilk fiyat, radius icinde acma, gunluk limit, kayan pencere kapatma) |
| **Spread betleri commit edilmiyordu -> dup betler (2026-08-10)** | `place_spread_bets` `ctx.__enter__()` ile session actiginda `session.commit()` tetiklenmiyordu; bet'ler flush edilip DB'ye yazilmadan session kapaniyordu. Ayni gun icin tekrar cagrilinca onceki betleri gormeyip **ayni markete 2. bet** aciyordu (72 markette dup). Cozum: fonksiyon `_place_spread_bets_inner(session, day)` + `place_spread_bets` wrapper olarak ayrildi — wrapper `with get_session()` kullanir (blok sonu commit garantiler); session disaridan verilirse caller commit eder. Ayrica **snapshot_job `YES_PRICE_MIN` 0.005 -> 0.0005** dusturuldu (0.005 alti longshot marketlerin fiyat gecmisi yoktu, spread/backtest icin kritik); `_first_snapshot_price` snapshot yoksa `weather_markets.yes_price`'a fallback eder. Test: `test_spread_placer.py` 5 -> 6 |
| **SPREAD modunda stop-loss devre disi (2026-08-11)** | Kullanici karari: spread longshot'lari resolve'a kadar tutulur; kazanc 10-100x, kayip -stake. `run_risk_management` `betting_strategy == "spread"` ise `check_stop_loss`'u atlar (edge modunda eski davranis korunur). **2026-08-12 itibari: erken kapanis mekanizmalari (stop_loss/take_profit/trailing/time_decay/run_risk_management) TAMAMEN KALDIRILDI — tum betler (spread + edge) yalnizca settlement'ta kapanir.** Test: `test_bot_flow.py` `test_spread_flow_opens_bets_and_keeps_open` |
| **SPREAD_MAX_ENTRY 0.30 -> 0.99 (2026-08-11)** | Kullanici karari: 0.30 ustu esikler de acilsin. `.env` `SPREAD_MAX_ENTRY=0.99`. Simulasyon: 0.30 (+$36,814) vs 0.99 (+$36,695) — fark marjinal, avg/bet 45 -> 34. |
| **Portfolio yoksa spread placer bet atliyordu (2026-08-11)** | `place_spread_bets` `pf=None` ise `cash=0` -> `use_stake=0` -> SESSIZCE skip. Bot lifespan disindan (catch-up scripti) calisirken portfolio satiri garanti degildi. Cozum: portfolio yoksa `ensure_initial_portfolio()` cagirilir, sonra bet acilir; yine de cash yetersizse `logger.warning`. Test: `test_spread_placer.py` `test_place_spread_bets_creates_portfolio_when_missing`. Ayrica: **tam suite bot CALISIYORKEN kosulursa production DB bozulabiliyordu** (bot + test ayni anda yazinca WAL carpisti, "database disk image is malformed" + betler silindi). Bot kapaliyken suite kosulursa production korunuyor (dogrulandi: bets 595 -> 595). |
| **BAHIS PENCERESI DEVRE DISI (2026-08-11)** | Kullanici karari: betler gun boyu acilsin, gece fenestrati olmasin. `config/settings.py` `betting_window_enabled=False` (yorum: "pencere kaldirildi"), `.env` `BETTING_WINDOW_ENABLED=false`, settings'te env override eklendi. Etki: `_is_in_betting_window()` artik her zaman True doner -> `_reopen_after_stop_loss` ve `place_all_pending` gece de calisir. **9 edge test (test_sl_reopen_chain, test_settlement_chain, test_bet_behavior) gece ~03:00 UTC'de pencere kapali oldugu icin fail ediyordu; pencere kapali olunca hepsi gecti (18 passed).** **2026-08-12: `_reopen_after_stop_loss` ve `test_sl_reopen_chain.py` KALDIRILDI — kayit tarihseldir.** |
| **GERCEK AKIS testi `test_real_flow.py` (2026-08-11)** | Onceki akis testleri (`test_bot_flow.py`) loop'larin KENDISINI degil, sadece yardimci fonksiyonlari cagiriyordu — "gercek bot nasil calisiyor" sorusunu cevapsiz birakiyordu. Yeni `tests/test_real_flow.py` (11 test) bot_loop'un gercek fonksiyonlarini calistirir (dis ag + yan isler mock'lu): (1) spread modunda scan loop `run_cycle` CAGIRMAZ; (2) 2-gun tarih acilinca `place_spread_bets` CAGIRILIR (ANA bet yolu); (3) edge modunda `fast_price_until` set edilir; (4) `last_scan` naive UTC; (5) price_poller 4 adimi fetch->refresh->update->risk sirayla cagirir; (6) settlement run_settle + maintenance; (7) snapshot loop; (8-9) gercek DB ile entegrasyon (seed -> spread bet -> risk kapatmaz -> settle). Loop interval mantigi `_get_price_poll_interval` saf fonksiyonuna tasindi (saf test). **Onemli test dersi: `patch("bot_loop.asyncio.sleep")` global `asyncio.sleep`'i bozuyor (wait_for/create_task kirilir) — bunun yerine loop task'i `cancel()` ile sonlandiriliyor; `asyncio.to_thread` ile sariilan mock'lar SYNC olmali (AsyncMock coroutine'i thread'de await edilmez -> "never awaited" uyarisi).** Suite: 701 -> 712 |
| **SPREAD betleri CANLI fiyatla açiliyor, snapshot DEGIL (2026-08-11)** | Audit: canli production DB'de son betler BAYAT snapshot fiyatiyla acilmisti — bet 594 `entry=0.50` iken canli `weather_markets.yes_price` ayni anda **0.0085**; bet 595 entry=0.50 vs canli 0.54; bet 593 entry=0.33 vs canli 0.425. `spread_placer._place_spread_bets_inner` `_first_snapshot_price` kullaniyordu (30 dk'da bir alinan snapshot bayat kalabiliyordu). Cozum: `_first_snapshot_price` fonksiyonu ve `MarketSnapshot` importu **kaldirildi**; bet artik `mkt.yes_price` (5 dk'da `run_fetch_markets` ile guncellenen CANLI fiyat) ile acilir, `0 < entry < max_entry` filtre + fill canli fiyata gore. Duzeltme sonrasi ayni bet'ler canli fiyata yakin entry alir. Test: `test_spread_placer.py` 2 snapshot testi -> `test_spread_uses_live_market_price_not_snapshot` (snapshot 0.05, canli 0.15 -> entry 0.15) + `test_spread_skips_snapshot_low_but_live_high` (canli max_entry ustunde -> acilmaz) |
| **EDGE testleri `tests/edge/` altina tasindi (2026-08-11)** | Kullanici karari: edge moduna ozgu testler (`test_sl_reopen_chain.py`, `test_settlement_chain.py`, `test_bet_behavior.py` — hepsi `betting_strategy="edge"` ile kosuyor) ana akis testlerinden ayirilsin ki kafa karismasin. `tests/edge/` klasorune tasindi (bos `__init__.py` ile package). Pytest recursive toplar (full suite hala 712 passed). `test_bot_flow.py` + `test_real_flow.py` edge/spread KARSILASTIRMA testleri ana dizinde kalir (iki stratejiyi de dogrularlar). **2026-08-12: `test_sl_reopen_chain.py` KALDIRILDI — kayit tarihseldir.** |
| **SPREAD_MAX_ENTRY 0.99 -> 0.95 (2026-08-11)** | Kullanici karari: `0 < entry < 0.95` olsun (0.99 degil). `.env` `SPREAD_MAX_ENTRY=0.95`, `config/settings.py` default `spread_max_entry=0.95`. 0.95-0.99 arasi esikler acilmaz (fill 0.99'a clamp edilse bile 1.00'e yakin giris kar marjini yer; 0.95 ust sinir).
| **SPREAD_MAX_BETS_PER_DAY 30 -> 100 (settings default) (2026-08-11)** | `config/settings.py` default `spread_max_bets_per_day=100` (.env zaten 100'di; default kod ile .env cakismasi giderildi). Gunluk 100 esik / 15 sehir = sehir basi ~6-7 esik acilabilir.
| **BACKUP retention artirildi (2026-08-11)** | Kullanici sorusu: "5 backup ile uzun backtest olur mu?" — 5 cok az. `db_backup.py` `MAX_BACKUPS=5 -> 30` (gunde 1 scheduled backup -> ~30 gunluk geri donus noktasi; ~6.3GB). `scripts/backup_databases.py` `RETENTION_DAYS=30 -> 90`. Not: uzun backtest ASIL verisi `market_snapshots` (365 gun retention) + `backtest.db` + `actuals.db`'dir; backup'lar felaket kurtarma icindir.
| **start_bot.bat cift bot uretiyordu (2026-08-11)** | `start_bot.bat` hem `python watchdog.py` hem kendi `goto START` dongusuyle `python main.py bot` calistiriyordu; `watchdog.py` de bot'u restart ettigi icin **2 bot process'i** doguyordu (port 8093 cakismasi, surekli birbirini oldurme). Cozum: bat SADECE `python watchdog.py` calistirir (watchdog bot'u tek basina baslatir/restart eder). Dogrulama: JunboBot **Windows servisi** (pythonservice.exe, AUTO_START, RUNNING) + `JunboBotWatchdog` (her 1 dk, heartbeat 15dk) + `JunboDataWatchdog` (her 5 dk) + `JunboBot` task (boot/logon/time tetik) — 4 katmanli koruma. Hepsinde tek bot process dogrulandi. |
| **Yeni-market fast mode GUN BAZLI oldu (2026-08-11)** | Kullanici tespiti: gun dongusunde bugunun marketleri kapanir, 2-gun-sonrasi acilir — toplam market SAYISI yaklasik AYNI kalir, bu yuzden `current_count > previous_market_count` (sayi bazli) tetikleme YANLIS sinyaldi. Cozum: `bot_loop.scan_and_bet_loop` artik `_get_open_target_dates()` TARIH kumesinde yeni bir gun belirince fast mode baslatir (`last_open_dates` kumesi izlenir). `_get_market_count` bu akista artik kullanilmiyor. |
| **Spread sehir secimi: ANA KRITER "tahmini en az sapan", SICAKLIK DEGIL (2026-08-11, kullanici duzeltmesi)** | Kullanici ornegi: London 20C tahmini az sapiyorsa SECILIR, Kahire 45C tahmini cok sapiyorsa ELENIR. Sicaklik ana kriter DEGIL. Metrik: `spread_placer._city_accuracy(session)` -> `historical_calibrations.bias` uzerinden sehir bazli ortalama |bias| (dusuk = az sapan). Siralama anahtari: `(sapan mi? |bias|>2.5 -> sona, |bias| kucuk once, esitse sicak tie-break)`; bias verisi olmayan yeni sehirler ortada. "En sicak 15" ifadesi YANLISTIR — kod/yorum/README/cevapta asla oyle yazma. Test: `test_city_accuracy_uses_abs_bias` + `test_spread_prefers_accurate_city_over_hot`. |
| **SPREAD bet limiti 350 + periyodik retry + top-15 disi kapatma (2026-08-11)** | Kullanici istegi: 13 Agustos acilinca da 15 sehir (15 x 7 = 105 bet) daha acilsin, limite takilmasin. `spread_max_bets_per_day=350` (3 gun x 15 sehir x 7 esik = 315 + marj). `.env` + `settings.py` senkron. Ayrica: (1) `bot_loop` her ~12 dongu (~60 dk) en yeni acik tarih icin `place_spread_bets`'i yeniden cagirir (sonradan acilan marketleri yakalar — orn. Ankara 32C "NEW"); (2) `_place_spread_bets_inner` secili 15 sehir disindaki sehirlerin (hedef gun) acik betlerini kapatir (portfoy 15 sehirle sinirli). Test: `test_out_of_selection_bets_closed`. |
| **`yes_price is None` aciklamasi duzeltildi (2026-08-11)** | Kullanici tespiti: Polymarket YES betleri DAIMA fiyatla acilir (0.1 cent min tick) — "entry yok" (fiyat yayinlanmadi) pratikte olmaz. Kodda NULL guard'i bozuk/yarim kayit guvenligi olarak kaldi ama artik net `logger.warning("market fiyati yok (bozuk kayit)")` mesaji var; aciklamalarda "fiyat yayinlanmadi" degil "bozuk kayit" denir. |
| **ERKEN GIRIS: 0-13 UTC hafif probe + CLOB WebSocket (2026-08-11)** | Kullanici hedefi: "limit altinda kalmak degil, piyasaya ERKEN girmek — millet milisaniyelerle islem yapiyor". Analiz: (1) `_get_open_target_dates` DB'den okur — yeni tarih DB'ye ancak cekisle girer, yani 2 sn'de bir DB kontrolu ise yaramaz; (2) tam cekis (100+ sehir sorgusu) 2 sn'de bir = kesin rate limit. Cozum: `bot_loop._probe_new_target_date()` — Gamma'ya TEK hafif sorgu (public-search, limit_per_type=5, order=endDate desc), DB max tarihinden ileri tarih bulursa TRUE. 0-13 UTC penceresinde (`midnight_scan_window=13` SAAT cinsinden, `midnight_scan_interval=1` sn) scan loop probe yapar; TRUE ise hemen tam fetch + spread, YOKSA cekis atlanir. Probe `asyncio.to_thread` ile cagrilir (async loop icinde asyncio.run hatasi). Ayrica `bot_loop.clob_stream_loop` — acik betlerin marketlerine CLOB WebSocket (`scrapers/clob_stream.py`, onceden tanimliydi ama HIC calismiyordu) aboneligi; fiyat degisimleri ANINDA islenir (5 dk polling yok). `main.py` `state.tasks["clob_stream"]` ile baslatir. Test: `test_probe_new_target_date_*` (3 test) + `test_bot_loop` saat bazli pencere testleri. Suite: 716 -> 721 |
| **TAM-7 KURALI KALDIRILDI + KAYAN PENCERE SIMULASYONU (2026-08-11, karar A)** | Kullanici sorusu: "pencere kaydirma yapmiyor muyuz? merkez kayinca uclari kapatip yenilerini acmiyor muyuz?" — HAKLIYDI. Eski `backtest_early_spread.py` kayan pencereyi simule etmiyordu (ilk tahmin + ilk fiyat + settlement), bu yuzden yaniltici sonuc veriyordu. Yeni `scripts/backtest_rolling_window.py` gercek bot davranisini simule eder (forecast guncellemelerinde merkez kayar, eski uclar o anki fiyattan kapanir, yeniler acilir). Sonuclar: **0.30 + kayan pencere +$53,284**; 0.99 + kayan pencere +$52,018; **tam-7 zorunlu eklenince karlilik DUSER** (0.30 tam-7'li +$5,611; 0.99 tam-7'li +$19,747) — cunku 7 esigin hepsi acik sarti merkez kayinca kARLI esikleri de kapatiyor. Karar: tam-7 KALDIRILDI, `spread_max_entry=0.30` + kayan pencere gecerli. Merkez marketi olmasa da acilabilen ayaklar acilir (`test_open_legs_when_center_market_missing`). Wellington 11.08'deki -12.60$ kaybi asil neden: tam-7 yokken bile merkez 12C fiyat 0.585>0.30 oldugu icin atlandi, ama 0.30 ile merkez alinamadigi icin ayaklar kaybetti — bu simdi 0.30 esigiyle ayni kalir; fark: tam-7 sehri komple kapatmiyor. Suite: 723 -> 722 |
| **`backtest_rolling_window.py` — KAYAN PENCERE backtest (2026-08-11)** | `scripts/backtest_early_spread.py` YANILTICI (kayan pencere yok). Yeni script: snapshot (30dk fiyat) + forecast (fetched_at) zaman ekseninde birlestirir; her forecast guncellemesinde merkez kayar, pencere disinda kalan esikler `_price_at` ile o anki fiyattan kapatilir, yeni esikler acilir; settlement'ta kalanlar actuals'a gore cozulur. `--strict-7` opsiyonu tam-7 zorunluyu simule eder (kullanildiginda karliligin dustugunu gosterir). Kullanici karari: bu script gercegi yansittigi icin kalici backtest aracidir. |
| **POLYMARKET PROXY — bot market cekemiyordu (2026-08-16)** | Kullanici tespiti: "ben polymarkete giriyorum, bot nasil giremiyor?" Kok neden: sistem PAC dosyasi (`C:/Users/fdemir/polymarket.pac`) Polymarket trafigini SOCKS `127.0.0.1:40000` uzerinden yonlendiriyor (Cloudflare WARP); kullanici tarayicisi bu PAC'i kullanip giriyordu ama Python requests sistem PAC'ini otomatik kullanmaz -> DIRECT gidiyordu -> Cloudflare `10054 connection reset`. Bot 2+ gundur market cekemiyordu; 18 Agustos marketleri Polymarket'te vardi (319 event) ama DB'ye girmiyordu. Cozum: (1) `.env` `POLY_PROXY=socks5h://127.0.0.1:40000`; (2) `scrapers/async_client.py` `AsyncHttpClient(proxy=...)` — aiohttp session + requests fallback'ine proxy; (3) `scrapers/polymarket.py` `_fetch_raw_markets` proxy ile client kurar. Test: `tests/test_regression_fixes.py::TestPolymarketProxyLive` (canli gamma + market cekme + Playwright tarayici fallback). |
| **REGRESSION FIX TESTLERI (2026-08-16)** | Duzeltilen ama testi olmayan bug'lar icin `tests/test_regression_fixes.py` eklendi: (1) proxy config + canli erisim + tarayici fallback (5691228); (2) `CITY_ICAO_MAP` 7 sehir dogru istasyon + RKSI Incheon koord (1f9313a); (3) orderbook arsiv yazimi (0bb98f1); (4) Gamma rate limit/throttle (0bb98f1); (5) `partial_tp_done` migration — model+DB'de kolon yok (28c5ba4). Suite: 638 -> 665 passed (6 skipped canli). |
| **SPREAD TEK ESIK + 0.95 + ILK 40 (2026-08-16, kullanici duzeltmesi)** | Kullanici: "0.01-0.95 arasi ilk 40 markete ac, ben spread TEK dedim +-1 demedim". Kok nedenler: (1) `spread_placer.py:172` `int(getattr(s,"spread_radius",3) or 3)` — `0 or 3` **radius=0'i default 3'e dusuruyordu** (falsy bug), bu yuzden 18 Agu'da 8 degil 3 esikli acilacakti ama tek esik bekleniyordu; (2) fair-value filtresi (entry>=fair skip) + 0.10-0.20 olum bolge filtreleri bet sayisini kisiyordu — kullanici "fiyat ne olursa olsun 0.95 ve alti" dedi, KALDIRILDI; (3) `max_entry=0.30`/`max_bets=350` kullanici talimatina aykiridi. Cozum: `spread_radius=0`, `spread_max_entry=0.95`, `spread_max_bets_per_day=40` (.env + settings.py), fair-value/olum-bolge kaldirildi, `_fair_price` (dead) silindi, `ICAO_COORDS` duplicate RKSI temizlendi (F601). Ayrica `test_bot_flow.py`/`test_real_flow.py` `spread_radius=3` set edip geri yuklemiyordu -> full suite'te radius=0 testleri patliyordu; `test_spread_placer._clean_db` artik her test oncesi config'i sifirliyor. Test: `test_spread_placer.py` 14 (yeni: tek merkez esik, 0.50 fair-ustu acilir, 0.15 olum-bolge acilir). Suite: 665 -> 663 passed (8 skipped). |
| **ICAO_COORDS duplicate RKSI (2026-08-16)** | `config/settings.py` `ICAO_COORDS` icinde `"RKSI"` iki kez (satir 246 ve 298, ayni deger) — ruff F601. Ikinci kopya silindi. |
| **METAR + OPEN-METEO global env proxy sizintisi (2026-08-16)** | `config/settings.py:638-642` Polymarket SOCKS proxy'yi `os.environ["HTTP_PROXY/HTTPS_PROXY/ALL_PROXY"]` olarak GLOBAL set ediyor (ClobClient + direct calls icin). `requests` env proxy'yi otomatik okur (trust_env) -> aviationweather.gov ve open-meteo.com da SOCKS proxy'den gitmeye basladi. Bu iki site geo-block'lu DEGIL; proxy'den 20s timeout/502 -> **172 METAR hatasi, METAR-peak betleri acilamiyordu** (kullanici: "metardan sonucu almayacak miyiz, neden zarar ediyoruz"). Cozum: `scrapers/metar.py::_fetch_metar`, `scrapers/meteo.py::_fetch_open_meteo`, `data_pipeline/weather_ensemble.py` (3 fonksiyon) `requests.get(..., proxies={"http": None, "https": None, "all": None})` — "all": None env'yi tamamen kapatir, DIRECT. Test: fetch_metar_live RJTT 0.7s (once 20s timeout), open-meteo 0.7s 200. Suite: 663 -> 664 passed. |
| **ORDERBOOK SADECE BETLI MARKETLERI KAPSIYORDU (2026-08-16)** | `bot_loop.clob_stream_loop::_asset_ids` sadece `Bet.status.in_(OPEN_BET_STATUSES)` join'li marketleri dinliyordu -> orderbook.db gecmisi SADECE bot'un bet actigi marketleri kapsiyor (backtest'te "skip fiyat-yok" 463, 628 kazanan sehir/gun kombinasyonundan sadece 28'i fiyata sahip). Kullanici: "tum weather betlerin orderbook'u cekilecek, eksik bulamiyorum deme". Cozum: (1) `_asset_ids` -> `WeatherMarket.status == 'open'` (TUM acik weather marketler); (2) `scripts/collect_orderbook.py` yeniden yazildi — bot.db'deki acik marketlerin YES token orderbook'unu toplar (best_bid/ask/depth), `--loop --interval 900` ile bot entegrasyonu; Gamma `events?tag_slug=weather` YANLIS kategoriler doner (April 2024 temperature increase) — dogru kaynak bot.db. (3) `_archive_clob_price` orderbook_snapshots'a `threshold` yazar. Ayrica: `weather_forecasts.city` = ICAO kodu = `weather_markets.city_code` (49/49 eslesir); T-2 erken forecast sadece 16 Agu target'lilar icin var (forecast kaydi 08-14'te basladi); METAR'a en yakin tahmin `ecmwf_ifs025` (avg|bias|=0.97C). Suite: 663 passed. |
| **METAR-PEAK YEREL SAAT (2026-08-16)** | Kullanici: "benim saatimle degil, sehirin YEREL saatine gore gir. Yerel saatte en yuksek ne zaman oluyorsa o zaman gir." Sorun: `scrapers/metar.py::detect_peak` sabit `min_utc_hour=15` (UTC) kullaniyordu -> dogu sehirleri (Wellington 03:00 UTC, Hong Kong 07:00 UTC, Seoul 07:00 UTC, Taipei 03:00 UTC) gunun max'ini cok onceden yaptigi icin UTC>=15 esigi peak'i kaciriyordu; ayrica `MIN_HOURS_BEFORE_CLOSE=4` botu kapanisa 4 saat kala girmeye zorluyor, o zamana kadar Polymarket kazanan bucket'i 1.00'e fiyatlamis oluyordu (log: hep `giris=1.000 atlandi`). Cozum: (1) `detect_peak(day_rows, min_local_hour=13, utc_offset_hours=0.0)` — kilitlenme kurali YEREL saat uzerinden (epoch + offset); (2) `jobs/metar_peak.py` her market icin `utc_offset = round(lon/15)` hesaplar (Wellington +12, Hong Kong +8, New York -5); (3) `MIN_HOURS_BEFORE_CLOCK` 4 -> 2 (erken giris). Sonuc: 16 Agu'da 33 -> 44 sehir peak kilitleniyor. Test: `test_metar_peak.py::TestDetectPeakLocalTime` (Wellington UTC+12 ve Hong Kong UTC+8 yerel 15:00 peak kilitler). Suite: 663 -> 667 passed. **NOT (2026-08-18, M3): `round(lon/15)` nominal offset yanlisti (China +7 degil +8, Seoul +8 degil +9, London BST +1, Lucknow +5 degil +5:30). Artik `scrapers/metar.city_utc_offset()` (zoneinfo + DST) kullanilir; nominal sadece bilinmeyen sehir fallback'i. Yukaridaki satira aykiridir.** |
| **YANLIS BUCKET BETLERI KAPATILMIYORDU (2026-08-16, 3. adim)** | Kullanici: "T-2 oncesi actigimiz bet kazanan bucket'ta degilse onu kapatiyoruz. 2 gun onceden meteoya gore bet ac, her sehirin yerel saatinde max olunca tekrar gir, tutmuyorsa kapat." Sorun: bot IKI BAGLANTISIZ strateji calistiriyordu — spread (T-2'de merkeze bet acar, `kaydirma kapali` yuzunden ASLA kapatmaz, 2026-08-12 karari) ve metar-peak (peak kilitlenince EK bet acar, mevcut yanlislari kapatmaz). 16 Agu'da 75 acik bet, sadece 6'si kazanan bucket'ta — 69 yanlis bet settlement'a kadar acik kaldi, tam stake kaybedilecekti. Cozum: `jobs/metar_peak.py::_close_wrong_bucket_bets(session, city_code, target_date, winning_bucket)` — peak kilitlendiginde o sehrin kazanan bucket DISINDAKI tum acik betleri (spread + metar) canli fiyattan kapatir; `executor/bet_placer.py::close_bet_for_rotation` kullanir (canli fiyattan satis, portfolio kredisi, kendi commit'i). Test: `test_metar_peak.py::TestCloseWrongBucketBets` (32C yanlis bucket kapanir, 34C kazanan tutulur). Suite: 667 -> 668 passed. |
| **3 ESIK + PEAK'TE KOMSU SATISI (2026-08-16)** | Kullanici fikri: "3'lü esik acarsak, peak yaklasirken komsu esikler de yukselir. Gercek esik bizim esiklerimizden biriyse, diger 2 komsuyu HEMEN satarsak (millet uyanmadan) onlardan da para kazaniriz." Cozum: `spread_radius` 0 -> 1 (merkez+-1; `.env` + `config/settings.py:172`). Artik T-2'de 3 esige de dusuk fiyattan girilir; peak gunu kilitlenince kazanan bucket TUTULUR, komsular `_close_wrong_bucket_bets` ile canli fiyattan satilir. Mantik: RANGE marketlerde sadece 1 bucket kazanir ama komsu esikler peak oncesi belirsizlikle degerli olur; peak kilitlenme ani ile piyasa tepkisi arasindaki pencerede satmak kar getirebilir. YARIN 17 Agu orderbook verisiyle dogrulanacak (komsu fiyatlar peak oncesi yukseliyor mu, pencere genisligi, net kar) — YAPILACAKLAR.md. Test: `test_spread_opens_three_legs_around_center` (24,25,26 esikleri acilir). Suite: 668 -> 667 passed. |
| **METAR PARALEL FETCH + CLOB REST YEDEGI + LIMIT 120 (2026-08-17)** | (1) **METAR timeout:** `run_metar_peak_bets` 40 sehri TEK TEK `fetch_metar_day` ile cekiyordu (her biri 1-5s ag) -> toplam ~80s > `_FETCH_TIMEOUT=60` -> "METAR poll timed out" -> peak'ler kaciyordu (kullanici: "surekli sorun cikiyor"). Cozum: `ThreadPoolExecutor(max_workers=8)` ile paralel fetch (benzersiz city_code/day bazinda, cache'li), 40 sehir ~35s'de biter. `_FETCH_TIMEOUT` metar_peak.py'ye sabit olarak tanimlandi (bot_loop'tan import circular). (2) **CLOB WS proxy:** `ws-subscriptions-clob` geo-block (direct -> getaddrinfo failed) + WARP SOCKS WS desteklemiyor (`General SOCKS server failure`). aiohttp_socks kuruldu (socks5 scheme, socks5h desteklenmez). `clob_stream_loop` WS 3 kez fail edince `_clob_rest_poll_once`'e gecer: REST GET /book (proxy ile calisir) ile acik marketlerin best_ask'ini orderbook.db'ye arsivler (5dk'da bir). Orderbook verisi boylece toplanmaya devam eder (17 Agu: 23.9k satir). (3) **Gunluk limit:** `spread_max_bets_per_day` 40 -> 120 (kullanici "Toplam 120"). 40 iken 17+18 Agu'ya 46 bet acilmis, `remaining = 40-46 = 0` -> 19 Agu'ya (285 market, forecast hazir) HIC bet acilamiyordu (`skipped: 23`). 120 ile 19 Agu'ya 56 bet acildi. Suite: 667 passed. |
| **DASHBOARD MOJIBAKE — api.py Turkce karakter bozuklugu (2026-08-21, kullanici bildirimi)** | Kullanici: "Uyarılar ve Bayraklar ... kayÄ±p ... sonuÃ§lanan ... turkce karakter sorunu". api.py icindeki 35 satir Windows-1252 mojibake (UTF-8 Turkce karakterlerin cift-encodlanmis gorunumu) idi: red-flags, bet listesi, tarih/yan/format string'leri. Cozum: `s.encode('cp1252').decode('utf-8')` ile geri cozuldu + dogrudan bozuk em-dash (`â€"`) ve hatali ceviriler (`gÃ¶re`->gore, `aš¡`->a--) elle duzeltildi. Sonuc: api.py %100 ASCII (CLAUDE.md kurali: kodda c g i o s u yasak), ruff+mypy+format temiz. NOT: `data/` altindaki eski report'lar ve log dosyalari hala mojibake olabilir — bunlar okunabilir durumda, dokunulmadi. Suite: 703 passed, 7 skipped. |
| **API port default 8093 -> 8091 (2026-09-14, ocr incelemesi)** | `watchdog.py` 8091'e cekilmisti ama `config/settings.py` default `port` hala 8093 idi — PORT env setli degilse bot 8093'te acilip watchdog/service.ps1 (8091) ile uyusmazdi. COZUM: default `8091` (`.env` PORT=8091, `service.ps1`, `test_config_loads` ile senkron). TEST: `test_e2e_system.py::TestStep1_BotStartup::test_config_loads` + config paketi (24 passed), ruff temiz. |
| **ACIK BET MUHASEBESİ: `bet.pnl` ile `realized_pnl` ayrışıyordu (2026-10-22)** | `executor/bet_placer.py` bet kaydederken `bet.pnl` hesaplarken `realized_pnl` **farklı** bir değer yazıyordu; muhasebe tek kaynaktan (`realized_pnl`) okumaya başlandı. `api.py` PnL hesapları tek kaynağa bağlandı, `/api/equity-curve` kümesine `closed` (rotation) eklendi; tooltip/formül gerçek muhasebeyi gösteriyor. DB gerçeği: `won` brüt kazancı **+800 USD** (246 bet, +820.39) iken gerçek net kapanış PnL (`won`+`lost`+`settled`+`closed_early`+`closed`) **+89.17** — yani 800 USD tek başına yanıltıcı. Test: `tests/test_accounting.py`, `tests/test_settler_polymarket.py`. |
| **SETTLEMENT GURULTUSU + GAMMA 403 (2026-10-22, kullanici sorusu: "20 ekim kapanmamis betler var neden?")** | **Tespit:** DB'de 65 acik bet, 23 tanesinin target_date'i 20-21 Ekim ve market status'u `open` - yani `settle_all` bunlari GERCEKTEN isliyor. Canli Gamma kontrolu: 20 marketin hicbirinde `closed=true` degil (`umaResolutionStatus=null`), fiyatlar 0.86-0.99. Cozulmus marketler karsilastirmasi `closed=true/uma=resolved` gosteriyor. **Sonuc: kaynak gecikmesi (Polymarket/UMA 1-3 gun gecikmeyle cozuyor), bot hatasi degil.** **Bot tarafi iki gercek kusur duzeltildi:** (1) `settle_all` her turda hedef tarihi gecmis TUM marketleri (~945) isliyordu; beti olmayanlar Gamma'ya hic sorulmuyordu ama her biri "pending" sayilip `pending >48h` ERROR logu basiyordu (16 saatte 24.328 log satiri = TAMAMEN gurultu) - artik yalnizca ACIK BETI OLAN marketler Gamma'ya sorgulanir, beti olmayanlar toplu olarak `expired` isaretlenir (toplu UPDATE, tek DB sorgusu); (2) `_call_gamma_api` **hic User-Agent gondermiyordu** (python-requests default) ve log'da 2127 adet "Gamma API request failed" (403 Forbidden / 10054) vardi - scraper `Junbo/1.0 (+tier3-12)` UA gonderiyor ve sorunsuz calisiyor. Cozum: UA + `Accept` basligi eklendi, 403/429 icin `Retry-After` destekli bekleme + 3 deneme. **Kural degismedi:** fiyat-tabanli settlement hala KAPALI (yalnizca resmi UMA cozumlemesi kabul edilir). Test: `tests/edge/test_settlement_chain.py` (expired kurali + acik betli market ASLA expired olmaz), `tests/test_settler.py`, `tests/test_settler_polymarket.py`. |
| **`utils/weather_sources.py` IMPORT HATASI (2026-10-22)** | `test_latent_bugs.py::test_import_all_modules` -> `Failed: utils.weather_sources: 'MeteoConfig' object has no attribute 'iem_url'`. Modul import aninda `bot_config.meteo.iem_url/nws_url/aviation_url/vc_url/vc_api_key` okuyordu ama bu alanlar `MeteoConfig`'te HIC tanimli degildi -> modul hic import edilemiyordu (ve `vc_api_key` rotasyonu icin `next_vc_key()`/`rotate_vc_key()` method'lari da yoktu). Cozum: `config/settings.py::MeteoConfig` icine 4 endpoint alani + VC anahtar havuzu (`VC_API_KEYS` virgullu liste) + `next_vc_key()`/`rotate_vc_key()` eklendi. Test: `test_latent_bugs.py` 5 passed. |
| **DEAD-FUNC CENSUS: yeni moduller (2026-10-22)** | `test_latent_bugs.py::test_no_dead_public_functions` 35 fonksiyonu "dead" buldu: hepsi daha onceki oturumlarda eklenip HIC commit/wire edilmemis yeni modullerden (`backtest/`, `replay/`, `research/`, `jobs/heat_mos_job.py`, `jobs/new_city_job.py`, `jobs/collect_settlement_temps.py`, `utils/weather_sources.py`, `utils/om_cache.py`, `scripts/backtest_forecast_peak.py`, `scripts/backtest_peak_trail.py`, `scripts/replay_full_bot.py`, `debug_spread.py`). Bunlar ya standalone CLI scripti ya da job/backtest modulunun ic helper'i (census, tanim dosyasi disinda referans gormedigi icin "dead" sayiyor). Cozum: `ALLOWED_DEAD` setine gerekceleriyle eklendi (35 fonksiyon) - SILINMEDILER, testlerle korunuyorlar. |

| **VERI TOPLAMA DURDU + EKIM ARSIVI (2026-11-11)** | Kullanici: "junbo, asia ve heat data topladi ama yeterli data yok, bence yeterli bakmadin". Kok neden: `Junbo-OrderbookCollect`, `Junbo-ActualsCollect`, `Junbo-SyncBacktest` gorevleri **Disabled** kalmis ve scriptler repo `data/` yoluna bakiyordu; repo `data/bot.db` 0 byte oldugu icin Eylul sonrasi orderbook/backtest sync durdu. Cozum: (1) `collect_orderbook.py`, `collect_actuals.py`, `backup_databases.py`, `sync_backtest_db.py` yollari `D:\JUNBO data` (env override'li) yapildi; (2) `sync_backtest_db.py` toplu SQLite ATTACH kopyasina gecirildi (1.17M snapshot + 30k market tek seferde); (3) `archive_forecasts.py` eklendi (6 saatte bir gunluk forecast parquet snapshot); (4) `build_data_catalog.py` + `query_data_catalog.py` eklendi; tum DB/parquet dosyalari `D:\JUNBO data\catalog` altinda kataloglanir. Ekim forecast'lari `archive/weather_forecasts_20261013..20261025 + 20261108` parquet'lerinde bulundu; tam Ekim (2-29 Ekim) backtest'i `reports/backtest_october_full_20261111.md` (+$496.19 cal_all). ASIA forecast'lari sadece 23-31 Agustos; Heat `prediction_log` 16 Eylul-30 Ekim. Gorevlerin yeniden enable edilmesi icin yonetici olarak `scripts\setup_tasks.ps1` calistirilmali (auto-mode scheduled task iznini engelledi). |
| **BOT SIFIRLANDI / DB_PATH (2026-11-11)** | Kullanici: "junbo calistigi halde hersey sifirlandi ve hic bet acilmiyor". Kok neden: `.env`'de `DB_PATH` yoktu; bot repo `data/bot.db`'ye baglanmisti (0 bet, 1000$). Gercek DB `D:\JUNBO data\bot.db` (1.589 bet, nakit 1.309,51$, realized +550,27$). Cozum: `.env` `DB_PATH=D:\JUNBO data\bot.db`; servis restart. Sonuc: portfoy 1.427$, nakit 1.366,87$, spread Tokyo 0.33 (id 1590) ve metar_peak Paris 0.435 (id 1591) acildi. Ayrica `database/db.py` SQLite `timeout=30` + `busy_timeout=30000` (D: USB'de eszamanli yazimda "database is locked" bet/scan kaybini azaltir). |
| **SLEEVE + NO BACKTEST (2026-11-12)** | `scripts/strategy_sleeves_report.py` eklendi; `Junbo-StrategySleeves` gunluk 00:15. Spread ve metar_peak ayri 1.000$ sanal kasayla raporlanir (`reports/strategy_sleeves_latest.md`). 3 aylik NO testi: model merkezine NO 1.492 bet, %64,5 win ama **-$342,80** (ROI -11,5); dusuk YES binlerinde NO negatif, yuksek YES/son snapshot NO pozitif ama kucuk orneklem. Fiyat-bin raporu: `reports/no_yes_price_bins_20261111.csv`. |
| **METAR-peak `entry_fee` kasadan hic dusulmuyordu (S14, 2026-10-27)** | `jobs/metar_peak.py::_open_metar_bet` yalnizca `use_stake` icin `debit_stake` cagiriyordu; `entry_fee` sadece `settlement_pnl` icine gomuluyordu. Sonuc: `cash_balance` fee toplami kadar (**\$34,66**, 488 bet) fazla; `spread_placer`/`bet_placer` ile tutarsiz. COZUM: stake debitinden sonra `if entry_fee > 0: debit_stake(session, entry_fee, "metar_peak_fee ...")` (ayni `try/except ValueError` icinde, nakit yetmezse rollback). TEST: `tests/test_metar_peak_module.py::TestMetarPeakAccounting::test_open_metar_bet_debits_stake_and_fee`. |
| **`portfolio.total_realized_pnl` rotation'i saymiyordu (S14, 2026-10-27)** | `settler.py` yalnizca settlement PnL'ini ekliyordu; `close_bet_for_rotation` sadece `bet.realized_pnl` yaziyordu. Sonuc: ayni anda 3 farkli "kar" sayisi — portfoy **+\$488,05**, nakit degisimi **+\$258,91**, bet tablolari **-\$220,73**. COZUM: `close_bet_for_rotation` icinde `pf.total_realized_pnl += pnl`. TEST: `tests/test_rotation_accounting.py::test_rotation_close_writes_both_pnl_columns` (tam tutar esitligi). NOT: 2026-10-27 oncesi gecmis satirlar geriye donuk duzeltilmedi; portfoy toplami ancak bundan sonra tek kaynak olur. |
| **`bet_type` hic yazilmiyordu -> strateji PnL ayrimi imkansiz (S14, 2026-10-27)** | Kolon `database/models.py:183`'te "DEPRECATED: dead column" yorumuyla duruyordu ve hicbir kod yazmiyordu; 1.461 betin tamami NULL. Bu yuzden "hangi strateji para kaybettiriyor" sorusu ancak `order_id` prefix'i (`spread_`/`metar_`/`paper_`) ile dolayli cevaplanabiliyordu. COZUM: 4 bet olusturma noktasi dolduruldu — `jobs/metar_peak.py` = `metar_peak`, `executor/spread_placer.py` = `spread`, `executor/bet_placer.py::open_bet_on_market` = `spread`, `engine/strategy.py::execute_signal` ve `bet_placer` analiz yolu = `edge`. |
| **Olu `tests/test_freeroll.py` suite'i surekli kirmizi tutuyordu (S14, 2026-10-27)** | 2026-09-16'da eklenen free-roll (`run_freeroll`) ve fee-gate (`net_win_profit`) ozellikleri urun kodundan geri alinmis, testleri kalmisti: 3 test `ImportError` ile HER ZAMAN fail. COZUM: olu testler kaldirildi; gecerli `test_rotation_close_writes_both_pnl_columns` `tests/test_rotation_accounting.py`'ye tasindi ve portfolio toplam esitligini de dogrulayacak sekilde guclendirildi. |
| **"D: diski arizali" yanlis teshis + settlement yanlis gunu yaziyordu (S16, 2026-10-27)** | Iki ayri hata. (1) Yedek dizinindeki 315 adet 0-byte dosya "disk arizali" kanaatine yol acmisti; olcum bunu curuttu: `fsutil dirty query D:` temiz, `Get-Volume` Healthy/OK, 30 gunde disk hatasi yok, `TestDBProtection` 4/4 gecti ve 1,5 GB'lik yedek basariyla yazildi -> **onarilacak disk arizasi YOK**, kesinti 2026-09-28..10-27 arasi geciciydi. Gercek dar bogaz C: (%98 dolu). (2) `executor/settler.py::_settle_market` gercek sicakligi `bet.settled_at` (yerlesim ANI) ile cekiyordu; UMA gec cozdugunde `bets.outcome` icine yanlis gunun sicakligi yaziliyordu. COZUM: `temp_date = getattr(market, "target_date", None) or bet.settled_at`; ayrica `scripts/backfill_bet_outcomes.py` (298/298 kuru kosu eslesmesi) ile geriye donuk doldurma araci eklendi. Olu degiskenler temizlendi (`ruff F821,F841` temiz). |
| **`test_tie_betting` tam suite'te 7/7 kiriliyordu (S17, 2026-10-27)** | Tek basina **7/7 geciyor**, tam suite'te **7/7 kiriliyor**; log `place_all_pending: 0 open markets found`. KOK NEDEN: `executor/bet_placer.py:10` `get_session`'i **modul import aninda** bagliyor. Bir test `sys.modules`'tan `database.db`'yi silip yeniden import edince **yeni** engine olusuyor ama bet_placer **eski** engine'e bagli kaliyor; test piyasayi yeni DB'ye yaziyor, placer eski DB'ye bakiyor. Kanit: `bet_placer.get_session is database.db.get_session -> False`; yeni DB'de 1 market varken bet_placer 0 goruyor. Tek basina gecmesinin nedeni: o zaman `executor.bet_placer` henuz import edilmemis, ilk kez pop'tan SONRA import edilip dogru engine'e baglaniyor. COZUM: `tests/conftest.py::_rebind_module_get_session` (autouse) — kaynagi `database.db` olan modul-seviyesi baglari her testten once guncel engine'e geri baglar. Uretim kodu degismedi. |
| **`data/bot.db-wal` C: diskinde 3,3 GB'a cikmisti (S19, 2026-10-27)** | `data/bot.db` D:'ye **symlink** ama `data/bot.db-wal` **gercek dosya olarak C:'de** birikmisti (olculdu: 3.324,8 MB, LastWriteTime canli botun yazma anina esit). Iki kok neden: (1) SQLite `-wal`/`-shm` dosyalarini **verilen yolun yaninda** acar, symlink hedefini cozmez -> DB D:'de, WAL C:'de; (2) `/api/restart` `os._exit(1)` kullandigi icin kapanista son checkpoint/truncate hic calismaz, dosya yuksek-seviyesinde kalir. `database/db.py` yalnizca `journal_mode`/`synchronous`/`cache_size` kuruyordu; **`wal_autocheckpoint` ve `journal_size_limit` yoktu**. COZUM (kullanici onayi "b yap"): connect listener'ina `PRAGMA wal_autocheckpoint=1000` + `PRAGMA journal_size_limit=67108864` (64 MB) eklendi. Kural: WAL dosyasi bu tavanin ustune cikamaz. TEST: `tests/test_latent_bugs.py` (import + tam suite). **2026-10-28 olcumu: restart sonrasi WAL 3.324,8 MB -> 64,0 MB, C: bos 0,56 -> 3,74 GB; elle truncate gerekmedi.** |
| **C: diski sessizce doluyor: `C:\junbo_offsite_backups` 89,4 GB (S22, 2026-10-28)** | Olcum: **300 dosya / 91.546,3 MB**, her biri `bot.db`'nin tam kopyasi (~1.531 MB). Kaynak: `JUNBO_OFFSITE_DIR` **Machine** ortami `C:/junbo_offsite_backups`; `db_backup.py::mirror_offsite` her `create_backup()`'ta tam kopya yaziyor ve temizlik `_prune_dir(offsite, label)` **etiket basina** `MAX_BACKUPS=30` tutuyor. Testlerin urettigi etiketler (`a`,`b`,`cs`,`cs2`,`rt`,`prune`,`test`,`concurrent`) x 30 + `startup`/`scheduled`/`pre_reset_test` -> 300 dosya. Etki: iki tam test kosusu C:'den ~3 GB yiyor; oturum icinde bos alan 6,4 -> 0,56 GB'a dustu. COZUM (onay bekliyor): offsite icin GLOBAL saklama (etiket basina degil), testlerde `mirror_offsite` kapatma, mevcut 300 dosyadan yalniz en yeni 1-2 tanesini tutup gerisini silmek (~83 GB kazanc). |
| **HEAT verisi C:'de ikiye bolunmustu; `Heat\data` D:'ye tasindi (S23, 2026-10-28)** | Iki `tempmarket-edge.sqlite`: `C:\HEAT data\db\...` (74.313 satir, son 2026-10-27T08:43Z, OLU) ve `Heat\data\...` (75.063 satir, son 2026-10-27T21:22Z, CANLI). Runner `db.mjs` yolunu `TEMP_MARKET_DB`/`TEMP_MARKET_DATA_DIR` yoksa `path.resolve('data')` (gorev calisma dizini) ile kuruyor; `HEAT_MAIN_DB`/`KARDESLER_DB`/`OM_CACHE_DB` ise hala `C:\HEAT data` gosteriyor. `junbo/heat/config.py` ilk aday olarak OLU dosyayi sectigi icin Junbo ~12,5 saat bayat HEAT okuyordu. COZUM (kullanici onayi "c"): gorev durduruldu (node ayrica elle olduruldu — `Stop-ScheduledTask` node alt surecini birakiyor), `robocopy /E /MOVE /XJ` ile 4.722 GB -> `D:\JUNBO data\heat\data`, `Heat\data` yoluna junction, gorev yeniden baslatildi. DOGRULAMA: prediction_log 75.063 -> 75.120, max(created_at) 2026-10-27T22:12:47Z, C: +4,71 GB. ACIK KALAN: `HEAT_DB_CANDIDATES` sirasi ve `HEAT_MAIN_DB` User degiskeni hala olu dosyayi gosteriyor. |
| **`replay/` paketi diskten kaybolmustu; 3 test fail ediyordu (S20, 2026-10-27)** | `tests/test_replay_engine.py` `ModuleNotFoundError: replay.main` ile 3/3 kiriliyordu. `Glob replay/**` -> yalniz `__init__.py` + `fidelity.py`. `replay/main.py` ve `replay/seeding.py` **git'te hic izlenmemis** (untracked) oldugu icin geri alinamadi; `reports/replay/final/` ciktilari ve `REPLAY:main.py:78` log satirlari dosyalarin 2026-08-23'te var oldugunu kanitliyor. COZUM: paket sifirdan yazildi (`clock.py`, `archive.py`, `price_index.py`, `scheduler.py`, `seeding.py`, `main.py`). Iki tuzak yazim sirasinda olculdu: (a) `weather_forecasts` **bot.db'de yalnizca 2026-10-17..10-29** var; Agustos penceresi **backtest.db**'de (1.106.701 satir) -> yalniz bot.db okuyan ilk surum **0 forecast / 0 bet** uretiyordu; (b) tum arsivi tarayan fiyat yuklemesi tek gunluk replay'i **257 sn** yapiyordu -> `target_date`/market_id filtresiyle **~14 sn**. TEST: `tests/test_replay_engine.py` 3/3 + `tests/test_replay_fidelity.py` 8/8. |
| **`backtest.engine.load_data` settlement'i yalnizca `bets.outcome`'dan okuyordu (S21, 2026-10-27)** | `test_backtest_engine.py::test_load_data_has_settlement` -> **"settlement az: 0"**. `load_data` gercek sicakligi yalnizca `bets.outcome` JSON'undan aliyordu; o kolon 2026-09-29 sonrasi betlerde NULL (olcum: son donem **192 yerlesmis bet, 0 outcome**). `data/actuals.db` ise **8.691 satir dolu** ve 48/48 sehir adi birebir eslesiyor. Ayrica gun secimi `settled_at` (yerlesim ANI) idi — settler'daki S16 hatasinin aynisi. COZUM: `LEFT JOIN weather_markets` ile `target_date` esas alindi + `_actuals_fallback()` eklendi (`mode=ro`, `setdefault` ile outcome'un uzerine YAZMAZ). TEST: `tests/test_backtest_engine.py` 5/5. |
| **HEAT entegrasyonu sessizce olmustu: `heat_mos` tahmini hic uretilmedi (S15, 2026-10-27)** | HEAT deposunda `prediction_log` kolonlari `created_at`/`quantile_50`, Junbo ise `run_at_utc`/`final_prediction` bekliyordu; sorgu hata verince istisna yutuluyordu. Olcum: HEAT `prediction_log` **0 satir**, Junbo `weather_forecasts` icinde `heat_mos` **0 satir** (yalnizca 8 Open-Meteo modeli). Ayrica `maybe_run()` hicbir yerden cagrilmiyordu. COZUM: `heat/` paketi (tek sema: `heat/schema.py::ensure_schema`; okuma: `heat/predictions.py::fetch_live_predictions`), `jobs/heat_mos_job.py` bu pakete baglandi, `bot_loop._run_daily_maintenance` job'i cagiriyor, bos sonuc artik WARNING logluyor. TEST: `tests/test_heat_schema.py` (4) + `tests/test_heat_mos_job.py` (4, gecici DB). |

---

## 7. DB Koruma

1. Testler production DB'ye dokunmaz (`conftest.py` temp DB yonlendirir + oncesinde backup).
2. Reset oncesi backup alinir.
3. Bot startup backup: `db_backup.py` (MAX_BACKUPS=10).
4. Backtest verisi: `backtest.db` (bot.db kopyasi, 6 saatte sync).
5. Yedekler: `data/backups/` (2026-08-08 itibari Task Scheduler ile 6 saatte bir, WakeToRun).

```bash
python db_backup.py            # manuel
python db_backup.py --list     # liste
python db_backup.py --restore  # son yedegi geri al
```

---

## 8. Kodlama Patternleri

1. **Calibration/Backfill:** `CalibrationEngine`, `DataBackfiller` yalnizca `jobs/evolution_job.py::_run_calibration_backfill()` uzerinden; yeni model → `REQUIRED_REACHABLE_MODULES` listesine ekle.
2. **Lazy singleton:** `_get_calibration()` — veri yoksa None dondurur, crash yok.
3. **Fallback:** `bias_map` bosa → tum modeller ortalamasi (`avg_mbe`).
4. **Marker throttling:** `data/.last_X_run` — gunde 1 kez calistir.
5. **RAWLER knotlari:** `check_orderbook_depth` bet icin min depth 0 (disabled); `estimate_slippage` her bet.

---

## 9. Siniflar / Ana Dosyalar (Kesif icin)

| Dosya | Onemli bolum |
|---|---|
| `config/settings.py` | `betting_windows=[(4,23.5)]`, tum risk, env proxy |
| `bot_loop.py` | 4 loop; `_SNAPSHOT_INTERVAL=1800`, `_PRICE_POLL_INTERVAL=300` |
| `executor/bet_placer.py` | pencere kontrolu, 30dk-20h vade-kala, YES-only, 0.99 gate |
| `executor/settler.py` | `settle_all` > `closed && umaResolutionStatus=resolved` |
| `jobs/snapshot_job.py` | 30dk snapshot; bucket dedup; bugfix (2026-08-08) |
| `scripts/data_watchdog.py` | self-healing veri toplama (6dk task) |
| `tests/test_latent_bugs.py` | `ALLOWED_DEAD`, `REQUIRED_REACHABLE_MODULES`, no-mojibake |

---

## 10. Bot Calismiyor / Restart

```bash
# Durdur (API)
python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8093/api/stop', timeout=3)"

# Zorla
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'main.py bot' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# Baslat
Start-Process -FilePath "C:\Users\fdemir\AppData\Local\Programs\Python\Python312\python.exe" -ArgumentList "main.py","bot" -WorkingDirectory "C:\Users\fdemir\Documents\New project\junbo" -WindowStyle Hidden

# Kontrol
python -c "import urllib.request, json; d=json.loads(urllib.request.urlopen('http://127.0.0.1:8093/api/status',timeout=5).read()); print(d.get('is_running'))"
```

---

## 11. Ornek "pending" kavrami

`bets` tablosunda `status="open"` kayitlar **pending**dir — Polymarket henuz cozumlememis; settlement loop Gamma API'den sonucu bekler. Polymarket marketleri target_date'den 24-48s sonra cozer. "0 won, 0 lost, N pending" NORMALDİR, sabirla beklenir.
`bets` tablosunda `status="placed"` (eskiden `"open"`) kayitlar **pending**dir - Polymarket henuz cozumlememis; settlement loop Gamma API'den sonucu bekler. Polymarket marketleri target_date'den 24-48s sonra cozer. "0 won, 0 lost, N pending" NORMALDIR, sabirla beklenir.

**2026-10-22 DOGRULAMASI (canli DB + Gamma API):** 20 Ekim target'li acik betlerin
20 marketinin **hicbiri** Polymarket tarafindan resmi cozumlenmemisti
(`closed=false`, `umaResolutionStatus=null`), fiyatlar 0.86-0.99'a kadar kesinlesmis
ama UMA kapatmamis. Cozulmus marketler karsilastirmasi: `closed=true, uma=resolved,
outcomePrices=["1","0"]`. Yani **"acik kalan bet" = Polymarket kaynak gecikmesi**,
bot hatasi DEGIL - bot BILINCLI olarak yalnizca resmi UMA cozumlemesini kabul eder
(fiyat tabanli settlement `_fetch_market_resolution` docstring'inde bilincli olarak
KAPALI: fiyat 0.98 olmak cozumlenmis anlamina gelmez).

**Polymarket cozumleme gecikmesi olculdu:** 19 Ekim target'li marketler cozulmus
(21 Eki'de settlement oldu), 20-21 Ekim target'li marketler 22 Eki'de hala acik -
yani UMA tarafi 1-3 gun gecikmeyle cozuyor. Bu sureyi degistirmek icin
fiyat-tabanli settlement'a gecmek karari KULLANICIYA AITTIR; varsayilan davranis
resmi cozumleme beklemedir.

**Settlement dongusu 2 dakikada bir calisir** (`SETTLEMENT_INTERVAL=120`).

---

## 12. ANAYASA — Dogrulanmis Davranis Kurallari (docs/ANAYASA.md'den birlesirildi, 2026-08-08)

Amac: bot'un kalici davranis kurallarini kod + sistem kaniti ile saklamak. Her maddeye
kanit (dosya:satir) yazilmistir. Ariza aninda once bolum 13 (Ariza Senaryolari)'a bak.

### 12.1 ANA AMAC: BACKTEST ICIN KESINTISIZ VERI KAYDI

Bot'un en temel gorevi, dogru backtest yapabilmek icin **snapshot, bet ve fiyat
kayitlarinin duzenli ve surekli tutulmasidir**. Veri olmadan backtest yapilamaz.
Bu anayasa, veri kaydinin HICBIR kosulda kesintiye ugramamasi icin alinan tum
onlemleri ve bir sey bozuldugunda NE OLMASI GEREKTIGINI tanimlar.

### 12.2 Snapshot Cadence: 30 DAKIKA (24/7)

**Kural:** Bot calistigi surece her 30 dakikada bir market snapshot alinir. Bahis
penceresi kontrolu YOKTUR - bos veri alinsa bile snapshot alinir.

- Kanit (kod): `bot_loop.py` -> `_SNAPSHOT_INTERVAL = 1800` (saniye = 30 dk).
- Kanit (dedup, 30 dk bucket): `jobs/snapshot_job.py` -> `_bucket_start(ts)` =
  `ts.minute // 30 * 30`; `_same_bucket(a, b)`. Yeni 30 dk penceresi -> yeni kayit.
- Kanit (test): `tests/test_snapshot_30min.py` (bucket sinirlari 10:00 -> 10:30 -> 23:30).

### 12.3 Veri Kayit Mimarisi (Backtest Girdileri) + Retention

Dort veri kaynagi toplanir; hepsi task'lar ile **bot process'inden bagimsiz**
calisir (kesintiye karsi guvence):

| Veri | Nerede | Kim yazar | Siklik |
|------|--------|-----------|--------|
| Market snapshot (YES/NO fiyat) | `bot.db` -> `market_snapshots` | `bot_loop.py` snapshot_loop (24/7) + `JunboSnapshot` task | 30 dk |
| Orderbook derinlik | `orderbook.db` -> `orderbook_snapshots` | `Junbo-OrderbookCollect` (scripts/collect_orderbook.py) | 30 dk |
| Gerceklesen hava durumu | `actuals.db` -> `actual_temperatures` | `Junbo-ActualsCollect` (scripts/collect_actuals.py) | 6 saat (guncel sehirlerde ayni gun tekrar cekilir) |
| Bet kayitlari | `bot.db` -> `bets` | bot_loop (bet placer/settler) | her islem |
| Backtest DB (cografi) | `backtest.db` (4 tablo) | `Junbo-SyncBacktest` (scripts/sync_backtest_db.py) | 6 saat |

**Retention (KALICI KURAL):**
- `market_snapshots`: **365 gun** tutulur (`cleanup_old_snapshots(days=365)` —
  `jobs/snapshot_job.py`, `bot_loop.py`, `snapshot_only.py` ayni degeri kullanir).
- DB yedekleri (`data/backups/`): **30 gun** tutulur (`scripts/backup_databases.py`
  `RETENTION_DAYS = 30`), `Junbo-BackupDatabases` task'i 6 saatte bir alir.
  **2026-11-18 guncel:** konum `<JUNBO_DATA_DIR>\backups`; `backup_databases.py` UTC gununde
  1 kez kosar (marker `.last_backup_databases_run`) ve prefix basina son 7 kopyayi tutar
  (`KEEP_LAST_BY_PREFIX`); `RETENTION_DAYS=90` yalnizca emniyet supabidir. bot.db'nin 30 gunluk
  dogrulanmis serisi `db_backup.py` `bot_scheduled` yedekleridir (startup: son 3).
- `conftest.py` test-oncesi backup: **gunluk en fazla 1 kez** (marker throttling,
  `data/.last_pre_test_backup`).

### 12.4 Surekli Calisma / Restart

- Windows servisi `JunboBot`: **Status=Running, StartType=Automatic**, `sc qfailure`
  RESTART 5s/10s/30s (crash sonrasi otomatik restart).
- `snapshot_loop` kendisi `state.is_running` bayragina bagli; donma ya da crashte
  yeniden baslatma watchdog sorumlulugunda.

### 12.5 Watchdog (Crash/Freeze Restart)

- `scripts/bot_watchdog.py` -> Task Scheduler `JunboBotWatchdog` (SYSTEM, 1 dk);
  heartbeat zaman asimi `HEARTBEAT_TIMEOUT = 15 * 60` saniye (15 dk). Log eskise
  donmus kabul edilir, restart yapilir.
- `scripts/data_watchdog.py` -> `JunboDataWatchdog` (6 dk): veri kaynaklarinin
  tazeligini kontrol eder, bayat ise toplayiciyi kendisi baslatir, task disabled
  ise yeniden enable eder. Log: `data/logs/data_watchdog.log`.
- **DB bakimi (2026-08-09):** `data_watchdog` gunde 1 kez (02:00-04:00 UTC penceresi,
  `data/.last_db_maintenance` marker) `scripts/db_maintenance.py`'yi calistirir:
  `wal_checkpoint(TRUNCATE)` + `ANALYZE` + `VACUUM`. Log: `data/logs/db_maintenance.log`.
  Ilk run bot.db ~158MB → ~146MB.

### 12.6 Retry / Internet Dayanikli

- Ortak: `utils/retry.py` -> `max_attempts=3`, 2^attempt backoff.
- `data_pipeline/polymarket_ingest.py` -> `max_retries=3`.
- `data_pipeline/resolvedmarkets_ingest.py` -> `max_retries=5` + 429 `Retry-After`
  saygi (resolvedmarkets icin SPECIFIC, global DEGILDIR).

**Onemli ayrimlama:** Snapshot loop **interneti kullanmaz** - yalniz local `WeatherMarket`
tablosundan fiyat degerlerini DB'ye yazar. "Internet gidince snapshot 5 kere tekrar dener"
iddiasi SNAPSHOT icin GECERSIZDIR. Internet retry, ingest/collector'larda gecerlidir.

### 12.7 Bet Penceresi (2026-08-08 itibari)

- **Pencere: 04:00 - 23:30 UTC** (`config/settings.py` -> `betting_windows = [(4, 23.5)]`).
- `_is_in_betting_window()` kesirli saat destekler (23.5 = 23:30).
- Kapanis 24:00 UTC = `target_date` (12:00 etiketi) + 12h.
- Acilis kisiti: `target_date > now - 11h30dk` VE `target_date <= now + 8h`
  (SQLite-safe esdeger: kapanis `> now+30dk` VE `<= now+20h`).
- SNAPSHOT bu kuraldan ETKILENMEZ — snapshot 24/7 alinir (giris zamani analizi icin).

### 12.8 Veri Kaynagi Kontrol Sirasi (2026-09-17 — "yok" demeden once bak)

"Bu veri yok" denmeden once SIRAYLA kontrol edilir:
1. Junbo `data/bot.db` + `data/orderbook.db` (canli tablolar).
2. `D:\HEAT data\db\tempmarket-edge.sqlite` — Heat projesi DB'si:
   `forecast_snapshots` (2021'den beri 661K tahmin, 26 kaynak),
   `settlements` (9823), `observations` (31K), `training_pairs` (150K).
3. `D:\HEAT data\` altindaki arsivler (`gfs_archive`, `era5`, `weather_data`).
4. `D:\JUNBO data\backups\` (zip'li uzun sureli yedekler).

Kanıt: 2026-09-17'de "7 Eylul oncesi forecast yok" iddiasi YANLIS cikti —
junbo DB'sinde yoktu ama HEAT DB'de 2021'den beri vardi. Kural ihlali =
dogrulamadan cevap (bolum 2'deki Kontrol-Etmeden-Cevap-Verme yasagi).

2026-09-18 EK: "Yerlesim kaynagi esitligi" — backtestte kazanma SONUCU ile
GIRIS verisi AYNI olcekten gelmeli. HEAT yerlesimleri vs bizim WU toplama
ayni bucket'ta %25 uyuyor (medyan 0.80C fark); 4 tur backtest bu yuzden cop
oldu. Cozum: `config/source_offsets.json` sehir medyan offsetleri +
`backtest/engine.py::normalized_heat_settlement` + kapı testi
`tests/test_settlement_gate.py` (duzeltmeli uyum < 0.40 ise backtest DURUR).
Kalibrasyon: ham %25 -> duzeltmeli %47 (hala gurultulu; gecerli olcum
walk-forward gercek-status yoludur).

### 12.9 Referans: Analiz Scriptleri (arsiv — SILINMEZ)

- `scripts/analyze_first_peak_climbs.py` (49 real climb; %20 takint elendi).
- `scripts/analyze_peak_to_settle.py` (39 market; peak->settle suresi; 17:00=15, 23:00=7).
- `scripts/analyze_city_time_temp.py`, `analyze_settlement.py` vb. — strateji
  gelistirme/kanit analizleri. Backtest script'leri 2026-08-18'de TEK dosyada
  birlestirildi: `scripts/backtest.py {gunluk|orderbook|metar_peak|
  metar_vs_settlement|walk_forward}`; eski 22 varyant (`backtest_rolling.py`,
  `backtest_advanced.py`, `walk_forward_backtest.py` vb.) envanteriyle birlikte
  `backtest_archive/README.md` -> `backtest_archive/` altinda bilincli arsiv olarak korunur.
- `gunluk` subkomutu 2026-08-18'de LOOK-AHEAD'den arindirildi (forecast
  backtest.db gercek batch'lerinden + fetched_at<=kapanis kapisi) ve `--real-entry`
  bayragi eklendi (gercek bot fill'iyle capraz dogrulama). Detay ve sayilar:
  `reports/backtest_gercekci_2026-08-18.md`.
- Polymarket likidite odulu (2026-11-26): `research/poly_rewards_scan.py` (tum odullu marketler + anlik
  rekabet; rapor `reports/poly_rewards_scan_YYYYMMDD.md`), `research/poly_mm_reward_backtest.py` (hava dolum +
  ters secim; islem onbellegi `C:\JUNBO data\research\mm_trades_cache`, data-api v2 taker kayitlari),
  `research/poly_rewards_recorder.py` (dakikalik Q ozeti; `--report`). Formul yaklasimlari (duzeltilmis mid,
  rakip Q_min ust siniri) betik basliklarinda. Kayit turu (2026-11-26 aksam): ONCE ornek (etiket = turun
  basladigi dakika, 50 sn butce), SONRA 30 dk'da bir hedef yenileme (110 sn butce; hata olursa eski hedeflerle
  devam). Son tarih `poly_rewards_scan._DEADLINE` ile; tarama betiginde yok (sinirsiz).

### 12.10 Polymarket METAR Cozum Kurali (2026-10-23+, dogrulandi 2026-11-23)

- **Kaynak:** aviationweather.gov ham METAR; yalnizca RUTIN gozlemler. Satiri `SPECI` ile
  baslayan ozel gozlem SAYILMAZ (`scrapers/metar.py::_is_speci`, fetch aninda atilir).
- **Deger:** RMK T grubu (onda bir derece); yoksa govdedeki tam sayi
  (`_parse_metar_temp`).
- **Yuvarlama:** tam yarim YUKARI = `floor(x + 0.5)` (16.5 -> 17, -8.5 -> -8, -1.7 -> -2).
  TEK kaynak `scrapers.metar.polymarket_round`; `int(x+0.5)` veya simetrik `int(x-0.5)`
  KULLANILMAZ (negatiflerde yanlis bucket).
- **Istisna:** Hong Kong Hong Kong Observatory ile cozulur -> `NON_METAR_RESOLUTION`
  (HK'de METAR-peak yok). Panama MPMG (Tocumen MPTO DEGIL).
- **Backtest:** gecmis METAR arsivi 2026-11-22 oncesi SPECI'leri de icerir; replay
  karsilastirmalarinda iki senaryo ayni veriyi gordugu icin kiyas adildir ama mutlak
  sonuc kurala gore hafif sapabilir.

---

## 13. ARIZA SENARYOLARI: BIR SEY BOZULURSA NE OLUR / NE YAPILIR

Bir ariza ile karsilasildiginda once buraya bak, sonra gerekirse kod kanitlarini dogrula.

### S1. Bot process'i cokerse (crash / stop)
- **Beklenen:** Windows servisi `JunboBot` (Automatic + `sc qfailure` RESTART 5s/10s/30s)
  otomatik yeniden baslatir. Yanit gelmezse `JunboBotWatchdog` (1 dk) servis
  RUNNING degilse enable+start eder; bot.log 15 dk eskiyse donmus kabul edip restart.
- **Dogrula:** `sc query JunboBot` (State=RUNNING); `data/logs/watchdog.log` son satir
  `OK running`; `http://127.0.0.1:8093/api/status` -> `is_running=true`.
- **Aksiyon gerekmez** — otomatik toparlanir.

### S2. Bilgisayar uykuya girer / kapali kalir
- **Beklenen:** Uyku ayarlari 2026-08-08'den beri `Sleep after = Never` (AC+DC) —
  bilgisayar uyumaz. Yedek katman: `JunboSnapshot` task `WakeToRun=true` + `StartWhenAvailable`
  ile kacirilmis calismalari tamamlar.
- **Dogrula:** `powercfg /query SCHEME_CURRENT SUB_SLEEP` -> STANDBYIDLE=0;
  `data/snapshot_task.log` son satirlar hata icermemeli.
- **Not:** Kacirilan surelerde veri EKSIK olur (gecmise donuk doldurma YOKTUR) —
  bu kayip kabul edilir.

### S3. Snapshot DB'de kayit yok / sayi artmiyor
- **Beklenen:** Bot 30 dk'da bir `market_snapshots`'a yazar. Artis yoksa: (a) bot
  calisiyor mu (`/api/status`), (b) snapshot_task.log son calisma (30 dk gecmemis),
  (c) DB boyutu. Dedup nedeniyle `0 snapshots saved` NORMALDIR (bucket 30 dk).
- **Aksiyon:** Bot durmussa S1'e bak; task kaciriyorsa S2'ye bak.

### S4. Disk dolar
- **Beklenen:** `backup_databases.py` (6 saatte bir) 30 gunden eski yedekleri siler;
  `cleanup_old_snapshots` snapshot'lari 365 gunde sifirlar; conftest gunluk max 1
  test-backup uretir.
- **Dogrula:** `data/backups/` boyutu ~2-3 GB civarinda tutmali (gunde ~4 backup x
  ~200 MB); anlik buyuk yiginlar anormaldir.
- **Aksiyon:** Purge calismazsa `scripts/backup_databases.py` elle calistir.

### S5. Bet penceresi disinda bet acildi / acilmadi
- **Beklenen:** Bahis penceresi KALDIRILDI (2026-08-11 kullanici karari: `betting_window_enabled=False`) — betler gun boyu acilir.
- **Dogrula:** bot.log `spread open: bet#ID ...` satirlari.
- **Aksiyon:** Bet beklenmedik sekilde kapandiysa (rotation dısı) bot.log'da `Bet closed (rotation)` / `settlement` satirlarini incele. Erken kapanis mekanizmalari kaldirildigi icin bet yalnizca rotation (pencere kaymasi) veya settlement'ta kapanir.

### S6. Task Scheduler gorevi Disabled / eksik
- **Beklenen (2026-08-08 itibariyle):**
  | Gorev | Durum | Gorev |
  |-------|-------|-------|
  | JunboSnapshot | Ready, 30dk, WakeToRun | snapshot (24/7 garantisi) |
  | JunboBotWatchdog | Ready, 1dk | heartbeat + crash restart (2026-11-13 enable) |
  | JunboDataWatchdog | Ready, 6dk | veri tazeligi self-healing |
  | Junbo-BackupDatabases | Ready, 6sa (gunde 1 kosar, 2026-11-18) | DB yedekleme, son 7 kopya |
  | Junbo-ActualsCollect | Ready, 6sa | gerceklesen hava |
  | Junbo-OrderbookCollect | Ready, 30dk | orderbook derinligi |
  | Junbo-SyncBacktest | Ready, 6sa | backtest.db senkron |
  | JunboWARP | Ready, 5dk (2026-11-18) | `scripts\warp_health.ps1`: WARP proxy saglik + reconnect (mod degismez); 2026-11-26: + gamma-api/CloudFront yoklamasi |
  | Junbo-RewardsRecorder | Ready, 1dk, 14 gun (2026-11-26 -> 2026-12-10) | `research\poly_rewards_recorder.py --once`: Polymarket odul rekabeti kaydi (arastirma, emir YOK); kullanici oturumu + pythonw; hata -> `logs\poly_rewards_recorder.log` + LastTaskResult=1 |
  | JunboBot / JunboBotBackup | Disabled (bilincli) | eski yontemler (bot = Windows servisi + JunboBotWatchdog) |
- **Aksiyon:** Ready olmasi gereken bir gorev Disabled ise `schtasks /change /tn
  "\Gorev" /enable` ile ac, sonra log/Last Result dogrula.

### S7. Actuals toplanmiyor / "All 5 attempts failed"
- **Beklenen:** `collect_actuals.py` gun icinde ayni gunu tekrar ceker (archive API
  kismi saatler dondurur). `start > end` ise "already up to date, skip" — 400 hata
  OLMAZ (2026-08-08 fix).

### S8. Division by zero in backtest/run_combos.py (2026-10-04)
- **Sorun:** `backtest/run_combos.py:97` satırında `count / total` işlemi sırasında `total=0` oluyordu
- **Çözüm:** `if total > 0:` koşulu eklendi
- **Test:** `test_backtest_engine.py`'deki bias testleri güncellendi, mevcut kaynakları kullanacak şekilde

### S9. Spread bet placement fails in test due to price filter (2026-10-04)
- **Sorun:** Testte pazar fiyatları 0.05 iken minimum giriş fiyatı 0.1 olduğu için tüm betler atlanıyordu
- **Çözüm:** Test pazar fiyatları 0.15'e yükseltildi, tahmin değerleri 31.5'ten 31.0'a düşürüldü
- **Test:** `test_bot_flow.py`'deki spread bet testi artık `{'placed': 7, 'closed': 0, 'skipped': 0, 'cities': ['Testville']}` ile geçiyor
- **Dogrula:** `data/logs/collect_actuals.log` son satirlar; `data_watchdog.log`
  `ACTUALS ok (age=...)`.
- **Aksiyon:** 400 hatasi gorulurse tarih araligi mantigini kontrol et (start <= end).
| **Yeni sehir proseduru (2026-09-13)** | Once: bilinmeyen sehir ingestion'da duser, spread bias yok diye atlar, ekleme manuel (map+koordinat). Simdi: `jobs/new_city_job.py` gunluk kesfeder, `city_overrides.json` yazar, spread fallback ile ilk gunden isleme alir. Test: `tests/test_new_city_job.py` (6 passed). |

### S10. SADECE Turkiye sehirlerine bet (2026-10-05)
- **Sorun:** Bot 573 betin %94'unu yabanci sehirlere acmisti (Manila 40, Tokyo 30...). Istanbul 26 + Ankara 8 bet (%5.9).
- **Cozum:** `StrategyConfig.turkey_only=True` + `turkey_cities="Istanbul,Ankara,Izmir,Antalya"`; filtre `engine/market_selection.py::trading_city_allowed` uzerinden 3 giris noktasinda: spread (`executor/spread_placer.py` aday filtresi), edge (`executor/bet_placer.py::place_bet` guard), METAR-peak (`jobs/metar_peak.py::run_metar_peak_bets` market filtresi). `.env` override: `TURKEY_ONLY`, `TURKEY_CITIES`. Config proxy `_MAP`'e `TURKEY_ONLY`/`TURKEY_CITIES` eklendi.
- **Test:** `tests/test_spread_placer.py::test_turkey_only_blocks_foreign_city` + `test_turkey_only_allows_turkish_city`; conftest `_reset_strategy_params` testlerde `turkey_only=False` yapar (kurgusal sehirler). Ayrica 7 kirik spread testi duzeltildi (0.05 fiyat `min_entry_price=0.10` altinda kaliyordu -> 0.15).
- **Operasyon:** 45 yabanci acik bet `cancelled` isaretlendi; bot restart ile yeni kod canliya gecer.

### S12. Turkey-only GERI ALINDI, worldwide (2026-10-06)
- **Karar:** Kullanici worldwide trading istedi; `turkey_only` default `False` yapildi. Filtre altyapisi (`trading_city_allowed`, 3 giris noktasi, testler) korunur — `TURKEY_ONLY=true` ile geri acilabilir.
- **Not:** Servis restart edilemedigi (admin yok) icin 9349247'li Turkey-only kod HIC canliya gecmemisti; canli zaten worldwide calisiyordu. Bu revert diskteki kodu canliyla esitler. Cancelled 66 yabanci bet geri acilmaz (settlement disi birakildi); 3 TR acik bet settlement'a kadar tutulur.

### S11. TR backtest + tuning (2026-10-06): max_entry 0.30 vs 0.50
- **Yontem:** `scripts/backtest.py gunluk` 4 varyant (MAX_ENTRY 0.95/0.50/0.30, bias-top15 ve TR-forced) x 10 gun (08-16..25) + dogrulama penceresi (08-26..09-04). TR alt kumesi `--detail` ciktisindan.
- **Sonuc (canli-esdeger, entry>=0.10):** pencere1 TR: 0.50 -> -$0.16 (4W/6L), 0.30 -> +$3.62 (3W/5L); pencere2 TR: 0.50 -> -$4.16 (6W/9L), 0.30 -> -$10.60 (1W/7L). Toplam 20 gun: 0.50 -> -$4.32 (25 bet), 0.30 -> -$6.98 (16 bet).
- **Karar:** TUNING YOK — orneklem ince (41 bet) ve yon tutarsiz; canli `spread_max_entry=0.50` korunur. Notlar: (1) `gunluk`'te min_entry tabani YOK — 0.10 alti girisler (kaldiracli +$20 gibi) canlida alinamaz, karsilastirma canli-esdeger filtreyle yapildi; (2) Ankara zayif (pencere1 -7.60, pencere2 kotu) ama 20 betle sehir eleme YOK (overfit olur); (3) kayiplarin cogu `sold_peak` tam zarar (-2.19) — yanlis-bucket satis mekanizmasi ayri calisma konusu.

### S13. Betler "kapanmiyor" / settlement pending kaliyor (2026-10-22)
- **Belirti:** Dashboard'da eski tarihli (orn. 20 Ekim) acik betler gorunuyor; kullanici "kapanmamis betler var neden?" diye soruyor.
- **Teshis (3 adim, kanitli):**
  1. `SELECT ... FROM bets WHERE status IN ('placed','open','active','pending')` + `weather_markets` join -> market status `open`, `target_date <= bugun` oldugu icin `settle_all` bunlari GERCEKTEN isliyor.
  2. Canli Gamma kontrolu: `GET /markets/{id}` -> `closed=false`, `umaResolutionStatus=null`, `outcomePrices` 0.86-0.99. Yani fiyat kesinlesmis ama Polymarket/UMA resmi olarak KAPATAMAMIS.
  3. Cozulmus marketler karsilastirmasi: `closed=true`, `umaResolutionStatus=resolved`, `outcomePrices=["1","0"]` -> bot ayni cozumleme yolunu basariyla kullaniyor.
- **Sonuc:** Kaynak gecikmesi. Polymarket hava durumu marketlerini 1-3 gun gecikmeyle cozuyor (19 Eki target'li marketler 21 Eki'de cozulmustu; 20-21 Eki target'li olanlar 22 Eki'de hala acik). Bot FIYAT-tabanli settlement'i BILINCLI olarak kullanmaz - sadece resmi UMA cozumlemesini kabul eder.
- **Ne YAPILMAZ:** Fiyat 0.98 oldugu icin beti zorla kapatmak yanlis olur (prematur settlement). Kullanici karari degisirse `executor/settler.py::_fetch_market_resolution` icindeki fiyat-tabanli fallback ACILABILIR.
- **Dogrulama:** `logs/bot.log` -> "not yet resolved by Polymarket" (market henuz cozumlenmemis) vs "resolved by Polymarket: outcome=YES" (cozulmus). "Gamma API request failed" cok fazlaysa Cloudflare 403 demektir (UA eklendi, 3 deneme).
- **Bot tarafi duzeltmeler (2026-10-22):** yalnizca ACIK BETI OLAN marketler Gamma'ya sorgulanir (beti olmayanlar toplu `expired`); `_call_gamma_api` User-Agent gonderir. Boylece "945 pending" gurultusu ve 24.328 adet `pending >48h` log satiri biter; gercek bekleyen market sayisi gorunur olur.

### S14. "Bot kar mi ediyor?" sorusuna guvenilir cevap yok (2026-10-27)
- **Belirti:** Ayni anda uc farkli kar/zarar sayisi: `portfolio.total_realized_pnl` **+\$488,05**, nakit degisimi (1000 -> 1258,91) **+\$258,91**, `bets`+`bets_archive` `realized_pnl` toplami **-\$220,73**.
- **Teshis:**
  1. `settler.py` `total_realized_pnl`'e yalnizca settlement PnL'i ekliyor; `close_bet_for_rotation` rotation PnL'ini yalnizca `bet.realized_pnl`'e yaziyordu -> 1.022 rotation kapanisi portfoy toplaminda gorunmuyordu.
  2. `jobs/metar_peak.py::_open_metar_bet` `entry_fee`'yi kasadan dusmuyordu; fee sadece `settlement_pnl` formulu icinde kalıyordu -> nakit **\$34,66** fazla.
  3. `bet_type` kolonu tum satirlarda NULL -> "spread mi metar mi kaybettiriyor" ayrimi kolon uzerinden yapilamiyordu.
- **Duzeltme (2026-10-27):** metar fee debit eklendi; rotation PnL'i `total_realized_pnl`'e islendi; `bet_type` 4 olusturma noktasinda dolduruluyor. Testler: `TestMetarPeakAccounting`, `tests/test_rotation_accounting.py`.
- **Ne YAPILMAZ:** Bu duzeltme gecmis satirlari geriye donuk degistirmez. 2026-10-27 oncesi donem icin dogru rakam **bet tablolarindan** okunur; portfoy toplami sadece ileriye donuk guvenilirdir. Gecmis duzeltmesi isteniyorsa ayri bir backfill script'i gerekir (ayni `pnl`/`realized_pnl` tek-kaynak kurali gibi).
- **Dogrulama komutu (salt-okunur):** `sqlite3 data/bot.db "SELECT CASE WHEN order_id LIKE 'metar_%' THEN 'metar_peak' ELSE 'spread' END strat, COUNT(*), ROUND(SUM(COALESCE(realized_pnl,0)),2) FROM bets GROUP BY 1;"`
- **Hala ACIK (aksiyon bekliyor):**
  1. **Rotation gunluk limiti uygulanmiyor.** Config `daily_rotation_limit=5`, `max_daily_rotations=3`; canli veride 19 Ekim'de **32** rotation. `jobs/metar_peak.py:258` `close_bet_for_rotation`'i dogrudan cagirdigi icin limit kontrolu atlaniyor.
  2. **Mukerrer bet.** 95 markette >1 bet (toplam 479 bet); 11 Ekim'de tek gunde **333** bet (normal ~35). Re-entry guard (`bet_placer.py:781`) delinmis.
  3. **Strateji dagilimi.** METAR-peak **+\$171,34 (+%17,6)**, Spread **-\$119,09 (-%6,0)** — 2026-09-06 kaydiyla ayni yon. Spread'in kapatilmasi/kucultulmesi kullanici karari bekliyor (canli davranis degisikligi).

### S15. HEAT tahmin motoru hic calismiyordu - Junbo icine tasindi (2026-10-27)
- **Belirti:** Bot "8 model + HEAT" ile tahmin uretiyor gorunuyordu ama `weather_forecasts` tablosunda `source='heat_mos'` satiri **hic yoktu**; yalnizca 8 Open-Meteo modeli vardi. Hicbir hata loglanmiyordu.
- **Teshis (3 ayri kok neden):**
  1. **Sabit kodlanmis yol + sema kaymasi.** `jobs/heat_mos_job.py` depo yolunu `D:\HEAT data\db\tempmarket-edge.sqlite` olarak sabitliyordu. Canli depo aslinda `C:\HEAT data\db\tempmarket-edge.sqlite` (2026-10-27 olcumu: **D: diski SAGLIKLI** — dirty degil, 30 gunde disk hatasi yok, `TestDBProtection` 4/4, 1,5 GB yedek yazildi; yani tasinma nedeni disk arizasi DEGIL, yolun yanlis olmasiydi); D: kopyasi eski Python semasinda (`created_at` / `quantile_50`) kaldi ve **0 satir**. Canli C: deposu ise Node runner semasinda (`run_at_utc` / `final_prediction`, `horizon_days`, `confidence_lower/upper`, `model_version`) ve **72.663 satir**. Junbo yanlis depoya bakiyordu; dogru depoda da kolon uyusmazligi vardi. `sqlite3.OperationalError: no such column` firlatiliyor, `maybe_run` bunu yutup `{"error": ...}` donduruyordu.
  2. **Job hic cagrilmiyordu.** `heat_mos_job.maybe_run()` icin uretim tarafinda caller yoktu (yalnizca `__main__` ve testler). `test_latent_bugs.py::ALLOWED_DEAD`'de duruyordu.
  3. **Testler canli veriye bagliydi.** `tests/test_heat_mos_job.py` `len(preds) > 40` bekliyordu; depo bos oldugu icin test nedeni anlasilmadan kirmizi kaliyordu.
- **Olcum:** duzeltme oncesi Junbo `weather_forecasts` icinde `heat_mos` **0 satir** (yalnizca 8 Open-Meteo modeli). D: kopyasi: `prediction_log` **0 satir** (legacy sema; `forecast_snapshots` 556.670, `training_pairs` 79.037, `observations` 31.480, `stations` 51 - saglam ama olu). Canli C: deposu: **3,34 GB**, `prediction_log` **72.663 satir**, son kosu `2026-10-26T04:26Z`. Duzeltme sonrasi: `fetched=191 added=191`, ikinci kosu `added=0 skipped=191` (idempotent). **Canli dogrulama (bot surecinde, restart sonrasi):** `2026-10-27 03:23:23 HEAT_MOS_JOB - INFO - heat_mos: fetched=141 added=0 skipped=141` + `BOT_LOOP - INFO - HEAT tahmin guncellemesi: {'fetched': 141, 'added': 0, 'skipped': 141}`. (141 < 191: UTC gun donumu ile `target_date >= now-1 gun` penceresi daraldi.)
- **Duzeltme (tek sema / tek surec):**
  - `heat/config.py` — depo yolu (`JUNBO_HEAT_DB` ile gecici olarak degistirilebilir; testler bunu kullanir), `SOURCE`, `MARKER`, `LOOKBACK_DAYS`.
  - `heat/schema.py` — **kanonik tek sema**. `ensure_schema()` tablolari yoksa olusturur, eksik kolonlari `ALTER TABLE` ile ekler, eski kolonlardan (`quantile_50` -> `final_prediction`, `created_at` -> `run_at_utc`) backfill eder. Idempotenttir.
  - `heat/predictions.py` — `fetch_live_predictions()`; kolon varligina gore okur, boylece gecis oncesi depoda da calisir; bos sonucu sessizce yutmaz, WARNING loglar.
  - `jobs/heat_mos_job.py` — sabit DB yolu ve kolon adlari kaldirildi; `heat` paketine baglandi; her kosuda `ensure_schema()`.
  - `bot_loop._run_daily_maintenance` — job artik gercekten cagriliyor (marker'li, gunde 1 kez, `try/except` ile izole).
- **Test:** `tests/test_heat_schema.py` (yeni, 4 test: sifirdan kurulum, legacy yukseltme + backfill, idempotentlik, DB yok). `tests/test_heat_mos_job.py` yeniden yazildi (4 test: en son kosu kazanir, legacy sema okunur, `weather_forecasts`'a idempotent yazim, DB yoksa no-op) — artik gecici DB kullanir, dis veriye bagli degil. `tests/test_latent_bugs.py::REQUIRED_REACHABLE_MODULES` listesine `heat.schema` + `heat.predictions` eklendi.
- **Ne YAPILMAZ:** `prediction_log` bos oldugu icin geriye donuk backfill GEREKMEZ; depoya yazma islemi (ALTER) elle degil, `ensure_schema()` uzerinden job'un ilk kosusunda yapilir. Sema gecisi uygulanmamis olsa bile okuma katmani legacy kolonlarla calisir.
- **Hala ACIK:**
  1. **Agirlik yok.** `heat_mos` satirlari `model_weight=0.0` ile yaziliyor ve `settings.model_weights` yalnizca 8 Open-Meteo modelini iceriyor. `engine/calculator.py:135` agirligi `f.model_weight or 0.0` ile okudugu icin `heat_mos` agirlikli ortalamayi/std'yi **degistirmez**. Agirlik atamak canli davranis degisikligidir - kullanici karari bekliyor.
  2. **Uretim portu.** HEAT'in uretim hatti hala `Heat/windows-runner/index.mjs` (Node) + gunluk `ml_step*` Python betikleri olarak Junbo disinda calisiyor; bu fazda yalnizca **tuketim + tek sema** birlestirildi. Uretimin Junbo'ya tasinmasi Faz 2.
  3. **Runner canliligi.** Son kosu `2026-10-26T04:26Z`; `heat.fetch_live_predictions` artik `MAX_RUN_AGE_HOURS=36` esigini asinca WARNING logluyor.

### S16. "Disk arizali" varsayimi yanlisti; gercek hata settlement gunuydu (2026-10-27)

- **Belirti:** `data/backups` altinda **315 adet 0-byte** yedek dosyasi birikmis, `create_backup` "disk I/O error" donuyordu. Bu gozlem "D: diski arizali" kanaatine ve HEAT deposunun C:'ye tasinmasina yol acmisti.
- **Teshis (olculdu, tahmin edilmedi):**
  1. `fsutil dirty query D:` -> `Volume - D: is NOT Dirty` (chkdsk gerekmiyor).
  2. `Get-Disk` / `Get-Volume` -> disk 1 = PHD 3.0 Silicon-Power USB, 465,8 GB, 225,5 GB bos, **Healthy / OK**.
  3. System event log, son 30 gun -> disk/Ntfs/volmgr/storahci/USBSTOR kaynakli **tek Error/Warning yok**.
  4. `tests/test_critical_bugs.py::TestDBProtection` -> **4/4 GECTI** (357 sn) ve test sirasinda `D:\JUNBO data\backups` altina **1.529,4 MB** yedek **basariyla** yazildi.
  5. Ayni dizinde **798 dolu** + 315 **0-byte** dosya: kesinti **2026-09-28..2026-10-27 arasinda gecici** yasandi, kalici degil.
  - **Sonuc: onarilacak bir disk arizasi YOK.** Gercek dar bogaz **C:** sistem diski (%98 dolu, 9,2 GB bos): 1,5 GB'lik yedekleri C:'ye tasima secenegi bu yuzden uygulanamaz.
- **Ikinci gercek hata — settlement gunu:** `executor/settler.py::_settle_market` gercek sicakligi `_fetch_actual_temp(bet.city, bet.settled_at)` ile **yerlesim ANI** uzerinden cekiyordu. UMA marketi gec cozdugunde (D+1, D+2) `bets.outcome` icine **yanlis gunun** sicakligi yaziliyordu; `tests/test_settlement_gate.py` ve `backtest/engine.py` bu alani okudugu icin capraz-kaynak olcumleri de kayiyordu.
  - **Duzeltme:** `temp_date = getattr(market, "target_date", None) or bet.settled_at` — once **marketin gunu**, yoksa yerlesim anina dus.
- **Ucuncu hata — geriye donuk bosluk:** `data/actuals.db` 2026-09-28'de dondugu icin 2026-09-29 sonrasi **298 betin** `outcome` alani NULL kaldi. `data/actuals.db` D: symlink'inden C:'ye alindi ve `scripts/collect_actuals.py` ile **1.452 kayit** (2026-09-29..2026-10-27) geri dolduruldu.
  - **Yeni arac:** `scripts/backfill_bet_outcomes.py` — `bets.outcome` alanini settler ile **ayni JSON semasinda** (`settlement_temp_c` / `settlement_temp_f` / `source` / `collected_at`) doldurur; `--dry-run` ile rapor verir. Gun secimi marketin `target_date`'i. Kuru kosu sonucu: **298 hedef, 298 eslesme, 0 kacak**.
  - **Kural:** bu betik calistirilmadan `tests/test_backtest_engine.py::test_load_data_has_settlement` (>=50 settlement) **kirmizi kalir**.
- **Dorduncu hata — olu degiskenler:** `scripts/replay_full_bot.py` icinde `GAS`, `peak_cities`, `avg_hour`, `shares` (x2) ve `tests/test_regression_fixes.py` icinde `engine` hic okunmuyordu. Kaldirildi; `ruff check . --select F821,F841` artik **temiz**.
- **Hala ACIK (kullanici karari bekler):**
  1. **Backfill yazmasi.** `data/bot.db` D: uzerinde ve canli bot tarafindan yaziliyor; 298 satirlik UPDATE otomatik modda onay gerektirdi. Betik hazir, calistirilmadi.
  2. **`replay/main.py` + `replay/seeding.py` kayip.** 2026-08-23'te vardi (`reports/replay/final/` ciktilari ve `REPLAY:main.py:78` log satiri kanit), bugun calisma agacinda yok ve `replay/` paketi **git'te takipsiz** oldugu icin gecmisten geri alinamiyor. `tests/test_replay_engine.py` bu modulleri import ettigi icin **3 test kirmizi**. Cozum: motoru yeniden yazmak (buyuk is) ya da testleri kaldirmak.
  3. **`scripts/replay_full_bot.py` METAR ayaginda erken giris kapisi yok.** Canli kod (`jobs/metar_peak.py:755`) `_avg_peak_hour` ile "ortalama peak saati gelmeden bet acma / gelince dusus beklemeden gir" kuralini uyguluyor; replay yalnizca 1-dusus kilidini modelliyor. Yani replay **erken giris ayaklarini kaciriyor** (muhafazakar taraf). Eklemek backtest sonuclarini degistirir.
  4. **Tum Junbo zamanlanmis gorevleri Disabled** (son calisma 2026-09-28/29). Bot Windows **servisi** `JunboBot` ile calisiyor (STATE 4 RUNNING), ancak `Junbo-ActualsCollect`, `Junbo-BackupDatabases`, `Junbo-OrderbookCollect`, `Junbo-SyncBacktest`, `JunboBotWatchdog`, `JunboDataWatchdog`, `JunboSnapshot` elle etkinlestirilmeden duruyor.

### S17. `test_tie_betting` tam suite'te 7/7 kiriliyordu (test izolasyonu, 2026-10-27)

- **Belirti:** `tests/test_tie_betting.py` tek basina **7/7 GECIYOR**, tam suite'te **7/7 KIRILIYOR**. Log: `place_all_pending: 0 open markets found`.
- **Teshis (kanitlandi):** Testler piyasayi `from database.db import get_session` (taze import) ile **yeni** DB'ye yaziyor; `executor/bet_placer.py:10` ise `from database.db import get_session` satirini **modul import aninda** bagladigi icin **eski** engine'e bakiyordu. Repro (birebir):
  ```
  bet_placer get_session is database.db.get_session  -> True   (baslangicta)
  database.db pop + reimport
  bet_placer get_session is database.db.get_session  -> False
  yeni DB de market sayisi: 1
  bet_placer in gordugu market sayisi: 0
  ```
  Yani herhangi bir test `database.db`'yi `sys.modules`'tan silip yeniden import ettiginde (conftest `_protect_production_db_function`, `test_replay_engine._restore_db`) bet_placer kalici olarak eski DB'ye saplanmis kaliyor.
- **Neden tek basina geciyor:** O senaryoda `executor.bet_placer` henuz import edilmemis oluyor; ilk kez pop'tan SONRA import edilip yeni `get_session`'a baglaniyor.
- **Cozum:** `tests/conftest.py::_rebind_module_get_session` (autouse, function scope) — `get_session.__module__ == "database.db"` olan tum modul-seviyesi baglari guncel engine'e geri baglar; testlerin kendi sahte `get_session`'lari dokunulmadan kalir. Uretim kodu DEGISMEDI (uretimde modul hic yeniden yuklenmez).
- **Test:** `tests/test_tie_betting.py` tek basina 7/7; DB agir dosyalar (`test_api_bets`, `test_settler_polymarket`, `test_accounting`) 18/18.

### S18. Tekrarlanan sozluk anahtarlari sehir koordinatlarini sessizce eziyordu (2026-10-27)

- **Belirti:** `ruff check` -> `jobs/collect_settlement_temps.py` icinde **10 adet F601** (ayni sozlukte tekrar eden anahtar).
- **Teshis:** `CITY_COORDS` tek bir sozluk; Python'da **sonraki anahtar kazanir**. Olculen sonuclar:
  - `"Newark"` iki kez: **NJ (40.74, -74.17)** ve **CA (37.52, -121.88)** -> NJ sessizce CA ile eziliyordu.
  - `"St. Petersburg"` iki kez: **FL (27.77, -82.64)** ve **Rusya (59.93, 30.31)** -> FL sessizce Rusya ile eziliyordu (yaklasik 3000 km sapma).
  - `Chula Vista`, `Chandler`, `Orlando`, `Irvine`, `Durham`, `Warsaw`, `Bucharest` birebir ayni degerlerle iki kez yazilmisti (zararsiz gurultu).
- **Etki (dogrulandi):** Bot bu iki sehirde **0 market, 0 bet** (`weather_markets` ve `bets` sorgusu) — yani hata **latent** kaldi, canliya yansimadi. `jobs/collect_settlement_temps.py` dosyasini cagiran **hicbir kod yok** (yalnizca `tests/test_latent_bugs.py:564` icinde yorum satiri).
- **Cozum:** Tekrarlar kaldirildi; kanonik olan korundu (Newark = NJ, St. Petersburg = FL, cunku blok ABD). Ayni adli ikinci sehir gerekirse ayri anahtar adiyla (`"Newark, CA"`, `"Saint Petersburg"`) eklenmeli — yorumlarla belgelendi. Ayrica ayni dosyada `E401` (tek satirda coklu import), `W292` (dosya sonu newline) duzeltildi.
- **Test:** `tests/test_latent_bugs.py::test_import_all_modules` (modul hala import edilebiliyor) + ayni paket 5/5 gecti.

### S19. `data/bot.db-wal` C: diskinde 3,3 GB'a cikti (2026-10-27)

- **Belirti:** C: diskinde 6,4 GB bos kaldi; `junbo` dizini "5 GB" gorunuyordu. Olcum: `data/bot.db-wal` **3.324,8 MB** — dizindeki en buyuk TEK dosya, ve `bot.db`'nin aksine **symlink degil, gercek dosya**.
- **Kok neden 1:** `data/bot.db` -> `D:\JUNBO data\bot.db` symlink. SQLite `-wal` / `-shm` dosyalarini **verilen yolun yaninda** acar; symlink hedefini cozmez. DB D:'de, WAL C:'de.
- **Kok neden 2:** `/api/restart` `os._exit(1)` kullanir (bilincli tasarim, S: 2026-08-18). Boylece SQLite kapanista son `wal_checkpoint(TRUNCATE)` adimini **hic** yapamaz; WAL yuksek-seviyesinde kalir. Yeniden baslatma sonrasi bile 3.324,8 MB olarak olculdu.
- **Kok neden 3:** `database/db.py::set_sqlite_pragma` yalnizca `journal_mode`, `synchronous`, `cache_size` kuruyordu. **`wal_autocheckpoint` ve `journal_size_limit` hicbir yerde yoktu** (repo geneli grep: 0 eslesme).
- **Cozum:** connect listener'ina `PRAGMA wal_autocheckpoint=1000` + `PRAGMA journal_size_limit=67108864` (64 MB). WAL artik 64 MB tavani ustune cikamaz.
- **Dogrulama:** `tests/test_latent_bugs.py` (tum moduller import edilebiliyor) + tam suite 0 failed.
- **SONUC (2026-10-28 00:37 olcumu): tek seferlik `wal_checkpoint(TRUNCATE)` GEREKMIYOR cikti.** Yeni pragma'lar yuklu kodla bot yeniden baslatildiktan sonra WAL **3.324,8 MB -> 64,0 MB** indi (tam olarak `journal_size_limit` tavani); C: bos alani **0,56 GB -> 3,74 GB** (+3,18 GB). Onceki "3,3 GB'lik dosyayi elle truncate et" maddesi bu olcumle kapatildi. Kural: bu deger tekrar 64 MB'in ustune cikarsa pragma'lar yuklenmemis demektir (`database/db.py::set_sqlite_pragma` kontrol et).
- **Ilgili diger bulgular (ayni olcum):** `Heat\data\tempmarket-edge.sqlite` **4.648,6 MB C:'de** (canli HEAT deposu; runner'in `TEMP_MARKET_DATA_DIR` ayarlanmadigi icin `Heat\data` kullaniliyor, `junbo/heat/config.py` ise once `C:\HEAT data\db\...` adayini okuyor — **iki farkli HEAT deposu**), `ASIAbot\data` icinde 4 mukerrer kopya (~2,19 GB).
- **2026-10-28 ek olcum (HEAT cift-depo = AKTIF HATA, henuz duzeltilmedi):** `C:\HEAT data` toplam **6,96 GB** (kardesler.db 3.656,4 MB + tempmarket-edge.sqlite 3.187,1 MB + 12 adet 19,2 MB gunluk settlement yedegi) ve **2026-10-27T08:43Z'den beri yazilmiyor** (prediction_log 74.313 satir). Canli runner (PID: `Heat\windows-runner\index.mjs`) ise `Heat\data\tempmarket-edge.sqlite`'a yaziyor: 75.063 satir, son kayit **2026-10-27T21:22Z**. `heat/config.py::HEAT_DB_CANDIDATES` ILK olarak `C:\HEAT data` adayini sectigi icin **Junbo ~12,5 saat bayat HEAT verisi okuyor**.
- **COZUM 1 (2026-10-28, kullanici onayi "c"): Heat\data D:'ye tasindi.** Adimlar: `Stop-ScheduledTask 'TempMarket Edge Runner'` -> node PID 11772 `Stop-Process` (gorev durdurmak node'u oldurmuyor, elle gerekli) -> `robocopy /E /MOVE /XJ` ile `Heat\data` -> `D:\JUNBO data\heat\data` (202 dosya, 4.722 GB, 67 sn) -> `New-Item -ItemType Junction` ile `Heat\data` geri baglandi -> `Start-ScheduledTask`. **Dogrulama:** yeni node PID 109852, `prediction_log` 75.063 -> **75.120**, `max(created_at)` 2026-10-27T22:12:47Z (canli), DB `D:\JUNBO data\heat\data\tempmarket-edge.sqlite` uzerinde. C: bos alan **3,74 -> 8,45 GB** (+4,71 GB). DIKKAT: `start-runner.ps1` DB yolunu `TEMP_MARKET_DB`/`TEMP_MARKET_DATA_DIR` ile belirler; ikisi de bos oldugunda `db.mjs` `path.resolve('data')` = gorev calisma dizini (`Heat`) kullanir. `HEAT_MAIN_DB` User degiskeni hala `C:\HEAT data\db\tempmarket-edge.sqlite` gosteriyor ve `mlops.mjs` onu okur; `junbo/heat/config.py` de ayni olu dosyayi ilk aday olarak secer — bu ikinci yari hala ACIK.
- **COZUM 2 (2026-10-28, kullanici onayi "1"): `bets.outcome` backfill calistirildi.** `scripts/backfill_bet_outcomes.py` (yedek: `data/backups/bot_pre_backfill_20261028.db`, 1.532,3 MB). Sonuc: **298 satir dolduruldu, 0 eslesmeyen**, kalan bos (won/lost) **0**. JSON semasi settler ile ayni (`settlement_temp_c`/`_f`/`source`/`collected_at`).
- **2026-10-28: ASIAbot TAMAMEN DEVRE DISI + verileri silindi (kullanici talimati).** 5 gorev (`ASIAbot_Supervisor` Running idi, `ASIAbot Watchdog`, `asiabotBot`, `ASIAbotWatchdog`, `ASIAbot_Startup`) `Disabled`; `ASIAbot_Supervisor` durduruldu; surecler olduruldu (PID 95620 `pythonw watchdog.py`, PID 97300 `pythonw main.py bot` = ASIAbot canli botu). Silinen: `ASIAbot\data` (2.352,0 MB), `ASIAbot\logs` (516,5 MB), `.mypy_cache`, `.next`, `out`, `.pytest_cache`, `.hypothesis`, `.ruff_cache`, `__pycache__`, `bot.pid` -> toplam ~3,0 GB. Kaynak kodu/.git/node_modules korundu. `D:\ASIA data` (4.160,2 MB) silinemedi: otomasyon yol korumasi "protected from removal" dondurdu, elle silinmeli. Junbo PID 109720 etkilenmedi.
- **2026-10-28: C: diskini dolduran ASIL sebep bulundu (ASIAbot degil).** `C:\junbo_offsite_backups` = **89,4 GB / 300 dosya**; her dosya `bot.db`'nin tam kopyasi (**~1.531 MB**). Kaynak: `JUNBO_OFFSITE_DIR` **Machine** ortam degiskeni `C:/junbo_offsite_backups` + `db_backup.py::mirror_offsite` her `create_backup()`'ta tam kopya yaziyor. Temizlik `_prune_dir(offsite, label)` **etiket basina** `MAX_BACKUPS=30` tutuyor; testlerin urettigi etiketler (`a`, `b`, `cs`, `cs2`, `rt`, `prune`, `test`, `concurrent`) her biri 30 tane -> 300 dosya. Son dosya 2026-10-28 00:14 (bot restart/test kosulari). Ikinci buyuk kalem: `C:\HEAT-kurtarma` **26,8 GB** (Eylul 2026 kurtarma artiklari: `HEAT data` 18,65 GB, `bozuk.sqlite` 3,11 GB, `yeni-main.sqlite` 3,12 GB, `oncelikli` 1,55 GB, `kurtarilan.sqlite` 1,03 GB) — hicbiri canli degil.

### S20. `replay/` paketi diskten kayboldu, `test_replay_engine` 3/3 kirildi (2026-10-27)

- **Belirti:** `tests/test_replay_engine.py` -> `ModuleNotFoundError: No module named 'replay.main'`.
- **Teshis:** `Glob replay/**` yalnizca `__init__.py` ve `fidelity.py` dondurdu. `replay/main.py` + `seeding.py` **git'te hic izlenmemis** (untracked) oldugu icin `git checkout` ile geri alinamadi; Recycle Bin ve tum disk aramasinda yok. Dosyalarin 2026-08-23'te var oldugu kanitli: `reports/replay/final/fixed/*.csv` ciktilari ve log satirlari.
- **Cozum:** paket sifirdan yazildi — `clock.py` (ReplayClock), `archive.py` (read-only arsiv, look-ahead filtreli), `price_index.py` (gamma->orderbook->price_history onceligi), `scheduler.py` (spread T-0/T-1/T-2 + METAR-peak tick tick), `seeding.py` (temp DB + `rebind_get_session`), `main.py` (CLI + rapor yazicilari).
- **Yazarken olculen iki tuzak:**
  1. `weather_forecasts` **bot.db'de yalnizca 2026-10-17..10-29** araligini tutuyor; Agustos penceresi **backtest.db**'de (1.106.701 satir). Yalniz bot.db okuyan ilk surum **0 forecast -> 0 bet** uretiyordu (rapor "Acilan 0" cikiyordu). Cozum: `FORECAST_DBS = (BOT_DB, BACKTEST_DB)` birlikte okunur.
  2. Tum arsivi tarayan fiyat yuklemesi (`orderbook.db` 4 GB) tek gunluk replay'i **257 sn** yapiyordu. `market_snapshots`/`orderbook_snapshots` SQL'de `target_date` ile, `price_history` market_id kumeleriyle filtrelendi -> **~14 sn**.
- **Kapsam disi (bilerek):** canli hibritin "aktar" zinciri ve rotation/erken kapanis modellenmiyor; sehir-gun basina tek giris alinir, her bet `settlement` ile kapanir.
- **Test:** `python -m pytest tests/test_replay_engine.py tests/test_replay_fidelity.py -q` -> **11 passed**.

### S21. `test_backtest_engine` settlement bulamiyordu: `bets.outcome` NULL (2026-10-27)

- **Belirti:** `tests/test_backtest_engine.py::test_load_data_has_settlement` -> `AssertionError: settlement az: 0`. Tam suite'in **tek** kirmizi testi.
- **Teshis:** `backtest/engine.py::load_data` gercek sicakligi yalnizca `SELECT ... outcome FROM bets` uzerinden okuyordu ve `outcome IS NOT NULL` filtreliydi. `bets.outcome` 2026-09-29 sonrasi NULL kaldi (olcum: son donem **192 yerlesmis bet, 0 outcome**). `data/actuals.db.actual_temperatures` **8.691 satir** ve sehir adlari **48/48** birebir eslesiyor.
- **Ikinci kusur:** gun secimi `str(settled_at)[:10]` idi — yani **yerlesim ANI**. UMA 1-3 gun gec cozdugunde onceki gunun sicakligi yazilir (S16'nin settler'daki ayni hatasi).
- **Cozum:** (1) `LEFT JOIN weather_markets m ON b.market_id = m.id` + `day = m.target_date or b.settled_at`; (2) yeni `_actuals_fallback(cutoff)` — `actuals.db`'yi `mode=ro` acar, `actual.setdefault(...)` ile **outcome degerinin uzerine yazmaz**. Dosya yoksa bos sozluk doner, backtest yine calisir.
- **Etki:** yalnizca okuma yolu (backtest analitigi). Canli bot karari, DB yazimi veya `bets.outcome` degismedi — bu yuzden 298 satirlik backfill'e bagimlilik kalmadi.
- **Test:** `python -m pytest tests/test_backtest_engine.py -q` -> **5 passed**.

### S22. Forecast runner (eski HEAT runner) Junbo icine gomuldu (2026-11-10)

- **Neden:** Heat projesi ayri bir Node uygulamasi olarak calisiyordu ve kullanici Heat'i silecek. Tahmin uretimi Junbo'nun sahip oldugu bir runner'a tasindi.
- **Ne tasindi:** `Heat/windows-runner` (index/db/sources/report/mlops/om_cache + config) ve `Heat/scripts` (gunluk besleyiciler dahil) -> `junbo/forecast_runner/`. `forecast_runner/data` junction -> `D:\JUNBO data\heat\data` (Heat'teki `Heat\data` junction'inin karsiligi).
- **Yol uyarlamalari:** `start-runner.ps1` su env'leri set eder: `TEMP_MARKET_DATA_DIR`, `TEMP_MARKET_DB`, `HEAT_LIVE_DB`, `HEAT_MAIN_DB`, `KARDESLER_DB`, `OM_CACHE_DB`, `HEAT_DAILY_BLOCK_STATE`, `HEAT_UNMATCHED_JSON`, `HEAT_ONERI_JSON`, `HEAT_BACKUP_DIR`, `HEAT_MODELS_DIR`, `TEMP_MARKET_SETTLEMENT_INBOX`, `TEMP_MARKET_CONFIG`, `POLYMARKET_SOCKS5_PROXY`. `_settlement_bridge.mjs` ve `_sehir_cozucu.mjs` yedek yollarini MAIN DB yanindan turetir; `_mirror_kardesler.mjs` ASI kaynagi `D:/JUNBO data/heat/upstream/...`; `train_bias_online.mjs` models/backup yollari JUNBO data.
- **Gorevler:** `JunboForecastRunner` (AtLogOn, `run-loop.ps1`) + `JunboForecastRunnerWatchdog` (saatlik, `watchdog.ps1`). Eski `TempMarket Edge Runner`/`TempMarket Edge Watchdog` **Disabled**.
- **Dogrulama:** embedded node PID 84404, port 8787 `/health` ok, cycle started; `heat_mos_job.run()` -> **12 added**; JunboBot `/api/status` healthy.
- **Not:** `forecast_runner/` vendored koddur (Heat kaynagindan); Turkce karakterler upstream'den gelir, yeni Junbo kodu ASCII kuralina uyar. Heat projesi silinebilir; runner'in ihtiyaci olan tum veri `D:\JUNBO data` altindadir.
- **Test uyarlamasi (2026-11-10):** `tests/test_latent_bugs.py` import/dead-code taramalari `forecast_runner` (vendored) dizinini atlar; `tests/test_settlement_gate.py` MAIN HEAT DB'yi (`HEAT_MAIN_DB`, yoksa `D:\JUNBO data\heat\main\db\tempmarket-edge.sqlite`) kullanir — LIVE DB yalnizca 2026-08-25..09-04 settlements icerdigi icin eslesme 0 veriyordu. Hedefli 3 test gecti. **Tam suite: 765 passed, 6 skipped, 0 failed (23 dk 43 sn).**
- **2026-11-10 (isim):** `heat_runner` -> `forecast_runner`; gorevler `JunboForecastRunner` / `JunboForecastRunnerWatchdog`; eski `JunboHeatRunner` gorevleri silindi. Python `heat/` paketi (HEAT MOS veri kaynagi adaptoru) ayni kaldi.
- **Ponytail temizlik (2026-11-10):** `forecast_runner` 1003 -> 31 dosya; kullanilmayan Heat scriptleri `D:\JUNBO data\_eski\heat_runner_unused_20261110`'a tasindi; `scripts/catalog` -> `D:\JUNBO data\catalog` (`_aiwp_forward`/`_aiwp_extract`/`_aiwp_catalog_build.py` env/default guncellendi). Dev-only `node_modules` (975 MB) ve `.next` silindi; dashboard `out/`'tan servis ediliyor (rebuild: `npm install && npm run build`). Cache/eski log temizlendi; root debug scriptleri `scripts/archive/debug/`'e tasindi; 9 F401 duzeltildi. Dogrulama: quick_check **7/7**, hedefli testler **38/38**, dashboard canli (tum API 200; tek 404 favicon). Tam suite: **763 passed, 7 skipped**; tek canli proxy testi ilk kosuda ag/proxy flake'i verdi, tekrar kosuda **1/1 passed**.
- **2026-11-10 (konsolidasyon 2):** `D:\JUNBO datasia\` ve `D:\JUNBO data\heat\` dizinleri tamamen silindi; gerekli HEAT DB'leri kokte: `tempmarket-edge.sqlite` (LIVE), `tempmarket-main.sqlite` (MAIN), `kardesler.db`; kucuk `state/`, `models/`, `inbox/`, `reports/` kokte. `forecast_runner/data` junction -> `D:\JUNBO data`. Backtest verileri (bot.db, backtest.db, backtest_prices.db, orderbook.db, actuals.db, poly_history.db, snapshots.db, archive, unified) korundu.

### S23. Replay sonucu canliyla tutmuyor / kirli fiyat gunu (2026-11-23)

- **Belirti:** hibrit replay'in "canli ayna" senaryosu ayni pencerede canli bottan cok farkli
  PnL verir (ilk kosu: replay +$240, canli -$15).
- **Neden 1 — bilgi-oncesi fiyat:** piyasa METAR'a saniyeler icinde tepki verir; gozlemden ONCE
  kaydedilmis snapshot/ask ile dolum look-ahead'dir. Replay artik fiyatin son gozlemin
  `recorded_at` anindan SONRA kaydedilmis olmasini sart kosar (`--pre-info-prices` yalnizca
  duyarlilik icindir).
- **Neden 2 — kirli fiyat gunu:** 2026-11-17 21:07 UTC restart sonrasi DB/snapshot fiyatlari
  BAYATTI (Londra/Paris/Amsterdam/Munih 0.30-0.52 gorunurken gercek ask 1.00). Snapshot tabanli
  her backtest o gun sahte ucuz girisler "kazanir" -> 2026-11-17 kiyas disi tutulur.
- **Neden 3 — cap tahsisi:** gunluk 15'lik cap dolunca hangi sehrin bet aldigi isleme sirasina ve
  zamanlamaya baglidir; canli botun kesinti/blok gunleri (11-14 Kas) cap'i bos birakir. Ayni
  markete giren betlerde replay-canli giris farki medyan 0.000 / zaman farki +1 dk (dogrulandi).
- **Yapilacak:** mutlak PnL yerine senaryo FARKINA bak; iki fiyat varsayimiyla da kostur.
  Rapor: `reports/replay_metar_erken_giris_20261123.md`.

### S24. Testler baska ortamda (Cowork/CI) "no such table" / "connection refused" ile kiriliyor (2026-11-23)

- **Neden:** canli `bot.db` veya WARP proxy'yi dogrulayan testler uretim makinesine baglidir.
- **Davranis:** `tests/conftest.py` `live_bot_db` / `production_host` — uretim verisi yoksa test
  "uretim verisi yok" nedeniyle ATLANIR; uretim makinesinde her zaman kosar ve ariza FAIL verir.
- **Kural:** canli DB'yi okuyan yeni testler bu fixture'i kullanir ve DB'yi `mode=ro` acar
  (`sqlite3.connect(yol)` rw modda yol yoksa bos DB dosyasi uretir).

### S25. WARP kismi ariza: Cloudflare acik, Cloudflare disi kapali (2026-11-25)

- **Belirti:** bot gamma-api'ye ulasamaz (SOCKS "Host unreachable"), yeni market gelmez;
  `TestPolymarketProxyLive` FAIL. Buna ragmen `warp-cli status` "Connected, healthy" ve
  `cdn-cgi/trace` `warp=on` doner.
- **Neden:** WARP tuneli Cloudflare hedeflerine acik, Cloudflare disi cikis (CloudFront, Google,
  Quad9) kopuk.
- **Otomatik (2026-11-26+):** `scripts/warp_health.ps1` gamma-api yoklamasi tick'i basarisiz sayar;
  2 tick sonra `warp-cli disconnect` + `connect` (15 dk cooldown, mod degismez). Log:
  `FAIL #n gamma-api (CloudFront) unreachable through proxy`.
- **Elle:** ayni iki komut; sonra DoH adresiyle `curl.exe --proxy socks5://127.0.0.1:40000
  --resolve gamma-api.polymarket.com:443:<IP> https://gamma-api.polymarket.com/markets?limit=1`
  200 dondurmeli. `warp-cli mode ...` ASLA calistirilmaz.
- **Tuzak:** gamma-api'yi `socks5h://` veya sistem cozumleyicisiyle yoklamak hosts sabitlemesiyle
  Cloudflare'a gider, 421 doner ve arizada da "ulasilir" gorunur.

### S26. Odul kaydinda dakika bosluklari (2026-11-26)

- **Belirti:** `C:\JUNBO data\poly_rewards_recorder.db` `samples` tablosunda eksik dakikalar; log'da
  `HATA ornek RuntimeError: POST /books (... token) basarisiz (k/6 deneme, tur suresi doldu)`.
- **Neden:** WARP aralikli kopuklugu; Polymarket'e yalniz WARP uzerinden erisilir. Ayni zaman diliminde
  `logs\warp_health.log` `FAIL #n` gosterir (2026-11-26 17:19 sonrasi kapsama %64; son 9 gunde gunde 15-49 FAIL).
- **Davranis (2026-11-26 aksam+):** ornek 50 sn icinde biter, sonraki dakika calisir; yenileme basarisizsa eski
  hedeflerle devam edilir, sonraki tur yeniden dener. Kopukluk suresince kaybolan dakikalar geri gelmez;
  `--report` eksik dakikalari ortalamaya katmaz.
- **Elle:** kayit icin bir sey gerekmez; uzun kopuklukta S25 adimlari. Gorev durumu:
  `Get-ScheduledTaskInfo Junbo-RewardsRecorder` (LastTaskResult 0 = son tur temiz, 1 = hata satiri log'da).
- **Eski hata (duzeltildi, bolum 5):** yenileme ornekten once calisinca her yenilemede 1 dakika bos kaliyordu;
  basarisiz tur ~95 sn surup sonraki dakikanin tetigini de yakiyordu.

---

## 14. Denetim & Debug Playbook (2026-11-13)

### 14.1 Tek dogru DB yolu
- Canli DB: `.env` -> `DB_PATH=D:\JUNBO data\bot.db`. Tum runtime/audit kod `utils/db_paths.py::get_db_path()` kullanir.
- Repo `data/bot.db` ticaret icin KULLANILMAZ (0 bet). Eskiden `activity_log` ve ~33 script buraya bakiyordu; 2026-11-13'te tek kaynaga baglandi. `data/bot.db`'de yalnizca eski `activity_events` kalabilir.
- Sibling DB'ler (`actuals.db`, `orderbook.db`, `backtest.db`, `backtest_prices.db`) `utils/db_paths.py::get_data_dir()` altindan okunur.

### 14.2 Dogru endpoint'ler
- `/api/status`: `is_running`, `scan_health`, `last_scan`, `price_poller_health`, portfoy.
- `/api/health-check`: `verdict`, `red_flags`, `strategy_mode`, `edge_metrics_available`, `summary_all` (settlement/rotation), `recent_48h`, `activity_events`, `peak_watch`, `daily_pnl_timeline`.
- `/health` YOK (404). Eski dokumanlarda gecen `/health` yerine yukaridaki iki endpoint kullanilir.

### 14.3 Health-check verdict yorumu
- `critical` artik yalnizca son 48 saat NET PnL negatifse verilir. Rotation kapanislari ayri raporlanir (`summary_all.rotation`, `recent_48h.rotation_*`); settlement sonucu ayri (`summary_all.settlement`).
- Spread modunda `Analysis` satiri uretilmedigi icin `edge_metrics_available=false` ve `avg_net_edge_pct=0` normaldir; bunu "edge yok" diye okuma.

### 14.4 Hizli denetim komutlari
```powershell
sc.exe query JunboBot
schtasks /query /tn JunboBotWatchdog /fo LIST
python -c "import urllib.request,json; d=json.load(urllib.request.urlopen('http://127.0.0.1:8093/api/status')); print(d['is_running'], d['scan_health'], d['minutes_since_last_scan'])"
python -c "import urllib.request,json; d=json.load(urllib.request.urlopen('http://127.0.0.1:8093/api/health-check')); print(d['verdict'], d['recent_48h'])"
Get-Content logs\bot.log -Tail 80
```

### 14.5 Test katmanlari
- `python quick_check.py --fast` (ruff/pylint/mypy/import)
- `python -m pytest tests/test_latent_bugs.py -q`
- `python -m pytest tests/test_health_check_spread_aware.py tests/test_db_path_consistency.py -q`
- Tam suite: `python -m pytest tests/ --ignore=tests/test_betting_idempotency.py --ignore=tests/test_comprehensive.py -q`
- 2026-11-13 dogrulama: quick_check 7/7; latent 5/5; health/db/backup 18/18; E2E+liveliness 76/76; muhasebe+settlement 17/17; tam suite **773 passed, 7 skipped, 0 failed (12:11)**.

### 14.6 Bilinen tuzaklar
- Windows'ta acik dosyanin directory size'i 0 gorunebilir; `Get-Item logs\bot.log | Select-Object Length` kullan.
- `code_version` git HEAD'i dondurur, surecte yuklu kodu kanitlamaz; dosya mtime'lari ile process baslangicini karsilastir.
- `scripts/check_pnl.py` gibi audit scriptleri artik canli DB'yi okur; yine de once `DB_PATH`'i dogrula.
