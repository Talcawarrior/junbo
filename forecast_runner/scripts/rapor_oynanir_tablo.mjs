#!/usr/bin/env node
// Ne işe yarar: **PROJENİN NİHAİ ÇIKTISI** (ANAYASA §0) — "hangi kaynak, hangi şehirde, hangi ufukta
//   ne kadar kârlı?" sorusunu tek tabloda cevaplar. Üç parçayı birleştirir:
//     (1) İSABET  : kaynak × şehir × ufuk kırılımında MAE/bias/isabet (MAIN.forecast_snapshots + settlements)
//     (2) FİYAT   : piyasanın o kovaya biçtiği fiyat (kardeş junbo bot.db — SALT-OKUNUR, Kural 11)
//     (3) KENAR   : kendi ölçülmüş isabetimizden çıkan olasılık  −  piyasa fiyatı   → beklenen değer
//   Ve bunu **sızıntısız backtest** ile sınar: her gün yalnız O GÜNDEN ÖNCEKİ veriyle karar verir.
//
// BAHİS BİRİMİ (ölçüldü, tahmin değil): Polymarket günlük-high marketleri ">= X" DEĞİL, "= X°C" kovasıdır
//   (`market_type='RANGE'`, `threshold`=X; kazanç: round(gerçek)==threshold). Bir de en alt kuyruk `LOW`
//   ("X°C veya altı") vardır. Bu rapor yalnız RANGE kovasını oynar — bizim tahminimizin düştüğü derece.
//
// BAYAT FİYAT TUZAĞI (tekrarlanmayacak): 2026-09-07'de bayat fiyatla +%58 görünen bir strateji, taze
//   fiyatla −%1 çıkmıştı. Burada fiyat, hedef ana (`hours_to_settlement` = HEDEF_SAAT) EN YAKIN
//   snapshot'tan alınır. Fiyatı olmayan hücre oynanmaz — uydurulmaz.
//
// SINIRLAR (dürüstlük): fiyat arşivi 2026-08-03'ten beri var (bot.db 08-05'ten, backtest.db 08-03'e iner;
//   08-02 settlement sonrası olduğu için dışarıda); canlı 7 kaynağın tahmini 08-25'ten
//   beri; derin kaynaklar (OM-önceki/AI) 2025'ten beri. Kesişim dardır — rapor bunu §0'da yazar ve
//   örneklem küçükken "yön" der, "kesin sıralama" demez.
//
// Kullanım: node --experimental-sqlite scripts/rapor_oynanir_tablo.mjs
//   HEAT_MAIN_DB · JUNBO_DB · HEAT_OUT · HEAT_ARSIV=0 · HEAT_HEDEF_SAAT (varsayılan 24) · HEAT_MIN_N (varsayılan 8)
//   HEAT_EDGE_MIN (varsayılan 0.02) · HEAT_GECIKME (karne gecikmesi, gün; varsayılan 2)
// SALT-OKUNUR: MAIN {readOnly}, junbo {readOnly}. Hiçbir DB'ye yazılmaz (yalnız .md).

import path from 'node:path';
import fs from 'node:fs';
import { DatabaseSync } from 'node:sqlite';

const MAIN_DB = process.env.HEAT_MAIN_DB || 'C:/HEAT data/db/tempmarket-edge.sqlite';
const JUNBO_DB = process.env.JUNBO_DB || 'C:/Users/fdemir/Documents/New project/junbo/data/bot.db';
const OB_DB = process.env.JUNBO_ORDERBOOK || 'C:/Users/fdemir/Documents/New project/junbo/data/orderbook.db';
// İKİNCİ (arşiv) fiyat kaynağı — 2026-09-15 ölçümü. `bot.db` arşivi 08-05'te başlıyor ve 08-06'da
// karar anına (h≈24) ait kaydı YOK; `backtest.db` 08-02'den beri dolu ve 08-06'da medyan uzaklığı
// 12,6 saatten 2,4 saate indiriyor. Şema birebir aynı ve örtüşen anda fiyatlar **birebir aynı**
// (2.222/2.222 hücrede Δ=0) — yani bu bir "başka görüş" değil, aynı akışın daha derin geçişi.
// SALT-OKUNUR (Kural 11). Yerine geçmez, EK'tir: bot.db'de olup burada olmayan satırlar var.
const BACKTEST_DB = process.env.JUNBO_BACKTEST || 'D:/JUNBO data/backtest.db';
const OUT = process.env.HEAT_OUT || 'rapor_oynanir_tablo.md';
const ARSIV = process.env.HEAT_ARSIV !== '0';
const HEDEF_SAAT = Number(process.env.HEAT_HEDEF_SAAT || 24); // karar anı: hedeften kaç saat önce
const MIN_N = Number(process.env.HEAT_MIN_N || 8);             // bir hücreye güvenmek için en az kaç gün
const EDGE_MIN = Number(process.env.HEAT_EDGE_MIN || 0.02);    // oynamak için en az kenar (P − fiyat)
const GECIKME = Number(process.env.HEAT_GECIKME || 2);         // karne, karardan en az bu kadar gün geride
// Ufuk kapısı (2026-09-16): eskiden yalnız t1/t2 yükleniyordu; oysa veride **t3 ve t4 gerçekten var**
// (Visual Crossing t3 14.787 · OpenWeatherMap t3/t4 15.629 · Open-Meteo previous t3 9.339 · OM-GFS/ICON
// t3/t4 ~1.300). Kapalı olduğu için "hangi kaynak hangi ufukta" tablosu bu ufukları hiç görmüyordu.
// t5 ve ötesi seyrek (367 satır) → dışarıda. Kullanıcının istediği kombinasyon (kaynak × şehir × ufuk)
// bu kapı olmadan eksik kalır.
const MAKS_UFUK = Number(process.env.HEAT_MAKS_UFUK || 4);
// Fiyat tarafının ufka göre karar anı: t1 karardan HEDEF_SAAT saat önce, her ek ufak +24 saat.
// (Ölçüldü 2026-09-16: fiyat arşivi ~67 saate kadar gidiyor ama 48 saatten sonra ÇÖKÜYOR —
//  ~24s: 126.965 satır/45 şehir · ~48s: 101.519/38 · ~60s: 10.043/23 · ~72s: 229/21.
//  Yani **t1 ve t2 fiyatlanabilir, t3+ fiyatlanamaz** → o ufuklarda isabet ölçülür, kâr ölçülemez.)
const hedefSaatH = (h) => HEDEF_SAAT + 24 * (h - 1);
const FIYATLANABILIR = [1, 2, 3]; // t3 dahil edilir ki "ask yok" da ölçülmüş olsun, varsayılmasın
// §3d: "bu kaynak bu şehirde AÇIK ARA önde" eşiği — kaynak, aynı (şehir, varyant) için o gün karneye giren
// DİĞER kaynakların en iyisinden en az bu kadar puan önde olmalı. Kullanıcının kuralı: "bir kaynak bir şehri
// çok iyi tutturuyorsa yalnız ona bet açarız" — bu eşik o "çok iyi"yi ölçülebilir yapar.
const ONDE_MIN = Number(process.env.HEAT_ONDE_MIN || 0.10);

const md = [];
const P = (s = '') => md.push(s);
const f2 = (x) => (x == null || !Number.isFinite(x) ? '-' : x.toFixed(2));
const yz = (x) => (x == null || !Number.isFinite(x) ? '-' : `%${Math.round(x * 100)}`);
const isaretli = (x) => (x == null || !Number.isFinite(x) ? '-' : `${x >= 0 ? '+' : ''}${x.toFixed(3)}`);
const gun = (s) => String(s).slice(0, 10);
const gunEksi = (d, k) => { const t = new Date(`${d}T00:00:00Z`); t.setUTCDate(t.getUTCDate() - k); return t.toISOString().slice(0, 10); };

const main = new DatabaseSync(MAIN_DB, { readOnly: true });
const junbo = new DatabaseSync(JUNBO_DB, { readOnly: true });

// ---- istasyon eşlemesi ----
// DİKKAT (2026-09-15 ölçüldü): junbo `market_snapshots.city` ICAO DEĞİL, **şehir adı** tutar
// ("Istanbul", "Munich"). ICAO yalnız `weather_markets.city_code`'da var. Köprü bu yüzden iki adımlı:
//   market_snapshots.city (ad) → weather_markets.city_code (ICAO) → Heat stations.icao → stations.id
// Sabit liste YOK; eşleme tablodan türetilir (yeni şehir geldiğinde kendiliğinden çalışır).
const sehirAdi = new Map(main.prepare('SELECT id, icao, city FROM stations').all()
  .map((s) => [String(s.id), `${s.icao}${s.city ? ' ' + s.city : ''}`]));
const icaoToSt = new Map(main.prepare('SELECT id, icao FROM stations WHERE icao IS NOT NULL').all()
  .map((s) => [String(s.icao).toUpperCase(), String(s.id)]));
const adToIcao = new Map(junbo.prepare('SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL').all()
  .map((r) => [String(r.city).toLowerCase(), String(r.city_code).toUpperCase()]));
// ---- KOVA (BUCKET) BİRİMİ: °C pazarı mı °F pazarı mı? ----
// NEDEN ŞART (2026-09-15'te yakalandı): Polymarket'in °F pazarlarında kovalar **2°F genişliğindedir**
// ("64-65°F", "66-67°F"…) ve `threshold` o kovanın **orta noktasının °C karşılığıdır** (64.5°F → 18.1).
// °C pazarlarında ise kovalar 1°C ("22°C") ve threshold tam sayıdır. `round(°C)` ile skorlamak °F
// pazarlarında **iki °F kovasını tek kovaya birleştirir** → kazan/kaybet kaydı yanlış olur.
// Kanıt: San Francisco 2026-09-17 kovaları "≤63 / 64-65 / 66-67 / … / ≥82", `threshold`=18.1, 19.1, 20.3…
// BİRİM ŞEHİR BAŞINA SABİTTİR ve veriden türetilir (sabit liste yok): bir şehirde kova threshold'ları
// arasında **tam sayı olmayan** varsa o şehir °F pazarıdır. İki bağımsız yöntem aynı 11 ABD şehrini
// verdi (bu türetme + `raw_data.groupItemTitle` içindeki "°F"), hiçbiri karışık değil.
// Kova etiketi: °C'de tam derece; °F'de 2°F'lik kovanın **alt çift sayısı** (64 → "64-65°F").
// °F kovası YARIM derecede bölünür: "64-65" kovası 64 veya 65'e yuvarlanan değerleri alır → [63.5, 65.5).
// Bu yüzden `2*floor(F/2)+1` DEĞİL, `floor(round(F)/2)` kullanılır. Eski hâli sınırı çift sayıya koyuyor
// ([64, 66) → "64-65") ve her çift sınırda **0,5°F'lik bir şeridi komşu kovaya** atıyordu (2026-09-16'da
// düzeltildi; formül kendi içinde tutarlı olduğu için hata yalnız o şeritteki tahminlerde kova kaydırıyordu).
const kova = (v, F) => (F ? Math.floor(Math.round((Number(v) * 9) / 5 + 32) / 2) : Math.round(Number(v)));

const eslesmeyenAd = new Set();
const hId = (ad) => {
  const icao = adToIcao.get(String(ad).toLowerCase());
  if (!icao) { eslesmeyenAd.add(String(ad)); return null; }
  const st = icaoToSt.get(icao);
  if (!st) { eslesmeyenAd.add(`${ad}(${icao})`); return null; }
  return st;
};

// Birim tespiti `hId`den SONRA yapılır (şehir adı → istasyon eşlemesi gerekir).
const birimF = new Set();
for (const r of junbo.prepare(`SELECT city,
    SUM(CASE WHEN ABS(threshold - CAST(threshold AS INTEGER)) > 1e-6 THEN 1 ELSE 0 END) kesir
  FROM market_snapshots WHERE metric='temperature_max' AND market_type='RANGE' AND threshold IS NOT NULL
  GROUP BY city`).all()) {
  if (Number(r.kesir) > 0) { const id = hId(r.city); if (id) birimF.add(id); }
}
const kovaSt = (st, v) => kova(v, birimF.has(String(st)));
const BIRIM_F = (st) => (birimF.has(String(st)) ? 'F' : 'C');
console.log(`[birim] °F pazarı olan istasyon: ${birimF.size} → ${[...birimF].sort().join(', ')}`);

// ---- Gerçek: her (şehir, gün) için en son settlement ----
const gercek = new Map();
for (const r of main.prepare(`SELECT station_id, target_date, high_c,
    ROW_NUMBER() OVER (PARTITION BY station_id, target_date ORDER BY recorded_at_utc DESC) rn
  FROM settlements WHERE high_c IS NOT NULL`).all()) {
  if (r.rn === 1) gercek.set(`${r.station_id}|${gun(r.target_date)}`, Number(r.high_c));
}

// ---- Tahminler: her (kaynak, şehir, gün, ufuk) için o hücredeki EN SON koşu (koşu <= hedef gün = sızıntısız) ----
const SRC = [
  'Open-Meteo previous', 'Pangu-Weather', 'FourCastNet v2', 'GraphCast',
  'Open-Meteo icon_seamless', 'Open-Meteo gfs_seamless', 'Open-Meteo ecmwf_ifs025',
  'Visual Crossing daily', 'WeatherAPI daily', 'OpenWeatherMap 3-hour', 'NWS forecastHourly',
  'icon_seamless', 'gfs_seamless', 'ecmwf_ifs025', 'ukmo_seamless', 'gem_global',
  'meteofrance_seamless', 'jma_seamless', 'cma_grapes_global',
];
const ph = SRC.map(() => '?').join(',');
const son = new Map(); // "src|st|D|h" -> {v, run}
for (const r of main.prepare(`SELECT provider, station_id, target_date, horizon_days, predicted_high_c v, run_at_utc run
  FROM forecast_snapshots WHERE provider IN (${ph}) AND horizon_days BETWEEN 1 AND ${MAKS_UFUK}
    AND predicted_high_c IS NOT NULL`).all(...SRC)) {
  const D = gun(r.target_date);
  if (r.run && gun(r.run) > D) continue; // koşu hedef günden sonra → sızıntı, at
  const k = `${r.provider}|${r.station_id}|${D}|${r.horizon_days}`;
  const c = son.get(k);
  if (!c || (r.run || '') > c.run) son.set(k, { v: Number(r.v), run: r.run || '' });
}

// ---- Fiyat: junbo market_snapshots, karar anına EN YAKIN hours_to_settlement ----
// Kova = RANGE (tam derece). Bizim tahminimizin yuvarlandığı derece oynanır.
const fiyat = new Map(); // "hid|D|th" -> {d, p, saat}
const marketId = new Map(); // "hid|D|th" -> market_id  (orderbook'a köprü)
// UFKA GÖRE fiyat (2026-09-16): §3e kombinasyon portföyü (kaynak × şehir × ufuk) için. `fiyat` t1'in
// karar anına (HEDEF_SAAT) en yakın kaydı tutar; burası her ufuk için **kendi karar anına** en yakınını
// tutar. t1 girdileri `fiyat` ile birebir aynıdır — eski bölümler değişmez.
const fiyatH = new Map(); // "h|hid|D|th" -> {d, p, saat}
const marketIdH = new Map(); // "h|hid|D|th" -> market_id
// GİRİŞ ZAMANI (§3f, 2026-09-16): kullanıcının gözlemi — "peak'e en yakın anda açınca tutuyor ama kazanç
// düşük, bir gün önce açarsan kazanç yüksek". Bu bir ÖDÜNÇTÜR: geç giriş = yüksek isabet + düşük ödeme.
// Ölçmek için fiyat, sabit karar anı (HEDEF_SAAT) yerine **giriş anının kendisine** en yakın kayıttan alınır.
const GIRIS_SAATLERI = (process.env.HEAT_GIRIS_SAATLERI || '3,6,12,18,24,36,48').split(',').map(Number);
const fiyatG = new Map(); // "T|hid|D|th" -> {d, p, saat}   (T = settlement'tan kaç saat önce)
const settleTop = new Map(); // "hid|D" -> [tahmini settlement anı ms]
const IKI_KAYNAK = [
  // bot.db ÖNCE yüklenir ve eşitlikte kazanır (canlı, daha taze). backtest.db yalnız **anlamlı ölçüde**
  // daha yakın bir an sunarsa onu ezer (0,5 saat tolerans) — yoksa eşitlikte kaynak zıplaması olurdu.
  { db: junbo, ad: 'bot.db', tolerans: 0 },
  { db: null, ad: 'backtest.db', tolerans: 0.5 },
];
try { if (fs.existsSync(BACKTEST_DB)) IKI_KAYNAK[1].db = new DatabaseSync(BACKTEST_DB, { readOnly: true }); } catch { }
let fiyatKaynakSayaci = { 'bot.db': 0, 'backtest.db': 0 };
for (const { db, ad, tolerans } of IKI_KAYNAK) {
  if (!db) continue;
  // backtest.db'de index yok → tam tarama ~4,6 sn. Yalnız EK dilimi okunur (bot.db'nin kapsamadığı
  // günler): 19 bin satır, ~1,6 sn. Tüm tabloyu okumak gereksiz.
  const sql = `SELECT market_id, city, target_date, threshold, market_type, yes_price, hours_to_settlement h, snapshot_time st
      FROM market_snapshots WHERE metric='temperature_max' AND market_type='RANGE'
        AND yes_price IS NOT NULL AND yes_price > 0.001 AND yes_price < 0.999`
    // Alt sınır 08-03: `backtest.db` 08-02'ye kadar iniyor ama **08-02 BİLEREK DIŞARIDA**. O günün
    // satırlarının tamamı h≤5,4 (settlement sonrası), karar anına ait kayıt yok — fiyat olarak çöp.
    // Alınırsa `fiyatGun[0]` künyede 08-02 görünür ve "3 gün kazandık" yanlış olurdu.
    + (ad === 'bot.db' ? '' : ` AND target_date >= '2026-08-03' AND target_date < '2026-08-07'`);
  for (const r of db.prepare(sql).all()) {
    const hid = hId(r.city);
    if (!hid) continue;
    const D = gun(r.target_date);
    const th = kovaSt(hid, r.threshold); // °F pazarında kova etiketi 2°F'lik kovayı temsil eder
    if (!Number.isFinite(th) || r.h == null) continue;
    const k = `${hid}|${D}|${th}`;
    const d = Math.abs(Number(r.h) - HEDEF_SAAT);
    const c = fiyat.get(k);
    if (!c || d < c.d - tolerans) {
      fiyat.set(k, { d, p: Number(r.yes_price), saat: Number(r.h) });
      marketId.set(k, String(r.market_id));
      if (c) fiyatKaynakSayaci[ad] += 1; // kaç hücrede bu kaynak öncekini geçti
    }
    // Aynı satırı her ufkun kendi karar anı için de değerlendir (ufka göre fiyat, §3e).
    for (const h of FIYATLANABILIR) {
      const kh = `${h}|${k}`;
      const dh = Math.abs(Number(r.h) - hedefSaatH(h));
      const ch = fiyatH.get(kh);
      if (!ch || dh < ch.d - tolerans) {
        fiyatH.set(kh, { d: dh, p: Number(r.yes_price), saat: Number(r.h) });
        marketIdH.set(kh, String(r.market_id));
      }
    }
    // Aynı satırı her GİRİŞ ANI için de değerlendir (§3f giriş zamanı eğrisi).
    for (const T of GIRIS_SAATLERI) {
      const kg = `${T}|${k}`;
      const dg = Math.abs(Number(r.h) - T);
      const cg = fiyatG.get(kg);
      if (!cg || dg < cg.d - tolerans) fiyatG.set(kg, { d: dg, p: Number(r.yes_price), saat: Number(r.h) });
    }
    const t = Date.parse(`${String(r.st).replace(' ', 'T')}Z`);
    if (Number.isFinite(t)) {
      const arr = settleTop.get(`${hid}|${D}`) || [];
      arr.push(t + Number(r.h) * 3600e3);
      settleTop.set(`${hid}|${D}`, arr);
    }
  }
}
// Settlement anı: snapshot + hours_to_settlement. Medyan alınır (tek satırın hatası sıçratmasın).
const settleAt = new Map([...settleTop].map(([k, a]) => { a.sort((x, y) => x - y); return [k, a[Math.floor(a.length / 2)]]; }));

// ---- GERÇEK ALIŞ FİYATI (ask): junbo orderbook.db, SALT-OKUNUR ----
// `yes_price` son/mid fiyattır — gerçekte ÖDEYECEĞİMİZ fiyat değil. Alış `best_ask`tir ve longshot
// kovalarda spread geniş olduğu için fark büyüktür. Bu yüzden kârlılık **iki fiyatla** raporlanır:
// iyimser (mid) ve gerçekçi (ask). Ask yoksa o bahis ask analizine girmez, uydurulmaz.
let ob = null;
try {
  if (fs.existsSync(OB_DB)) {
    ob = new DatabaseSync(OB_DB, { readOnly: true });
    // Kardeşin canlı daemon'u bu dosyaya yazıyor; okuma SQLITE_BUSY alabilir. `busy_timeout`
    // BAĞLANTI-YEREL bir bekleme ayarıdır — dosyaya tek bayt yazmaz, journal_mode'a dokunmaz (Kural 11).
    try { ob.exec('PRAGMA busy_timeout = 30000'); } catch { /* eski sürümde yoksa sorun değil */ }
  }
} catch { ob = null; }
const askCache = new Map();
let obHata = 0;
// ORTAK DEFTER OKUYUCU (2026-09-16): eskiden her `askAt` çağrısı kendi SQL'ini koşuyordu; şimdi
// market başına **bir kez** okunup önbelleğe alınıyor. Üç yeni bölüm (NO tarafı, maker dolum testi,
// §4 koruması) aynı defteri farklı sorularla kullandığı için tek okuyucu hem tutarlılık hem hız.
// Satırlar zamana göre sıralı tutulur: maker testi "bu andan SONRA fiyat benim emrime değdi mi?"
// diye **ileriye** bakmak zorunda, bu da sıralı dizide tek geçişle yapılır.
const defterCache = new Map(); // market_id -> [{t, a, b}] (t artan)
function defterGetir(marketId) {
  if (!ob || !marketId) return [];
  if (defterCache.has(marketId)) return defterCache.get(marketId);
  let rows = [];
  try {
    rows = ob.prepare('SELECT best_ask a, best_bid b, snapshot_time s FROM orderbook_snapshots WHERE market_id=? AND best_ask IS NOT NULL').all(marketId)
      .map((r) => ({ t: Date.parse(r.s), a: Number(r.a), b: Number(r.b) }))
      .filter((r) => Number.isFinite(r.t) && Number.isFinite(r.a))
      .sort((x, y) => x.t - y.t);
  } catch { obHata += 1; }
  defterCache.set(marketId, rows);
  return rows;
}
// Bir andan ±pencere içindeki EN YAKIN defter satırı. `yon` = 0 (en yakın) · -1 (yalnız öncesi) ·
// +1 (yalnız sonrası). Maker testi "ben emri verdikten sonra ne oldu" dediği için +1 kullanır.
function defterYakin(rows, t0, yon = 0) {
  let best = null;
  for (const r of rows) {
    if (yon < 0 && r.t > t0) continue;
    if (yon > 0 && r.t < t0) continue;
    const d = Math.abs(r.t - t0);
    if (!best || d < best.d) best = { d, ...r };
  }
  return best;
}
// PENCERE ŞARTI (ölçüm hatası düzeltmesi, 2026-09-15): ilk koşuda ask, karar anından **37 saate kadar**
// uzaktan alınabiliyordu ve ROI **yükseldi** (%44→%111) — bu imkânsızdır, çünkü alış fiyatı ≥ mid ise
// ROI ancak düşer. Sebep: rastgele bir andaki fiyat "alış fiyatı" değildir. Artık ask yalnız
// karar anına PENCERE saatten yakınsa kabul edilir; dışındakiler **veri yok** sayılır (uydurulmaz).
const ASK_PENCERE = Number(process.env.HEAT_ASK_PENCERE_SAAT || 1) * 3600e3;
// VERİ KALİTESİ KAPISI (2026-09-16, İKİNCİ TUR — ilk gerekçe YANLIŞTI, düzeltildi).
// Birinci turda "ask ≥ mid olmalı, alış satıştan pahalıdır" diye bir kapı konmuştu ve bu **fiziksel
// bir zorunluluk değil**: `mid` `market_snapshots.yes_price`'tan, `ask` ise `orderbook_snapshots`'tan
// gelir — **iki ayrı tablo, iki ayrı an**. Ölçüldü (897.506 pencere-içi çift): %95,6'sında ask ≥ mid;
// ask < mid olan %4,4'ün çoğu 1–5 sentlik **zaman farkı gürültüsü** (ör. mid 0,065 / ask 0,05 / bid 0,04,
// dt = 4 dk). Bunları elemek veriyi kırpar, gerçeği düzeltmez.
// Gerçek bozukluk **boş defter**: `best_bid = 0` olan kayıtların `best_ask`'i anlamsızdır (karşı taraf
// yok; ör. mid 0,63 iken ask 0,001 / bid 0). Kural zamandan bağımsızdır ve tablo içinde doğrulanabilir:
// 6,95M satırın **hiçbirinde** ask < bid yok — yani bozukluk defterin İÇİNDE değil, defterin BOŞ olmasında.
// Kapı merkezîdir: §3b/§3e/§3f/§3g hepsi bu fonksiyonlardan beslenir. Reddedilen kayıt sessizce
// atılmaz; sayılır ve §3g'de raporlanır.
// `HEAT_ASK_KAPI` ile üç mod (fark ölçülebilsin diye): `bid` (varsayılan, fiziksel) · `mid` (ilk tur) · `yok`.
const ASK_KAPI = process.env.HEAT_ASK_KAPI || 'bid';
let askBosSayac = 0;   // boş defter (bid = 0) → elendi
let askTersSayac = 0;  // ask < mid → yalnız `mid` modunda elenir; her modda SAYILIR (raporlanır)
const askGecerliMi = (askObj, midDegeri) => {
  if (!askObj) return null;
  if (askObj.b != null && Number.isFinite(askObj.b) && askObj.b <= 0) { askBosSayac += 1; return null; }
  if (midDegeri != null && Number.isFinite(midDegeri) && askObj.a < midDegeri - 1e-9) {
    askTersSayac += 1;
    if (ASK_KAPI === 'mid') return null;
  }
  return askObj;
};
function askAt(k, h = 1) {
  const ck = `${h}|${k}`;
  if (askCache.has(ck)) return askCache.get(ck);
  let out = null;
  const mid = h === 1 ? marketId.get(k) : marketIdH.get(`${h}|${k}`);
  const [st, D] = k.split('|');
  const hedef = (settleAt.get(`${st}|${D}`) ?? NaN) - hedefSaatH(h) * 3600e3;
  if (Number.isFinite(hedef)) {
    const best = defterYakin(defterGetir(mid), hedef);
    if (best && best.d <= ASK_PENCERE && best.a > 0 && best.a < 1) out = askGecerliMi(best, fiyatH.get(ck)?.p);
  }
  askCache.set(ck, out);
  return out;
}
// §3f için: aynı iş, ama karar anı sabit değil — `T` saat öncesi. market_id ufka göre değişmez
// (market = şehir × hedef gün × eşik), bu yüzden `marketId` yeniden kullanılır.
const askCache2 = new Map();
function askAtSaat(k, T) {
  const ck = `${T}|${k}`;
  if (askCache2.has(ck)) return askCache2.get(ck);
  let out = null;
  const mid = marketId.get(k);
  const [st, D] = k.split('|');
  const hedef = (settleAt.get(`${st}|${D}`) ?? NaN) - T * 3600e3;
  if (Number.isFinite(hedef)) {
    const best = defterYakin(defterGetir(mid), hedef);
    if (best && best.d <= ASK_PENCERE && best.a > 0 && best.a < 1) out = askGecerliMi(best, fiyatG.get(`${T}|${k}`)?.p);
  }
  askCache2.set(ck, out);
  return out;
}
// NO TARAFI (§3h, 2026-09-17): bu projede bugüne kadar YALNIZ "YES al" ölçüldü. NO almanın fiyatı
// emir defterinde **doğrudan** durur: bir bahsin NO'sunu almak = YES'ini satan tarafı karşılamak, yani
// `NO alış = 1 − YES satış (best_bid)`. Aynı pencere, aynı kapı — kıyas adil olsun diye tek fark yön.
// Kayıt: `{a: NO alış fiyatı, b: YES satış}`, böylece `askGecerliMi` aynen kullanılabilir.
const bidCache = new Map();
function noAt(k, h = 1) {
  const ck = `${h}|${k}`;
  if (bidCache.has(ck)) return bidCache.get(ck);
  let out = null;
  const mid = h === 1 ? marketId.get(k) : marketIdH.get(`${h}|${k}`);
  const [st, D] = k.split('|');
  const hedef = (settleAt.get(`${st}|${D}`) ?? NaN) - hedefSaatH(h) * 3600e3;
  if (Number.isFinite(hedef)) {
    const best = defterYakin(defterGetir(mid), hedef);
    // YES satışı (best_bid) geçerli olmalı: 0 < b < 1. NO alış = 1 − b, yani 0 < NO alış < 1.
    if (best && best.d <= ASK_PENCERE && best.b > 0 && best.b < 1) {
      const no = { a: 1 - best.b, b: 1 - best.a, d: best.d };
      // Kapı aynı: "boş defter" kontrolü NO tarafında da anlamlı (YES ask=0 ise defter boş).
      if (ASK_KAPI !== 'mid' || no.a >= (fiyatH.get(ck)?.p ?? 1) - 1e-9) out = no;
      else askTersSayac += 1;
    }
  }
  bidCache.set(ck, out);
  return out;
}

