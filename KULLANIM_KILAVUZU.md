# Junbo Kullanim Kilavuzu

**Versiyon:** 1.0
**Tarih:** Haziran 2026
**Platform:** Polymarket (Hava Durumu Ticaret Botu)

---

## 1. Proje Nedir

Junbo, Polymarket uzerinde hava durumu kaynakli piyasalarda otomatik alim-satim yapan bir Python botudur. Bot, 8 farkli hava durumu modelinden (GFS, ECMWF, GEM, ICON, JMA, CMA, UKMO, MeteoFrance) tahminleri agirlikli olarak birlestirir, Polymarket'teki fiyatlari karsilastirir ve deger avantaji (edge) tespit ettiginde kademeli (ladder) bahisler acar.

Botu sifirdan kuran birinin bilmesi gereken temel ozellikler:

- **Kagit modu (DRY_RUN):** Varsayilan olarak aktiftir. Gercek para kullanilmaz, yalnizca simulasyon yapilir.
- **Dashboard:** Next.js 16 + shadcn/ui + Recharts ile yapilmis web arayuzu. Port 8092'de calisir.
- **Otomasyon:** `scan_and_bet_loop` ve `settlement_loop` arka plan donguleri ile surekli calisir.
- **Risk yonetimi:** Kelly kesirli bahis boyutlandirma, stop-loss, take-profit, trailing stop, time-decay, sehir bazli pozisyon siniri.
- **SIA (Self-Improving Algorithm):** Model agirliklarini Brier skorlamasi ile otomatik optimize eder.
- **ASI-Evolve:** Genetik algoritma ile strateji evrimi, kalibrasyon, cognition base, LLM 3-layer loop.
- **Slippage modeli:** Tiered siparis defteri simulasyonu ile net edge hesaplama.

---

## 2. Mimari Yapisi

```
Junbo/
  main.py                 # CLI giris noktasi, port cakizma cozumu
  api.py                  # FastAPI sunucusu, tum API endpointleri, BotState
  bot_loop.py             # Arka plan donguleri: scan_and_bet_loop, settlement_loop
  config/
    settings.py           # Tum ayarlar, sehir-ICAO eslesmesi, varsayilan degerler
    logging_config.py     # Dosya loglama (10MB x 5 dondurme)
  database/
    db.py                 # SQLAlchemy engine, WAL modu, oturum yonetimi
    models.py             # Tablo tanimlari (WeatherMarket, WeatherForecast, Analysis, Bet, Portfolio, ModelPerformance, HistoricalCalibration)
  scrapers/
    polymarket.py         # Gamma API ile Polymarket verisi cekme
    meteo.py              # Open-Meteo + WeatherAPI ile hava durumu verisi
    async_client.py       # Async HTTP istemcisi
  engine/
    calculator.py         # 8-model ensemble hesaplama, agirlikli olasilik, Kelly kriteri
    strategy.py           # RiskManager, BettingEngine, SIALoop, ActiveRiskManagement
    market_parser.py      # Piyasa sorularini cozumleme (HIGH/LOW/RANGE), sehir eslesmesi
    decision.py           # BetDecision sinifi
  executor/
    bet_placer.py         # Bahis yerlestirme (kagit ve canli mod), ladder order
    settler.py            # Gamma API uzerinden sonuc kontrolu ve karsilama
  utils/
    kelly.py              # Kelly katsayisi hesaplama
    probability.py        # Olasilik estimasyonu, normal CDF
    accounting.py         # Nakit hesaplama (debit, credit, karsilama)
    weights_store.py      # Model agirlik ve strateji parametreleri dosya okuma/yazma
    validators.py         # Girdi dogrulama
    price_sanity.py       # Fiyat dogrulama, EV hesaplama
    retry.py              # Tekrar deneme dekoratoru
    errors.py             # Ozel istisna siniflari
    formulas.py           # PnL, exposure, fee hesaplama formulleri
    slippage.py           # Slippage modeli (flat/tiered/orderbook)
  asi_engine/             # ASI-Evolve sistemi
    orchestrator.py       # Ana koordinasyon
    asi_evolve.py         # Genetik algoritma ile strateji evrimi
    calibration_engine.py # Sehir bazli bias kalibrasyonu
    cognition_base.py     # Icgoru depolama
    data_backfiller.py    # Tarihsel veri doldurma
    backtest_simulator.py # Virtual backtest
    karpathy_weekly.py    # Haftalik grid search
    llm_client.py         # Z.AI API istemcisi
    llm_loop_orchestrator.py  # LLM 3-layer loop
    analyzer_agent.py     # Analiz ajanı
    researcher_agent.py   # Arastirma ajanı
    sia_harness.py        # SIA test sistemi
    sia_hourly.py         # Saatlik SIA optimizasyonu
  jobs/
    scheduler.py          # Is parcasi yonetimi: run_fetch_markets, run_parse_markets, run_fetch_weather, run_analyze, run_place_bets, run_settle, run_cycle
  data_pipeline/          # Veri hatti
  dashboard/              # Next.js dashboard (static export)
  src/                    # Next.js kaynak kodlari (app, components, hooks, lib)
  tests/                  # 304 test dosyasi
  scripts/                # Diagnostik ve yardimci scriptler
  Makefile                # Kod kalitesi komutlari
  pyproject.toml          # ruff/mypy ayarlari
  pytest.ini              # Test konfigurasyonu
  requirements.txt        # Bagimliliklar
  package.json            # Next.js bagimliliklari
  .env.example            # Ornek ortam degiskenleri
```

