# JunboBot ANAYASA - Dogrulanmis Davranis Kurallari Rehberi

Bu belge, bot'un kalici davranis kurallarini **kod + sistem kaniti** ile dogrulanmis haliyle
saklar. Amac: her oturumda tekrar ogrenme/tekrar dogrulama gerektirmeden "standard" olarak
referans alinmasini saglamak. Her maddeye kanit (dosya:satir) yazilmistir.

Dogrulama tarihi: 2026-08-07 (kod + system + db).

Not: Bu dosya tamamen ASCII karakterlerle yazilmistir (proje kurali geregi Turkce
karakter yasaktir: c->c, g->g, i->i, o->o, s->s, u->u).

---

## 0. ANA AMAC: BACKTEST ICIN KESINTISIZ VERI KAYDI

Bot'un en temel gorevi, dogru backtest yapabilmek icin **snapshot, bet ve fiyat
kayitlarinin duzenli ve surekli tutulmasidir**. Veri olmadan backtest yapilamaz.

Bu anayasa, veri kaydinin HICBIR kosulda kesintiye ugramamasi icin alinan tum
onlemleri ve bir sey bozuldugunda NE OLMASI GEREKTIGINI tanimlar.

---

## 1. Snapshot Cadence: 30 DAKIKA (24/7)

**Kural:** Bot calistigi surece her 30 dakikada bir market snapshot alinir. Bahis
penceresi kontrolu YOKTUR - bos veri alinsa bile snapshot alinir.

- Kanit (kod): `bot_loop.py:463` -> `_SNAPSHOT_INTERVAL = 1800` (saniye = 30 dk).
- Kanit (loop): `bot_loop.py:466-491` -> `snapshot_loop()`, `while state.is_running` +
  `asyncio.sleep(1800)`; bet penceresi kurali icermez.
- Kanit (dedup, 30 dk bucket): `jobs/snapshot_job.py` -> `_bucket_start(ts)` =
  `ts.minute // 30 * 30`; `_same_bucket(a, b)`. Yeni 30 dk penceresi -> yeni kayit.
- Kanit (test): `tests/test_snapshot_30min.py` (bucket sinirlari 10:00 -> 10:30 -> 23:30).

**Not:** Onceki hali saatlik (3600) idi; 30 dk, `test_snapshot_30min.py` eklenerek
saatlik dedup -> 30 dk bucket dedup seklinde kod degisikligi yapildi.

---

## 2. Pazaryeri Verisi DB Kanitlari

- `bot.db` -> `market_snapshots`: 11197 satir (2026-08-05 04:22 -> 2026-08-07 02:00).
  Gun bazinda: 08-05=2976, 08-06=7497, 08-07=724; tum saatlerde kayit gorunuyor.
- `snapshots.db` -> 14125 satir (Aug 4-5).
- `orderbook.db` -> `orderbook_snapshots`: 3692 satir (Junbo-OrderbookCollect).
- `actuals.db` -> `actual_temperatures`: 4599 satir (Junbo-ActualsCollect).
- `backtest.db` -> 35551 snapshot + 32616 forecast + 166 bet + 4019 market
  (Junbo-SyncBacktest, 6 saatte bir guncellenir).

**Yorum:** Eksik gorunen saatler KOD hatasi degil - bot o saatte calismiyordu (gecmis
oturumda/gecen hafta bot 7/24 degildi). Kural "bot calistigi sure boyunca 30 dk'da bir";
geriye donuk (backfill) kapatma sozu icermez.

---

## 3. Surekli Calisma / Restart

- Windows servisi `JunboBot`: **Status=Running, StartType=Automatic** (dogrulama 2026-08-07).
- Makine uptime: 99 saat (onceki 4 gun bir uyku gorulmedi).
- `snapshot_loop` kendisi `state.is_running` bayragina bagli; donma ya da crashte
  yeniden baslatma watchdog sorumlulugunda.

---

## 4. Watchdog (Crash/Freeze Restart)