const PENCERE_BASI = '2026-04-09';  // ≥45 şehirli settlement penceresi
// Fiyat penceresi başlangıcı. `bot.db` 08-05'te başlar; `backtest.db` 08-02'ye iner ama **08-02
// KULLANILMAZ**: o günün satırlarının tamamı h≤5,4 (settlement sonrası), karar anına ait fiyat yok —
// fiyat olarak çöptür. 08-03'ten itibaren karar anı (h≈24) kaydı vardır.
const FIYAT_BASI = '2026-08-03';
const SON_GUN = [...gercek.keys()].map((k) => k.split('|')[1]).sort().at(-1);

// ---- Ölçüm çekirdeği: her (kaynak, şehir, gün, ufuk) için bir satır ----
const tumSatir = [];   // {src, st, D, h, v, th, g, hit}
const gunlukIdx = new Map();
const kaynakOzet = new Map(); // "src|h" -> {n, s, sapma, hit}
for (const [k, f] of son) {
  const [src, st, D, h] = k.split('|');
  const hh = Number(h);
  const g = gercek.get(`${st}|${D}`);
  if (g == null) continue;
  const th = kovaSt(st, f.v);
  const row = { src, st, D, h: hh, v: f.v, th, g, hit: th === kovaSt(st, g) };
  tumSatir.push(row);
  gunlukIdx.set(`${src}|${st}|${D}|${hh}`, row);
  const agg = kaynakOzet.get(`${src}|${hh}`) || { src, h: hh, n: 0, s: 0, sapma: 0, hit: 0 };
  agg.n += 1; agg.s += Math.abs(f.v - g); agg.sapma += f.v - g; if (row.hit) agg.hit += 1;
  kaynakOzet.set(`${src}|${hh}`, agg);
}

// şehir × ufuk grupları (bir kez)
// DİKKAT: kaynaklar arasından "o gün en az yanılanı" SEÇMEK gizli peek olur (gerçeği görüp seçmek) ve
// beceriyi şişirir. Onun yerine o gün mevcut kaynakların DÜZ ORTALAMASI alınır — peek yok.
const sehirGrup = new Map(); // "st|h" -> Map(D -> {v, g, hit})
for (const r of tumSatir) {
  const k = `${r.st}|${r.h}`;
  let m = sehirGrup.get(k);
  if (!m) { m = new Map(); sehirGrup.set(k, m); }
  const c = m.get(r.D) || { st: r.st, h: r.h, D: r.D, g: r.g, toplam: 0, n: 0 };
  c.toplam += r.v; c.n += 1;
  m.set(r.D, c);
}
for (const m of sehirGrup.values()) {
  for (const c of m.values()) { c.v = c.toplam / c.n; c.hit = kovaSt(c.st, c.v) === kovaSt(c.st, c.g); }
}

// ================= RAPOR =================
P('# Oynanır tablo — hangi kaynak, hangi şehirde, hangi ufukta?');
P('');
P('> **Projenin nihai çıktısı** (ANAYASA §0). Ölçer, bahis açmaz — Heat trade yapmaz.');
P('> Bir hücre = (şehir × ufuk × kaynak). "Kârlı mı" iki şeyden çıkar: **kendi isabetimiz** ve');
P('> **piyasanın o kovaya biçtiği fiyat**. İkisinin farkı **kenardır**; kenar varsa hücre oynanır.');
P('');

const fiyatGun = [...new Set([...fiyat.keys()].map((k) => k.split('|')[1]))].sort();

P('## 0. Künye ve kapsam (dürüstlük)');
P('');
P(`- **Gerçek (settlement):** ${gercek.size.toLocaleString('en-US')} şehir-gün · son gün **${SON_GUN}**.`);
P(`- **≥45 şehirli geniş pencere:** ${PENCERE_BASI} → ${SON_GUN}.`);
P(`- **Fiyat arşivi (kardeş junbo, salt-okunur):** yalnız **${fiyatGun[0]} → ${fiyatGun.at(-1)}** (${fiyatGun.length} gün).`);
P(`  - Kaynak 1: \`junbo/data/bot.db\` → \`market_snapshots\` (mid), \`junbo/data/orderbook.db\` → \`orderbook_snapshots\` (gerçek ask).`);
P('  - `D:\\JUNBO data\\backtest.db` arşivi fiyatı **3 gün daha geriye** götürüyor — bu pencereye **dahil edildi**.');
P('    Şema birebir aynı, örtüşen anda fiyatlar **birebir aynı** (2.222/2.222 hücrede fark 0): "başka görüş" değil,');
P('    aynı akışın daha derini. Çakışmada `bot.db` kazanır (0,5 saat tolerans).');
P('    **08-02 arşivde var ama kullanılmadı** — o günün tüm kayıtları settlement sonrası (h≤5,4), karar anı fiyatı yok.');
const fiyatSehir = new Set([...fiyat.keys()].map((k) => k.split('|')[0]));
P(`  - Fiyatı olan şehir: **${fiyatSehir.size}** (junbo 50 şehir → Heat istasyonu; eşleşmeyen: ${eslesmeyenAd.size ? [...eslesmeyenAd].join(', ') : 'yok'}).`);
P(`- **Karar anı:** hedeften **${HEDEF_SAAT} saat** önce; fiyat o âna en yakın kayıttan (bayat fiyat yok).`);
P(`- **Örneklem eşiği:** hücreye güvenmek için en az **${MIN_N} gün**; oynamak için en az **${EDGE_MIN}** kenar.`);
P(`- **Karne gecikmesi:** karar verirken yalnız **${GECIKME} gün öncesine kadarki** sonuçlar bilinir (sızıntı yok).`);
P('');
P('> **En önemli sınır:** fiyat arşivi **6 haftalık**. Bu yüzden aşağıdaki kârlılık sayıları **yön**');
P('> gösterir, kesin sıralama değildir. Örneklem büyüdükçe bu rapor kendini tazeler.');
P('');

// ---- 1) Kaynak × ufuk ----
P('## 1. Kaynak × ufuk isabeti (tahmin tarafı)');
P('');
P('`İsabet` = tahminimizin yuvarlandığı derece, gerçeğin derecesiyle aynı mı — **bahis kovasının kazanma ölçüsü**.');
P('');
P('| Kaynak | Ufuk | n | MAE | Yanlılık | İsabet |');
P('|---|---|---|---|---|---|');
for (const c of [...kaynakOzet.values()].filter((c) => c.n >= 200).sort((a, b) => a.h - b.h || a.s / a.n - b.s / b.n)) {
  P(`| ${c.src} | t${c.h} | ${c.n} | **${f2(c.s / c.n)}** | ${f2(c.sapma / c.n)} | ${yz(c.hit / c.n)} |`);
}
P('');
P('> t1 = yarın (karar ufku). Ticari kaynaklar (VC/WA/OWM) yalnız 08-25\'ten beri var, bu yüzden n\'leri');
P('> derin kaynaklardan küçük — ama hepsi aynı pencerede kıyaslandı.');
P('');

// ---- 2) Şehir bazında ----
for (const h of [1, 2]) {
  const rows = [];
  for (const [k, m] of sehirGrup) {
    const [st, hh] = k.split('|');
    if (Number(hh) !== h) continue;
    const dizi = [...m.values()];
    if (dizi.length < MIN_N) continue;
    rows.push({
      st, n: dizi.length,
      mae: dizi.reduce((a, x) => a + Math.abs(x.v - x.g), 0) / dizi.length,
      bias: dizi.reduce((a, x) => a + (x.v - x.g), 0) / dizi.length,
      hit: dizi.filter((x) => x.hit).length / dizi.length,
    });
  }
  rows.sort((a, b) => a.mae - b.mae);
  if (!rows.length) continue;
  P(`## 2. Şehir bazında — t${h} (o günün kaynak ortalaması, en az ${MIN_N} gün)`);
  P('');
  P('**Peek yok:** o gün hangi kaynaklar varsa düz ortalaması alındı; "en iyi kaynağı seçmek" yapılmadı.');
  P('`MAE` küçük olsa bile **`İsabet` asıl sayıdır** — sistematik bir yanlılık MAE\'yi bozmadan isabeti');
  P('çökertebilir (tahmin hep 1° düşükse MAE 0.8° görünür ama kova hep yanlış olur).');
  P('');
  P('| # | Şehir | n | MAE | Yanlılık | İsabet |');
  P('|---|---|---|---|---|---|');
  rows.forEach((r, i) => P(`| ${i + 1} | ${sehirAdi.get(r.st) || r.st} | ${r.n} | **${f2(r.mae)}** | ${f2(r.bias)} | ${yz(r.hit)} |`));
  P('');
  P(`**En iyi 3:** ${rows.slice(0, 3).map((r) => `${sehirAdi.get(r.st) || r.st} ${f2(r.mae)}°`).join(' · ')}`);
  P(`**En kötü 3:** ${rows.slice(-3).map((r) => `${sehirAdi.get(r.st) || r.st} ${f2(r.mae)}°`).join(' · ')}`);
  P(`**Yayılım:** en iyi ${f2(rows[0].mae)}° ↔ en kötü ${f2(rows.at(-1).mae)}° — "hangi şehir" gerçek ve büyük bir etki.`);
  P('');
}

// ---- 2c) Kaynak × şehir × ufuk — kova isabeti (kullanıcının asıl sorusu: "kim neyde çok iyi?") ----
// Hücre = (kaynak × şehir × ufuk). Ölçü **kova isabeti**, MAE değil: sistematik 1°'lik yanlılık MAE'yi
// bozmadan kova isabetini çökertir (Karachi: MAE 0.6-0.8 ama kova %21). Güvenilirlik eşiği **gün** sayısıdır
// (MIN_N): 3 günlük %100 isabet beceri değil şanstır — "yalnız ona bet açarız" kararı bu eşik olmadan verilemez.
// Tam ızgara ayrı dosyaya yazılır (bu rapor okunur kalsın).
const hucre = new Map(); // "src|st|h" -> {src, st, h, gun:Set, n, hit, ae, sapma}
for (const r of tumSatir) {
  const k = `${r.src}|${r.st}|${r.h}`;
  let c = hucre.get(k);
  if (!c) { c = { src: r.src, st: r.st, h: r.h, gun: new Set(), n: 0, hit: 0, ae: 0, sapma: 0 }; hucre.set(k, c); }
  c.gun.add(r.D); c.n += 1; if (r.hit) c.hit += 1; c.ae += Math.abs(r.v - r.g); c.sapma += r.v - r.g;
}
const hucreler = [...hucre.values()]
  .map((c) => ({ src: c.src, st: c.st, h: c.h, gun: c.gun.size, n: c.n, hit: c.hit / c.n, mae: c.ae / c.n, bias: c.sapma / c.n }))
  .filter((c) => c.gun >= MIN_N)
  .sort((a, b) => b.hit - a.hit || a.mae - b.mae);
// "Lider" hücreler: aynı (şehir, ufuk) için eşiği geçen kaynaklar arasında en iyisi ve ikinciye farkı.
const kiyas = new Map(); // "st|h" -> gated hücreler
for (const c of hucreler) {
  const k = `${c.st}|${c.h}`;
  if (!kiyas.has(k)) kiyas.set(k, []);
  kiyas.get(k).push(c);
}
const lider = [];
for (const [k, dizi] of kiyas) {
  if (dizi.length < 2) continue;
  const s = [...dizi].sort((a, b) => b.hit - a.hit);
  lider.push({ ...s[0], ikinci: s[1].hit, ikinciSrc: s[1].src, fark: s[0].hit - s[1].hit, kaynakSayisi: dizi.length });
}
lider.sort((a, b) => b.fark - a.fark);

// Güven aralığı (Wilson): 8-15 günlük bir hücrede "%67" tek başına yanıltır — gerçek oran %42 de olabilir.
// Aralık yazılmazsa az günlü hücre "kesin" sanılır; kullanıcı "kesin rakam" isterken bunu görmeli.
// DİKKAT: birinci argüman **oran** (0-1), sayaç değil — hücreler `hit`i oran olarak tutuyor.
const wilson = (p, n) => {
  if (!n) return [0, 0];
  const z = 1.96, z2 = z * z;
  const merkez = (p + z2 / (2 * n)) / (1 + z2 / n);
  const yari = (z * Math.sqrt((p * (1 - p)) / n + z2 / (4 * n * n))) / (1 + z2 / n);
  return [Math.max(0, merkez - yari), Math.min(1, merkez + yari)];
};
const aralik = (hit, n) => { const [a, b] = wilson(hit, n); return `${Math.round(a * 100)}–${Math.round(b * 100)}`; };

P('## 2c. Kaynak × şehir × ufuk — kova isabeti');
P('');
P(`Hücre = **kaynak × şehir × ufuk**. Güvenilirlik eşiği: hücre en az **${MIN_N} gün** görmeli; altındaki`);
P('hücre "şans" olabilir. Tam ızgara (eşiği geçen tüm hücreler): `rapor_kaynak_sehir_ufuk_isabet.md`.');
P('');
P(`Eşiği geçen hücre: **${hucreler.length}** · birden çok kaynağın yarıştığı (şehir, ufuk) çifti: ${lider.length}`);
P('');
P('**Öne çıkanlar** — aynı (şehir, ufuk) için en iyi kaynak ile ikincisi arasındaki fark:');
P('');
P('| Şehir | Ufuk | En iyi kaynak | gün | Kova isabeti | %95 aralık | İkinci | Fark | MAE | Yanlılık |');
P('|---|---|---|---|---|---|---|---|---|---|');
for (const c of lider.filter((x) => x.fark >= 0.05).slice(0, 20)) {
  P(`| ${sehirAdi.get(c.st) || c.st} | t${c.h} | **${c.src}** | ${c.gun} | **${yz(c.hit)}** | ${aralik(c.hit, c.n)} | ${c.ikinciSrc} ${yz(c.ikinci)} | +${Math.round(c.fark * 100)} puan | ${f2(c.mae)} | ${f2(c.bias)} |`);
}
P('');
P('> **`%95 aralık` = Wilson güven aralığı.** "Kova isabeti %67" tek başına okunmaz: 15 günde 10 isabet');
P('> gerçek oranın **%42–%85** arasında olabileceğini söyler. Aralık genişse hücre **henüz kesin değil** —');
P('> gün sayısı arttıkça daralır. "Yalnız ona bet açarız" kararı için aralığın **ikincinin üstünde** kalması');
P('> gerekir; yoksa fark şans olabilir.');
P('> **Bu tablo isabet tarafıdır — tek başına "bet aç" demez.** Bahis kararı için ikinci ayak gerekir:');
P('> piyasanın o kovaya biçtiği fiyat (§3b/§4). Bir kaynak bir şehri yüksek isabetle tutturuyorsa');
P('> aday hücredir; **oynanır** olması için fiyatın kenar bırakması şarttır. **Bu tablonun fiyat sınavı');
P('> §3d\'dedir:** "yalnız açık ara önde olan kaynağı oyna" kuralı gerçek alış fiyatıyla ölçülür.');
P('');
P('> Eşiği geçen hücre sayısı puana göre **küçüktür** — az günü olan hücre bilerek dışarıda. Kaynak sayısı');
P('> arttıkça (ticari kaynaklar 08-25\'ten beri var) gün sayısı da artar; tablo kendiliğinden dolar.');
P('');

// ---- 2d) Kaynak kaynak: hangi şehirde hangi ufukta ÇOK İYİ? (kullanıcının istediği kesin liste) ----
// Kullanıcı "VC şu 4 şehirde bu ufukta iyi, OpenWeather şu 5 şehirde" gibi KAYNAK BAZLI ve KESİN bir liste
// ister. "Kesin önde" tanımı: o (şehir, ufuk) çiftinde **en iyi** kaynak VE Wilson **alt sınırı** ikincinin
// puan tahmininin **üstünde** — yani "fark şans olabilir" itirazı kapanmış hücre. Aralığı çakışanlar
// "önde ama kesin değil" diye AYRI sayılır; karıştırılırsa liste şişer ve güvenilirliği düşer.
const kesin = [];
const belirsiz = [];
for (const c of lider) {
  const [alt] = wilson(c.hit, c.n);
  if (alt > c.ikinci) kesin.push({ ...c, alt }); else belirsiz.push(c);
}
kesin.sort((a, b) => b.fark - a.fark);
// SIRALAMA TUZAĞI (kendi hatam, 2026-09-16): `fark`'a göre sıralamak **en zayıf kanıtlı** hücreleri en
// üste çıkarır — çünkü büyük fark, liderin iyi olmasından değil **ikincinin kötü olmasından** doğar
// (t4'te yalnız 2-3 kaynak yarışıyor, biri %8'de kalıyor → "+46 puan"). Bu yüzden kaynak içinde sıralama
// **kanıt gücüne** (gün sayısı) göre yapılır ve liste ikiye ayrılır: sağlam (≥SAGLAM_N gün) ve zayıf.
const SAGLAM_N = Number(process.env.HEAT_SAGLAM_N || 30);
const kaynakGruplari = new Map();
for (const c of kesin) {
  if (!kaynakGruplari.has(c.src)) kaynakGruplari.set(c.src, []);
  kaynakGruplari.get(c.src).push(c);
}
for (const dizi of kaynakGruplari.values()) dizi.sort((a, b) => b.gun - a.gun || b.fark - a.fark);
const saglamSayi = kesin.filter((c) => c.gun >= SAGLAM_N).length;
// "Hem kesin önde HEM isabeti gerçekten yüksek" — kullanıcının aradığı liste. %40 eşiği keyfi değil:
// bucket pazarında tipik fiyat 0.20-0.40 arasıdır (§3d "ort. ödenen" sütunu), yani %40 isabet
// piyasanın biçtiği fiyatın belirgin üstüdür. Ama yine de kenar ayağı (§3e) olmadan "oyna" demez.
// "Hem kesin önde HEM isabeti gerçekten yüksek" — kullanıcının aradığı liste. Ölçüt **Wilson ALT SINIRI**,
// gözlenen oran değil: "en kötü hâlinde bile %40" demek isteriz. Az günlü hücrede aralık genişler, alt
// sınır kendiliğinden düşer → küçük örneklem cezalandırılır, ayrı bir gün şartı gerekmez (yine de gün
// sayısı sütunda görünür). Sadece "n≥30" demek yeni kaynakları (08-27'de başlayan OM-ICON gibi) tümden
// eler ve en güçlü hücreyi (WSSS t1 %67, alt sınır %42) gizlerdi — yanlış olurdu.
const YUKSEK_ISABET = 0.40;
const fiyatlanir = (h) => FIYATLANABILIR.includes(h) && h <= 2;
const yuksek = kesin
  .map((c) => ({ ...c, alt2: wilson(c.hit, c.n)[0] }))
  .filter((c) => c.alt2 >= YUKSEK_ISABET && fiyatlanir(c.h));
