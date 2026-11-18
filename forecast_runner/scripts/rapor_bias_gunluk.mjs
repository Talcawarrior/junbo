#!/usr/bin/env node
// Ne işe yarar: HER GÜN koşan, insan-okur bias raporu (rapor_bias_gunluk.md): "bugün hangi kaynak hangi
//   şehri tutturuyor ve bias düzeltmesi ne kadar düzeltiyor". Runner'ın 08:00 UTC bloğunda settlement
//   köprüsünden SONRA koşar; çıktıyı data/rapor_arsivi/ altına gün adıyla arşivler.
//
// NEDEN AYRI: `rapor_kaynak_sehir_bias.mjs` bir BACKTEST'tir (penceresi elle sabit, seyrek koşulur).
//   Bu rapor aynı ölçümü KAYAN pencereyle günlük yapar ki bias'ın bugünkü hâli her sabah tek sayfada görünsün.
//
// KURALLAR (kanonik skorlayıcıyla BİREBİR aynı sorgu — `rapor_iskabet_t02.mjs` satır 72-88 ile aynı):
//   - Gerçek = settlements.high_c (en son recorded_at_utc); tahmin = her (kaynak,şehir,gün,ufuk) için SON koşu.
//   - OM-ECMWF04 (bozuk akış, pred=0) her yerde HARİÇ. t1 = yarın (karar ufku).
//
// SIZINTISIZLIK (bu raporun tek kritik kuralı):
//   Test penceresi = en son çözülen N gün. Öğrenme penceresi = ondan KESİN ÖNCEKİ M gün. Kesişmezler.
//   Ham skor hiçbir şey öğrenmez. Düzeltme yalnız öğrenme penceresinden kurulur, test penceresinde skorlanır.
//   Mevcut `bias_params` KULLANILMAZ (o aynı pencereyi de görür → in-sample olurdu).
//
// Kullanım: node scripts/rapor_bias_gunluk.mjs
//   BIAS_SON_GUN   — son gün (varsayılan: settlement'ta çözülmüş en son gün)
//   BIAS_TEST_GUN  (varsayılan 7)  — test penceresi kaç gün
//   BIAS_OGREN_GUN (varsayılan 28) — öğrenme penceresi kaç gün (testten hemen önce)
//   BIAS_OUT, HEAT_MAIN_DB, BIAS_ARSIV (varsayılan 1 = arşivle)
// SALT-OKUNUR: DB {readOnly:true} açılır, hiçbir şey yazılmaz (yalnız .md dosyaları yazılır).

import path from 'node:path';
import fs from 'node:fs';
import { DatabaseSync } from 'node:sqlite';

const MAIN_DB = process.env.HEAT_MAIN_DB || 'C:/HEAT data/db/tempmarket-edge.sqlite';
const OUT = process.env.BIAS_OUT || 'rapor_bias_gunluk.md';
const ARSIV = process.env.BIAS_ARSIV !== '0';
const TEST_GUN = Number(process.env.BIAS_TEST_GUN || 7);
const OGREN_GUN = Number(process.env.BIAS_OGREN_GUN || 28);
const K = 12;        // shrinkage sabiti (üretimdeki train_bias_online.mjs ile aynı)
const OLS_MIN = 15;  // OLS için en az çift (üretimle aynı)
const MIN_HUCRE = 3; // bir hücrenin gösterilmesi/yarışması için en az şehir-gün

