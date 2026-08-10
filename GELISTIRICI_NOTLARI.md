# Gelistirici Notlari — Junbo Bot

**Son guncelleme:** 2026-08-08 — Turkiye karakter kurali projenin AGENTS.md'sinde; bu dosya gelistirici kurallari ve teknik referansi tek yerde tutar.

---

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
# HEDEF: "686 passed, 7 skipped, 0 failed" (2026-08-10 itibari; tsc --noEmit = 0 hata)

# 7) Dokumantasyon senkronu (ZORUNLU, agents.md kurali)
# README.md + GELISTIRICI_NOTLARI.md — bugfix/karar/feature commit'lenmeden once ekle/duzelt
```bash
git diff --stat  # .py/.ts degisti ama README/NOTLAR yoksa -> dokumantasyon unutuldu
```

Test gecmeden commit/push/bot restart YOK. Basarisiz test "pre-existing" diye atlanmaz; du.zeltilir.

---

## 2. KRITIK YASALAR (dokunulmaz)

1. **`database/db.py`'ye ASLA dokunulmaz** — engine, SessionLocal, DB_PATH kritik altyapi. Degisiklik gerekiyorsa ayni modulun disinda yap (testlerde `importlib.reload`).
2. **Gerçek DB asla direkt SQL ile degistirilmez** — tum islemler API/uygulama katmanindan. Testler temp DB kullanir (`conftest.py`).
3. **DRY_RUN=true kalici** — `executor/bet_placer.py` `_live_allowed = False` kod seviyesinde sabit. Kullaniciya canli trade onerilmez.
4. **TURKCE KARAKTER YASAK** — kodda, yorumda, commit mesajinda, log mesajinda `ç ğ ı ö ş ü` kullanilmaz. Yerine `c g i o s u` yazilir (mojibake onleme).
5. **Sadece istenen degisikligi yap** — hedef disindaki kodu, stili, yerlesimi EZME.
6. **Minimal diff** — en kucuk degisimle coz.

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

Aktif branchlar: `restore/05-clean-state` (production), `ponytail-audit`, `feature/partial-tp`.

---

## 4. Test Katmanlari (CI)

| # | Katman | Arac | Dosya |
|---|---|---|---|
| 1 | Lint | ruff (F821, E722, F401...) | quick_check.py |
| 2 | Type | mypy | quick_check.py |
| 3 | Latent bug | import-all, dead-code, calibration | test_latent_bugs.py |
| 4 | Core | calculator, ASI, calibrasyon | test_calculator.*, test_ai... |
| 5 | Unit/regresyon | formuller, kelly, risk | test_units.py + digerleri |
| 6 | E2E | uctan u can | test_e2e_system.py test_integration_e2e.py |
| 7 | Full | hepsi | `pytest tests/ ...` |
| 8 | **Davranis (2026-08-08)** | gercek DB + gercek modul, mock YOK | test_sl_reopen_chain.py, test_settlement_chain.py, test_bet_behavior.py, test_risk_behavior.py |
| 9 | **Replay (2026-08-08)** | production DB kopyasi | `python scripts/replay_test.py` |

### Davranis Testleri Haritasi (2026-08-08)

Hangi dosya hangi kurali korur:

