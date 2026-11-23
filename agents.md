# Junbo AI Agent Instructions

## KALICI KARAR: HER ZAMAN PAPER MODE (SORMA, DEGISTIRME)

**2026-08-07 kullanici karari: Bot her zaman PAPER/SIMULATION mode'da calisir. Gercek Polymarket trade'i ASLA acilmayacak.**
- `.env` icinde `DRY_RUN=true` sabittir (silinmez, false yapilmaz)
- `executor/bet_placer.py` icinde `_live_allowed = False` KOD SEVIYESINDE sabitlenmistir
- Bu kural hakkinda kullaniciya ASLA soru sorma, "gercek trade'i aktif edelim mi" diye ONERME
- Ayrica `LIVE_TRADING_ENABLED=true` gibi bir ayar YAPMAYACAKSIN

## KRITIK KURAL: TURKCE KARAKTER YASAK

**Kodda, yorumlarda, commit mesajlarinda, log mesajlarinda ASLA Turkce karakter kullanma.**
C, S, G, I, O, U harflerinin cirkeli (sapkali) versiyonlari yasaktir.

- Dogru: `Kritik kural`, `duzeltme`, `olustur`, `calistir`, `degistir`, `tur` yorumu
- Yanlis: `Kritik kural`, `düzeltme`, `oluştur`, `çalıştır`, `değiştir`, `tür`

Kurallar:
1. `ç` -> `c`, `Ç` -> `C`
2. `ğ` -> `g`, `Ğ` -> `G`
3. `ı` -> `i`, `İ` -> `I`
4. `ö` -> `o`, `Ö` -> `O`
5. `ş` -> `s`, `Ş` -> `S`
6. `ü` -> `u`, `Ü` -> `U`
7. `â`, `î`, `û` gibi diger aksanli harfler de duz harfe cevrilir

**Neden:** Turkce karakterler Windows console'da mojibake (bozuk goruntuleme) yaratir, dosya kodlamasi sorunlarina yol acar, ve `test_no_mojibake_in_python_files` testini kirmasa da tutarliligi bozar.

Bu kural commit oncesi kontrol edilir: `ruff check .` mojibake yakalamaz, manuel veya `grep -P "[çğıöşüÇĞİÖŞÜ]"` ile dogrula.

## KRITIK KURAL: Kontrol Etmeden Cevap Verme

**ASLA doğrulamadan cevap verme.** Herhangi bir şey söylediğinde ÖNCE:
1. Kodu oku
2. API'yi sorgula veya DB'yi kontrol et
3. Testi çalıştır
4. Sonra cevap ver

"Tamam", "düzeltilmiş", "çalışıyor", "tamamlandı" gibi ifadeler kullanmadan ÖNCE **kanıt göster** (log, API çıktısı, test sonucu).

**Yalan söyleme.** Emin olmadığın şeyi "oldu" deyip geçme. Bilmiyorsan "bilmiyorum, kontrol edeyim" de.

## KRİTİK KURAL: Sadece İstenen Değişiklikleri Yap

**Kullanıcı sadece belirli bir şeyi düzeltmemi söylediyse, SADECE o şeyi düzelt.**
**Başka hiçbir kodu, stili, boyutu, yerleşimi ELEME.**
**Bozuk olmayan hiçbir yeri DEĞİŞTİRME.**

- Kullanıcı "X'i düzelt" dediyse → SADECE X'i düzelt
- Kullanıcı "Y'yi kaldır" dediyse → SADECE Y'yi kaldır
- Kullanıcı "Z'yi ekle" dediyse → SADECE Z'yi ekle
- **BAŞKA HİÇBİR ŞEY YAPMA** — kart boyutları, fontlar, yerleşim, renkler, padding, margin, gap, vs.

Eğer bir değişiklik yapacaksan, ÖNCE kullanıcıya sor:
- "Bu değişikliği yapmamı mı istiyorsun?"
- "Şu anda bozuk mu?"