// --- kaynak tanımları: `rapor_iskabet_t02.mjs` satır 33-47 ile birebir aynı olmalı ---
const ARCHIVE = ['ecmwf_ifs025', 'gem_global', 'gfs_seamless', 'icon_seamless', 'jma_seamless', 'meteofrance_seamless', 'ukmo_seamless', 'cma_grapes_global'];
const SRC = {
  'Open-Meteo önceki': { provs: ['Open-Meteo previous'], short: 'OM-önceki' },
  'Pangu-Weather': { provs: ['Pangu-Weather'], short: 'Pangu' },
  'FourCastNet v2': { provs: ['FourCastNet v2'], short: 'FCN' },
  'GraphCast': { provs: ['GraphCast'], short: 'GC' },
  ...Object.fromEntries(ARCHIVE.map((m) => [m, { provs: [m], short: 'arc-' + m.replace('_seamless', '').replace('_global', '').replace('ecmwf_ifs025', 'ECMWF').replace('meteofrance', 'MF').toUpperCase() }])),
  'NOAA GFS': { provs: ['NOAA GFS daily'], short: 'GFS-daily' },
  'Visual Crossing': { provs: ['Visual Crossing daily'], short: 'VC' },
  'WeatherAPI': { provs: ['WeatherAPI daily'], short: 'WA' },
  'OpenWeatherMap': { provs: ['OpenWeatherMap 3-hour'], short: 'OWM' },
  'NWS (canlı)': { provs: ['NWS forecastHourly'], short: 'NWS' },
  'OM-ECMWF04': { provs: ['Open-Meteo ecmwf_ifs04'], short: 'OM-ECMWF04' }, // tarihsel (ad ölü, pred=0)
  'OM-IFS025': { provs: ['Open-Meteo ecmwf_ifs025'], short: 'OM-IFS025' }, // 2026-09-15'ten beri canlı akış
  'OM-GFS': { provs: ['Open-Meteo gfs_seamless'], short: 'OM-GFS' },
  'OM-ICON': { provs: ['Open-Meteo icon_seamless'], short: 'OM-ICON' },
};
const BROKEN = new Set(['OM-ECMWF04']);
const DEEP_SRC = new Set(['Open-Meteo önceki', 'Pangu-Weather', 'FourCastNet v2', 'GraphCast', 'NOAA GFS', ...ARCHIVE]);
const LIVE_SRC = new Set(['Visual Crossing', 'WeatherAPI', 'OpenWeatherMap', 'NWS (canlı)', 'OM-GFS', 'OM-ICON']);
const famOf = (ps) => (DEEP_SRC.has(ps) ? 'derin' : 'canlı');
const CONFIG = JSON.parse(fs.readFileSync(path.resolve('windows-runner/config.json'), 'utf8'));
const stName = Object.fromEntries(CONFIG.stations.map((s) => [s.id, `${s.icao} ${s.city}`]));

const db = new DatabaseSync(MAIN_DB, { readOnly: true });

// ---- 0) Pencereler: test = en son çözülmüş N gün, öğrenme = ondan önceki M gün (kesişmez) ----
const sonEnv = process.env.BIAS_SON_GUN;
const sonGun = sonEnv || db.prepare('SELECT MAX(target_date) d FROM settlements WHERE high_c IS NOT NULL').get()?.d;
if (!sonGun) { console.error('settlement boş — rapor üretilemez.'); process.exit(1); }
const gunler = db.prepare('SELECT DISTINCT target_date d FROM settlements WHERE high_c IS NOT NULL AND target_date <= ? ORDER BY d').all(sonGun).map((r) => r.d);
const testDays = gunler.slice(-TEST_GUN);
const testFrom = testDays[0], testTo = testDays[testDays.length - 1];
const onceki = gunler.filter((d) => d < testFrom);
const learnEnd = onceki[onceki.length - 1];
const learnStart = learnEnd ? onceki.slice(-OGREN_GUN)[0] : null;
const LOAD_FROM = learnStart || testFrom;

