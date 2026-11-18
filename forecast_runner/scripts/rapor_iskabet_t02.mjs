#!/usr/bin/env node
// KANONİK t0-2 İSKABET SİSTEMİ (2026-09-05): geçmiş tahminleri ufuk (t0/t1/t2) × kaynak bazında
// gerçek settlement ile karşılaştırır; şehir × kaynak isabetini gösterir.
//
// MANTIK (doğrulanmış kurallar):
//   - Gerçek (truth) = settlements.high_c, yalnız target_date <= bugün. MAIN settlements 2026-09-05'te
//     onarıldı: 8.824 gerçek (derin <=08-24 + LIVE kopyası 398). Gelecek sahtesi YOK.
//   - Tahmin = her (provider, station, target, horizon) için SON run (run_at desc ilk).
//   - Horizon t0=aynı-gün, t1=yazın (day-ahead), t2=öbür gün. Kaynakların horizon etiketi bununla eşleşir
//     (AIWP h=hedef-gün farkı; OM previous h=1..2; canlı runner h=0..2; OM arşivi h=0 yalnız).
//   - Hangi DB'den: HEPSİ MAIN (C:\HEAT data\db). Canlı aileler de 2026-09-07'den itibaren MAIN'e arşivleniyor
//     (scripts/_archive_canli_forecasts.mjs, runner UTC 08:00 tetikler); MAIN canlının LIVE'ın üst kümesi aynasıdır.
//   - HARİÇ: "…daily" Temmuz deney artığı (provider="X daily", source=model adı), NWS derin 2021-22
//     (settlement yok → skorlanamaz), gelecek hedefler.
//   - t0 uyarısı: OM arşivi/aynı-gün koşuları fiilen gerçekleşene yakın olabilir; kaynak kıyası t1/t2 esas.
//
// Kullanım: node --experimental-sqlite scripts/rapor_iskabet_t02.mjs   (RAPOR_TODAY=YYYY-MM-DD ile sabitlenebilir)
import path from 'node:path';
import fs from 'node:fs';
import { DatabaseSync } from 'node:sqlite';

const TODAY = process.env.RAPOR_TODAY || new Date().toISOString().slice(0, 10);
const SINCE = process.env.RAPOR_SINCE || null;            // alt sınır (son-1-ay testi: RAPOR_SINCE=2026-08-05)
const OUT = process.env.RAPOR_OUT || 'rapor_iskabet_t02.md'; // çıktı dosyası
const MIN_DEEP = process.env.RAPOR_MINDEEP ? Number(process.env.RAPOR_MINDEEP) : 30; // derin şehir-kazananı min gün
const MAIN_DB = process.env.HEAT_MAIN_DB || 'C:/HEAT data/db/tempmarket-edge.sqlite';
const LIVE_DB = path.resolve('data/tempmarket-edge.sqlite');
const CONFIG = JSON.parse(fs.readFileSync(path.resolve('windows-runner/config.json'), 'utf8'));
const stName = Object.fromEntries(CONFIG.stations.map((s) => [s.id, `${s.icao} ${s.city}`]));

