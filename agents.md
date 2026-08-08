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
2. **`GELISTIRICI_NOTLARI.md`** — gelistiriciye yonelik: "Bilinen Kritik Hatalar & Cozumler" tablosuna her bugfix eklenir (hata + cozum), test komutlarindaki beklenen sayilar (ornegin "653 passed") gercek ciktilarla senkron tutulur.
3. Eski "bugfix" kayitlari SILINMEZ — tarih damgasiyla ek guncelleme yapilir (or: `(2026-08-08)`).
4. Commit mesajinda dokumantasyon degisikligi de yer alir (ayri commit zorunlu DEGIL).
5. Kontrol: commit oncesi `git diff --stat` — eger .py/.ts dosyasi degistiyse ve README/NOTLAR ayni commit icinde yoksa, dokumantasyon unutuldu demektir; ger eve donup ekle.

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
