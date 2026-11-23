# METAR-peak erken giris: ACIK vs KAPALI (replay)

Uretim: 2026-11-23 03:11 | pencere 2026-10-23 .. 2026-11-22 (UTC gun) | tick 10 dk | stake = canli METAR_STAKE_USD

Motor: `python -m replay.main --strategy metar --metar-mode hybrid --settle polymarket [--no-early] [--early-max-price X] [--include-non-metar]`

## Veri kapsami ve sinirlar

- METAR arsivi (bot.db `metar_observations`) **28 Eki - 6 Kas arasi bos** (bot kapaliydi); 7 Kas ve 16 Kas kismi. Fiyat (market_snapshots) ayni aralikta yok. Bu gunlerde ne canli ne replay bet var.
- Settlement: Polymarket resmi sonucu (`raw_data`) > canli betin won/lost'u > arsiv METAR max (yedek). Kaynak sayilari asagida.
- Yedek dogrulamasi: arsiv METAR max bucket'i Polymarket YES marketiyle 762/859 sehir-gunde ayni (%88.7); C-bucket'li (ABD disi) 632/662 (%95.5).
- Arsiv gecmisi SPECI gozlemlerini de icerir (Polymarket kurali onlari saymaz) — iki senaryo da ayni veriyi gorur, kiyas adildir.
- Giris: DB fiyat vekili (snapshot / orderbook orta, hangisi tazeyse) + taze CLOB ask dolum; aktar satisi CLOB bid (yoksa snapshot). Ekim'de orderbook yok -> yalnizca 30 dk snapshot.
- Arsiv yukleme 25 sn.

## Ozet ve yorum

- **Yon kesin, buyukluk belirsiz.** Erken girisi kapatmak her iki fiyat varsayiminda da daha iyi:
  tutucu (bilgi-sonrasi fiyat) +$146.91, iyimser (bilgi-oncesi fiyat, look-ahead) +$165.97 fark.
  Canli veri de ayni yonde: giris <=0.50 betler -$44.66, >0.50 betler +$11.84.
- **Mekanizma:** erken betler zirveden ONCE acilir; sicaklik 1-3 derece daha yukselince aktar
  zinciri ucuz betleri ~0.01 bid'den kapatir (tutucu senaryoda 66 aktar, -$129.91). Ayrica gunluk
  15'lik cap'i erken doldurup sonraki kilit betlerini engeller.
- **Erken KAPALI'nin kazanci kucuk ve az bet:** tutucu senaryoda 37 bet, +$14.67 (gunde ~2 bet).
  Kilitten sonra kazanan bucket cogu zaman 0.95 ustu fiyatlanir ve bet acilmaz. Bu, "zarari durdurur
  ama buyuk kar uretmez" demektir.
