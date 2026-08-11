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

# Çıktı: "634 passed, 0 failed" olmalı.
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

**Bet açma stratejisi (her high/low sicaklik marketi icin):**
1. Polymarket'ten tum acik weather marketlerini cek
2. Her `(sehir, tarih, metric)` grubunda en yuksek `yes_price` olan marketi sec
3. **Her zaman YES tarafina bet ac** — high/low fark etmez, sadece en yuksek fiyati sec
4. `max_entry_price = 0.99` — 0.99 ve ustu fiyatlara bet ACILMAZ (kar marjini korumak icin, 0.99→1.00 komisyonsuz kayip ederiz)
5. 2+ gun sonrasi betler saat 13:00'dan once acilmaz (time gate — ilk acilista belirsiz)
6. Smart rotation: ayni grupta daha iyi fiyatli market bulunursa eski bet kapatilir, yenisi acilir
7. Tie: ayni fiyata sahip iki market varsa ikisini de ac, birinin one gecmesini bekle, geride kalanini sat
8. Price poller her 5 dakikada fiyat gunceller (settlement ve risk kontrolleri icin)
9. Polymarket UI dogrulama: `scripts/verify_ui_markets.py` her 2 saatte bir DB'yi Polymarket Gamma API ile karsilastirir
10. **Net edge kontrolu:** `should_bet` kosulunda `net_edge >= effective_min_edge` zorunlu — negatif veya cok dusuk edge varsa bet acilmaz

**Kasa bosluk sorunu:** Betler `open_bet_on_market` ve `place_all_pending` icinde `check_exposure_cap` ve `max_bet_cap` ile karsilanir. Kasa yetersizligi varsa bet reddedilir ve `Insufficient cash` log'u yazilir.

### 0 failed → Otomatik Bot Başlatma

Full test suite **0 failed** ile geçtiyse, bot otomatik restart edilir:

```bash
# 1. Mevcut bot'u durdur
python -c "
import socket
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(2)
result = sock.connect_ex(('127.0.0.1', 8093))
sock.close()
if result == 0:
    import urllib.request
    try:
        urllib.request.urlopen('http://127.0.0.1:8093/stop', timeout=3)
        print('Bot stopped via API')
    except Exception as e:
        print(f'Stop API failed: {e} (may need manual kill)')
else:
    print('Bot not running, skipping stop')
"

# 2. Biraz bekle (port boşalsın)
timeout /t 3 /nobreak >nul

# 3. Bot'u başlat
start /B python main.py bot
echo "Bot started on port 8093"
```

Alternatif (watchdog ile):
```bash
python watchdog.py start
```

Bot'un çalıştığını doğrula:
```bash
python -c "
import urllib.request, json
try:
    r = urllib.request.urlopen('http://127.0.0.1:8093/health', timeout=5)
    data = json.loads(r.read())
    print(f'Health OK: {data}')
except Exception as e:
    print(f'Health check failed: {e}')
"
```

## Zorunlu Test Komutları (Her Değişiklikten Sonra)

Bu projede **dead code, import hatası, calibration bug'ı** gibi sessiz hatalar daha önce CI'den kaçtı. Aşağıdaki adımlar **her kod değişikliğinden sonra** zorunludur.

### 1. Latent-Bug Testleri (Önce Bunu Çalıştır)
```bash
python -m pytest tests/test_latent_bugs.py -v --tb=long
```
Bu test paketi:
- **test_import_all_modules** — tüm .py dosyalarını import eder, kırık import/yazım hatası yakalar
- **test_config_proxy_map_matches_botconfig** — config proxy senkronizasyonu
- **test_required_modules_reachable** — zorunlu modüller import edilebilir mi
- **test_required_reachable_names** — zorunlu fonksiyon/sınıf isimleri var mı
- **test_calibration_get_calibrated_temperature_no_crash** — calibration crash yapmaz
- **test_calibration_fallback_mbe_matches** — fallback değer geçerli
- **test_no_dead_public_functions** — public fonksiyonların en az bir caller'ı var mı

### 2. Lint + Type Check
```bash
python quick_check.py --fast
```
Ya da teker teker:
```bash
ruff check . --ignore F401
mypy --ignore-missing-imports .
```

### 3. Değiştirilen Modülün Testleri
```bash
# Örnekler:
python -m pytest tests/test_calculator.py -v --tb=short
python -m pytest tests/test_calibration_audit.py -v --tb=short
python -m pytest tests/test_preflight.py -v --tb=short
```

### 3b. Config + Strateji Testleri (settings.py/strategy.py/bet_placer.py değişikliklerinde)
```bash
python -m pytest tests/test_faz25_35.py tests/test_strategy_selection.py tests/test_days_ahead_regression.py -v --tb=short
```
Bu testler `flat_bet_usd`, `max_bet_amount`, `total_exposure_pct`, `rotation_threshold` gibi config değerlerini doğrular.

### 4. E2E + Liveliness Testleri
```bash
python -m pytest tests/test_e2e_system.py tests/test_integration_e2e.py tests/test_faz2_e2e_mock.py tests/test_liveliness_audit.py --tb=short -q
```
Bu paket botun uçtan uca akışını (fetch → parse → analyze → bet) ve canlılık/işlevsellik kontrollerini doğrular. Sonraki adımlara geçmeden önce geçmeli.

### 4b. Muhasebe + Settlement Testleri (DRY_RUN/muhasebe değişikliklerinde ZORUNLU)
```bash
python -m pytest tests/test_accounting.py tests/test_settler_polymarket.py tests/test_betting_idempotency.py tests/test_signals_active_positions.py --tb=short -q
```
Bu testler DRY_RUN modunda bile muhasebenin tutarlı olduğunu doğrular. `debit_stake` / `credit_sale` / `credit_settlement` değişikliklerinde bu paket mutlaka çalıştırılmalı.

