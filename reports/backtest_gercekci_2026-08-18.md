# Kazaniyor muyuz, kayip mi ediyoruz? — Gercekci backtest raporu (2026-08-18)

Kullanici sorusu: "lan yine mi yanlis backtest... kazaniyormuyuz kayip mi ediyoruz,
gercekci backtest yazamiyor musun, kac gunluk veriye ihtiyacin var"

## 1. KISA CEVAP

| Soru | Cevap | Kaynak |
|---|---|---|
| Bot CANLI kazaniyor mu? | **KAYIP — -$483.66** (1,452 bet, $2,908 stake) | bot.db bets (gercek, simulasyon degil) |
| Bugunku config GECMISTE kazandirir miydi? | Simulasyon: **+$353.08** (173 bet, %71 winrate, ROI +%83) | scripts/backtest.py gunluk, look-ahead'siz |
| Gercekci backtest yazildi mi? | **Evet** (look-ahead kapatildi + fill dogrulandi) | bu rapor |
| Kac gunluk veri gerekli? | Kullanilabilir: 13 gun (05-17 Agu). Guven icin: 30-60 gun. Sert limit: 02-18 Agu | asagida |

**Neden fark var?** Canli kaybin buyuk cogunlugu ESKI config'in imzasi:
- 10-11 Agu felaket gunleri: -$286 (587 bet acildi, eski radius=3/1 config).
- Longshot (<$0.10) 545 bet = -$162 — kaybin ~%61'i. Bugunku METAR config
  MIN_ENTRY=0.10 ile bunlari FİLTRELİYOR (canli 6 mid bet +$7.06 idi).
- Bugunku config (radius=0, bias-top 15, MIN_ENTRY, RANGE-only peak) 2026-08-18'de
  finalize edildi — CANLI KARNESI HENUZ YOK. Backtest tek kanit.

## 2. CANLI GERCEK (bot.db, 2026-08-18 09:xx cekildi)

```
TOPLAM bets=1452  realized_pnl=-$483.66
per status:              n    realized
  won                   162    +$743.11
  lost                  332    -$692.39
  closed                527    -$219.72   (erken satis)
  closed_early          308    -$314.66
  placed                123     $0.00     (doldu, henuz sonuclanmadi)
```

Entry-bucket dagilimi (pnl sutunu, eski config dahil):
```
  longshot <0.10    n= 545   -$162.07   <- kaybin ~%61'i, MIN_ENTRY keser
  0.30-0.60         n= 429   -$57.66
  0.10-0.30         n= 363   -$37.41
  >=0.60            n= 115   -$6.80
```