// ---- 1) Satırları yükle (kanonik sorgu; öğrenme + test birlikte) ----
const rows = [];
for (const [ps, cfg] of Object.entries(SRC)) {
  if (BROKEN.has(ps)) continue;
  const ph = cfg.provs.map(() => '?').join(',');
  const found = db.prepare(`
    WITH latest AS (
      SELECT fs.station_id st, fs.target_date td, fs.horizon_days h, fs.predicted_high_c v,
             ROW_NUMBER() OVER (PARTITION BY fs.provider, fs.station_id, fs.target_date, fs.horizon_days
                                ORDER BY fs.run_at_utc DESC) rn
      FROM forecast_snapshots fs
      WHERE fs.target_date <= ? AND fs.target_date >= ?
        AND fs.horizon_days IN (0,1,2) AND fs.provider IN (${ph})
        AND EXISTS (SELECT 1 FROM settlements s2
                    WHERE s2.station_id = fs.station_id AND s2.target_date = fs.target_date)
    )
    SELECT l.st, l.td, l.h, l.v,
           (SELECT s.high_c FROM settlements s
            WHERE s.station_id=l.st AND s.target_date=l.td
            ORDER BY s.recorded_at_utc DESC LIMIT 1) actual
    FROM latest l WHERE l.rn=1
  `).all(testTo, LOAD_FROM, ...cfg.provs);
  for (const r of found) {
    if (r.actual == null || r.v == null || !Number.isFinite(r.v)) continue;
    rows.push({ ps, short: cfg.short, fam: famOf(ps), st: r.st, td: r.td, h: r.h, v: r.v, actual: r.actual });
  }
}
db.close();

const inWin = (r, a, b) => r.td >= a && r.td <= b;
const testRows = rows.filter((r) => inWin(r, testFrom, testTo));
const learnRows = learnStart ? rows.filter((r) => inWin(r, learnStart, learnEnd)) : [];

function stats(list, getv) {
  const s = { n: list.length, mae: NaN, rmse: NaN, bias: NaN, hit1: NaN, hit2: NaN };
  if (!s.n) return s;
  let e = 0, e2 = 0, b = 0, h1 = 0, h2 = 0;
  for (const r of list) {
    const d = (getv ? getv(r) : r.v) - r.actual, a = Math.abs(d);
    e += a; e2 += d * d; b += d;
    if (a <= 1) h1++; if (a <= 2) h2++;
  }
  s.mae = e / s.n; s.rmse = Math.sqrt(e2 / s.n); s.bias = b / s.n; s.hit1 = h1 / s.n; s.hit2 = h2 / s.n;
  return s;
}
const fmt = (x) => (Number.isFinite(x) ? x.toFixed(2) : '-');
const sgn = (x) => (Number.isFinite(x) ? (x >= 0 ? '+' : '') + x.toFixed(2) : '-');
const pct = (x) => (Number.isFinite(x) ? (100 * x).toFixed(0) : '-');
const days = (l) => new Set(l.map((r) => r.td)).size;
const cities = (l) => new Set(l.map((r) => r.st)).size;

function ols(list) {
  const n = list.length;
  if (n < OLS_MIN) return null;
  let sx = 0, sy = 0, sxx = 0, sxy = 0;
  for (const r of list) { sx += r.v; sy += r.actual; sxx += r.v * r.v; sxy += r.v * r.actual; }
  const mx = sx / n, my = sy / n, vx = sxx / n - mx * mx;
  if (!(vx > 1e-9)) return null;
  const a = Math.min(1.5, Math.max(0.5, (sxy / n - mx * my) / vx));
  return { a, b: my - a * mx };
}
// Öğrenme penceresinden düzeltici kur (üretimdeki train_bias_online ile aynı formül).
function makeCorrector(learn, scope) {
  const global = new Map();
  for (const r of learn) { const k = `${r.ps}|${r.h}`; (global.get(k) || global.set(k, []).get(k)).push(r); }
  for (const [k, l] of global) global.set(k, { ...stats(l), ols: ols(l) });
  const city = new Map();
  if (scope === 'sehir') {
    for (const r of learn) { const k = `${r.ps}|${r.h}|${r.st}`; (city.get(k) || city.set(k, []).get(k)).push(r); }
    for (const [k, l] of city) {
      const g = global.get(`${l[0].ps}|${l[0].h}`), s = stats(l);
      city.set(k, { n: s.n, shrunk: g ? (s.n * s.bias + K * g.bias) / (s.n + K) : s.bias, ols: ols(l) });
    }
  }
  return (r) => {
    if (scope === 'global') { const g = global.get(`${r.ps}|${r.h}`); return g && Number.isFinite(g.bias) ? r.v - g.bias : r.v; }
    const c = city.get(`${r.ps}|${r.h}|${r.st}`);
    if (!c) return r.v;
    return c.ols ? c.ols.a * r.v + c.ols.b : r.v - c.shrunk;
  };
}

