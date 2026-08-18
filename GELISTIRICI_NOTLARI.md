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
# HEDEF: "667 passed, 8 skipped, 0 failed" (2026-08-16 itibari; tsc --noEmit = 0 hata)

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

> **Erken kapanis mekanizmalari KALDIRILDI (2026-08-12):** `RiskConfig`, `run_risk_management`, `check_stop_loss`, `check_take_profit`, `check_trailing_stop`, `check_time_decay`, `check_early_exit`, `check_rebalance`, `check_model_reversal`, `_reopen_after_stop_loss`, `partial_tp_done` silindi. Betler yalnizca settlement'ta kapanir. Silinen testler: `test_active_risk_management.py`, `test_take_profit_comprehensive.py`, `test_risk_behavior.py`, `edge/test_sl_reopen_chain.py`, `scripts/replay_test.py`. Suite: **633 passed, 7 skipped**.

> **Top-15 kapatma KALDIRILDI (2026-08-12):** `spread_placer` artik top-15 disi sehirlerin betlerini KAPATMAZ (kullanici karari: "ilk 15 bias sadece yeni gun aciliminda, kapatma yapilmayacak"). Kullanilmayan `close_losing_twin_bets` (tie_loser), `_cleanup_stale_bets` (stale_cleanup) ve tarihsel `24h_rule` mekanizmalari silindi — kodda hicbir kapanis uretmiyorlardi. Bet loglarina `bet#ID` eklendi.

> **METAR zirve-tespiti (2026-08-14):** `scrapers/metar.py` (aviationweather.gov NOAA, bedava) + `jobs/metar_peak.py` + `bot_loop.metar_loop` (30dk). Acik marketli sehirlerin METAR sicakligi gun icinde izlenir; max'a cikip **2 kez arka arkaya duserse** zirve kilitlenir, kazanan bucket'a (round(max)) tek esik YES bet ($1, order_id `metar_*`). Kapanisa <4 saat kalan sehirler atlanir. Not: aviationweather.gov sadece son 30 saat veri tutar — gecmis gun dogrulamasi yapilamaz, canli izleme icin tasarlandi. Sermaye +1000 USD. 14 Agu'da 5 bet acildi (entry 0.010).

> **GUNCEL SPREAD CONFIG (2026-08-16 kullanici karari "tek esik, 0.95, ilk 40"):** `spread_radius=0` (TEK esik: sadece tam merkez), `spread_max_cities=12`, `spread_max_entry=0.95` (0.01-0.95 arasi her fiyat), `spread_stake_usd=2.0`, `spread_max_bets_per_day=40`. Fair-value ve 0.10-0.20 olum bolge filtreleri KALDIRILDI (2026-08-16). Backtest 2026-08-14 (orderbook): radius0 +$41.9, radius3 -$317. Tarihsel kayitlardaki `spread_radius=1`/`0.30`/`350` ESKI konfigurasyonlardir (2026-08-15) — guncel durum icin bu nota bak.

**Kural:** Yeni bug bulundugunda once davranis testi yaz (sentetik + gercek DB), sonra duzelt.

**ALLOWED_DEAD** guncellemesi: yeni public fonksiyon eklediginde ya caller ekle ya da `tests/test_latent_bugs.py::ALLOWED_DEAD` kumesine aciklama ile ekle (entry point ise otomatik gecer).

---

## 5. Bilinen Kritik Hatalar & Cozumler

| Hata | Cozum |
|---|---|
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
  gelistirme/kanit analizleri. Backtest script'leri 2026-08-18'de TEK dosyada
  birlestirildi: `scripts/backtest.py {gunluk|orderbook|metar_peak|
  metar_vs_settlement|walk_forward}`; eski 22 varyant (`backtest_rolling.py`,
  `backtest_advanced.py`, `walk_forward_backtest.py` vb.) envanteriyle birlikte
  `backtest_archive/README.md` -> `backtest_archive/` altinda bilincli arsiv olarak korunur.
- `gunluk` subkomutu 2026-08-18'de LOOK-AHEAD'den arindirildi (forecast
  backtest.db gercek batch'lerinden + fetched_at<=kapanis kapisi) ve `--real-entry`
  bayragi eklendi (gercek bot fill'iyle capraz dogrulama). Detay ve sayilar:
  `reports/backtest_gercekci_2026-08-18.md`.

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