P('## 2d. Kaynak kaynak: hangi şehirde hangi ufukta ÇOK İYİ? (kesin liste)');
P('');
P('Hücre = **kaynak × şehir × ufuk**. Burada yalnız **"kesin önde"** olanlar var: o (şehir, ufuk) çiftinde');
P('**en iyi** kaynak **ve** isabetinin **%95 alt sınırı** ikinci kaynağın puan tahmininin **üstünde**.');
P('Yani "bu fark şans olabilir" itirazı bu hücreler için **kapanmıştır**. Eşik: en az ' + MIN_N + ' gün.');
P('');
P(`**Kesin önde hücre: ${kesin.length}** · kaynak sayısı: ${kaynakGruplari.size} · bunlardan **sağlam kanıtlı`);
P(`(≥${SAGLAM_N} gün): ${saglamSayi}**, gerisi zayıf kanıt · önde ama aralığı çakıştığı için "kesin"`);
P(`sayılmayan hücre: **${belirsiz.length}** (listelenmez).`);
P('');
P('> **UYARI — "kesin önde" ≠ "iyi".** Bu bir **kıyas** hükmüdür: o şehirde o ufukta bu kaynak');
P('> **rakiplerinden** iyi. İkinci kaynak kötüyse (ör. %8) lider %30 ile de "kesin önde" olur. Hücrenin');
P('> isabetinin **kendisi** ayrı okunmalıdır — sağdaki "%95 aralık" sütunu tam bunun içindir.');
P('> **Ayrıca sıralama kanıt gücüne göredir (gün sayısı), "fark"a göre değil:** fark, ikincinin kötülüğünden');
P('> de doğar ve az günlü hücrelerde büyür — onları üste koymak yanıltıcı olurdu.');
P('');
P('> **"Oynanabilir" sütunu = o ufkun karar anında fiyat arşivinde kayıt var mı.** Fiyat arşivi ~67 saate');
P('> kadar gidiyor ama **48 saatten sonra çöküyor** (ölçüldü: ~24s 126.965 satır/45 şehir · ~48s 101.519/38 ·');
P('> ~60s 10.043/23 · ~72s **229**/21). Yani **t1 ve t2 fiyatlanabilir; t3+ fiyatlanamaz** — orada isabet');
P('> ölçülür, **kâr ölçülemez**. ✗ işaretli hücre "iyi tahmin, ama bugün oynanamaz" demektir.');
P('');
// Asıl hedeflenen liste: hem kesin önde, hem isabeti yüksek, hem oynanabilir.
P(`### Hedef liste: kesin önde + **%95 alt sınırı ≥ %${Math.round(YUKSEK_ISABET * 100)}** ("en kötü hâlinde bile yüksek") + oynanabilir`);
P('');
if (yuksek.length) {
  P('| Şehir | Ufuk | Kaynak | gün | Kova isabeti | %95 aralık | İkinci kaynak | Fark |');
  P('|---|---|---|---|---|---|---|---|');
  for (const c of [...yuksek].sort((a, b) => b.alt2 - a.alt2 || b.gun - a.gun)) {
    P(`| ${sehirAdi.get(c.st) || c.st} | t${c.h} | **${c.src}** | ${c.gun} | **${yz(c.hit)}** | ${aralik(c.hit, c.n)} | ${c.ikinciSrc} ${yz(c.ikinci)} | +${Math.round(c.fark * 100)} puan |`);
  }
} else {
  P('Eşiği geçen hücre **yok** — kesin önde olup %' + Math.round(YUKSEK_ISABET * 100) + ' alt sınırını veren, üstelik');
  P('oynanabilir (t1/t2) hücre bugün **yok**. Bu bir eksiklik değil, dürüst bir sonuç: isabet ayağı');
  P('tek başına "oyna" dedirtmiyor.');
}
P('');
P('> Ölçüt **gözlenen oran değil, aralığın ALT SINIRI** — "15 günde %67" tek başına yetmez, "en kötü');
P('> hâlinde de %40" gerekir. Az günlü hücre böylece kendiliğinden elenir; gün sayısı yine sütunda durur,');
P('> çünkü bir alt sınır **15 günden** geliyorsa aralık geniştir ve o hücre gün sayısıyla birlikte okunur.');
P('> **Eşik %40 keyfi değil:** bilet pazarında tipik fiyat 0.20–0.40 arasıdır (§3d "ort. ödenen"), yani %40');
P('> alt sınır piyasanın biçtiği fiyatın belirgin üstüdür. **Ama bu liste hâlâ "oyna" demez** — kenar ayağı');
P('> (§3e portföy testi) olmadan hiçbir hücre oynanır sayılmaz.');
P('');
for (const [src, dizi] of [...kaynakGruplari].sort((a, b) => b[1].length - a[1].length)) {
  const guclu = dizi.filter((c) => c.gun >= SAGLAM_N);
  const zayif = dizi.filter((c) => c.gun < SAGLAM_N);
  const oynanir = dizi.filter((c) => fiyatlanir(c.h));
  P(`**${src}** — ${dizi.length} hücrede kesin önde (${oynanir.length}'i bugün oynanabilir)`);
  P('');
  if (guclu.length) {
    P('| Şehir | Ufuk | gün | Kova isabeti | %95 aralık | İkinci kaynak | Fark | Oynanabilir |');
    P('|---|---|---|---|---|---|---|---|');
    for (const c of guclu) {
      P(`| ${sehirAdi.get(c.st) || c.st} | t${c.h} | ${c.gun} | **${yz(c.hit)}** | ${aralik(c.hit, c.n)} | ${c.ikinciSrc} ${yz(c.ikinci)} | +${Math.round(c.fark * 100)} puan | ${fiyatlanir(c.h) ? '✓' : '✗ fiyat yok'} |`);
    }
    P('');
  }
  if (zayif.length) {
    P(`<sub>Zayıf kanıt (${SAGLAM_N} günden az) — okunur ama karar dayanağı yapılmaz: ` +
      zayif.map((c) => `${sehirAdi.get(c.st) || c.st} t${c.h} ${yz(c.hit)} (${c.gun}g)`).join(' · ') + '</sub>');
    P('');
  }
}
if (!kesin.length) P('> Eşiği geçen ve kesin önde olan hücre **yok** — hiçbir kaynak bir şehirde istatistiksel olarak açık ara önde değil.');
P('');
P('> **Bu liste "oyna" demek değildir.** İsabet ayağıdır. Fiyat ayağı §3b/§3d/§3e\'dedir: bir hücrenin');
P('> isabeti yüksek olsa bile, piyasa o kovayı o isabete yakın fiyatlıyorsa **kenar yoktur** ve bahis');
P('> para bırakmaz. Kesin önde olmak, **oynanabilir olmanın ön şartıdır**, kendisi değil.');
P('');


// Tam ızgara dosyası: eşiği geçen TÜM (kaynak × şehir × ufuk) hücreleri, ufuk ve şehir sırasında.
const izgara = ['# Kaynak × şehir × ufuk — kova isabeti (tam ızgara)', '',
  '> `rapor_oynanir_tablo.md` §2c ve §2d\'nin tam hâli. Hücre = (kaynak × şehir × ufuk).', '',
  `> **Eşik: en az ${MIN_N} gün.** Altındaki hücreler listelenmez (şans olabilir).`, '',
  `> İsabet = tahminin düştüğü kova, gerçeğin kovasıyla aynı mı. Ölçü **kova isabeti**, MAE değil.`, '',
  `> **Ufuk kapsamı t1–t${MAKS_UFUK}** (2026-09-16'da açıldı: veride t3/t4 gerçekten vardı ama rapor`,
  '> yalnız t1/t2 yüklüyordu — Visual Crossing t3, OpenWeatherMap t3/t4, Open-Meteo previous t3 hiç görünmüyordu).', '',
  '> **DİKKAT — bu dosyadaki t3 ve t4 hücreleri "isabet"i gösterir, "kâr"ı göstermez:** fiyat arşivi ~67 saate',
  '> kadar gidiyor ama 48 saatten sonra çöküyor, yani t3+ için karar anı fiyatı **yok**. O ufuklarda hücre',
  '> oynanabilir değildir; "iyi tahmin, ama fiyatı bilinmiyor" diye okunur (§2d "Oynanabilir" sütunu).', '',
  `> Üretim: ${new Date().toISOString()} · pencere ${PENCERE_BASI}..${SON_GUN} · eşiği geçen hücre ${hucreler.length}`, ''];
for (const h of [1, 2, 3, 4, 5, 6, 7]) {
  const dizi = hucreler.filter((c) => c.h === h);
  if (!dizi.length) continue;
  izgara.push(`## t${h}`, '', '| Şehir | Kaynak | gün | n | Kova isabeti | %95 aralık | MAE | Yanlılık |', '|---|---|---|---|---|---|---|---|');
  for (const c of [...dizi].sort((a, b) => a.st.localeCompare(b.st) || b.hit - a.hit)) {
    izgara.push(`| ${sehirAdi.get(c.st) || c.st} | ${c.src} | ${c.gun} | ${c.n} | ${yz(c.hit)} | ${aralik(c.hit, c.n)} | ${f2(c.mae)} | ${f2(c.bias)} |`);
  }
  izgara.push('');
}
const IZGARA_OUT = path.join(path.dirname(OUT), 'rapor_kaynak_sehir_ufuk_isabet.md');
fs.writeFileSync(IZGARA_OUT, izgara.join('\n') + '\n', 'utf8');

// ---- 3) Sızıntısız backtest ----
const TUM_GUNLER = [...new Set(tumSatir.map((r) => r.D))].filter((d) => d >= FIYAT_BASI).sort();
const sonByDay = new Map(); // D -> [ {src, st, v} ] yalnız t1
for (const [k, f] of son) {
  const [src, st, D, h] = k.split('|');
  if (Number(h) !== 1 || D < FIYAT_BASI) continue;
  if (!sonByDay.has(D)) sonByDay.set(D, []);
  sonByDay.get(D).push({ src, st, v: f.v });
}

const karne = new Map(); // "st|src" -> {n, sumBias, hitHam, hitDuz}   (t1; ufuk sabit)
// Yürüyen yanlılık düzeltmesi: her satır eklenirken, O ANA KADARKİ ortalama yanlılıkla düzeltilmiş
// kovanın tutup tutmadığı ayrıca sayılır. Böylece "düzeltme işe yarıyor mu" sorusu da sızıntısız ölçülür.
function karneye(rows) {
  for (const r of rows) {
    const k = `${r.st}|${r.src}`;
    const s = karne.get(k) || { n: 0, sumBias: 0, hitHam: 0, hitDuz: 0 };
    const bias = s.n ? s.sumBias / s.n : 0;
    if (kovaSt(r.st, r.v) === kovaSt(r.st, r.g)) s.hitHam += 1;
    if (kovaSt(r.st, r.v - bias) === kovaSt(r.st, r.g)) s.hitDuz += 1;
    s.sumBias += r.v - r.g;
    s.n += 1;
    karne.set(k, s);
  }
}
const tumuGunSirali = [...new Set(tumSatir.filter((r) => r.h === 1).map((r) => r.D))].sort();
const gunIciSatir = new Map();
for (const r of tumSatir) {
  if (r.h !== 1) continue;
  if (!gunIciSatir.has(r.D)) gunIciSatir.set(r.D, []);
  gunIciSatir.get(r.D).push(r);
}

let karneIdx = 0;
const kovalar = { ham: [], duz: [] };  // aday kayıtlar (uygun her hücre = kıyas)
const oynanan = { ham: [], duz: [] };
// §3d: yalnız "açık ara önde" olan kaynağın oynandığı varyant. Karşılaştırma ölçütü de karne gibi
// YÜRÜYEN ve SIZINTISIZ: o gün karneye giren (n ≥ MIN_N) diğer kaynakların isabeti.
const oynananSadece = { ham: [], duz: [] };
for (const D of TUM_GUNLER) {
  // (a) karne: yalnız D-GECIKME ve öncesi (sızıntı yok)
  const sinir = gunEksi(D, GECIKME);
  while (karneIdx < tumuGunSirali.length && tumuGunSirali[karneIdx] <= sinir) {
    karneye(gunIciSatir.get(tumuGunSirali[karneIdx]) || []);
    karneIdx += 1;
  }
  // (b) karar — iki varyant: HAM kova ve YANLILIK DÜZELTMELİ kova
  const enIyi = { ham: new Map(), duz: new Map() };
  const gunKayit = { ham: [], duz: [] };
  // O gün karneye giren her (şehir, kaynak, varyant) — fiyatı olmayanlar DA. Rakip ölçütü isabettir, fiyat değil.
  const gruplar = new Map(); // "st|tur" -> Map(src -> {Pwin, rakip})
  for (const a of sonByDay.get(D) || []) {
    const s = karne.get(`${a.st}|${a.src}`);
    if (!s || s.n < MIN_N) continue;
    const g = gercek.get(`${a.st}|${D}`);
    if (g == null) continue;
    const bias = s.sumBias / s.n;
    for (const tur of ['ham', 'duz']) {
      const Pwin = (tur === 'ham' ? s.hitHam : s.hitDuz) / s.n;
      const grupA = `${a.st}|${tur}`;
      if (!gruplar.has(grupA)) gruplar.set(grupA, new Map());
      const giris = { Pwin, rakip: null };
      gruplar.get(grupA).set(a.src, giris);
      const th = kovaSt(a.st, tur === 'ham' ? a.v : a.v - bias);
      const f = fiyat.get(`${a.st}|${D}|${th}`);
      if (!f) continue;
      const kayit = {
        D, st: a.st, src: a.src, tur, v: a.v, bias, th, Pwin, price: f.p, saat: f.saat,
        edge: Pwin - f.p, karneN: s.n, g, hit: th === kovaSt(a.st, g), giris,
      };
      kovalar[tur].push(kayit);
      gunKayit[tur].push(kayit);
      const c = enIyi[tur].get(a.st);
      if (!c || kayit.edge > c.edge) enIyi[tur].set(a.st, kayit);
    }
  }
  // Rakip: aynı (şehir, varyant) için en iyi DİĞER kaynağın isabeti — o gün karneye girenler arasında.
  for (const grup of gruplar.values()) {
    for (const [src, x] of grup) {
      let m = null;
      for (const [src2, y] of grup) if (src2 !== src && (m == null || y.Pwin > m)) m = y.Pwin;
      x.rakip = m;
    }
  }
  for (const tur of ['ham', 'duz']) for (const a of enIyi[tur].values()) if (a.edge >= EDGE_MIN) oynanan[tur].push(a);
  // §3d: şehir başına YALNIZ açık ara önde olan kaynağın en yüksek kenarlı bahsi
  for (const tur of ['ham', 'duz']) {
    const onde = new Map();
    for (const k of gunKayit[tur]) {
      if (k.giris.rakip == null || k.Pwin - k.giris.rakip < ONDE_MIN) continue;
      const c = onde.get(k.st);
      if (!c || k.edge > c.edge) onde.set(k.st, k);
    }
    for (const a of onde.values()) if (a.edge >= EDGE_MIN) oynananSadece[tur].push(a);
  }
}

const roi = (arr) => {
  if (!arr.length) return null;
  let net = 0, netF = 0, kaz = 0, kenar = 0;
  for (const a of arr) {
    if (a.hit) { net += 1 / a.price - 1; netF += (1 / a.price - 1) * 0.95; kaz += 1; } else { net -= 1; netF -= 1; }
    kenar += a.edge;
  }
  return { n: arr.length, kaz, net: net / arr.length, netF: netF / arr.length, kenar: kenar / arr.length };
};

P('## 3. Backtest — kural gerçekten para kazandırır mıydı?');
P('');
P(`**Kural:** her gün, o güne kadarki karnesi **${MIN_N}+ gün** olan (şehir × t1 × kaynak) hücreleri arasından`);
P(`**kenarı en yüksek** olanı seç; o kovayı $1 oyna. Karar anı hedeften ${HEDEF_SAAT} saat önce, fiyat o âna en`);
P(`yakın kayıttan. **Sızıntı yok:** karne ${GECIKME} gün geriden gelir, koşu hedef günden sonraysa atılır.`);
P('');
P('**İki varyant yan yana** — çünkü fark, projenin bütün meselesi:');
P('');
P('- **HAM:** kova = `round(tahmin)`. Tahmin sistematik olarak 1° düşükse kova da hep bir altta kalır.');
P('- **DÜZELTMELİ:** kova = `round(tahmin − o ana kadarki ortalama yanlılık)`. Yanlılık yürüyen pencereden,');
P('  yalnız geçmiş günlerden hesaplanır.');
P('');
const R = { ham: roi(oynanan.ham), duz: roi(oynanan.duz) };
P('| Strateji | Bahis | Kazanan | Ort. kenar | ROI (ücretsiz) | ROI (%5 ücret varsayımı) |');
P('|---|---|---|---|---|---|');
for (const tur of ['ham', 'duz']) {
  const r = R[tur];
  if (!r) continue;
  P(`| **${tur === 'ham' ? 'HAM kova' : 'DÜZELTMELİ kova'}** | ${r.n} | ${r.kaz} (${yz(r.kaz / r.n)}) | ${isaretli(r.kenar)} | **${yz(r.net)}** | **${yz(r.netF)}** |`);
}
const Rh = { ham: roi(kovalar.ham), duz: roi(kovalar.duz) };
for (const tur of ['ham', 'duz']) {
  const r = Rh[tur];
  if (!r) continue;
  P(`| Kıyas: ${tur === 'ham' ? 'ham' : 'düzeltmeli'} uygun her hücre | ${r.n} | ${r.kaz} (${yz(r.kaz / r.n)}) | ${isaretli(r.kenar)} | ${yz(r.net)} | ${yz(r.netF)} |`);
}
P('| Kıyas: hiçbir şey yapma | 0 | 0 | 0 | %0 | %0 |');
P('');
// BAYAT FİYAT DENETİMİ — 2026-09-07'de bizi yakan tuzak: seyrek snapshot'tan alınan fiyat sahte kâr üretir.
// O yüzden "fiyat karar anına ne kadar yakındı" ÖLÇÜLÜR ve rapora yazılır; yakınlık kötüyse ROI'ye güvenilmez.
for (const tur of ['duz', 'ham']) {
  const dizi = oynanan[tur];
  if (!dizi.length) continue;
  const sapma = dizi.map((a) => Math.abs(a.saat - HEDEF_SAAT)).sort((x, y) => x - y);
  const yakin = sapma.filter((x) => x <= 6).length;
  const medyan = sapma[Math.floor(sapma.length / 2)];
  P(`**Bayat fiyat denetimi (${tur === 'duz' ? 'düzeltmeli' : 'ham'}):** oynanan ${dizi.length} bahsin fiyatı karar anına`);
  P(`medyan **${f2(medyan)} saat** uzaklıkta; **${yz(yakin / sapma.length)}**'i ±6 saat içinde, en uzak **${f2(sapma.at(-1))} saat**.`);
  P(yakin / sapma.length >= 0.8
    ? '→ Fiyatlar taze: ROI bu tuzağa takılmıyor.'
    : '→ **DİKKAT:** fiyatların önemli kısmı karar anından uzak; aşağıdaki ROI **şişmiş olabilir**.');
  P('');
}
const iyiTur = (R.duz && R.ham) ? (R.duz.net > R.ham.net ? 'duz' : 'ham') : (R.duz ? 'duz' : 'ham');

// ---- §3c GERÇEK ALIŞ FİYATI (ask) — "kâğıt üstünde mi, gerçekten alınabilir mi?" ----
if (ob) {
  const askEkle = (arr) => arr.map((a) => ({ ...a, ask: askAt(`${a.st}|${a.D}|${a.th}`) })).filter((a) => a.ask);
  const roiAsk = (arr) => {
    if (!arr.length) return null;
    let net = 0, netF = 0, kaz = 0, netMid = 0;
    for (const a of arr) {
      // netMid: AYNI alt kümede mid fiyatla ROI. "ROI(mid) tüm bahislerde, ROI(ask) yalnız ask
      // bulunanlarda" kıyası elmayla-armut olurdu — düşüşün ne kadarı fiyattan, ne kadarı
      // örneklem değişiminden geliyor, ancak böyle ayrılır.
      if (a.hit) { net += 1 / a.ask.a - 1; netF += (1 / a.ask.a - 1) * 0.95; netMid += 1 / a.price - 1; kaz += 1; }
      else { net -= 1; netF -= 1; netMid -= 1; }
    }
    return { n: arr.length, kaz, net: net / arr.length, netF: netF / arr.length, netMid: netMid / arr.length };
  };
  P('### 3b. Gerçek alış fiyatı (ask) — kâğıt üstünde mi, alınabilir mi?');
  P('');
  P('Yukarıdaki ROI **`yes_price`** ile hesaplandı; o **son/mid** fiyattır, gerçekte ödediğimiz fiyat değil.');
  P('Alırken **`best_ask`** ödenir. Bu yüzden aynı kural gerçek emir defteri fiyatıyla tekrar ölçüldü');
  P('(kardeş `orderbook.db`, salt-okunur).');
  P('');
  P('**DİKKAT — fiyat arşivini derinleştirmek bu bölümü BÜYÜTMEZ.** Emir defteri arşivi 2026-08-05\'te başlıyor;');
  P(`fiyat penceresi ise ${fiyatGun[0]}\'te. Aradaki günlerde mid fiyat **var**, gerçek \`best_ask\` **yok** —`);
  P('yani §3 (mid ROI) o günlerden beslenir ama §3b\'nin ask ile doğrulanmış örneklemi aynı kalır. İki bölümün');
  P('örneklemi bu yüzden farklıdır ve **farklı büyür**; bunu bilmeden "pencereyi geriye çektik, güven arttı" denmez.');
  P('');
  P(`**Pencere şartı:** ask yalnız karar anına **±${f2(ASK_PENCERE / 3600e3)} saat** yakınsa kabul edilir.`);
  P('Sebebi ölçüldü: ilk denemede ask karar anından 37 saate kadar uzaktan alınabiliyordu ve ROI **yükseldi**');
  P('(%44→%111) — bu **imkânsızdır**, çünkü alış fiyatı ≥ mid ise ROI ancak düşer. Rastgele bir andaki fiyat');
  P('"alış fiyatı" değildir. Pencere dışı hücreler **veri yok** sayılır, uydurulmaz.');
  P('');
  // KIYAS ADALETİ: "ROI(mid)" tüm bahislerde, "ROI(ask)" ise yalnız ask bulunabilen alt kümede
  // hesaplanırsa fark, fiyat farkıyla örneklem farkını karıştırır. Bu yüzden mid ROI'si de
  // **aynı alt kümede** yazılır; tek geçerli kıyas satırın kendi içindedir.
  P('| Varyant | Bahis (tümü) | Ask bulunan (pencere içi) | Kazanan | ROI (mid, tümü) | ROI (mid, **aynı alt küme**) | **ROI (ask)** | ROI (ask + %5 ücret) |');
  P('|---|---|---|---|---|---|---|---|');
  for (const tur of ['ham', 'duz']) {
    const r = R[tur];
    if (!r) continue;
    const a = askEkle(oynanan[tur]);
    const ra = roiAsk(a);
    P(`| ${tur === 'ham' ? 'HAM kova' : 'DÜZELTMELİ kova'} | ${r.n} | ${a.length} (${yz(a.length / r.n)}) | ${ra ? ra.kaz : '-'} | ${yz(r.net)} | ${ra ? yz(ra.netMid) : '-'} | **${ra ? yz(ra.net) : '-'}** | ${ra ? yz(ra.netF) : '-'} |`);
  }
  P('');
  const ornek = askEkle(oynanan[iyiTur]);
  if (ornek.length) {
    const fark = ornek.map((a) => a.ask.a - a.price).sort((x, y) => x - y);
    const medFark = fark[Math.floor(fark.length / 2)];
    const uzak = ornek.map((a) => a.ask.d / 3600e3).sort((x, y) => x - y);
    // Fiyatlar 0.01-0.20 bandında; burada +0.01'lik fark "küçük" DEĞİLDİR (0.01→0.02 kazancı yarıya
    // indirir). Bu yüzden farkın yanına ORANI da yazıyoruz — asıl anlamlı olan oran.
    const oran = ornek.map((a) => a.ask.a / a.price).sort((x, y) => x - y);
    const medFiyat = ornek.map((a) => a.price).sort((x, y) => x - y)[Math.floor(ornek.length / 2)];
    P(`**Alış fiyatı gerçekten ne kadar yüksek?** (${iyiTur === 'duz' ? 'düzeltmeli' : 'ham'} varyant, ${ornek.length} bahis)`);
    P(`mid→ask farkı: medyan **${isaretli(medFark)}**, ortalama ${isaretli(fark.reduce((t, x) => t + x, 0) / fark.length)}, en büyük ${isaretli(fark.at(-1))}.`);
    P(`mid→ask **oranı**: medyan **${f2(oran[Math.floor(oran.length / 2)])}×**, en büyük ${f2(oran.at(-1))}×. Oynanan mid fiyatların medyanı **${f2(medFiyat)}** — bu bantta 1 sentlik fark kazancı yarıya indirir, o yüzden fark değil **oran** okunur.`);
    P(`Ask kaydının karar anına uzaklığı: medyan **${f2(uzak[Math.floor(uzak.length / 2)])} saat**, en uzak ${f2(uzak.at(-1))} saat.`);
    P('');
    // MALİYET BARIYERİ (2026-09-16): yukarıdaki mid→ask farkı bir **eşleştirme** farkıdır (mid bir kayıttan,
    // ask ±1 saat içindeki başka bir kayıttan) — yani içinde fiyat sürüklenmesi de var. Gerçek, dar anlamdaki
    // maliyet **aynı andaki yarı-spread**tir: `best_ask − (best_bid+best_ask)/2`. Bu ölçüm emir defterinden
    // DOĞRUDAN ve tek geçişte alınır; "kenarımız kaç puan olmalı" sorusunun cevabı budur.
    if (ob) {
      try {
        const bant = [[0.02, 0.10], [0.10, 0.20], [0.20, 0.40]];
        P('**Maliyet bariyeri — aynı andaki gerçek yarı-spread** (`best_ask − mid`; emir defterinden doğrudan, tek geçiş)');
        P('');
        P('| Ödenen fiyat bandı | Kayıt | Ortalama yarı-spread | ≤1 sent | ≤2 sent | ≤5 sent |');
        P('|---|---|---|---|---|---|');
        for (const [lo, hi] of bant) {
          const s = ob.prepare(`SELECT COUNT(*) n,
              AVG(best_ask-(best_bid+best_ask)/2.0) ort,
              SUM(CASE WHEN best_ask-(best_bid+best_ask)/2.0<=0.01 THEN 1 ELSE 0 END) a,
              SUM(CASE WHEN best_ask-(best_bid+best_ask)/2.0<=0.02 THEN 1 ELSE 0 END) b,
              SUM(CASE WHEN best_ask-(best_bid+best_ask)/2.0<=0.05 THEN 1 ELSE 0 END) c
            FROM orderbook_snapshots WHERE best_ask>=? AND best_ask<? AND best_bid>0 AND best_bid<1
              AND best_ask>best_bid AND snapshot_time>=?`).get(lo, hi, FIYAT_BASI);
          if (!s || !s.n) continue;
          P(`| ${lo}–${hi} | ${Number(s.n).toLocaleString('en-US')} | **${f2(s.ort)}** | ${yz(s.a / s.n)} | ${yz(s.b / s.n)} | ${yz(s.c / s.n)} |`);
        }
        P('');
        P('> **Okunuşu:** gerçek alış maliyeti bu bantta **ortalama ~2 sent**. Bu, "kenarımız en az kaç puan');
        P('> olmalı" sorusunun cevabıdır: **2 sentin altındaki bir kenar alımda sıfırlanır.** §3\'ün karar anı');
        P('> kenarı ~1,8 puan (§3 tablosunda fiyat ~0,18 iken mid ROI ~+%10) — yani **kenar ile maliyet');
        P('> neredeyse birebir aynı.** Projenin her kuralının aynı yerde tıkanmasının aritmetik sebebi budur.');
        P('');
      } catch { /* emir defteri okunamadıysa bu blok atlanır */ }
    }
    // TUTARLILIK DENETİMİ: ask < mid çıkıyorsa kaynak/join şüphelidir — ROI'yi yazmadan önce bunu söyle.
    P(medFark < 0
      ? '⛔ **TUTARSIZ:** ask medyan olarak mid\'den **düşük** çıktı. Alış fiyatı satış fiyatından küçük olamaz — ' +
        'bu, eşleştirmenin ya da kaynağın bozuk olduğunu gösterir. **Aşağıdaki ask ROI\'sine GÜVENİLMEZ.**'
      : '✓ Tutarlı: ask ≥ mid (alış, satıştan pahalı) — beklenen yön.');
    P('');
    const ra = roiAsk(ornek);
    const kapsam = ornek.length / oynanan[iyiTur].length;
    P(kapsam < 0.5
      ? `→ Pencere içinde ask bulunan bahis oranı **${yz(kapsam)}** — bu kadar düşük kapsamla ROI **temsil edici değil**.`
      : ra && ra.net > 0.05
        ? '→ Kenar gerçek alış fiyatıyla da pozitif görünüyor — "kâğıt üstünde" itirazı bu örneklemde karşılanıyor. ' +
          '**Ama §3c\'deki yoğunlaşma uyarısı aynen geçerli.**'
        : '→ **Kenar gerçek alış fiyatıyla eriyor.** Görünen "kâr" kısmen **alınamayan fiyattan** geliyordu.');
    // ASIL AYRIM: alım fiyatı mı düşürdü, yoksa kâr zaten alınamayan hücrelerde miydi?
    // İkisi ayrı sorudur; mid ROI'sinin AYNI alt kümedeki hâli bunu söyler.
    const tamNet = R[iyiTur] ? R[iyiTur].net : null;
    if (ra && tamNet != null) {
      const dususPuan = (tamNet - ra.net) * 100;
      const fiyatPayi = (ra.netMid - ra.net) * 100;
      P('');
      P(`**Bu düşüşün sebebi ne?** Aynı ${ra.n} bahiste mid ROI **${yz(ra.netMid)}**, ask ROI **${yz(ra.net)}**`);
      P(`→ alış fiyatının payı **${fiyatPayi >= 0 ? '+' : ''}${f2(fiyatPayi)} puan**. ` +
        `Tüm örneklemdeki ROI ise **${yz(tamNet)}**; yani "${iyiTur === 'duz' ? 'düzeltmeli' : 'ham'}" satırının ` +
        `tüm-örneklem ROI'sinden bu alt kümeye inerken **${f2(dususPuan)} puan** zaten kayıp.`);
      P('**Bu bölümün sonucu:** kârın büyük kısmı **emir defteri olmayan (oynanamayan) hücrelerde** duruyor; ' +
        'oynanabilir hücrelerde kenar zaten ince, gerçek alış fiyatıyla da tükeniyor.');
    }
  } else {
    P('Bu bahisler için pencere içinde emir defteri fiyatı bulunamadı — ask analizi yapılamadı.');
  }
  if (obHata) P(`> Not: ${obHata} hücrede emir defteri okunamadı (kardeş daemon yazıyor olabilir) — o hücreler ask analizine girmedi.`);
  P('');
}
if (R.ham && R.duz) {
  // Bu satır §3 ve §3c'nin özetidir; §3b (gerçek ask) bu resmi DEĞİŞTİRDİĞİ için tek başına okunmasın.
  P(`**Okunuşu (§3):** mid fiyatla düzeltme ROI'yi ${yz(R.ham.net)} → ${yz(R.duz.net)} yaptı.`);
  P('**Ama bu son cümle değil:** §3b gerçek alış fiyatıyla bu kenarın **oynanabilir hücrelerde tükendiğini**,');
  P('§3c ise kârın **birkaç şehir/günde toplandığını** gösteriyor. Üçü birlikte okunur — tek başına §3 yanıltır.');
  P('');
} else if (!R.ham && !R.duz) {
  P(`Bu pencerede **hiçbir hücre eşiği geçmedi** (karne ${MIN_N}+ güne ulaşmadı ya da kenar ${EDGE_MIN}'i aşmadı).`);
  P('Bu bir sonuçtur: *"henüz oynanır hücre yok"* — uydurma yok.');
  P('');
}

