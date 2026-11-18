// AIWP ILERI TAHMIN (S3 arsivi, GPU/Colab'siz)
//  En guncel arsiv init'ini (HEAD ile, bugunden geriye 12Z>00Z) secip gelecek hedef gunler
//  icin per-sehir gunluk-max tahmini uretir ve MAIN DB forecast_snapshots'a yazar
//  (source='AIWP', target_date >= bugun; gecmis geri-doldurma satirlarina dokunmaz).
//
//  Boru: (1) plan: catalog/forward_runs.json yaz    --plan ile sadece bu
//        (2) eksik katalog build (python, ~7dk/dosya)
//        (3) extract & yaz (node _aiwp_extract.mjs, AIWP_RUNS_FILE ile)
//
//  env:
//    AIWP_FROM=YYYY-MM-DD   ilk hedef gun (default: yarin UTC)
//    AIWP_DAYS=N            kac gun (default 7, horizon 1..9 icinde)
//    AIWP_NEWEST_FROM=YYYY-MM-DD  en guncel initi bu tarihten geriye ara (default: bugun)
//    AIWP_WORKERS=N         katalog build paralel (default 3)
//  Kullanim: node --experimental-sqlite scripts/_aiwp_forward.mjs [--plan]
import { DatabaseSync } from 'node:sqlite';
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

const PLAN = process.argv.includes('--plan');
const DB = process.env.HEAT_MAIN_DB || 'C:/HEAT data/db/tempmarket-edge.sqlite';
const CFG = JSON.parse(fs.readFileSync(path.resolve('windows-runner/config.json'), 'utf8'));
const stations = CFG.stations;
const MODELS = [
  { code: 'FOUR_v200_GFS', provider: 'FourCastNet v2' },
  { code: 'PANG_v100_GFS', provider: 'Pangu-Weather' },
  { code: 'GRAP_v100_GFS', provider: 'GraphCast' },
];
const BASE = 'https://noaa-oar-mlwp-data.s3.amazonaws.com';
const CAT = process.env.HEAT_CATALOG_DIR || path.resolve('data/catalog');
const FWD_FILE = path.join(CAT, 'forward_runs.json');
fs.mkdirSync(CAT, { recursive: true });