---

## 3. Kurulum

### 3.1 On Kosullar

- Python 3.12 veya daha yuksek
- Node.js 20 veya daha yuksek (dashboard build icin)
- pip (Python paket yoneticisi)
- npm veya bun (JavaScript paket yoneticisi)
- Git (depo klonlama icin)
- Windows, macOS veya Linux

### 3.2 Depoyu Klonlama

```powershell
git clone https://github.com/Talcawarrior/Junbo.git
cd Junbo
```

### 3.3 Python Bagimliliklari

```powershell
pip install -r requirements.txt
```

### 3.4 Ortam Degiskenleri (.env Dosyasi)

`.env.example` dosyasini `.env` olarak kopyalayin ve duzenleyin:

```powershell
cp .env.example .env
```

Asagidaki ornek bir `.env` dosyasi:

```ini
# === BOT MODU ===
DRY_RUN=true

# === PORTFOLIO ===
INITIAL_PORTFOLIO=10000.0

# === ARALIKLAR (saniye) ===
SCAN_INTERVAL=300
SETTLEMENT_INTERVAL=120
SIA_INTERVAL=86400

# === RISK SINIRLARI ===
MAX_EXPOSURE_PCT=0.25
MAX_BET_PCT=0.03
KELLY_FRACTION=0.15
CITY_CAP=4

# === SUNUCU ===
HOST=127.0.0.1
PORT=8092

# === LLM (ZhipuAI / GLM) ===
ZAI_API_KEY=anahtar_buraya
ZAI_BASE_URL=https://api.z.ai/api/paas/v4/
LLM_MODEL=glm-4.5-flash

# === VERI KAYNAGI ===
RESOLVEDMARKETS_API_KEY=anahtar_buraya

# === CANLI TRADING (DRY_RUN=false ise gerekli) ===
# POLY_PRIVATE_KEY=
# POLY_API_KEY=
# POLY_API_SECRET=
# POLY_API_PASSPHRASE=

# === OPSIONEL: HAVA DURUMU API ===
# WEATHERAPI_KEY=
```

**Onemli notlar:**
- `DRY_RUN=true` varsayilan degerdir. Canli trading icin `false` yapin.
- `INITIAL_PORTFOLIO` baslangic bakiyesini belirler (varsayilan $10,000).
- `MAX_BET_PCT=0.03` her bahis icin max portfoy orani (%3).
- `KELLY_FRACTION=0.15` Kelly kesir carpani (%15).
- `CITY_CAP=4` sehir basina maksimum pozisyon sayisi.
- `PORT=8092` dashboard portu.