- **Sadakat:** canli ayna (D) -$118 vs gercek canli -$33. Ayni markete giren betlerde giris farki
  medyan 0.000, zaman farki +1 dk (motor dogru); fark cap tahsisinden (hangi sehir cap'i doldurur) ve
  canli botun 11-14 Kas kesinti gunlerinden gelir. Mutlak PnL yerine senaryo farkina bakilmali.
- **2026-11-17 kiyas disi:** restart sonrasi bayat fiyat olayi (Londra/Paris/Amsterdam/Munih 0.30-0.52
  gorunurken gercek ask 1.00) replay'e sahte ucuz kazanclar yazdiriyordu.

## Sonuc (2026-11-17 HARIC - kirli fiyat gunu)

| Senaryo | Bet | Settle kazanan | Aktar kapanis | Aktar PnL | Isabet | Net PnL | ROI |
|---|---|---|---|---|---|---|---|
| Erken giris ACIK (canli kural, HK haric) | 197 | 47/131 | 66 | $-129.91 | %24 | **$-132.24** | %-33.6 |
| Erken giris KAPALI (yalnizca 1-dusus kilidi) | 37 | 29/30 | 7 | $-12.21 | %78 | **$+14.67** | %+19.8 |
| Peak saatini bekle + HER ZAMAN kilit (erken_max=0) | 35 | 27/29 | 6 | $-10.19 | %77 | **$+14.38** | %+20.5 |
| Canli ayna: erken ACIK + HK dahil (canliyla kiyas) | 197 | 44/131 | 66 | $-102.93 | %22 | **$-118.01** | %-30.0 |
| DUYARLILIK erken ACIK, bilgi-oncesi fiyat (look-ahead) | 244 | 94/110 | 134 | $-107.88 | %39 | **$+225.88** | %+46.3 |
| DUYARLILIK erken KAPALI, bilgi-oncesi fiyat (look-ahead) | 137 | 125/128 | 9 | $-11.76 | %91 | **$+391.85** | %+143.0 |
| CANLI bot (gercek, HK dahil) | 195 | 71/135 | 59 | $-102.92 | - | **$-32.82** | - |
| CANLI bot, HK haric | 185 | - | - | - | - | **$-24.65** | - |
| CANLI giris<=0.50 / >0.50 | 155 / 40 | - | - | - | - | **$-44.66 / $+11.84** | - |

## Sonuc (tum pencere, kirli gun DAHIL - yalnizca seffaflik icin)

| Senaryo | Bet | Settle kazanan | Aktar kapanis | Aktar PnL | Isabet | Net PnL | ROI |
|---|---|---|---|---|---|---|---|
| Erken giris ACIK (canli kural, HK haric) | 212 | 59/146 | 66 | $-129.91 | %28 | **$-79.24** | %-18.7 |
| Erken giris KAPALI (yalnizca 1-dusus kilidi) | 48 | 40/41 | 7 | $-12.21 | %83 | **$+76.46** | %+79.6 |
| Peak saatini bekle + HER ZAMAN kilit (erken_max=0) | 44 | 36/38 | 6 | $-10.19 | %82 | **$+60.99** | %+69.3 |
| Canli ayna: erken ACIK + HK dahil (canliyla kiyas) | 212 | 56/146 | 66 | $-102.93 | %26 | **$-65.01** | %-15.3 |
| DUYARLILIK erken ACIK, bilgi-oncesi fiyat (look-ahead) | 259 | 106/125 | 134 | $-107.88 | %41 | **$+278.87** | %+53.8 |
| DUYARLILIK erken KAPALI, bilgi-oncesi fiyat (look-ahead) | 149 | 136/140 | 9 | $-11.76 | %91 | **$+451.55** | %+151.5 |
| CANLI bot (gercek, HK dahil) | 201 | 77/141 | 59 | $-102.92 | - | **$-15.43** | - |
| CANLI bot, HK haric | 191 | - | - | - | - | **$-7.26** | - |
| CANLI giris<=0.50 / >0.50 | 159 / 42 | - | - | - | - | **$-30.72 / $+15.29** | - |

_Asagidaki tablolar 2026-11-17 HARIC._

## Donem bazinda

| Senaryo | 23-27 Eki bet | 7-22 Kas bet | 23-27 Eki PnL | 7-22 Kas PnL |
|---|---|---|---|---|
| Erken giris ACIK (canli kural, HK haric) | 66 | 131 | $-20.08 | $-112.16 |
| Erken giris KAPALI (yalnizca 1-dusus kilidi) | 14 | 23 | $-0.59 | $+15.27 |
| Peak saatini bekle + HER ZAMAN kilit (erken_max=0) | 13 | 22 | $-0.73 | $+15.11 |
| Canli ayna: erken ACIK + HK dahil (canliyla kiyas) | 66 | 131 | $-16.99 | $-101.02 |
| DUYARLILIK erken ACIK, bilgi-oncesi fiyat (look-ahead) | 75 | 169 | $+110.13 | $+115.75 |
| DUYARLILIK erken KAPALI, bilgi-oncesi fiyat (look-ahead) | 68 | 69 | $+199.86 | $+191.99 |
| CANLI bot (HK dahil) | 64 | 131 | $-18.78 | $-14.04 |

## Tetik ve giris fiyati kirilimi

**Erken giris ACIK (canli kural, HK haric)**

| Grup | Bet | Settle kazanan | Aktar | PnL |
|---|---|---|---|---|
| giris<=0.50 | 164 | 28/107 | 57 | $-114.93 |
| giris>0.50 | 33 | 19/24 | 9 | $-17.31 |
| tetik=early | 160 | 28/104 | 56 | $-120.50 |
| tetik=early+aktar | 21 | 6/13 | 8 | $-11.53 |
| tetik=lock | 13 | 10/11 | 2 | $-2.97 |
| tetik=lock+aktar | 3 | 3/3 | 0 | $+2.75 |

**Erken giris KAPALI (yalnizca 1-dusus kilidi)**

| Grup | Bet | Settle kazanan | Aktar | PnL |
|---|---|---|---|---|
| giris<=0.50 | 5 | 2/3 | 2 | $+13.73 |
| giris>0.50 | 32 | 27/27 | 5 | $+0.94 |
| tetik=lock | 34 | 26/27 | 7 | $-6.04 |
| tetik=lock+aktar | 3 | 3/3 | 0 | $+20.71 |

**Peak saatini bekle + HER ZAMAN kilit (erken_max=0)**

| Grup | Bet | Settle kazanan | Aktar | PnL |
|---|---|---|---|---|
| giris<=0.50 | 5 | 2/4 | 1 | $+13.68 |
| giris>0.50 | 30 | 25/25 | 5 | $+0.70 |
| tetik=lock | 32 | 24/26 | 6 | $-6.33 |
| tetik=lock+aktar | 3 | 3/3 | 0 | $+20.71 |

## Settlement kaynagi

| Senaryo | polymarket | canli_bet | metar_arsiv | aktar | sonuc yok (PnL disi) |
|---|---|---|---|---|---|
| Erken giris ACIK (canli kural, HK haric) | 128 | 0 | 3 | 66 | 0 |
| Erken giris KAPALI (yalnizca 1-dusus kilidi) | 29 | 0 | 1 | 7 | 0 |
| Peak saatini bekle + HER ZAMAN kilit (erken_max=0) | 28 | 0 | 1 | 6 | 0 |
| Canli ayna: erken ACIK + HK dahil (canliyla kiyas) | 127 | 1 | 3 | 66 | 0 |
| DUYARLILIK erken ACIK, bilgi-oncesi fiyat (look-ahead) | 109 | 0 | 1 | 134 | 0 |
| DUYARLILIK erken KAPALI, bilgi-oncesi fiyat (look-ahead) | 122 | 0 | 6 | 9 | 0 |

## Sadakat (D canli ayna vs gercek canli betler)

- Ayni market'e giris: 133 market (replay 197 bet, canli 195 bet)
- Siki eslesme (ayni market + |zaman| <= 15 dk + |giris| <= 0.05): 54 | precision 0.27 | recall 0.28

## Gunluk PnL

| Gun | A_erken_acik | B_erken_kapali | C_saat_bekle_kilit | D_canli_ayna | A_iyimser | B_iyimser | CANLI |
|---|---|---|---|---|---|---|---|
| 2026-10-23 | 15 / $-8.30 | 5 / $-0.77 | 5 / $-0.77 | 15 / $-8.30 | 15 / $+18.30 | 15 / $+33.46 | 15 / $-5.23 |
| 2026-10-24 | 15 / $+3.88 | 5 / $-1.54 | 4 / $-1.68 | 15 / $+3.88 | 15 / $-4.05 | 15 / $+27.07 | 15 / $-0.97 |
| 2026-10-25 | 15 / $-18.62 | 2 / $+1.44 | 2 / $+1.44 | 15 / $-10.73 | 15 / $+40.75 | 15 / $+90.43 | 15 / $-16.73 |
| 2026-10-26 | 6 / $-1.44 | 2 / $+0.27 | 2 / $+0.27 | 6 / $-1.44 | 15 / $+11.88 | 15 / $+23.79 | 9 / $+2.97 |
| 2026-10-27 | 15 / $+4.39 | 0 / $+0.00 | 0 / $+0.00 | 15 / $-0.40 | 15 / $+43.24 | 8 / $+25.11 | 10 / $+1.18 |
| 2026-11-08 | 1 / $-1.79 | 0 / $+0.00 | 0 / $+0.00 | 1 / $-1.79 | 1 / $-0.10 | 0 / $+0.00 | 0 / $+0.00 |
| 2026-11-09 | 2 / $+6.02 | 1 / $+0.26 | 1 / $+0.26 | 2 / $+6.02 | 15 / $+62.42 | 15 / $+69.99 | 14 / $+49.83 |
| 2026-11-10 | 15 / $-19.67 | 4 / $+0.06 | 4 / $+0.06 | 15 / $-23.11 | 15 / $+4.63 | 11 / $+5.01 | 12 / $+35.84 |
| 2026-11-11 | 2 / $-4.18 | 0 / $+0.00 | 0 / $+0.00 | 2 / $-4.18 | 3 / $-5.98 | 1 / $-2.09 | 1 / $+2.52 |
| 2026-11-12 | 15 / $+15.30 | 0 / $+0.00 | 0 / $+0.00 | 15 / $+4.67 | 15 / $+28.14 | 1 / $+20.13 | 15 / $-11.70 |
| 2026-11-13 | 4 / $-3.88 | 0 / $+0.00 | 0 / $+0.00 | 4 / $-3.88 | 15 / $+9.36 | 12 / $+31.82 | 4 / $+4.01 |
| 2026-11-14 | 2 / $-0.73 | 0 / $+0.00 | 0 / $+0.00 | 2 / $-0.73 | 15 / $+57.71 | 12 / $+59.67 | 2 / $+4.27 |
| 2026-11-15 | 15 / $-8.87 | 2 / $+0.27 | 1 / $+0.17 | 15 / $-8.85 | 15 / $+4.55 | 2 / $+0.27 | 15 / $-13.22 |
| 2026-11-18 | 15 / $-19.83 | 4 / $+17.72 | 4 / $+17.72 | 15 / $-20.00 | 15 / $+1.16 | 4 / $+9.41 | 9 / $-14.40 |
| 2026-11-19 | 15 / $-18.78 | 4 / $-1.10 | 4 / $-1.16 | 15 / $-18.78 | 15 / $-6.44 | 3 / $-1.52 | 16 / $-24.76 |
| 2026-11-20 | 15 / $-19.15 | 2 / $+0.28 | 2 / $+0.28 | 15 / $-19.15 | 15 / $-13.72 | 2 / $+0.29 | 13 / $-13.40 |
| 2026-11-21 | 15 / $-23.93 | 3 / $-0.87 | 3 / $-0.87 | 15 / $-6.56 | 15 / $-20.03 | 3 / $-1.25 | 15 / $-17.63 |
| 2026-11-22 | 15 / $-12.68 | 3 / $-1.36 | 3 / $-1.36 | 15 / $-4.67 | 15 / $-5.96 | 3 / $+0.27 | 15 / $-15.39 |

Senaryo sureleri: A_erken_acik 7 sn, B_erken_kapali 3 sn, C_saat_bekle_kilit 3 sn, D_canli_ayna 3 sn, A_iyimser 3 sn, B_iyimser 3 sn