const md = [];
const P = (s = '') => md.push(s);
const srcOrder = Object.keys(SRC).filter((p) => !BROKEN.has(p));
const t1 = testRows.filter((r) => r.h === 1);

P('# Günlük Bias Raporu — bugün hangi kaynak hangi şehri tutturuyor?');
P('');
P('> Ölçümdür; Heat trade yapmaz. Gerçek = Polymarket settlement (o günün gerçekleşen en yüksek sıcaklığı).');
P(`> Bu rapor her gün otomatik koşar; aşağıdaki sayı **${testFrom} → ${testTo}** penceresine aittir.`);
P('');
P('## 0. Künye');
P('');
P(`- **Test penceresi (ölçülen):** ${testFrom} → ${testTo} = **${testDays.length} gün**.`);
P(`- **Öğrenme penceresi (düzeltmenin öğrendiği):** ${learnStart ? `${learnStart} → ${learnEnd} = ${onceki.slice(-OGREN_GUN).length} gün` : '**yok** (test penceresinden önce çözülmüş gün yok → düzeltme ölçülemez)'}.`);
P(`- **Kesişme:** ${learnEnd && learnEnd < testFrom ? `yok ✔ (öğrenme son günü ${learnEnd} < test başlangıcı ${testFrom})` : '—'} — düzeltme sızıntısızdır.`);
P(`- **Ölçülen:** ${srcOrder.length} kaynaktan verisi olan **${new Set(testRows.filter((r) => r.h === 1).map((r) => r.ps)).size}**'i · ${cities(t1)} şehir · t1 ${t1.length} satır.`);
P('- **Tahmin seçimi:** her (kaynak, şehir, gün, ufuk) için **en son** koşu. **Hariç:** OM-ECMWF04 (bozuk akış).');
P('- **Ufuk:** **t1 = yarın** — karar ufku budur. t0 (aynı gün) karar için geçtir, t2 daha zordur.');
P('');

// ---- 1) Kapsam ----
P('## 1. Kapsam — hangi kaynaktan kaç gün / kaç şehir (test penceresi)');
P('');
P('`*` = yapısal dar kaynak (ör. NWS yalnız ABD). "Kaynak başına N gün" ≠ "hepsi birlikte N gün" (Kural 12).');
P('');
P('| Kaynak | Aile | t0 gün/şehir | t1 gün/şehir | t2 gün/şehir |');
P('|---|---|---|---|---|');
for (const ps of srcOrder) {
  const l = testRows.filter((r) => r.ps === ps);
  if (!l.length) { P(`| ${ps} | ${famOf(ps)} | - | - | - |`); continue; }
  const cell = (h) => { const x = l.filter((r) => r.h === h); return x.length ? `${days(x)}/${cities(x)}` : '-'; };
  const narrow = cities(l.filter((r) => r.h === 1)).valueOf() > 0 && cities(l.filter((r) => r.h === 1)) < 45 ? ' *' : '';
  P(`| ${ps}${narrow} | ${famOf(ps)} | ${cell(0)} | ${cell(1)} | ${cell(2)} |`);
}
P('');

// ---- 2) t1 kaynak geneli ----
P('## 2. t1 (yarın) — kaynak geneli, HAM');
P('');
P('MAE = ortalama kaç derece yanıldı (küçük iyi) · bias = hep sıcak mı (+) soğuk mu (−) söylüyor · hit = yüzde kaç kez tutturdu.');
P('');
P('| # | Kaynak | Aile | n | MAE | RMSE | bias | hit≤1° | hit≤2° | Şehir | Gün |');
P('|---|---|---|---|---|---|---|---|---|---|---|');
const per = srcOrder.map((ps) => ({ ps, x: t1.filter((r) => r.ps === ps) }))
  .map((o) => ({ ...o, s: stats(o.x) })).filter((o) => o.s.n).sort((a, b) => a.s.mae - b.s.mae);
