#!/usr/bin/env node
// Ne işe yarar: **Heat'in KENDİ tahmini gerçeği ne kadar tutuyor?** — üretimde her döngüde yazılan
//   `prediction_log` satırlarını Polymarket settlement'ıyla karşılaştırır ve `rapor_heat_isabet.md` üretir.
//   Runner'ın 08:00 UTC bloğunda koşar; çıktıyı data/rapor_arsivi/ altına gün adıyla arşivler.
//
// NEDEN AYRI BİR RAPOR: mevcut raporlar KAYNAKLARI ölçer (`rapor_iskabet_t02.md` = her sağlayıcı tek tek,
//   `rapor_bias_gunluk.md` = düzeltmenin kaynak başına kazancı). Hiçbiri "Heat'in nihai tahmini" olan
//   `mosForecastC`'yi ölçmez. `rapor_mos_deney.md` ise MOS yöntemini GEÇMİŞ veriyle ispatlayan bir deneydir,
//   üretimin canlı skoru değildir. Bu rapor tam o boşluğu kapatır: "biz ne dedik, ne oldu".
//
// TAHMİN KAYNAĞI: `prediction_log` (MAIN). `reports` tablosu UNIQUE(station_id,target_date) olduğu için her
//   döngü önceki raporu eziyordu → t1/t2 geçmişi kayboluyordu. Defter ezmiyor; her döngü ayrı satır.
//   Ufuk (t0/t1/t2) satırdaki `horizon_days` alanındadır — runner onu istasyonun KENDİ saat dilimine göre
//   hesaplar (UTC farkı Pasifik/Avustralya'da günü kaydırıp ufku yanlış sayardı).
//
// SKORLAMA KURALI (kanonik `rapor_iskabet_t02.mjs` ile aynı):
//   - Gerçek = settlements.high_c, her (şehir, gün) için EN SON recorded_at_utc'li satır.
//   - Bir (şehir, hedef gün, ufuk) hücresi için o ufuktaki **EN SON** tahmin alınır (gün içinde tahmin
//     olgunlaşır; "son sözümüz" ölçülür).
//   - t0 = aynı gün (karar için GEÇTİR), t1 = yarın (karar ufku), t2 = öbür gün.
//
// Kullanım: node --experimental-sqlite scripts/rapor_heat_isabet.mjs
//   HEAT_OUT (varsayılan rapor_heat_isabet.md) · HEAT_ARSIV=0 ile arşiv kapatılır
//   HEAT_BENCH_PROVIDER (varsayılan 'Open-Meteo icon_seamless') — §5 tek-kaynak kıyası için
//   HEAT_MIN_N (varsayılan 5) — bir şehrin tabloya girmesi için en az kaç skorlu tahmin
// SALT-OKUNUR: MAIN {readOnly:true} açılır, DB'ye hiçbir şey yazılmaz (yalnız .md yazılır).

import path from 'node:path';
import fs from 'node:fs';
import { DatabaseSync } from 'node:sqlite';

const MAIN_DB = process.env.HEAT_MAIN_DB || 'C:/HEAT data/db/tempmarket-edge.sqlite';
const OUT = process.env.HEAT_OUT || 'rapor_heat_isabet.md';
const ARSIV = process.env.HEAT_ARSIV !== '0';
const BENCH = process.env.HEAT_BENCH_PROVIDER || 'Open-Meteo icon_seamless';
const MIN_N = Number(process.env.HEAT_MIN_N || 5);

const md = [];
const P = (s = '') => md.push(s);
const fmt = (x, d = 2) => (x == null || !Number.isFinite(x) ? '-' : x.toFixed(d));
const yuzde = (x) => (x == null || !Number.isFinite(x) ? '-' : `${Math.round(x * 100)}%`);

const db = new DatabaseSync(MAIN_DB, { readOnly: true }); // Kural 9/11: rapor salt-okunur okur