**Kural:** Bot donar veya coker ise yeniden baslatilir.

- `scripts/bot_watchdog.py` -> Task Scheduler araciligiyla (SYSTEM olarak) calisir;
  heartbeat zaman asimi `HEARTBEAT_TIMEOUT = 15 * 60` saniye (15 dk). Tahmini:
  heartbeat log'u aracilig'iyla kontrol edilir, tek yol restart.
- `scripts/watchdog_task.bat` -> `python bot_watchdog.py` (Task Scheduler cagirir).
- Alternatif restart dongusu: `start_bot.bat` -> watchdog + `python main.py bot`,
  crash oldugunda ~3 saniyede yeniden baslatir.

---

## 5. Retry / Internet Dayanikli

**Kural:** Dis veri kaynaklarinda (Polymarket gibi) hatali isteklerde yeniden dener:

- Kanit (ortak): `utils/retry.py` -> `max_attempts=3`.
- Kanit (ingest): `data_pipeline/polymarket_ingest.py` -> `max_retries=3`,
  2^attempt backoff + min 60 s.
- Kanit (resolved/audit): `data_pipeline/resolvedmarkets_ingest.py` -> `max_retries=5`
  + 429 `Retry-After` saygi. // 5 sayi resolvedmarkets icin SPECIFIC, global DEGILDIR.

**Onemli ayrimlama:** Snapshot loop **interneti kullanmaz** - yalniz local `WeatherMarket`
tablosundan (zaten onbellek/canli olan) fiyat degerlerini veritabanina (DB) yazar. Yani
"internet gitdeginde snapshot 5 kere tekrar dener" iddiasi SNAPSHOT icin GEERSIZDIR -
snapshot uzak API'den veri cekmez. Internet retry, ingest/collectolator'larda gecerlidir.

---

## 5b. VERI KAYIT MIMARISI (Backtest Girdileri)

