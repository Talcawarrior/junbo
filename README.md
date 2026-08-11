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
| Bot loop | 4 asyncio arka plan dongusu | `bot_loop.py` |
| Calculator | 8-model ensemble olasilik, Kelly | `engine/calculator.py` |
| Strategy | RiskManager + BettingEngine + early exit | `engine/strategy.py` |
| Bet Placer | Paper + canli bahis | `executor/bet_placer.py` |
| Settler | Gamma API settlement kontrol, PnL | `executor/settler.py` |
| Scraper | Polymarket + hava durumu cekici | `scrapers/` |
| DB | SQLAlchemy + WAL | `database/db.py` |
| Config | Tum ayarlar | `config/settings.py` |
| Job scheduler | run_cycle, run_settle, risk_management | `jobs/scheduler.py` |

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
  (tarama, stoploss, hesap) artik kesintisiz calisir. Kontrol: `powercfg /query SCHEME_CURRENT SUB_SLEEP`.

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
| `POLY_PROXY` | - | SOCKS5 proxy (Turkiye ise WARP kullan) |

---

## 4. Bahis Penceresi (2026-08-11 karar — KAPALI)

- **Pencere DEVRE DISI (2026-08-11 karar):** betler gun boyu acilir.
  `betting_window_enabled=False` (`.env BETTING_WINDOW_ENABLED=false`).
- Eski pencere mantigi (04:00-23:30 UTC) artik uygulanmaz.

---

## 4b. Spread Stratejisi (Ana Mod, 2026-08-10)

**Varsayilan moddur** (`BETTING_STRATEGY=spread`). Eski edge-tabanli mod `BETTING_STRATEGY=edge` ile geri donulebilir.

- Yeni **2-gun-sonrasi tarih** acildiginda (bot_loop 2-day-ahead tespiti):
  - En son meteo tahmini etrafinda **+/- 3 dereceye** (spread) YES bet acilir.
  - Giris fiyati = **CANLI `weather_markets.yes_price`** (5 dk'da guncellenir; bayat snapshot degil, 2026-08-11).
  - `0 < entry < 0.30` olan esiklere, esik basina $2 (backtest en iyi config — 2026-08-11).
  - **KAYAN PENCERE:** merkez kayinca (meteo tahmini guncellenir) eski pencerenin
    disinda kalan esikler **o anki fiyattan kapatilir**, yeni pencereye giren eksik
    esikler acilir. Tam-7 zorunlulugu YOKTUR (backtest karliligi dusurdugunden
    kaldirildi; merkez marketi olmasa da acilabilen ayaklar acilir).
  - Tahmini **en az sapan ilk 15 sehir** secilir (tahmini gercege en yakin tutanlar —
    dusuk |bias|; SICAKLIK DEGIL, 2026-08-11 kullanici karari. Bias'siz yeni sehir acilmaz).
  - Gunluk **max 350 bet** (3 gun x 15 sehir x 7 esik = 315 + marj; 13 Agustos acilinca
    da 15 sehir daha acilir, limite takilmaz).
- **ERKEN GIRIS (0-13 UTC hafif probe):** Snapshot analizi ilk market acilislarinin
  04:00-12:30 UTC'ye yayildigini gosterdi. 00:00-13:00 UTC penceresinde bot her ~1 sn
  Polymarket Gamma'ya TEK hafif sorgu atar (public-search limit 5); DB'deki max acik
  tarihten ileri bir tarih gorurse HEMEN tam market cekisi + spread bet acar. Yeni
  tarih yoksa cekis yapilmaz (rate limit korunur). Pencere disinda normal 5 dk tarama.
- **CLOB WebSocket:** Acik betlerin marketleri gercek zamanli WebSocket ile dinlenir
  (5 dk polling yerine milisaniye fiyat akisi).
- **Kayan pencere:** tahmin guncellendiginde (25C -> 27C) yeni merkezin +/-(radius)
  disinda kalan acik esikler **kapatilir**, yeni penceredeki esikler acilir.
- **Periyodik retry:** Polymarket marketleri zamana yayilarak acildigi icin bot her
  ~60 dk bir en yeni acik tarih icin spread betlerini tekrar dener — sonradan acilan
  esikler (orn. Ankara 32C "NEW") de yakalanir. Top-15 disinda kalan sehirlerin acik
  betleri kapatilir (portfoy 15 sehirle sinirli).
- Backtest (7 gun, gercek veri): spread=3, max_entry<0.30, RAW -> **+$36,814**,
  %50.6 kazanma, 5/5 gun pozitif. `scripts/backtest_early_spread.py`.
- **Kalibrasyon spread'te kapatilir** (CALIB +$28k < RAW +$37k) — edge-tabanli
  stratejide degerli oldugu icin calculator'da aktif kalir.

| Ayar (.env) | Varsayilan | Aciklama |
|---|---|---|
| `BETTING_STRATEGY` | `spread` | `edge` = eski mod |
| `SPREAD_RADIUS` | `3` | tahmin +/- derece |
| `SPREAD_MAX_CITIES` | `15` | tahmini en az sapan ilk N sehir (sicaklik degil) |
| `SPREAD_MAX_ENTRY` | `0.30` | ust fiyat siniri (0.30 ve ustu acilmaz; backtest en iyi) |
| `SPREAD_STAKE_USD` | `2.0` | esik basina stake |
| `SPREAD_MAX_BETS_PER_DAY` | `350` | gunluk bet limiti (3 gun x 15 sehir x 7 = 315 + marj) |

---

## 5. Bot Loops (bot_loop.py)

| Loop | Interval | Gorevi |
|---|---|---|
| `scan_and_bet_loop` | 300sn (hizli 60sn) | fetch → parse → analyze → bet → update → risk |
| `price_poller_loop` | 300sn | fiyat tazele + risk (stop-loss/tp/trailing) |
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
| stop_loss_pct | 30 | SL |
| take_profit_pct | 100 | TP |
| trailing_stop_pct | 15 | TS |

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
# -> "695 passed, 7 skipped, 0 failed" (2026-08-11 durumda; tsc --noEmit 0 hata)

# Davranis testleri (gercek DB, mock'suz — modul etkilesim bug'lari icin)
python -m pytest tests/test_sl_reopen_chain.py tests/test_settlement_chain.py tests/test_bet_behavior.py tests/test_risk_behavior.py -q

# Replay testi (production DB kopyasi uzerinde — conftest temp DB'ye mudahale ettigi icin script olarak calisir)
python scripts/replay_test.py
# -> SONUC: OK (0 yanlis expired + reopen crash'siz)

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

---

*Eski dosyalar (KULLANIM_KILAVUZU, SETUP_REPORT, SYSTEM_TESTING_REPORT, DEVELOPER_NOTES, gelistirme_notlari) 2026-08-08'de tek README.md dosyasina birlestirildi. AGENTS.md ve agents.md korunur.*