// ---- 1) Gerçek (settlement): her (şehir, gün) için en son kayıt ----
const gercek = new Map();
for (const r of db.prepare(`
  SELECT station_id, target_date, high_c,
         ROW_NUMBER() OVER (PARTITION BY station_id, target_date ORDER BY recorded_at_utc DESC) rn
  FROM settlements`).all()) {
  if (r.rn === 1 && r.high_c != null) gercek.set(`${r.station_id}|${r.target_date}`, Number(r.high_c));
}

// ---- 2) Şehir adları (tablolarda okunur isim olsun) ----
const sehirAdi = new Map();
for (const s of db.prepare('SELECT id, icao, city FROM stations').all()) {
  sehirAdi.set(String(s.id), `${s.icao || String(s.id).toUpperCase()} ${s.city || ''}`.trim());
}

// ---- 3) Tahmin defteri: her (şehir, gün, ufuk) için EN SON tahmin ----
const hamSatirlar = db.prepare(`
  SELECT station_id, target_date, horizon_days, run_at_utc, raw_prediction, final_prediction, model_version,
         ROW_NUMBER() OVER (PARTITION BY station_id, target_date, horizon_days ORDER BY run_at_utc DESC) rn
  FROM prediction_log WHERE final_prediction IS NOT NULL`).all();
const tahminler = hamSatirlar.filter((r) => r.rn === 1);

// ---- 4) Skorla ----
const skorlu = [];
for (const t of tahminler) {
  const g = gercek.get(`${t.station_id}|${t.target_date}`);
  if (g == null) continue;
  const ufuk = Number(t.horizon_days);
  if (!Number.isFinite(ufuk) || ufuk < 0 || ufuk > 3) continue;
  const hata = Number(t.final_prediction) - g;
  skorlu.push({
    ...t, ufuk, gercek: g, hata,
    ae: Math.abs(hata),
    hamAe: t.raw_prediction != null ? Math.abs(Number(t.raw_prediction) - g) : null,
  });
}

function istatistik(liste, alan = 'ae') {
  const d = liste.map((r) => r[alan]).filter((x) => Number.isFinite(x));
  if (!d.length) return null;
  const n = d.length;
  const mae = d.reduce((s, x) => s + x, 0) / n;
  const rmse = Math.sqrt(d.reduce((s, x) => s + x * x, 0) / n);
  const sapma = liste.map((r) => r.hata).reduce((s, x) => s + x, 0) / n;
  return {
    n, mae, rmse, sapma,
    hit1: d.filter((x) => x <= 1).length / n,
    hit2: d.filter((x) => x <= 2).length / n,
    sehir: new Set(liste.map((r) => r.station_id)).size,
  };
}

if (!skorlu.length) {
  console.error('Skorlanabilir tahmin yok: prediction_log boş ya da bu şehir-günlerin settlement\'ı yok.');
  console.error('Defteri geçmiş raporlardan doldurmak için: node --experimental-sqlite scripts/_tahmin_defteri_geri_doldur.mjs --uygula');
  db.close();
  process.exit(1);
}

const ufuklar = [0, 1, 2, 3].map((h) => ({ h, rows: skorlu.filter((r) => r.ufuk === h) })).filter((u) => u.rows.length);
const tumu = skorlu;
const gunler = [...new Set(skorlu.map((r) => r.target_date))].sort();
const kaynaklar = [...new Set(skorlu.map((r) => r.model_version))].sort();
const dosyaTarih = new Date().toISOString().slice(0, 10);

// ================= RAPOR =================
P('# Heat kendi tahminini ne kadar tutturuyor?');
P('');
P('> Ölçümdür; Heat trade yapmaz. **Tahmin = Heat\'in kendi nihai tahmini** (`mosForecastC`; MOS yoksa ham');
P('> ortalama). **Gerçek = Polymarket settlement** (o günün gerçekleşen en yüksek sıcaklığı).');
P('> Bu rapor her gün otomatik koşar; aşağıdaki sayılar aşağıdaki kapsama aittir.');
P('');