### 5. Full Test Run (Push Öncesi)
```bash
python -m pytest tests/ \
  --ignore=tests/test_betting_idempotency.py \
  --ignore=tests/test_comprehensive.py \
  --tb=short -q
```

### 6. Codegraph Sync
Kod değişikliğinden sonra index güncel kalsın (plugin etkinse):
```bash
codegraph sync
```
Ya da MCP tool ile `codegraph-plugin-sync`. Değişen dosyalar/semboller yeni çalışan botta ve sonraki keşiflerde doğru görünsün.

### 7. Bot Restart (Full suite 0 failed ise ZORUNLU)
Full test suite **0 failed** ile geçtiyse bot restart edilir — yeni kod çalışan süreçte kullanılsın:
```bash
# 1. Mevcut bot process'lerini bul ve durdur
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'main.py bot' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# 2. Biraz bekle (port boşalsın)
Start-Sleep -Seconds 3

# 3. Bot'u başlat
Start-Process -FilePath "python" -ArgumentList "main.py","bot" -WorkingDirectory "C:\Users\fdemir\Documents\New project\junbo" -WindowStyle Hidden
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
   ve `JunboBotWatchdog` (1dk), `JunboDataWatchdog` (5dk), `JunboBot` task (boot/logon)
   ile korunur. Port 8093'u sadece TEK `main.py bot` process'i dinlemeli.
3. Codegraph guncel mi? Degisiklik yaptiysan `codegraph sync` calistir.
4. Belirtilen isi yap. Tam suite 0 failed ise bot restart.
5. Dokumantasyon: README + GELISTIRICI_NOTLARI senkronu.
6. Commit + PUSH (kullaniciya sormadan, push bekleyen commit varsa).

### Kalici gercekler (dagilip kesfetme, MEVCUT durum budur)
- **Git remote:** `https://github.com/talcawarrior/junbo.git` — repo buraya TASINDI.
  REMOTE URL'DE GOMULU PUSH TOKENI VAR (`ghp_...@`). Asla bu token'i logla/goster.
  Repo tasinma olayi BILINIYOR; her prompt'ta "repo tasinmis" diye YENIDEN kesfetme.
- **Bot modu: PAPER (DRY_RUN).** Gercek Polymarket trade'i ASLA acilmaz. Sorma.
- **Strateji: SPREAD (ana mod).** `spread_radius=3`, `spread_max_cities=15`,
  `spread_max_entry=0.50` (0.50 ve ustu fiyata O SEHRE HIC GIRILMEZ — 0.50->1.00
  sadece 2x, zararli; kullanici karari 2026-08-11), `spread_stake_usd=2.0`,
  `spread_max_bets_per_day=350` (3 gun x 15 sehir x 7 = 315 + marj),
  `betting_window_enabled=False` (gun boyu bet).
- **Spread bet canli fiyatla acilir:** `_place_spread_bets_inner` entry fiyatini
  `mkt.yes_price`'dan okur (5 dk'da `run_fetch_markets` ile guncellenir).
  `_first_snapshot_price` KALDIRILDI (bayat snapshot yerine canli fiyat).
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
  4. **SEHIR YA TAM 7 BETLI OLUR YA DA HIC OLMAZ (KATI KURAL, 2026-08-11).**
     Bir sehirde forecast center +/- radius (7 esik) icindeki TUM esiklerin acik
     marketi VAR ve fiyati `0 < yes_price < max_entry` olmali. Herhangi bir esik
     eksikse (market yok / fiyat yuksek) O SEHRE HIC girilmez VE o sehrin o gun
     acik betleri KAPATILIR. "7 betli olacak yoksa o sehir olmayacak."
     Gerekce (Wellington 11.08): merkez 12C atlandi ama 9,10,11,13,14,15 ayaklara
     girildi -> 6 bet lost, -12.60$. Eksik ayak = spread mantigi bozuk.
     Kod: `spread_placer._place_spread_bets_inner` — `targets` (center±3) icin
     once tam-7 kontrolu, eksikse `close_bet_for_rotation` ile mevcut betleri kapat.
     Test: `test_city_skipped_when_center_market_missing`,
     `test_city_skipped_when_center_price_high`.
  4b. **`spread_max_entry=0.99` (fiyat onemsiz, test icin butun betlere gir).**
     Kullanici basi: "ilk basta sana fiyat onemli degil butun betlere gir onemli
     olan testi gormemiz demedim". 0.50 kurali KALDIRILDI (merkez esigi eliyordu).
     0.99 ustu fiyat pratikte yoktur.
- **Son suite durumu:** 712 passed, 6 skipped, 0 failed.
  (Ignore: test_betting_idempotency, test_comprehensive).
- **Test DB izole:** `tests/conftest.py` temp DB'ye yonlendirir — suite bot
  CALISIYORKEN de production DB'ye dokunmaz. Yine de guvenli tarafta kalmak icin
  degisiklik sonrasi ilgili testler + full suite calistirilir.
- **commit oncesi:** `ruff check . --ignore F401`, `mypy . --ignore-missing-imports`,
  `ruff format --check` degisen dosyalarda.
- **Bot start:** Windows servisi `JunboBot` (pythonservice.exe, AUTO_START).
  Manuel baslatma: `Start-Process cmd -ArgumentList "/c", start_bot.bat -WindowStyle Hidden`
  (bat SADECE watchdog.py calistirir; cift bot cakismasini onlemek icin dongu yok).