Backtest icin dort veri kaynagi toplanir. Hepsinin task'lar ile **bagimsiz**
calismasi (bot process'inden ayri) kesintiye karsi guvence verir:

| Veri | Nerede | Kim yazar | Siklik | Boyut (2026-08-07) |
|------|--------|-----------|--------|---------------------|
| Market snapshot (YES/NO fiyat) | `bot.db` -> `market_snapshots` | `bot_loop.py` snapshot_loop (24/7) + `JunboSnapshot` task (30dk) | 30 dk | 11.197 satir |
| Orderbook derinlik | `orderbook.db` -> `orderbook_snapshots` | `Junbo-OrderbookCollect` (scripts/collect_orderbook.py) | 1 saat | 3.692 satir |
| Gerceklesen hava durumu | `actuals.db` -> `actual_temperatures` | `Junbo-ActualsCollect` (scripts/collect_actuals.py) | 6 saat | 4.599 satir |
| Bet kayitlari | `bot.db` -> `bets` | bot_loop (bet placer/settler) | her islem | 117 bet |
| Backtest DB (cografi) | `backtest.db` (4 tablo) | `Junbo-SyncBacktest` (scripts/sync_backtest_db.py) | 6 saat | 35.551 snapshot + 32.616 forecast + 166 bet + 4.019 market |

**Retention (KALICI KURAL):**
- `market_snapshots`: **365 gun** tutulur. Kanit: `jobs/snapshot_job.py:121`
  `cleanup_old_snapshots(days: int = 365)`; `bot_loop.py:480` ve `snapshot_only.py`
  ayni degeri kullanir. (Eski 30 gun degeri backtest verisini cok erken silerdi -
  2026-08-07'de 365'e cikarildi.)
- DB yedekleri (`data/backups/`): **30 gun** tutulur (`scripts/backup_databases.py:44`
  `RETENTION_DAYS = 30`). Yedekler `Junbo-BackupDatabases` task'i ile 6 saatte bir alinir.
- `conftest.py` test-oncesi backup: **gunluk en fazla 1 kez** (marker throttling,
  `tests/conftest.py` `_pre_test_backup`). Eski davranis her test kosusunda bot.db
  kopyaliyordu (218 dosya / 33.5 GB birikim) - 2026-08-07'de duzeltildi.

---

## 6. "Bilgisayar 24 saat calisir / uykudayken wonla snapshot" - DUZELTILDI (2026-08-07)

**Onceki durum:** Bu iddia daha once tam saglanmamisti. Yapilan duzeltmelerle
artik saglanmaktadir:

### 6a. JunboSnapshot gorevi (KIRIKTI -> DUZELTILDI)

- **Kirik:** `snapshot_task.bat` -> eksik `snapshot_only.py` dosyasini cagiriyordu;
  her saat `python: can't open file snapshot_only.py` hatali loglari, `Last Result: 2`.
- **Duzeltme:** `snapshot_only.py` olusturuldu (repo kokunde). Task Scheduler'dan
  bagimsiz calisan, `take_market_snapshots()` + `cleanup_old_snapshots(days=365)`
  cagiran tek seferlik snapshot script'i.
- **Dogrulama:** `schtasks /run` sonrasi `Last Result: 0`; `data/snapshot_task.log`
  son satir `snapshot_only: 0 snapshots saved` (dedup nedeniyle 0 — bot 30dk loop'u
  ayni bucket'a zaten kayit yazmis). DB'de son snapshot 02:00, toplam 11197 kayit.

### 6b. JunboSnapshot araligi + WakeToRun (EKLENDI)

- Gorev XML'i guncellendi: `Repetition Interval` **PT1H -> PT30M** (30 dakikada bir),
  `Settings/WakeToRun` **true** (bilgisayar uykudaysa uyandirip snapshot alir),
  `StartWhenAvailable` true (uyandiktan sonra kacirilmis calismayi tamamlar).
- **Dogrulama:** XML'de `WakeToRun=true`, `Interval=PT30M`; task Status=Ready,
  Last Result=0 (elle tetikleme sonrasi).

### 6c. JunboBotWatchdog gorevi (DISABLED -> ENABLED)

- **Once:** Task Disabled idi; `logs/watchdog.log` 2026-07-20'de kesiliyordu —
  yani 17 gundur heartbeat/donma/crash izleme YOKTU.
- **Duzeltme:** `schtasks /change /tn "\JunboBotWatchdog" /enable` — task
  `scripts/bot_watchdog.py`'yi her 1 dk'da bir calistirir: servis RUNNING degilse
  enable+start; bot.log 15dk'dan eskiyse donmus kabul edip restart.
- **Dogrulama:** Log'a yeni satirlar geldi: `2026-08-07 04:56:01 OK running (log
  age 10s)`, `04:57:02 OK running (log age 2s)`; task Status=Ready.

### 6d. Windows servisi JunboBot

- **Status=Running, StartType=Automatic**; `sc qfailure`: RESTART 5s/10s/30s
  (crash sonrasi otomatik restart). Uptime 99 saat.

**Netice:** "Bilgisayar uykudayken otomatik uyanir ve snapshot alir" artik Task
Scheduler (`JunboSnapshot`, WakeToRun=true, 30dk) + bot ici `snapshot_loop`
(30dk, 24/7) + watchdog (1dk heartbeat) olmak uzere UCLU KATMANLA saglanmaktadir.

---

## 7. Yapilacak Duzeltmeler (KAPALI - 2026-08-07)

- ~~Task Scheduler `JunboSnapshot`: eksik `snapshot_only.py`~~ -> KAPANDI: dosya
  olusturuldu, task 30dk + WakeToRun ile guncellendi, `Last Result: 0`.
- ~~"Wake to run"~~ -> KAPANDI: `JunboSnapshot` XML'inde `WakeToRun=true` set edildi.
- ~~`JunboBotWatchdog` Disabled~~ -> KAPANDI: enable edildi, log dogrulandi.
- ~~`cleanup_old_snapshots(days=30)` backtest verisini erken siliyordu~~ -> KAPANDI:
  `jobs/snapshot_job.py:121`, `bot_loop.py:480`, `snapshot_only.py` hepsi `days=365`.
- ~~`backup_databases.py` purge eski formatlari parse edemiyordu (`bot_startup_*`,
  `bot_scheduled_*`, `bot_manual_*`, `bot_test_*` - "Could not parse" hatasi)~~ ->
  KAPANDI: regex tabanli `_parse_backup_ts()` (`(\d{8})[_-](\d{6})`) eklendi, 8 format
  test edildi, calistirma sirasinda hata YOK.
- ~~`conftest.py` her test kosusunda bot.db kopyaliyordu (218 backup / 33.5 GB)~~ ->
  KAPANDI: gunluk marker throttle (`data/.last_pre_test_backup`), 217 eski dosya
  silindi (33.5 GB), yeniden dogrulandi: 2 test kosusu, yeni backup YOK.

---

## 7b. ARIZA SENARYOLARI: BIR SEY BOZULURSA NE OLUR / NE YAPILIR

Bu bolum, "bir sey bozuldugunda ne olmasi gerektigini" tanimlar. Bir ariza ile
karsilasildiginda once buraya bak, sonra gerekirse kod kanitlarini dogrula.

### S1. Bot process'i cokerse (crash / stop)
- **Beklenen:** Windows servisi `JunboBot` (Automatic + `sc qfailure` RESTART 5s/10s/30s)
  otomatik yeniden baslatir. Yanit gelmezse `JunboBotWatchdog` (1 dk) servis
  RUNNING degilse enable+start eder; bot.log 15 dk eskiyse donmus kabul edip restart.
- **Dogrula:** `sc query JunboBot` (State=RUNNING); `data/logs/watchdog.log` son satir
  `OK running`; `http://127.0.0.1:8093/api/status` -> `is_running=true`.
- **Aksiyon gerekmez** — otomatik toparlanir.

### S2. Bilgisayar uykuya girer / kapali kalir
- **Beklenen:** `JunboSnapshot` task `WakeToRun=true` ile uyandirir ve kacirilmis
  calismalari `StartWhenAvailable` ile tamamlar; uyaninca 30 dk'da bir snapshot alir.
- **Dogrula:** `schtasks /query /tn "\JunboSnapshot"` -> Status=Ready, Next Run
  yakinda; `data/snapshot_task.log` son satirlar hata icermemeli.
- **Not:** Kacirilan surelerde veri EKSIK olur (gecmise donuk doldurma YOKTUR) —
  bu kayip kabul edilir; asil garanti uyku sirasinda bile alinmasidir.

### S3. Snapshot DB'de kayit yok / sayi artmiyor
- **Beklenen:** Bot 30 dk'da bir `market_snapshots`'a yazar. Artis yoksa: (a) bot
  calisiyor mu (`/api/status`), (b) snapshot_task.log son calisma (30 dk gecmemis),
  (c) DB boyutu. Dedup nedeniyle `0 snapshots saved` NORMALDIR (bucket 30 dk).
- **Aksiyon:** Bot durmussa S1'e bak; task kaciriyorsa S2'ye bak.

### S4. Disk dolar
- **Beklenen:** `backup_databases.py` (6 saatte bir, `Junbo-BackupDatabases`) 30
  gunden eski yedekleri siler; `cleanup_old_snapshots` snapshot'lari 365 gunde
  sifirlar; conftest gunluk max 1 test-backup uretir.
- **Dogrula:** `data/backups/` boyutu 2-3 GB civarinda tutmali (gunde ~4 backup x
  190 MB); anlik buyuk yiginlar anormaldir (ornek: 218 dosya / 33.5 GB -> throttle
  oncesi conftest kopyalari).
- **Aksiyon:** Purge calismazsa `scripts/backup_databases.py` elle calistir.

### S5. Bet penceresi disinda bet acildi / acilmadi
- **Beklenen:** Bet pencereleri `[(3,6),(12,15),(19,22)]` (bot.log: `Betting window
  KAPALI (hour=..., windows=...)`). SNAPSHOT bu kuraldan ETKILENMEZ — snapshot
  24/7 alinir (kasitli: giris zamani analizi icin).
- **Dogrula:** bot.log `Betting window` satirlari; bet saatleri pencerelerle uyumlu.

### S6. Purge "Could not parse" hatasi
- **Beklenen:** `backup_databases.py` tum formatlari regex ile parse eder
  (`bot_YYYYMMDD_HHMMSS`, `bot_startup_YYYYMMDD_HHMMSS_ffffff`, ...). Tekrar
  "Could not parse" gorulurse YENI format eklenmis demektir -> `TIMESTAMP_RE`
  guncelle ve 8 formatli unit testi (komut: regex test) yeniden calistir.
- **Aksiyon:** `scripts/backup_databases.py` icindeki `_parse_backup_ts()` + test.

### S7. Task Scheduler gorevi Disabled / eksik
- **Beklenen (2026-08-07 itibariyle):**
  | Gorev | Durum | Gorev |
  |-------|-------|-------|
  | JunboSnapshot | Ready, 30dk, WakeToRun | snapshot (24/7 garantisi) |
  | JunboBotWatchdog | Ready, 1dk | heartbeat + crash restart |
  | Junbo-BackupDatabases | Ready, 6sa | DB yedekleme + purge |
  | Junbo-ActualsCollect | Ready, 6sa | gerceklesen hava |
  | Junbo-OrderbookCollect | Ready, 1sa | orderbook derinligi |
  | Junbo-SyncBacktest | Ready, 6sa | backtest.db senkron |
  | JunboBot | Disabled (bilincli) | eski bat yontemi; yerine servis |
  | JunboBotBackup | Disabled (bilincli) | yerine Junbo-BackupDatabases |
  | JunboWARP | Disabled (bilincli) | kullanilmiyor |
- **Aksiyon:** Ready olmasi gereken bir gorev Disabled ise `schtasks /change /tn
  "\Gorev" /enable` ile ac, sonra log/Last Result dogrula.

---

## 8. Referans: Analiz Scriptleri (arsiv)

- `scripts/analyze_first_peak_climbs.py` (49 real climb; %20 takint elendi).
- `scripts/analyze_peak_to_settle.py` (39 market; peak->settle suresi; 17:00=15, 23:00=7).
- `scripts/analyze_dash_zones.py` vb. gayri kod-analitigi icin.

---

## 9. Dogrulama Standart Proseduru (OpenCode bu dosyayi LSP/agent olarak okuyamaz)

Kod degisikliginden sonra her zaman su sira (AGENTS.md'de de tekrar):
1. `ruff check . --fix`
2. `mypy . --ignore-missing-imports`
3. `pytest -q --tb=no` (mevcut test takimi gecmeli; 2026-08-07: 585 passed, 6 skipped)
4. Playwright E2E (headless=false) - dashboard canli veri, bet acma/kapama, ekran goruntusu
5. Sorun duzelt, tekrar calistir; tum testler gecmeden "tamamlandi" deme.

---

*(Bu dosya, dogrulanmis kurallari kalici kayit olarak saklar. 2026-08-07 itibariyle
 tum onemli kiriklar kapatildi: (1) `JunboSnapshot` -> `snapshot_only.py` olusturuldu,
 30dk + WakeToRun=true, Last Result=0; (2) `JunboBotWatchdog` enable edildi, heartbeat
 log dogrulandi; (3) snapshot retention 365 gun; (4) backup purge regex ile duzeltildi;
 (5) conftest test-backup'lari gunluk 1'e throttle edildi, 33.5 GB temizlendi.
 Kalan disabled task'lar (`JunboBot`, `JunboBotBackup`, `JunboWARP`) bilincli olarak
 kapatilmistir — bot Windows servisi (JunboBot, AUTO_START + failure restart)
 uzerinden yonetiliyor. ARIZA SENARYOLARI icin bolum 7b'ye bak.)*