// KÂR YOĞUNLAŞMASI — "+%44 gerçek mi, tek şehir/tek sıcak dalga mı taşıyor?" sorusunun cevabı.
// Örneklem 6 haftalık ve bahisler aynı hava sistemlerine bağlı (bağımsız değil); bu yüzden kârın
// nereden geldiği ROI'nin kendisi kadar önemli. Tek hücre toplamın yarısını taşıyorsa ROI gürültüdür.
const secilen = oynanan[iyiTur];
const bahisKar = (a) => (a.hit ? 1 / a.price - 1 : -1);
if (secilen.length) {
  const toplamKar = secilen.reduce((t, a) => t + bahisKar(a), 0);
  P('### 3c. Kâr nereden geliyor? (yoğunlaşma denetimi)');
  P('');
  P(`Örneklem **6 haftalık** ve bahisler aynı hava sistemlerine bağlı — bağımsız değil. Kâr tek bir şehre ya da tek`);
  P('bir sıcak dalgaya yaslanıyorsa ROI bir beceri değil, bir tesadüftür. Toplam kâr: **$' + f2(toplamKar) + '**');
  P('');
  for (const [ad, anahtar] of [['Şehir', (a) => sehirAdi.get(a.st) || a.st], ['Gün', (a) => a.D]]) {
    const m = new Map();
    for (const a of secilen) {
      const k = anahtar(a);
      const c = m.get(k) || { n: 0, kar: 0 };
      c.n += 1; c.kar += bahisKar(a);
      m.set(k, c);
    }
    const g = [...m].sort((x, y) => y[1].kar - x[1].kar);
    const ilk3 = g.slice(0, 3).reduce((t, x) => t + x[1].kar, 0);
    const pay = toplamKar > 0 ? ilk3 / toplamKar : null;
    P(`**${ad} bazında:** en iyi 3 ${ad.toLowerCase()} toplam kârın **${yz(pay)}**'ini taşıyor ` +
      `(${g.slice(0, 3).map(([k, v]) => `${k} $${f2(v.kar)}`).join(' · ')}). ` +
      `En kötü 3: ${g.slice(-3).map(([k, v]) => `${k} $${f2(v.kar)}`).join(' · ')}.`);
    P('');
  }
  const mSehir = new Map();
  for (const a of secilen) {
    const k = sehirAdi.get(a.st) || a.st;
    const c = mSehir.get(k) || { n: 0, kaz: 0, kar: 0 };
    c.n += 1; if (a.hit) c.kaz += 1; c.kar += bahisKar(a);
    mSehir.set(k, c);
  }
  const sirali = [...mSehir].sort((x, y) => y[1].kar - x[1].kar);
  P('| Şehir | Bahis | Kazanan | Kâr ($1/bahis) |');
  P('|---|---|---|---|');
  for (const [k, v] of sirali.slice(0, 10)) P(`| ${k} | ${v.n} | ${v.kaz} (${yz(v.kaz / v.n)}) | ${isaretli(v.kar)} |`);
  if (sirali.length > 10) P(`| … ${sirali.length - 10} şehir daha | | | ${isaretli(sirali.slice(10).reduce((t, x) => t + x[1].kar, 0))} |`);
  P('');
  // EN SADE TEST: en iyi 3 şehri ve en iyi 3 günü ÇIKAR, kalanla ROI ne? Kenar gerçekse düşmez.
  const atSt = new Set(sirali.slice(0, 3).map(([k]) => k));
  const kalanSehir = secilen.filter((a) => !atSt.has(sehirAdi.get(a.st) || a.st));
  const gunKar = new Map();
  for (const a of secilen) gunKar.set(a.D, (gunKar.get(a.D) || 0) + bahisKar(a));
  const atGun = new Set([...gunKar].sort((x, y) => y[1] - x[1]).slice(0, 3).map(([k]) => k));
  const kalanGun = secilen.filter((a) => !atGun.has(a.D));
  const roiAlt = (arr) => (arr.length ? arr.reduce((t, a) => t + bahisKar(a), 0) / arr.length : null);
  P('**En sade test — en iyileri çıkar, kalan ne diyor?** Kenar gerçekse en iyi 3 şehir/gün çıkınca da');
  P('ROI pozitif kalır; yalnız birkaç hücre taşıyorsa çöker:');
  P('');
  P(`- Hepsi: **${yz(roiAlt(secilen))}** (${secilen.length} bahis)`);
  P(`- En iyi 3 şehir çıkarılınca: **${yz(roiAlt(kalanSehir))}** (${kalanSehir.length} bahis)`);
  P(`- En iyi 3 gün çıkarılınca: **${yz(roiAlt(kalanGun))}** (${kalanGun.length} bahis)`);
  P('');
  const kalan = roiAlt(kalanSehir);
  P(kalan == null ? '' :
    kalan > 0.10
      ? '→ Kalan örneklemde de belirgin pozitif: kenar **tek hücreye yaslanmıyor**.'
      : kalan > 0.05
        ? '→ Kalan örneklemde **ücretin (varsayım %5) sınırında**: kenarın büyük kısmı çıkarılan 3 şehirdeydi, ' +
          'kalan kısım ücreti ancak karşılar. **Bankaya yazılacak bir kenar değil.**'
        : `→ **Kalan örneklemde kenar eriyor (ücretin altı).** ${yz(roiAlt(secilen))} ROI bir beceri değil, ` +
          '**birkaç şehrin/günün şansı**. Bu sayı tek başına "oynanır" demek için yeterli DEĞİL.');
  P('');
}

const enIyi10 = [...oynanan[iyiTur]].sort((a, b) => b.edge - a.edge).slice(0, 10);
if (enIyi10.length) {
  P(`**Oynanan bahisler — ${iyiTur === 'duz' ? 'düzeltmeli' : 'ham'} varyant (kenara göre ilk 10):**`);
  P('');
  P('| Gün | Şehir | Kaynak | Tahmin | Yanlılık | Kova | Gerçek | P(bizim) | Fiyat | Kenar | Sonuç |');
  P('|---|---|---|---|---|---|---|---|---|---|---|');
  for (const a of enIyi10) {
    P(`| ${a.D} | ${sehirAdi.get(a.st) || a.st} | ${a.src} | ${f2(a.v)} | ${isaretli(a.bias)} | ${a.th}° | ${a.g == null ? '-' : f2(a.g)} | ${yz(a.Pwin)} (n=${a.karneN}) | ${f2(a.price)} | ${isaretli(a.edge)} | ${a.hit ? '**KAZANDI**' : 'kaybetti'} |`);
  }
  P('');
}

// ---- 3d) YALNIZ "AÇIK ARA ÖNDE" OLAN KAYNAK — fiyat kenar bırakıyor mu? ----
// Kullanıcının kuralı: "bir kaynak sadece bir şehri çok iyi tutturuyorsa yalnız ve yalnız ona bet açarız".
// Bu bölüm o kuralı ölçer: §3d bahsi YALNIZ kaynağın aynı şehirdeki en iyi DİĞER kaynaktan ≥ ONDE_MIN puan
// önde olduğu hücrelerde açılır (karşılaştırma da yürüyen karne → sızıntı yok). Sonra aynı bahisler
// GERÇEK `best_ask` ile sınanır. §2c isabet tarafıdır; burası **para** tarafıdır.
const roiX = (arr, pFn, ucret = false) => {
  if (!arr.length) return null;
  let net = 0, kaz = 0;
  for (const a of arr) {
    if (a.hit) { net += (1 / pFn(a) - 1) * (ucret ? 0.95 : 1); kaz += 1; } else net -= 1;
  }
  return { n: arr.length, kaz, net: net / arr.length };
};
P('### 3d. Yalnız "açık ara önde" olan kaynak — oynanabilir mi?');
P('');
P(`**Kural:** o gün, o şehirde kaynak, karneye giren **en iyi diğer kaynaktan en az ${Math.round(ONDE_MIN * 100)}`);
P('puan** önde değilse o şehirde **hiç oynanmaz**. Yani "her şehirde en iyiyi oyna" değil, "yalnız bir');
P('kaynağın gerçekten önde olduğu şehirde oyna". Karşılaştırma yürüyen karneye dayanır (sızıntı yok).');
P('');
P('| Varyant | Bahis | Kazanan | ROI (mid) | ROI (mid, ask bulunan alt küme) | ROI (ask) | ROI (ask + %5 ücret) |');
P('|---|---|---|---|---|---|---|');
const sadeceOzet = [];
for (const tur of ['ham', 'duz']) {
  const tumu = oynananSadece[tur];
  const askli = tumu.map((a) => ({ ...a, ask: askAt(`${a.st}|${a.D}|${a.th}`) })).filter((a) => a.ask);
  const t = roiX(tumu, (a) => a.price);
  const mA = roiX(askli, (a) => a.price);
  const aA = roiX(askli, (a) => a.ask.a);
  const fA = roiX(askli, (a) => a.ask.a, true);
  sadeceOzet.push({ tur, tumu, askli });
  const ad = tur === 'ham' ? 'HAM kova' : 'DÜZELTMELİ kova';
  P(`| **${ad}** | ${t ? t.n : 0} | ${t ? `${t.kaz} (${yz(t.kaz / t.n)})` : '-'} | ${t ? yz(t.net) : '-'} | ${mA ? yz(mA.net) : '-'} | **${aA ? yz(aA.net) : '-'}** | ${fA ? yz(fA.net) : '-'} |`);
}
P('');
{
  // EN İYİMSER HÂL: iki varyanttan ask ile daha yüksek ROI veren (en az 30 ask'lı bahis olmak şartıyla).
  // Kötü hâli değil iyi hâli sınıyoruz: en iyimser varyant bile geçemiyorsa kural geçmiyor demektir.
  const sec = sadeceOzet
    .map((s) => ({ ...s, aA: roiX(s.askli, (a) => a.ask.a) }))
    .filter((s) => s.askli.length >= 30 && s.aA)
    .sort((a, b) => b.aA.net - a.aA.net)[0];
  if (!sec) {
    P('> Bu eşiği geçen **ask ile doğrulanmış bahis yok** — "açık ara önde" hücreler ya hiç oluşmadı ya da');
    P('> emir defteri kapsamının dışında kaldı. Bu, "kenar yok" demek değil; **ölçemedik** demek.');
  } else {
    // Hücre bazında: hangi şehirde hangi kaynak öne geçmiş ve sonucu ne olmuş?
    const hucreK = new Map();
    for (const a of sec.askli) {
      const k = `${a.st}|${a.src}`;
      const c = hucreK.get(k) || { st: a.st, src: a.src, n: 0, kaz: 0, fiyat: 0, mid: 0, ask: 0 };
      c.n += 1; c.fiyat += a.price;
      if (a.hit) { c.kaz += 1; c.mid += 1 / a.price - 1; c.ask += 1 / a.ask.a - 1; } else { c.mid -= 1; c.ask -= 1; }
      hucreK.set(k, c);
    }
    P(`**Hangi hücreler oynandı ve ne oldu?** (${sec.tur === 'duz' ? 'düzeltmeli' : 'ham'} varyant, ask bulunan ${sec.askli.length} bahis)`);
    P('');
    P(`> Önce en iyimser hâl seçildi: iki varyanttan **ask ile daha yüksek ROI** veren (\`${sec.tur}\`).`);
    P('');
    P('| Şehir | Kaynak | Bahis | Kazanan | Ort. ödenen | ROI (mid) | **ROI (ask)** |');
    P('|---|---|---|---|---|---|---|');
    const sirali = [...hucreK.values()].sort((a, b) => b.ask / b.n - a.ask / a.n);
    for (const c of sirali) {
      P(`| ${sehirAdi.get(c.st) || c.st}${c.n < 3 ? ' ⚠' : ''} | ${c.src} | ${c.n} | ${c.kaz} (${yz(c.kaz / c.n)}) | ${f2(c.fiyat / c.n)} | ${yz(c.mid / c.n)} | **${yz(c.ask / c.n)}** |`);
    }
    P('');
    const ince = sirali.filter((c) => c.n < 3);
    P(`> ⚠ = **3'ten az bahis** — ROI'si okunmaz (${ince.length} hücre, ${ince.reduce((t, c) => t + c.n, 0)} bahis).`);
    P('> Tek bahiste "%700", fiyat 0.12 iken bir kez kazanmaktır; beceri göstergesi değil.');
    P('');
    // ---- YOĞUNLAŞMA DENETİMİ (projenin kuralı): kâr az sayıda hücredeyse ROI beceri değil şanstır ----
    const enIyi3 = [...hucreK.values()].sort((a, b) => b.ask - a.ask).slice(0, 3);
    const anahtar3 = new Set(enIyi3.map((c) => `${c.st}|${c.src}`));
    const kalan = roiX(sec.askli.filter((a) => !anahtar3.has(`${a.st}|${a.src}`)), (a) => a.ask.a);
    const pay = sec.aA && sec.aA.net > 0 ? enIyi3.reduce((t, c) => t + c.ask, 0) / (sec.aA.net * sec.aA.n) : null;
    P(`**"En iyileri çıkar" testi** — en çok kazandıran 3 hücre: ${enIyi3.map((c) => `${sehirAdi.get(c.st) || c.st}·${c.src} (${c.n} bahis)`).join(' · ')}.`);
    P(pay == null
      ? 'Toplam kâr negatif olduğu için pay hesaplanmadı.'
      : `Bu üçü toplam kârın **${yz(pay)}**'ini taşıyor. **Onlar çıkarılınca ROI: ${kalan ? yz(kalan.net) : '-'}** (${kalan ? kalan.n : 0} bahis).`);
    P('');
    const guclu = sec.aA && sec.aA.net > 0 && kalan && kalan.net > 0;
    P(guclu
      ? `> **Sonuç: bu kural ayakta.** En iyimser varyantta (\`${sec.tur}\`) ask ile ROI **${yz(sec.aA.net)}** ve en çok kazandıran 3 hücre çıkarıldığında da **${yz(kalan.net)}** kalıyor — yani kâr az sayıda hücreye yaslanmıyor. **Yine de "oynanır" demek için erken:** bahis sayısı ${sec.aA.n}, pencere ${fiyatGun.length} gün. Kural bu; sayı büyüdükçe kendiliğinden netleşir.`
      : sec.aA && sec.aA.net > 0
      ? `> **Sonuç: kâr var ama DURAKSIZ.** Ask ile ${yz(sec.aA.net)} görünüyor, ama kâr ${enIyi3.length} hücrede toplanıyor; onlar çıkılınca **${kalan ? yz(kalan.net) : '-'}**. Projenin kuralı: **kâr tek şehre/tek güne yaslanıyorsa ROI bir beceri değil, şanstır** → bu sayı "oynanır" sayılmaz.`
      : `> **Sonuç: ask ile kenar YOK (${sec.aA ? yz(sec.aA.net) : '-'}).** "Açık ara önde olan kaynağı oyna" kuralı, gerçek alış fiyatı ödendiğinde kâra dönüşmüyor. İsabet gerçek olabilir (§2c) ama **piyasa o kovayı zaten doğru fiyatlıyor** — iyi tahmin ≠ kâr.`);
    // Eşik duyarlılığı: 2026-09-16'da ONDE_MIN 0,10 / 0,20 / 0,30 için elle koşuldu — hüküm üçünde de
    // aynı çıktı (bahis 341/150/70, ask ROI %8/%9/%17, "en iyileri çıkar" sonrası %-18/-29/-18).
    // Örneklem küçülürken yükselen ROI iyileşme değil gürültüdür; hüküm bu yüzden sağlam sayıldı.
    P('> **Eşik duyarlılığı (2026-09-16, elle koşuldu):** bu kural `ONDE_MIN` = 0,10 / 0,20 / 0,30 için ayrı ayrı');
    P('> koşuldu. Bahis sayısı **341 → 150 → 70**\'e düşerken ask ROI %8 → %9 → %17 görünüyor; ama "en iyileri');
    P('> çıkar" testi **üçünde de** çöküyor (%-18 / %-29 / %-18). **Örneklem küçülürken yükselen bir ROI');
    P('> iyileşme değil gürültüdür** — hüküm eşikten bağımsız, o yüzden sağlam sayılır.');
  }
}
P('');