// ---- 0) künye ----
P('## 0. Künye');
P('');
P(`- **Ölçülen:** ${skorlu.length} skorlu tahmin · ${new Set(skorlu.map((r) => r.station_id)).size} şehir · ${gunler.length} hedef gün (${gunler[0]} → ${gunler.at(-1)}).`);
P(`- **Tahmin kaynağı:** MAIN.prediction_log — ${tahminler.length} tahmin satırından ${skorlu.length}'i gerçeğiyle eşleşti.`);
P(`- **Satır türleri:** ${kaynaklar.join(', ')}.`);
P('- **Ufuk:** **t1 = yarın** (karar ufku) · t0 = aynı gün (karar için GEÇTİR) · t2 = öbür gün.');
P('- **Tekilleştirme:** her (şehir, hedef gün, ufuk) için o ufuktaki **en son** tahmin alındı.');
P('');
P('> **Kapsam uyarısı (Kural 12):** "kaç gün var" tek sayı değildir — ufuk başına ayrı okunur ve');
P('> "kaynak başına N gün" ≠ "hepsi birlikte N gün". Aşağıdaki her tablo kendi kapsamını yazar.');
P('');

// ---- 1) ufuk bazında isabet ----
P('## 1. Ufka göre isabetimiz');
P('');
P('| Ufuk | n | Şehir | MAE | RMSE | Yanlılık | ≤1° | ≤2° |');
P('|---|---|---|---|---|---|---|---|');
for (const { h, rows } of ufuklar) {
  const s = istatistik(rows);
  const etiket = h === 0 ? 't0 (aynı gün)' : h === 1 ? '**t1 (yarın)**' : `t${h}`;
  P(`| ${etiket} | ${s.n} | ${s.sehir} | **${fmt(s.mae)}** | ${fmt(s.rmse)} | ${fmt(s.sapma)} | ${yuzde(s.hit1)} | ${yuzde(s.hit2)} |`);
}
const hepsi = istatistik(tumu);
P(`| **hepsi** | ${hepsi.n} | ${hepsi.sehir} | **${fmt(hepsi.mae)}** | ${fmt(hepsi.rmse)} | ${fmt(hepsi.sapma)} | ${yuzde(hepsi.hit1)} | ${yuzde(hepsi.hit2)} |`);
P('');
P('> **Nasıl okunur:** MAE = ortalama mutlak hata (°C, küçük iyi). Yanlılık artıysa **sıcak**, eksiyse **soğuk**');
P('> söylüyoruz. t0 karar için geçtir (gün içinde gerçek zaten görülmeye başlar) — asıl ölçü **t1**dir.');
P('');

// ---- 2) MOS vs ham ortalama ----
P('## 2. MOS düzeltmesi bizde işe yarıyor mu?');
P('');
P('İki tahmin **aynı hücrelerde** kıyaslanır: ham ortalama (düzeltilmemiş) ve Heat nihai (MOS varsa MOS).');
P('');
P('| Ufuk | n | Ham MAE | Heat MAE | Kazanç | Heat daha iyi | Heat daha kötü |');
P('|---|---|---|---|---|---|---|');
let toplamKazanc = 0, toplamKiyas = 0;
for (const { h, rows } of ufuklar) {
  const cift = rows.filter((r) => r.hamAe != null);
  if (!cift.length) continue;
  const ham = istatistik(cift, 'hamAe');
  const heat = istatistik(cift, 'ae');
  const iyi = cift.filter((r) => r.ae < r.hamAe).length;
  const kotu = cift.filter((r) => r.ae > r.hamAe).length;
  toplamKazanc += ham.mae - heat.mae; toplamKiyas += 1;
  P(`| ${h === 1 ? '**t1**' : `t${h}`} | ${cift.length} | ${fmt(ham.mae)} | **${fmt(heat.mae)}** | ${ham.mae - heat.mae >= 0 ? '+' : ''}${fmt(ham.mae - heat.mae)} | ${iyi} | ${kotu} |`);
}
if (toplamKiyas) {
  P('');
  P(`**Özet:** ${toplamKiyas} ufkun ${ufuklar.filter((u) => u.rows.some((r) => r.hamAe != null)).filter((u) => {
    const c = u.rows.filter((r) => r.hamAe != null); const ham = istatistik(c, 'hamAe'); const heat = istatistik(c, 'ae');
    return heat.mae < ham.mae;
  }).length}'inde MOS ham ortalamayı geçti.`);
}
P('');