| Dosya | Kapsam |
|---|---|
| `tests/test_sl_reopen_chain.py` | SL -> settler -> reopen tam zincir; kayip market haric en yuksek fiyatli secim; kapanis gecmeden expired yok |
| `tests/test_settlement_chain.py` | Settler: kapanis (target+12h) gecmeden expired YASAK; gectiyse expired; acik betli market asla expired |
| `tests/test_bet_behavior.py` | Acilis: oglen sonrasi bugunku market acilabilir (kapanis 24:00); kapanis gectiyse kapali; 20h+ kala kapali; fiyat gate [0.10,0.95); duplicate yok; nakit siniri; grup basina tek bet; rotation threshold |
| `tests/test_risk_behavior.py` | SL/TP/trailing/time-decay KARARLARI gercek bet ile; exit_position paper sell (bet kapatma scheduler'da) |
| `scripts/replay_test.py` | Production DB kopyasi: settle_all + reopen gercek veriyle, invariant: kapanisi gecmemis market expired OLMAMALI |

**Kural:** Yeni bug bulundugunda once davranis testi yaz (sentetik + gercek DB), sonra duzelt.

**ALLOWED_DEAD** guncellemesi: yeni public fonksiyon eklediginde ya caller ekle ya da `tests/test_latent_bugs.py::ALLOWED_DEAD` kumesine aciklama ile ekle (entry point ise otomatik gecer).

---

## 5. Bilinen Kritik Hatalar & Cozumler

| Hata | Cozum |
|---|---|
| `max_bet_pct` kelly.py 10x fark | `kelly.py` artik `bot_config.strategy.max_bet_pct` okur |
| Fee rate tutarsizligi | `strategy.py` `current_fee_rate` kullanir; `slippage.py` guncellendi |
| `min_edge` cift kontrol | strategy.min_edge kontrolu kaldirildi; calculator'e birakildi |
| Timezone crash (fast_mode_until) | `.replace(tzinfo=None)` |
| Gamma API `tokens[]` yok | scraper `outcomePrices` fallback; bestBid=0 / bestAsk=1 atlar |
| Take profit `{pct:.1%}` double | ratio kullani, format ratio |
| **Snapshot 30dk durdu (2026-08-08)** | `jobs/snapshot_job.py` bucket-bug: farkli bucket'ta yeni satir yazilmiyordu; du/zeltildi |
| **SL sonrasi pencere disi yeni-lider acilimi (2026-08-08)** | Wellington 12C/13C gece 00:00-04:00 UTC'de cift kayip. `_reopen_after_stop_loss` artik `_is_in_betting_window()` (04:00-23:30) gated; grupta ACIK bet varsa acilmaz (tek pozisyon); `_STOP_LOSS_REOPEN_WINDOW=6h` cutoff. Test: `test_reopens_new_leader_after_stop_loss_in_window` |
| **JunboSnapshot LastResult=1 (2026-08-08)** | Task action `...\..\snapshot_task.bat` parent dizini isaret ediyordu; dogru yol `...junbo\snapshot_task.bat` yapildi. LastResult=0, 17 snapshot |
| **Uyku: tarama duruyor (2026-08-08)** | Wake timers DC=Disabled, AC=Important → `powercfg /waketimers` bos. AC+DC=Enable yapildi; Sleep after + Hibernate = Never → bot loop'lari kesintisiz |
| **TS tip hatalari (2026-08-08)** | 24 hata: import HistoryStats, Signal'e threshold/metric/strike_temp, mapOpenPositions strikeTemp, KpiData fallback (availableCash/totalEntryFee/gercekKayip), brierScore null guard, exitType PT union, result ROTATION, mapActivityFeed status/health/weights imzalari → `tsc --noEmit` 0 hata |
| **target_date 12:00 etiketi kapanis saniliyordu (2026-08-08)** | `bet_placer` `target_date <= now` / `target_date > now+30dk` kullanarak 12:00 etiketini kapanis (24:00) saniyordu → 12:30 UTC sonrasi hicbir markete bet acilamiyordu (SL sonrasi reopen dahil; "0 open markets"). Kapanis = target_date + 12h. SQLite-safe esdeger: `target_date > now-11h30dk` ve `target_date <= now+8h`. Test: `test_tie_betting.py` helper `_td()` kapanis now+20h icinde olacak sekilde duzeltildi |
| **max_openable nakit sinirsizdi (2026-08-08)** | Eski formul `max_openable = max_exposure - exposure` nakit ust sinirini yok sayiyordu → "Max acilabilir $884" derken cuzdanda $849. Yeni: `min(nakit, max_exposure - exposure)`, API `max_openable_now`, frontend `maxOpenableUsd` backend'den okur. Invariant test: `max_openable_now <= free_cash` (test_all_functions.py) |
| **"Gercek Kayip" KPI kaldirildi (2026-08-08)** | `gercek_kayip = initial - equity_cash` exposure'i (bagli sermaye) kayip saniyordu + fee zaten PnL icinde (settlement_pnl = payout - stake - entry_fee). Kaldirildi; yerine `entry_fee_trade_count` (fee odenen islem sayisi) eklendi, Toplam Fee kartinda gosterilir |
| **SL sonrasi yeniden acilim calismiyordu (2026-08-08)** | Iki bug birlesiyordu: (1) `settler.py` acik beti olmayan marketi hemen `expired` yapiyordu (SL ile kapanan betin marketi hala canli: Toronto 30C 0.97, Miami 32.5C 0.925) -> `_reopen_after_stop_loss` status='open' aradigi icin bulamiyordu; (2) `_reopen_after_stop_loss` best == lost_market_id ise skip ediyordu, ikinci en yuksek farkli marketi denemiyordu. Duzeltme: settler kapanis (target+12h=24:00 UTC) gecmeden expired yapmaz; reopen kayip market haric en yuksek fiyatliyi secer. Test: `test_tie_betting.py` + elle dogrulama (Toronto 31C acildi) |
| **DURUM: Davranis testleri eklendi (2026-08-08)** | Kacan bug'lar hep modul ETKILESIMINDE cikiyordu (settler x reopen, target_date x kapanis). Izole unit testler (test_active_risk_management.py 42 test ama 0 gercek DB — hepsi MagicMock) bunlari yakalayamadi. Eklendi: `test_sl_reopen_chain.py` (SL->settler->reopen zinciri), `test_settlement_chain.py` (kapanis gecmeden expired yok), `test_bet_behavior.py` (acilis filtresi, vade, gate, rotation), `test_risk_behavior.py` (SL/TP/TS/time-decay gercek DB ile). Suite: 653 -> 680 |
| **DURUM: TP/TS/time-decay config kapali (2026-08-08)** | `config/settings.py` risk: `take_profit_pct=999.0`, `trailing_stop_pct=999.0`, `time_decay_hours=0` — TP/TS/time-decay fiilen devre disi! SL calisiyor (0.2). Bu bilincli mi yoksa bug mu karar gerekli. Testler config'i gecici set ederek davranisi dogruluyor |
| **REPLAY testi: production DB kopyasi (2026-08-08)** | Sentetik testler gercek DB'deki durumlari yakalayamaz. `scripts/replay_test.py` production bot.db'yi kopyalar, kopya uzerinde settle_all + reopen calistirir: kapanisi (target+12h) gecmemis market expired YAPILMAMALI + reopen crash'siz. Neden pytest DEGIL script: conftest DB_PATH'i temp DB'ye cevirir, bot_config singleton ilk importta donar — replay pytest icinde calisamaz. Kullanim: `python scripts/replay_test.py` (cikis 0=OK). 2026-08-08 dogrulama: 3064 market, 0 yanlis expired, 7 acik-bet'siz SL grubu islendi (gate reddi) |
| **`_fetch_open_meteo_model` tanimsiz idi (2026-08-09)** | `scrapers/meteo.py` `fetch_for_markets` icinde tanimsiz `self._fetch_open_meteo_model(...)` cagilisi ilk model'da `AttributeError` -> `except Exception`'a dusup SESSIZCE 0 satir uretiyordu (8-modelli per-model loop etkisiz) + ayni (lat,lon,date) icin cift istek riski. Cozum: kirlik loop ve kullanilmaz `openmeteo_models` listesi **silindi**; canli yol `fetch_all_markets` → `get_multi_model_forecast` ve aggregate `_fetch_open_meteo`/`_fetch_weatherapi` KORUNDU. Test: `test_meteo.py` |
| **DB bakimi ANALYZE+VACUUM eksikti (2026-08-09)** | Dosya buyudukce istatistikler eskimeyor, boyut artiyordu; `ANALYZE`/`VACUUM` hic calismiyordu. Cozum: `scripts/db_maintenance.py` (wal_checkpoint(TRUNCATE) → ANALYZE → VACUUM) + `data_watchdog` icinde **gunde 1 kez 02:00-04:00 UTC** penceresinde (`data/.last_db_maintenance` marker). VACUUM canli bot ile lock riski → sessiz pencere secildi. Ilk run: bot.db 157.88MB → 146.58MB (~11.3MB save) |
| **BAYAT FİYATLA BET ACILDI (2026-08-10)** | Beijing 32°C (10 Ağu) marketine 08:59 UTC'de **0.18'e** bet acildi; gerçek CLOB book fiyati o an **~0.98** idi (Gamma `outcomePrices` ~1 saat bayat kaldi; snapshot'larda 01:42→08:54 arasi 7 saatlik bosluk vardi). Bot, bet acarken sadece DB'deki `market.yes_price`'ı (Gamma'dan) kullaniyor, gerçek işlem fiyatini CLOB'dan dogrulamiyordu → paper fill gercekte hic var olmamis fiyattan. Cozum: `utils/clob_live.py` eklendi — `raw_data`'dan `clobTokenIds[0]` (YES) cikarir, CLOB `/book`'tan canli ask/bid ceker; `bet_placer.open_bet_on_market` + `place_bet` artık bet acmadan once canli fiyatla DB fiyatini karsilastirir, sapma > %15 ise **bet reddedilir** (stale guard; CLOB erisilemezse eski davranis korunur). Test: `tests/test_clob_live.py` (12 test) |
| **Acik betlerde bayat giris fiyati (2026-08-10, elle duzeltildi)** | Ayni bayatlik 41 acik betten **17'sini** etkilemisti (Beijing %81.5, Hong Kong %57.1, Seoul %61.2, Tokyo %76.4, KL %76.3...). `scripts/fix_stale_entry_prices.py` yazildi: her acik betin CLOB fiyat gecmisinden `placed_at` anindaki gercek fiyati ceker, %15+ sapma olanlarda entry_price/price/fair_value/shares/current_price/entry_fee/unrealized_pnl'i canli bot formulleriyle yeniden hesaplar (dry-run varsayilan, `--apply` yazar). **2026-08-10 uygulandi**: 17 bet duzeltildi, DB backup `data/backups/bot_pre_pricefix_*.db`. Kapanmis/arsiv betlere dokunulmadi (PnL gerceklesmis). Test: `tests/test_fix_stale_entry_prices.py` (9 test) — token cikarimi, düzeltme matematigi, sapma esigi |
| **KALIBRASYON bos — model bias duzeltilmiyordu (2026-08-10)** | `historical_calibrations` tablosu **0 satir**; `jobs/evolution_job.py::_run_calibration_backfill` bos govde (sadece log). Sonuc: Busan (MBE -2.9C), Seoul (-1.5C), LA (+1.9C) gibi sistematik model sapmalari tahminlere yansimiyordu — edge hesaplari bias'li tahminlerle yapiliyordu. Cozum: (1) `scripts/backfill_calibration.py` — junbo'nun **kendi verisiyle** (`weather_forecasts` per-model × `actuals.db` Archive) `historical_calibrations`'i **58,064 satirla** doldurdu (8 model × 48 sehir × max/min, INSERT OR REPLACE, idempotent). (2) `utils/calibration.py` — ASIAbot'tan tasinan `CalibrationEngine` (sehir/model MBE map, `raw - MBE`), lazy singleton. (3) `engine/calculator.py` `latest_by_source`'ta her model tahmini kalibre edilir (bias_map yoksa eski davranis korunur). (4) `_run_calibration_backfill` artik gunde 1 kez backfill script'ini calistirir + bias map'i tazeler. Test: `tests/test_calibration_engine.py` (7 test). Dogrulama: Busan max gfs raw=30 -> 33.26, Seoul max gfs raw=31 -> 35.09, bilinmeyen sehir degismez. Ayrica `test_bot_loop.py::test_cleanup_stale_bets_cancels_only_stale` sabit tarihler (08-08) kullandigindan bugun (08-10) 48h sinirini asip flaky oluyordu — goreli tarihlerle duzeltildi |

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

### 12.8 Referans: Analiz Scriptleri (arsiv — SILINMEZ)

- `scripts/analyze_first_peak_climbs.py` (49 real climb; %20 takint elendi).
- `scripts/analyze_peak_to_settle.py` (39 market; peak->settle suresi; 17:00=15, 23:00=7).
- `scripts/analyze_city_time_temp.py`, `analyze_settlement.py` vb. — strateji
  gelistirme/kanit analizleri. Backtest script'leri (`backtest_rolling.py`,
  `backtest_advanced.py`, `walk_forward_backtest.py`, `backtest/simulator.py`) ile
  birlikte bilincli arsiv olarak korunur.

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
- **Beklenen:** Bet penceresi `[(4, 23.5)]` (04:00-23:30 UTC). Disinda bet acilmaz;
  kapanis 24:00 UTC = target_date + 12h (2026-08-08 fix: 12:00 etiketi kapanis
  sanilmaz).