const ARCHIVE = ['ecmwf_ifs025', 'gem_global', 'gfs_seamless', 'icon_seamless', 'jma_seamless', 'meteofrance_seamless', 'ukmo_seamless', 'cma_grapes_global'];
// her kaynak: hangi DB, hangi provider adları, kısa etiket, aile
const SRC = {
  'Open-Meteo önceki': { db: 'MAIN', provs: ['Open-Meteo previous'] },
  'Pangu-Weather': { db: 'MAIN', provs: ['Pangu-Weather'] },
  'FourCastNet v2': { db: 'MAIN', provs: ['FourCastNet v2'] },
  'GraphCast': { db: 'MAIN', provs: ['GraphCast'] },
  ...Object.fromEntries(ARCHIVE.map((m) => [m, { db: 'MAIN', provs: [m] }])),
  'NOAA GFS': { db: 'MAIN', provs: ['NOAA GFS daily'] },
  'Visual Crossing': { db: 'MAIN', provs: ['Visual Crossing daily'] },
  'WeatherAPI': { db: 'MAIN', provs: ['WeatherAPI daily'] },
  'OpenWeatherMap': { db: 'MAIN', provs: ['OpenWeatherMap 3-hour'] },
  'NWS (canlı)': { db: 'MAIN', provs: ['NWS forecastHourly'] },
  'OM-ECMWF04': { db: 'MAIN', provs: ['Open-Meteo ecmwf_ifs04'] }, // tarihsel (ad ölü, pred=0)
  'OM-IFS025': { db: 'MAIN', provs: ['Open-Meteo ecmwf_ifs025'] }, // 2026-09-15'ten beri canlı akış
  'OM-GFS': { db: 'MAIN', provs: ['Open-Meteo gfs_seamless'] },
  'OM-ICON': { db: 'MAIN', provs: ['Open-Meteo icon_seamless'] },
};
// aile sınıflandırması (özet için)
const FAMILY = {};
for (const k of Object.keys(SRC)) {
  FAMILY[k] = ['Pangu-Weather', 'FourCastNet v2', 'GraphCast'].includes(k) ? 'AI (NOAA AIWP)'
    : k === 'Open-Meteo önceki' ? 'Geleneksel NWP (anonim)'
    : ARCHIVE.includes(k) ? 'OM arşiv NWP (t0)'
    : ['Visual Crossing', 'WeatherAPI', 'OpenWeatherMap', 'NWS (canlı)'].includes(k) ? 'Canlı API'
    : k === 'NOAA GFS' ? 'NOAA GFS (2024-25)'
    : 'OM canlı per-model';
}

const dbCache = {};
const BROKEN = new Set(['OM-ECMWF04']); // canlı ecmwf_ifs04 akışı bilinen bozuk (pred=0) — sıralama/özetten hariç
function open(tag) {
  if (!dbCache[tag]) dbCache[tag] = new DatabaseSync(tag === 'MAIN' ? MAIN_DB : LIVE_DB, { readOnly: true });
  return dbCache[tag];
}

// provider adları -> DB'ler; tek sorguda o DB'nin tüm istenen providerları
function loadRows() {
  const rows = [];
  for (const [pShort, cfg] of Object.entries(SRC)) {
    const db = open(cfg.db);
    const ph = cfg.provs.map(() => '?').join(',');
    const found = db.prepare(`
      WITH latest AS (
        SELECT fs.station_id st, fs.target_date td, fs.horizon_days h, fs.predicted_high_c v,
               ROW_NUMBER() OVER (PARTITION BY fs.provider, fs.station_id, fs.target_date, fs.horizon_days
                                  ORDER BY fs.run_at_utc DESC) rn
        FROM forecast_snapshots fs
        WHERE fs.target_date <= ? ${SINCE ? 'AND fs.target_date >= ?' : ''}
          AND fs.horizon_days IN (0,1,2) AND fs.provider IN (${ph})
          AND EXISTS (SELECT 1 FROM settlements s2
                      WHERE s2.station_id = fs.station_id AND s2.target_date = fs.target_date)
      )
      SELECT l.st, l.td, l.h, l.v,
             (SELECT s.high_c FROM settlements s
              WHERE s.station_id=l.st AND s.target_date=l.td
              ORDER BY s.recorded_at_utc DESC LIMIT 1) actual
      FROM latest l WHERE l.rn=1
    `).all(...(SINCE ? [TODAY, SINCE] : [TODAY]), ...cfg.provs);
    for (const r of found) rows.push({ ps: pShort, fam: FAMILY[pShort], ...r });
  }
  return rows;
}

const R = loadRows();