## KRITIK KURAL: Dokumantasyon Senkronu (README + GELISTIRICI_NOTLARI)

**Her kod degisikligi, bugfix, karar veya yeni feature commit'lenmeden ONCE mutlaka dokumante edilir.**

Kurallar:
1. **`README.md`** — kullanicilara yonelik: mimari degisiklik, yeni ayar, Task Scheduler degisikligi, kurulum adimi, sorun giderme tablosu, Kararlar Log bolumu mutlaka guncellenir.
2. **`GELISTIRICI_NOTLARI.md`** — gelistiriciye yonelik: "Bilinen Kritik Hatalar & Cozumler" tablosuna her bugfix eklenir (hata + cozum), test komutlarindaki beklenen sayilar (ornegin "653 passed") gercek ciktilarla senkron tutulur. **Bu dosya ayni zamanda eski `docs/ANAYASA.md` icerigini de tasir** (bolum 12: dogrulanmis davranis kurallari; bolum 13: ariza senaryolari) — ANAYASA ayri dosya olarak YOKTUR, icerigi burada yasatilir.
3. Eski "bugfix" kayitlari SILINMEZ — tarih damgasiyla ek guncelleme yapilir (or: `(2026-08-08)`).
4. Commit mesajinda dokumantasyon degisikligi de yer alir (ayri commit zorunlu DEGIL).
5. Kontrol: commit oncesi `git diff --stat` — eger .py/.ts dosyasi degistiyse ve README/NOTLAR ayni commit icinde yoksa, dokumantasyon unutuldu demektir; ger eve donup ekle.
6. **Ariza senaryolari GELISTIRICI_NOTLARI bolum 13'e islenir** — yeni bir ariza tipi kesfedildiginde (S1-S7 gibi) madde eklenir; davranis kurali degistiginde bolum 12 guncellenir.

**On madde:** README ve NOTLAR senkronsuz commit kabul edilmez.

## KRITIK KURAL: Sifir Tolerans — Hicbir Test Gecilmez

**Hiçbir test "pre-existing" diye geçilmez, ignore edilmez, sonraya bırakılmaz.**
Eğer bir test fail ediyorsa, testin kendisi mi yanlış yoksa kod mu hatalı diye bakılır ve **derhal düzeltilir.**

```bash
# Her kod değişikliğinden sonra FULL test suite çalıştır:
python -m pytest tests/ \
  --ignore=tests/test_betting_idempotency.py \
  --ignore=tests/test_comprehensive.py \
  --tb=short -q

# (test_betting_idempotency ve test_comprehensive ignore edilebilir)
```

Eğer bir test fail ediyorsa:
1. Hatanın kaynağını bul (test mi, kod mu, config mi)
2. Düzelt
3. Full suite'i tekrar çalıştır
4. **0 failed görene kadar iş bitmez**

## KRİTİK KURAL: Tüm Bug'lar Düzeltilir

**Eski ya da yeni, bulunan TÜM bug'lar derhal düzeltilir.** "Eski bug", "sonradan olmuştur" gibi bahaneler kabul edilmez.

Tarama yapılacak:
1. `ruff check . --select F821,F841` — undefined name ve unused variable
2. `python -m py_compile <file>` — syntax error
3. Import test — tüm modüller import edilebilir mi
4. Runtime test — `.first()` sonrası None check var mı, division by zero riski var mı

Bulunan her bug düzeltilip test edilmeli, sonra commit atılmalı.

## Botun Temel Mantığı

**GUNCEL STRATEJI (2026-11-18: SPREAD KAPALI, METAR-only; bakiniz: CLAUDE.md bolum 2 — yetkili config):**

**1) SPREAD modu (KAPALI, `SPREAD_ENABLED=false`):**
- **2026-11-18 kullanici karari:** gercek orderbook + gercek Polymarket sonucu ile 2 aylik backtest **1.067 bet, %22.3 win, -$854.67**; edge filtresi (`entry < fair`) de negatif. Spread kapatildi; kod korunur, `SPREAD_ENABLED=true` ile geri acilir.
- Eski parametreler: `spread_radius=0`, `spread_max_cities=40`, `spread_max_entry=0.50`, `spread_stake_usd=2.0`, gunluk limit 120.
- Acilan betler settlement'a kadar TUTULUR; erken kapanis mekanizmalari YOKTUR.

