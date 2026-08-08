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
# HEDEF: "653 passed, 6 skipped, 0 failed" (tsc --noEmit = 0 hata)

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

*Bir radden analiz dokumanlari icin `docs/ANAYASA.md` (calisma kurallari/self-healing) ve `specs/` altindaki spec'lere gidin — bunlar ayri dosyalar olarak korunur.*