### 3.5 Dashboard Build

```powershell
# Next.js bagimliliklarini yukle
npm install

# Dashboard'u build et
npm run build

# Build sonrasi dosyalari kopyala (Windows)
Copy-Item -Path "out\*" -Destination "dashboard\out\" -Recurse -Force

# Linux/Mac
cp -r out/* dashboard/out/
```

### 3.6 Veritabani Olusturma

```powershell
python -c "from database.db import init_db; init_db()"
```

Bu komut `data/bot.db` SQLite dosyasini olusturur.

### 3.7 Ilk Calistirma

```powershell
python main.py bot
```

Bu komut:
1. SQLite veritabanini baslatir
2. FastAPI sunucusunu 8092 portunda baslatir
3. Dashboard'u tarayicida acar: `http://localhost:8092`
4. `scan_and_bet_loop` ve `settlement_loop` arka plan dongulerini baslatir

---

## 4. CLI Komutlari

Tum komutlar `python main.py <komut>` seklinde calistirilir.

| Komut | Aciklama |
|-------|----------|
| `python main.py bot` | Bot + API + Dashboard + arka plan donguleri (hepsi bir arada) |
| `python main.py run` | Sadece API + Dashboard (bot donguleri olmadan) |
| `python main.py fetch` | Tek seferlik piyasa verisi cek |
| `python main.py weather` | Tek seferlik hava durumu tahmini cek |
| `python main.py analyze` | Tek seferlik analiz calistir |
| `python main.py bet` | Tek seferlik bahis yerlestir |
| `python main.py settle` | Tek seferlik karsilama kontrolu yap |
| `python main.py report` | Gunluk konsolide PnL ve ticaret raporu |
| `python main.py reset` | Bot'u sifirla (tum betleri iptal et, portfoy sifirla) |

**Dikkat:** `reset` komutu geri alinamaz. Tum bahis kayitlari, analizler ve sinyaller silinir.

---

## 5. Dashboard Kullanimi

Dashboard, bot calisirken `http://localhost:8092` adresinde erisilebilir.

### 5.1 Dashboard Yapisi

Dashboard Next.js 16 + shadcn/ui + Recharts ile yapilmis modern bir web arayuzudur. Tum veriler FastAPI endpointlerinden WebSocket uzerinden gercek zamanli olarak guncellenir.

### 5.2 Erisilebilir Sayfalar

- **Ana Sayfa (`/`)**: Bot durumu, portfoy degeri, PnL, acik pozisyonlar
- **Swagger (`/docs`)**: API dokumantasyonu ve test araci
- **Health Check (`/api/health-check`)**: Kapsamli saglik kontrolu (edge dagilimi, red flags, 7 gunluk PnL)

### 5.3 Dashboard Ozellikleri

- Bot durum rozeti (RUNNING/STOPPED)
- Portfoy kartlari: Net Sermaye, Acik Bahis sayisi, Acik/Kapali/Toplam PnL, Gunluk ROI
- Aktif Sinyaller tablosu: Giris fiyati, guncel fiyat, edge, PnL, ladder durumu
- Global Market Watch: Piyasadaki tum hava durumu bahisleri
- Gecmis Bahisler: Kazanilan/kaybedilen bahisler, exit tipi (TP/SL/TS/TD/ST)
- Analytics: Portfolio grafigi, kazanma orani
- ASI-Evolve paneli: Model agirliklari, kalibrasyon, cognition base
- Red Flags: Otomatik risk uyarilari

---

## 6. Botun Calisma Dongusu

Bot iki arka plan dongusu ile calisir:

### 6.1 scan_and_bet_loop (Ana Dongu)

Bu dongu surekli calisir ve su adimlari icerir:

1. **Piyasa Verisi Cekme (fetch_markets):** Gamma API'sinden Polymarket'teki hava durumu piyasalarini ceker
2. **Piyasa Analizi (parse_markets):** Cekilen piyasa verilerini isler, sehir/tarih/metrik bilgilerini cikarir
3. **Hava Durumu Tahmini (fetch_weather):** 8 modelden hava durumu tahminlerini toplar
4. **Tek Dongu Islemi (run_cycle):** Tek bir oturumda analiz -> bahis -> fiyat guncelleme -> risk yonetimi islemlerini calistirir

**GeceStratejisi:** Gece 00:00'dan sonra, 2 gun oncedeki piyasalari erken yakalamak icin daha kisa araliklarla tarama yapar (varsayilan: 60 saniye, 60 dakika boyunca).

### 6.2 settlement_loop (Karsilama Dongusu)

Bu dongu surekli calisir ve su islemleri yapar:

1. **Karsilama (settle):** Kapanmis piyasalardaki bahislerin sonucunu kontrol eder
2. **Gunluk DB Temizligi:** Eski tahminleri arsivler, VACUUM yapar
3. **SIA Optimizasyonu:** Her saat basinda model agirliklarini optimize eder

---

## 7. Hesaplama Motoru

### 7.1 Model Ensemble

Bot, 8 farkli kaynaktan hava durumu tahmini alir ve agirlikli olarak birlestirir:

| Model | Varsayilan Agirlik | Kaynak |
|-------|-------------------|--------|
| GFS Seamless | %30 | NOAA (ABD) |
| ECMWF IFS 025 | %25 | ECMWF (Avrupa) |
| GEM Global | %15 | Environment Canada |
| ICON Global | %10 | DWD (Almanya) |
| JMA Seamless | %8 | Japan Meteorological Agency |
| CMA Grapes Global | %5 | China Meteorological Administration |
| UKMO Seamless | %4 | UK Met Office |
| MeteoFrance Seamless | %3 | MeteoFrance |

Her modelin agirligi SIA algoritmasi tarafindan dinamik olarak optimize edilir. Agirliklar `data/strategy_params.json` dosyasinda saklanir.

### 7.2 Olasilik Hesaplama

Bot, sicaklik tahminlerini olasiliga cevirme icin normal dagilim (Gaussian) kullanir:

```
P(sicaklik > strike) = 1 - CDF(strike | model_tahmini, model_std)
```

- `model_tahmini`: Ensemble agirlikli ortalama sicaklik
- `model_std`: Model tahminlerinin standart sapmasi (belirsizlik olceri)
- `CDF`: Kumulatif dagilim fonksiyonu

### 7.3 Edge ve EV Hesaplama

**Edge:** Modelin tahmin ettigi olasilik ile piyasa fiyati arasindaki fark:
```
edge = model_prob - market_price
```

**Net Edge:** Slippage ve ucretler sonrasi kalan edge:
```
net_edge = raw_edge - slippage - fee_drag
```

**Expected Value (EV):** Bahis basina beklenen deger:
```
EV = model_prob * (1/price - 1) - (1 - model_prob)
```

EV > 0 ise bahis karlidir. `min_edge` esigi (varsayilan %5) asilmasi gerekir.

### 7.4 Kelly Katsayisi

Bahis boyutunu belirlemek icin Kelly formulu kullanilir:
```
kelly_fraction = (edge * price - (1 - price)) / (price - 1)
```

Kelly kesri ile bahis tutari:
```
bet_amount = kelly_fraction * kelly_fraction / total_kelly * bankroll
```

`kelly_fraction` negatif ise bahis acilmaz.

### 7.5 Slippage Modeli

Bot, uclu slippage modeli kullanir:

- **Flat:** Sabit slippage yuzdesi (varsayilan %0.5)
- **Tiered:** Fiyat bazli kademeli (0.05 altinda %3, 0.05-0.10 arasi %1, 0.10 ustunde %0.5)
- **Orderbook:** Canli derinlik bazli (gelecek ozellik, tiered'a duser)

---

## 8. Risk Yonetimi

### 8.1 Parametreler

| Parametre | Varsayilan | Aciklama |
|-----------|------------|----------|
| INITIAL_PORTFOLIO | 10000.0 | Baslangic bakiyesi (USD) |
| MAX_EXPOSURE_PCT | 0.25 | Toplam acik pozisyon siniri (portfoyun %25'i) |
| MAX_BET_PCT | 0.03 | Tek bahis icin max bakiye orani (%3) |
| MIN_BET_SIZE | 1.0 | Minimum bahis tutari (USD) |
| KELLY_FRACTION | 0.15 | Kelly kesir carpani (%15) |
| CITY_CAP | 4 | Sehir basina maksimum pozisyon |
| FEE_DRAG | 0.02 | Polymarket taker ucreti (%2) |
| DAILY_LOSS_LIMIT | 0.05 | Gunluk max kayip orani (%5) |

### 8.2 Risk Kontrolleri

Her bahis yerlestirmeden once su kontroller yapilir:
- Max exposure asimi kontrolu
- Tek bahis max limit kontrolu
- Kelly kesri negatif mi kontrolu
- Fiyat sagliligi kontrolu (0.01-0.99 arasi)
- Piyasa turu dogrulugu kontrolu
- Sehir kapasitesi kontrolu (CITY_CAP)
- Gunluk kayip limiti kontrolu
- Edge escalation (kapanis zamanina yakin daha guclu edge gerekli)

### 8.3 Aktif Risk Yonetimi

Acik pozisyonlar icin surekli risk kontrolu:

| Mekanizma | Varsayilan | Aciklama |
|-----------|------------|----------|
| Stop-Loss (SL) | %30 | Fiyat belirli bir seviyeye duserse pozisyon kapatilir |
| Take-Profit (TP) | %100 | Fiyat hedefe ulasirsak pozisyon kapatilir |
| Trailing Stop (TS) | %15 | Fiyat yukseldikce stop seviyesi yukari cekilir |
| Time Decay (TD) | 24 saat | Kapanis tarihi yaklasirken, %10 zarardaysa kapat |
| Model Reversal | - | Model tahmini tersine donerse pozisyon kapatilir |

### 8.4 Ladder Order

Birden fazla fiyat kademesinde kademeli bahis:
- Fiyat dustukce ek bahisler acilir
- Her kademe icin ayri fiyat hedefi belirlenir
- Toplam exposure siniri korunur

### 8.5 Red Flags (Otomatik Uyarilar)

Bot otomatik olarak su durumlari tespit eder ve uyarir:
- Son 48 saatte 7+ kayip (kalibrasyon bozulmus olabilir)
- 24 saatte 0 bahis acilmis (edge threshold cok yuksek olabilir)
- Tum net edge'ler %2.5 altinda (maliyeti karsilamiyor)
- Win rate %50 altinda (model tahminleri guvenilmez)
- 50+ acik pozisyon (risk yonetimi asiliyor)

---

## 9. Veritabani Yapisi

SQLite veritabani `data/bot.db` dosyasinda saklanir. WAL (Write-Ahead Logging) modu aktiftir.

### 9.1 Tablolar

**WeatherMarket:** Piyasa bilgileri
- id, question, city, city_code, metric, threshold, threshold_unit
- threshold_low, threshold_high (RANGE piyasalari icin)
- target_date, latitude, longitude, market_type (HIGH/LOW/RANGE)
- yes_price, no_price, volume, liquidity
- status, first_seen, last_updated, raw_data

**WeatherForecast:** Hava durumu tahminleri
- id, market_id, city, lat, lon, target_date, metric
- source, predicted_value, confidence, model_weight
- fetched_at, raw_data

**Analysis:** Analiz sonuclari
- id, market_id, estimated_probability, market_implied_prob
- edge, raw_edge, slippage_pct
- avg_forecast_value, std_forecast_value, num_sources
- recommended_side, recommended_amount, confidence_score
- should_bet, reason, model_predictions (JSON)
- analyzed_at

**Bet:** Bahis kayitlari
- id, market_id, analysis_id, city_code, city, outcome, stake, stake_amount
- entry_price, shares, current_price, pnl, unrealized_pnl
- fair_value, expected_value, strike_temp, bet_type, side
- realized_pnl, status, ladder_data (JSON), result_data (JSON)
- amount, price, potential_payout, order_id, tx_hash, error_message
- entry_fee, placed_at, settled_at, close_reason, closed_at

**Portfolio:** Portfoy durumu
- id, initial_value, current_value, cash_balance, total_value
- total_realized_pnl, total_won, total_lost, daily_pnl, last_updated

**ModelPerformance:** Model performans metrikleri
- id, model_name, total_predictions, correct_predictions
- accuracy, num_predictions, brier_score, weight
- last_updated, recorded_at

**HistoricalCalibration:** Tarihsel kalibrasyon kayitlari
- id, city_code, city, date, metric, model
- predicted_value, actual_value, bias, created_at

---

## 10. API Endpointleri

Bot `http://localhost:8092` uzerinde FastAPI sunucusu calistirir.

### 10.1 Durum ve Portfoy

| Endpoint | Yontem | Aciklama |
|----------|--------|----------|
| `/api/status` | GET | Bot durumu, portfoy degeri, PnL, acik bahisler |
| `/api/health-check` | GET | Kapsamli saglik kontrolu (edge dagilimi, red flags, 7 gunluk PnL) |
| `/api/equity-curve` | GET | Gunluk equity curve |
| `/api/slippage` | GET | Son slippage verileri |

### 10.2 Piyasalar ve Bahisler

| Endpoint | Yontem | Aciklama |
|----------|--------|----------|
| `/api/markets` | GET | Tum hava piyasalari + tahminler + kacan sinyaller |
| `/api/bets` | GET | Bahis gecmisi (status, limit, offset filtresi) |
| `/api/signals` | GET | Acik pozisyonlar + canli edge takibi |
| `/api/history` | GET | Kapanmis bahislerin W/L/ROI gecmisi |

### 10.3 ASI-Evolve

| Endpoint | Yontem | Aciklama |
|----------|--------|----------|
| `/api/asi/weights` | GET | Guncel model agirliklari + performans metrikleri |
| `/api/asi/cognition` | GET | Cognition Base icgoruleri |
| `/api/asi/calibration` | GET | Sehir bazli bias kalibrasyon haritasi |
| `/api/asi/evolve` | POST | 5 turlu evrim pipeline'i baslat |
| `/api/asi/backfill` | POST | Tarihsel veri backfill (Open-Meteo) |
| `/api/asi/calibration/recalculate` | POST | Kalibrasyon bias'larini yeniden hesapla |
| `/api/asi/autoresearch/run` | POST | AI Scientist arastirma motoru |
| `/api/asi/trades` | GET | On-chain Polymarket trade verisi |
| `/api/asi/orderbook` | GET | CLOB orderbook derinligi |

### 10.4 Kontrol

| Endpoint | Yontem | Aciklama |
|----------|--------|----------|
| `/api/start` | POST | Bot dongulerini baslat |
| `/api/stop` | POST | Bot dongulerini durdur |
| `/api/reset` | POST | Bot'u sifirla (tum betleri iptal et) |
| `/api/cleanup` | POST | Eski analizleri temizle, stale betleri iptal et |
| `/ws` | WebSocket | Guncellemeleri canli olarak al |

---

## 11. Loglama

### 11.1 Dosya Loglama

- **Dosya:** `logs/bot.log`
- **Boyut:** 10 MB'a ulastiginda dondurulur
- **Sayi:** Maksimum 5 dosya (eski dosyalar `bot.log.1`, `bot.log.2` vb. olarak adlandirilir)
- **Seviyeler:** INFO (varsayilan)
- **Kodlama:** UTF-8

### 11.2 Log Formatlari

```
# Konsol
2026-06-30 12:00:00 - SCRAPER_POLYMARKET - INFO - Piyasa cekildi: 45 adet

# Dosya
2026-06-30 12:00:00 - SCRAPER_POLYMARKET - INFO - Piyasa cekildi: 45 adet
```

---

## 12. Testler

Junbo, 304 test dosyasi icerir. Testleri calistirmak icin:

```powershell
# Tum testleri calistir
pytest

# Coverage ile
coverage run -m pytest
coverage report
```

### 12.1 Test Kategorileri

- **Birim testleri:** Kelly, olasilik, piyasa parse, dogrulama, slippage
- **Entegrasyon testleri:** API, veritabani, scrapers
- **Faz testleri:** faz2, faz3, faz4, faz5, faz6 (her faz farkli bir ozelligi test eder)
- **E2E testleri:** Mock ile tam pipeline testi
- **SIA testleri:** SIA optimizasyon dongusu testleri
- **Karpathy testleri:** Grid search testleri
- **Slippage testleri:** Slippage modeli testleri

### 12.2 Kod Kalitesi

```powershell
# Lint kontrolu
ruff check .

# Format
ruff format .

# Tip kontrolu
mypy .

# Pre-commit (otomatik calisir)
pre-commit run --all-files

# Tam pipeline
ruff check . && mypy . && pytest
```

---

## 13. Canli Trading

Canli trading icin `.env` dosyasinda Polymarket API anahtarlari tanimlanmalidir.

### 13.1 Gerekli Anahtarlar

| Anahtar | Aciklama |
|---------|----------|
| POLY_PRIVATE_KEY | Polymarket ozel anahtari |
| POLY_API_KEY | API anahtari |
| POLY_API_SECRET | API sifresi |
| POLY_API_PASSPHRASE | API parola cumlesi |

### 13.2 Canli Modu Aktif Etme

`.env` dosyasinda:
```
DRY_RUN=false
```

### 13.3 Canli Trading Risk Uyarisi

- Canli trading gercek para ile calisir.
- Varsayilan olarak `DRY_RUN=true` (kagit modu) ayarlidir.
- Canli moda gecmeden once kagit modunda test edin.
- `MAX_EXPOSURE_PCT` degerini dusuk tutun (0.25 veya altinda).
- `MAX_BET_PCT` degerini dusuk tutun (0.03 veya altinda).
- `MIN_EDGE` degerini yuksek tutun (minimum %5-10).
- `KELLY_FRACTION` degerini dusuk tutun (%15 veya altinda).
- `CITY_CAP` degerini dusuk tutun (4 veya altinda).

---

## 14. Hata Ayiklama

### 14.1 Yaygin Hatalar ve Cozumleri

**Hata:** `ModuleNotFoundError: No module named 'xxx'`
**Cozum:** Bagimliliklari yukleyin: `pip install -r requirements.txt`

**Hata:** `ConnectionError: Could not connect to Polymarket`
**Cozum:** Internet baglantinizi kontrol edin. Gamma API erisim engeli olabilir.

**Hata:** `DatabaseError: database is locked`
**Cozum:** Diger bir bot instance'i calisiyor olabilir. Tum botlari durdurun.

**Hata:** `KeyError: 'ZAI_API_KEY'`
**Cozum:** `.env` dosyasinda LLM anahtarini tanimlayin.

**Hata:** Dashboard acilmiyor
**Cozum:** `npm run build` ile dashboard'u yeniden build edin. `dashboard/out/` klasorunun dolu oldugundan emin olun.

**Hata:** Port cakismasi
**Cozum:** Bot otomatik olarak port sahibini oldurur. Manuel olarak `netstat -ano | findstr :8092` ile kontrol edin.

### 14.2 Log Kontrolu

Hata durumunda log dosyasini inceleyin:
```powershell
# Son 50 satir
Get-Content logs\bot.log -Tail 50

# Hatalari filtrele
Select-String -Path logs\bot.log -Pattern "ERROR"
```

### 14.3 SAGLIK KONTROLU

Botun sagligini kontrol etmek icin:
```
GET /api/health-check
```

Bu endpoint su bilgileri verir:
- Son 48 saatte acilan bahis sayisi
- Red flag'ler (otomatik risk uyarilari)
- Edge dagilimi (ortalama, min, max)
- Tum zamanlarin kazanma orani
- Exit tipi dagilimi (TP/SL/TS/TD/ST)
- Son 7 gunluk PnL

---

## 15. SSS

**S: Bot birden fazla instance olarak calisabilir mi?**
H: Hayir. SQLite dosyasi ayni anda yalnizca bir instance tarafindan kullanilabilir.

**S: Model agirliklari nasil degistirilir?**
H: Agirliklar `data/strategy_params.json` dosyasinda saklanir. Dosyayi duzenleyebilir veya SIA algoritmasinin otomatik optimize etmesini bekleyebilirsiniz. ASI-Evolve ile de optimize edebilirsiniz.

**S: Yeni sehir nasil eklenir?**
H: `config/settings.py` dosyasindaki `_CITY_ICAO_MAP` sozlugune sehir ve ICAO kodunu ekleyin. `_ICAO_COORDS` sozlugune de koordinatlari ekleyin.

**S: Portfoy nasil sifirlanir?**
H: `python main.py reset` komutunu calistirin veya `POST /api/reset` endpoint'ini kullanin.

**S: Bot arka planda nasil calistirilir?**
H: `python main.py bot` komutunu arka planda baslatabilirsiniz.

**S: Dashboard neden acilmiyor?**
H: Once `npm run build` ile dashboard'u build edin. Sonra `Copy-Item -Path "out\*" -Destination "dashboard\out\" -Recurse -Force` ile dosyalari kopyalayin.

**S: Kalibrasyon nasil calistirilir?**
H: `POST /api/asi/calibration/recalculate` endpoint'ini kullanin. Veya `asi_engine/calibration_engine.py` dosyasini dogrudan calistirin.

**S: ASI-Evolve nasil calistirilir?**
H: `POST /api/asi/evolve` endpoint'ini kullanin. 5 turlu evrim pipeline'i baslatir.

---

## 16. Dosya Ozeti

| Dosya | Satir | Aciklama |
|-------|-------|----------|
| main.py | 229 | CLI giris noktasi, port cakizma cozumu |
| api.py | 1387+ | FastAPI sunucusu, tum API endpointleri, BotState |
| bot_loop.py | 158 | Arka plan donguleri |
| config/settings.py | 580 | Tum ayarlar, sehir haritasi, Config proxy |
| config/logging_config.py | 67 | Loglama yapilandirmasi |
| database/db.py | - | Veritabani baglantisi |
| database/models.py | 266 | Tablo tanimlari (7 tablo) |
| scrapers/polymarket.py | 530 | Polymarket veri cekme |
| scrapers/meteo.py | 411 | Hava durumu veri cekme |
| engine/calculator.py | 665 | Model ensemble, agirlikli olasilik |
| engine/strategy.py | 1241 | Risk yonetimi, bahis motoru, SIA |
| engine/market_parser.py | - | Piyasa cozumleme |
| executor/bet_placer.py | 560 | Bahis yerlestirme |
| executor/settler.py | 354 | Karsilama motoru |
| utils/kelly.py | - | Kelly katsayisi |
| utils/probability.py | - | Olasilik hesaplama |
| utils/accounting.py | - | Nakit hesaplama |
| utils/weights_store.py | - | Agirlik dosya okuma/yazma |
| utils/slippage.py | - | Slippage modeli |
| utils/formulas.py | - | PnL, exposure, fee formulleri |
| jobs/scheduler.py | 392 | Is parcasi yonetimi |
| asi_engine/orchestrator.py | - | ASI-Evolve koordinasyon |
| asi_engine/asi_evolve.py | - | Genetik algoritma |
| asi_engine/calibration_engine.py | - | Kalibrasyon |
| asi_engine/cognition_base.py | - | Icgoru depolama |
| asi_engine/data_backfiller.py | - | Veri doldurma |
| asi_engine/llm_client.py | - | LLM istemcisi |
| asi_engine/llm_loop_orchestrator.py | - | LLM 3-layer loop |