**2) METAR-peak modu (HIBRIT, 2026-08-20):**
- `_avg_peak_hour`: sehir bazli gecmis ortalama peak saati (bugun haric, >=3 gun) — saat gelmeden bet acilmaz
- Saat gelince cur_max'a ERKEN giris adayi; bucket fiyati **0.50 ustunde** ise 1-dusus kilidi beklenir, altinda dusus beklemeden girilir
- Aktar: zirve asilirsa (cur_max > kilitli peak) eski bet kapatilir + yenisine acilir (zincir 22->23->24)
- `MIN_ENTRY=0.05`, stake $3, SADECE `temperature_max` + `RANGE` (tam bucket)
- **KARA LISTE KALDIRILDI (2026-08-21):** `METAR_PEAK_BLACKLIST` bos — TUM sehirlerde METAR-peak bet acilir (eski liste VHHH, ZGSZ, KBKF, KATL, KSEA, KSFO, NZWN %20-57 tutma oraniyla kullanici riski kabul etti; veri toplama + spread zaten devam ediyordu). Ileride tekrar kisitlamak icin set doldurulur
- **POLYMARKET METAR KURALI (2026-11-23):** HK disi tum sehirler RUTIN METAR (SPECI sayilmaz) + RMK T grubu (onda bir) + `floor(x+0.5)` ile cozulur; tek yuvarlama `scrapers.metar.polymarket_round`. Hong Kong HKO ile cozuldugu icin `NON_METAR_RESOLUTION={"VHHH"}` -> HK'de METAR-peak YOK (bu kara liste DEGIL). Panama = MPMG
- **ERKEN GIRIS KIYASI (2026-11-23, KARAR KULLANICIDA):** replay (gercek Polymarket sonucu, 17 Kas kirli gun haric) erken ACIK -$132.24 / KAPALI +$14.67; canli giris <=0.50 -$44.66, >0.50 +$11.84. Kod DEGISMEDI; `EARLY_MAX_PRICE=0.50` duruyor
- Bayat METAR (>45 dk): DERHAL yeniden cekim denenir; taze gelirse devam, gelmezse atla (aktif duzeltme)

**3) Korumalar:** stale fiyat guard (CLOB, %15 sapma), orderbook.db stale guard yedegi, 45dk bayat METAR, PAPER MODE kalici (`_live_allowed=False`).

Eski edge-tabanli mod (`BETTING_STRATEGY=edge`) korunur ama kullanilmaz.

## Zorunlu Test Komutları (Her Değişiklikten Sonra)

Bu projede **dead code, import hatası, calibration bug'ı** gibi sessiz hatalar daha önce CI'den kaçtı. Aşağıdaki adımlar **her kod değişikliğinden sonra** zorunludur.

### 1. Full Test Run (Push Öncesi)
```bash
python -m pytest tests/ \
  --ignore=tests/test_betting_idempotency.py \
  --ignore=tests/test_comprehensive.py \
  --tb=short -q
```

### 2. Codegraph Sync
Kod değişikliğinden sonra index güncel kalsın (plugin etkinse):
```bash
codegraph sync
```
Ya da MCP tool ile `codegraph-plugin-sync`. Değişen dosyalar/semboller yeni çalışan botta ve sonraki keşiflerde doğru görünsün.