per.forEach((o, i) => {
  const b = i === 0;
  P(`| ${i + 1} | ${b ? '**' : ''}${o.ps}${b ? '**' : ''} | ${famOf(o.ps)} | ${o.s.n} | ${b ? '**' : ''}${fmt(o.s.mae)}${b ? '**' : ''} | ${fmt(o.s.rmse)} | ${sgn(o.s.bias)} | ${pct(o.s.hit1)}% | ${pct(o.s.hit2)}% | ${cities(o.x)} | ${days(o.x)} |`);
});
P('');

// ---- 3) Şehir × kaynak: MAE matrisi + kazanan ----
const cols = per.filter((o) => days(o.x) >= MIN_HUCRE).map((o) => o.ps);
const stOrder = [...new Set(t1.map((r) => r.st))].sort((a, b) => (stName[a] || a).localeCompare(stName[b] || b, 'tr'));
const cellOf = (st, ps) => { const x = t1.filter((r) => r.st === st && r.ps === ps); return x.length >= MIN_HUCRE ? stats(x) : null; };

P(`## 3. Bugün hangi kaynak hangi şehri tutturuyor (${cities(t1)} şehir)`);
P('');
P(`Hücre **${MIN_HUCRE}'ten az** veriyle ölçüldüyse "-" yazar; "-" = "bilinmiyor", "kötü" değil.`);
P('');
P('### 3a. MAE matrisi (küçük iyi) — kalın = o şehirde en iyi');
P('');
P(`| Şehir | ${cols.map((p) => SRC[p].short).join(' | ')} |`);
P(`|---|${cols.map(() => '---').join('|')}|`);
for (const st of stOrder) {
  const cs = cols.map((p) => cellOf(st, p));
  const best = cs.reduce((bi, c, i) => (c && (bi < 0 || c.mae < cs[bi].mae) ? i : bi), -1);
  P(`| ${stName[st] || st} | ${cs.map((c, i) => (c ? (i === best ? '**' : '') + fmt(c.mae) + (i === best ? '**' : '') : '-')).join(' | ')} |`);
}
P('');
P('### 3b. bias matrisi (artı = sıcak söylüyor) — kalın = 3a\'daki kazanan');
P('');
P(`| Şehir | ${cols.map((p) => SRC[p].short).join(' | ')} |`);
P(`|---|${cols.map(() => '---').join('|')}|`);
for (const st of stOrder) {
  const cs = cols.map((p) => cellOf(st, p));
  const best = cs.reduce((bi, c, i) => (c && (bi < 0 || c.mae < cs[bi].mae) ? i : bi), -1);
  P(`| ${stName[st] || st} | ${cs.map((c, i) => (c ? (i === best ? '**' : '') + sgn(c.bias) + (i === best ? '**' : '') : '-')).join(' | ')} |`);
}
P('');

// kazanan tablosu + marj
const winCount = new Map(); const wRows = [];
for (const st of stOrder) {
  const cands = cols.map((p) => ({ p, s: cellOf(st, p) })).filter((o) => o.s).sort((a, b) => a.s.mae - b.s.mae);
  if (!cands.length) continue;
  const marj = cands[1] ? cands[1].s.mae - cands[0].s.mae : NaN;
  winCount.set(cands[0].p, (winCount.get(cands[0].p) || 0) + 1);
  wRows.push({ st, w: cands[0], marj, second: cands[1] });
}
P('### 3c. Şehir başına kazanan ve marj');
P('');
P('Marj = ikincinin MAE\'si eksi kazananınki. **Marj < 0.15° ise pratikte berabere** — "bu şehirde X kesin" denmez.');
P('');
P('| Şehir | Kazanan | MAE | Marj | 2. |');
P('|---|---|---|---|---|');
for (const r of wRows.sort((a, b) => a.w.s.mae - b.w.s.mae))
  P(`| ${stName[r.st] || r.st} | **${r.w.p}** | **${fmt(r.w.s.mae)}** | ${Number.isFinite(r.marj) ? fmt(r.marj) : '-'} | ${r.second ? `${r.second.p} ${fmt(r.second.s.mae)}` : '-'} |`);