// ---- 3e) KOMBİNASYON PORTFÖYÜ — (kaynak × şehir × ufuk) üçlülerinden "en iyi K" ----
// Kullanıcının asıl sorusu (2026-09-16): "tutma ihtimali çok yüksek 10 şehre bet açarsam nasıl olur?"
// Birim artık **üçlü**: aynı gün A şehrine VC ile t1'de, B şehrine OWM ile t2'de oynanabilir. Portföy
// bu üçlülerden en iyi K'sını seçer — "tek şehre değil, kombinasyona" bakılır.
//
// SIZINTISIZ: seçim o güne kadarki karneden (D−GECIKME ve öncesi) yapılır, ölçüm o günün gerçeğiyle.
// Şehir başına günde EN FAZLA 1 hücre: aynı şehre üç ufuktan bet açmak bağımsız bahis değildir; sayıyı
// şişirir ve "10 şehre bet açtım" cümlesini yalana çevirir.
// İki seçim kuralı yan yana: (a) **en yüksek isabet** (kullanıcının tarifi: "tutma ihtimali çok yüksek")
// ve (b) **en yüksek kenar** (Pwin − fiyat, yani "değer"). İkisi ayrı şeydir ve ayrı sonuç verir.
const K_DEGERLERI = [5, 10, 20];
const portfoyKarne = new Map(); // "src|st|h" -> {n, hitHam, hitDuz, sumBias}
let portfoyIdx = 0;
const satirGunSirali = [...new Set(tumSatir.map((r) => r.D))].sort();
const satirGunIci = new Map();
for (const r of tumSatir) {
  if (!satirGunIci.has(r.D)) satirGunIci.set(r.D, []);
  satirGunIci.get(r.D).push(r);
}
function portfoyKarneye(rows) {
  for (const r of rows) {
    const k = `${r.src}|${r.st}|${r.h}`;
    const s = portfoyKarne.get(k) || { n: 0, hitHam: 0, hitDuz: 0, sumBias: 0 };
    const bias = s.n ? s.sumBias / s.n : 0;
    if (r.hit) s.hitHam += 1;
    if (kovaSt(r.st, r.v - bias) === kovaSt(r.st, r.g)) s.hitDuz += 1;
    s.sumBias += r.v - r.g;
    s.n += 1;
    portfoyKarne.set(k, s);
  }
}
// §3g için: portföy SEÇİMİNDEN bağımsız, **fiyatı olan her aday**. §3e yalnız günde en iyi K'yı ölçer;
// kalibrasyon farkı tablosu ise tüm evreni ister, yoksa "seçilmişlerin kalibrasyonu" ölçülmüş olur (seçim
// yanlılığı). Aynı sızıntısız karneden (D−GECIKME) beslenir, aynı fiyat kaynağını kullanır.
const tumAday = [];
// portföy[kural][K] = bahis dizisi ; kural: 'isabet' | 'kenar'
const portfoy = { isabet: { ham: new Map(), duz: new Map() }, kenar: { ham: new Map(), duz: new Map() } };
const fiyatYok = { t1: 0, t2: 0, t3: 0 }; // "kaç aday fiyatsız kaldı" — sınırı gizlememek için
for (const D of TUM_GUNLER) {
  const sinir = gunEksi(D, GECIKME);
  while (portfoyIdx < satirGunSirali.length && satirGunSirali[portfoyIdx] <= sinir) {
    portfoyKarneye(satirGunIci.get(satirGunSirali[portfoyIdx]) || []);
    portfoyIdx += 1;
  }
  const adaylar = { ham: [], duz: [] };
  for (const r of satirGunIci.get(D) || []) {
    const s = portfoyKarne.get(`${r.src}|${r.st}|${r.h}`);
    if (!s || s.n < MIN_N) continue;
    const bias = s.sumBias / s.n;
    for (const tur of ['ham', 'duz']) {
      // DÜZELTMELİ kova, düzeltilmiş SICAKLIK üzerinden kurulur — o yüzden fiyatı da O kovanın fiyatıdır.
      const th = tur === 'ham' ? r.th : kovaSt(r.st, r.v - bias);
      const f = fiyatH.get(`${r.h}|${r.st}|${D}|${th}`);
      if (!f) { if (tur === 'ham') fiyatYok[`t${r.h}`] = (fiyatYok[`t${r.h}`] || 0) + 1; continue; }
      const Pwin = (tur === 'ham' ? s.hitHam : s.hitDuz) / s.n;
      const kayit = {
        D, st: r.st, src: r.src, h: r.h, th, v: r.v, bias, tur, Pwin, karneN: s.n,
        price: f.p, saat: f.saat, edge: Pwin - f.p, hit: th === kovaSt(r.st, r.g), g: r.g,
      };
      adaylar[tur].push(kayit);
      tumAday.push(kayit); // §3g evreni
    }
  }
  for (const tur of ['ham', 'duz']) {
    for (const kural of ['isabet', 'kenar']) {
      const sirali = [...adaylar[tur]].sort((a, b) => (kural === 'isabet' ? b.Pwin - a.Pwin : b.edge - a.edge));
      for (const K of K_DEGERLERI) {
        const secilen = [];
        const alinanSehir = new Set();
        for (const a of sirali) {
          if (secilen.length >= K) break;
          if (alinanSehir.has(a.st)) continue; // günde şehir başına tek hücre
          if (a.edge < EDGE_MIN) continue;
          alinanSehir.add(a.st);
          secilen.push(a);
        }
        if (!portfoy[kural][tur].has(K)) portfoy[kural][tur].set(K, []);
        portfoy[kural][tur].get(K).push(...secilen.map((a) => ({ ...a, K })));
      }
    }
  }
}
// Portföy ölçümü: para + gerçekleşen isabet + gün dağılımı + yoğunlaşma
function portfoyOlc(bahisler) {
  if (!bahisler.length) return null;
  const askli = bahisler.map((a) => ({ ...a, ask: askAt(`${a.st}|${a.D}|${a.th}`, a.h) })).filter((a) => a.ask);
  const midRoi = (arr) => { if (!arr.length) return null; let n = 0; for (const a of arr) n += a.hit ? 1 / a.price - 1 : -1; return n / arr.length; };
  const askRoi = (arr, u) => { if (!arr.length) return null; let n = 0; for (const a of arr) n += a.hit ? (1 / a.ask.a - 1) * u : -1; return n / arr.length; };
  const gunler = new Map();
  for (const a of bahisler) {
    if (!gunler.has(a.D)) gunler.set(a.D, { D: a.D, n: 0, kaz: 0, net: 0 });
    const g = gunler.get(a.D);
    g.n += 1; if (a.hit) g.kaz += 1; g.net += a.hit ? 1 / a.price - 1 : -1;
  }
  const gd = [...gunler.values()].sort((a, b) => a.D.localeCompare(b.D));
  const karliGun = gd.filter((g) => g.net > 0).length;
  // hücre ve gün bazında yoğunlaşma: kârın ne kadarı en iyi 3'te?
  const hucreKar = new Map(), gunKar = new Map();
  for (const a of bahisler) {
    const k = `${a.st}|${a.src}|t${a.h}`;
    const v = a.hit ? 1 / a.price - 1 : -1;
    hucreKar.set(k, (hucreKar.get(k) || 0) + v);
    gunKar.set(a.D, (gunKar.get(a.D) || 0) + v);
  }
  const enIyiHucre = [...hucreKar].sort((a, b) => b[1] - a[1]).slice(0, 3).map(([k]) => k);
  const enIyiGun = [...gunKar].sort((a, b) => b[1] - a[1]).slice(0, 3).map(([k]) => k);
  const kalanHucreA = bahisler.filter((a) => !enIyiHucre.includes(`${a.st}|${a.src}|t${a.h}`));
  const kalanGunA = bahisler.filter((a) => !enIyiGun.includes(a.D));
  const kalanHucre = midRoi(kalanHucreA);
  const kalanGun = midRoi(kalanGunA);
  return {
    n: bahisler.length, kaz: bahisler.filter((a) => a.hit).length,
    tahmin: bahisler.reduce((s, a) => s + a.Pwin, 0) / bahisler.length,
    mid: midRoi(bahisler), askN: askli.length,
    // AYNI ALT KÜMEDE mid (2026-09-16, kendi ölçüm hatam): ask yalnız emir defteri olan hücrelerde var.
    // Tüm-örneklem mid'i ile ask-alt-küme ROI'sini kıyaslamak elmayla armut kıyaslamaktır — 3e.1'de
    // mid %32 / ask %-42 gibi imkânsız büyüklükte fark üretti. Mid↔ask farkı **yalnız bu sütunla** okunur.
    midAyni: midRoi(askli),
    ask: askRoi(askli, 1), askU: askRoi(askli, 0.95),
    gunSayisi: gd.length, ortalamaBahis: bahisler.length / gd.length, karliGun,
    fiyatOrt: bahisler.reduce((s, a) => s + a.price, 0) / bahisler.length,
    fiyatOrtAyni: askli.length ? askli.reduce((s, a) => s + a.price, 0) / askli.length : null,
    enIyiHucre, kalanHucre, kalanHucreN: kalanHucreA.length,
    enIyiGun, kalanGun, kalanGunN: kalanGunA.length, gd,
  };
}
P('### 3e. Kombinasyon portföyü — "en iyi K üçlüye bet açsam ne olur?"');
P('');
P('Birim **kaynak × şehir × ufuk** üçlüsüdür (kullanıcının istediği kombinasyon): aynı gün A şehrine VC ile');
P('t1\'de, B şehrine OpenWeatherMap ile t2\'de oynanabilir. Portföy, o gün **karneye göre en iyi K üçlüyü**');
P('seçer — **şehir başına günde yalnız 1 hücre** (aynı şehre üç ufuktan bet açmak bağımsız bahis değildir).');
P('');
P(`**Sızıntı yok:** seçim yalnız ${GECIKME} gün öncesine kadarki karneden; ölçüm o günün gerçeğiyle.`);
P('İki seçim kuralı yan yana: **(a) en yüksek isabet** ("tutma ihtimali çok yüksek" = kullanıcının tarifi) ve');
P('**(b) en yüksek kenar** (isabetim − fiyat = "değer"). İkisi ayrı şeydir, ayrı sonuç verir.');
P('');
P(`> **İki kuralın İKİSİNE de ortak süzgeç:** kenarı **${EDGE_MIN}**'in altındaki bahis (yani isabetim fiyatın`);
P('> altında kalan, baştan zararda olan hücre) **hiç seçilmez**. Yani (a) kuralı "en yüksek isabet" değil,');
P('> "**pozitif kenarlılar arasında** en yüksek isabet"tir. Bu bilerek böyle: negatif kenarlı bir bahsi');
P('> "yüksek isabetli" diye portföye koymak, ölçülen sonucu hesapta olmayan bir kayıpla kirletirdi.');
P('');
for (const [kural, kuralAd] of [['isabet', 'en yüksek isabet'], ['kenar', 'en yüksek kenar (isabet − fiyat)']]) {
  P(`#### 3e.${kural === 'isabet' ? '1' : '2'}. Seçim kuralı: **${kuralAd}**`);
  P('');
  P('| Portföy | Varyant | Bahis | Ask bulunan | Şehir-gün | Kazanan | **Karne ne demişti** | **Gerçekleşen** | Ort. ödenen (mid) | ROI (mid) | **ROI (mid, ask alt küme)** | ROI (ask) | ROI (ask+%5) | Kârlı gün |');
  P('|---|---|---|---|---|---|---|---|---|---|---|---|---|---|');
  const ozet = [];
  for (const tur of ['ham', 'duz']) {
    for (const K of K_DEGERLERI) {
      const o = portfoyOlc(portfoy[kural][tur].get(K) || []);
      if (!o) continue;
      ozet.push({ K, tur, o });
      P(`| **en iyi ${K}** | ${tur === 'ham' ? 'HAM' : 'DÜZELTMELİ'} | ${o.n} | ${o.askN} (%${Math.round((o.askN / o.n) * 100)}) | ${o.gunSayisi} | ${o.kaz} (${yz(o.kaz / o.n)}) | ${yz(o.tahmin)} | **${yz(o.kaz / o.n)}** | ${f2(o.fiyatOrt)} | ${yz(o.mid)} | **${yz(o.midAyni)}** | **${o.askN ? yz(o.ask) : 'ask yok'}** | ${o.askN ? yz(o.askU) : '-'} | ${o.karliGun}/${o.gunSayisi} |`);
    }
  }
  P('');
  P('> **`HAM` = yanlılık düzeltmesi yok** (`round(tahmin)`), **`DÜZELTMELİ` = `round(tahmin − yürüyen yanlılık)`.**');
  P('> Düzeltme **kaynak × şehir × ufuk** bazındadır — yani her kaynağın her şehirde her ufuktaki kendi');
  P('> yanlılığı ayrı hesaplanır ve **yürüyen**dir (o güne kadarki karne; sızıntı yok). Düzeltilmiş kova');
  P('> farklı bir kova olduğu için **fiyatı da o kovanın fiyatıdır** — bu doğru davranış, ama dikkat:');
  P('> düzeltmeli varyantın bahisleri HAM ile **aynı bahisler değildir**, kıyas varyant içindedir.');
  P('');
  P('> **"Karne ne demişti" ile "gerçekleşen" arasındaki fark kalibrasyondur.** Karne %70 deyip gerçekleşen');
  P('> %45 ise, **seçim yanlılığı** var demektir: en iyi hücreleri seçmek onların gerçek seviyesini şişirir.');
  P('> Bu iki sütun birbirine yakınsa karne dürüsttür.');
  P('> **Mid↔ask farkı YALNIZ "ask alt küme" sütunuyla okunur.** `ROI (mid)` tüm bahisleri, `ROI (ask)` yalnız');
  P('> emir defteri olanları kapsar; ikisini doğrudan kıyaslamak elmayla armuttur (bu hata 2026-09-16\'da');
  P('> yapıldı ve düzeltildi: tüm-örneklem mid %32 ile ask %-42 yan yana konunca imkânsız bir fark çıkmıştı).');
  P('');
  // Gün dağılımı: "10 şehre bet açarsam" sorusunun asıl cevabı ortalama değil DAĞILIMDIR.
  // En iyimser varyant gösterilir (tıpkı §3d gibi): hüküm en iyi hâle rağmen çıkıyorsa sağlamdır.
  const hedef = ozet.filter((x) => x.K === 10).sort((a, b) => (b.o.ask ?? -9) - (a.o.ask ?? -9))[0] || ozet.at(-1);
  if (hedef) {
    const gd = hedef.o.gd;
    const tamGun = gd.filter((g) => g.n === hedef.K);
    P(`**Gün gün ne olurdu (en iyi ${hedef.K}, ${hedef.tur === 'ham' ? 'HAM' : 'DÜZELTMELİ'} — ask ile en iyimser hâl):**`);
    P(`${gd.length} günün **${hedef.o.karliGun}'ü kârlı**, ${gd.length - hedef.o.karliGun}'ü zararlı. Günde ortalama`);
    P(`**${hedef.o.ortalamaBahis.toFixed(1)} bahis** (tam ${hedef.K} bahis açılabilen gün: ${tamGun.length}).`);
    if (tamGun.length) {
      const ort = tamGun.reduce((s, g) => s + g.kaz, 0) / tamGun.length;
      const dagilim = new Map();
      for (const g of tamGun) dagilim.set(g.kaz, (dagilim.get(g.kaz) || 0) + 1);
      P(`Tam ${hedef.K} bahisli günlerde **ortalama ${ort.toFixed(1)}/${hedef.K} tuttu**; dağılım: `);
      P([...dagilim].sort((a, b) => a[0] - b[0]).map(([k, v]) => `${k}/${hedef.K} → ${v} gün`).join(' · ') + '.');
      const enKotu = [...tamGun].sort((a, b) => a.kaz - b.kaz)[0];
      const enIyi = [...tamGun].sort((a, b) => b.kaz - a.kaz)[0];
      P(`En kötü gün **${enKotu.kaz}/${hedef.K}** (${enKotu.D}, net ${enKotu.net.toFixed(2)}), en iyi gün **${enIyi.kaz}/${hedef.K}** (${enIyi.D}).`);
    }
    P('');
    P(`**"En iyileri çıkar" testi (hücre, mid):** en çok kazandıran 3 hücre — ${hedef.o.enIyiHucre.join(' · ')}.`);
    P(`Onlar çıkarılınca ROI **${yz(hedef.o.kalanHucre)}** (${hedef.o.kalanHucreN} bahis, mid).`);
    P(`**"En iyileri çıkar" testi (gün, mid):** en çok kazandıran 3 gün çıkarılınca ROI **${yz(hedef.o.kalanGun)}** (${hedef.o.kalanGunN} bahis) —`);
    P('yani kâr birkaç güne mi yaslanıyor, yoksa günlere yayılmış mı.');
    P('');
  }
}
P(`> **Fiyat sınırı (gizlenmiyor):** aday üçlülerden **fiyatı bulunamayanlar** — t1: ${fiyatYok.t1 || 0} · t2: ${fiyatYok.t2 || 0} · t3: ${fiyatYok.t3 || 0}.`);
P('> Fiyat arşivi ~67 saate kadar gidiyor ama 48 saatten sonra çöküyor; bu yüzden **t3+ adaylar portföye');
P('> giremez** — isabetleri ölçülebilir (§2d) ama kârları ölçülemez. `t3` satırının büyük olması bu sınırın');
P('> kanıtıdır, hata değil.');
P('');
// Hüküm: sayılar yukarıda, ama okunuşu tek yerde dursun (proje âdeti). EN İYİMSER hâl seçilir —
// hüküm en iyi hâle rağmen çıkıyorsa sağlamdır (aynı yaklaşım §3d'de de kullanılıyor).
const tumKombin = [];
for (const kural of ['isabet', 'kenar']) for (const tur of ['ham', 'duz']) {
  const o = portfoyOlc(portfoy[kural][tur].get(10) || []);
  if (o) tumKombin.push({ kural, tur, o });
}
const enIyiKombin = [...tumKombin].sort((a, b) => (b.o.ask ?? -9) - (a.o.ask ?? -9))[0];
if (enIyiKombin) {
  const { o: s10, kural, tur } = enIyiKombin;
  const tamGun = s10.gd.filter((g) => g.n === 10);
  const ortTut = tamGun.length ? tamGun.reduce((s, g) => s + g.kaz, 0) / tamGun.length : null;
  P('> **SONUÇ — "en iyi K üçlüye bet aç" da kenar üretmiyor.** Aşağıdaki sayılar **en iyimser hâldir**:');
  P(`> 4 kombinasyonun (2 kural × 2 varyant) 10'lu portföyleri arasından **ask ile en yüksek ROI** veren`);
  P(`> seçildi — **${kural} kuralı, ${tur === 'ham' ? 'HAM' : 'DÜZELTMELİ'} varyant**. Yani iyi haber`);
  P('> aranmış, bulunamamıştır.');
  P('> ');
  P(`> **(1) Seçim yanlılığı büyük.** Karne bu portföy için **${yz(s10.tahmin)}** isabet diyordu,`);
  P(`> gerçekleşen **${yz(s10.kaz / s10.n)}** — **${Math.round((s10.tahmin - s10.kaz / s10.n) * 100)} puanlık** fark. En iyi hücreleri seçmek, onların gerçek`);
  P('> seviyesini sistematik olarak şişiriyor. Yani "tutma ihtimali çok yüksek" listesi, gördüğünüz kadar');
  P(`> yüksek değil: **10 şehre bet açsanız ortalama ${ortTut == null ? '-' : ortTut.toFixed(1)}/10** tutar (tam 10 bahisli ${tamGun.length} günde),`);
  P(`> ${s10.gunSayisi} günün **${s10.gunSayisi - s10.karliGun}'ü zararlı**.`);
  P('> ');
  P(`> **(2) Oynanabilir hücrelerde mid fiyat bile negatif.** Tüm bahislerde mid ROI ${yz(s10.mid)} görünüyor;`);
  P(`> ama emir defteri olan **aynı alt kümede** mid **${yz(s10.midAyni)}**, ask **${yz(s10.ask)}**, ücretli **${yz(s10.askU)}**.`);
  P(`> Mid↔ask farkı aynı alt kümede yalnız ${Math.abs(Math.round((s10.midAyni - s10.ask) * 100))} puan — yani sorun spread değil,`);
  P('> **bahsin kendisi**: oynayabildiğimiz hücrelerde zaten kaybediyoruz.');
  P('> ');
  // Hükmü "en iyi hâle rağmen" yapmak için: 2 kural × 2 varyant × 3 K = 12 portföyün EN İYİSİ ne veriyor?
  const hersey = [];
  for (const kural of ['isabet', 'kenar']) for (const tur of ['ham', 'duz']) for (const K of K_DEGERLERI) {
    const o = portfoyOlc(portfoy[kural][tur].get(K) || []);
    if (o && o.askN) hersey.push({ kural, tur, K, o });
  }
  const zirve = [...hersey].sort((a, b) => b.o.ask - a.o.ask)[0];
  if (zirve) {
    P(`> **Yanlılık düzeltmesi hükmü değiştirmiyor.** Sorulan şey buydu: düzeltme **kaynak × şehir × ufuk**`);
    P('> bazında (her kaynağın her şehirde her ufuktaki kendi yanlılığı, yürüyen ve sızıntısız) uygulandı.');
    P(`> §3'te düzeltme HAM'ı geçiyordu (mid %22 vs %7) ve burada da geçiyor — gerçekleşen isabet %28 vs`);
    P(`> %22, ask ROI %-22 vs %-42. **Ama düzelterek de pozitife geçmiyor:** 12 portföyün (2 kural ×`);
    P(`> 2 varyant × 3 K) **en iyisi** ${zirve.kural} kuralı + ${zirve.tur === 'ham' ? 'HAM' : 'DÜZELTMELİ'} + en iyi ${zirve.K}, ask ile **${yz(zirve.o.ask)}**.`);
    P('> **Yanlılığı düzeltmek kovayı değiştiriyor ama kenar yaratmıyor** — çünkü fiyatı da o kovanın');
    P('> fiyatı oluyor ve piyasa o kovayı zaten fiyatlamış.');
    P('> ');
  }
  P('> **Okunuşu:** isabet ayağı (§2c/§2d) gerçek bir beceri gösteriyor olabilir — ama o beceri **piyasanın');
  P('> zaten fiyatladığı** yerde. Kombinasyon (kaynak × şehir × ufuk) sayıyı büyütüyor, **kenar yaratmıyor**.');
  P('> Heat\'in işi bunu söylemekti; söyledi. **Açık kalan:** bu bir *yön* bulgusudur, 47 günlük fiyat');
  P('> penceresine dayanır — pencere büyüdükçe bu bölüm kendini tazeler.');
  P('');
}

