# Junbo — Polymarket Hava Ticaret Botu

**Kendini Geliştiren Yapay Zeka ile Polymarket Hava Tahmin Piyasalarında Otomatik Alım Satım Botu.**

![Python](https://img.shields.io/badge/python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)
![Next.js](https://img.shields.io/badge/Next.js-16-black)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Özellikler

- **🤖 Tam Otomatik** — Market tarama → hava durumu çekme → analiz → bahis yerleştirme → settlement döngüsü
- **🌤️ 8 Model Ensemble** — GFS, ECMWF, GEM, ICON, JMA, CMA, UKMO, Météo-France — SIA ağırlık optimizasyonu ile
- **🧠 SIA Loop** — Self-Improving Agent, Brier skoruna göre model ağırlıklarını ve strateji parametrelerini otomatik günceller
- **🔬 ASI-Evolve** — Genetik algoritma ile strateji evrimi (virtual backtest + crossover + mutation)
- **📊 Dashboard** — Next.js 16 + shadcn/ui + Recharts ile canlı takip (http://localhost:8092)
- **⚡ Slippage Modeli** — Tiered sipariş defteri simülasyonu (net edge hesaplama)
- **🛡️ Risk Yönetimi** — Kelly fraction, stop-loss, take-profit, trailing stop, city cap, exposure limit
- **🔍 Karpathy Search** — Grid search ile strateji parametre optimizasyonu (min_edge, kelly_fraction, vs.)
- **🧪 LLM 3-Layer Loop** — Z.AI API ile araştırma, analiz ve karar katmanları
- **📈 Canlı API** — FastAPI + WebSocket ile anlık durum, portföy, PnL, edge dağılımı
- **🧹 Pre-commit Pipeline** — Ruff + Mypy ile otomatik kalite kontrol

---

## Mimari

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Junbo Core                                 │
├─────────────┬───────────────┬──────────────┬────────────────────────┤
│  Scrapers   │   Weather     │   Engine     │   Executor             │
│  ┌──────┐   │   ┌────────┐  │  ┌─────────┐ │  ┌──────┐  ┌───────┐  │
│  │Poly  │   │   │Open-   │  │  │Analiz   │ │  │Bet   │  │Settle│  │
│  │Market│───┼──▶│Meteo   │──┼─▶│Kalsülasyon│─┼─▶│Placer│─▶│ment  │  │
│  └──────┘   │   │8 Model │  │  │+ Edge   │ │  └──────┘  └───────┘  │
│             │   └────────┘  │  └─────────┘ │                       │
│  ┌──────┐   │              │  ┌─────────┐ │  ┌──────────────────┐ │
│  │Gamma │   │              │  │SIA Loop │ │  │Risk Manager     │ │
│  │ API  │   │              │  │(Ağırlık │ │  │Kelly/Stop/Expo..│ │
│  └──────┘   │              │  │Optim.)  │ │  └──────────────────┘ │
└─────────────┴───────────────┴──┴─────────┴─┴──────────────────────┘
┌─────────────────────────────────────────────────────────────────────┐
│                       ASI-Evolve                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐  │
│  │Orchestrator  │─▶│Genetik Algo  │─▶│Virtual Backtest + Crossover│ │
│  └──────────────┘  └──────────────┘  └──────────────────────────┘  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐  │
│  │Calibration   │  │Data Backfiller│  │Cognition Base (Insights)│  │
│  └──────────────┘  └──────────────┘  └──────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────────┐
│                       API & Dashboard                                │
│  FastAPI (port 8091) ←── Next.js 16 Static Export                 │
│  /api/status, /api/markets, /api/bets, /api/signals, /api/history │
│  /api/health-check, /api/asi/weights, /api/asi/evolve             │
│  WebSocket /ws ───→ Canlı güncellemeler                            │
└─────────────────────────────────────────────────────────────────────┘
```

### Veri Akışı

1. **Fetch** — Polymarket gamma-api ile açık hava piyasalarını tara
2. **Weather** — Open-Meteo API'den 8 farklı modelin tahminlerini çek
3. **Weight** — SIA ağırlıkları ile weighted ensemble hesapla
4. **Calibrate** — Kalibrasyon düzeltmesi uygula (şehir bazlı bias)
5. **Analyze** — Edge = model_prob - market_price; Kelly büyüklük + slippage
6. **Place** — BetPlacer ile polymarket CLOB'a emir gönder (veya dry-run)
7. **Settle** — Settlement sonrası PnL güncelle, SIA feedback

---

## Hızlı Başlangıç

### Gereksinimler

- Python 3.12+
- Node.js 20+ (dashboard build için)
- Bir Polymarket hesabı ve API anahtarları

### Kurulum

```bash
# Repoyu klonla
git clone https://github.com/Talcawarrior/Junbo.git
cd Junbo

# Python bağımlılıkları
pip install -r requirements.txt

# .env yapılandırması
cp .env.example .env
# .env dosyasını düzenle (API anahtarları, tercihler)

# Veritabanı
python -c "from database.db import init_db; init_db()"
```

### Dashboard Build

```bash
# Root'tan build et
npm run build

# Windows (manual copy — postbuild 'cp' çalışmaz)
Copy-Item -Path "out\*" -Destination "dashboard\out\" -Recurse -Force

# Linux/Mac
cp -r out/* dashboard/out/
```

### Çalıştırma

```bash
# Bot + API + Dashboard + Background loops (hepsi bir arada)
python main.py bot

# Sadece API + Dashboard (bot loop'ları olmadan)
python main.py run

# Tek seferlik operasyonlar
python main.py fetch    # Marketleri tara
python main.py analyze  # Analiz yap
python main.py bet      # Bahis yerleştir
python main.py settle   # Settlement
python main.py report   # Rapor
```

Bot ayağa kalktığında:
- **API**: http://localhost:8091
- **Dashboard**: http://localhost:8091 (Next.js)
- **Swagger**: http://localhost:8091/docs

---

## API Referansı

### Durum ve Portföy

| Endpoint | Açıklama |
|----------|----------|
| `GET /api/status` | Bot durumu, portföy değeri, PnL, açık bahisler |
| `GET /api/health-check` | Kapsamlı sağlık kontrolü (edge dağılımı, red flags, 7 günlük PnL) |

### Piyasalar ve Bahisler

| Endpoint | Açıklama |
|----------|----------|
| `GET /api/markets` | Tüm hava piyasaları + tahminler |
| `GET /api/bets` | Bahis geçmişi (status, limit, offset filtresi) |
| `GET /api/signals` | Açık pozisyonlar + canlı edge takibi |
| `GET /api/history` | Kapanmış bahislerin W/L/ROI geçmişi |
| `GET /api/asi/trades` | On-chain Polymarket trade verisi |

### ASI-Evolve

| Endpoint | Açıklama |
|----------|----------|
| `GET /api/asi/weights` | Güncel model ağırlıkları |
| `GET /api/asi/cognition` | Cognition Base içgörüleri |
| `GET /api/asi/calibration` | Şehir bazlı bias kalibrasyon haritası |
| `POST /api/asi/evolve` | 5 turlu evrim pipeline'ı başlat |
| `POST /api/asi/backfill` | Tarihsel veri backfill (Open-Meteo) |
| `POST /api/asi/calibration/recalculate` | Kalibrasyon bias'larını yeniden hesapla |
| `POST /api/asi/autoresearch/run` | AI Scientist araştırma motoru |

### Kontrol

| Endpoint | Açıklama |
|----------|----------|
| `POST /api/start` | Bot döngülerini başlat |
| `POST /api/stop` | Bot döngülerini durdur |
| `POST /api/reset` | Bot'u sıfırla (tüm bet'leri iptal et) |
| `WS /ws` | WebSocket canlı güncellemeler |

---

## Konfigürasyon

### `.env` Değişkenleri

| Değişken | Varsayılan | Açıklama |
|----------|-----------|----------|
| `DRY_RUN` | `true` | Gerçek emir göndermeden simülasyon |
| `INITIAL_PORTFOLIO` | `1000.0` | Başlangıç portföy değeri ($) |
| `SCAN_INTERVAL` | `300` | Market tarama aralığı (saniye) |
| `SETTLEMENT_INTERVAL` | `120` | Settlement kontrol aralığı (saniye) |
| `SIA_INTERVAL` | `86400` | SIA optimizasyon aralığı (saniye) |
| `MAX_EXPOSURE_PCT` | `0.25` | Maksimum exposure oranı |
| `MAX_BET_PCT` | `0.003` | Maksimum bet büyüklüğü (portföy %) |
| `KELLY_FRACTION` | `0.15` | Fractional Kelly katsayısı |
| `CITY_CAP` | `4` | Şehir başına maksimum pozisyon |
| `HOST` | `127.0.0.1` | Sunucu adresi |
| `PORT` | `8091` | API portu |
| `FEE_DRAG` | `0.02` | Polymarket taker fee (%2) |

### LLM Yapılandırması

```env
ZAI_API_KEY=anahtar                          # Z.AI API anahtarı
ZAI_BASE_URL=https://api.z.ai/api/paas/v4/   # API base URL
LLM_MODEL=glm-4.5-flash                       # Model adı
```

### Strateji Parametreleri

Parametreler `data/strategy_params.json` üzerinden yönetilir — Karpathy Search veya SIA Loop tarafından otomatik güncellenir:

| Parametre | Varsayılan | Açıklama |
|-----------|-----------|----------|
| `min_edge` | 5% | Minimum edge eşiği |
| `kelly_fraction` | 15% | Fractional Kelly |
| `min_entry_price` | 0.35 | Minimum giriş fiyatı |
| `inefficiency_min` | -0.124 | Minimum verimsizlik eşiği |

---

## Modeller

SIA Loop tarafından optimize edilen 8 hava modeli:

| Model | Varsayılan Ağırlık | Kaynak |
|-------|-------------------|--------|
| GFS Seamless | %35 | NOAA |
| ECMWF IFS 0.25 | %35 | ECMWF |
| GEM Global | %5 | Environment Canada |
| ICON Global | %5 | DWD (Almanya) |
| JMA Seamless | %5 | Japan Meteorological Agency |
| CMA Grapes Global | %5 | China Meteorological Administration |
| UKMO Seamless | %4 | UK Met Office |
| Météo-France Seamless | %3 | Météo-France |

---

## CLI Komutları

```bash
# Ana komutlar
python main.py bot          # Bot + API + Dashboard + background loops
python main.py run          # Sadece API + Dashboard

# Tek seferlik işlemler
python main.py fetch        # Marketleri tara
python main.py weather      # Hava durumunu çek
python main.py analyze      # Analiz yap
python main.py bet          # Bahis yerleştir
python main.py settle       # Settlement
python main.py report       # Rapor
```

---

## Geliştirme

### Kalite Araçları

```bash
# Lint
ruff check .

# Format
ruff format .

# Type check
mypy .

# Tüm testler
pytest

# Coverage
coverage run -m pytest
coverage report

# Pre-commit (otomatik çalışır)
pre-commit run --all-files

# Full pipeline
ruff check . && mypy . && pytest
```

### Test Yapısı

```
tests/
├── test_accounting.py               # Portföy muhasebe testleri
├── test_active_risk_management.py    # Risk yönetimi testleri
├── test_calculator.py                # Hava durumu hesaplama
├── test_calculator_min_edge.py       # Edge eşiği testleri
├── test_calculator_real.py           # Gerçek veri ile hesap
├── test_config_consistency.py        # Config tutarlılık
├── test_faz2_e2e_mock.py .. 6.py     # End-to-end mock testleri
├── test_karpathy_weekly.py           # Karpathy search testi
├── test_sia_hourly.py                # SIA Loop testleri
├── test_slippage.py                  # Slippage modeli
└── test_weights_store.py             # Ağırlık depolama
```

---

## Proje Yapısı

```
Junbo/
├── asi_engine/          # ASI-Evolve: calibration, cognition, evolving
├── config/              # Settings, logging
├── dashboard/           # Next.js 16 dashboard (shadcn/ui + Recharts)
├── data/                # Runtime veri (weights, params, backtest)
├── data_pipeline/       # PolyMarket veri çekme + işleme
├── database/            # SQLAlchemy ORM (Bet, Portfolio, Analysis..)
├── engine/              # Core: calculator, strategy, risk manager
├── executor/            # BetPlacer, Settlement
├── jobs/                # Zamanlanmış görevler (scheduler)
├── scrapers/            # Polymarket, Open-Meteo API clients
├── scripts/             # Diagnostic/utility script'ler
├── tests/               # 304 test
├── utils/               # Kelly, slippage, probability, accounting
├── main.py              # Bot + API + CLI giriş noktası
├── .pre-commit-config.yaml  # Pre-commit hooks
├── mypy.ini             # Mypy yapılandırması
└── pyrightconfig.json   # Pyright yapılandırması
```

---

## Lisans

MIT