function agg(list) {
  const n = list.length;
  let e = 0, e2 = 0, b = 0, hit1 = 0, hit2 = 0;
  const cities = new Set(), days = new Set(); let mn = '9999', mx = '';
  for (const r of list) { const d = r.v - r.actual; const a = Math.abs(d);
    e += a; e2 += d * d; b += d; if (a <= 1) hit1++; if (a <= 2) hit2++;
    cities.add(r.st); days.add(r.td); if (r.td < mn) mn = r.td; if (r.td > mx) mx = r.td; }
  return { n, mae: n ? e / n : NaN, rmse: n ? Math.sqrt(e2 / n) : NaN, bias: n ? b / n : NaN,
    hit1: n ? hit1 / n : NaN, hit2: n ? hit2 / n : NaN, cities: cities.size, days: days.size,
    range: mx ? `${mn}..${mx}` : '-' };
}
const fmt = (x) => (Number.isFinite(x) ? x.toFixed(2) : '-');
const pct = (x) => (Number.isFinite(x) ? (100 * x).toFixed(0) : '-');
const DEEP_SRC = new Set(['Open-Meteo önceki', 'Pangu-Weather', 'FourCastNet v2', 'GraphCast', 'NOAA GFS']);
const LIVE_SRC = new Set(['Visual Crossing', 'WeatherAPI', 'OpenWeatherMap', 'NWS (canlı)', 'OM-GFS', 'OM-ICON']);

// şehir bazında "en iyi kaynak" tablosu; deep=uzun kapsam (>=minDeepGun), live=canlı pencere (>=3 şehir-gün)
function cityWinnerMd(rows, titleDeep, titleLive) {
  const out = [];
  const byCity = new Map();
  for (const r of rows) if (!BROKEN.has(r.ps)) { if (!byCity.has(r.st)) byCity.set(r.st, []); byCity.get(r.st).push(r); }
  const pick = (list, srcSet, minDays) => {
    const per = new Map();
    for (const r of list) if (srcSet.has(r.ps)) { if (!per.has(r.ps)) per.set(r.ps, []); per.get(r.ps).push(r); }
    return [...per.entries()].map(([ps, l]) => { const a = agg(l); return { ps, a }; })
      .filter((c) => c.a.days >= minDays).sort((a, b) => a.a.mae - b.a.mae);
  };
  for (const [title, srcSet, minDays] of [[titleDeep, DEEP_SRC, MIN_DEEP], [titleLive, LIVE_SRC, 3]]) {
    const lines = [];
    for (const [st, list] of byCity) {
      const cands = pick(list, srcSet, minDays);
      if (!cands.length) continue;
      const b = cands[0];
      lines.push({ st, best: b.ps, mae: b.a.mae, n: b.a.n,
        all: cands.slice(0, 4).map((c) => `${c.ps} ${fmt(c.a.mae)}(${c.a.n})`).join(' · ') });
    }
    lines.sort((a, b) => a.mae - b.mae);
    out.push(`**${title}** (en az ${minDays} farklı gün verisi olanlar)`);
    out.push('');
    out.push('| Şehir | En iyi kaynak | MAE | (n) | Diğerleri (MAE,n) |');
    out.push('|---|---|---|---|---|');
    for (const r of lines) out.push(`| ${stName[r.st] || r.st} | **${r.best}** | **${fmt(r.mae)}** | ${r.n} | ${r.all} |`);
    out.push('');
  }
  return out;
}

const md = [];
md.push('# Kaynak × Ufuk (t0/t1/t2) İsabet Raporu — geçmiş tahminler ne kadar tuttu?');
md.push('');
md.push(`Üretim: ${new Date().toISOString().slice(0, 10)} · Gerçekleşen = Polymarket settlement (hedef ≤ ${TODAY}) · Tahmin = (kaynak, şehir, hedef, ufuk) için **en son koşu**.`);
if (SINCE) md.push(`**Pencere: ${SINCE} .. ${TODAY} (yalnız bu aralıktaki hedef günler).**`);
md.push('');
md.push('**Kurallar:** gerçek yalnız `settlements.high_c` (geçmiş, gerçek). MAIN settlements 2026-09-05 onarıldı (gelecek sahtesi yok, canlı gerçeğiyle eşitlendi). "…daily" deney artığı ve skorlanamayan derin NWS (2021-22) dışarıda. t1/t2 kaynak kıyası için esas ufuklardır; t0 aynı-gün koşusu gerçekleşene yakın olabilir.');
md.push('');
md.push('---');
md.push('');