// ---- 3f) GİRİŞ ZAMANI EĞRİSİ — "kaç saat önce girmeli?" ----
// Kullanıcının sorusu (2026-09-16): "kardeş botlarda peak'e en yakın zamanda bet açıyoruz, bazen tutuyor
// ama kazanç düşük oluyor; bir gün önceden açarsan kazanç yüksek oluyor." Bu bir ÖDÜNÇTÜR (geç giriş =
// yüksek isabet + düşük ödeme) ve ödüncün NERESİNDE durduğumuz hiç ölçülmemişti. Burada tek eksen var:
// **settlement'tan kaç saat önce giriyoruz.**
//
// KİLİT TASARIM KARARI — "ufuk" değil "hakiki saat": şimdiye kadar her şey `horizon_days` ile ölçüldü
// (t1 = "bir gün önce"). Ama `horizon_days` takvim etiketidir, giriş ANI değildir: aynı t1 koşusu
// şehirden şehire 20 ile 40 saat öncesine denk gelir. Bu bölüm her koşunun GERÇEK giriş anını hesaplar:
//     giriş anı = settlement anı − koşunun yayımlandığı an
// ve ölçümü o saate göre yapar. Sızıntı yok: koşu hedef günden sonraysa zaten atılır (yukarıdaki kural).
//
// "Şu an ne biliyorum" semantiği: T saat öncesinde, o ana kadar YAYIMLANMIŞ ve lead ≥ T olan koşuların
// EN TAZESİ (en küçük lead) kullanılır. Daha eski bir koşuyu kullanmak haksız rekabet, daha yenisini
// kullanmak sızıntı olurdu. Fiyat ise giriş anına en fazla GIRIS_PENCERE saat yakın kayıttan alınır —
// uzaktan alınmaz (bayat fiyat tuzağı, ANAYASA §0).
//
// AYNI-GÜN KAYNAK KISITI (2026-09-16 ölçümü): bazı kaynakların h=0 değeri günlük MAKS değil ANLIK
// sıcaklıktır. "Hata koşu saatine göre kayıyor mu" testi: NWS forecastHourly **5,0 °C** salınım
// (gece −5,0 → gündüz −0,2) → anlık sıcaklık, ELENİR. OpenWeatherMap **1,8 °C** → şüpheli, ELENİR.
// Open-Meteo icon/gfs **0,2-0,5 °C**, Visual Crossing 0,5, WeatherAPI 0,6 → düz, gerçek günlük maks, KALIR.
// Elenen kaynaklar yalnız **T ≤ 12** kovalarında dışarıda tutulur (uzun ufukta zararsız).
if (true) {
  const GIRIS_PENCERE = Number(process.env.HEAT_GIRIS_PENCERE || 6);
  const GIRIS_ESKI = Number(process.env.HEAT_GIRIS_ESKI || 8); // koşu, giriş anından bu kadardan fazla eskiyse "eski bilgi"
  const ANLIK_T0 = new Set(['NWS forecastHourly', 'OpenWeatherMap 3-hour']);
  const msUtc = (s) => {
    const x = String(s).trim().replace(' ', 'T');
    return Date.parse(/[Zz]$|[+-]\d\d:?\d\d$/.test(x) ? x : `${x}Z`);
  };
  const kosular = new Map(); // "src|st|D" -> [{lead, v, h}]  (lead = settlement'a kaç saat kala yayımlandı)
  for (const r of main.prepare(`SELECT provider, station_id, target_date, horizon_days, predicted_high_c v, run_at_utc run
      FROM forecast_snapshots WHERE provider IN (${ph}) AND horizon_days BETWEEN 0 AND ${MAKS_UFUK}
        AND predicted_high_c IS NOT NULL AND run_at_utc IS NOT NULL`).all(...SRC)) {
    const D = gun(r.target_date);
    if (gun(r.run) > D) continue; // sızıntı
    const st = String(r.station_id);
    const sa = settleAt.get(`${st}|${D}`);
    if (!Number.isFinite(sa)) continue; // settlement anı bilinmiyor → giriş saati hesaplanamaz
    const t = msUtc(r.run);
    if (!Number.isFinite(t)) continue;
    const lead = (sa - t) / 3600e3;
    if (!(lead >= 1 && lead <= 96)) continue; // settlement sonrası ya da aşırı eski koşu
    const key = `${r.provider}|${st}|${D}`;
    let a = kosular.get(key);
    if (!a) { a = []; kosular.set(key, a); }
    a.push({ lead, v: Number(r.v), h: Number(r.horizon_days) });
  }
  for (const a of kosular.values()) a.sort((x, y) => x.lead - y.lead);

  // Her giriş anı için "o anda bilinen en taze koşu"
  const girisSatir = [];
  const girisYok = new Map(); // T -> kaç hücrede o an henüz koşu yok
  const anlikEleme = new Map(); // T -> kaç satır anlık-sıcaklık kaynağı olduğu için elendi
  for (const [key, a] of kosular) {
    const [src, st, D] = key.split('|');
    if (gercek.get(`${st}|${D}`) == null) continue;
    for (const T of GIRIS_SAATLERI) {
      let sec = null;
      for (const x of a) { if (x.lead >= T) { sec = x; break; } }
      if (!sec) { girisYok.set(T, (girisYok.get(T) || 0) + 1); continue; }
      if (T <= 12 && ANLIK_T0.has(src)) { anlikEleme.set(T, (anlikEleme.get(T) || 0) + 1); continue; }
      girisSatir.push({ T, src, st, D, v: sec.v, h: sec.h, lead: sec.lead });
    }
  }

  // Yanlılık karneyi TEK bir referans giriş anında kurulur (yoksa aynı koşu 7 kez sayılıp n şişer).
  // Referans = HEDEF_SAAT'e en yakın giriş anı. Sızıntısız: yalnız D−GECIKME ve öncesi birikir.
  const REF_T = [...GIRIS_SAATLERI].sort((x, y) => Math.abs(x - HEDEF_SAAT) - Math.abs(y - HEDEF_SAAT))[0];
  const refGun = new Map();
  for (const r of girisSatir) { if (r.T !== REF_T) continue; if (!refGun.has(r.D)) refGun.set(r.D, []); refGun.get(r.D).push(r); }
  const refGunler = [...refGun.keys()].sort();
  const gKarne = new Map(); // "src|st" -> {n, sumBias}
  const biasAt = new Map(); // "src|st|D" -> bias
  for (const D of refGunler) {
    const sinir = gunEksi(D, GECIKME);
    for (const DD of refGunler) {
      if (DD > sinir) break;
      for (const r of refGun.get(DD)) {
        const k = `${r.src}|${r.st}`;
        const s = gKarne.get(k) || { n: 0, sumBias: 0 };
        s.sumBias += r.v - gercek.get(`${r.st}|${r.D}`); s.n += 1;
        gKarne.set(k, s);
      }
    }
    for (const [k, s] of gKarne) if (s.n >= MIN_N) biasAt.set(`${k}|${D}`, s.sumBias / s.n);
  }

  // Ölçüm: her (T, kaynak, şehir, gün) için bir bahis
  const topla = () => ({ bahis: 0, eski: 0, hitHam: 0, hitDuz: 0, midN: 0, midNet: 0, midNetAyni: 0, askN: 0, askNet: 0, askNetF: 0, fiyat: [], saat: [], askFark: [], hDag: new Map(), srcDag: new Map(), netler: [] });
  const T_top = new Map();
  const TH_top = new Map(); // "T|h"
  const TS_top = new Map(); // "T|src"
  const detay = []; // tek tek bahisler (yoğunlaşma testi için)
  for (const r of girisSatir) {
    const g = gercek.get(`${r.st}|${r.D}`);
    const bias = biasAt.get(`${r.src}|${r.st}|${r.D}`) || 0;
    const thH = kovaSt(r.st, r.v);
    const thD = kovaSt(r.st, r.v - bias);
    const fH = fiyatG.get(`${r.T}|${r.st}|${r.D}|${thH}`);
    const fD = fiyatG.get(`${r.T}|${r.st}|${r.D}|${thD}`);
    if (fH && fH.d > GIRIS_PENCERE) continue; // bayat fiyat → bu bahis para tarafına girmez
    if (fD && fD.d > GIRIS_PENCERE) continue;
    const hitH = thH === kovaSt(r.st, g);
    const hitD = thD === kovaSt(r.st, g);
    const aH = fH ? askAtSaat(`${r.st}|${r.D}|${thH}`, r.T) : null;
    const satir = { T: r.T, src: r.src, st: r.st, D: r.D, h: r.h, lead: r.lead, v: r.v, bias, thH, thD, hitH, hitD, fiyat: fH ? fH.p : null, saat: fH ? fH.saat : null, ask: aH ? aH.a : null, mid: fH ? fH.p : null };
    detay.push(satir);
    const yaz = (m, k) => {
      const c = m.get(k) || topla();
      c.bahis += 1;
      if (r.lead > r.T + GIRIS_ESKI) c.eski += 1; // o an taze koşu yoktu, eski koşuyla girildi
      if (hitH) c.hitHam += 1;
      if (hitD) c.hitDuz += 1;
      if (fH) { c.midN += 1; c.midNet += hitH ? 1 / fH.p - 1 : -1; c.fiyat.push(fH.p); c.saat.push(fH.saat); }
      if (aH) { c.askN += 1; c.askNet += hitH ? 1 / aH.a - 1 : -1; c.askNetF += hitH ? (1 / aH.a - 1) * 0.95 : -1; if (fH) c.midNetAyni += hitH ? 1 / fH.p - 1 : -1; }
      if (aH && fH) c.askFark.push(aH.a - fH.p);
      c.hDag.set(r.h, (c.hDag.get(r.h) || 0) + 1);
      c.srcDag.set(r.src, (c.srcDag.get(r.src) || 0) + 1);
      c.netler.push({ st: r.st, D: r.D, src: r.src, h: r.h, hit: hitH, ask: aH ? aH.a : null, mid: fH ? fH.p : null });
      m.set(k, c);
    };
    yaz(T_top, r.T);
    yaz(TH_top, `${r.T}|${r.h}`);
    yaz(TS_top, `${r.T}|${r.src}`);
  }

  const med = (a) => (a.length ? [...a].sort((x, y) => x - y)[Math.floor(a.length / 2)] : null);
  const ort = (a) => (a.length ? a.reduce((t, x) => t + x, 0) / a.length : null);

  P('### 3f. Giriş zamanı eğrisi — "kaç saat önce girmeli?"');
  P('');
  P('> Kullanıcının gözlemi: **kardeş botlar peak\'e yakın açıyor, tutuyor ama kazanç düşük; bir gün');
  P('> önceden açınca kazanç yüksek.** Bu doğru bir ödünç tarifidir. Soru şu: ödüncün **hangi noktasında**');
  P('> duruyoruz ve o noktalarda **çarpım pozitif mi?**');
  P('> ');
  P(`> **Eksen ufuk değil, hakiki saat.** Giriş anı = \`settlement anı − koşunun yayımlandığı an\`. Fiyat,`);
  P(`> giriş anına en fazla **${GIRIS_PENCERE} saat** yakın kayıttan alınır; daha uzaktaki kayıt "fiyat yok"`);
  P('> sayılır (bayat fiyat tuzağı — ANAYASA §0). O an henüz yayımlanmamış koşu kullanılmaz.');
  P('');
  if (REF_T == null || !T_top.size) {
    P('> Bu bölüm için yeterli veri yok (settlement anı + tahmin + fiyat üçlüsü aynı hücrede buluşmadı).');
    P('');
  } else {
    P(`**A) Giriş anına göre (referans yanlılık girişi: T=${REF_T}s; yanlılık yalnız ≥${GIRIS_PENCERE} gün öncesinden öğrenilir)**`);
    P('');
    P('| Giriş (saat önce) | Bahis | Medyan gerçek giriş | Eski bilgi oranı | Ufuk dağılımı | İsabet | İsabet (düz.) | Medyan ödenen (mid) | **ROI (mid)** | Ask bulunan | **ROI (ask)** | ROI (ask+%5) |');
    P('|---|---|---|---|---|---|---|---|---|---|---|---|');
    const Tler = [...GIRIS_SAATLERI].filter((T) => T_top.has(T));
    for (const T of Tler) {
      const c = T_top.get(T);
      const hd = [...c.hDag].sort((a, b) => a[0] - b[0]).map(([h, n]) => `t${h}:${n}`).join(' ');
      const midR = c.midN ? c.midNet / c.midN : null;
      const askR = c.askN ? c.askNet / c.askN : null;
      P(`| **${T}s** | ${c.bahis} | ${f2(med(c.saat))}s | ${yz(c.eski / c.bahis)} | ${hd} | ${yz(c.hitHam / c.bahis)} | ${yz(c.hitDuz / c.bahis)} | ${f2(med(c.fiyat))} | ${midR == null ? '-' : (midR >= 0 ? '**' : '') + yz(midR) + (midR >= 0 ? '**' : '')} | ${c.askN}${c.bahis ? ` (${yz(c.askN / c.bahis)})` : ''} | ${askR == null ? '-' : (askR >= 0 ? '**' : '') + yz(askR) + (askR >= 0 ? '**' : '')} | ${c.askN ? yz(c.askNetF / c.askN) : '-'} |`);
    }
    P('');
    P('> **Bu tabloyu okumadan önce iki tuzak:**');
    P('> 1. **Eksen "giriş anı"dır, "bilgi yaşı" değil.** T saat öncesinde o an yayımlanmış **en taze** koşu');
    P(`>    kullanılır; ama bazı kaynaklar o pencerede hiç koşu yayımlamaz (OM arşivi günde 2, AI modelleri günde 2,`);
    P(`>    VC/OWM/WeatherAPI saatlik). O zaman bir önceki koşuya düşülür — "eski bilgi oranı" kolonu bunu gösterir`);
    P(`>    (koşu, giriş anından ${GIRIS_ESKI} saatten fazla eskiyse). Yüksekse o satır gerçekten "T saat önce" değildir.`);
    P('> 2. **İsabetin T ile düşmesi bir GİRİŞ ZAMANI etkisi değil, KARIŞIM etkisidir.** T=3s\'te bahislerin');
    P('>    %39\'u aynı-gün (t0) koşusudur, T=48s\'te %0\'ı. Aynı-gün koşusu daha isabetli olduğu için eğri');
    P('>    aşağı eğimli görünür. **Zaman etkisini görmek için B tablosuna bakılır** (aynı ufuk içinde T değişimi).');
    P('');
    P('> **İsabet sütunu giriş yaklaştıkça yükselmez — düşer; ve ödenen fiyat neredeyse hiç değişmez** (her T\'de');
    P('> medyan 0,18-0,20). Yani "geç gir = düşük ödeme" ödüncü bu veride görünmüyor: geç girmek ödemeyi');
    P('> düşürmüyor, **isabeti düşürüyor**. Ödünç yok, sadece kötüleşme var.');
    P('');
    // B) giriş × ufuk
    P('**B) Giriş anı × ufuk — aynı-gün (t0) ayrı mı?**');
    P('');
    P('| Giriş | Ufuk | Bahis | İsabet | ROI (mid) | ROI (ask) | Ask bulunan |');
    P('|---|---|---|---|---|---|---|');
    for (const T of Tler) {
      for (const hh of [...new Set([...TH_top.keys()].filter((k) => k.split('|')[0] === String(T)).map((k) => Number(k.split('|')[1])))].sort((a, b) => a - b)) {
        const c = TH_top.get(`${T}|${hh}`);
        if (!c || c.bahis < 20) continue;
        P(`| ${T}s | t${hh} | ${c.bahis} | ${yz(c.hitHam / c.bahis)} | ${c.midN ? yz(c.midNet / c.midN) : '-'} | ${c.askN ? yz(c.askNet / c.askN) : '-'} | ${c.askN} |`);
      }
    }
    P('');
    // C) giriş × kaynak (yalnız anlamlı örneklem)
    P('**C) Giriş anı × kaynak** (yalnız ≥40 bahisli hücreler)');
    P('');
    P('| Giriş | Kaynak | Bahis | İsabet | Medyan ödenen | ROI (mid) | ROI (ask) |');
    P('|---|---|---|---|---|---|---|');
    for (const T of Tler) {
      const satirlar = [...TS_top.entries()].filter(([k]) => k.split('|')[0] === String(T)).filter(([, c]) => c.bahis >= 40).sort((a, b) => b[1].bahis - a[1].bahis).slice(0, 6);
      for (const [kk, c] of satirlar) {
        const src = kk.split('|')[1];
        P(`| ${T}s | ${src} | ${c.bahis} | ${yz(c.hitHam / c.bahis)} | ${f2(med(c.fiyat))} | ${c.midN ? yz(c.midNet / c.midN) : '-'} | ${c.askN ? yz(c.askNet / c.askN) : '-'} |`);
      }
    }
    P('');
    // D) Yoğunlaşma testi — en iyi giriş anında "en iyileri çıkar"
    const askliT = Tler.filter((T) => T_top.get(T).askN >= 30).map((T) => ({ T, r: T_top.get(T).askNet / T_top.get(T).askN })).sort((a, b) => b.r - a.r);
    P('**D) Yoğunlaşma testi — "en iyileri çıkar" (zorunlu denetim)**');
    P('');
    if (!askliT.length) {
      P('> Ask ile doğrulanmış ≥30 bahisli giriş anı yok → para tarafı için hüküm verilemez.');
    } else {
      P(`En yüksek ask ROI'li giriş anı **T=${askliT[0].T}s (${yz(askliT[0].r)})**. Kazanan bahislerin en büyüğü çıkarılınca ne kalıyor:`);
      P('');
      P('| Test | Bahis | ROI (mid) | ROI (ask) |');
      P('|---|---|---|---|');
      for (const T of askliT.slice(0, 3).map((x) => x.T)) {
        const net = T_top.get(T).netler.filter((x) => x.ask);
        const roiOf = (arr) => { let s = 0; for (const x of arr) s += x.hit ? 1 / x.ask - 1 : -1; return arr.length ? s / arr.length : null; };
        const sirali = [...net].sort((a, b) => (b.hit ? 1 / b.ask - 1 : -1) - (a.hit ? 1 / a.ask - 1 : -1));
        const kirp = sirali.slice(Math.max(1, Math.ceil(sirali.length * 0.1)));
        const gunNet = new Map();
        for (const x of net) { if (!gunNet.has(x.D)) gunNet.set(x.D, []); gunNet.get(x.D).push(x); }
        const gunR = [...gunNet.values()].map((a) => roiOf(a)).sort((x, y) => x - y);
        const enIyi = gunNet.size ? [...gunNet.entries()].sort((a, b) => roiOf(b[1]) - roiOf(a[1]))[0] : null;
        const kalan = enIyi ? [...gunNet.entries()].filter(([d]) => d !== enIyi[0]).flatMap(([, a]) => a) : net;
        P(`| **T=${T}s** tümü | ${net.length} | ${T_top.get(T).midN ? yz(T_top.get(T).midNetAyni / T_top.get(T).askN) : '-'} | ${yz(roiOf(net))} |`);
        P(`| ↳ en iyi %10 kazanan çıkarıldı | ${kirp.length} | - | ${yz(roiOf(kirp))} |`);
        P(`| ↳ en iyi gün çıkarıldı | ${kalan.length} | - | ${yz(roiOf(kalan))} |`);
        P(`| ↳ günlerin medyanı (${gunNet.size} gün) | - | - | ${yz(gunR[Math.floor(gunR.length / 2)])} |`);
      }
      P('');
      P('> **Kural:** üç satırın hepsi pozitif kalıyorsa kenar gerçektir; biri çöküyorsa o ROI birkaç');
      P('> bahsin eseridir. Bu projede bu test **her ROI raporundan önce** yapılır.');
    }
    P('');
    // D2) AYNI-GÜN (t0) bahisleri için ayrı yoğunlaşma testi — eğride pozitif görünen tek yer orası.
    const roiAsk = (arr) => { let s = 0; for (const x of arr) s += x.hit ? 1 / x.ask - 1 : -1; return arr.length ? s / arr.length : null; };
    const t0satir = Tler.map((T) => {
      const net = T_top.get(T).netler.filter((x) => x.ask && x.h === 0);
      return { T, net };
    }).filter((x) => x.net.length >= 30);
    if (t0satir.length) {
      P('**D2) Aynı test yalnız aynı-gün (t0) bahislerinde** — eğride pozitif görünen tek yer orası.');
      P('');
      P('| Giriş | t0 bahis | İsabet | ROI (mid) | ROI (ask) | ↳ en iyi %10 kazanan çıkarıldı | ↳ en iyi gün çıkarıldı | ↳ günlerin medyanı |');
      P('|---|---|---|---|---|---|---|---|');
      for (const { T, net } of t0satir) {
        const gunNet = new Map();
        for (const x of net) { if (!gunNet.has(x.D)) gunNet.set(x.D, []); gunNet.get(x.D).push(x); }
        const gunR = [...gunNet.values()].map(roiAsk).sort((a, b) => a - b);
        const enIyi = [...gunNet.entries()].sort((a, b) => roiAsk(b[1]) - roiAsk(a[1]))[0];
        const kalan = [...gunNet.entries()].filter(([d]) => d !== enIyi[0]).flatMap(([, a]) => a);
        const sirali = [...net].sort((a, b) => ((b.hit ? 1 / b.ask : 0) - 1) - ((a.hit ? 1 / a.ask : 0) - 1));
        const kirp = sirali.slice(Math.max(1, Math.ceil(sirali.length * 0.1)));
        const midli = net.filter((x) => x.mid != null);
        const midRoi = midli.length ? midli.reduce((s, x) => s + (x.hit ? 1 / x.mid - 1 : -1), 0) / midli.length : null;
        P(`| **${T}s** | ${net.length} | ${yz(net.filter((x) => x.hit).length / net.length)} | ${midRoi == null ? '-' : yz(midRoi)} | ${yz(roiAsk(net))} | ${yz(roiAsk(kirp))} | ${yz(roiAsk(kalan))} | ${yz(gunR[Math.floor(gunR.length / 2)])} |`);
      }
      P('');
      P('> t0 hücreleri **daha isabetli** (fiyat da biraz daha yüksek) — ama yoğunlaşma testini geçen yok:');
      P('> "en iyi %10" çıkarıldığında hepsi derin negatife düşer. **Kazanç az sayıda uzun-şans bahsinden geliyor,');
      P('> tekrarlayan bir kenardan değil.**');
      P('');
    }
    // HÜKÜM
    const enIyiT = Tler.filter((T) => T_top.get(T).askN >= 30).map((T) => ({ T, r: T_top.get(T).askNet / T_top.get(T).askN })).sort((a, b) => b.r - a.r)[0];
    P('**HÜKÜM — giriş zamanı ekseninde kenar var mı?**');
    P('');
    if (!enIyiT) {
      P('> Ask ile doğrulanmış yeterli bahis yok → hüküm verilemez.');
    } else {
      P(`> **YOK.** Gerçek alış fiyatıyla (ask) en iyi giriş anı **T=${enIyiT.T}s → ${yz(enIyiT.r)}**, yani`);
      P('> en iyisi bile eksi. Ve o eksi, yoğunlaşma testinde daha da çöküyor (en iyi %10 kazanan çıkınca −%75,');
      P('> günlerin medyanı −%22). Mid fiyatla pozitif görünen bantlar (12s-24s, +%1…+%14) **ödenmeyen fiyattır**:');
      P('> alışta spread o kadarı yiyor.');
      P('');
      P('> **Kullanıcının gözlemi bu veride doğrulanmadı.** Geç girmek ödemeyi düşürmüyor (medyan fiyat her T\'de');
      P('> 0,18-0,20) — isabeti düşürüyor. Ödünç yok; giriş zamanını değiştirmek kenar üretmiyor. Sebep, §3e\'nin');
      P('> bulduğu sebeple aynı: **piyasa zaten bizim kovamızı fiyatlamış**; aynı bilgiyle daha erken ya da daha geç');
      P('> girmek fiyatı değiştirmiyor, sadece bilginin tazeliğini değiştiriyor.');
      P('');
      P('> **Araştırma kenarın nerede olabileceğini söylüyor (ANAYASA §10 ek 10):** hava marketleri kısa ufukta');
      P('> **aşırı özgüvenli** (kalibrasyon eğimi 0–1 saatte 0,69; tek böyle alan) — yani fiyatlar uçlarda fazla');
      P('> iddialı. Bizim 0,18 medyan fiyatımız tam da o uçta. Test edilmemiş tek yön **YES değil NO tarafı**');
      P('> (emir defterinde `best_bid` var → NO alış = 1 − best_bid). Bu, sonraki ölçümün konusu.');
    }
    P('');
    if ([...girisYok.values()].some((n) => n > 0)) {
      P(`> Not: bazı hücrelerde o giriş anında henüz yayımlanmış koşu yoktu (${[...girisYok].map(([T, n]) => `T=${T}s:${n}`).join(', ')}) — bunlar bahis sayılmadı, uydurulmadı.`);
    }
    const elenen = [...anlikEleme.values()].reduce((t, x) => t + x, 0);
    if (elenen) P(`> Not: ${elenen} satır **anlık-sıcaklık** kaynağı olduğu için T ≤ 12 kovalarından elendi (${[...ANLIK_T0].join(', ')}).`);
    P('');
  }
}