// ---- 3) şehir bazında (t1 ağırlıklı) ----
const t1 = (ufuklar.find((u) => u.h === 1) || { rows: [] }).rows;
const sehirT1 = [...new Set(t1.map((r) => r.station_id))]
  .map((id) => ({ id, rows: t1.filter((r) => r.station_id === id) }))
  .filter((c) => c.rows.length >= MIN_N)
  .map((c) => ({ ...c, s: istatistik(c.rows) }))
  .sort((a, b) => a.s.mae - b.s.mae);
if (sehirT1.length) {
  P(`## 3. Şehir bazında (t1 = yarın · en az ${MIN_N} tahmin)`);
  P('');
  P('| # | Şehir | n | MAE | Yanlılık | ≤1° |');
  P('|---|---|---|---|---|---|');
  sehirT1.forEach((c, i) => P(`| ${i + 1} | ${sehirAdi.get(c.id) || c.id} | ${c.s.n} | **${fmt(c.s.mae)}** | ${fmt(c.s.sapma)} | ${yuzde(c.s.hit1)} |`));
  P('');
  P(`**En iyi:** ${sehirAdi.get(sehirT1[0].id) || sehirT1[0].id} (MAE ${fmt(sehirT1[0].s.mae)}) · **en kötü:** ${sehirAdi.get(sehirT1.at(-1).id) || sehirT1.at(-1).id} (MAE ${fmt(sehirT1.at(-1).s.mae)}).`);
  P('');
} else {
  P('## 3. Şehir bazında (t1)');
  P('');
  P(`Henüz yeterli t1 tahmini yok (bir şehir için en az ${MIN_N} gerekiyor). Runner yeni kodla koştukça birikecek.`);
  P('');
}

// ---- 4) gün gün ----
P('## 4. Gün gün: iyileşiyor muyuz?');
P('');
P('| Hedef gün | n | MAE | ≤1° |');
P('|---|---|---|---|');
for (const g of gunler) {
  const s = istatistik(skorlu.filter((r) => r.target_date === g));
  P(`| ${g} | ${s.n} | ${fmt(s.mae)} | ${yuzde(s.hit1)} |`);
}
P('');

// ---- 5) tek en iyi kaynakla kıyas ----
// Aynı (şehir, hedef gün, ufuk) hücresinde BENCH sağlayıcısının o ufuktaki en son tahmini.
const benchRows = db.prepare(`
  SELECT station_id, target_date, horizon_days, predicted_high_c,
         ROW_NUMBER() OVER (PARTITION BY station_id, target_date, horizon_days ORDER BY run_at_utc DESC) rn
  FROM forecast_snapshots WHERE provider = ?`).all(BENCH);
const bench = new Map();
for (const b of benchRows) if (b.rn === 1) bench.set(`${b.station_id}|${b.target_date}|${b.horizon_days}`, Number(b.predicted_high_c));
const benchKiyas = skorlu
  .map((r) => {
    const v = bench.get(`${r.station_id}|${r.target_date}|${r.ufuk}`);
    return v == null ? null : { ...r, benchAe: Math.abs(v - r.gercek) };
  })
  .filter(Boolean);
if (benchKiyas.length) {
  P(`## 5. Tek en iyi kaynakla kıyas: \`${BENCH}\``);
  P('');
  P('"Hepsini birleştirmek" yerine **tek bir iyi kaynağı olduğu gibi kullanmak** yeterli miydi?');
  P('Aynı hücrelerde kıyas:');
  P('');
  P('| Ufuk | n | Tek kaynak MAE | Heat MAE | Kazanç | Heat daha iyi | Tek kaynak daha iyi |');
  P('|---|---|---|---|---|---|---|');
  for (const h of [0, 1, 2, 3]) {
    const c = benchKiyas.filter((r) => r.ufuk === h);
    if (!c.length) continue;
    const b = istatistik(c, 'benchAe');
    const t = istatistik(c, 'ae');
    const iyi = c.filter((r) => r.ae < r.benchAe).length;
    const kotu = c.filter((r) => r.ae > r.benchAe).length;
    P(`| ${h === 1 ? '**t1**' : `t${h}`} | ${c.length} | ${fmt(b.mae)} | **${fmt(t.mae)}** | ${b.mae - t.mae >= 0 ? '+' : ''}${fmt(b.mae - t.mae)} | ${iyi} | ${kotu} |`);
  }
  P('');
} else {
  P('## 5. Tek en iyi kaynakla kıyas');
  P('');
  P(`\`${BENCH}\` için örtüşen hücre bulunamadı — kıyas yapılamadı.`);
  P('');
}

