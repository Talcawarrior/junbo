# Junbo - Self-Evolving Weather Prediction Bot

**Port: 8093** | **Framework: FastAPI + Next.js** | **Dry-Run Mode: KALICI (DRY_RUN=true)**

**Son guncelleme:** 2026-10-04 | **Konum:** `C:\Users\fdemir\Documents\New project\junbo`

---

## 1. Proje Nedir

Junbo, Polymarket'teki sicaklik piyasalari (highest/lowest temperature) icin otomatik bahis acan bir Python botudur. 8 hava durumu modelinden (GFS, ECMWF, GEM, ICON, JMA, CMA, UKMO, MeteoFrance) agirlikli ensemble tahmin uretir, Polymarket fiyati ile karsilastirir (edge) ve Kelly criterion ile pozisyon boyutlandirarak YES tarafina bahis acar.

**KALICI KARAR (2026-08-07): Bot her zaman PAPER (DRY_RUN) modunda calisir. Canli Polymarket trade'i asla acilmaz.** `.env` icinde `DRY_RUN=true` sabittir; `executor/bet_placer.py` icinde `_live_allowed = False` kod seviyesinde sabitlenmistir. Kullaniciya canli trade sorulmaz, onerilmez.

**Not:** Piyasa kapanisi `target_date` gununun **24:00 UTC**'sidir (target_date icindeki 12:00 tarih etiketidir, kapanis degildir).

---

## 2. Mimari

```
Polymarket Gamma API ──> scrapers/polymarket.py ──> weather_markets (DB)
Open-Meteo + WeatherAPI ──> scrapers/meteo.py ──> weather_forecasts (DB)
                      ──> engine/calculator.py  ──> analyses (DB) [edge, prob, EV]
                      ──> engine/strategy.py    ──> karar + risk
                      ──> executor/bet_placer.py ──> bets (DB) [paper CLOB]
                      ──> executor/settler.py   ──> settled / PnL
```

### Moduller

| Modul | Sorumluluk | Dosya |
|---|---|---|
| API | FastAPI endpoints, WebSocket, BotState | `api.py` |
| Bot loop | 6 asyncio arka plan dongusu | `bot_loop.py` |
| Calculator | 8-model ensemble olasilik, Kelly | `engine/calculator.py` |
| Strategy | RiskManager + BettingEngine + early exit | `engine/strategy.py` |
| Bet Placer | Paper + canli bahis | `executor/bet_placer.py` |
| METAR peak | Canli istasyon sicakligi -> zirve tespiti -> tek esik bet | `jobs/metar_peak.py` + `scrapers/metar.py` |
| Settler | Gamma API settlement kontrol, PnL | `executor/settler.py` |
| Scraper | Polymarket + hava durumu cekici | `scrapers/` |
| DB | SQLAlchemy + WAL | `database/db.py` |
| Config | Tum ayarlar | `config/settings.py` |
| Job scheduler | run_cycle, run_settle | `jobs/scheduler.py` |

**Stack:** Python 3.12+, FastAPI, SQLite (WAL), SQLAlchemy 2, Next.js 16 dashboard, pytest.

---

## 3. Veri Toplama ve Backtest Veri Seti (KRITIK)

Backtest icin kesintisiz veri toplama esastir. Asagidaki sistem 2026-08-08'de kuruldu ve kendini izler.

### Veri Kaynaklari

| # | Veri | Kaynak | DB | Cadence |
|---|---|---|---|---|
| 1 | Price snapshots | Polymarket Gamma | `bot.db` → `market_snapshots` | **30 dk** (24/7) |
| 2 | Orderbook depth | CLOB API | `orderbook.db` | **10 dk** (paralel, 2026-08-21) |
| 3 | Actual temperatures | Open-Meteo Archive | `actuals.db` | 6 saat |
| 4 | Backtest kopyasi | bot.db'den kopya | `backtest.db` | 6 saat (sync) |
| 5 | Backup | 4 DB yedekleri | `data/backups/` | 6 saat |
| 6 | Backtest fiyatlari | Orderbook + CLOB | `backtest_prices.db` | 6 saat (replay icin) |
| 7 | Snapshot arsivi | Market snapshotlar | `snapshots.db` | 30 dk (replay icin) |
| 8 | Polymarket gecmis | Gamma API | `poly_history.db` | 6 saat (analiz icin) |
| 9 | Evolution deneyleri | ASI/bias deneyleri | `asi_evolve_experiments.db` | gunde 1 (03:00 UTC) |
| 10 | Forecast arsivi | bot.db hot forecast | `D:\JUNBO data\archive\forecasts_snapshot_*.parquet` | 6 saat |
| 11 | Veri katalogu | tum DB/parquet taramasi | `D:\JUNBO data\catalog\data_catalog.db` | 6 saat |

> **Veri koku:** kalici veriler `D:\JUNBO data` altindadir (`bot.db`, `backtest.db`, `orderbook.db`, `actuals.db`, `archive/`, `unified/`, `catalog/`). Repo `data/` yalnizca gelistirme/test icindir. Tum veri dosyalarinin yeri ve tarih kapsami: `D:\JUNBO data\catalog\DATA_CATALOG.md`; sorgu: `python scripts/query_data_catalog.py --search <tablo>`.

> **Not:** `actuals.db`, `backtest.db`, `backtest_prices.db`, `snapshots.db` gunluk sync task'lari henuz dolmamissa 0 byte gorunebilir; `scripts/sync_backtest_db.py` + `scripts/collect_actuals.py` + `scripts/collect_orderbook.py` calistiktan sonra dolar.

### Task Scheduler Gorevleri (SYSTEM, WakeToRun=True, StartWhenAvailable=True)