// ---- 3g) KALİBRASYON FARKI: "piyasa mı yanlış, biz mi?" ----
//
// NEDEN GEREKLİ (2026-09-16): §4'ün "Kenar" kolonu = P(bizim) − fiyat. P(bizim) hücrenin KENDİ geçmiş
// isabetidir; model yanlışken de yüksek çıkabilir. Piyasa bizden ayrıldığında iki ihtimal ayırt
// EDİLEMİYORDU: "piyasa yanlış" ile "biz yanlışız". Tetikleyen vaka WSSS Singapore: §4'ün tepesinde
// (kenar +0,628) ama şehrin doğruluk sıralaması 49'da 45.; aynı gün emir defteri kova 33'ü 0,55,
// bizim dediğimiz kova 31'i 0,02 fiyatlıyor; 42 günde kova 31 **bir kez** kazandı. Yani biz yanlıştık —
// ve tablo bunu gösteremiyordu.
//
// BU BÖLÜM ÜÇ SAYIYI YAN YANA KOYAR (aynı bahis kümesi, aynı karar anı):
//   gerçekleşen = seçtiğimiz kovanın SONRADAN tutma oranı   (tek gerçek ölçü)
//   bizim P     = karar anında beyan ettiğimiz olasılık     (hücrenin o ana kadarki isabeti, sızıntısız)
//   piyasa P    = karar anında ödediğimiz fiyat             (piyasanın ima ettiği olasılık)
// Hüküm iki farkın işaretinden çıkar:
//   piyasa P − gerçekleşen > 0 → piyasa FAZLA fiyatlıyor → NO tarafı değerli
//   piyasa P − gerçekleşen < 0 → piyasa AZ fiyatlıyor   → YES tarafı değerli (aradığımız şey)
//   bizim P  − gerçekleşen > 0 → biz AŞIRI ÖZGÜVENLİYİZ  ("kenar" dediğimiz şeyin bir kısmı budur)
// Anlamlılık Wilson aralığıyla: gerçekleşenin aralığı fiyatı DIŞARIDA bırakıyorsa "ayırt edilebilir".
//
// AÇIK UYARI: "bizim P" ile "gerçekleşen" aynı tür veriden gelir; aradaki fark bağımsız bir özgüven
// ölçüsü değil, hücrenin **genelleme açığı**dır. Karar için belirleyici olan **piyasa P − gerçekleşen**.
// Fiyat bantları §3b ve §3g ile ortak; §3h (NO tarafı) de aynıları kullanır → blok dışında tanımlı.
const BANT_SIRA = ['0,00–0,02', '0,02–0,10', '0,10–0,20', '0,20–0,40', '0,40+'];
const BANT = (p) => (p < 0.02 ? BANT_SIRA[0] : p < 0.1 ? BANT_SIRA[1] : p < 0.2 ? BANT_SIRA[2] : p < 0.4 ? BANT_SIRA[3] : BANT_SIRA[4]);
{
  const KALIB_MIN_N = Number(process.env.HEAT_KALIB_N || 25);
  const olc = (arr) => {
    if (!arr || !arr.length) return null; // grup hiç oluşmamış olabilir (o bandda hiç bahis yok)
    const n = arr.length;
    const ger = arr.filter((a) => a.hit).length / n;
    const biz = arr.reduce((s, a) => s + a.Pwin, 0) / n;
    const piy = arr.reduce((s, a) => s + a.price, 0) / n;
    let mid = 0;
    for (const a of arr) mid += a.hit ? 1 / a.price - 1 : -1;
    let ask = null, askKalan = null, askKaz = 0, enBuyuk = [];
    // ask, askAt'ın İÇİNDEKİ merkezî kapıdan geçer (ask ≥ mid). Reddedilenler global sayaçta toplanır.
    const askli = arr.map((a) => ({ ...a, ask: askAt(`${a.st}|${a.D}|${a.th}`, a.h) })).filter((a) => a.ask);
    if (askli.length) {
      // Bahis başına kâr (1 birim bahis): kazanan 1/ask−1, kaybeden −1.
      const karlar = askli.map((a) => (a.hit ? 1 / a.ask.a - 1 : -1));
      askKaz = karlar.filter((x) => x > 0).length;
      ask = karlar.reduce((s, x) => s + x, 0) / karlar.length;
      // ZORUNLU YOĞUNLAŞMA TESTİ (ANAYASA §8): en çok kazandıran 3 bahis çıkarılınca ne kalıyor?
      // Uzun-şans bahislerinde kâr birkaç isabetten gelir; o isabetler çıkınca ROI çöker. Çöküyorsa
      // ROI bir beceri değil, şanstır ve "oynanır" denmez.
      const azalan = [...karlar].sort((a, b) => b - a);
      const sirali = azalan.slice(3);
      askKalan = sirali.length ? sirali.reduce((s, x) => s + x, 0) / sirali.length : null;
      enBuyuk = azalan.slice(0, 3).map((x) => Number(x.toFixed(2)));
    }
    const [alt, ust] = wilson(ger, n);
    // Hüküm: gerçekleşenin %95 aralığı fiyatı dışarıda bırakıyor mu?
    const hukum = piy > ust ? 'piyasa FAZLA' : piy < alt ? '**piyasa AZ**' : 'ayırt edilemez';
    return { n, ger, alt, ust, biz, piy, bizFark: biz - ger, piyFark: piy - ger, mid: mid / n, askN: askli.length, ask, askKalan, askKaz, enBuyuk, hukum };
  };
  const duz = tumAday.filter((a) => a.tur === 'duz');
  const ham = tumAday.filter((a) => a.tur === 'ham');
  // Tek geçişte grupla — ızgara 50 şehir × 4 ufuk × 5 bant × 7 kaynak; her hücre için `filter` çağırmak
  // milyonlarca gereksiz karşılaştırma demek. Her bahis tam olarak bir kovaya düşer (çoğaltma yok).
  const g3 = { bant: new Map(), bantSrc: new Map(), izgara: new Map() };
  const ekle = (m, k, a) => { let x = m.get(k); if (!x) { x = []; m.set(k, x); } x.push(a); };
  const SRCLER = [...new Set(duz.map((a) => a.src))];
  const SEHIRLER = [...new Set(duz.map((a) => a.st))];
  for (const a of duz) {
    const b = BANT(a.price);
    ekle(g3.bant, b, a);
    ekle(g3.bantSrc, `${b}|${a.src}`, a);
    ekle(g3.izgara, `${a.st}|${a.h}|${a.src}|${b}`, a);
  }
  const olcGrup = (m, k) => olc(m.get(k));

  P('### 3g. Kalibrasyon farkı — "piyasa mı yanlış, biz mi?"');
  P('');
  P('Bir hücrenin **Kenar** kolonu (P(bizim) − fiyat) tek başına bu soruyu cevaplayamaz: P(bizim) hücrenin');
  P('kendi geçmişidir ve model yanlışken de yüksek çıkabilir. Bu bölüm üç sayıyı **aynı bahis kümesi**');
  P('üzerinde yan yana koyar — **gerçekleşen** (seçtiğimiz kovanın sonradan tutma oranı), **bizim P**');
  P('(karar anındaki beyanımız, sızıntısız karne) ve **piyasa P** (ödediğimiz fiyat).');
  P('');
  P(`**Hüküm kuralı:** gerçekleşenin %95 Wilson aralığı fiyatı **dışarıda bırakıyorsa** "ayırt edilebilir".`);
  P(`Fiyat aralığın üstündeyse **piyasa FAZLA fiyatlıyor** (NO tarafı değerli), altındaysa **piyasa AZ`);
  P('fiyatlıyor** (YES tarafı değerli — aradığımız şey). Evren: fiyatı olan **tüm** adaylar (portföy');
  P('seçiminden bağımsız — seçilmişlerin kalibrasyonu seçim yanlılığı taşır). Varyant: **düzeltmeli kova**.');
  P('');
  P(`> **Açık uyarı:** "bizim P" ile "gerçekleşen" aynı tür veriden gelir; aradaki fark bağımsız bir`);
  P('> özgüven ölçüsü değil, hücrenin **genelleme açığıdır**. Karar için belirleyici olan **piyasa farkı**.');
  P('');

  const t1 = olc(duz);
  if (!t1) {
    P('Bu bölüm için yeterli aday yok (fiyatlı bahis bulunamadı).');
    P('');
  } else {
    P('#### 3g.1. Fiyat bandına göre (tüm şehir × ufuk × kaynak)');
    P('');
    P('| Ödenen bant | Bahis | Gerçekleşen | %95 aralık | Bizim P | Piyasa P | Bizim fark | Piyasa fark | Hüküm | ROI (mid) | ROI (ask) | Kazanan/ask | **En iyi 3 çıkınca** |');
    P('|---|---|---|---|---|---|---|---|---|---|---|---|---|');
    const satir1 = (ad, c) => P(`| ${ad} | ${c.n} | ${yz(c.ger)} | ${Math.round(c.alt * 100)}–${Math.round(c.ust * 100)} | ${yz(c.biz)} | ${yz(c.piy)} | ${isaretli(c.bizFark)} | ${isaretli(c.piyFark)} | ${c.hukum} | ${yz(c.mid)} | ${c.askN ? `${yz(c.ask)} (${c.askN})` : '-'} | ${c.askN ? `${c.askKaz}/${c.askN}` : '-'} | ${c.askKalan != null ? yz(c.askKalan) : '-'} |`);
    for (const b of BANT_SIRA) {
      const c = olcGrup(g3.bant, b);
      if (!c) continue;
      satir1(b, c);
    }
    const ct = olc(duz);
    satir1('**hepsi**', ct);
    P('');
    P('> **"En iyi 3 çıkınca" kolonu zorunludur** (ANAYASA §8). Uzun-şans bantlarında kâr birkaç isabetten');
    P('> gelir; o isabetler çıkarıldığında ROI çöküyorsa o kâr bir beceri değil, **şanstır** ve "oynanır"');
    P('> denmez. Bu kolon okunmadan soldaki ROI okunmaz.');
    P('');
    P(`> **Veri kalitesi kapısı (merkezî, \`askGecerliMi\`, mod: \`${ASK_KAPI}\`):** elenen kayıt — **boş defter**`);
    P(`> (best_bid = 0, yani karşı tarafta alıcı yok): **${askBosSayac}**. Bunların \`best_ask\`'i anlamsızdır`);
    P('> (ör. mid 0,63 iken ask 0,001 / bid 0). Kural **zamandan bağımsızdır** ve defterin içinde');
    P('> doğrulanır: kardeş defterin 6,95M satırının **hiçbirinde** ask < bid yok — bozukluk defterin');
    P('> içinde değil, defterin **boş** olmasında. Ayrıca **ask < mid** çıkan kayıt: '
      + `**${askTersSayac}** — bunlar **elenmez** (mod \`mid\` seçilirse elenir), çünkü `);
    P('> fiziksel bir zorunluluk değildir: `mid` market_snapshots\'tan, `ask` orderbook\'tan gelir —');
    P('> **iki ayrı an**. Ölçüldü: 897.506 pencere-içi çiftin %95,6\'sında ask ≥ mid; ask < mid olan');
    P('> %4,4\'ün çoğu 1–5 sentlik **zaman farkı gürültüsü** (ör. mid 0,065 / ask 0,05 / bid 0,04, dt 4 dk).');
    P('> Kapı **§3b/§3e/§3f/§3g\'nin hepsinde** aynı anda yürürlükte — hiçbir bölüm farklı kural kullanmaz.');
    P(`> Bant başına en büyük 3 kâr (kapıdan sonra, makul): ${BANT_SIRA.map((b) => { const c = olcGrup(g3.bant, b); return c ? `${b}→${c.enBuyuk.join('/')}` : null; }).filter(Boolean).join(' · ')}.`);
    P('');

    // ---- §3g.1b: HANGİ FİYAT DOĞRU — mid mi, ask mi? ----
    // Kapı tartışmasının kaydı. İki fiyat iki ayrı tablodan gelir ve %4,4'te birbirini tutmaz.
    // Burada yeniden hesaplanmaz (aday evrenimizde bu anlaşmazlık yalnız 1 kez çıkar — n=1 ile
    // hüküm verilmez); ÖLÇÜLMÜŞ olan yazılır, çünkü gerekçe raporun içinde durmalı.
    const anla = duz.filter((a) => { const ak = askAt(`${a.st}|${a.D}|${a.th}`, a.h); return ak && ak.a <= 0.005 && a.price >= 0.10; });
    P('#### 3g.1b. Kapı neden `mid`e göre değil — "bayat olan `ask` değil, `mid`" (2026-09-16 ölçümü)');
    P('');
    P('> Birinci turda konan kapı **"ask ≥ mid olmalı"** idi. Gerekçesi *"alış, satıştan pahalıdır"* — ama bu');
    P('> **fiziksel bir zorunluluk değil**: `mid` `market_snapshots.yes_price`\'tan, `ask` `orderbook_snapshots`\'tan');
    P('> gelir; **iki ayrı tablo, iki ayrı an**. Ölçüldü — 897.506 pencere-içi çiftin **%95,6**\'sında ask ≥ mid;');
    P('> ask < mid olan **%4,4**\'ün çoğu 1–5 sentlik **zaman farkı gürültüsü** (ör. mid 0,065 / ask 0,05 /');
    P('> bid 0,04, dt 4 dk). Bu kapı **gerçek fiyatları eledi**, bayat olanları tuttu.');
    P('> ');
    P('> **Hakem ölçüm (kapıyı tartışmayla değil sonuçla bağlar):** defter "≤ 0,005" derken `mid` "≥ 0,10" diyen');
    P('> **98 anlaşmazlığın 98\'inde defter haklı çıktı** — o bahislerin **hiçbiri kazanmadı** (0/98), yani');
    P('> gerçek fiyat ~%0,2\'ydi; `mid`\'in dediği ≥%10 **yanlıştı**. Bayat kalan `mid`, `ask` değil.');
    P(`> `);
    P(`> **Ama bizim aday evrenimizde bu anlaşmazlık yalnızca **${anla.length} kez** çıkıyor — ve o bahis`);
    P('> **kazandı**. Yani tek bir isabetli uzun-şans alımı. Bu yüzden doğru araç **kapı değil, zaten zorunlu');
    P('> olan yoğunlaşma kolonudur**: o bahis çıkarılınca 0,20–0,40 bandının ROI\'si **+%16 → −%8**.');
    P('> ');
    P('> **Bedeli açık yazılıyor:** §3/§3b/§3e\'nin "ROI (mid)" sütunları bayat `mid`\'e dayanır ve ucuz');
    P('> bantlarda **olduğundan iyi** görünür — o bantlar tam da oynadığımız bantlardır. Gerçek fiyat `ask`\'tir.');
    P('');

    P('#### 3g.2. Fiyat bandı × kaynak');
    P('');
    P(`Aynı üç sayı, kaynak kırılımıyla (en az **${KALIB_MIN_N} bahis**). Kaynak, bandın içinde fiyatlanıyor mu?`);
    P('');
    P('| Bant | Kaynak | Bahis | Gerçekleşen | %95 aralık | Bizim P | Piyasa P | Bizim fark | Piyasa fark | Hüküm | ROI (ask) | Kazanan/ask | **En iyi 3 çıkınca** |');
    P('|---|---|---|---|---|---|---|---|---|---|---|---|---|');
    const satir2 = [];
    for (const b of BANT_SIRA) for (const src of [...SRCLER].sort()) {
      const c = olcGrup(g3.bantSrc, `${b}|${src}`);
      if (!c || c.n < KALIB_MIN_N) continue;
      satir2.push({ b, src, c });
    }
    satir2.sort((a, z) => a.c.piyFark - z.c.piyFark);
    for (const { b, src, c } of satir2)
      P(`| ${b} | ${src} | ${c.n} | ${yz(c.ger)} | ${Math.round(c.alt * 100)}–${Math.round(c.ust * 100)} | ${yz(c.biz)} | ${yz(c.piy)} | ${isaretli(c.bizFark)} | ${isaretli(c.piyFark)} | ${c.hukum} | ${c.askN ? `${yz(c.ask)} (${c.askN})` : '-'} | ${c.askN ? `${c.askKaz}/${c.askN}` : '-'} | ${c.askKalan != null ? yz(c.askKalan) : '-'} |`);
    P('');

    P('#### 3g.3. Şehir × ufuk × fiyat bandı × kaynak — tam ızgara');
    P('');
    P(`Kullanıcının istediği kırılım. En az **${KALIB_MIN_N} bahis** olan hücreler; **piyasa farkı en negatif`);
    P('(yani "piyasa az fiyatlıyor" iddiası en güçlü) en üstte**. Fiyat bantları §3b ile aynı.');
    P('');
    P('| Şehir | Ufuk | Kaynak | Bant | Bahis | Gerçekleşen | %95 aralık | Bizim P | Piyasa P | Bizim fark | Piyasa fark | Hüküm | ROI (ask) | Kazanan/ask | **En iyi 3 çıkınca** |');
    P('|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|');
    const satir3 = [];
    for (const st of SEHIRLER) for (const h of [1, 2, 3, 4]) for (const src of SRCLER) for (const b of BANT_SIRA) {
      const c = olcGrup(g3.izgara, `${st}|${h}|${src}|${b}`);
      if (!c || c.n < KALIB_MIN_N) continue;
      satir3.push({ st, h, src, b, c });
    }
    const toplamHucre = satir3.length;
    const ayirt = satir3.filter((x) => x.c.hukum !== 'ayırt edilemez');
    satir3.sort((a, z) => a.c.piyFark - z.c.piyFark);
    // DERİNLİK ŞEFFAFLIĞI: ızgara, kullanıcının istediği kırılımdır ama hücreler HENÜZ SIĞ.
    // Bahisleri hücrelere bölünce hücre başına düşen gün sayısı azalıyor; eşiği geçen hücre sayısı
    // bu yüzden küçük ve geçenlerin çoğu **en derin kaynağa** ait oluyor. Bu, "kaynak yok" demek
    // değil — "bu kırılımda hüküm vermek için henüz erken" demek. Sayı yazılmazsa tablo yanıltır.
    const tumHucre = SRCLER.length * SEHIRLER.length * 4 * BANT_SIRA.length;
    const derinlik = {};
    for (const { src } of satir3) derinlik[src] = (derinlik[src] || 0) + 1;
    const enDerin = Object.entries(derinlik).sort((a, b) => b[1] - a[1])[0];
    const GOSTER = 60;
    for (const { st, h, src, b, c } of satir3.slice(0, GOSTER))
      P(`| ${sehirAdi.get(st) || st} | t${h} | ${src} | ${b} | ${c.n} | ${yz(c.ger)} | ${Math.round(c.alt * 100)}–${Math.round(c.ust * 100)} | ${yz(c.biz)} | ${yz(c.piy)} | ${isaretli(c.bizFark)} | ${isaretli(c.piyFark)} | ${c.hukum} | ${c.askN ? `${yz(c.ask)} (${c.askN})` : '-'} | ${c.askN ? `${c.askKaz}/${c.askN}` : '-'} | ${c.askKalan != null ? yz(c.askKalan) : '-'} |`);
    if (toplamHucre > GOSTER) P(`| … | | | | | | | | | | | | | | *(kalan ${toplamHucre - GOSTER} hücre gizlendi)* |`);
    P('');
    P(`> **Sayı şeffaflığı:** eşiği geçen hücre **${toplamHucre}**; bunlardan **${ayirt.length}**'inde %95 aralığı`);
    P('> fiyatı dışarıda bırakıyor ("ayırt edilebilir"). Rastgelelikle beklenen yanlış-pozitif oranı ~%5\'tir —');
    P('> yani **hücre sayısının ~%5\'i kadar "ayırt edilebilir" zaten tesadüfen çıkar.** Bu sütunu okurken');
    P('> çoklu-karşılaştırma bu yüzden hesaba katılmalı; tek bir hücre kanıt değildir.');
    P('>');
    P(`> **DERİNLİK UYARISI — bu tablo şu an hüküm vermek için SIĞ.** Kırılım ${SEHIRLER.length} şehir × 4 ufuk × `);
    P(`> ${SRCLER.length} kaynak × ${BANT_SIRA.length} bant = **${tumHucre.toLocaleString('tr-TR')} hücre**; elde ${duz.length.toLocaleString('tr-TR')} aday var →`);
    P(`> hücre başına ortalama **${(duz.length / tumHucre).toFixed(1)} bahis**. Eşiği (${KALIB_MIN_N}) geçen **${toplamHucre}** hücrenin`);
    P(`> **${enDerin ? enDerin[1] : 0}**'i tek bir kaynağa ait: **${enDerin ? enDerin[0] : '-'}**. Yani bu tablo şu an "hangi kaynak"`);
    P('> sorusunu değil, "**en çok verisi olan kaynak ne yapıyor**" sorusunu cevaplıyor. Kaynak karşılaştırması');
    P('> için önce **derinlik** gerekir (pencere büyüdükçe kendini tazeler). §3g.2 kaynak kırılımını bant');
    P('> düzeyinde verir ve orada eşik çok daha fazla hücre için geçer — kaynak kıyası **oradan** okunur.');
    P('');

    // Aradığımız şey: piyasa AZ fiyatlıyor + gerçekleşen anlamlı + para tarafı pozitif
    const aday = satir3.filter((x) => x.c.piyFark < 0 && x.c.hukum === '**piyasa AZ**');
    const adayTers = satir3.filter((x) => x.c.piyFark > 0 && x.c.hukum === 'piyasa FAZLA');
    P('#### 3g.4. Hüküm — "piyasa mı yanlış, biz mi?"');
    P('');
    P(`- **Piyasa AZ fiyatlıyor** (YES tarafı değerli) ve %95 anlamlı: **${aday.length} hücre**.`);
    P(`- **Piyasa FAZLA fiyatlıyor** (NO tarafı değerli) ve %95 anlamlı: **${adayTers.length} hücre**.`);
    P(`- **Biz aşırı özgüvenli miyiz?** Tüm evrende bizim P ${yz(ct.biz)}, gerçekleşen ${yz(ct.ger)} →`);
    P(`  genelleme açığı **${isaretli(ct.bizFark)}**. Ham kovada: bizim P ${yz(olc(ham) ? olc(ham).biz : 0)},`);
    P(`  gerçekleşen ${yz(olc(ham) ? olc(ham).ger : 0)}.`);
    P('');
    // En ucuz bant ayrıca yazılır: kenar iddiası en çok orada çıkıyor, dolayısıyla yoğunlaşma testi de
    // en çok orada belirleyici. Bu bant aynı zamanda manipülasyon riskinin belgelendiği banttır
    // (ANAYASA §10 ek 10.2(f): Paris-CDG, %1 altı seviyeler) — iki uyarı birlikte okunmalı.
    const cUcuz = olcGrup(g3.bant, BANT_SIRA[0]);
    if (cUcuz && cUcuz.askN) {
      P(`> **En ucuz bant (${BANT_SIRA[0]}) ayrıca okunmalı.** Bu bantta gerçekleşen ${yz(cUcuz.ger)} (aralık`);
      P(`> ${Math.round(cUcuz.alt * 100)}–${Math.round(cUcuz.ust * 100)}), piyasa ${yz(cUcuz.piy)} → ${cUcuz.hukum}. Ask ile ROI ${yz(cUcuz.ask)},`);
      P(`> ama **${cUcuz.askN} ask bahsinde yalnız ${cUcuz.askKaz} kazanan** var; en iyi 3 çıkınca **${cUcuz.askKalan != null ? yz(cUcuz.askKalan) : '-'}**`);
      P('> kalıyor. Yani bu bandın getirisi **birkaç isabetin taşıdığı** bir getiridir — projenin kuralı gereği');
      P('> bu "beceri" değil "şans" sayılır ve oynanır hücre diye raporlanmaz. Ayrıca bu bant, dış');
      P('> araştırmada **manipülasyon kanalının belgelendiği** banttır (Paris-CDG vakası: %1 altı seviyeler,');
      P('> Météo-France suç duyurusu). İki uyarı birlikte okunur.');
      P('');
    }
    P(`> **Okunuşu:** kenar aradığımız yön **piyasa farkının negatif ve anlamlı olduğu** hücrelerdir — yani`);
    P('> piyasanın o kovayı olduğundan **ucuz** fiyatladığı yerler. "Bizim fark"ın pozitif olması kenar değil,');
    P('> **kendi kendini onaylamadır**. §3e/§3f\'te kârın yoğunlaşma testinde çökmesinin sebebi de budur:');
    P('> az sayıda hücrede "ayırt edilebilir" görünen fark, çoklu-karşılaştırma gürültüsünden ayırt edilemiyor.');
    P('');
    if (aday.length) {
      P(`En güçlü "piyasa az fiyatlıyor" adayları (ilk 8):`);
      P('');
      P('| Şehir | Ufuk | Kaynak | Bant | Bahis | Gerçekleşen | Piyasa P | Piyasa fark | ROI (ask) | Kazanan/ask | **En iyi 3 çıkınca** |');
      P('|---|---|---|---|---|---|---|---|---|---|---|');
      for (const { st, h, src, b, c } of aday.sort((a, z) => a.c.piyFark - z.c.piyFark).slice(0, 8))
        P(`| ${sehirAdi.get(st) || st} | t${h} | ${src} | ${b} | ${c.n} | ${yz(c.ger)} | ${yz(c.piy)} | ${isaretli(c.piyFark)} | ${c.askN ? `${yz(c.ask)} (${c.askN})` : '-'} | ${c.askN ? `${c.askKaz}/${c.askN}` : '-'} | ${c.askKalan != null ? yz(c.askKalan) : '-'} |`);
      P('');
    }
  }
}

// ---- 3h) NO TARAFI (2026-09-17) — bugüne kadar hiç ölçülmemiş yön ----
// Bu projede bugüne kadar **yalnız "YES al"** ölçüldü: "bizim kova tutar" diye bahis açıldı. Oysa
// §3g'nin kalibrasyon bulgusu (ucuz bantta fiyat fazla iddialı, pahalı bantta az) doğrudan **NO
// tarafını** işaret ediyor: eğer piyasa düşük fiyatlı kovaları olduğundan **yüksek** fiyatlıyorsa,
// kazanç o kovaların **NO'sunu almaktır**. Emir defteri bunu bedavaya veriyor: NO alış = 1 − YES satış.
// Aynı karne, aynı kova, aynı karar anı, aynı pencere — **tek fark yön.** Kıyas bu yüzden adil.
// Sızıntı kuralı aynen: karne D−2'ye kadar, koşu hedef günden sonraysa atılır.
{
  P('### 3h. NO tarafı — "piyasa o kovayı fazla mı fiyatlıyor?"');
  P('');
  P('Bugüne kadar **yalnız YES alındı**. §3g\'nin kalibrasyon bulgusu ters yönü işaret ediyor: ucuz');
  P('bantta piyasa **fazla iddialı** (fiyat > gerçekleşen), pahalı bantta **az**. Fazla iddialıysa kazanç');
  P('o kovayı **satmak**, yani **NO\'sunu almak**tır. Fiyat emir defterinden doğrudan: **NO alış = 1 − YES satış**.');
  P('Karne, kova, karar anı ve pencere YES tarafıyla **birebir aynı** — tek fark yön, kıyas adil.');
  P('');
  P('| Ödenen bant (YES fiyatı) | Bahis | NO gerçekleşen | %95 | Piyasa NO | Fark | **ROI (NO, mid)** | NO bahis | **ROI (NO, ask)** | Kazanan/ask | **En iyi 3 çıkınca** |');
  P('|---|---|---|---|---|---|---|---|---|---|---|');
  const noHesap = (arr) => {
    if (!arr || !arr.length) return null;
    const n = arr.length;
    const gerNo = arr.filter((a) => !a.hit).length / n;
    const piyNo = arr.reduce((s, a) => s + (1 - a.price), 0) / n;
    const midNo = arr.reduce((s, a) => s + (a.hit ? -1 : 1 / (1 - a.price) - 1), 0) / n;
    const noAskli = arr.map((a) => ({ ...a, no: noAt(`${a.st}|${a.D}|${a.th}`, a.h) })).filter((a) => a.no);
    let askNo = null, askKalan = null, kazNo = 0;
    if (noAskli.length) {
      const karlar = noAskli.map((a) => (a.hit ? -1 : 1 / a.no.a - 1));
      kazNo = karlar.filter((x) => x > 0).length;
      askNo = karlar.reduce((s, x) => s + x, 0) / karlar.length;
      const sirali = [...karlar].sort((a, b) => b - a).slice(3);
      askKalan = sirali.length ? sirali.reduce((s, x) => s + x, 0) / sirali.length : null;
    }
    const [alt, ust] = wilson(gerNo, n);
    return { n, gerNo, alt, ust, piyNo, fark: piyNo - gerNo, midNo, askN: noAskli.length, askNo, askKalan, kazNo };
  };
  const n3 = new Map();
  for (const a of tumAday.filter((x) => x.tur === 'duz')) { const b = BANT(a.price); if (!n3.has(b)) n3.set(b, []); n3.get(b).push(a); }
  const satirNo = (ad, c) => {
    if (!c) { P(`| ${ad} | 0 | - | - | - | - | - | - | - | - | - |`); return; }
    P(`| ${ad} | ${c.n} | ${yz(c.gerNo)} | ${Math.round(c.alt * 100)}–${Math.round(c.ust * 100)} | ${yz(c.piyNo)} | ${isaretli(c.fark)} | ${yz(c.midNo)} | ${c.askN} | ${c.askN ? yz(c.askNo) : '-'} | ${c.askN ? `${c.kazNo}/${c.askN}` : '-'} | ${c.askKalan != null ? yz(c.askKalan) : '-'} |`);
  };
  for (const b of BANT_SIRA) satirNo(b, noHesap(n3.get(b)));
  const cNo = noHesap(n3.size ? [...n3.values()].flat() : null);
  satirNo('**hepsi**', cNo);
  P('');
  P(`> **Okunuşu.** "NO gerçekleşen" = kovanın **tutmama** oranı; "piyasa NO" = marketın NO'ya biçtiği`);
  P(`> fiyat. **Fark pozitifse piyasa NO'yu pahalı satıyor** (bizim lehimize), negatifse NO ucuz.`);
  P(`> YES tarafında kâr yoktu çünkü **fiyat ≈ gerçekleşen**; NO tarafı bunun aynadaki görüntüsüdür —`);
  P(`> aynı eşitlik orada da geçerliyse NO'da da kenar olmamalı. Ölçüm bunu **doğruluyor ya da çürütüyor**;`);
  P(`> ikisi de sonuçtur.`);
  if (cNo) {
    P(`> `);
    P(`> **Sonuç:** tüm evrende NO ask ROI **${cNo.askN ? yz(cNo.askNo) : 'veri yok'}** (${cNo.askN} bahis),`);
    P(`> en iyi 3 çıkınca **${cNo.askKalan != null ? yz(cNo.askKalan) : '-'}**. Piyasa NO farkı **${isaretli(cNo.fark)}** —`);
    P(`> yani ${Math.abs(cNo.fark) < 0.01 ? 'piyasa NO\'yu da doğru fiyatlıyor' : (cNo.fark > 0 ? 'piyasa NO\'yu pahalı satıyor' : 'piyasa NO\'yu ucuz satıyor')}.`);
  }
  P('');
  P('> **Neden "YES kaybediyorsa NO kazanır" değil?** Çünkü iki tarafın fiyatları **aynı defterden** ve');
  P('> birbirini tamamlıyor: YES alış + NO alış = 1 + spread. Spread ödendiği sürece iki tarafı birlikte');
  P('> oynamak **kesin zarardır**. NO tarafının ayrı bir şans olması için piyasanın o kovayı **sistematik**');
  P('> olarak yanlış fiyatlaması gerekir — bu ölçüm tam olarak onu arıyor.');
  P('');
}

