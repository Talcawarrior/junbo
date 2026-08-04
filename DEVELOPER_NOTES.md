# Geliştirici Notları — Junbo Bot

## ZORUNLU: Her Kod Değişikliği Sonrası

```bash
python quick_check.py          # 7 test, ~78 saniye
python quick_check.py --fast   # sadece lint + import, ~15 saniye
```

Bu testleri **her commit öncesi** ve **bot restart öncesi** çalıştır.
Test geçmezse commit yapma, push etme.

---

## Test Katmanları

| # | Araç | Ne yapar | Dosya |
|---|------|----------|-------|
| 1 | **RUFF** | Undefined names (F821), bare except (E722), unused imports (F401) | quick_check.py |
| 2 | **PYLINT** | Code quality: broad except, reimport, fstring logging | quick_check.py |
| 3 | **MYPY** | Type annotations, Optional hataları | quick_check.py |
| 4 | **CRITICAL** | 26 regression test: timezone, API, scraper, DB, backup, take profit | test_critical_bugs.py |
| 5 | **UNIT+RISK** | 104 test: formül, kelly, risk manager, take profit | 3 test dosyası |
| 6 | **REGRESSION** | 27 test: bilinen bug'ların tekrarlamaması | test_regression.py |
| 7 | **IMPORT** | Tüm kritik modüller import edilebilir | quick_check.py |

---

## Bilinen Kritik Hatalar ve Çözümleri

### B3 — max_bet_pct 10x Fark
- `config/settings.py`: `max_bet_pct = 0.003` (%0.3)
- `utils/kelly.py`: `max_bet_pct = 0.03` (%3)
- **Çözüm**: `kelly.py` artık `bot_config.strategy.max_bet_pct` okuyor

### B4 — Fee Rate Tutarsızlığı
- `config/settings.py`: `fee_drag = 0.02` (ölü kod)
- `utils/slippage.py`: `FEE_PCT = 0.05` (hardcoded)
- `strategy.py`: `current_fee_rate` (dinamik)
- **Çözüm**: strategy.py artık `current_fee_rate` kullanıyor. `slippage.py` de güncellendi.

### B5 — min_edge Çifte Kontrol
- `calculator.py`: `effective_min_edge` (dinamik, time-to-close)
- `strategy.py`: düz `min_edge` (sabit %5)
- **Çözüm**: strategy.py'deki min_edge check kaldırıldı, calculator'a bırakıldı.

### Timezone Crash (bot_loop.py)
- `fast_mode_until` timezone-aware, `now` naive → crash
- **Çözüm**: `fast_mode_until` artık `.replace(tzinfo=None)` yapıyor

### Gamma API Format Değişikliği
- Polymarket `tokens[]` döndürmüyor artık
- **Çözüm**: scraper `outcomePrices` fallback ekledi, `bestBid=0` / `bestAsk=1` atlıyor

### Take Profit Format String
- `{pct:.1%}` 100 ile çarpıyordu (double multiply)
- **Çözüm**: ratio kullanımı, format `{pct:.1%}` artık ratio formatlıyor

---

## DB Koruma Kuralları

1. **Hiçbir test production DB'ye dokunmaz** — `conftest.py` temp DB'ye yönlendirir
2. **Her test öncesi backup** — `conftest.py` `_pre_test_backup()`
3. **Reset öncesi backup** — `api.py` ve `main.py` reset'ten önce backup alır
4. **Bot startup backup** — Her restart'ta `db_backup.py` çalışır
5. **Backup limiti**: MAX_BACKUPS = 10, eski olanlar otomatik temizlenir

```bash
python db_backup.py           # Manuel backup
python db_backup.py --list    # Backup'ları listele
python db_backup.py --restore # Son backup'ı geri yükle
```

---

## Bot Başlatma

```bash
python main.py bot             # Botu başlat
python main.py reset           # Botu sıfırla (backup alır)
```

Port: 8093. API key: `.env` dosyasında `JUNBO_API_KEY`.

### Bot Durumu Kontrol

```bash
# API
curl http://127.0.0.1:8093/api/status

# Log
Get-Content logs\bot.log -Tail 10
```

---

## Branch Kullanımı

| Branch | Amaç |
|--------|------|
| `restore/05-clean-state` | Ana iş akışı (production) |
| `ponytail-audit` | Ponytail audit + CI testleri |
| `feature/partial-tp` | Partial take-profit özelliği |

### Push Kuralı
1. `quick_check.py` 7/7 geçmeden push ETME
2. DB'ye dokunmadan push ETME
3. Yeni branch oluştur, `restore/05-clean-state`'e dokunma

---

## Dosya Yapısı

```
junbo/
├── bot_loop.py           # Scan + settlement loop
├── main.py               # Bot giriş noktası
├── api.py                # FastAPI endpoints
├── quick_check.py        # CI test suite (7 test)
├── db_backup.py          # Backup utility
├── config/settings.py    # Bot config (bot_config singleton)
├── engine/
│   ├── strategy.py       # RiskManager + exit checks
│   ├── calculator.py     # Probability + edge hesaplama
│   └── market_parser.py  # Polymarket parser
├── executor/
│   ├── bet_placer.py     # Bahis açma
│   └── settler.py        # Bahis kapatma/settlement
├── jobs/
│   └── scheduler.py      # run_cycle, risk_management
├── scrapers/
│   ├── polymarket.py     # Gamma API scraper
│   └── meteo.py          # Open-Meteo weather fetch
├── utils/
│   ├── formulas.py       # pnl_ratio, roi_pct, polymarket_fee
│   ├── kelly.py          # Kelly criterion
│   └── slippage.py       # Slippage + fee hesaplama
├── database/
│   ├── db.py             # SQLAlchemy engine + session
│   ├── models.py         # Bet, WeatherMarket, Portfolio, Analysis
│   └── db_cleanup.py     # Parquet archiving
└── tests/
    ├── conftest.py       # DB koruma + backup
    ├── test_critical_bugs.py  # 26 kritik regression test
    ├── test_take_profit_comprehensive.py  # 23 exit test
    ├── test_active_risk_management.py     # 42 risk test
    ├── test_units.py     # 104 unit test
    └── test_regression.py # 27 regression test
```