// ---- 6) dürüstlük ----
P('## 6. Dürüstlük notları');
P('');
P(`1. **Kapsam dengesiz.** t0 = ${(ufuklar.find((u) => u.h === 0) || { rows: [] }).rows.length} tahmin ama t0 karar için`);
P('   geçtir: o saatte günün gerçeği kısmen görülmüştür. Asıl karar ufku **t1**dir ve oradaki örnek sayısı');
P('   yukarıdaki tabloda yazılıdır — azsa sıralama değil **yön** okunur.');
P('2. **Geçmiş eksik, uydurulmadı.** `reports` tablosu her döngüde önceki raporu ezdiği için t1/t2 geçmişi');
P('   kaybolmuştu; defter (`prediction_log`) 2026-09-15\'te devreye girdi. Ondan öncesi için yalnız');
P('   `reports`tan kurtarılabilen kısım var (`model_version` = `reports-backfill`), ve o kısım t0 ağırlıklıdır.');
P('   Yeni ufuklar ancak runner yeni kodla koştukça birikir — bu raporda "şimdilik az" diye yazılır, tahmin edilmez.');
P('3. **Tekilleştirme seçimi sonucu etkiler:** her hücrede o ufuktaki EN SON tahmin alındı. Gün içi ilk');
P('   tahminle kıyaslamak farklı bir sayı verir; bu rapor "son sözümüz"ü ölçer.');
P('4. **Yanlılık ±°C**: artı = sıcak söylüyoruz. Sistematik yanlılık varsa §2\'deki düzeltme onu kapatmalıdır;');
P('   kapatmıyorsa düzeltme o şehirde/ufukta çalışmıyor demektir.');
if (kaynaklar.some((k) => k.startsWith('reports-backfill'))) {
  P('5. **`reports-backfill` satırları** kayıtlı raporlardan çıkarıldı (ufuk, raporun üretim anı ile hedef gün');
  P('   farkından, istasyonun kendi saat diliminde hesaplandı). `mos-v1`/`naive-v1` satırları ise runner\'ın');
  P('   yeni tahmin defterinden gelir ve ufku doğrudan taşır.');
}
P('');
P(`*Üretim: ${new Date().toISOString()} · kaynak ${path.basename(MAIN_DB)} (salt-okunur) · \`node scripts/rapor_heat_isabet.mjs\` · ${gunler[0]}..${gunler.at(-1)} · ${skorlu.length} skorlu tahmin · kıyas kaynağı ${BENCH}*`);

fs.writeFileSync(OUT, md.join('\n') + '\n', 'utf8');
let arsivYolu = null;
if (ARSIV) {
  const dir = process.env.HEAT_ARSIV_DIR || path.join('data', 'rapor_arsivi');
  fs.mkdirSync(dir, { recursive: true });
  arsivYolu = path.join(dir, `rapor_heat_isabet_${dosyaTarih}.md`);
  fs.writeFileSync(arsivYolu, md.join('\n') + '\n', 'utf8');
}
const t1s = istatistik(t1);
console.log(`yazıldı: ${OUT}${arsivYolu ? ` + arşiv ${arsivYolu}` : ''}`);
console.log(`  ${skorlu.length} skorlu tahmin · ${new Set(skorlu.map((r) => r.station_id)).size} şehir · ${gunler[0]}..${gunler.at(-1)}`);
if (t1s) console.log(`  t1 (karar ufku): n=${t1s.n} MAE=${fmt(t1s.mae)} hit≤1=${yuzde(t1s.hit1)}`);
db.close();