function todayIso() { return new Date().toISOString().slice(0, 10); }
function addDays(iso, n) {
  const d = new Date(iso + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
function filePath(code, date, hh) {
  const y = date.slice(0, 4), mmdd = date.slice(5).replace('-', '');
  return `${code}/${y}/${mmdd}/${code}_${date.replace(/-/g, '')}${hh}_f000_f240_06.nc`;
}
const availCache = new Map();
async function exists(code, date, hh) {
  const key = `${code}|${date}|${hh}`;
  if (availCache.has(key)) return availCache.get(key);
  const r = await fetch(`${BASE}/${filePath(code, date, hh)}`, { method: 'HEAD', signal: AbortSignal.timeout(25000) });
  const ok = r.status === 200;
  availCache.set(key, ok);
  return ok;
}
// en guncel mevcut init: newestFrom tarihinden geriye (12Z tercih, sonra 00Z)
async function newestInit(code, newestFrom) {
  for (let b = 0; b < 7; b++) {
    const d = addDays(newestFrom, -b);
    for (const hh of ['12', '00']) {
      if (await exists(code, d, hh)) return { date: d, hh };
    }
  }
  return null;
}

const FROM = process.env.AIWP_FROM || addDays(todayIso(), 1);
const DAYS = Number(process.env.AIWP_DAYS || 7);
const NEWEST_FROM = process.env.AIWP_NEWEST_FROM || todayIso();

const runTargets = new Map(); // code|date|hh -> [{st,D,horizon}]
for (const m of MODELS) {
  const init = await newestInit(m.code, NEWEST_FROM);
  if (!init) { console.warn(`${m.code}: son 7 gunde init bulunamadi`); continue; }
  const sMs = Date.parse(init.date + 'T00:00:00Z');
  const key = `${m.code}|${init.date}|${init.hh}`;
  let added = 0;
  for (let i = 0; i < DAYS; i++) {
    const D = addDays(FROM, i);
    const horizon = Math.round((Date.parse(D + 'T00:00:00Z') - sMs) / 86400000);
    if (horizon < 1 || horizon > 9) continue; // f240 kapsamasi + tam-gun garantisi
    if (!runTargets.has(key)) runTargets.set(key, []);
    for (const st of stations) { runTargets.get(key).push({ st: st.id, D, horizon }); added++; }
  }
  console.log(`[plan] ${m.code} init=${init.date}${init.hh} hedef=${FROM}..${addDays(FROM, DAYS - 1)} (${DAYS}g) -> ${added} (istasyon,gun)`);
}
const runs = [];
for (const [key, targets] of runTargets) {
  const [code, date, hh] = key.split('|');
  runs.push({ code, date, hh, file: filePath(code, date, hh), targets });
}
runs.sort((a, b) => a.file.localeCompare(b.file));
fs.writeFileSync(FWD_FILE, JSON.stringify({ generatedAt: new Date().toISOString(), from: FROM, days: DAYS, runs }, null, 0));
const nT = runs.reduce((a, r) => a + r.targets.length, 0);
console.log(`[plan] forward_runs.json: ${runs.length} run, ${nT} (model,istasyon,gun) hedef`);
if (PLAN) { console.log('--plan: bitti (katalog/extract yok).'); process.exit(0); }

// --- (2) eksik katalog build ---
const missing = runs.filter((r) => !fs.existsSync(path.join(CAT, `cat_${r.code}_${r.date.replace(/-/g, '')}${r.hh}.json`)));
if (missing.length) {
  console.log(`[build] ${missing.length} yeni katalog islenecek (workers=${process.env.AIWP_WORKERS || 3})...`);
  const r = spawnSync('python', [path.resolve('scripts/_aiwp_catalog_build.py')], {
    env: { ...process.env, AIWP_RUNS_FILE: FWD_FILE, AIWP_WORKERS: process.env.AIWP_WORKERS || '3' },
    stdio: 'inherit',
  });
  if (r.status !== 0) { console.error('katalog build HATA', r.status); process.exit(1); }
} else {
  console.log('[build] tum run kataloglu (cache).');
}

// --- (3) extract & yaz ---
const x = spawnSync(process.execPath, ['--experimental-sqlite', path.resolve('scripts/_aiwp_extract.mjs')], {
  env: { ...process.env, AIWP_RUNS_FILE: FWD_FILE, AIWP_VIA: 'aiwp-forward' },
  stdio: 'inherit',
});
if (x.status !== 0) { console.error('extract HATA', x.status); process.exit(1); }

// --- ozet ---
const db = new DatabaseSync(DB, { readOnly: true });
const agg = db.prepare(
  `SELECT provider p, COUNT(*) n, MIN(horizon_days) hlo, MAX(horizon_days) hhi, MIN(target_date) dlo, MAX(target_date) dhi
   FROM forecast_snapshots WHERE source='AIWP' AND target_date >= ? GROUP BY provider ORDER BY p`
).all(FROM);
console.log('\n== ILERI AIWP ozet (hedef>=' + FROM + ') ==');
for (const r of agg) console.log(`${r.p.padEnd(16)} n=${r.n} horizon ${r.hlo}-${r.hhi} D ${r.dlo}..${r.dhi}`);
const sample = (process.env.AIWP_SAMPLE || 'katl').toLowerCase();
console.log(`\n== ornek: ${sample} per model (hedef|model: tahminC) ==`);
const srows = db.prepare(
  `SELECT provider p, target_date D, predicted_high_c v FROM forecast_snapshots
   WHERE source='AIWP' AND station_id=? AND target_date >= ? ORDER BY target_date, provider`
).all(sample, FROM);
const byD = {};
for (const r of srows) { (byD[r.D] ||= {})[r.p] = r.v; }
for (const [D, m] of Object.entries(byD)) {
  console.log(D, Object.entries(m).map(([p, v]) => `${p.split(' ')[0]}:${v.toFixed(1)}`).join('  '));
}
db.close();
console.log('\nBitti. (Kayit: forecast_snapshots, source=AIWP, via=aiwp-forward, hedef gelecek.)');