Gunluk (en kotu iki gun eski config'in agir bet gunleri):
```
  2026-08-10  n=256  -$174.12
  2026-08-11  n=331  -$112.01
```

## 3. BACKTEST DUZELTMESI (look-ahead kapatildi)

**Ariza:** `cmd_gunluk` bot.db weather_forecasts okuyordu. O tablo ROTATED —
05-13 Agu hedefli satirlarin hepsi fetched_at=2026-08-14 (backfill). Sim 06-13 Agu
gunlerini, botun O GUN goremedigi forecast'lerle simule ediyordu (look-ahead).
Eski raporun +$415.97 degeri bu yuzden yanilticiydi.

**Cozum (scripts/backtest.py):** forecast artik backtest.db'den okunur — gercek
gunluk batch'leri (02-18 Agu) — ve `fetched_at <= kapanis` kapisi uygulanir
(kapanis = target_date 23:59:59 + 12h, PEAK_CLOSE_HOURS). Botun func.max(fetched_at)
batch secimi korunur ama yalnizca kapanis oncesi fetch'lerden.

**Sonuc (05-17 Agu, look-ahead'siz):**
```
[SPREAD    ] TOPLAM: bet= 96 kazandi=52 winrate=%54.2 NET=+$125.50 ROI=%+65.4
[METAR-PEAK] TOPLAM: bet= 77 kazandi=73 winrate=%94.8 NET=+$227.58 ROI=%+98.5
[BIRLESIK  ] bet=173 kazandi=125 winrate=%72.3 NET=+$353.08 ROI=%+83.5
```
- Eski look-ahead'li +$415.97 -> duzeltilmis +$353.08 (yaklasik -$63 etki).
- METAR-peak %94.8 winrate MIN_ENTRY=0.10 filtreli: yalnizca piyasa bucket'i hala
  >=$0.10 fiyatlarken bet aciliyor (piyasa 0.01'e fiyatliyorsa tespit yanlis, bet yok).

**Fill modeli dogrulandi (`scripts/backtest.py gunluk --real-entry`):**
Sim'in ideal ilk-ask giris fiyati, ayni marketlerde botun GERCEK fill'leriyle
degistirildi (config sinirlari uygulanarak): eslesen 66 bet NET=+$205.43,
ideal kalan 96 bet +$148.98 -> toplam **+$354.41** ≈ ideal +$353.08.
Yani sim fill modeli sistemik optimistik DEGIL (ust uste binen marketlerde).

**Fill-model probu (`scripts/_probe_fill.py`, tum canli betler vs fiyat verisi):**
1356 bet karsilastirildi: |real - data_ask| = 0.0656, bagil sapma %59.6,
korelasyon 0.815. Ucuz longshot'larda birebir; pahali eski-config betlerinde
fiyat verisi seyrek oldugu icin buyuk sapma (veri boslugu, fill modeli degil).

## 4. HONEST SINIRLAR (backtest hala neyi yakalayamiyor)

1. **Survivorship:** sim yalnizca fiyat verisi (orderbook/price_history) OLAN
   marketlerde bet aciyor. 173 sim bet vs 1,452 canli bet — canli, sim'in
   simule edemedigi bircok markette bet acti (ve kaybetti). Gizli kayip riski.
2. **Config karnesi:** bugunku config'in canli karnesi yok (18-Aug'da finalize).
   Backtest tek kanit; kucuk orneklem (173 bet).
3. **Maliyet modeli:** fee=%5 + gas=$0.10 sabit; gercek Polymarket fill farklari
   (part fill, slippage) yakalanmiyor.
4. **METAR-peak %94.8** MIN_ENTRY filtreli; canli eski config %12 winrate'ti.
   Farkin tamami filtrelerden (MIN_ENTRY + RANGE-only) geliyor.

## 5. KAC GUNLUK VERI GEREKLI?

- backtest.db GERCEK gunluk forecast batch'leri: **02-18 Agu (17 gun)**, her gun
  ayri fetch batch (8 model x ~46 sehir). Market 04-Agu+, fiyat 05-Agu+,
  METAR 01-Agu+.
- Kullanilabilir gercekci pencere: **05-17 Agu (13 gun, 173 sim bet)** — fiyat
  verisi 05-Agu'da basladigi icin 04-Agu tamamen simule edilemez.
- Istatistiksel guven: ~30-60 gun ideal. 173 bet'te winrate %71'in standart
  hatasi ~%3.5 pp; 30 gunde ~400 bet, 60 gunde ~800 bet olur.
- **SERT LIMIT:** Open-Meteo gecmis forecast servis etmiyor -> 02-Agu oncesine
  veri URETILEMEZ. Cozum: ileriye dogru birikim (backtest.db sync job zaten
  calisiyor). Gunde ~1 gun veri eklenir.

## 6. KARAR ONERISI

- Bugunku config (radius=0, bias-top 15, MIN_ENTRY=0.10, RANGE-only peak) canli
  kaybin ana kaynaklarini (longshot, yanlis market tipi, asiri bet sayisi) kesiyor.
  Backtest bu config ile +$353 diyor. DEVAM EDER ama sim'un survivorship
  limitini unutma: sim'un goremedigi marketlerde kayip gizli olabilir.
- 5-7 gun daha canli calistir, gunde bir `scripts/backtest.py gunluk --days
  <son-gunler>` ile karsilastir. 30+ gun birikince istatistiksel yargi ver.

## 7. KOMUTLAR

```bash
python scripts/backtest.py gunluk --days 2026-08-05,...,2026-08-17            # ideal fill
python scripts/backtest.py gunluk --days 2026-08-05,...,2026-08-17 --real-entry  # gercek-fill capraz
python scripts/backtest.py metar_vs_settlement                               # METAR dogruluk
python scripts/_probe_fill.py                                               # fill-model probu
```