P('');
P('| Kaynak | Kazandığı şehir | Payı |');
P('|---|---|---|');
[...winCount.entries()].sort((a, b) => b[1] - a[1]).forEach(([p, c]) => P(`| ${p} | **${c}** | ${((100 * c) / wRows.length).toFixed(0)}% |`));
P('');
const tie = wRows.filter((r) => Number.isFinite(r.marj) && r.marj < 0.15).length;
P(`**Uyarı:** ${wRows.length} şehrin **${tie}**'inde marj 0.15°'nin altında → o şehirlerde kazanan berabere sayılmalı.`);
P('');

// ---- 4) Düzeltme ne kadar düzeltiyor (sızıntısız) ----
P('## 4. Bias düzeltmesi ne kadar düzeltiyor? — SIZINTISIZ');
P('');
if (!learnStart) {
  P('Öğrenme penceresi yok (test penceresinden önce çözülmüş gün yok) — düzeltme ölçülemedi.');
} else {
  P(`Düzeltme **${learnStart} → ${learnEnd}** günlerinden öğrenildi, **${testFrom} → ${testTo}** günlerinde uygulandı.`);
  P('İki pencere kesişmiyor. Düzeltilmiş skor, ham skorla **aynı şehir-günler** üzerinden kıyaslanır.');
  P('');
  const cG = makeCorrector(learnRows, 'global');
  const cS = makeCorrector(learnRows, 'sehir');
  P('| Kaynak | n (test) | Ham MAE | Ham bias | Kaynak-geneli düz. MAE | Şehir-bazlı düz. MAE | Kazanç | İyileşen/Bozulan şehir |');
  P('|---|---|---|---|---|---|---|---|');
  let better = 0, total = 0;
  for (const o of per) {
    const tr = o.x; if (!tr.length) continue;
    const sRaw = stats(tr), sG = stats(tr, cG), sC = stats(tr, cS);
    total++; if (sRaw.mae - sC.mae > 0) better++;
    let up = 0, down = 0;
    for (const st of new Set(tr.map((r) => r.st))) {
      const l = tr.filter((r) => r.st === st); if (l.length < MIN_HUCRE) continue;
      const d = stats(l).mae - stats(l, cS).mae;
      if (d > 0.01) up++; else if (d < -0.01) down++;
    }
    P(`| ${o.ps} | ${sRaw.n} | ${fmt(sRaw.mae)} | ${sgn(sRaw.bias)} | ${fmt(sG.mae)} | **${fmt(sC.mae)}** | ${sRaw.mae - sC.mae >= 0 ? '+' : ''}${fmt(sRaw.mae - sC.mae)} | ${up}/${down} |`);
  }
  P('');
  P(`**Özet:** ${total} kaynağın **${better}**'inde düzeltme ham skoru iyileştirdi.`);
  P('');
  P('> **Nasıl okunur:** "Kazanç" pozitifse o kaynağı olduğu gibi değil, bilinen sapmasını çıkararak kullanmak');
  P('> daha isabetli. Kaynak-geneli sütun yalnız ufka bakar; şehir-bazlı sütun şehrin kendi sapmasını kullanır');
  P(`> (şehirde az veri varsa kaynak geneline çekilir, shrinkage K=${K}). İkisi arasındaki fark, o kaynağın`);
  P('> sapmasının **şehirden şehire değiştiğini** gösterir.');
  // en çok kazandıran / kaybettiren
  if (t1.length) {
    const gains = per.map((o) => ({ p: o.ps, g: stats(o.x).mae - stats(o.x, cS).mae, raw: stats(o.x).mae, cor: stats(o.x, cS).mae })).filter((o) => Number.isFinite(o.g));
    const worst = [...gains].sort((a, b) => a.g - b.g)[0];
    const best = [...gains].sort((a, b) => b.g - a.g)[0];
    P('');
    if (best) P(`- **En çok kazandıran:** ${best.p} — ham ${fmt(best.raw)} → düzeltilmiş **${fmt(best.cor)}** (${fmt(best.g)}° kazanç).`);
    if (worst && worst.g < 0) P(`- **Dikkat, bozan:** ${worst.p} — ham ${fmt(worst.raw)} → düzeltilmiş ${fmt(worst.cor)} (${fmt(worst.g)}°). Bu kaynakta düzeltme şimdilik zarar veriyor.`);
  }
}
P('');