### 3. Bot Restart (Full suite 0 failed ise ZORUNLU)
Full test suite **0 failed** ile geçtiyse bot restart edilir — yeni kod çalışan süreçte kullanılsın:
```bash
# 1. Mevcut bot process'lerini bul ve durdur
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'main.py bot' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# 2. 2026-11-18: Bot `JunboBot` Windows servisi altinda calisir; servis izleyicisi
#    (junbo_service.py `_monitor`) cocuk sureci ~8 sn icinde KENDISI yeniden baslatir.
#    Start-Process ile ikinci bot BASLATMA (servis disi kopya + 8093 port cakismasi).
#    Servis kapaliysa: net start JunboBot
Start-Sleep -Seconds 10
Get-NetTCPConnection -LocalPort 8093 -State Listen | Select-Object OwningProcess
```

Bot'un çalıştığını doğrula:
```bash
python -c "
import urllib.request, json
try:
    r = urllib.request.urlopen('http://127.0.0.1:8093/api/status', timeout=5)
    d = json.loads(r.read())
    print(f'Status OK: is_running={d.get(\"is_running\")} last_scan={d.get(\"last_scan\")}')
except Exception as e:
    print(f'Status check failed: {e}')
"
```

---

## Kodlama Pattern'leri

### Calibration/Backfill (Dead-Code'a Düşmemesi İçin)
- `CalibrationEngine`, `DataBackfiller` sadece `jobs/evolution_job.py::_run_calibration_backfill()` üzerinden çağrılır
- `bot_loop._run_daily_maintenance` içinde evolution job tetiklenir
- Yeni bir model/engine eklerken **mutlaka** `test_latent_bugs.py::REQUIRED_REACHABLE_MODULES` listesine ekle

### Lazy Singleton Pattern (Calculator İçin)
```python
_CALIBRATION_ENGINE: CalibrationEngine | None = None

def _get_calibration() -> CalibrationEngine | None:
    global _CALIBRATION_ENGINE
    if _CALIBRATION_ENGINE is None:
        ce = CalibrationEngine()
        if ce.bias_map:
            _CALIBRATION_ENGINE = ce
    return _CALIBRATION_ENGINE
```
- Veri yoksa None döner, crash olmaz
- Her analysiste yeniden yüklenmez

### Fallback Pattern (Kaynak Adı Uyuşmazlığı İçin)
```python
# Ex: "openmeteo" diye bir model bias_map'te yok → tüm modellerin ortalamasını kullan
avg_mbe = _city_metric_avg_mbe(city, metric)
return raw_temp - avg_mbe
```

### Marker Throttling (Günde 1 Kez)
```python
_MARKER_PATH = "data/.last_X_run"
_marker = _read_marker()
if _marker != today:
    run_X()
    _write_marker(today)
```
- Restart'larda tekrarlanmaz
- `bot_loop.py::_run_daily_maintenance` içinde çağrılır

### CI Katmanları (Kaçış Yok)
1. **Lint + Type Check** (ruff, mypy)
2. **Latent-bug testleri** (import-all, dead-code census)
3. **Core engine testleri** (calculator, asi, calibration)
4. **Unit/regression/property**
5. **E2E/integration**
6. **Full suite + coverage**

Ayrintili test paketleri: README "Test Paketi" tablosu ve GELISTIRICI_NOTLARI test komutlari.

---

## ALLOWED_DEAD Güncelleme

`test_latent_bugs.py::ALLOWED_DEAD` seti — yeni bir public fonksiyon eklediğinde:
1. Eğer fonksiyon bir **entry point** (FastAPI route, CLI, callback, framework hook) ise → ALLOWED_DEAD'a ekle
2. Eğer fonksiyonun **caller'ı varsa** → hiçbir şey yapma, test otomatik geçer
3. Eğer fonksiyon **gerçekten dead** ise → test fail eder, **ya caller ekle ya da allowlist'e ekle**

Ekleme formatı:
```python
"fonksiyon_adi",  # neden dead olduğu açıklaması
```

---

## KALICI PROJE BELLEK (2026-08-11 guncel — her prompt'ta yeniden kesfetme)

Bu bolum projenin ANAKAYNAK DURUMUNU kaydeder. Yeni bir prompt geldiginde
BUNLARI BIR DAHA SORMA, KEŞFETME, audit etme — dogrudan burada yazilani esas al.
Degisiklik oldugunda bu bolumu de guncelle.