- **Dogrula:** bot.log `Betting window` satirlari; bet saatleri pencereyle uyumlu.
- **Aksiyon:** Pencere disinda bet acildiysa `_reopen_after_stop_loss` + `_is_in_betting_window`
  kontrol et (SL sonrasi yeniden acilim pencereye tabi).

### S6. Task Scheduler gorevi Disabled / eksik
- **Beklenen (2026-08-08 itibariyle):**
  | Gorev | Durum | Gorev |
  |-------|-------|-------|
  | JunboSnapshot | Ready, 30dk, WakeToRun | snapshot (24/7 garantisi) |
  | JunboBotWatchdog | Ready, 1dk | heartbeat + crash restart |
  | JunboDataWatchdog | Ready, 6dk | veri tazeligi self-healing |
  | Junbo-BackupDatabases | Ready, 6sa | DB yedekleme + purge |
  | Junbo-ActualsCollect | Ready, 6sa | gerceklesen hava |
  | Junbo-OrderbookCollect | Ready, 30dk | orderbook derinligi |
  | Junbo-SyncBacktest | Ready, 6sa | backtest.db senkron |
  | JunboBot / JunboBotBackup / JunboWARP | Disabled (bilincli) | eski yontemler |
- **Aksiyon:** Ready olmasi gereken bir gorev Disabled ise `schtasks /change /tn
  "\Gorev" /enable` ile ac, sonra log/Last Result dogrula.

### S7. Actuals toplanmiyor / "All 5 attempts failed"
- **Beklenen:** `collect_actuals.py` gun icinde ayni gunu tekrar ceker (archive API
  kismi saatler dondurur). `start > end` ise "already up to date, skip" — 400 hata
  OLMAZ (2026-08-08 fix).
- **Dogrula:** `data/logs/collect_actuals.log` son satirlar; `data_watchdog.log`
  `ACTUALS ok (age=...)`.
- **Aksiyon:** 400 hatasi gorulurse tarih araligi mantigini kontrol et (start <= end).