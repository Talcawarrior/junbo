# Junbo - Self-Evolving Weather Prediction Bot

**Port: 8093** | **Framework: FastAPI + Next.js** | **Dry-Run Mode: KALICI (DRY_RUN=true)**

**Son guncelleme:** 2026-08-08 | **Konum:** `C:\Users\fdemir\Documents\New project\junbo`

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
| 2 | Orderbook depth | CLOB API | `orderbook.db` | **30 dk** |
| 3 | Actual temperatures | Open-Meteo Archive | `actuals.db` | 6 saat |
| 4 | Backtest kopyasi | bot.db'den kopya | `backtest.db` | 6 saat (sync) |
| 5 | Backup | 4 DB yedekleri | `data/backups/` | 6 saat |

### Task Scheduler Gorevleri (SYSTEM, WakeToRun=True, StartWhenAvailable=True)

| Gorev | Script | Interval | Not |
|---|---|---|---|
| `JunboSnapshot` | `snapshot_only.py` (30dk bucket dedup) | 30 dk | bot kapali olsa da alir |
| `Junbo-OrderbookCollect` | `scripts/collect_orderbook.py` | 30 dk | |
| `Junbo-ActualsCollect` | `scripts/collect_actuals.py` | 6 saat | gercek sicaklik |
| `Junbo-SyncBacktest` | `scripts/sync_backtest_db.py` | 6 saat | |
| `Junbo-BackupDatabases` | `scripts/backup_databases.py` | 6 saat | |
| `JunboBotWatchdog` | `scripts/bot_watchdog.py` | 1 dk | bot process/restart |
| `JunboDataWatchdog` | `scripts/data_watchdog.py` | 6 dk | **KENDI KENDINI TAMIR** |

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
| `DB_PATH` | `data/bot.db` | Ana DB |
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
  cikip **2 kez arka arkaya dustugunde** (YEREL saat >= 13:00) zirve kilitlenir,
  o sehrin kazanan bucket'ina **tek esik YES** bet acilir
  ($3, order_id `metar_*`, bias-top 40 sehir, `MIN_ENTRY=0.10`). 2026-08-17:
  canli 30 bet NET -$32.84 — 24 longshot (entry 0.01-0.03) -$39.90 kaybetti,
  entry>=0.10 6 bet +$7.06; MIN_ENTRY=0.10 ile 0.01-0.03'ler elendi.
  **2026-08-18 (kullanici "tam bucket a aciyoruz"): peak mantigi SADECE
  `metric=temperature_max` + `market_type=RANGE` (tam bucket) marketlerine bet
  acar.** Canli 11 yanlis bet (6 temperature_min + 5 HIGH/LOW, hepsi 0.01 entry)
  o yuzden acilmisti; or-above/or-below marketleri tam bucket kazananli degildir.
  Kapanisa <2 saat kalan sehirler atlanir. **2026-08-18 audit (C1/C2/C3/M3):**
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
| `/api/health-check` | GET | Health + red flags |
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

---

## 10. Test

```powershell
# FULL suite (0 failed hedefi)
python -m pytest tests/ --ignore=tests/test_betting_idempotency.py --ignore=tests/test_comprehensive.py --tb=short -q
# -> "667 passed, 8 skipped, 0 failed" (2026-08-16 durumda; tsc --noEmit 0 hata)

# Davranis testleri (gercek DB, mock'suz — modul etkilesim bug'lari icin)
python -m pytest tests/test_settlement_chain.py tests/test_bet_behavior.py -q

# Latent bug (once)
python -m pytest tests/test_latent_bugs.py -v --tb=long

# Hizli lint + import
python quick_check.py --fast
```

Detaylar icin `GELISTIRICI_NOTLARI.md`'ya bakin (bolum 12: dogrulanmis davranis kurallari — eski docs/ANAYASA.md; bolum 13: ariza senaryolari).

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
| Dashboard yok | `npm run build` sonrasi `Copy-Item out dashboard\out` |

---

## 12. Kararlar Log

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
- **2026-08-18:** **GERCEKCI BACKTEST (look-ahead kapandi + fill dogrulandi).** Kullanici: "hani butun veriler elinde vardi, kazaniyor muyuz kayip mi ediyoruz, gercekci backtest yazamiyor musun, kac gunluk veriye ihtiyacin var." **CANLI GERCEK: KAYIP -$483.66** (1,452 bet; won +$743 / lost -$692 / closed -$220 / closed_early -$315). Kaybin ~%61'i longshot (<$0.10, 545 bet -$162) + 10-11 Agu eski-config gunleri -$286. **Backtest arizasi:** `gunluk` bot.db weather_forecasts okuyordu (ROTATED: 05-13 Agu hedefleri 14-Agu'da backfill) -> 06-13 Agu'yu botun goremedigi forecast'lerle oynuyordu (LOOK-AHEAD; eski +$415.97 yaniltici). **COZUM:** forecast `backtest.db` gercek gunluk batch'lerinden (02-18 Agu) + `fetched_at <= kapanis` kapisi; yeni `--real-entry` bayragi sim entry'sini ayni marketteki gercek bot fill'iyle degistirir (config sinirlari uygulanir). Duzeltilmis sonuc 05-17 Agu: **+$353.08** (173 bet, %71 winrate; ideal fill) ~ **+$354.41** (gercek-fill capraz) -> fill modeli optimistik DEGIL. Sim sadece fiyat verisi olan marketleri simule eder (173 vs canli 1,452 bet) — survivorship siniri. Veri: 02-18 Agu gercek batch (17 gun), kullanilabilir pencere 05-17 Agu; 30-60 gun istatistiksel guven icin ideal, Open-Meteo gecmis servis etmediginden sert limit. Rapor: `reports/backtest_gercekci_2026-08-18.md`; prob: `scripts/_probe_fill.py`.
- **2026-08-18:** **WALK-FORWARD AUDIT + DEBUG (W1-W5).** Kullanici: "walk forward neden bu kadar uzun suruyor, tum backtestleri audit ve debug et". `scripts/backtest.py walk_forward` 5 gercek ariza ile duzeltildi: (W1) sonuc kaynagi `bets` tablosu (~44 cozumlu satir) -> `parse_resolved_outcome(raw_data)` (bot.db oncelikli, 04-17 Agu tam); (W2) `snap.get("threshold", 25)` hatasi — snapshot'ta threshold kolonu yok, her bet sabit 25 esigiyle hesaplaniyordu -> model_prob 0.99'a kilitlenip sahte %100 winrate veriyordu; (W3) saatlik snapshot'ta ayni markete 11x yeniden giris -> market basina TEK bet (seen seti); (W4) her snapshot'ta 113k forecast lineer tarama (~17.5 milyar karsilastirma, saatler) -> tek seferlik indeks ile **~90 saniye**; (W5) giris fiyati snapshot artefaktindan degil orderbook+CLOB serisinden. Duzeltilmis SONUC: 1,325 bet, **%19.6 winrate, -$1,950.93, ROI -%14.7** — eski sabit edge modeli (P(max>=esik) vs fiyat) GERCEK veride kaybediyor; eski +$464.65/%100 tamamen sahteydi. NOT: walk_forward eski edge modelini test eder; botun SU ANKI stratejisi icin `gunluk` gecerlidir. Test: `test_realistic_backtest.py` + `test_latent_bugs.py` gecer.

---

*Eski dosyalar (KULLANIM_KILAVUZU, SETUP_REPORT, SYSTEM_TESTING_REPORT, DEVELOPER_NOTES, gelistirme_notlari) 2026-08-08'de tek README.md dosyasina birlestirildi. AGENTS.md ve agents.md korunur.*