### Oturum baslangic proseduru (her prompt basinda SADECE 1 kez)
1. `git status -sb` + `git log origin/main..main --oneline` calistir — push bekleyen commit var mi?
   Eger VARSA iş bitiminde push et, kullaniciya "push edildi mi" diye SORMADAN.
2. Bot calisiyor mu? `GET 127.0.0.1:8093/api/status` — cagri basarisizsa bot kapali.
   Bot ayrica bir Windows servisidir (`sc.exe query JunboBot` — STATE 4 RUNNING olmali)
   ve `JunboBotWatchdog` (1dk), `JunboDataWatchdog` (6dk) ile korunur. `JunboBot` task'i
   KASITLI Disabled (servis + watchdog yeterli; ikisi birden cift bot riski yaratir). Port 8093'u sadece TEK `main.py bot` process'i dinlemeli.
3. Codegraph guncel mi? Degisiklik yaptiysan `codegraph sync` calistir.
4. Belirtilen isi yap. Tam suite 0 failed ise bot restart.
5. Dokumantasyon: README + GELISTIRICI_NOTLARI senkronu.
6. Commit + PUSH (kullaniciya sormadan, push bekleyen commit varsa).

### Kalici gercekler (dagilip kesfetme, MEVCUT durum budur)
- **Git remote:** `https://github.com/talcawarrior/junbo.git` — repo buraya TASINDI.
  REMOTE URL'DE GOMULU PUSH TOKENI VAR (`ghp_...@`). Asla bu token'i logla/goster.
  Repo tasinma olayi BILINIYOR; her prompt'ta "repo tasinmis" diye YENIDEN kesfetme.
- **Bot modu: PAPER (DRY_RUN).** Gercek Polymarket trade'i ASLA acilmaz. Sorma.
- **Canli DB yolu:** `.env` `DB_PATH=C:\JUNBO data\bot.db` + `JUNBO_DATA_DIR=C:\JUNBO data` (2026-11-18 tasima). `D:\JUNBO data` yedek/arsiv olarak durur. Repo `data/bot.db` ticaret icin KULLANILMAZ; activity/peak_watch dahil tum path'ler `utils/db_paths.py` uzerinden okunur. `/health` endpoint'i YOK; dogru: `/api/status`, `/api/health-check`.
- **Strateji: METAR-only (2026-11-18).** `SPREAD_ENABLED=false`; spread placer + edge dongusu bet acmaz. METAR-peak hibrit calisir. Spread parametreleri (tarihsel): `spread_radius=0`, `spread_max_cities=40`, `spread_max_entry=0.50`, `spread_stake_usd=2.0`, `spread_max_bets_per_day=120`.
- **Spread bet canli fiyatla acilir (KAPALI):** `_place_spread_bets_inner` entry fiyatini
  `mkt.yes_price`'dan okur; `SPREAD_ENABLED=true` ile geri acilir.
