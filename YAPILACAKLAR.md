# YAPILACAKLAR (2026-08-12) — Backtest Altyapı + Tahmin İyileştirme

> Bu dosya, "Polymarket hava botu" analiz raporundaki önerilerin durumunu izler.
> Karar: açık 173 bet'in settlement'ı bekleniyor (12 Ağu 15:00 TSİ). Kod değişikliği
> settlement sonrası + bu listedeki ölçümler tamamlandıktan sonra yapılacak.

---

## 📌 2026-08-16 YENİ STRATEJİ: 3 EŞİK + PEAK'TE KOMŞU SATIŞI (YARIN TEST)

> **Kullanıcı fikri:** "3'lü eşik açarsak, peak yaklaşırken komşu eşikler de yükselir.
> Gerçek eşik bizim eşiklerimizden biriyse, diğer 2 komşuyu HEMEN satarsak (millet
> uyanmadan) onlardan da para kazanırız."
>
> **Yapıldı (16 Ağu):**
> - `spread_radius` 0 -> 1 (3 eşik: merkez±1, config/.env) — T-2'de 3 eşiğe de düşükten gir
> - `_close_wrong_bucket_bets` (metar_peak.py) — peak kilitlenince kazanan bucket
>   dışındaki TÜM açık betleri (komşular dahil) canlı fiyattan satar
> - `test_spread_placer` radius=1'e güncellendi (3 eşik açılır)
>
> **YARIN DOĞRULANACAK (17 Ağu orderbook verisi toplanınca):**
> - [ ] Komşu eşikler peak öncesi gerçekten yükseliyor mu? (orderbook'ta 13:00 vs peak anı)
> - [ ] Peak kilitlenince bot komşuları henüz çökmeden satabiliyor mu? (pencere genişliği)
> - [ ] Komşu satışından net kar var mı? (entry düşük - satış yüksek mi?)
> - [ ] `backtest_metar_peak.py` / `backtest_kayan_pencere.py` ile 3-eşik + satış simülasyonu

---

## ✅ YAPILDI (ölçüldü, sonuçları kaydedildi)

| # | Madde | Sonuç |
|---|-------|-------|
| 4.1 | Look-ahead doğrulaması | Backtest entry vs gerçek orderbook ask **%87 sapma**. "İlk snapshot" backtest'leri güvenilmez. |
| 4.2 | Ücret/fill muhasebesi | Orderbook backtest'e fee+gaz eklendi; first_ask/median_ask seçeneği var. |
| 4.3 | Çözüm kaynağı | Tüm 4871 market **Weather Underground** ile çözülüyor (raw_data.resolutionSource). Open-Meteo grid ≠ WU istasyonu riski tespit edildi. |
| 4.4 | Config çelişkisi | SPREAD_MAX_ENTRY=0.30 canlıda, README 0.99/0.95 diyor → **tutarsızlık açık**. |
| 5-Ö1 | Ölçüm altyapısı | `scripts/backtest_orderbook.py`: dönem/market/fill/fee/max-drawdown çıktısı eklendi. |
| 5-Ö2 | Walk-forward | Kalibrasyon simülasyonlarında walk-forward kullanıldı (C3, C3c). |
| 5-Ö3 | Net-EV filtre | Test edildi: orderbook'ta **marjinal** (-332 → -325). Kademeli stake **kötü** (-778). |
| 5-Ö4 | Model ağırlıkları | Test edildi: equal/global/city hepsi ~-$353 → **PnL'de farksız**. Global inverse-MAE MAE'yi %7 iyileştiriyor ama PnL'ye dönmüyor. |
| 5-Ö5 | Koşullu spread | Test edildi: std tabanlı radius 2..4 → **-332 → -238** (iyileştiriyor). |
| 6-A | Radius 1/2/3 | Orderbook grid: spread=0 **+$13.77 KAR**, spread=1 **-$111**, spread=3 **-$321** → spread açmak zarar, tek eşik kâr. |
| 6-B | max_entry 0.30/0.50/0.95 | Grid: tek eşikte 0.30 +$12.4, 0.95 +$13.8. |
| 6-C | Eşit/global/şehir ağırlık | Orderbook'ta farksız (-351/-356/-355). |
| 6-F | Stake modelleri | Kademeli EV stake **zararı büyütüyor** (-778). |
| Ek | Kalibrasyon verisi | **205,864 satır / 91 şehir / 177 gün** yüklendi (arşiv parquet'leri). Kalibrasyon MAE'yi %19-20 iyileştiriyor. |
| Ek | Gaussian vs Empirical | Gaussian kalın kuyrukta **yanlış** (%24 fazla büyük hata). Empirical CDF tutma tahminini %48→%53 çıkarıyor. |

---

## 🔴 YAPILMADI / YARIN BAKILACAK (öncelik sırasıyla)

### A. Ölçüm altyapısı eksikleri
- [x] **1. VWAP / konservatif taker fill** — backtest_orderbook.py'ye `--fill vwap` eklendi (ilk 20 snapshot zaman-ağırlıklı ort). Karşılaştırma: first_ask -$5 / median -$18 / vwap -$23 (spread=0). (Rapor 4.1, Deney D)
- [x] **2. Kısmi fill + gecikme + slippage simülasyonu** — `c2_fill_sim.py`: slippage %0/1/5, min_depth 0/50, kısmi fill. Sonuç: slippage kârı azaltıyor (-19.9→-24.2), depth/kısmi fill etkisiz (orderbook derinliği hep yeterli). (Rapor 5-Ö7)
- [x] **3. Eksik tablo/kolon açık hata** — backtest_orderbook.py'ye veri kontrolü eklendi: orderbook_market/eslesen_market/forecast/cozumlenmis/ortusme sayıları + 0 eslesmede HATA, <100'de UYARI. (Rapor 5-Ö1)

### B. Tahmin doğruluğu iyileştirme (settlement sonrası kod kararı)
- [x] **4. Empirical CDF** `utils/probability.py`'ye EKLENDİ (`estimate_probability_empirical`, `empirical_cdf`, max/min ayrı). Ölçüm: tutma %48→%53, Brier 0.266→0.260. Orderbook backtest: Gaussian -$24.7 vs Empirical -$18.4 (KAL, vwap, tek esik).
- [ ] **5. Tek eşik (spread_radius=0)** — orderbook'ta tek kârlı config (ilk koşuda +$13, sonra veri büyüyünce -$5; veri canlı büyüyor). spread_placer'ı merkez eşiğine indir. **Kod kararı settlement sonrası.**
- [x] **6. max/min ayrı modelleme** — ölçüldü: MAX std 1.98, MIN std 1.53 (0.46C fark, ayrı gerekli). Empirical CDF max/min ayrı dağılımlarla kodlandı. (Rapor 5-Ö6)

### C. Risk yönetimi (henüz ölçülmedi)
- [ ] **Korelasyon matrisi** — aynı bölge şehirleri ortak hava sistemlerinden etkileniyor; exposure cap korelasyon-ağırlıklı olmalı. (Rapor 4.5)
- [ ] **Şehir-gün net risk limiti + senaryo bazlı max kayıp** — bet sayısı yerine risk bazlı limit. (Rapor 4.5)

### D. Deney matrisi tamamlanmamış hücreler
- [ ] **Deney A tam**: radius 1/2/3 ayrı ayrı orderbook'ta (spread=0/1/3 grid yapıldı ama 2 eksik).
- [ ] **Deney B tam**: max_entry 0.10/0.20 orderbook grid'ine eklenmedi.
- [ ] **Deney E**: "settlement tutma vs pozitif net-EV erken çıkış" — net-EV filtre test edildi ama erken çıkış (SL) kaldırıldı; karşılaştırma net değil.
- [ ] **Deney G**: "tüm şehirler vs bias/MAE filtreli şehirler" net karşılaştırması yok.
- [ ] Her deneye: profit factor, şehir katkısı, gün bazlı kayıp oranı, ücret/slippage toplamı, out-of-sample CI ekle. (Rapor 6)

---

## 📌 KRİTİK KARARLAR (settlement sonrası)
1. **spread_radius 3 → 0** (tek eşik) — orderbook verisi: tek kârlı config (+$13.77 vs -$321).
2. **Empirical CDF** — Gaussian'ın yerine (kalın kuyruk gerçek dağılım).
3. **spread_max_entry** — 0.30 vs 0.95 çelişkisini çöz (ölçüm: ikisi de tek eşikte kârlı, 0.95 hafif üstün).
4. **Kalibrasyon** — MAE'yi iyileştiriyor ama eşik kazanma tahminini tek başına artırmıyor; Empirical CDF ile birlikte kullanılmalı.

## 🔬 AÇIK BEKLEME
- **173 açık bet** settlement (12 Ağu 15:00 TSİ) — kalibrasyon + fair-value + dead-band + kaydırmasız config'in gerçek testi.