// ---- 5) t0 / t2 özet ----
P('## 5. Ek — t0 (aynı gün) ve t2 (öbür gün) özeti');
P('');
P('| Kaynak | t0 n / MAE | t1 n / MAE | t2 n / MAE |');
P('|---|---|---|---|');
for (const ps of srcOrder) {
  const cell = (h) => { const x = testRows.filter((r) => r.ps === ps && r.h === h); return x.length ? `${x.length} / ${fmt(stats(x).mae)}` : '-'; };
  P(`| ${ps} | ${cell(0)} | ${cell(1)} | ${cell(2)} |`);
}
P('');
P('> **t0 uyarısı:** OpenWeatherMap ve NWS\'in t0 değeri günün maksimumunu değil **o anki/canlı sıcaklığı** izler');
P('> (günün maksimumu elbette daha yüksek → sahte büyük soğuk sapma). t0 satırı bu iki kaynak için "kötü kaynak" diye okunmaz.');
P('');

// ---- 6) dürüstlük ----
P('## 6. Dürüstlük notları');
P('');
P(`1. **Pencere ${testDays.length} gün** — sıralama değil **yön** okunur. Özellikle canlı ailede hücre başına çift sayısı`);
P('   azdır; tek bir sıcak/soğuk dalga sıralamayı değiştirebilir (§3c marj sütunu tam bunun için var).');
P('2. **Ham skor temiz, düzeltme sızıntısız:** §2/§3 tamamen hamdır, hiçbir parametre öğrenmez. §4 düzeltmeyi');
P('   ayrı bir pencerede öğrenip burada uygular.');
P('3. **Mevcut `bias_params` tablosu bu raporda kullanılmaz** — o canlı hücreler için aynı pencereyi de görür');
P('   (in-sample). Rapor kendi öğrenme penceresinden sıfırdan hesaplar.');
P('4. **Yapısal dar kaynak:** NWS yalnız ABD (~11-12 şehir) — 45 şehir eşiğiyle ölçülüp "veri yok" denmez (`*`).');
P('5. **Kardeş verisi:** OM-GFS hücrelerinde 2026-08-02→08-25 günleri kardeş bot JUNBO arşivinden tamamlandı');
P('   (eşdeğerliği ölçüldü: örtüşmede medyan fark 0.00 °C) — `scripts/_kardes_om_import.mjs`.');
P('');
P(`*Üretim: ${new Date().toISOString()} · kaynak ${path.basename(MAIN_DB)} (salt-okunur) · \`node scripts/rapor_bias_gunluk.mjs\` · test ${testFrom}..${testTo} · öğrenme ${learnStart || '-'}..${learnEnd || '-'}*`);

fs.writeFileSync(OUT, md.join('\n') + '\n', 'utf8');
let arsivYolu = null;
if (ARSIV) {
  const dir = process.env.BIAS_ARSIV_DIR || path.join('data', 'rapor_arsivi');
  fs.mkdirSync(dir, { recursive: true });
  // Arşiv adı ÜRETİM günü (kanonik `rapor_iskabet_<bugün>.md` deseniyle aynı); pencerenin bittiği gün
  // (testTo) dosyanın içinde §0'da yazılı — ikisi karıştırılmasın diye adı bilerek bugün.
  arsivYolu = path.join(dir, `rapor_bias_gunluk_${new Date().toISOString().slice(0, 10)}.md`);
  fs.writeFileSync(arsivYolu, md.join('\n') + '\n', 'utf8');
}
console.log(`yazıldı: ${OUT}${arsivYolu ? ` + arşiv ${arsivYolu}` : ''}`);
console.log(`  test ${testFrom}..${testTo} (${testDays.length} gün) · öğrenme ${learnStart || '-'}..${learnEnd || '-'} · t1 ${t1.length} satır · ${cities(t1)} şehir`);
if (per.length) console.log(`  t1 en iyi: ${per[0].ps} (MAE ${fmt(per[0].s.mae)}, n=${per[0].s.n})`);