// §1 kapsam matrisi: her kaynak x ufuk -> kaç şehir-gün gerçek skor var
md.push('## 1. Elimizdeki geçmiş: kaynak × ufuk kapsamı (kaç şehir-gün gerçekle karşılaştırılabilir)');
md.push('');
md.push('| Kaynak | Aile | t | n (şehir-gün) | şehir | benzersiz gün | tarih aralığı |');
md.push('|---|---|---|---|---|---|---|');
{
  const key = (r) => `${r.ps}|${r.h}`;
  const g = new Map();
  for (const r of R) { const k = key(r); if (!g.has(k)) g.set(k, []); g.get(k).push(r); }
  const order = (a, b) => b[1][0].h - a[1][0].h || (b[1].length - a[1].length); // önce t1/derin kapsam
  for (const [k, list] of [...g.entries()].sort((x, y) => x[1][0].h - y[1][0].h || x[0].localeCompare(y[0]))) {
    const [ps, h] = k.split('|');
    const a = agg(list);
    md.push(`| ${ps} | ${FAMILY[ps]} | t${h} | ${a.n} | ${a.cities} | ${a.days} | ${a.range} |`);
  }
  void order;
}
md.push('');

// §2 t1 tablosu
md.push('## 2. t1 (yarınki en yüksek) — kaynak bazında isabet');
md.push('');
md.push('> En karar verici ufuk: bir gün önceden "yarın ne kadar" denmiş. MAE küçük = iyi. bias + → sıcak söylemiş, − → soğuk söylemiş.');
md.push('');
md.push('| Kaynak | Aile | MAE | bias | hit≤1° | hit≤2° | n | gün |');
md.push('|---|---|---|---|---|---|---|---|');
for (const [ps, list] of groupByHorizon(1)) {
  const a = agg(list);
  md.push(`| ${ps}${BROKEN.has(ps) ? ' ⚠bozuk' : ''} | ${FAMILY[ps]} | **${fmt(a.mae)}** | ${fmt(a.bias)} | ${pct(a.hit1)}% | ${pct(a.hit2)}% | ${a.n} | ${a.days} |`);
}
md.push('');

function groupByHorizon(h) {
  const m = new Map();
  for (const r of R) if (r.h === h) { if (!m.has(r.ps)) m.set(r.ps, []); m.get(r.ps).push(r); }
  return [...m.entries()].sort((a, b) => agg(a[1]).mae - agg(b[1]).mae);
}

// §2b t1 şehir×kaynak
md.push('### 2a. t1 — şehir bazında hangi kaynak kazandı');
md.push('');
md.push('> Uyarı: derin kaynaklar (anonim NWP + AI, aylarca veri) ile canlı kaynaklar (~8 günlük tek pencere) ayrı değerlendirilir — örneklem farkı yüzünden MAE\'leri doğrudan kıyaslamak yanıltır. Tablolarda (n) her zaman görünür.');
md.push('');
md.push(...cityWinnerMd(R.filter((r) => r.h === 1), 'Uzun kapsam (derin geçmiş):', 'Canlı aileler (MAIN aynası — pencere biriktikçe uzar):'));
md.push('');

// §3 t2 tablosu + şehir
md.push('## 3. t2 (öbür gün) — kaynak bazında isabet');
md.push('');
md.push('| Kaynak | Aile | MAE | bias | hit≤1° | hit≤2° | n | gün |');
md.push('|---|---|---|---|---|---|---|---|');
for (const [ps, list] of groupByHorizon(2)) {
  const a = agg(list);
  md.push(`| ${ps}${BROKEN.has(ps) ? ' ⚠bozuk' : ''} | ${FAMILY[ps]} | **${fmt(a.mae)}** | ${fmt(a.bias)} | ${pct(a.hit1)}% | ${pct(a.hit2)}% | ${a.n} | ${a.days} |`);
}
md.push('');
md.push('### 3a. t2 — şehir bazında hangi kaynak kazandı');
md.push('');
md.push(...cityWinnerMd(R.filter((r) => r.h === 2), 'Uzun kapsam (derin geçmiş):', 'Canlı aileler (MAIN aynası — pencere biriktikçe uzar):'));
md.push('');