- **VERI BOSLUGU (2026-11-18 tespit + fix):** `orderbook_snapshots` 2026-09-28 19:59Z -> 2026-11-11 18:59Z arasi yok (~44 gun; id bazli ikili aramayla dogrulandi). Kok neden: `JunboDataWatchdog` 19.09 01:30 -> 11.11 21:53 arasi calismadi; `JunboSnapshot` (29.09) ve `Junbo-BackupDatabases` (28.09) Disabled kaldi; `ensure_task_enabled` PowerShell komutu bozuktu (`}}` + 30s timeout) ve bu iki gorev listede yoktu. Fix: gecerli PowerShell + `TASK_ENSURE_NAMES` tam liste, `bot_watchdog` data-watchdog log bekcisi, CLOB arsiv yolu `get_data_dir()`. Ekim depth geri getirilemez; Ekim fiyat verisi `market_snapshots`'ta (1.087.985 satir). Runner DB'leri (`tempmarket-edge/main`, `kardesler`) C:'ye tasindi; `forecast_runner\data` junction C:'yi gosteriyor.
- **DENETIM DUZELTMELERI (2026-11-18 aksam):** (a) Yedek: `backup_databases.py` UTC gununde 1 kez
  (marker `<JUNBO_DATA_DIR>\.last_backup_databases_run`) + prefix basina son 7 kopya; `data_watchdog`
  BACKUP esigi marker > 26 sa. (b) `.env` `JUNBO_HEAT_DB`/`HEAT_MAIN_DB`/`OM_CACHE_DB` -> `C:\JUNBO data`.
  (c) CLOB REST yedegi `DetachedInstanceError` yuzunden hic calismiyordu -> duzeltildi; arsiv toplu tek
  transaction, WS arsiv yazimi `asyncio.to_thread`, collector 100 satirda bir commit. (d) Runner kapilari
  UTC slot tabanli (`forecast_runner/windows-runner/gates.mjs`; WN2 12s, Open-Meteo 6s, IEM 24s).
  (e) `JunboWARP` = `scripts\warp_health.ps1` (5 dk; proxy yoklama + reconnect; WARP modu ASLA
  degistirilmez -- `start_warp.ps1` OBSOLETE, `mode warp` SOCKS'u kapatir). (f) `gamma-api` HTTP 421
  makine hosts sabitlemesinden gelir; bot `utils/poly_dns.py` (DoH) ile asar -- WARP arizasi sanma.
- **DOGRULANMIS BULGULAR (2026-08-11, kullanici tespiti):**
  1. **Yeni-market fast mode GUN BAZLI olmalidir, sayi bazli DEGIL.** Gun dongusu:
     bugunun marketleri kapanir, 2-gun-sonrasi acilir -> toplam market sayisi
     YAKLASIK AYNI KALIR. Sayi artisiyla tetikleme YANLIS. Dogru sinyal:
     acik TARIH kumesinde yeni bir gun belirmesi (13. gunun marketleri acilmasi).
     Bot bunu `_get_open_target_dates()` kumesiyle algilar, sayiyla DEGIL.
  1b. **ERKEN GIRIS icin 0-13 UTC penceresinde hafif probe (1 sn).** Kullanici
     hedefi (2026-08-11): "limit altinda kalmak degil, piyasaya ERKEN girmek —
     millet milisaniyelerle islem yapiyor". Snapshot analizi: ilk market acilislari
     04:00-12:30 UTC'ye yayiliyor (sabit gece yarisi acilisi YOK). Cozum:
     `bot_loop._probe_new_target_date()` — Polymarket Gamma'ya TEK hafif sorgu
     (public-search, limit_per_type=5, order=endDate desc) ile DB'deki max acik
     tarihten ileri tarih var mi bakar. 0-13 UTC penceresinde scan loop her ~1 sn
     (midnight_scan_interval=1) probe yapar; yeni tarih VARSA hemen tam
     `run_fetch_markets` + `place_spread_bets`, YOKSA cekis yapilmaz (rate limit
     korunur). Pencere disinda (13:00+) normal 5 dk tarama. `midnight_scan_window`
     artik SAAT cinsinden (13). Probe `asyncio.to_thread` ile cagrilir (async loop
     icinde asyncio.run hatasi olmamasi icin).
  1c. **CLOB WebSocket aktive (2026-08-11).** `bot_loop.clob_stream_loop(state)` —
     acik betlerin marketlerine Polymarket CLOB WebSocket aboneligi; fiyat
     degisimlerini ANINDA alir (polling 5 dk yok). `CLOBMarketStream` (scrapers/
     clob_stream.py) onceden tanimliydi ama HIC kullanilmiyordu; main.py'de
     state.tasks["clob_stream"] olarak baslatilir. Fiyat olayi gelince ilgili
     WeatherMarket.yes_price/no_price guncellenir (status degistirilmez).
  2. **Spread sehir secimi: ANA KRITER "tahmini EN AZ SAPAN" sehirdir, sicaklik DEGIL.**
     Kullanici ornegi (2026-08-11): London tahmini 20C az sapiyorsa SECILIR,
     Kahire tahmini 45C cok sapiyorsa ELENIR. Sicaklik degeri ONEMLIDIR.
     Dogru metrik: `historical_calibrations.bias` uzerinden sehir bazli ortalama
     |bias| (tahmin - gercek, mutlak). Dusuk |bias| = az sapan = SECILIR.
     |bias| > 2.5C olan sehirler sapan sayilir, en sona atilir (elenir).
     Siralama anahtari: (sapan mi?, |bias| kucuk once) — sicaklik SADECE
     esitlikte tie-break. BU KURALI HICBIR ZAMAN "en sicak 15" diye YANLIS
     YAZMA/YAPMA; kodda, yorumlarda, dokumanda, cevapta hep "en az sapan" olur.
  3. **`yes_price is None` (entry yok) pratikte olmaz.** Polymarket YES betleri
     daima fiyatla acilir (0.1 cent min tick). `entry = mkt.yes_price` NULL
     guard'i sadece bozuk/yarim veri icin guvenliktir; normal kosulda tetiklenmez.
     Kullanici bunu "sallama" olarak isaretledi — aciklamalarda gercek mekanizma
     boyle soylenmemeli.
  4. **TAM-7 KURALI KALDIRILDI (2026-08-11 kullanici karari A).** Backtest
     (gercek bot davranisi, kayan pencere dahil — `backtest_archive/backtest_rolling_window.py`):
     tam-7 zorunlu karliligi DUSURUYOR: 0.30 tam-7'siz +$53,284 vs tam-7'li +$5,611;
     0.99 tam-7'siz +$52,018 vs tam-7'li +$19,747. Sebep: 7 esigin hepsi acik +
     fiyat siniri altinda sarti, merkez kayinca kARLI esikleri de kapatiyor.
     Gecerli kural: `spread_max_entry=0.30` + KAYAN PENCERE (merkez kayinca eski
     uclari kapat, yenileri ac) — tam-7 yok. Merkez marketi olmasa da acilabilen
     ayaklar acilir. Test: `test_open_legs_when_center_market_missing`.
  4b. **`spread_max_entry=0.30`.** Backtest en iyi config (kayan pencere + 0.30
     -> +$53,284). 0.30 ustu fiyatli esiklere girilmez. (Onceden 0.99 denenmis,
     kullanici "fiyat onemsiz" demisti; sonra veri 0.30'u gosterdi.)
- **Son suite durumu:** 837 passed, 7 skipped, 0 failed (2026-11-23, 569 sn; Cowork METAR kurali +
  hibrit replay + uretim-makinesi kapisi). (Onceki: 797/8/2 — 2026-11-18 aksam, 2 hata WARP kopuklugu.)
  (Ignore: test_betting_idempotency, test_comprehensive).
- **Uretim makinesi kapisi (2026-11-23):** canli `bot.db`/WARP'i dogrulayan testler `live_bot_db` /
  `production_host` fixture'larini kullanir (conftest): uretimde HER ZAMAN kosar (ariza = FAIL),
  canli veri olmayan ortamda (Cowork/CI) acik nedenle atlanir. Canli DB'yi okuyan test `mode=ro` acar.
- **Test DB izole:** `tests/conftest.py` temp DB'ye yonlendirir — suite bot
  CALISIYORKEN de production DB'ye dokunmaz. Yine de guvenli tarafta kalmak icin
  degisiklik sonrasi ilgili testler + full suite calistirilir.
- **commit oncesi:** `ruff check . --ignore F401`, `mypy . --ignore-missing-imports`,
  `ruff format --check` degisen dosyalarda.
- **Bot start:** Windows servisi `JunboBot` (pythonservice.exe, AUTO_START).
  Manuel baslatma: `Start-Process cmd -ArgumentList "/c", start_bot.bat -WindowStyle Hidden`
  (bat SADECE watchdog.py calistirir; cift bot cakismasini onlemek icin dongu yok).