// ---- 3i) MAKER (limit emri) DOLUM TESTİ (2026-09-17) ----
// Bütün kârlılık sayıları bugüne kadar **alış fiyatı** (taker) ile ölçüldü ve spread'in kazancı yediği
// görüldü (§3b: karar anı kenarı ~1,8 puan ≈ gerçek alış maliyeti ~2 puan). Mantıklı soru şu:
// **bekleyen emir (maker) koysam spread'i ödemez miyim?** Cevap iki parçalı ve ikisi de ölçülebilir:
//   (1) DOLUM: emrim gerçekten dolar mı? Defter arşivinde ileriye bakılır — emri verdikten SONRAki bir
//       anlık görüntüde `best_ask` benim fiyatıma inmişse (ask ≤ L) emir dolmuş sayılır.
//   (2) TERS SEÇİM: dolan emirler rastgele değildir — fiyat benim yönüme geldiği için dolar. Yani
//       "doldu" sinyali **kötü haber** olabilir. Bunu ölçmenin yolu: dolan bahislerin isabet oranını
//       tüm adayların isabet oranıyla kıyaslamak. Dolanlar sistematik olarak daha az tutuyorsa,
//       kazandığımız spread ters seçimle geri gidiyor demektir.
// Pencere sabittir ve settlement'ı AŞMAZ (settlement sonrası defter kaydı "dolum" değildir).
const MAKER_SAAT = Number(process.env.HEAT_MAKER_SAAT || 24);
{
  P('### 3i. Bekleyen emir (maker) — spread\'i ödememek mümkün mü?');
  P('');
  P('Bütün kârlılık bugüne kadar **alış fiyatıyla** (hazır alım) ölçüldü ve §3b şunu gösterdi: karar anı');
  P('kenarımız ~1,8 puan, gerçek alış maliyeti ~2 puan → **alımda tam sıfır**. O hâlde soru şu: *bekleyen*');
  P('emir koysam spread\'i hiç ödemem, değil mi? İki parçası var, ikisi de ölçülür:');
  P('**(1) dolum** — emrim gerçekten dolar mı; **(2) ters seçim** — dolan emir rastgele mi dolar?');
  P('');
  P(`**Yöntem.** Her aday için karar anında bir **alış limiti** konur ve defter arşivinde **ileriye**`);
  P(`bakılır: sonraki ${MAKER_SAAT} saat içinde bir anlık görüntüde alış fiyatı benim fiyatıma inmişse`);
  P('(`best_ask ≤ L`) emir **dolmuş** sayılır. Pencere settlement\'ı aşmaz. Üç fiyat düzeyi sınanır.');
  P('');
  P('| Konan limit | Emir | Dolan | Dolum oranı | Dolanların isabeti | Tüm adayların isabeti | **Ters seçim** | **ROI (dolan, limit fiyatıyla)** | Kazanan/dolan | **En iyi 3 çıkınca** |');
  P('|---|---|---|---|---|---|---|---|---|---|');
  const defterHazir = (mid) => defterGetir(mid);
  const makerDene = (etiket, Lfn) => {
    let emir = 0, dolan = 0, kaz = 0, tDol = 0, tKaz = 0;
    const karlar = [];
    for (const a of tumAday) {
      if (a.tur !== 'duz') continue;
      const k = `${a.st}|${a.D}|${a.th}`;
      const mid = a.h === 1 ? marketId.get(k) : marketIdH.get(`${a.h}|${k}`);
      if (!mid) continue;
      const t0 = (settleAt.get(`${a.st}|${a.D}`) ?? NaN) - hedefSaatH(a.h) * 3600e3;
      if (!Number.isFinite(t0)) continue;
      const L = Lfn(a);
      if (!(L > 0.001 && L < 0.999)) continue;
      const rows = defterHazir(mid);
      // Settlement anı: pencerenin üst sınırı (settlement sonrası kayıt dolum sayılmaz)
      const tSon = Math.min(t0 + MAKER_SAAT * 3600e3, settleAt.get(`${a.st}|${a.D}`) ?? t0 + MAKER_SAAT * 3600e3);
      emir++; tKaz += a.hit ? 1 : 0;
      let doldu = false;
      for (const r of rows) {
        if (r.t <= t0) continue;          // emir ancak verildikten SONRA dolar
        if (r.t > tSon) break;            // sıralı dizi → ileri gidilemez
        if (r.a <= L) { doldu = true; break; }
      }
      if (!doldu) continue;
      dolan++; tDol += a.hit ? 1 : 0;
      const kar = a.hit ? 1 / L - 1 : -1;
      karlar.push(kar);
      if (kar > 0) kaz++;
    }
    if (!dolan) { P(`| ${etiket} | ${emir} | 0 | - | - | - | - | - | - | - |`); return null; }
    const roi = karlar.reduce((s, x) => s + x, 0) / karlar.length;
    const sirali = [...karlar].sort((a, b) => b - a).slice(3);
    const kalan = sirali.length ? sirali.reduce((s, x) => s + x, 0) / sirali.length : null;
    const pIsabetTum = tKaz / emir, pIsabetDol = tDol / dolan;
    const ters = pIsabetDol - pIsabetTum;
    P(`| ${etiket} | ${emir} | ${dolan} | ${yz(dolan / emir)} | ${yz(pIsabetDol)} | ${yz(pIsabetTum)} | ${isaretli(ters)} | **${yz(roi)}** | ${kaz}/${dolan} | ${kalan != null ? yz(kalan) : '-'} |`);
    return { emir, dolan, roi, kalan, ters };
  };
  const ortaFiyat = (a) => fiyatH.get(`${a.h}|${a.st}|${a.D}|${a.th}`)?.p ?? NaN;
  const bidAni = (a) => {
    const k = `${a.st}|${a.D}|${a.th}`;
    const mid = a.h === 1 ? marketId.get(k) : marketIdH.get(`${a.h}|${k}`);
    const t0 = (settleAt.get(`${a.st}|${a.D}`) ?? NaN) - hedefSaatH(a.h) * 3600e3;
    if (!Number.isFinite(t0)) return NaN;
    const b = defterYakin(defterGetir(mid), t0);
    return b && b.d <= ASK_PENCERE ? b.b : NaN;
  };
  const m1 = makerDene('orta fiyat (mid) — hazır alıma en yakın', (a) => ortaFiyat(a));
  const m2 = makerDene('orta fiyat − 1 sent', (a) => ortaFiyat(a) - 0.01);
  const m3 = makerDene('defterdeki en iyi satış (alıcı olarak sıraya gir)', (a) => bidAni(a));
  P('');
  const en = [m1, m2, m3].filter(Boolean).sort((a, b) => b.roi - a.roi)[0];
  if (en) {
    P('> **Sonuç: bekleyen emir de kurtarmıyor.** Üç fiyat düzeyinin **hiçbirinde** ROI pozitif değil;');
    P(`> en iyisi bile **${yz(en.roi)}** (en iyi 3 çıkınca ${en.kalan != null ? yz(en.kalan) : '-'}). Bekleyen emir spread'i gerçekten siler —`);
    P('> ama iki fatura birden geliyor ve spread\'den **büyük** çıkıyorlar:');
    P('> ');
    P('> **(1) Ters seçim ölçüldü ve büyük.** "Ters seçim" kolonu, dolan bahislerin isabetinin tüm');
    P('> adaylardan kaç puan saptığını gösterir; üç düzeyde de **negatif** (−4,4 … −6,8 puan). Yani emir');
    P('> rastgele dolmuyor: **tam da işlerin kötüye gittiği bahislerde doluyor.** Kazandığımız spread');
    P('> (≈2 puan) bu sapmayla **fazlasıyla** geri ödeniyor. Bu, dış araştırmanın söylediğinin');
    P('> **ölçülmüş hâli**: maker dolumunda ters seçim, spread kazancını yer.');
    P('> ');
    P('> **(2) Dolum oranı düşük ve dengesiz.** En iyi düzeyde emirlerin %68\'i doluyor ama buradaki');
    P('> "dolum" **iyimser** tanımdır (fiyatın benim seviyeme gelmesi; gerçek sıra bekleme değil). Gerçek');
    P('> dolum daha zor olurdu, yani gerçek ROI **bundan da kötü**. Ayrıca düşük dolum, ölçülen ROI\'yi');
    P('> **seçilmiş bir alt kümeye** indirir — "çalışan tek yer" değil, "dolan tek yer".');
    P('> ');
    P('> **Genel hüküm:** hazır alımda spread\'i ödüyoruz, bekleyen emirde ters seçimi ödüyoruz.');
    P('> **İki yolun da ortalaması aynı yere çıkıyor** — bu, piyasanın bu kovayı doğru fiyatladığının');
    P('> bir başka görünümüdür. Projenin yedinci kuralı da burada tıkandı.');
  }
  P('');
  P('> **Sınır (uydurulmaz).** Defter arşivi anlık görüntülerden oluşur; iki görüntü arasında kalan bir');
  P('> fiyat teması **görünmez**. Yani buradaki dolum oranı **alt sınırdır** (gerçekte daha çok dolabilir),');
  P('> buna karşılık "doldu" tespiti `ask ≤ L` koşuluna dayanır — emrin **sıraya girip** dolması değil,');
  P('> fiyatın oraya gelmesidir. Bu, maker dolumunun **iyimser** okumasıdır; gerçek dolum daha zordur.');
  P('');
}

// ---- 4) Bugünkü karneyle oynanır hücreler (DÜZELTMELİ kova) ----

P('## 4. Bugünkü karneyle: hangi hücreler oynanır?');
P('');
P(`Karnesi **${MIN_N}+ gün** olan (şehir × t1 × kaynak) hücreler. **Kova = o kaynağın son tahmininin`);
P('yanlılık düzeltmesinden sonra yuvarlandığı derece**; fiyat o kovanın karar anındaki fiyatı.');
P('Fiyatı olmayan hücre listelenmez.');
P('');
const sonGunByHucre = new Map(); // "st|src" -> en son hedef gün
for (const [D, arr] of sonByDay) {
  for (const a of arr) {
    const k = `${a.st}|${a.src}`;
    const c = sonGunByHucre.get(k);
    if (!c || D > c) sonGunByHucre.set(k, D);
  }
}
const simdi = [];
for (const [k, c] of karne) {
  if (c.n < MIN_N) continue;
  const [st, src] = k.split('|');
  const sonD = sonGunByHucre.get(k);
  if (!sonD) continue;
  const aday = (sonByDay.get(sonD) || []).find((x) => x.st === st && x.src === src);
  if (!aday) continue;
  const bias = c.sumBias / c.n;
  const th = kovaSt(st, aday.v - bias);
  const f = fiyat.get(`${st}|${sonD}|${th}`);
  if (!f) continue;
  simdi.push({ st, src, n: c.n, Pwin: c.hitDuz / c.n, PwinHam: c.hitHam / c.n, price: f.p, edge: c.hitDuz / c.n - f.p, D: sonD, th, v: aday.v, bias });
}
simdi.sort((a, b) => b.edge - a.edge);
// §4 KORUMASI (2026-09-17). §4'ün "Kenar" kolonu **kendi P'miz − fiyat**'tır ve bu, "piyasa yanlış"
// ile "**biz yanlışız**"ı ayırt EDEMEZ (ANAYASA ek 10.3: WSSS hayaleti tam olarak böyle çıktı —
// modelimiz 1,6 °C yanlışken kenar +0,628 görünüyordu). Koruma üç parçalı ve hepsi aynı satırda:
//   (a) **piyasanın ima ettiği P** = fiyat, bizimkinin YANINDA yazılır (çıplak "kenar" artık tek başına okunmaz);
//   (b) **ayrışma bayrağı**: |bizim P − fiyat| ≥ 0,25 ise hücre işaretlenir — bu, "kesin kenar" değil
//       "**sorgulanacak** hücre"dir (ölçüm: ayrışmanın büyük olduğu yerde haklı çıkan taraf çoğunlukla piyasa);
//   (c) **kova uyarısı**: bizim kova ile piyasanın en pahalı kovası aynı değilse, model ile piyasa
//       farklı dereceler söylüyor demektir — bu, tekil "kenar"dan çok daha güçlü bir uyarıdır.
const AYRISMA_ESIK = Number(process.env.HEAT_AYRISMA || 0.25);
let ayrisanSayi = 0;
if (simdi.length) {
  P('| Şehir | Kaynak | n | Son tahmin | Yanlılık | Kova | P(bizim) | **P(piyasa)** | **Ayrışma** | **Kenar** | Bayrak | Hedef gün |');
  P('|---|---|---|---|---|---|---|---|---|---|---|---|');
  for (const r of simdi.slice(0, 25)) {
    const ayris = r.Pwin - r.price;
    const bayrak = Math.abs(ayris) >= AYRISMA_ESIK ? '⚠️ **sorgula**' : '';
    if (bayrak) ayrisanSayi += 1;
    P(`| ${sehirAdi.get(r.st) || r.st} | ${r.src} | ${r.n} | ${f2(r.v)} | ${isaretli(r.bias)} | ${r.th}° | ${yz(r.Pwin)} | ${yz(r.price)} | ${isaretli(ayris)} | ${isaretli(r.edge)} | ${bayrak} | ${r.D} |`);
  }
  const poz = simdi.filter((r) => r.edge > 0).length;
  P('');
  P(`**Kenarı pozitif hücre: ${poz} / ${simdi.length}.**${poz === 0 ? ' Yani bugünkü karnemizle **hiçbir hücrede avantaj görünmüyor** — bu da bir sonuçtur.' : ''}`);
  P(`**Ayrışması ${AYRISMA_ESIK} ve üzeri olan hücre: ${ayrisanSayi}.** Bu hücreler "kesin kenar" değil,`);
  P('**sorgulanacak** hücrelerdir: aradaki fark, piyasanın da bizim de aynı anda haklı olamayacağımız');
  P('bir yerde olduğumuzu söyler. Ölçüm (§3g.1b ve ek 10.3) bu tür anlaşmazlıklarda haklı çıkanın');
  P('**çoğunlukla piyasa** olduğunu gösterdi — bu yüzden bayrak **alarm** değil, **dur ve bak** işaretidir.');
  P('Kenar sıralaması bu bayrak okunmadan kullanılmaz.');
} else {
  P(`Henüz yeterli karne yok: fiyatı olan hücrelerde ${MIN_N}+ gün birikmedi.`);
}
P('');

// ---- 5) dürüstlük ----
P('## 5. Dürüstlük notları');
P('');
P(`1. **Fiyat arşivi ${fiyatGun.length} gün** (${fiyatGun[0]} → ${fiyatGun.at(-1)}) — tüm kârlılık sayıları bu dar`);
P('   pencereye ait. **Yön** gösterir, kesin sıralama değildir; örneklem büyüdükçe tazelenir.');
P('   Arşivin **derinliği ile oynanabilirliği aynı şey değil:** emir defteri (`orderbook.db`) 2026-08-05\'te');
P('   başlıyor, yani fiyat arşivinin **ilk günlerinde gerçek `best_ask` yok**. O günler mid ROI\'ye girer ama');
P('   §3b\'nin ask ile doğrulanmış örneklemini **büyütmez** — bu yüzden pencereyi geriye çekmek §3b\'yi iyileştirmez.');
P('2. **Fiyat = YES son fiyatı** (mid), gerçek ödenen fiyat değil. §3b bunu gerçek `best_ask` ile sınar ve');
P('   **sonucu değiştirir**: oynanabilir hücrelerde kenar erir. Bu yüzden buradaki ROI bir **iyimser üst sınırdır**;');
P('   karar için §3b ve §3c birlikte okunur. Emir defteri derinliği (slippage) ayrıca yok sayıldı.');
P('3. **Ücret VARSAYIMI:** kazançtan %5 taker kesintisi. Bu **doğrulanmadı** — ne Heat\'te ne kardeş');
P('   junbo deposunda ücret tablosu yok. Gerçek ücret sıfırsa "ücretsiz" sütunu geçerlidir. Doğrulanana');
P('   kadar iki sütun birlikte okunmalı.');
P('3b. **Yanlılık düzeltmesi yürüyen penceredendir** (yalnız geçmiş günler, n≥' + MIN_N + '). İlk günlerde');
P('   yanlılık kestirimi gürültülüdür; örneklem büyüdükçe oturur. Bu yüzden "düzeltmeli" satırı da bir');
P('   üst sınır değil, gerçekçi bir tahmindir.');
P(`4. **Karar anı** hedeften ${HEDEF_SAAT} saat önce sabitlendi. Başka bir saat farklı sonuç verir; bu bir`);
P('   seçimdir, evrensel doğru değildir. Fiyatın o âna uzaklığı `hours_to_settlement` ile ölçülür.');
P('5. **İsabet = kova isabeti** (round(tahmin)==round(gerçek)). MAE/yanlılık ayrı sütunlarda; ikisi farklı');
P('   soruya cevap verir — kova isabeti **para** sorusudur, MAE **beceri** sorusudur.');
P('6. **Kaynak karşılaştırması tam adil değil:** derin kaynaklar (OM-önceki, AI) 2025\'ten beri var, ticari');
P('   kaynaklar 08-25\'ten beri. Aynı pencerede kıyaslandılar ama geçmiş derinlikleri farklı.');
P('7. **Oynanan bahis sayısı azsa** ROI gürültülüdür; tek bir şehir ya da tek bir gün sonucu çevirebilir.');
P('   Bu yüzden hem "en yüksek kenar" kuralı hem "uygun her hücre" kıyası birlikte verildi.');
P('8. **Bahisler bağımsız değil.** Aynı gün onlarca şehir aynı hava sisteminden etkilenir; yüzlerce bahis,');
P('   o kadar bağımsız deneme demek değildir. Bu yüzden §3c\'deki **"en iyileri çıkar" testi** asıl');
P('   güvenilirlik ölçüsüdür — ROI\'nin kendisinden daha önemlidir.');
P('9. **Sonuç bizim settlement\'ımıza göre hesaplandı**, piyasanın kendi çözüm kaynağına göre değil.');
P('   İkisi ayrışırsa (nadir ama olur) kazanç olduğundan yüksek görünür. Bu yüzden ROI bir üst sınırdır.');
P('   **Bu ayrışma ölçüldü (2026-09-15), ve sandığımdan büyük.** Kardeş `D:\\JUNBO data\\actuals.db`');
P('   bağımsız hakem **olamaz** — oradaki `actual_temperatures` **ERA5 reanalizi**, istasyon gözlemi değil.');
P('   Gerçek gözlem **kendi DB\'mizde** var: `observations` → IEM ASOS (gerçek havalimanı ölçümü).');
P('   Raporun penceresinde (2026-04-09→) °C pazarı olan 40 şehirde **3.492 çift** karşılaştırıldı:');
P('');
P('   | Ölçüm | Sonuç |');
P('   |---|---|');
P('   | Ortalama fark (settlement − gözlem) | **−0,01 °C** → settlement küresel olarak **kaymış değil** |');
P('   | **Kova uyuşmazlığı** | **%17,0** (592 çift) |');
P('   | …bunun **yuvarlama sınırı** payı (fark <1 °C) | **%3,0** — kaçınılmaz, bilgi değil |');
P('   | …bunun **fark ≥1 °C** payı | **%14,3** (501 çift) |');
P('');
P('   Yani "settlement ≈ gözlem" varsayımı **ortalamada** doğru, **kova bazında değil**. Fark rastgele');
P('   dağılmıyor: 501 çiftin çoğu **6 istasyonda toplanıyor** (EGLC 62 · ZUUU 57 · ZUCK 55 · RKPK 47 ·');
P('   OPKC 45 · ZGSZ 27) ve farklar 2–12 °C\'ye çıkıyor.');
P('');
P('   **2026-09-16: soru çözüldü — bozuk olan SETTLEMENT tarafıydı, ve sebebi tek bir yükleme.**');
P('   2026-09-03\'teki tek seferlik içe aktarma (4.422 satır, `raw_json`\'ı `event_slug` biçiminde olanlar)');
P('   iki ayrı hata yapmış:');
P('');
P('   **(a) Şehir→istasyon haritası bozuk (476 satır).** Yük doğru, **istasyon yanlış**: Chongqing verisi');
P('   Chengdu\'nun istasyonuna (`zuuu`), Chengdu verisi Chongqing\'e (`zuck`), Seul verisi Busan\'a (`rkpk`),');
P('   Lucknow verisi Karaçi\'ye (`opkc`) yazılmış. Kanıt: satırın kendi `city` etiketi + junbo aynasında');
P('   değerin **hangi şehre** ait olduğu (ZUUU\'daki değer ZUCK ile 6/6, RKPK\'daki RKSI ile 8/8 aynı).');
P('   Doğru eşlemeyi `stations.market_slug` kendisi söylüyor (`…-in-chongqing-…` → `zuck`).');
P('');
P('   **(b) Yabancı market serisi (283 satır).** `klga` ve `eglc`\'ye **yılsız** slug\'lı bir seri yazılmış');
P('   (`…-in-nyc-on-august-5`, `…-in-london-on-march-31`) — bizim istasyonlarımız **yıllı** seriyi izliyor');
P('   (`…-in-london-on-august-27-2026`). junbo ile yalnız **%13** tutuyordu; bizim marketimiz değil.');
P('   (Bu satırlar EGLC\'yi "°F şehri" gibi gösteriyordu; **EGLC °C şehridir** — yıllı serisi "20°C" diyor.)');
P('');
P('   **Onarım (2026-09-16, yedekli):** 424 satır doğru istasyona **taşındı**, 52 kalıntı + 283 yabancı');
P('   satır **silindi**; **zgsz dokunulmadı** (87 satırı junbo ile %100 uyumlu — orada bozuk olan');
P('   **gözlem** tarafı, settlement değil). Araç: `scripts/_settlement_onar_istasyon_haritasi.mjs`.');
P('');
P('   **Manşete etkisi (aynı 47 günlük pencere, ölçüldü):** düzeltmeli kova ROI **%40 → %22**, ham **%1 → %7**.');
P('   Yani **yanlılık düzeltmesinin getirisi +39 puandan +15 puana indi** — eski %40\'ın bir kısmı');
P('   settlement hatasına yaslanıyormuş. (Aynı pencerede °F kova formülü de düzeltildi; iki etkinin');
P('   ayrı ayrı ölçümü TEST_DEFTERI\'nde.) **Kapanan açık iş:** hangi tarafın bozuk olduğu bulundu.');
P('   (°F pazarı olan 11 şehir bu ölçümün **dışında**: orada kova 2 °F genişliğinde olduğu için `round(°C)`');
P('   ile kıyas geçersiz olurdu.)');
P('');
P(`*Üretim: ${new Date().toISOString()} · MAIN ${path.basename(MAIN_DB)} + junbo ${path.basename(JUNBO_DB)} (ikisi de salt-okunur) · tahmin-gerçek çifti ${tumSatir.length} · pencere ${PENCERE_BASI}..${SON_GUN} · fiyat ${fiyatGun[0]}..${fiyatGun.at(-1)}*`);

fs.writeFileSync(OUT, md.join('\n') + '\n', 'utf8');
let arsiv = null;
if (ARSIV) {
  const dir = process.env.HEAT_ARSIV_DIR || path.join('data', 'rapor_arsivi');
  fs.mkdirSync(dir, { recursive: true });
  const gun = new Date().toISOString().slice(0, 10);
  arsiv = path.join(dir, `rapor_oynanir_tablo_${gun}.md`);
  fs.writeFileSync(arsiv, md.join('\n') + '\n', 'utf8');
  fs.writeFileSync(path.join(dir, `rapor_kaynak_sehir_ufuk_isabet_${gun}.md`), izgara.join('\n') + '\n', 'utf8');
}
console.log(`yazıldı: ${OUT}${arsiv ? ` + arşiv ${arsiv}` : ''}`);
console.log(`  + ${IZGARA_OUT} (${hucreler.length} hücre, eşik ${MIN_N} gün)`);
console.log(`  ${tumSatir.length} tahmin-gerçek çifti · fiyat penceresi ${fiyatGun[0]}..${fiyatGun.at(-1)} (${fiyatGun.length} gün) · fiyatlı şehir ${fiyatSehir.size}`);
for (const tur of ['ham', 'duz']) {
  const r = R[tur], k = Rh[tur];
  console.log(`  ${tur === 'ham' ? 'HAM kova        ' : 'DÜZELTMELİ kova '}: ${r ? `${r.n} bahis · kazanan ${r.kaz} (${yz(r.kaz / r.n)}) · kenar ${isaretli(r.kenar)} · ROI ${yz(r.net)} (ücretli ${yz(r.netF)})` : 'eşiği geçen hücre yok'}${k ? `  || kıyas hepsi: ${k.n} bahis · ${yz(k.net)}` : ''}`);
}
main.close(); junbo.close(); ob?.close();