// §4 t0 tablosu (OM arşivi derin dahil)
md.push('## 4. t0 (aynı gün) — kaynak bazında isabet');
md.push('');
md.push('> Dikkat: t0 = aynı gün içinde toplanan tahmin. İki sorun: (1) gün içi geç toplanan tahmin gerçekleşene yaklaşır (VC 0.53 gibi aşırı iyi görünür); (2) canlı sağlayıcıların t0 değeri toplama saatine/ufuk etiketine duyarlı (NWS/OWM t0 burada t1\'den kötü çıkıyor — güvenilmez). **Karar/kıyas için t1/t2 esas alınır.** OM arşivi t0 (aynı-gün model analizi, 578 gün) uzun vadeli model kıyası için yukarıda ayrıdır.');
md.push('');
md.push('| Kaynak | Aile | MAE | bias | hit≤1° | hit≤2° | n | gün |');
md.push('|---|---|---|---|---|---|---|---|');
for (const [ps, list] of groupByHorizon(0)) {
  const a = agg(list);
  md.push(`| ${ps}${BROKEN.has(ps) ? ' ⚠bozuk' : ''} | ${FAMILY[ps]} | **${fmt(a.mae)}** | ${fmt(a.bias)} | ${pct(a.hit1)}% | ${pct(a.hit2)}% | ${a.n} | ${a.days} |`);
}
md.push('');
md.push('### 4a. t0 — OM arşivi 8 modelde şehir bazında en iyi model (uzun kapsam, en az 40 gün)');
md.push('');
{
  const byCity = new Map();
  for (const r of R) if (r.h === 0 && FAMILY[r.ps] === 'OM arşiv NWP (t0)') { if (!byCity.has(r.st)) byCity.set(r.st, []); byCity.get(r.st).push(r); }
  const rows = [];
  for (const [st, list] of byCity) {
    const per = new Map();
    for (const r of list) { if (!per.has(r.ps)) per.set(r.ps, []); per.get(r.ps).push(r); }
    const cands = [...per.entries()].map(([ps, l]) => { const a = agg(l); return { ps, a }; }).filter((c) => c.a.days >= 40).sort((a, b) => a.a.mae - b.a.mae);
    if (!cands.length) continue;
    const best = cands[0];
    rows.push({ st, best: best.ps, mae: best.a.mae, n: best.a.n, d: best.a.days,
      all: cands.slice(0, 3).map((c) => `${c.ps} ${fmt(c.a.mae)}(${c.a.days}g)`).join(' · ') });
  }
  rows.sort((a, b) => a.mae - b.mae);
  md.push('| Şehir | En iyi arşiv modeli | MAE | (n,gün) | Diğerleri |');
  md.push('|---|---|---|---|---|');
  for (const r of rows) md.push(`| ${stName[r.st] || r.st} | **${r.best}** | **${fmt(r.mae)}** | ${r.n},${r.d}g | ${r.all} |`);
}
md.push('');