| Gorev | Script | Interval | Not |
|---|---|---|---|
| `JunboSnapshot` | `snapshot_only.py` (30dk bucket dedup) | 30 dk | bot kapali olsa da alir |
| `Junbo-OrderbookCollect` | `scripts/collect_orderbook.py` | 10 dk | paralel fetch (15 worker) + lock |
| `Junbo-ActualsCollect` | `scripts/collect_actuals.py` | 6 saat | gercek sicaklik |
| `Junbo-SyncBacktest` | `scripts/sync_backtest_db.py` | 6 saat | markets + market_snapshots |
| `Junbo-ArchiveForecasts` | `scripts/archive_forecasts.py` | 6 saat | gunluk forecast snapshot |
| `Junbo-DataCatalog` | `scripts/build_data_catalog.py` | 6 saat | yeni DB'leri kataloga ekler |
| `Junbo-StrategySleeves` | `scripts/strategy_sleeves_report.py` | gunluk 00:15 | spread/metar ayri sanal kasa |
| `Junbo-BackupDatabases` | `scripts/backup_databases.py` | 6 saat (gunde 1 kosar) | 2026-11-18: UTC gununde 1 kez + prefix basina son 7 kopya (C: dolmasin) |
| `JunboBotWatchdog` | `scripts/bot_watchdog.py` | 1 dk | bot process/restart (2026-11-13: tekrar Ready) |
| `JunboDataWatchdog` | `scripts/data_watchdog.py` | 6 dk | **KENDI KENDINI TAMIR** |
| `JunboWARP` | `scripts/warp_health.ps1` | 5 dk | 2026-11-18: WARP proxy saglik kontrolu + otomatik reconnect; mod ASLA degismez (`start_warp.ps1` OBSOLETE). 2026-11-26: iki yoklama -- Cloudflare trace + gamma-api/CloudFront (DoH adresi + `curl --resolve`, socks5://); biri basarisizsa tick basarisiz |

> **2026-11-11 notu:** Veri toplama gorevleri (`Junbo-OrderbookCollect`, `Junbo-ActualsCollect`, `Junbo-SyncBacktest`) bir sure **Disabled** kaldi ve scriptler repo `data/` yoluna bakiyordu; bu yuzden Eylul sonrasi orderbook/backtest verisi durdu. Script yollari `D:\JUNBO data`'ya cevrildi, `sync_backtest_db.py` toplu kopyaya gecirildi. Gorevleri yeniden kurmak/enable etmek icin yonetici PowerShell'de: `powershell -ExecutionPolicy Bypass -File scripts\setup_tasks.ps1`. **2026-11-13:** DB yolu tek kaynaga baglandi: `utils/db_paths.py` (.env `DB_PATH`); `activity_log` + 33 script + `executor/settler.py` + `db_backup.py` artik canli `D:\JUNBO data` DB'sini kullanir. `JunboBotWatchdog` tekrar enable edildi.

**JunboDataWatchdog** (kritik): her 6 dakikada bir tum veri kaynaklarinin tazeligini kontrol eder.
Bayat veri bulursa ilgili toplayiciyi kendisi baslatir; task disabled olduysa yeniden enable eder.
Ayrica gunde 1 kez (02:00-04:00 UTC, `data/.last_db_maintenance` marker) `scripts/db_maintenance.py` ile
DB bakimi (ANALYZE + VACUUM + WAL checkpoint) yapar — dosya boyutunu ve sorgu istatistiklerini taze tutar.
Log: `data/logs/data_watchdog.log` / `data/logs/db_maintenance.log`.

### Bilinen Bugfix (2026-08-08)

- `jobs/snapshot_job.py` — eski mantik farkli 30dk bucket'ta `existing = None` yapiyor ama
  yeni satir olusturmuyordu (snapshot durdu, 0 kayit). Duzeltildi: bucket degistiginde her seferinde yeni
  satir yazilir. `take_market_snapshots` = 541 kayit dogrulandi.
- `JunboSnapshot` task action pat i — `cmd /c "...junbo\..\snapshot_task.bat"` parent dizini
  isaret ediyordu (bat yoktu, LastResult=1). Duzeltildi: dogru yol `...junbo\snapshot_task.bat`,
  LastResult=0, 17 snapshot tasarlandi.
- **Uyku/tarama sorunu (2026-08-08):** Windows "Allow wake timers" DC=Disabled, AC="Important only"
  idi → bilgisayar uyurken hicbir task uyanamiyordu (`powercfg /waketimers` bos). Duzeltildi:
  AC+DC=Enable; ayrica **Sleep after AC+DC = Never (0)**, Hibernate = Never — bot loop'lari
  (tarama, hesap) artik kesintisiz calisir. Kontrol: `powercfg /query SCHEME_CURRENT SUB_SLEEP`.

---

## 3b. Kurulum

```powershell
# 1) Bagimliliklar
pip install -r requirements.txt

# 2) Ortam
Copy-Item .env.example .env
# DRY_RUN=true sabit

# 3) Dashboard (istege bagli)
npm install
npm run build
Copy-Item -Path "out\*" -Destination "dashboard\out\" -Recurse -Force

# 4) Bot
python main.py bot
# Dashboard: http://127.0.0.1:8093 | API: http://127.0.0.1:8093/api/status
```

### .env Onemli Degiskenler

| Degisken | Varsayilan | Aciklama |
|---|---|---|
| `DRY_RUN` | `true` | Paper modu (KALICI) |
| `JUNBO_API_KEY` | rastgele | API auth (POST icin) |
| `INITIAL_PORTFOLIO` | `1000.0` | Baslangic bakiyesi |
| `SCAN_INTERVAL` | `300` | Tarama araligi (sn) |
| `SETTLEMENT_INTERVAL` | `120` | Settlement araligi |
| `MAX_EXPOSURE_PCT` | `0.25` | Toplam pozisyon siniri |
| `MAX_BET_PCT` | `0.03` | Tek bahis siniri |
| `KELLY_FRACTION` | `0.15` | Fractional Kelly |
| `CITY_CAP` | `4` | Sehir basina acik bet |
| `FLAT_BET_USD` | `10.0` | Sabit bet tutari (Kelly override) |
| `MAX_ENTRY_PRICE` | `0.99` | 0.99+ fiyata bet acilmaz |
| `HOST` / `PORT` | `127.0.0.1` / `8093` | Sunucu |
| `DB_PATH` | `D:\JUNBO data\bot.db` (.env) | Ana DB; repo `data/bot.db` ticaret icin KULLANILMAZ (eski kopya) |
| `METAR_EARLY_ENTRY` | `false` | METAR-peak ERKEN giris (2026-11-23 kullanici karari: KAPALI). `false` = yalnizca 1-dusus kilidi + aktar; `true` = eski hibrit (ortalama peak saati + <=0.50 fiyatta dusussuz giris). Degisiklik restart ister |
| `POLY_PROXY` | `socks5h://127.0.0.1:40000` | Polymarket SOCKS5 proxy — sistem PAC'i (`polymarket.pac`) WARP'a yonlendirir; **zorunlu** (2026-08-16: proxy yoksa bot `10054` alir, market cekemez) |

---

## 4. Bahis Penceresi (2026-08-11 karar — KAPALI)

- **Pencere DEVRE DISI (2026-08-11 karar):** betler gun boyu acilir.
  `betting_window_enabled=False` (`.env BETTING_WINDOW_ENABLED=false`).
- Eski pencere mantigi (04:00-23:30 UTC) artik uygulanmaz.

---

## 4b. Spread Stratejisi (Ana Mod, 2026-08-10)

**Varsayilan moddur** (`BETTING_STRATEGY=spread`). Eski edge-tabanli mod `BETTING_STRATEGY=edge` ile geri donulebilir.

- Yeni **2-gun-sonrasi tarih** acildiginda (bot_loop 2-day-ahead tespiti):
  - En son meteo tahmininin **tam merkezine** (tek esik, radius=0) YES bet acilir.
    (2026-08-16 kullanici karari: "her sehre meteo ne diyorsa TEK bet, tam merkez,
    +/- 1 demedim". Backtest 2026-08-14: radius0 +$41.9, radius3 -$317.)
  - Giris fiyati = **CANLI `weather_markets.yes_price`** (5 dk'da guncellenir; bayat snapshot degil, 2026-08-11).
  - `0.01 < entry < 0.95` olan her fiyata bet acilir (esik basina $2, 2026-08-16
    kullanici karari: "fiyat ne olursa olsun 0.95 ve alti"). Fair-value ve
    0.10-0.20 olum bolge filtreleri KALDIRILDI (2026-08-16).
  - **KAYAN PENCERE KAPALI (2026-08-12):** merkez kayarsa bile acilan betler settlement'a
    kadar TUTULUR. Backtest: kaydirma her config'de zarar (shift -26 vs noshift +74).
    Sadece yeni esikler acilir, eski penceredekiler kapatilmaz.
  - Tahmini **en az sapan ilk 15 sehir** secilir (tahmini gercege en yakin tutanlar —
    dusuk |bias|; SICAKLIK DEGIL, 2026-08-11 kullanici karari. Bias'siz yeni sehir acilmaz).
    **Sehir secimi SADECE yeni gun acilisinda kullanilir; sehir secilmeden dusse bile
    acik betleri KAPATILMAZ (2026-08-12 kullanici karari).**
   - Gunluk **max 120 bet** (2026-08-16 kullanici karari: "Toplam 120").
   - **WORLDWIDE (2026-10-06 kullanici karari):** 2026-10-05'teki Turkey-only
     kisitlamasi KALDIRILDI — spread + edge + METAR-peak tum dunyada bet acar
     (`turkey_only=false`). Filtre kodu (`trading_city_allowed`) duruyor,
     `TURKEY_ONLY=true` ile istenirse yeniden acilabilir.
- **ERKEN GIRIS (0-13 UTC hafif probe):** Snapshot analizi ilk market acilislarinin
  04:00-12:30 UTC'ye yayildigini gosterdi. 00:00-13:00 UTC penceresinde bot her ~1 sn
  Polymarket Gamma'ya TEK hafif sorgu atar (public-search limit 5); DB'deki max acik
  tarihten ileri bir tarih gorurse HEMEN tam market cekisi + spread bet acar. Yeni
  tarih yoksa cekis yapilmaz (rate limit korunur). Pencere disinda normal 5 dk tarama.
- **CLOB WebSocket:** Acik betlerin marketleri gercek zamanli WebSocket ile dinlenir
  (5 dk polling yerine milisaniye fiyat akisi). Her fiyat olayi orderbook.db'ye
  best_ask olarak arsivlenir (backtest icin kalici CLOB gecmisi, 2026-08-16).
- **METAR zirve-tespiti (2026-08-14):** Acik marketli sehirlerin METAR canli istasyon
  sicakligi (aviationweather.gov, NOAA bedava) 30dk'da bir izlenir; sicaklik max'a
  cikip **1 kez dustugunde** (2026-08-18 kullanici: "20 21 22 22 21 diyorsa ikinci
  21 ve altini bekleme 22 ye bet ac") (YEREL saat >= 13:00) zirve kilitlenir,
  o sehrin kazanan bucket'ina **tek esik YES** bet acilir
  ($3, order_id `metar_*`, tum sehirler — bias filtresi YOK, `MIN_ENTRY=0.05`
  E config 2026-08-18). 2026-08-17: canli 30 bet NET -$32.84 — 24 longshot
  (entry 0.01-0.03) -$39.90 kaybetti; MIN_ENTRY=0.05 ile 0.01-0.03'ler elendi.
  **2026-08-18 (kullanici "tam bucket a aciyoruz"): peak mantigi SADECE
  `metric=temperature_max` + `market_type=RANGE` (tam bucket) marketlerine bet
  acar.** Canli 11 yanlis bet (6 temperature_min + 5 HIGH/LOW, hepsi 0.01 entry)
  o yuzden acilmisti; or-above/or-below marketleri tam bucket kazananli degildir.
  **2026-08-20 HIBRIT (kullanici onayi):** sehir bazli gecmis ortalama peak saati
  (`jobs/metar_peak.py::_avg_peak_hour`, bugun haric, en az 3 gun) hesaplanir; o
  saate kadar bet acilmaz; saat gelince cur_max'a ERKEN giris adayi olur — bucket
  marketi fiyati **0.50'nin USTUNDEYSE** 1-dusus kilidini bekler (piyasa biliyor),
  altindaysa dusus beklemeden girer. Aktar (zirve asilmasi -> kapat + yenisine ac)
  her iki modda da calisir. Backtest (05-19 Agu, cap 6/gun + 30dk dongu gecikmesi):
  hibrit compound $200 -> $4,070 (kilit $3,629, erken saf $2,973).
  **2026-11-23 ERKEN GIRIS KAPATILDI (kullanici karari):** ucuz erken betler zirveden
  once acilip sicaklik yukselince aktar'da ~0.01'den kapaniyordu. Replay (gercek
  Polymarket sonucu, 17 Kas haric): erken ACIK -$132.24 / KAPALI +$14.67; canli giris
  <=0.50 betler -$44.66. Artik `METAR_EARLY_ENTRY=false` (varsayilan): ortalama peak
  saati hesaplanmaz, YALNIZCA 1-dusus kilidi (30 dk onay) + aktar. Geri acmak:
  `.env` `METAR_EARLY_ENTRY=true` + restart.
  **2026-08-21 KARA LISTE KALDIRILDI:** `METAR_PEAK_BLACKLIST` bosaltildi
  (kullanici karari) — eski listedeki sehirlerde (VHHH %20, ZGSZ %29, KBKF/KATL
  %43, KSEA/KSFO/NZWN %57 tutma) METAR-peak bet yeniden acilir. Gecmis karar
  kaydi: GELISTIRICI_NOTLARI.md. **BAYAT METAR'da aktif
  duzeltme:** son gozlem esikten eski ise (30dk istasyonlarda 45dk, 60dk
  saatlik istasyonlarda 90dk — kadans-bilgili, 2026-08-20) o sehrin METAR'i DERHAL
  yeniden cekilir; taze gelirse devam, gelmezse sehir-gun basina 1 kez alarm.
  **2026-08-21 agresif yeniden cekim (kullanici onayi):** (A) ilk paralel fetch
  bos/hata donerse sessiz atlama YOK - o sehrin METAR'i hemen yeniden cekilir
  (Buenos Aires 19 Agu 17:07->20:34 sessizligi). (B) bayat yeniden cekim TEK
  deneme yerine 3 deneme (aralarda 3 sn bekleme); 3'u de bayat/basarisizsa
  sehir-gun basina 1 kez alarm + yeni bet yok.
  **2026-08-21 orderbook kayit sikligi + backtest cikis gercekciligi (kullanici onayi):**
  `collect_orderbook.py` serial cekim (~2000 market ~20dk) yuzunden 30dk'da bir
  ornekliyordu; watchdog sonraki tick'lerde DB kilitli gorup ust uste collect
  baslatiyordu ("database is locked" seli). Cozum: `ThreadPoolExecutor(15)` paralel
  fetch (dongu ~4dk) + `.orderbook_collect.lock` (cakisan run'i durdurur);
  `data_watchdog.py` `ORDERBOOK_MAX_AGE` 45dk->10dk; `Junbo-OrderbookCollect` task
  every30m->every10m. Backtest `metar_peak_live` cikis (break/rotation) da
  `--entry-delay-min` gecikmesine tabi artik (once break anindaki bayat fiyati
  okuyordu). Cap taramasi (hibrit + cikis gecikmeli): cap6 +$444 / cap12 +$834.
  **2026-08-21 GUNLUK CAP (kullanici onayi):** `METAR_PEAK_MAX_BETS_PER_DAY=12`
  eklendi (`.env` -> `config/settings.py` -> `jobs/metar_peak.py`). Backtest cap
  sweep'inde cap12 en karli (+$834, %82.6 winrate; cap7 +$539 ROI en yuksek ama
  cap nadiren dolu — canli 18 Agu'da ~4 bet). Cap, gunluk (UTC) acilan `metar_%`
  betlerini sayar (status bagimsiz: gun icinde kapanan da cap'e sayilir — backtest
  semantigi); cap dolunca YENI bet acilmaz, yanlis-bucket kapatma (aktar/zincir)
  cap'tan bagimsiz devam eder.
  **2026-08-18 audit (C1/C2/C3/M3):**
  stake artik `debit_stake` ile dusulur (onceden HIC dusulmuyordu -> kagit nakit
  yanlizdi); bucket/peak `int(x+0.5)` half-up (banker's round half-even DEGIL);
  DB yes_price CLOB canli ask ile %15'ten fazla sapiyorsa bet reddedilir
  (CLOB hataliysa bet asla engellenmez); saat dilimi `round(lon/15)` nominal yerine
  `scrapers.metar.city_utc_offset()` (zoneinfo + DST: China +8, Seoul +9, London BST +1).
- **METAR vs Polymarket cozum uyusmasi (2026-08-18, `scripts/backtest.py metar_vs_settlement`):**
  Polymarket weather marketleri Weather Underground istasyon verisinden cozulur; WU zaten
  NOAA/NWS METAR verisini yayinlar (ayni istasyon, ayni deger). Test: round(METAR max) ==
  kazanan bucket uyusmasi **%74 (70/95)**; Open-Meteo Archive actual (bias hesabinin
  temeli!) sadece **%30 (55/184)**. Buna gore bias sehir secimi, market cozumunden FARKLI
  bir dogruluga karsi kalibre ediliyordu — spread kayiplarinin olasi kaynagi.
  **COZUM (2026-08-18):** bias kalibrasyonu artik METAR/WU istasyonuna karsi yapilir —
  `scripts/backfill_calibration.py --source metar --apply` (METAR kapsamayan satirlari
  tablodan temizler). Tablo METAR uyumlu yeniden kuruldu: **1280 satir, 0 duplicate,
  0/1080 actual-vs-METAR uyusmazligi, 48 sehir, 8 model.** Gunde bir `evolution_job`
  `--source metar` ile tazeler. Tahmin modelleri sorunlu DEGIL (ecmwf_ifs025 METAR'a en
  yakin, avg|bias|=0.97C) — yanlis olan bias REFERANSI idi; artik referans = cozum kaynagi.
- **Open-Meteo TLS (2026-08-18):** Avast Web/Mail Shield on-makine SSL intercept'i
  `requests`+certifi'yi CERTIFICATE_VERIFY_FAILED'e dusuruyordu (archive/forecast fetch'leri
  bos donuyordu). `data_pipeline/weather_ensemble.py` artik Windows sistem sertifika deposuyla
  dogruluyor (`ssl.create_default_context()` + `_SystemStoreAdapter`) — TLS ACIK kalir, Avast
  kokune guvenir. `scripts/collect_actuals.py` zaten CERT_NONE idi, actuals toplama kesintisiz.
- **Periyodik retry:** Polymarket marketleri zamana yayilarak acildigi icin bot her
  ~60 dk bir en yeni acik tarih icin spread betlerini tekrar dener — sonradan acilan
  esikler (orn. Ankara 32C "NEW") de yakalanir. Secilmeyen sehirlerin acik
  betleri KAPATILMAZ (2026-08-12 kullanici karari — kazanan esikler bile satiliyordu).
- Backtest (orderbook, gercek veri): radius0 + max_entry0.95 + bias-top15 = en karli
  (guvenli pencere 05-16 Agu +$28.67, %34.6 winrate; radius1+bias40 -$200.60 EN KOTU,
  2026-08-17). `scripts/backtest.py orderbook`.
- **Kalibrasyon spread'te kapatilir** (CALIB +$28k < RAW +$37k) — edge-tabanli
  stratejide degerli oldugu icin calculator'da aktif kalir.

| Ayar (.env) | Varsayilan | Aciklama |
|---|---|---|
| `BETTING_STRATEGY` | `spread` | `edge` = eski mod |
| `SPREAD_RADIUS` | `0` | TEK esik: tahmin merkezinin tamamina bet (2026-08-16) |
| `SPREAD_MAX_CITIES` | `15` | tahmini en az sapan ilk N sehir (sicaklik degil) |
| `SPREAD_MAX_ENTRY` | `0.95` | ust fiyat siniri (0.95 ve alti her fiyata acilir, 2026-08-16) |
| `SPREAD_STAKE_USD` | `2.0` | esik basina stake |
| `SPREAD_MAX_BETS_PER_DAY` | `120` | gunluk bet limiti (2026-08-16: "Toplam 120") |

---

## 4c. TIPA-TIPA Replay Backtest Motoru (replay/)

**Anayasa geregi** (CLAUDE.md §7): tum backtestler botun GERCEK kodunu arsivlenmis
veriye karsi calistirir — `scripts/backtest.py` (yeniden yazim modeli) DEGIL.

`replay/` paketi bot'un gercek fonksiyonlarini
(`executor/spread_placer.py::place_spread_bets`, `jobs/metar_peak.py::run_metar_peak_bets`,
`executor/settler.py::SettlementEngine.settle_all`) arsivlenmis 2026-08-04 → 08-22
verisine karsi, kontrol edilebilir saat (`ReplayClock`) + arsiv mock'lari ile calistirir.
**Look-ahead YASAKTIR**: her T aninda yalnizca T'den once var olan veri gorunur
(forecast `fetched_at<=T`, METAR `obs_time<=T`, market `first_seen<=T`, fiyat
zaman-pencereli, settlement yalnizca D+1 00:00 UTC'de).

> **2026-08-23 Fidelity duzeltmeleri** (spread fidelity 0/1079 -> duzeldi):
> 1. **Look-ahead (replay/archive.py):** naive `.timestamp()` makine saatini (UTC+3)
>    varsaydigi icin forecast/market/calibration ~3 saat ERKEN gorunuyordu; artik
>    `_epoch()` (UTC-epok) — replay canliyla ayni zamanda gorur.
> 2. **Retry kadansi (replay/scheduler.py):** 0-13 UTC geceyarisi penceresinde
>    spread retry HER tick'te calisir (canlinin ~1 sn taramasi; eski saatlik retry
>    40 dk gec kaliyordu); yeni veri yoksa cagri atlanir. Fidelity tanimi:
>    market_id esit + `|placed_at|<=15dk` + `|entry|<=0.05`.
> 3. **Fiyat kaynagi (replay/price_index.py):** `entry_price` gamma/snapshot
>    ONCELIKLI (canli yes_price `outcomePrices[0]` kaynagi); orderbook best_ask
>    ikincil; `_seed` look-ahead (zaman-penceresiz ilk-gamma kacagi) KALDIRILDI.
>    Eski orderbook-onceligi replay'i canlidan 30-90 dk ERKEN bet actiriyordu
>    (orderbook ask'i gamma'dan once dusuk gosteriyor) — canli giris fiyati her
>    ornekte gamma'ya esit. Artik market fiyat verisi gelene kadar bet acilmaz.
> 4. **Config drift override (replay/main.py):** `--spread-max-cities N` —
>    canli bot 08-21/22'de eski `max_cities=40` ile calisiyordu (proses 08-22
>    22:04'e kadar restart edilmedi); replay bu donemi 40 ile, gunceli 15 ile
>    uretir (`bot_config.strategy.spread_max_cities` override).

> **2026-10-27 Yeniden insa (replay paketi sifirdan yazildi):** `replay/main.py`,
> `replay/seeding.py`, `replay/clock.py`, `replay/archive.py`, `replay/price_index.py`,
> `replay/scheduler.py` diskten kaybolmustu (git'te hic izlenmemis); `tests/test_replay_engine.py`
> icindeki 3 test bu yuzden fail ediyordu. Paket yeniden yazildi:
> 1. **Look-ahead korumasi iki kat:** fiyatlar yalnizca `t` anindan ONCE okunur
>    (`allow_price_after=False` varsayilan) ve `weather_markets.first_seen > t` olan markete
>    hic girilmez. `_utc_epoch()` naive damgayi acikca UTC sayar (makine saat dilimi kullanilmaz).
> 2. **Forecast kaynagi iki DB:** `bot.db` yalnizca son ~10 gunu tutar (olcum: 2026-10-17..10-29);
>    gecmis pencere `backtest.db`'de (1.106.701 satir, 2026-08-01..09-30). Ikisi birlikte okunur.
>    Bu duzeltilmeden 20 Agu icin **0 forecast** bulunuyor ve replay **0 bet** uretiyordu.
> 3. **METAR bacagi TICK TICK calisir:** her tick'te `detect_peak` yalnizca o ana kadarki
>    gozlemleri gorur, zirve kilitlenince o tick'te girilir. Onceki hal gun sonunu bekleyip
>    gunu tek seferde degerlendiriyordu (giris saati yapay olarak geciyordu).
> 4. **Fiyat yuklemesi pencereye filtrelendi:** `market_snapshots` / `orderbook_snapshots`
>    SQL'de `target_date` ile, `price_history` market_id kumeleriyle. Tek gunluk replay
>    **257 sn -> ~14 sn**.
> 5. **KAPSAM DISI:** canli hibritin **"aktar" zinciri** (zirve asilirsa eski beti kapat,
>    yenisine gir) bu surumde modellenmez — sehir-gun basina tek giris noktasi alinir.
>    Rotation/erken kapanis da yoktur: her bet `settlement` ile kapanir.
>
> Ciktilar: `rapor.md` (Gunluk Ozet), `ledger_bets.csv`, `mechanism_trace.csv`,
> `activity_events.csv`; `fidelity.md` yalnizca `--fidelity` verilirse.

> **2026-11-23 Hibrit METAR modu + zaman duzeltmesi:**
> 1. **Zaman hatasi:** scheduler tick epoch'u naive `datetime.timestamp()` ile aliniyordu ->
>    makine saat dilimi (UTC+3) yuzunden her karar **3 saat eski** veriyle veriliyordu ve UTC
>    gununun son 3 saati gorulmuyordu. Artik `_utc_ts()` (naive = UTC).
> 2. **`--metar-mode hybrid`** (varsayilan `lock` eski davranis): canli `run_metar_peak_bets`
>    kurali — sehir bazli gecmis ortalama peak saati, ucuz (`<= EARLY_MAX_PRICE`) ERKEN giris,
>    pahaliysa 1-dusus kilidi (30 dk onay), AKTAR zinciri (yanlis bucket CLOB bid'den kapatilir,
>    yeni bucket'a girilir), bayat METAR korumasi (45/90 dk), gunluk cap, `NON_METAR_RESOLUTION`
>    (HK) haric. Erken giris varsayilani CANLI ayardir (`METAR_EARLY_ENTRY`, 2026-11-23'ten
>    beri kapali); `--early` / `--no-early` zorlar; `--early-max-price X` esigi degistirir.
> 3. **Look-ahead korumasi (yeni):** METAR gozlemi botun ilk gordugu an (`recorded_at`) oncesi
>    gorulmez; **dolum/satis/esik fiyati o bilgiden SONRA kaydedilmis olmali** (piyasa METAR'a
>    saniyeler icinde tepki verir; bilgi-oncesi fiyatla dolum replay'i yapay iyimser yapiyordu).
>    `--pre-info-prices` yalnizca duyarlilik analizi icindir (look-ahead icerir).
> 4. **`--settle polymarket`:** marketin GERCEK Polymarket sonucu (`raw_data`) > canli betin
>    won/lost'u > arsiv METAR max (yedek; Polymarket'la %88.7, C-bucket'li sehirlerde %95.5 ayni).
> 5. **fixed stake:** METAR stake artik canli `METAR_STAKE_USD` (eski sabit $3 drift'ti).
> 6. **Kirli veri:** 2026-11-17 (restart sonrasi bayat fiyat olayi) snapshot tabanli kiyaslarda
>    dislanmali. METAR arsivi 2026-10-28..11-06 bos.
>
> ```powershell
> # erken giris ACIK (2026-11-23 oncesi canli kural) vs KAPALI, gercek Polymarket sonucu, 10 dk tick
> python -m replay.main --strategy metar --metar-mode hybrid --settle polymarket --tick-minutes 10 `
>   --early --window 2026-10-23..2026-11-22 --out-dir reports/replay/hibrit_acik
> python -m replay.main --strategy metar --metar-mode hybrid --settle polymarket --tick-minutes 10 `
>   --no-early --window 2026-10-23..2026-11-22 --out-dir reports/replay/hibrit_kapali
> ```
>
> Sonuc (2026-11-23): `reports/replay_metar_erken_giris_20261123.md` (bkz. Kararlar Log).

**SAHTE VERI YOK**: tum girdiler arsivden gelir (`data/*.db` 6 DB + `unified/markets.parquet`
+ 30 kalibrasyon parquet). Arsiv DB'leri `mode=ro` ile okunur; bot kendi temp DB'sine yazar.
Production DB'lere HICBIR yazi yok. Market `city_code`'u bot.db'den gelir (canli botun
kullandigi ICAO kodlari) — arsivdeki eski backtest.db kodlari (EGLL/LFPG vb.) METAR'siz
oldugundan replay bunlari islemez.

```powershell
# Tam pencere: her iki strateji + her iki stake modu
#   fixed: $2 spread / METAR = canli METAR_STAKE_USD   compound: bankroll*0.03
python -m replay.main --strategy both --stake-mode both --fidelity `
  --window 2026-08-04..2026-08-22 --tick-minutes 5 --out-dir reports/replay/final

# Eski donem fidelity (canli 08-21/22 max_cities=40 idi) vs guncel (15):
python -m replay.main --strategy spread --stake-mode fixed --fidelity `
  --window 2026-08-19..2026-08-22 --spread-max-cities 40 --out-dir reports/replay/mc40_old
python -m replay.main --strategy spread --stake-mode fixed --fidelity `
  --window 2026-08-20..2026-08-23 --spread-max-cities 15 --out-dir reports/replay/mc15_cur
```

Ciktilari (`reports/replay/<run>/<mode>/`):
- `rapor.md` — gun-gun ledger: acilan/kazanan/kaybeden bet, PnL, nakit, toplam deger
- `ledger_bets.csv` — bet-bet: acilis zamani, strateji, sehir, esik, entry, stake, kapanis sekli (rotation/settlement), pnl
- `mechanism_trace.csv` — mekanizma izi: T-2 acilis, sehir secimi (bias siralamasi), peak kilidi/erken giris, rotation, blok nedenleri
- `activity_events.csv` — bot'un `log_event` ciktisi
- `fidelity.md` — replay-vs-canli karsilastirma (market_id esit + `|placed_at|<=15dk` + `|entry|<=0.05`); yalnizca `--fidelity`

**Dogruluk kaniti**: replay betleri canli `bot.db.bets` ile birebir karsilastirilir
(precision = eslesen replay / toplam replay; recall = eslesen canli / toplam canli).

Test: `python -m pytest tests/test_replay_engine.py tests/test_replay_fidelity.py tests/test_replay_hybrid.py -q`.

Not: bot su an CALISIYOR olsa da replay guvenlidir — arsivler `mode=ro`, bot yalnizca
kendi temp DB'sine yazar.

---

## 5. Bot Loops (bot_loop.py)

| Loop | Interval | Gorevi |
|---|---|---|
| `scan_and_bet_loop` | 300sn (hizli 60sn) | fetch → parse → analyze → bet → update |
| `price_poller_loop` | 300sn | fiyat tazele + acik bet PnL guncelle |
| `settlement_loop` | 120sn | settle + cleanup + daily maintenance |
| `snapshot_loop` | 1800sn (30dk) | market_snapshots kaydi |

---

## 6. Bet Stratejisi

1. Her `(city, target_date, metric)` grubunda **en yuksek yes_price** market secilir.
2. **HER ZAMAN YES** tarafina bet (HIGH/LOW farketmez).
3. `max_entry_price = 0.99` — ustu acilmaz.
4. Pencere + edge + exposure cap + city cap + kasa kontrolu.
5. Smart rotation: ayni grubda daha iyi fiyat cikarsa eski kapatilir.
6. Tie: ayni fiyatta iki market varsa ikisi acilir, geride kalani satilir.
7. `net_edge >= effective_min_edge` zorunludur.

### Formula

- **Probability:** `P(sicaklik > strike) = 1 - CDF(threshold | mean, std)` (Gaussian, days_ahead, market_type)
- **Edge:** `model_prob - market_price`
- **EV:** `edge - fee_rate * price * (1-price)` + slippage/gas
- **Kelly:** `f* = (p*b - q)/b`, `kelly_fraction=0.15`
- **Fee (Polymarket weather):** `fee = shares * 0.05 * p * (1-p)`
- **Unrealized PnL:** `shares * (current - entry)`

### Risk Parametrleri

| Param | Deger | Aciklama |
|---|---|---|
| min_edge | 0.1% - 0.5% (time-close ile escalasyon) | Efektif |
| flat_bet_usd | 10 | Sabit |
| max_bet_amount | 1000 | Tek bet ustu |
| total_exposure_pct | 100% | Toplam marjin |
| kelly_fraction | 0.15 | |
| edge_escalation_hours | 24 | kapanisa yaklasinca 2x min_edge |

> **Erken kapanis (stop-loss / take-profit / trailing / time-decay) KALDIRILDI (2026-08-12):** betler yalnizca settlement'ta kapanir. `RiskConfig`, `run_risk_management`, `_reopen_after_stop_loss`, `check_stop_loss`, `check_take_profit`, `check_trailing_stop`, `check_time_decay`, `check_early_exit`, `check_rebalance` kaldirildi.

---

## 7. CLI Komutlar

```powershell
python main.py bot       # Bot: API + 4 loop + dashboard
python main.py run       # API + dashboard, loop yok
python main.py fetch     # Polymarket piyasasi cek
python main.py weather   # hava tahmini cek
python main.py analyze   # analiz
python main.py bet       # tek seferlik bet
python main.py settle    # settlement
python main.py report    # PnL raporu
python main.py reset     # SIFIRLA (backup alir)
```

---

## 8. API Endpointleri

| Endpoint | Yontem | Aciklama |
|---|---|---|
| `/api/status` | GET | Bot durum, portfoy |
| `/api/health-check` | GET | Health + red flags; `strategy_mode`, `edge_metrics_available`, `summary_all.settlement`/`rotation`, `recent_48h` |
| `/api/markets` | GET | acik piyasalar |
| `/api/bets` | GET | betler |
| `/api/signals` | GET | aktif pozisyonlar |
| `/api/history` | GET | kapanmis bets |
| `/api/equity-curve` | GET | equity |
| `/api/slippage` | GET | slippage |
| `/api/start` `/api/stop` `/api/reset` | POST | kontrol (X-API-Key) |
| `/ws` | WS | live push |

---

## 9. Git Is Akisi (GELISTIRICI NOTLARI ile birlikte)

- Her degisiklik yeni branch: `fix/...`, `feature/...`.
- Once unit + E2E testler, sonra commit.
- **Push kurallari:** `restore/05-clean-state` ana is akisi; ornegin dogrudan itmek yok.
- **Codegraph sync:** Kod degisikliginden sonra `codegraph sync` (veya MCP `codegraph-plugin-sync`) — index guncel kalsin.
- **Bot Restart (Full suite 0 failed ise ZORUNLU):**
  1. Mevcut bot'u durdur: `GET /api/stop` veya `python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8093/stop')"`
  2. Bekle: `timeout /t 3 /nobreak >nul`
  3. Baslat: `start /B python main.py bot`
  4. Dogrula: `GET /api/status` → `is_running=true`

---

## 10. Test

### Test Paketleri (AGENTS.md zorunlu sırası)

| Kategori | Test Dosyaları | Ne Zaman Çalıştırılır |
|---|---|---|
| **1. Latent Bug** | `test_latent_bugs.py` | **Her değişiklikte ÖNCE** (import, dead-code, calibration crash) |
| **2. Lint + Type** | `python quick_check.py --fast` (ruff + mypy) | Her değişiklikte |
| **3. Core Engine** | `test_calculator.py`, `test_calibration_engine.py`, `test_probability_market_types.py` | Calculator/calibration/probability değişikliklerinde |
| **4. Config + Strateji** | `test_faz25_35.py`, `test_strategy_selection.py`, `test_days_ahead_regression.py` | `settings.py` / `strategy.py` / `bet_placer.py` / `spread_placer.py` değişikliklerinde |
| **5. E2E + Liveliness** | `test_e2e_system.py`, `test_integration_e2e.py`, `test_faz2_e2e_mock.py`, `test_liveliness_audit.py` | Her değişiklikte (bot uç-uça akış) |
| **6. Muhasebe + Settlement** | `test_accounting.py`, `test_settler_polymarket.py`, `test_betting_idempotency.py`, `test_signals_active_positions.py` | DRY_RUN / muhasebe / settlement / debit_stake değişikliklerinde |
| **7. METAR / Spread** | `test_metar_peak.py`, `test_metar_peak_module.py`, `test_metar_peak_config.py`, `test_spread_placer.py` | METAR-peak / spread strateji değişikliklerinde |
| **8. Replay / Backtest** | `test_replay_engine.py`, `test_replay_fidelity.py`, `test_replay_hybrid.py`, `test_realistic_backtest.py` | Replay motoru / backtest scripti değişikliklerinde |
| **9. Regression** | `test_regression_fixes.py`, `test_bugfix_coverage.py` | Bugfix sonrası |
| **10. Full Suite (Push Öncesi)** | `pytest tests/ --ignore=tests/test_betting_idempotency.py --ignore=tests/test_comprehensive.py --ignore=tests/test_replay_engine.py --tb=short -q` | **Push öncesi — 0 failed hedefi** |

### Komut Özet

```powershell
# 1. Latent bug (ÖNCE)
python -m pytest tests/test_latent_bugs.py -v --tb=long

# 2. Lint + Type
python quick_check.py --fast
# veya ayrı:
ruff check . --fix
mypy . --ignore-missing-imports

# 3-9. İlgili paket (değişen modüle göre yukarıdaki tablodan seç)

# 10. Full suite (push öncesi ZORUNLU)
python -m pytest tests/ --ignore=tests/test_betting_idempotency.py --ignore=tests/test_comprehensive.py --ignore=tests/test_replay_engine.py --tb=short -q
# -> "667 passed, 8 skipped, 0 failed" (2026-09-06 durumda)
```

> **Not:** `test_betting_idempotency.py` ve `test_comprehensive.py` flaky/uzun olduğu için, `test_replay_engine.py` motoru henuz yazilmadigi icin ignore edilir. Detaylı test stratejisi için `GELISTIRICI_NOTLARI.md` Bölüm 12-13'e bakın.

---

## 11. Sorun Giderme

| Sorun | Cozum |
|---|---|
| Bot baslamiyor | `netstat -ano | findstr :8093`; python processleri temizle; `main.py bot` |
| Snapshot kaydi yok | `data/logs/data_watchdog.log` kontrol; `Start-ScheduledTask JunboSnapshot` |
| Task Disabled | `data_watchdog` otomatik enable eder; elle: `Enable-ScheduledTask -TaskName ...` |
| Orderbook veri yok | `python scripts/collect_orderbook.py` elle; sonra task'a don |
| DB kilitli | tek instance calistigindan emin ol |
| Polymarket ulasilmiyor | Cloudflare WARP `warp-cli connect` veya POLY_PROXY |
| Gamma API `HTTP 421` | `gamma-api` CloudFront'a tasindi; `utils/poly_dns.py` DoH ile cozer (`POLY_DNS_FIX=0` kapatir, `POLY_GAMMA_IP=1.2.3.4` sabit IP verir) |
| Bot gamma-api'ye ulasamiyor ("Host unreachable") ama `warp-cli status` "Connected, healthy" ve `cdn-cgi/trace` `warp=on` | WARP kismi arizasi (2026-11-25): Cloudflare hedefleri acik, Cloudflare disi (CloudFront/Google/Quad9) kapali. 2026-11-26'dan beri `warp_health.ps1` gamma-api'yi de yoklar; 2 basarisiz tick'te kendisi `warp-cli disconnect` + `connect` yapar (log: `FAIL #n gamma-api (CloudFront) unreachable through proxy`). Elle ayni iki komut; `warp-cli mode ...` ASLA. gamma-api'yi `socks5h://` ile yoklama: WARP adi hosts sabitlemesinden cozer, Cloudflare 421 doner ve arizada da ulasilir gorunur |
| Bot `unable to open database file` | `D:\JUNBO data` kok dizini var mi; `data\bot.db` symlink hedefi erisilebilir mi |
| Dashboard yok | `npm run build` sonrasi `Copy-Item out dashboard\out` |
| Testler baska ortamda (Cowork/CI) "no such table: bets" / "connection refused" ile kiriliyor | Bu testler uretim makinesinin durumunu (canli `bot.db`, WARP) dogrular. 2026-11-23'ten beri `tests/conftest.py` `live_bot_db` / `production_host` kapisi: canli DB yoksa "uretim verisi yok" nedeniyle atlanir, uretim makinesinde her zaman kosar (ariza = FAIL). Canli DB'yi okuyan yeni testler bu fixture'i kullanir ve `mode=ro` acar |
| Gorevler terminal penceresi acip kapiyor | Windows 11 + Windows Terminal varsayilan terminal; `-WindowStyle Hidden` yetmez (yeni sekme acar). Cozum: gorev action'i `pythonw.exe scripts\run_hidden.py <komut>` (CREATE_NO_WINDOW). 2026-11-20'de `JunboWARP`, `JunboForecastRunnerWatchdog` ve `JunboForecastRunner` bu sekilde degistirildi; XML yedekleri `C:\JUNBO data\state\*_task_backup_20261120.xml` |

---

## 12. Kararlar Log

- **2026-11-26 (WARP saglik betigine gamma-api yoklamasi):** Kullanici "Evet, ekle" dedi. 2026-11-25 aksam WARP Cloudflare disi hedeflere ulasamadi (CloudFront/Google/Quad9 "Host unreachable"). `scripts/warp_health.ps1` yalnizca `cdn-cgi/trace` (Cloudflare) yokladigi icin 22:34'ten sonra yine "OK" dedi; bot gamma-api'ye ulasamadi, iki canli proxy testi kirildi, reconnect elle yapildi. Cozum: ikinci yoklama `Test-GammaProxy` -- gamma-api adresi Cloudflare DoH'tan (1.1.1.1, proxy uzerinden) alinir ve `curl --resolve` ile socks5:// uzerinden baglanilir (botun `utils/poly_dns.py` yolu). Olcum: `socks5h://` ile WARP adi makine hosts sabitlemesinden cozer ve Cloudflare 421 doner; bu yol arizada da gecerdi, kullanilmaz. Herhangi bir HTTP durumu "ulasilabilir" sayilir (Polymarket tarafi hata WARP arizasi degil, reconnect cozmez); en fazla 2 adres denenir. Iki yoklamadan biri basarisizsa tick basarisiz; esik (2 tick), cooldown (15 dk) ve "mod ASLA degismez" ayni; log satiri nedeni yazar. En kotu sure ~2 dk (gorev siniri PT5M). Dogrulama: gercek betik 4.2 sn, gorev `LastTaskResult=0`; kopya betikle simulasyon (trace gecer, gamma kapali, sahte warp-cli) -> FAIL #1, FAIL #2 + RECONNECT. Test: `tests/test_warp_health_script.py` 9 passed (+6).
- **2026-11-23 (karar: METAR-peak erken giris KAPATILDI):** Kullanici "Evet, kapat" dedi (asagidaki kiyasa dayanarak). `config/settings.py` `StrategyConfig.metar_early_entry=False` (env `METAR_EARLY_ENTRY`); `jobs/metar_peak.py` bayrak kapaliyken `_avg_peak_hour` hesaplamaz -> yalnizca 1-dusus kilidi (30 dk onay) + aktar. `EARLY_MAX_PRICE=0.50` kodda durur (bayrak acilirsa kullanilir). Replay hibrit modu varsayilan olarak canli ayari izler (`--early` / `--no-early` zorlar). Beklenti: gunluk bet sayisi belirgin duser (replay ~2/gun), zarar eden ucuz erken betler biter. Geri donus: `.env` `METAR_EARLY_ENTRY=true` + restart.
- **2026-11-23 (Polymarket METAR kurali + test kok nedenleri + erken giris replay'i):** (1) **Cowork yamasi uygulandi** (commit oncesi bu makinede dogrulandi): Polymarket 2026-10-23'ten beri HK disi tum sehirleri RUTIN METAR (SPECI sayilmaz), T grubu (onda bir) ve `floor(x+0.5)` yuvarlamayla cozer -> `scrapers/metar.py` SPECI'yi atar, sicakligi bu kurala gore okur; METAR-peak/spread/replay tek `polymarket_round`; Panama MPMG; Hong Kong HKO ile cozuldugu icin `NON_METAR_RESOLUTION` (HK'de METAR-peak yok — kara liste DEGIL, `METAR_PEAK_BLACKLIST` bos). (2) **Kirik testlerin gercek nedeni:** bu makinede tek kirik `test_placed_is_only_open` idi — spread'i (2026-11-18'de kapatildi) acik pozisyonlarda zorunlu sayiyordu -> bilinen-strateji kontrolu. Cowork'teki 9 kirik "ortama ozgu" degildi: testler uretim makinesini (canli `bot.db`, WARP) beyan etmeden dogruluyor ve rw `sqlite3.connect` yol yoksa BOS bot.db uretiyordu -> `live_bot_db`/`production_host` kapisi + salt-okunur baglanti. (3) **Replay:** naive timestamp yuzunden 3 saat eski veriyle karar veriyordu (duzeltildi); canli hibrit kurali yeni `--metar-mode hybrid` ile modellendi; bilgi-oncesi fiyat look-ahead'i engellendi. (4) **Erken giris kiyasi** (23-27 Eki + 8-22 Kas, 17 Kas kirli fiyat gunu HARIC, gercek Polymarket sonucu, $2 stake): erken ACIK (canli kural) **197 bet -$132.24**, erken KAPALI (yalnizca 1-dusus kilidi) **37 bet +$14.67**; fark bilgi-oncesi fiyat (iyimser) varsayiminda da ayni yonde (+$225.88 vs +$391.85). Canli: giris <=0.50 155 bet **-$44.66**, >0.50 40 bet **+$11.84**. Erken girisi kapatmak canli davranisi degistirir -> **KARAR KULLANICIDA, kod degismedi.** Rapor: `reports/replay_metar_erken_giris_20261123.md`.
- **2026-11-20 (test altyapisi duzeltmeleri):** Tam suite 5 kirik veriyordu. (1) `test_liveliness_audit` + `test_resolution_rules` hala repo `data/bot.db`'yi ariyordu (canli DB 2026-11-18'de `C:\JUNBO data`'ya tasindi) -> `utils.db_paths.get_db_path()`. (2) `backtest/run.py` import aninda `load_data(12)` calistiriyordu -> `main()` + `__main__` guard. (3) `data/backtest_results/*.py` import taramasina giriyordu -> `data` dizini haric. (4) `backtest/engine.py` orderbook okumasi canli yazici kilidi altinda 30 sn'de dusuyordu -> `busy_timeout` 120 sn. (5) `replay/archive.py` DB yollari sabit `REPO_ROOT/data`'ydi (canli DB tasindi) -> call-time `get_data_dir()`; `replay/main.py` `get_db_path()`. (6) `backtest/run_combos.py` import aninda `load_data` calistiriyordu + kullanilmayan degisken + mukerrer print -> `main()` guard + temizlik.
- **2026-11-20 (terminal pencere flasi):** Kullanici "surekli terminal acilip kapaniyor" bildirdi. WinEvent kancasi + process izleme ile olculdu: kaynak `JunboWARP` gorevi (her 5 dk, Interactive, `powershell -WindowStyle Hidden -File warp_health.ps1`). Windows 11'de varsayilan terminal Windows Terminal oldugu icin `-WindowStyle Hidden` yeni bir terminal sekmesi aciyor ve script bitene kadar (~40 sn) acik kaliyor; saatlik `JunboForecastRunnerWatchdog` da ayni. Cozum: yeni `scripts/run_hidden.py` (`pythonw.exe` + `CREATE_NO_WINDOW` sarmalayici); uc gorevin action'i (`JunboWARP`, `JunboForecastRunnerWatchdog`, `JunboForecastRunner`) `pythonw.exe run_hidden.py powershell.exe ...` yapildi; `JunboForecastRunner` calisirken yalnizca tanim degistirildi, yeni tanim bir sonraki baslatmada devreye girer. Dogrulama: `JunboWARP` ve `JunboForecastRunnerWatchdog` elle tetiklendi -> `LastTaskResult=0`, WinEvent izlemede **0 pencere olayi**. Yedek/geri donus: `C:\JUNBO data\state\JunboWARP_task_backup_20261120.xml` ve `JunboForecastRunnerWatchdog_task_backup_20261120.xml` -> `Register-ScheduledTask -Xml (Get-Content <yedek>) -TaskName <ad> -Force`.
- **2026-11-18 aksam (denetim duzeltmeleri):** (1) **Yedek C:'yi dolduracakti:** `backup_databases.py` 6 saatte bir 4 DB'yi (~7 GB) kopyalayip 90 gun sakliyordu (~28 GB/gun, C: ~5 gunde dolacakti). Artik UTC gununde 1 kez (marker `.last_backup_databases_run`) + prefix basina son 7 kopya; bot.db'nin 30 gunluk dogrulanmis serisi `db_backup.py` `bot_scheduled`'da. (2) `.env` `JUNBO_HEAT_DB`/`HEAT_MAIN_DB`/`OM_CACHE_DB` hala D:'yi gosteriyordu (runner tasindiktan sonra donmus kopya) -> C:. (3) **CLOB REST yedegi hic calismamisti:** `get_session()` commit'i ORM nesnelerini expire ediyor, `raw_data` session disinda okununca `DetachedInstanceError` yutuluyordu -> 0 HTTP istegi, `token_id='0'` arsiv satiri 0. Duzeltildi (kolon sorgusu + duz kopya); arsiv toplu tek transaction; WS arsiv yazimi `asyncio.to_thread`; `collect_orderbook.py` 100 satirda bir commit (tek transaction kilidi 3-19 dk tutuyordu). (4) **WeatherNext 3+ gun cekilmedi:** runner kapisi `utcHour % 12` tur basinda sabitti, uzun/kayan turlar 00/12'yi atliyordu; ayni sorun Open-Meteo (6s) ve IEM (24s). Artik UTC slot kapisi (`forecast_runner/windows-runner/gates.mjs`, durum `state\gated_slots_utc.json`). (5) `JunboWARP` gorevi `scripts\warp_health.ps1`'e yonlendirildi (5 dk, proxy yoklamasi + reconnect, mod degismez); eski `start_warp.ps1` `mode warp` ile SOCKS'u kapatiyordu. (6) `bot_watchdog` uzun data-watchdog tick'ini artik "stale" saymaz (gorev Running ise 40 dk'ya kadar normal). (7) `backtest/*.py` ve 2 testteki D: varsayilanlari `utils/db_paths`'e baglandi. Not: `gamma-api` HTTP 421 makine hosts sabitlemesinden; bot `utils/poly_dns.py` ile asiyor.
- **2026-11-18 (spread kapatma + veri/DB tasima):** Gercek orderbook ask + gercek Polymarket sonucu ile 2 aylik backtest: spread **1.067 bet, %22.3 win, -$854.67** (edge filtreli varyant da negatif); METAR hibrit yogun pencerede (3-17 Agu) **195 bet, %81.5 win, +$1.032,00**. Karar: `.env` `SPREAD_ENABLED=false`; spread placer ve edge dongusu bet acmaz, yalnizca METAR-peak calisir (PAPER mod). Veri denetimi: `orderbook.db`'de **29 Eyl-10 Kas arasi 44 gun snapshot yok**; canli `weather_forecasts` yalnizca Kasim, `backtest_prices.db` yalnizca 3-17 Agustos. Bot DB'leri `C:\JUNBO data`'ya tasindi; D: yedek olarak korunuyor. Backtest araclari: `%` help crash, O(n^2) market lookup, PK tabanli aralik ve indeksli orderbook okuma duzeltildi. **Ekim boslugu kok nedeni:** `JunboDataWatchdog` 19.09-11.11 arasi durdu; `JunboSnapshot` + `Junbo-BackupDatabases` Disabled kaldi; `ensure_task_enabled` komutu bozuktu. Fix: gecerli PowerShell + tam liste, data-watchdog bekcisi, CLOB arsiv yolu. Runner DB'leri (`tempmarket-edge/main`, `kardesler`) C:'ye tasindi, `forecast_runner\data` junction C:.
- **2026-11-13 (denetim + debug fix):** (1) `/api/health-check` spread-farkinda: verdict son 48s NET PnL'e bagli; rotation ve settlement ayri (`summary_all.settlement`/`rotation`, `recent_48h`); `strategy_mode` + `edge_metrics_available` eklendi; gunluk PnL timeline rotation kapanislarini da iceriyor. Canli dogrulama: verdict `healthy`, son 48s net +$20.55, settlement %70.7. (2) DB yolu tek kaynak: `utils/db_paths.py`; `activity_log` + 33 script + `executor/settler.py` (actuals) + `db_backup.py` + `jobs/collect_settlement_temps.py` canli `D:\JUNBO data\bot.db`'ye baglandi; repo `data/bot.db` ticaret icin kullanilmiyor; `reset_bot.py` artik `--yes` istiyor. (3) `JunboBotWatchdog` tekrar enable (1 dk). (4) `test_no_dead_public_functions` uretilmis `data/` scriptlerini taramiyor. Yeni testler: `test_health_check_spread_aware.py` (4), `test_db_path_consistency.py` (5). Dogrulama: quick_check 7/7, latent 5/5, health/db/backup 18/18, E2E+liveliness 76/76, muhasebe+settlement 17/17, tam suite **773 passed, 7 skipped, 0 failed (12:11)**.
- **2026-11-10 (Ponytail temizlik):** `forecast_runner` 1003 -> 31 dosya (kullanilmayan Heat scriptleri `D:\JUNBO data\_eski\heat_runner_unused_20261110`'a arsivlendi); `scripts/catalog` -> `D:\JUNBO data\catalog`; dev-only `node_modules` (975 MB) ve `.next` silindi (dashboard `out/`'tan servis ediliyor; rebuild: `npm install && npm run build`); cache/eski log temizlendi; root debug scriptleri `scripts/archive/debug/`'e tasindi; 9 F401 duzeltildi. Dogrulama: quick_check 7/7, hedefli 38/38, dashboard canli (tum API 200); tam suite 763 passed, 7 skipped (tek canli proxy testi ilk kosuda flake, tekrar 1/1 passed).
- **2026-11-10:** **Forecast runner (eski HEAT runner) Junbo icine gomuldu.** Heat projesinin calisan Node runner'i (`windows-runner/*` + gerekli `scripts/*`) `junbo/forecast_runner/` altina kopyalandi; veri yollari `D:\JUNBO data` kokune sabitlendi (`tempmarket-edge.sqlite` LIVE, `tempmarket-main.sqlite` MAIN, `kardesler.db`; ayrica `state`/`models`/`inbox`); `asia` ve `heat` dizinleri tamamen silindi. `start-runner.ps1` tum yollari env olarak verir (`TEMP_MARKET_DATA_DIR`, `TEMP_MARKET_DB`, `HEAT_LIVE_DB`, `HEAT_MAIN_DB`, `KARDESLER_DB`, `OM_CACHE_DB`, `HEAT_DAILY_BLOCK_STATE`, ...). Yeni gorevler: `JunboForecastRunner` (logon, `run-loop.ps1`) + `JunboForecastRunnerWatchdog` (saatlik); eski `TempMarket Edge Runner` ve `TempMarket Edge Watchdog` **Disabled**. `forecast_runner/data` junction'i canli HEAT deposuna baglidir. Eski Heat projesi artik gerekmez (silinebilir): tahminler LIVE DB'ye yazilir, Junbo `JUNBO_HEAT_DB` ile okur ve `heat_mos` job'u aktarir. Dogrulama: embedded node port 8787 `/health` ok; `heat_mos_job.run()` 12 satir ekledi.
- **2026-08-07:** DRY_RUN kalici (paper mod).
- **2026-07:** Erken acilis SL sorunu → 8-18h kurali; daha sonra 04-23:30 temel pencere.
- **Peak analiz:** Ilk-peak saatleri UTC band 10:00 / 19:00 / 23:00 — pencere 04-23:30 bu bandi icerir.
- **2026-08-08:** Snapshot+orderbook kesin 30dk; WakeToRun; DataWatchdog; pencere [04:00-23:30]; SL sonrasi yeniden acilim pencereye baglandi (`_reopen_after_stop_loss` — Wellington 12C/13C gece cift kayip duzeltildi); Dashboard'da `strike_temp` (Sıcaklık) kolonu; TS tip hatalari sifirlandi (24 hata); snapshot task path + uyku/wake timer duzeltildi.
- **2026-08-08 (ikinci tur):** `target_date` 12:00 etiketi artik kapanis (24:00) sanilmiyor — 12:30 UTC sonrasi bet acilamama bug'i duzeltildi (SL sonrasi reopen dahil; Miami 33.6C yeni peak acildi). `max_openable_now` nakitle sinirlandi (`min(nakit, limit)`); "Gercek Kayip" KPI kaldirildi (fee zaten PnL icinde), yerine fee islem sayisi eklendi.
- **2026-08-09:** `scrapers/meteo.py` `fetch_for_markets` icindeki tanimsiz `_fetch_open_meteo_model` kirik loop silindi (dead/kirik kod + cift istek riski). DB bakimi eklendi: `scripts/db_maintenance.py` + `data_watchdog` gunde 1 kez utc ANALYZE+VACUUM (02-04 UTC). Ilk run: bot.db 157.9 → 146.6MB.
- **2026-08-10:** **Bayat fiyat guard'ı** — Beijing 32°C beti 0.18'den acildi ama gercek CLOB fiyati 0.98 idi (Gamma ~1 saat bayat). Bet acmadan once artık `utils/clob_live.py` ile canli CLOB ask/bid cekilir; DB fiyati canlidan >%15 saparsa bet reddedilir (CLOB erisilemezse eski davranis korunur). Test: `tests/test_clob_live.py`.
- **2026-08-10 (ikinci tur):** Ayni bayatlik 41 acik betten 17'sini etkilemisti — `scripts/fix_stale_entry_prices.py` ile elle duzeltildi (entry_price/shares/fee/pnl gercek CLOB fiyatina gore). Test: `tests/test_fix_stale_entry_prices.py`. DB backup: `data/backups/bot_pre_pricefix_*.db`.
- **2026-08-10 (ucuncu tur):** **Model kalibrasyonu aktif** — `historical_calibrations` 0 satirdi, `_run_calibration_backfill` bos govdeydi. `scripts/backfill_calibration.py` kendi verisiyle (weather_forecasts × actuals) **58,064 satir** doldurdu; `utils/calibration.py` (CalibrationEngine, ASIAbot'tan tasindi) calculator'a baglandi — her model tahmini sehir/model MBE ile duzeltilir (bias_map yoksa eski davranis). Gunde 1 kez otomatik backfill. Test: `tests/test_calibration_engine.py`.
- **2026-08-10 (dorduncu tur):** **Erken giris + spread backtest** — `scripts/backtest_early_spread.py`: market acilir acilmaz (ilk snapshot fiyati), meteo tahmini etrafinda ±3 dereceye (spread) YES bet, max_entry<0.30, kalibrasyonsuz → **813 bet, %50.6, +$36,814** (5/5 gun pozitif). Kalibrasyon spread'te zararli (CALIB +$28k < RAW +$37k) ama edge-tabanli stratejide degerli — calculator'da aktif.
- **2026-08-10 (besinci tur):** **Spread stratejisi ANA MOD** — `executor/spread_placer.py`: yeni 2-gun-sonrasi tarih acildiginda en son meteo tahmini +/-3 dereceye, ilk snapshot fiyatindan, en sicak ilk 15 sehre YES bet (gunluk 30 limit). **Kayan pencere:** tahmin guncellenince yeni pencerenin disinda kalan esikler kapatilir. `BETTING_STRATEGY=spread` (varsayilan) / `edge` (eski mod, geri donulebilir). Test: `tests/test_spread_placer.py`.
- **2026-08-10 (altinci tur):** **Spread commit bug'i duzeltildi** — `place_spread_bets` `with get_session()` wrapper'a cekildi (bet'ler commit edilmiyordu -> ayni markete dup bet). `snapshot_job` `YES_PRICE_MIN` 0.005 -> **0.0005** (0.005 alti longshot marketler artik fiyat gecmisinde); `_first_snapshot_price` snapshot yoksa market yes_price'a fallback. 11-12 Agustos icin catch-up: ~190 spread bet acildi, dup'lar kayan pencere ile temizlendi.
- **2026-08-11:** **SPREAD_MAX_ENTRY 0.30 -> 0.99** (kullanici karari). **Spread modunda stop-loss devre disi** (`run_risk_management` spread'de SL atlar). **Portfolio yoksa spread placer otomatik olusturur** (0-cash sessiz skip bug'i). SPREAD_MAX_BETS_PER_DAY kalici **100**. Tam suite bot kapaliyken kosulmali (bot + test ayni anda production DB'yi bozuyordu).
- **2026-08-12:** **Erken kapanis mekanizmalari komple kaldirildi** (kullanici karari: "sistemde hicbir yerde stoploss/take-profit/partial-TP ve benzeri kalmayacak"). `RiskConfig`, `run_risk_management`, `check_stop_loss`, `check_take_profit`, `check_trailing_stop`, `check_time_decay`, `check_early_exit`, `check_rebalance`, `check_model_reversal`, `_reopen_after_stop_loss` ve `partial_tp_done` kolonu kaldirildi. Betler yalnizca settlement'ta kapanir (backtest ile ayni davranis). SL/TP test dosyalari (test_active_risk_management, test_take_profit_comprehensive, test_risk_behavior, edge/test_sl_reopen_chain, scripts/replay_test) silindi.
- **2026-08-12 (ikinci tur):** **Top-15 kapatma KALDIRILDI** (kullanici karari: "ilk 15 bias sadece yeni gun aciliminda kullanilacak, kapatma yapilmayacak"). Sehir top-15'ten dusse bile acik betler settlement'a kadar TUTULUR — sadece yeni bet acilmaz. Bu, Istanbul 13 Agustos betlerinin tamaminin "out of top-15 selection" ile satilmasini (kazanan esikler dahil) onler. Kullanilmayan `tie_loser` (`close_losing_twin_bets`), `stale_cleanup` (`_cleanup_stale_bets`) ve tarihsel `24h_rule` mekanizmalari da silindi (kodda kapanis uretmiyorlardi). Bet loglarina `bet#ID` eklendi (izlenebilirlik).
- **2026-08-12 (ucuncu tur):** **Backtest dogrulamasi** — `max_entry` taramasi (0.29-0.12) ve spread (3/5/7) x kaydirma (shift/noshift) kombinasyonlarinin tamaminda EN KARLI senaryo: **spread=3 + KAYDIRMASIZ (acilan bet settlement'a kadar) + tum fiyatlar = +$74.26**. `spread_max_entry=0.30` kazananlarin %65'ini kesiyor (0.30 alti winrate %8.6 vs 0.30 ustu %45) — bot'un canli config'i hala 0.30, karar logu (0.99) ile celisiyor.
- **2026-08-14:** **METAR zirve-tespiti tek esik stratejisi.** Polymarket weather marketleri Weather Underground istasyon verisinden cozuluyor; WU ticari API ~$500/ay ama aviationweather.gov (NOAA resmi METAR, **bedava**, 30dk guncelleme) AYNI istasyon verisini verir. `scrapers/metar.py` + `jobs/metar_peak.py`: acik marketli sehirlerin METAR'ini gun icinde izler, sicaklik max'a cikip **2 kez arka arkaya dustugunde** zirve kilitlenir, o sehrin kazanan bucket'ina (round(max)) **tek esik YES** bet acar ($1 stake, order_id `metar_*`). `bot_loop.metar_loop` 30dk'da bir calisir. Kapanisa <4 saat kalan sehirler atlanir. Sermaye +1000 USD (cash $191->$1191). Manuel dogrulama: 14 Agu'da 5 bet acildi (London 25C, Dallas 37C, Toronto 26C, Mexico City 19C, Buenos Aires 10C, entry 0.010). Kullanici karari: "acik bet sehirleri listesini al, metardan takip et, dustugunu teyit edince beti yapistir, %100 onemli degil, tek esik kayip az". Suite: 636 passed.

- **2026-08-16:** **Polymarket proxy + regression testleri.** Kullanici "ben polymarkete giriyorum bot nasil giremiyor?" dedi. Kok neden: sistem PAC dosyasi Polymarket'i SOCKS `127.0.0.1:40000`'a (WARP) yonlendiriyor; Python requests PAC kullanmadigindan DIRECT'ten `10054` aliyordu. `.env` `POLY_PROXY=socks5h://127.0.0.1:40000` + `AsyncHttpClient(proxy=...)` + `polymarket._fetch_raw_markets` proxy ile — bot artik 18 Agustos marketlerini goruyor (DB'de 289). **Regression testleri (`tests/test_regression_fixes.py`):** proxy (canli + tarayici fallback), CITY_ICAO_MAP 7 sehir istasyon duzeltmesi, RKSI koord, orderbook arsiv, Gamma rate limit, partial_tp migration. Suite: 665 passed.
- **2026-08-16:** **METAR/Open-Meteo global env proxy sizintisi.** `config/settings.py` Polymarket SOCKS proxy'yi `os.environ` global olarak set ediyordu -> aviationweather.gov + open-meteo da proxy'den gitmeye basladi (geo-block degiller ama 20s timeout/502). 172 METAR hatasi yuzunden METAR-peak (kesin sonuc, %91.7 backtest) betleri acilamiyordu. Cozum: `requests.get(..., proxies={"http": None, "https": None, "all": None})` ile bu iki kaynak DIRECT. Suite: 664 passed.
- **2026-08-16:** **Orderbook toplamayi TUM weather marketlere genisletme.** `bot_loop.clob_stream_loop` SADECE acik betli marketleri dinliyordu (`_asset_ids` Bet join'li) -> orderbook gecmisi sadece bet acilan marketleri kapsiyordu (backtest "fiyat-yok" ile sinirli). Kullanici: "tum weather betlerin orderbook'u cekilecek". Cozum: (1) `clob_stream_loop._asset_ids` artik `WeatherMarket.status=='open'` olan TUM marketleri dinler + arsivler; (2) `scripts/collect_orderbook.py` bot.db'deki acik marketlerin YES token orderbook'unu (best_bid/ask/depth) toplar, `--loop --interval 900` ile bot entegrasyonu; (3) `_archive_clob_price` orderbook_snapshots'a threshold yazar. Gamma `events?tag_slug=weather` YANLIS kategoriler doner (April 2024 temperature increase) — dogru kaynak bot.db. Ayrica `weather_forecasts.city` = ICAO = `weather_markets.city_code` (49/49 eslesir); T-2 erken forecast sadece 16 Agu target'lilar icin var (forecast kaydi 08-14'te basladi). METAR'a en yakin tahmin: `ecmwf_ifs025` (avg|bias|=0.97C). Suite: 663 passed.
- **2026-08-16:** **METAR-peak YEREL saat mantigi.** Kullanici: "benim saatimle degil, sehirin YEREL saatine gore gir. Yerel saatte en yuksek ne zaman oluyorsa o zaman gir." Sorun: `detect_peak` sabit UTC>=15:00 esigi kullaniyordu -> dogu sehirleri (Wellington 03:00 UTC, Hong Kong 07:00 UTC, Seoul 07:00 UTC) peak'i kaciriyordu; ayrica `MIN_HOURS_BEFORE_CLOSE=4` botu kapanisa 4 saat kala girmeye zorluyordu, o zamana kadar fiyat 1.00 olmustu. Cozum: `detect_peak(day_rows, min_local_hour=13, utc_offset_hours=lon/15)` — kilitlenme kurali YEREL saat uzerinden (boylamdan kaba UTC offset); `metar_peak` her market icin offset'i lon/15 hesaplar; `MIN_HOURS_BEFORE_CLOSE` 4 -> 2 (erken giris). Test: Wellington UTC+12, Hong Kong UTC+8 yerel 15:00 peak kilitler. Suite: 667 passed.
- **2026-08-16:** **METAR-peak yanlis bucket betlerini kapatma (3. adim).** Kullanici: "T-2 oncesi actigimiz bet kazanan bucket'ta degilse onu kapatiyoruz. 2 gun onceden meteoya gore bet ac, her sehirin yerel saatinde max olunca tekrar gir, tutmuyorsa kapat." Sorun: bot iki baglantisiz strateji calistiriyordu — spread (T-2'de acar, ASLA kapatmaz) ve metar-peak (peak'te EK bet acar, yanlislari kapatmaz). 16 Agu'da 75 acik bet, sadece 6'si kazanan bucket'ta — 69 yanlis bet settlement'a kadar acik kaldi. Cozum: `jobs/metar_peak.py::_close_wrong_bucket_bets` — peak kilitlendiginde o sehrin kazanan bucket DISINDAKI tum acik betleri canli fiyattan kapatir (close_bet_for_rotation ile, portfolio kredisi). Suite: 668 passed.
- **2026-08-16:** **3 ESIK (radius=1) + peak'te komsu satisi.** Kullanici fikri: "3'lü eşik açarsak, peak yaklaşırken komsu esikler de yukselir. Gercek esik bizim esiklerimizden biriyse, diger 2 komsuyu HEMEN satarsak (millet uyanmadan) onlardan da para kazaniriz." Cozum: `spread_radius` 0 -> 1 (merkez±1, T-2'de 3 esige dusukten gir); peak gunu kilitlenince kazanan bucket TUTULUR, komsular `_close_wrong_bucket_bets` ile canli fiyattan satilir. Yarinki orderbook verisiyle dogrulanacak (YAPILACAKLAR.md). Suite: 667 passed.
- **2026-08-17:** **METAR paralel fetch + CLOB REST yedegi + gunluk limit 120.** (1) `run_metar_peak_bets` 40 sehri TEK TEK cekiyordu -> 60s `_FETCH_TIMEOUT`'a dusup "METAR poll timed out" oluyor, peak'ler kaciyordu. Cozum: `ThreadPoolExecutor` (8 worker) ile paralel METAR fetch — 40 sehir ~35s'de biter. (2) CLOB WebSocket proxy'den gidemiyor (WARP SOCKS WS desteklemiyor -> `General SOCKS server failure`; direct -> geo-block). Cozum: `clob_stream_loop` WS 3 kez fail edince `_clob_rest_poll_once` (REST GET /book, proxy ile) yedigine gecer — fiyat verisi toplanmaya devam eder. (3) `spread_max_bets_per_day` 40 -> 120 (kullanici: "Toplam 120") — 40 iken 17+18 Agu dolu, 19 Agu'ya hiç sira kalmıyordu. Suite: 667 passed.
- **2026-08-17:** **KARAR: radius=0 (tek esik) + bias-top 15 + METAR MIN_ENTRY=0.10.** Canli METAR-peak analizi (30 bet NET **-$32.84**): 24 longshot (entry 0.01-0.03) **-$39.90** kaybetti, entry>=0.10 6 bet **+$7.06** kazandi -> `MIN_ENTRY=0.10` eklendi (piyasa bucket'i 0.01'e fiyatliyorsa ~%1 sans = METAR tespiti yanlis). Safe-window orderbook backtest (05-16 Agu): guncel radius=1+bias40 MATRISIN EN KOTUSU (**-$200.60**); radius=0+bias15 en iyi (**+$28.67**, %34.6 winrate) -> .env'de `SPREAD_RADIUS=0`, `SPREAD_MAX_CITIES=15`. Komsu-satisi KENDINI KURTARMIYOR (canli 421 kapanis NET **-$184.92**, guvenli pencere 349 bet -$159.07; HOLD -$473 vs SELL -$134) — sadece hasar kontrolu, zarari ~$340 kurtariyor. SESSION_OZET'teki METAR "+$139 / ROI %165" CLAIRVOYANT (look-ahead + dead code), GERCEK DEGIL. Gercekci: `scripts/backtest_metar_peak_realistic.py`. CLOB REST poll paralel (16 worker) — sequential ~1900 market en kotu ~8 saat yerine dakikalar. Suite: 660 passed.
- **2026-08-18:** **AUDIT FIXLERI (C1/C2/C3/M3/M12 + WS fallback).** (1) **C1** `jobs/metar_peak.py` stake artik `debit_stake` ile dusulur — onceden HIC dusulmuyordu, kagit nakit ve exposure yanlis kaydediliyordu. (2) **C2** banker's rounding (Python `round()` half-even) -> half-up `int(x+0.5)`: `spread_placer` merkez, `metar_peak` bucket + kazanan karsilastirma, `backtest_gunluk` ayni kurala cekildi (26.5C artik bucket 27, 26 degil). (3) **C3** stale/fantom fiyat guardi: DB `weather_markets.yes_price` CLOB canli ask ile %15'ten fazla sapiyorsa bet REDDEDILIR (spread_placer + metar_peak; CLOB hataliysa bet asla engellenmez — bet_placer ile ayni kural). (4) **M3** saat dilimi `round(lon/15)` nominal yerine `scrapers/metar.city_utc_offset()` (zoneinfo + DST): China +8 (yoksa +7), Seoul +9, London BST +1, Lucknow +5:30 dogru. (5) **M12** `_close_wrong_bucket_bets` yalnizca `temperature_max` + `RANGE` marketlerini kapatir (temperature_min/HIGH/LOW'ya bucket karsilastirmasi uygulanmaz). (6) **WS->REST fallback:** `clob_stream.run()` 3 art arda baglanti hatasinda (`max_retries=None`) dis donguye firlatir — onceden sonsuz ic retry `ws_fail_streak`'i artirmiyor, REST yedegi HIC devreye girmiyordu; `_clob_rest_poll_once` artik yes_price/no_price/last_updated'i de gunceller (REST fiyati canli besler). Testler: `test_metar_peak.py` (M12 regresyon + C1 debit), `test_clob.py` (3-fail escalation), `test_latent_bugs.py` allowlist. Suite: **671 passed, 8 skipped, 0 failed**; ruff+mypy+format temiz.
- **2026-10-13:** **Equity grafigi duzeltildi** — rotation-kapanislar (`closed`, net -564) egride sayilmiyor, ~1680 gosteriyordu. Simdi tum kapali statusler + `realized_pnl` ile tepe ~1122 (gercek birikimli PnL). Dogrulama: `/api/equity-curve` canli.
- **2026-10-22:** **Settlement gurultusu + Gamma 403 duzeltildi** ("20 ekim kapanmamis betler var neden?"). (1) **Tespit:** 20 Ekim target'li 23 acik betin 20 marketi canli Gamma'da `closed=false, umaResolutionStatus=null` (fiyatlar 0.86-0.99) — yani Polymarket/UMA henuz resmi cozumlememis; cozulmus marketler `closed=true/uma=resolved`. **Kaynak gecikmesi, bot hatasi degil** — bot yalnizca resmi UMA cozumlemesini kabul eder (fiyat-tabanli settlement bilincli olarak KAPALI). (2) **Duzeltme:** `settle_all` artik yalnizca ACIK BETI OLAN marketleri Gamma'ya sorgular (beti olmayanlar toplu `expired`); onceki hâli her turda ~945 marketi isliyor, "945 pending" + 24.328 `pending >48h` log satiri uretiyordu. (3) `_call_gamma_api` artik `User-Agent` gonderiyor (scraper ile ayni `Junbo/1.0` UA) + 403/429 icin 3 deneme; onceki hâli basliksiz istek atiyordu (log'da 2127 "Gamma API request failed"). Kural degismedi. Test: `tests/edge/test_settlement_chain.py`, `tests/test_settler*.py`, `tests/test_latent_bugs.py`.
- **2026-10-12:** **Ekran rakamlari DB gercegine baglandi.** Rotation-kapanis `Bet.pnl` yazmiyordu (kapali toplam -409.10 vs -536.09 ayrismasi) -> kod + 414 satir backfill ile tek kaynak (`realized_pnl`); API status cift hesabi temizlendi; "Toplam sermaye" tooltip/formulu gercek hesaba cekildi (nakit + acik stake + acik PnL). Dogrulama: API 97.22 / 0.61 / 97.83 / fee 87.53 / nakit 1282.05 / acik 68 uyumlu; 18 test passed; bot restart.
- **2026-08-18:** **GERCEKCI BACKTEST (look-ahead kapandi + fill dogrulandi).** Kullanici: "hani butun veriler elinde vardi, kazaniyor muyuz kayip mi ediyoruz, gercekci backtest yazamiyor musun, kac gunluk veriye ihtiyacin var." **CANLI GERCEK: KAYIP -$483.66** (1,452 bet; won +$743 / lost -$692 / closed -$220 / closed_early -$315). Kaybin ~%61'i longshot (<$0.10, 545 bet -$162) + 10-11 Agu eski-config gunleri -$286. **Backtest arizasi:** `gunluk` bot.db weather_forecasts okuyordu (ROTATED: 05-13 Agu hedefleri 14-Agu'da backfill) -> 06-13 Agu'yu botun goremedigi forecast'lerle oynuyordu (LOOK-AHEAD; eski +$415.97 yaniltici). **COZUM:** forecast `backtest.db` gercek gunluk batch'lerinden (02-18 Agu) + `fetched_at <= kapanis` kapisi; yeni `--real-entry` bayragi sim entry'sini ayni marketteki gercek bot fill'iyle degistirir (config sinirlari uygulanir). Duzeltilmis sonuc 05-17 Agu: **+$353.08** (173 bet, %71 winrate; ideal fill) ~ **+$354.41** (gercek-fill capraz) -> fill modeli optimistik DEGIL. Sim sadece fiyat verisi olan marketleri simule eder (173 vs canli 1,452 bet) — survivorship siniri. Veri: 02-18 Agu gercek batch (17 gun), kullanilabilir pencere 05-17 Agu; 30-60 gun istatistiksel guven icin ideal, Open-Meteo gecmis servis etmediginden sert limit. Rapor: `reports/backtest_gercekci_2026-08-18.md`; prob: `scripts/_probe_fill.py`.
- **2026-08-18:** **1 DUSUS KILIT + AKTAR (kullanici stratejisi).** Kullanici: "20 21 22 22 21 diyorsa ikinci 21 ve altini bekleme 22 ye bet ac; 23 e cikarsa 22 yi kapa 23 e ac; 23 e ciktiginda 1 adet dusmesini beklemeyecek hemen acacak." Degisiklikler: (1) `detect_peak`/`peak_lock` 2 dusus -> **1 dusus** kilit (erken giris, fiyat 0.99'a oturmadan); (2) kilit bozulunca (cur_max > kilitli peak) eski betler kapatilir VE **yeni zirvenin bucket'ina dusus beklenmeden bet acilir** (aktar zinciri; canli `jobs/metar_peak.py` her dongude, backtest `metar_peak_live`/`gunluk` 2 adim). Backtest 05-17 Agu: `metar_peak_live` **202 bet %88.6 +$593.62 ROI +%98** (onceki 159 bet +$375.81); `gunluk` BIRLESIK **311 bet +$955.31 ROI +%120.5** (onceki +$471.54). Test: `test_metar_peak.py` 1-dusus kurali + kullanici ornegi (22 kilit), `TestMetarPeakBrokenLock` aktar dogrulamasi.
- **2026-08-18:** **ONAYSIZ RESTART: `/api/restart` endpoint'i.** Kullanici: "onaysiz yap". Non-admin shell LocalSystem servisini durduramaz; `/api/stop` sureci cikarmaz. Cozum: `api.py`'de `/api/restart` — `os._exit(1)` ile kasitli cikis; SCM failure-recovery (JunboBot: RESTART 5s/10s/30s) sureci otomatik yeniden baslatir, guncel kod yuklenir. Kullanim: `curl -X POST http://127.0.0.1:8093/api/restart`. Endpoint'in canliya yuklenmesi icin bir kez admin restart gerekir; sonrasinda tum restart'lar onaysiz.
- **2026-08-18:** **YENI CONFIG BACKTEST SONUCLARI (bias'siz peak + kilit-bozulma kapatmasi).** `gunluk` backtest yeni canli config'e cekildi: peak TUM sehirler (bias-top 40 kaldirildi) + kilitli peak ASILINCA asilma aninda kapatma (hem spread hem peak legi). 05-17 Agu: **SPREAD 96 bet +$126.64 | PEAK 116 bet %95.7 +$344.90 | BIRLESIK 212 bet %76.9 NET +$471.54** (eski bias-40'li +$353.08'den +$118 iyilesme). `metar_peak_live` da ayni kurala cekildi: 168 bet %83.3 **+$358.15** (onceki +$304.83; kilit-bozulma kapatmasi +$53 ekledi, winrate %79.2->%83.3). Bot 19:38 UTC'de restart edildi, yeni kod canlida.
- **2026-08-18:** **MILAN ERKEN KILIT DUZELTMESI + METAR-PEAK BIAS FILTRESI KALDIRILDI.** Kullanici: "kapatma mekanizmasi yok ya koy; Metar betleri acilirken bias a gerek yok, nasil olsa peak tespit edilmis oluyor." Canli olay: Milan'da 31C kilitlendi, bet acildi, sonra 32C geldi — kapatma yeni zirvenin 2 dususle kilitlenmesini beklerken 31C fiyati 0.0005'e coktu (~-$3). Cozum: (1) kilitli peak ASILIRSA derhal kapat (2 dusus beklemeden, yeni cummax kazanan sayilir), kapatma kazanan-bucket marketi olmasa da cagrilir (sehir-gun basina bir kez); (2) **METAR-peak bias-top 40 sehir filtresi KALDIRILDI** — peak tespit edildigine gore bias gereksiz, TUM sehirlerin acik RANGE+max marketlerine bakilir. Not: koddaki "yerel saat 13" zaten YEREL saattir (Milan UTC+2: kilit 11:50 UTC = yerel 13:50; bet 12:26 UTC = yerel 14:26 — kural dogru calisti, sorun erken kilit teyidindeydi). Test: `TestMetarPeakBrokenLock` eklendi, bias testleri kaldirildi.
- **2026-08-18:** **METAR 24 SAAT TOPLAMA + BACKFILL.** Kullanici: "24 saat veri topla bundan sonra ve backfill ile eksik gunleri tamamla." Sorun: METAR toplama yalnizca acik marketi olan (kapanisa >2h kalan) sehirlere bagliydi -> aksam ~22:00'den sonra duruyordu; 14 Agu arsivi sadece 1 sehir, 15-17 Agu akşam kesik. Cozum: `jobs/metar_peak.py::collect_metar_archive()` — metar_loop (30dk) icinden bet mantigindan bagimsiz, TUM sehirlerin bugunku METAR'ini idempotent arsivler (bot restart gerektirir — admin). Backfill: `scripts/backfill_metar_history.py` ile 14-17 Agu dolduruldu (14 Agu 1->49 sehir; tum gunler 23:58'e kadar tam). `metar_peak_live` backtest etkisi: 153->168 bet, NET **+$275.75 -> +$304.83**.
- **2026-08-18:** **SAF METAR-PEAK BACKTEST (`metar_peak_live`).** Kullanici: "sadece order book ve metar ile backtest yap, hic 2 gun onceden bet acma, metar ile peak takibi yap, tespit ettiginde 3 usd bet ac sehire ve bir adet; slippage, fee, gas koy; gunluk kazanc raporu ver." Yeni subcommand `python scripts/backtest.py metar_peak_live` — forecast/bias YOK (bias-top sehir filtresi uygulanmaz, tum sehirler); `detect_peak` kilitlenince sehir basina TEK $3 YES bet (sadece RANGE temperature_max); giris = kilitlenme sonrasi ilk gercek ask + slippage $0.01 + fee %5 + gas $0.10. Sonuc 03-18 Agu: MIN_ENTRY=0.10 ile **153 bet, %80.4 winrate, NET +$275.75** (stake $459, fee+gas $25.70, slippage etkisi -$23.06); saf (--min-entry 0) 241 bet %53.5 NET +$324.95. Gunluk tablo raporu komutun ciktisinda. Test: `test_latent_bugs.py` allowlist + `test_realistic_backtest.py`.
- **2026-08-18:** **WALK-FORWARD AUDIT + DEBUG (W1-W5).** Kullanici: "walk forward neden bu kadar uzun suruyor, tum backtestleri audit ve debug et". `scripts/backtest.py walk_forward` 5 gercek ariza ile duzeltildi: (W1) sonuc kaynagi `bets` tablosu (~44 cozumlu satir) -> `parse_resolved_outcome(raw_data)` (bot.db oncelikli, 04-17 Agu tam); (W2) `snap.get("threshold", 25)` hatasi — snapshot'ta threshold kolonu yok, her bet sabit 25 esigiyle hesaplaniyordu -> model_prob 0.99'a kilitlenip sahte %100 winrate veriyordu; (W3) saatlik snapshot'ta ayni markete 11x yeniden giris -> market basina TEK bet (seen seti); (W4) her snapshot'ta 113k forecast lineer tarama (~17.5 milyar karsilastirma, saatler) -> tek seferlik indeks ile **~90 saniye**; (W5) giris fiyati snapshot artefaktindan degil orderbook+CLOB serisinden. Duzeltilmis SONUC: 1,325 bet, **%19.6 winrate, -$1,950.93, ROI -%14.7** — eski sabit edge modeli (P(max>=esik) vs fiyat) GERCEK veride kaybediyor; eski +$464.65/%100 tamamen sahteydi. NOT: walk_forward eski edge modelini test eder; botun SU ANKI stratejisi icin `gunluk` gecerlidir. Test: `test_realistic_backtest.py` + `test_latent_bugs.py` gecer.
- **2026-08-24:** **T-1 SPREAD AÇILISI + BACKTEST GERCEK KOD CAGIRISI.** (1) `bot_loop.py` — spread botuna T-1 eklendi: `_get_t1_spread_target(today, open_dates)` yardimcisi yarin pazari aciksa `today+1`'i doner; periyodik spread retry (60dk) blogunda `place_spread_bets(tomorrow)` cagirilir. Onceden spread yalnizca `max(open_dates)` (en ileri = T+2) hedefe aciliyordu; T-1 (yarin) hic kapsanmiyordu. Ayni gunluk cap (`spread_max_bets_per_day`) T-1+T-2 paylasilir; dup-guard tekrar acmayi onler. (2) `scripts/backtest.py` — `peak_lock()` yeniden-yazimi SILINDI; artik gercek `scrapers.metar.detect_peak`'i ARTIMLI cagirir (canli botun 5dk dongusunun birebir simulasyonu, `is_confirmed=True` olan ilk an = lock_epoch). ANAYASA geregi backtest gercek kodu cagirir; sonuc birebir ayni (peak kurali zaten senkrondu). NOT: metar_peak_live T0 win-rate'i (~%97) guard eksikliginden (HIBRIT/cap/stale default kapali) hala gercek bottan yuksektir — ayri bir degerlendirme gerekli.

- **2026-08-22:** **GAMMA-PROXY YOLU KENDINI-DUZELTME (kalici cozum).** 22 Agu 10:18-14:36 arasi bot her scan'de `ClientConnectorError: Cannot connect to host gamma-api.polymarket.com:443` aldi (direct egress de engelliydi; yalnizca restart duzeltiyordu). Kok neden: uzun omurlu process'te WARP SOCKS proxy yolu bozuluyor; `_afetch` her batch'te taze session kursa da ayni batch'in tum istekleri TEK connector paylasiyor ve hic retry yoktu (`fetch_many` hatalari None olarak yutuyor, `polymarket.py`'deki `@retry` HIC tetiklenmiyordu). Cozum: (1) `scrapers/async_client.py` — `_is_connection_error()` siniflandiricisi; `_afetch` baglanti-hatali item'lari `_MAX_CONN_RETRIES=1` kez TAZE session+connector'la (`_new_session`) tekrar dener; `_record_conn_outcome` surekli hata sirasinda DEGRADED (dashboard'da gorunur), duzelince RECOVERED loglar; `_ensure_session` 15dk yas siniri. (2) `scrapers/polymarket.py` — TUM gamma sorgulari None donerse `requests.ConnectionError` raise -> `@retry(3)` scan seviyesinde tekrar dener. Test: `test_scrapers_async.py` (retry + siniflandirma). Bot restart ile canliya yuklendi.

- **2026-10-27:** **BACKTEST SETTLEMENT BAGIMLILIGI KALDIRILDI (S21).** `backtest/engine.py::load_data` gercek sicakligi yalnizca `bets.outcome` JSON'undan okuyordu; o kolon 2026-09-29 sonrasi **NULL** (olcum: 192 yerlesmis bet, 0 outcome) oldugu icin `test_backtest_engine::test_load_data_has_settlement` **"settlement az: 0"** veriyordu. Cozum: gun secimi artik `weather_markets.target_date` (settled_at degil) + `_actuals_fallback()` ile `data/actuals.db` (8.691 satir, 48/48 sehir eslesiyor) yedegi (`mode=ro`, outcome'un uzerine yazmaz). Boylece 298 satirlik `bets.outcome` backfill'i **zorunlu olmaktan cikti**. Test: `tests/test_backtest_engine.py` 5/5.
- **2026-10-27:** **REPLAY PAKETI YENIDEN YAZILDI.** `replay/main.py`, `seeding.py`, `clock.py`, `archive.py`, `price_index.py`, `scheduler.py` diskten kaybolmustu (git'te izlenmemis); `tests/test_replay_engine.py` 3 test bu yuzden fail ediyordu. Paket sifirdan yazildi: look-ahead iki kat korunuyor (fiyat yalnizca `t`'den once + `first_seen <= t`), forecast `bot.db` + `backtest.db` birlikte okunuyor (bot.db yalnizca son ~10 gunu tutuyor; bu duzeltilmeden replay **0 bet** uretiyordu), METAR bacagi tick tick calisiyor, fiyat yuklemesi pencereye filtrelendi (tek gun 257 sn -> ~14 sn). Kapsam disi: "aktar" zinciri ve rotation modellenmiyor (sehir-gun basina tek giris, bet settlement'ta kapanir). Test: `tests/test_replay_engine.py` 3/3, `tests/test_replay_fidelity.py` 8/8. Detay: §4c.
- **2026-10-27:** **WAL SINIRSIZ BUYUMESI KAPATILDI (S19).** Olcum: `data/bot.db` D:'ye symlink ama `data/bot.db-wal` **3.324,8 MB gercek dosya olarak C:'de** birikmisti — SQLite `-wal` dosyasini VERILEN yolun yaninda acar, symlink'i cozmez. Ayrica `/api/restart` `os._exit(1)` kullandigi icin kapanista son checkpoint/truncate hic yapilamiyor. Cozum: `database/db.py` connect listener'ina `PRAGMA wal_autocheckpoint=1000` ve `PRAGMA journal_size_limit=67108864` (64 MB) eklendi; WAL artik bu tavanin ustune cikamaz. **2026-10-28 dogrulamasi: yeni kodla restart sonrasi WAL 3.324,8 MB -> 64,0 MB, C: bos alani 0,56 GB -> 3,74 GB. Elle `wal_checkpoint(TRUNCATE)` gerekmedi.** Test: `tests/test_latent_bugs.py` (import + pragma), tam suite.
- **2026-09-06:** **21 AGUSTOS'DAN ITIBAREN KARLILIK ANALIZI (Paper/DRY_RUN).** 21 Agu - 5 Eyl araliginda **543 bet** acildi: **METAR-peak 192 bet, 121W/6L (%63.0 WR), +\$92.37** (karlı); **Spread 351 bet, 55W/47L (%15.7 WR), -\$112.17** (zararli). **Toplam: -\$19.80** (hafif zarar). Gunluk detay: 21 Agu +\$34.11, 22 Agu -\$54.20, 23 Agu -\$28.48, 24 Agu +\$26.66, 25 Agu -\$37.73, 26 Agu +\$8.99, 27 Agu -\$2.17, 28 Agu +\$2.40, 29 Agu -\$3.83, 30 Agu +\$1.46, 31 Agu +\$11.35, 1 Eyl +\$12.70, 2 Eyl +\$14.76, 3 Eyl +\$3.04, 4 Eyl -\$7.71, 5 Eyl -\$1.15. **Gozlem:** METAR-peak (1-dusus kilit + aktar, cap=12, MIN_ENTRY=0.05) guclu; Spread (radius=0, bias-top 15, max_entry=0.95) dusuk winrate — entry fiyati 0.30-0.95 araligindaki betler kayip yaratiyor. Backtest (05-17 Agu, radius=0 + max_entry=0.30) +\$53k vs canli -\$112 — config farki (max_entry 0.30 vs 0.95) ve piyasa rejimi degisimi olabilir.
- **2026-11-08:** **VERI KOKU + GAMMA 421 + FETCH TIMEOUT.** (1) `data\bot.db` symlink'i ve `data\*` junction'lari `D:\JUNBO data`'yi gosteriyordu ama kok dizin yoktu -> servis 3 sn'de bir "unable to open database file" ile crash loop yapiyordu. Cozum: `D:\JUNBO data` -> `D:\Bot DATA backups\JUNBO data` junction (veri kopyalanmadi/silinmedi); `PRAGMA quick_check` ok (1.6 GB: 1516 bets, 76161 weather_markets, 86279 weather_forecasts). (2) `gamma-api.polymarket.com` CloudFront'a tasindi; makinedeki `hosts` pini onu eski Cloudflare edge'ine yonlendiriyordu -> her istek `HTTP 421 "not served on this path"`. Sistem `hosts` dosyasi DEGISTIRILMEDI (kullanici karari: "proje icinde coz"). Cozum: `utils/poly_dns.py` yalnizca bu host icin `socket.getaddrinfo` sarmalayicisi — WARP SOCKS proxy uzerinden Cloudflare DoH (`https://1.1.1.1/dns-query`), TTL 3600 sn, `POLY_GAMMA_IP` fallback, `POLY_DNS_FIX=0` ile kapatilir; `config/settings.py` proxy blogunda `install()`. `aiohttp` proxy'si hostname'i proxy'ye verip uzak DNS yaptigi icin (`rdns=True`) `scrapers/async_client.py` artik SOCKS proxy'de `aiohttp_socks.ProxyConnector(rdns=False)` kurar; `socks5h://` -> `socks5://` normalize edilir. Kanit: async fetch 200, "Toplam 2991 market cekildi", yeni loglarda 0 HTTP 421. (3) 97 sorgu x 1 sn throttle ~100 sn surdugu icin 60 sn `_FETCH_TIMEOUT` her taramada doluyordu; `asyncio.to_thread` iptal edilmedigi icin scan loop + price poller ayni anda ikinci yazici baslatiyordu (`UNIQUE constraint failed: weather_markets.id`). Cozum: `bot_loop._MARKET_FETCH_TIMEOUT=300` (yalnizca market fetch) + `jobs/scheduler.run_fetch_markets` single-flight kilidi. Test: `test_scrapers_async` + `test_regression_fixes` 30 passed, 1 skipped; `test_bot_flow` + `test_real_flow` 15 passed; `test_latent_bugs` + `test_clob` 17 passed; `test_poly_dns` 3 passed.
- **2026-11-08 (ikinci tur):** **Kalibrasyon verisi temizligi + offset tazeleme.** (1) `database/db_cleanup.py::archive_old_forecasts` 10 gunluk "hot" esigini `historical_calibrations` tablosuna da uyguluyordu; tablo dogasi geregi tamamen gecmis oldugu icin her temizlikte TUM satirlar Parquet'a tasinip SQLite'tan siliniyordu. Sonuc: `bias_map` bosaldi ("bias map for 0 cities"), tahmin bias duzeltmesi durdu ve `tests/test_backtest_engine.py::test_bias_varies_by_city` kirmizi kaldi (2026-11-08: 6050 satir arsivlendi + silindi; ayni gun backfill, forecasts de temizlendigi icin 0 satir yazdi). Cozum: arsiv dongusu `historical_calibrations`'i atlar (referans veri, birkac bin satir); Parquet arsivi yine `cold_days` ile budanir. (2) `tests/test_settlement_gate.py` esik karsilastirmasinda float gurultusu (0.4-0.1 -> 0.30000000000000004) giderildi: fark 2 ondaliga yuvarlandi. Veri tarafi gercekten kaymisti (2026-10-27 olcumu 159 cift -> bugun 361 cift); `config/source_offsets.json` testin kullandigi ayni yontemle yeniden olculdu: 33 sehir guncellendi, raw uyum 0.296, offset-duzeltmeli uyum 0.429; eski dosya `config/source_offsets.json.bak_20261108`. Kalan adim: silinen 6050 kalibrasyon satirinin `scripts/import_calibration_parquet.py --file historical_calibrations_20261108.parquet --apply` ile geri yuklenmesi (canli DB yazimi - kullanici onayi gerekir).
- **2026-11-09:** **Offsite yedek dizini Junbo veri kokune tasindi.** makine ortami `D:/junbo_offsite_backups` idi; `db_backup.mirror_offsite` her yedegin dogrulanmis (JUNBO_BACKUP_KEY varsa Fernet sifreli) kopyasini oraya yazar, etiket basina `MAX_BACKUPS=30` tutar. Dizindeki **154 dosya / 98.468,8 MB** `D:\JUNBO data\offsite_backups` altina tasindi (ayni disk, rename; tarih damgalari korundu: en eski 2026-07-26, en yeni 2026-11-09 00:01 scheduled); kaynak dizin bosaltilip silindi; makine ortami yeni yola cevrildi. Calisan servis yeni ortami bir sonraki restart'ta alir. NOT: offsite artik veri ile AYNI diskte (D:), fiziksel olarak ayri disk degil. Silinen 6050 kalibrasyon satiri kullanici onayi ile `scripts/import_calibration_parquet.py --file historical_calibrations_20261108.parquet --apply` ile geri yuklendi (arac varsayilan `--file` listesini de ekledigi icin `historical_calibrations_20260730.parquet` (80.520 satir) de yazildi; tabloda toplam 86.570 satir, 100 sehir, 2026-03-31..10-26).

---

*Eski dosyalar (KULLANIM_KILAVUZU, SETUP_REPORT, SYSTEM_TESTING_REPORT, DEVELOPER_NOTES, gelistirme_notlari) 2026-08-08'de tek README.md dosyasina birlestirildi. AGENTS.md ve agents.md korunur.*
- **2026-09-13:** **YENI SEHIR OTO-KESIF (`jobs/new_city_job.py`).** Bilinmeyen sehirli market gorulunce: resolution ICAO market verisinden alinir, aviationweather stationinfo + Open-Meteo geocoding (<150km) ile dogrulanir, `config/city_overrides.json` yazilir (settings.py elle degismez). Ingestion/forecast `get_live_city_map()` ile restart'siz devreye alir. Bias: gunluk backfill ilk yerlesimde uretir; o zamana kadar spread GLOBAL-ortalama fallback ile kabul eder (atlamaz). Gunluk bakimda `bot_loop._run_daily_maintenance` calistirir.
- **2026-09-16:** **Free-roll + fee-gate + cozum-kurali tablosu.** +%30 guclenince anapara cekilir (run_freeroll, 5dk poll); fee sonrasi net kar \.05 alti giris yok; 50 sehirlik resolution_rules.json (istasyon+rounding) + test. ASIAbot kopyalari: MIN_ENTRY 0.10, std>2.5 anlasmazlik atlama.
- **2026-09-17:** **Backup tasiyici:** data/backups -> D:\JUNBO dataackups junction (uzun backtest verisi silinmez). Dashboard build oncesi junctionlar gecici kaldirilir (tailwind D: tarayamiyor), sonra geri alinir.
- **2026-09-18:** **Akademik kopyalar:** Heat MOS canli tahminleri heat_mos kaynagi (gunluk job); ampirik-CDF A/B sonucu Gauss kaldi (Brier 0.676 vs 0.700); anlasmazlik esigi dinamiklesti (sehir rolling-std, 1.5-3.5).
- **2026-10-27:** **Muhasebe dogrulugu duzeltmeleri (S14) + karlilik teshisi.** (1) **Teshis (1.724 bet, gercek DB):** toplam gerceklesen PnL **-\$220,73** (ciro \$4.700,74 -> **-%4,7**). Ayrim: settlement'a kadar tutulan **+\$1.044,77**, rotation ile erken kapanan **-\$1.265,50**. Strateji bazinda: **METAR-peak +\$171,34 (+%17,6)**, **Spread -\$119,09 (-%6,0)**. 2026-09-06 kaydiyla ayni yon (METAR kazaniyor, Spread kaybediyor). (2) **Fiyat-kalibrasyon testi:** rotation dahil edildiginde hicbir fiyat araligi kalici edge gostermiyor (+%14,9 ile -%6,6 arasi, toplam +%1,9) - settlement-only tabloda gorunen "+25-35 puan edge" **survivorship** yanilgisidir (olen bacaklar rotation ile erken cikiyor). (3) **Duzeltme 1:** `jobs/metar_peak.py::_open_metar_bet` artik `entry_fee`'yi de kasadan dusuyor - onceki halde fee yalnizca `realized_pnl` icine gomuluyordu, `cash_balance` **\$34,66** fazla gorunuyordu. (4) **Duzeltme 2:** `close_bet_for_rotation` rotation PnL'ini artik `portfolio.total_realized_pnl`'e de yaziyor - onceki halde portfoy toplami **+\$488** iken bet tablolari **-\$221** diyordu. (5) **Duzeltme 3:** `bet_type` kolonu (spread / metar_peak / edge) dolduruluyor - onceden tamamen NULL oldugu icin strateji bazli PnL ayrimi yapilamiyordu. (6) Olu `tests/test_freeroll.py` kaldirildi (koddan geri alinmis `run_freeroll`/`net_win_profit` API'sini test ediyordu, 3 test surekli kirikti); gecerli rotation testi `tests/test_rotation_accounting.py`'ye tasindi. Test: `TestMetarPeakAccounting` + `test_rotation_accounting.py`.
- **2026-10-27 (tespit, aksiyon bekliyor):** Rotation'un gunluk limiti fiilen uygulanmiyor: config `daily_rotation_limit=5` / `max_daily_rotations=3`, canli veride **19 Ekim'de 32 rotation**; `jobs/metar_peak.py:258` `close_bet_for_rotation`'i dogrudan cagirip limit kontrolunu atliyor. Ayrica 95 markette >1 bet (479 bet, 11 Ekim'de tek gunde 333 bet) - re-entry guard delinmis.
- **2026-10-28 (tespit, ONAY BEKLIYOR):** **HEAT cift-depo arizasi — Junbo bayat HEAT verisi okuyor.** Iki ayri `tempmarket-edge.sqlite` var: (1) `C:\HEAT data\db\tempmarket-edge.sqlite` **3,11 GB, 74.313 prediction, son kayit 2026-10-27T08:43Z (OLU)**; (2) `C:\...\New project\Heat\data\tempmarket-edge.sqlite` **4,55 GB, 75.063 prediction, son kayit 2026-10-27T21:22Z (CANLI)**. `heat/config.py::HEAT_DB_CANDIDATES` ILK olarak olu olani sectigi icin `heat_mos` tahminleri **~12,5 saat bayat** kaliyor. Ek disk bulgusu: `C:\HEAT data` toplamda **6,96 GB** (3.656,4 MB `kardesler.db` + 3.187,1 MB sqlite + 12 x 19,2 MB gunluk settlement yedegi) ve tamami C: diskinde; `Heat\data` ayri 4,66 GB. Duzeltme iki adim ister ve ikisi de canli davranisi/veriyi degistirdigi icin onay bekliyor: (a) `HEAT_DB_CANDIDATES` sirasini canli depo once olacak sekilde cevirmek, (b) olu `C:\HEAT data` kopyasini C:'den kaldirmak (≈6,96 GB kazanc).
- **2026-10-28:** **HEAT `data` dizini D:'ye tasindi (S23).** Kullanici onayi "c" ile: `TempMarket Edge Runner` gorevi durduruldu (node alt sureci ayrica elle olduruldu — `Stop-ScheduledTask` tek basina node'u birakiyor), `Heat\data` **4.722 GB / 202 dosya** `robocopy /E /MOVE /XJ` ile `D:\JUNBO data\heat\data` altina tasindi, `Heat\data` yoluna **junction** kuruldu, gorev yeniden baslatildi. Dogrulama: yeni node PID 109852, `prediction_log` 75.063 -> **75.120**, `max(created_at)` **2026-10-27T22:12:47Z**, C: bos alan **+4,71 GB**. Not: `HEAT_MAIN_DB` User degiskeni ve `junbo/heat/config.py` hala OLU `C:\HEAT data` dosyasini gosteriyor — bu yari acik.
- **2026-10-28:** **`bets.outcome` backfill calistirildi.** `scripts/backfill_bet_outcomes.py` ile **298 satir** dolduruldu (**0 eslesmeyen**, kalan bos 0). Yedek: `data/backups/bot_pre_backfill_20261028.db` (1.532,3 MB, D:'de).
- **2026-10-28:** **ASIAbot tamamen devre disi + verileri silindi.** 5 zamanlanmis gorev `Disabled` (`ASIAbot_Supervisor` calisiyordu), surecler olduruldu (PID 95620 `watchdog.py`, PID 97300 `main.py bot`). Silinen: `ASIAbot\data` (2.352,0 MB), `ASIAbot\logs` (516,5 MB), `.mypy_cache`/`.next`/`out`/`.pytest_cache`/`.hypothesis`/`.ruff_cache`/`__pycache__`/`bot.pid` -> **~3,0 GB**. Kaynak kodu, `.git`, `node_modules` korundu. `D:\ASIA data` (4.160,2 MB) **silinemedi**: otomasyon yol korumasi "protected from removal" dondurdu; elle silinmeli. Junbo etkilenmedi.
- **2026-10-28 (tespit, ONAY BEKLIYOR):** **C: diskini dolduran ASIL sebep `C:\junbo_offsite_backups` = 89,4 GB.** 300 dosya, her biri `bot.db`'nin tam kopyasi (~1.531 MB). `JUNBO_OFFSITE_DIR` Machine ortami bu dizini gosteriyor ve `db_backup.py::mirror_offsite` her `create_backup()`'ta tam kopya yaziyor; temizlik `_prune_dir` **etiket basina** `MAX_BACKUPS=30` tutuyor, testlerin urettigi 8 ayri etiket x 30 = 240 dosya. Sonuc: iki tam test kosusu C:'den ~3 GB yiyor. Ikinci kalem: `C:\HEAT-kurtarma` **26,8 GB** (Eylul 2026 kurtarma artiklari, hicbiri canli degil). Oneri: offsite icin GLOBAL saklama (1-2 dosya) + testlerde mirror kapatma + mevcut 300 dosyanin eskilerini silmek ≈ **83 GB** kazanc; `HEAT-kurtarma` D:'ye tasinabilir veya silinebilir.
- **2026-10-27 (disk olcumu + settlement gun hatasi, S16):** (1) **Disk teshisi:** "D: diski arizali" varsayimi **olculerek curutuldu**. `fsutil dirty query D:` -> temiz; `Get-Disk`/`Get-Volume` -> Healthy/OK; System log son 30 gun -> disk/NTFS/volmgr/storahci kaynakli **tek hata yok**; `tests/test_critical_bugs.py::TestDBProtection` **4/4 gecti** ve test sirasinda `D:\JUNBO data\backups` altina **1.529 MB** yedek yazildi. D: = disk 1, PHD 3.0 Silicon-Power USB, 465,8 GB / 225,5 GB bos. Yedek dizininde 798 dolu + 315 **0-byte** dosya var; 0-byte'lar 2026-09-28..10-27 arasindaki **gecici** kesinti doneminden kalmis. **Chkdsk gerekmedi — onarilacak bir disk arizasi yok.** (2) **Gercek sorun C::** sistem diski **9,2 GB bos (%98 dolu)**; bu yuzden 1,5 GB'lik yedekleri C:'ye tasima secenegi uygulanamaz. (3) **`executor/settler.py` gun hatasi:** gercek sicaklik `bet.settled_at` (yerlesim ANI) ile cekiliyordu; UMA gec cozdugunde (D+1, D+2) **yanlis gunun** sicakligi `bets.outcome` icine yaziliyordu. Artik `market.target_date` (marketin gunu) kullaniliyor, yoksa `settled_at`'e dusuluyor. (4) **`scripts/backfill_bet_outcomes.py` (yeni):** `bets.outcome` alanini settler ile **ayni JSON semasinda** (`settlement_temp_c` / `settlement_temp_f` / `source` / `collected_at`) geriye donuk doldurur; kuru calistirma `--dry-run`. Olcum: 2026-09-01 sonrasi **298 bet** `outcome` alani bos, `actuals.db` (2026-10-27'ye kadar dolu, 8.691 kayit) ile **298/298 eslesiyor**. Bu backfill calistirilana kadar `tests/test_backtest_engine.py::test_load_data_has_settlement` (>=50 settlement) **kirmizi kalir** — yazma kullanici onayi bekliyor. (5) **`data/actuals.db` C:'ye alindi** (once D: symlink'iydi) ve `scripts/collect_actuals.py` ile 2026-09-29..10-27 arasi **1.452 kayit** geri dolduruldu. (6) **Olu degisken temizligi:** `scripts/replay_full_bot.py` icinde 4 adet kullanilmayan yerel degisken (`GAS`, `peak_cities`, `avg_hour`, `shares` x2) ve `tests/test_regression_fixes.py`'de 1 adet kaldirildi; `ruff check . --select F821,F841` artik **temiz**. **Bilinen eksik (kullanici karari bekler):** `scripts/replay_full_bot.py` METAR ayaginda canli koddaki `avg_hour` erken-giris kapisini **modellemiyor** — replay yalnizca 1-dusus kilidini uyguluyor, erken giris ayaklarini kaciriyor (muhafazakar taraf). Eklemek backtest sonuclarini degistirir. (7) **Test izolasyon hatasi (S16):** Bir test `sys.modules`'tan `database.db`'yi silip yeniden import edince **yeni** bir engine olusuyor, ama `executor.bet_placer` gibi moduller `from database.db import get_session` ile **eski** fonksiyona bagli kaliyordu. Sonuc: test piyasalarini yeni DB'ye yaziyor, `place_all_pending` eski DB'ye bakip `0 open markets found` diyor → `tests/test_tie_betting.py` tam suite'te **7/7 kirmizi**, tek basina **7/7 yesil**. Kanit: `bet_placer.get_session is database.db.get_session -> False`, yeni DB'de 1 market varken bet_placer 0 goruyor. COZUM: `tests/conftest.py::_rebind_module_get_session` (autouse) — kaynagi `database.db` olan modul-seviyesi `get_session` baglarini her testten once guncel engine'e geri baglar; testlerin kendi sahte `get_session`'lari korunur. (8) **`jobs/collect_settlement_temps.py` sehir koordinatlari:** ayni sozlukte 10 tekrarli anahtar vardi ve Python'da **sonraki kazanir** — `Newark` NJ koordinati NJ degil **CA** (37.52,-121.88) olarak, `St. Petersburg` FL koordinati **Rusya** (59.93,30.31) olarak cozuluyordu (~3000 km sapma). Bot bu iki sehirde 0 market / 0 bet oldugu icin hata **latent** kaldi; dosyayi cagiran kod da yok. Tekrarlar kaldirildi, kanonik deger korundu, `E401`/`W292` duzeltildi. Sonuc: `ruff check . --ignore F401` **tamamen temiz**, `mypy` degisen 8 modulde **temiz**.
- **2026-10-27 (tespit, aksiyon bekliyor):** Tum Junbo zamanlanmis gorevleri **Disabled** (son calisma 2026-09-28/29): `Junbo-ActualsCollect`, `Junbo-BackupDatabases`, `Junbo-OrderbookCollect`, `Junbo-SyncBacktest`, `JunboBot`, `JunboBotWatchdog`, `JunboDataWatchdog`, `JunboSnapshot`, `JunboWARP`. Bot **Windows servisi** `JunboBot` uzerinden calisiyor (STATE 4 RUNNING) ve API saglikli, ancak veri toplama/senkron gorevleri elle yeniden etkinlestirilmeden duruyor. Ayrica `replay/` paketinde `replay/main.py` ve `replay/seeding.py` **calisma agacinda yok** (2026-08-23'te vardi — `reports/replay/final/` ciktisi ve `REPLAY:main.py:78` log satiri kanit); `tests/test_replay_engine.py` var olmayan bu modulleri import ettigi icin **3 test kirmizi**.
- **2026-10-27 (HEAT entegrasyonu, S15 - tek sema / tek surec):** HEAT tahmin motoru Junbo icine tasindi. (1) **Tespit:** `heat_mos` entegrasyonu fiilen olmustu. (a) `jobs/heat_mos_job.py` depo yolunu `D:\HEAT data\...` olarak sabit kodluyordu; canli depo ise **`C:\HEAT data\db\tempmarket-edge.sqlite`** (Node runner, 3,34 GB). D: kopyasi eski Python semasini tasiyan olu bir kopya (0 tahmin). **2026-10-27 olcumu: D: diski SAGLIKLI** — `fsutil dirty query D:` temiz, son 30 gunde disk/NTFS hatasi yok, `TestDBProtection` 4/4 gecti ve 1,5 GB'lik yedek basariyla yazildi. Yani sorun disk degil, **yolun yanlis olmasiydi**. (b) Sabit kodlanan D: kopyasi eski Python semasini (`created_at`/`quantile_50`) tasirken Junbo `run_at_utc`/`final_prediction` bekliyordu; sorgu hata verince istisna yutuluyordu. (c) `maybe_run()` hicbir yerden cagrilmiyordu - olu koddu. **Olcum (duzeltme oncesi):** D: `prediction_log` **0 satir**, Junbo `weather_forecasts` icinde `heat_mos` **0 satir** (yalnizca 8 Open-Meteo modeli). Canli C: deposu ise **72.663 tahmin** iceriyordu - yani uretim calisiyordu, tuketim olmustu. (2) **Tek sema:** yeni `heat/` paketi - `heat/config.py` (depo yolu, `JUNBO_HEAT_DB` ile degistirilebilir), `heat/schema.py` (`ensure_schema`: kanonik DDL, eksik kolonlari `ALTER TABLE` ile ekler, `quantile_50`->`final_prediction` / `created_at`->`run_at_utc` backfill, idempotent), `heat/predictions.py` (`fetch_live_predictions`: her iki kolon varyantini okur). (3) **Tek surec:** `bot_loop._run_daily_maintenance` artik `heat_mos_job.maybe_run()` cagiriyor (marker'li, gunde 1 kez, hata izole). (4) **Sessizlik bitti:** depoda tahmin yoksa job WARNING logluyor. (5) **Test:** `tests/test_heat_schema.py` (yeni, 4 test) + `tests/test_heat_mos_job.py` yeniden yazildi (canli `>40 satir` beklentisi yerine gecici DB, 4 test). **Kritik not:** `heat_mos` satirlari `model_weight=0.0` ile yazilir ve `settings.model_weights` yalnizca 8 Open-Meteo modelini icerir; `engine/calculator.py:135` agirligi `f.model_weight or 0.0` ile okudugu icin `heat_mos` **agirlikli ortalamayi ve standart sapmayi degistirmez**. Yani entegrasyon veri akisini acti (canli dogrulama: `fetched=191 added=191`, ikinci kosu `added=0`) ama tahminleri henuz etkilemiyor. Agirlik atamak canli davranis degisikligidir ve kullanici karari bekler.
