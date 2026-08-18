# Backtest Arsivi (2026-08-18 konsolidasyon)

Bu dizin, `scripts/` altinda biriken 20+ ayri backtest script'inin **hepsini**
barindirir. 2026-08-18'de aktif kullanilan 5 backtest `scripts/backtest.py`
tek dosyasinda birlestirildi; geri kalani buraya tasindi.

**OKUYAN BIR SONRAKI OTURUM:** Eger biri "backtest script'i nerede?" diye
sorarsa — once `scripts/backtest.py` (subkomutlu, aktif) ve bu README (arsiv
envanteri) okunur. Eski bir script'in davranisini incelemek gerekirse buradan
okunabilir; AMA canli calistirmak icin `scripts/backtest.py` kullanilir.

**Calistirabilirlik:** Bu arsiv bilincli olarak repo KOKUNDE tutulur (`_REPO_ROOT`
hesabi `dirname(dirname(__file__))` -> buradan dogru cikar). Yani arsivdeki her
script oldugu yerde calisabilir (2026-08-18 dogrulandi: gunluk, orderbook,
metar_peak_realistic, test_metar_vs_settlement PYTHONPATH'siz calisti). Yine de
guncel sonuclar icin `scripts/backtest.py` kullanilir — arsiv yalnizca referans/
geri-dondurme icindir.

## Aktif kullanim (tek komut dosyasi)

```bash
python scripts/backtest.py gunluk --days 2026-08-16,2026-08-17 [--detail]
python scripts/backtest.py orderbook [--spread 3] [--bias-top 15] [--fill first_ask]
python scripts/backtest.py metar_peak [--hours-before 6] [--stake 3.0]
python scripts/backtest.py metar_vs_settlement [--min-day 2026-08-13] [--max-day 2026-08-17]
python scripts/backtest.py walk_forward
```

- `scripts/backtest.py` dokumantasyonu + her subkomutun kaynak kodu: tek dosya.
- Bu arsivdeki 5 "MERGED" dosya, birlestirme ONCESI birebir kopyalardir
  (geri dondurme/yedek amaciyla korunur; calismazlarsa suclama — `backtest.py`
  dogrusudur).

## Envanter

| Dosya | Durum | Ne idi |
|-------|-------|--------|
| `backtest_gunluk.py` | MERGED | Gun gun gercekci backtest (botun 2026-08-18 modu) -> `backtest.py gunluk` |
| `backtest_orderbook.py` | MERGED | Orderbook best_ask ile ham vs kalibreli fair-value -> `backtest.py orderbook` |
| `backtest_metar_peak_realistic.py` | MERGED | METAR-peak gercekci (round(actual) vs clairvoyant) -> `backtest.py metar_peak` |
| `test_metar_vs_settlement.py` | MERGED | METAR bucket vs gercek Polymarket kapanisi dogrulama -> `backtest.py metar_vs_settlement` |
| `walk_forward_backtest.py` | MERGED | Walk-forward (look-ahead'siz) model dogrulama -> `backtest.py walk_forward` |
| `backtest_advanced.py` | OBSOLETE | Cok lu cok metrikli analiz; daha sonra gunluk/orderbook ile gecildi |
| `backtest_bankroll.py` | OBSOLETE | Bankroll yonetimi deneyi; canli kullanilmiyor |
| `backtest_clairvoyant.py` | OBSOLETE | Look-ahead kazanan-bucket backtesti — GERCEK DISI (ROI %286) |
| `backtest_early_spread.py` | OBSOLETE | Erken spread acilis deneyi; erken kapanis KALDIRILDI |
| `backtest_erken_giris.py` | OBSOLETE | Erken giris deneyi; ayni sebeple bitti |
| `backtest_hibrit.py` | OBSOLETE | Hibrit (spread+peak tek dosya) deneme; gunluk.py'de standardize edildi |
| `backtest_hibrit2.py` | OBSOLETE | Hibrit v2 deneme; gecersiz |
| `backtest_hibrit_detay.py` | OBSOLETE | Hibrit detay; gecersiz |
| `backtest_kayan_pencere.py` | OBSOLETE | Kayan pencere backtesti; kayan pencere KAPATILDI |
| `backtest_komsu_satisi.py` | OBSOLETE | Komsu esik satisi (3-esik); radius=0'a donuldu, gecersiz |
| `backtest_metar_peak.py` | OBSOLETE | METAR-peak CLAIRVOYANT versiyonu (%286 ROI — GERCEK DEGIL) |
| `backtest_rolling.py` | OBSOLETE | Rolling pencere eski; rolling_window ile degisti |
| `backtest_rolling_window.py` | OBSOLETE | Rolling pencere; kayan pencere kapandigi icin bitti |
| `backtest_tek_esik.py` | OBSOLETE | Tek esik erken; gunluk.py'de birebir suruyor |
| `backtest_tek_esik_detay.py` | OBSOLETE | Tek esik detay; gecersiz |
| `backtest_true.py` | OBSOLETE | Eski 'true' backtest; orderbook ile degisti |
| `backtest_wu_hourly.py` | OBSOLETE | Weather Underground saatlik karsilastirma; WU API erisilemez |

## Neden 22 script oldu?

Konsolidasyon oncesi her deney (radius, esik sayisi, kayan pencere, komsu
satisi, hibrit, bankroll...) ayri bir `backtest_*.py` olarak scripts/ altina
eklenmis. Bunlarin cogu, kullanici karariyla kapatilan veya yanlis cikan
strateji varyantlarinin izleriydi. Tek aktif model (2026-08-18): SPREAD
(radius=0, bias-top 15) + METAR-PEAK (bias-top 40, RANGE) — `backtest.py gunluk`
bunu birebir modeller.

## Kimse nerede calistirir?

- Backtest: `scripts/backtest.py`
- Veri toplama: `scripts/backfill_price_history.py`, `scripts/backfill_metar_history.py`
- Backtest DB senkron: `scripts/sync_backtest_db.py`
- Calibrasyon icer aktarma: `scripts/import_calibration_parquet.py`