// §5 aile/ufuk toplam özeti
md.push('## 5. Aile özeti (kaynak grupları birleşik, t1/t2)');
md.push('');
{
  const by = new Map();
  for (const r of R) if ((r.h === 1 || r.h === 2) && !BROKEN.has(r.ps)) {
    const k = `${r.fam}|${r.h}`; if (!by.has(k)) by.set(k, []); by.get(k).push(r);
  }
  md.push('| Aile | t | MAE | bias | n | gün |');
  md.push('|---|---|---|---|---|---|');
  for (const [k, list] of [...by.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
    const a = agg(list);
    md.push(`| ${k.split('|')[0]} | t${k.split('|')[1]} | **${fmt(a.mae)}** | ${fmt(a.bias)} | ${a.n} | ${a.days} |`);
  }
}
md.push('');
md.push('## 6. Sonuç — sistem şu an ne söylüyor');
md.push('');
{
  const clean = (h) => [...R].filter((r) => r.h === h && !BROKEN.has(r.ps));
  const bestOf = (list, minDays) => {
    const per = new Map(); for (const r of list) { if (!per.has(r.ps)) per.set(r.ps, []); per.get(r.ps).push(r); }
    const cands = [...per.entries()].map(([ps, l]) => ({ ps, a: agg(l) })).filter((c) => c.a.days >= minDays).sort((a, b) => a.a.mae - b.a.mae);
    return cands[0]; };
  const t1 = clean(1);
  const deepT1 = bestOf(t1.filter((r) => ['Open-Meteo önceki', 'Pangu-Weather', 'FourCastNet v2', 'GraphCast', 'NOAA GFS'].includes(r.ps)), 30);
  const aiT1 = bestOf(t1.filter((r) => ['Pangu-Weather', 'FourCastNet v2', 'GraphCast'].includes(r.ps)), 30);
  const liveT1 = bestOf(t1.filter((r) => ['Visual Crossing', 'WeatherAPI', 'OpenWeatherMap', 'NWS (canlı)', 'OM-GFS', 'OM-ICON'].includes(r.ps)), 3);
  const aiBias = ['Pangu-Weather', 'FourCastNet v2', 'GraphCast'].map((p) => agg(t1.filter((r) => r.ps === p)).bias);
  const cool = aiBias.filter(Number.isFinite).reduce((a, b) => a + b, 0) / aiBias.filter(Number.isFinite).length;
  const omPrev = agg(t1.filter((r) => r.ps === 'Open-Meteo önceki'));
  md.push(`1. **t1 (yarın) için en derin ve en iyi kaynak: "Open-Meteo önceki" (anonim geleneksel NWP)** — ${omPrev.n} şehir-gün / ${omPrev.days} gün üzerinden MAE ${fmt(omPrev.mae)}, bias ${fmt(omPrev.bias)}. "Şu an geçmişte hangi tek kaynak en çok tutturmuş" sorusunun istatistiksel yanıtı budur.`);
  md.push(`2. **AI modelleri (Pangu / FourCastNet / GraphCast) günlük derin** ama ham olarak geride: en iyisi ${aiT1 ? `${aiT1.ps} MAE ${fmt(aiT1.a.mae)}` : '-'}; üçünde de ortak **soğuk bias ≈ ${fmt(cool)}°C**. Ham sayı düşük görünse de bu sabit soğukluk bias-düzeltmesiyle kısmen geri kazanılır (bkz. kalibrasyon raporu).`);
  if (liveT1) md.push(`3. **Canlı API'lerin skoru yalnız ~8 günlük pencereye dayanıyor** (n küçük, tek hava olayına duyarlı): pencerede en iyi ${liveT1.ps} MAE ${fmt(liveT1.a.mae)} (${liveT1.a.n} şehir-gün). "Hangi canlı kaynak uzun vadede tutturuyor" için henüz istatistik yok — geçmiş biriktikçe bu tablo güçlenecek.`);
  md.push(`4. **t2 (öbür gün) aynı sırayı korur** (derin anonim ${fmt(agg(clean(2).filter((r) => r.ps === 'Open-Meteo önceki')).mae)} MAE); ufuk uzadıkça hata büyür, beklenen.`);
  md.push('5. Kapsam dürüstlüğü: AI/OM-derin skorlar 2025-01→2026-09 gerçek settlement\'lı; canlı blok 2026-08-27→09-04. OM canlı `ecmwf_ifs04` akışı bozuk (pred=0) olduğundan tüm özet/sıralamalardan hariç tutuldu.');
  md.push('6. Şehir bazında hangi kaynak önde olduğu yukarıdaki 2a/3a tablolarındadır; sıralamalar kaynak × şehir × ufuk MAE ile üretildi.');
}
md.push('');

fs.writeFileSync(path.resolve(OUT), md.join('\n'), 'utf8');
console.log(`Rapor yazıldı: ${OUT}${SINCE ? ` (pencere ${SINCE}..${TODAY})` : ''}`);
console.log(`Toplam karşılaştırılabilir şehir-gün: ${R.length}`);

// konsol özeti
console.log('\n--- t1 kaynak sıralaması (MAE) ---');
for (const [ps, list] of groupByHorizon(1)) { const a = agg(list); console.log(`${ps.padEnd(18)} ${fmt(a.mae)}  bias=${fmt(a.bias)}  hit1=${pct(a.hit1)}%  n=${a.n}  gün=${a.days}`); }
console.log('\n--- t2 kaynak sıralaması (MAE) ---');
for (const [ps, list] of groupByHorizon(2)) { const a = agg(list); console.log(`${ps.padEnd(18)} ${fmt(a.mae)}  bias=${fmt(a.bias)}  hit1=${pct(a.hit1)}%  n=${a.n}  gün=${a.days}`); }
