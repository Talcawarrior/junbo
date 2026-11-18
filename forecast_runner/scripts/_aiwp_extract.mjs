// Adim 3: katalogdaki t2 düzlemlerinden AI model gunluk-max tahminlerini hesapla.
//  --dry: DB'ye yazmaz, sadece gercek settlement ile karsilastirip ozet basar (dogrulama).
//  Tam:  D: DB forecast_snapshots'a upsert eder.
//  Kullanim: node --experimental-sqlite scripts/_aiwp_extract.mjs [--dry]
import { DatabaseSync } from 'node:sqlite';
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';

const CAT = process.env.HEAT_CATALOG_DIR || path.resolve('data/catalog');
const RUNS_PATH = process.env.AIWP_RUNS_FILE ? path.resolve(process.env.AIWP_RUNS_FILE) : path.join(CAT, 'runs.json');
const DRY = process.argv.includes('--dry');
const DB = process.env.HEAT_MAIN_DB || 'C:/HEAT data/db/tempmarket-edge.sqlite';
const CFG = JSON.parse(fs.readFileSync(path.resolve('windows-runner/config.json'), 'utf8'));
const stations = Object.fromEntries(CFG.stations.map((s) => [s.id, s]));
const BUCKET = 'https://noaa-oar-mlwp-data.s3.amazonaws.com';

const db = new DatabaseSync(DB, { readOnly: DRY });
const MODEL_PROVIDER = { 'FOUR_v200_GFS': 'FourCastNet v2', 'PANG_v100_GFS': 'Pangu-Weather', 'GRAP_v100_GFS': 'GraphCast' };
// settlement actual map (hedef <= bugun) — station|D -> high
const acts = {};
if (DRY) {
  const rows = db.prepare(`SELECT station_id st, target_date D, high_c FROM settlements WHERE high_c IS NOT NULL AND target_date <= '2026-09-03'`).all();
  for (const r of rows) acts[`${r.st}|${r.D}`] = r.high_c;
}
const delStmt = db.prepare(`DELETE FROM forecast_snapshots WHERE source='AIWP' AND provider=? AND station_id=? AND target_date=? AND horizon_days=? AND run_at_utc=?`);
const insStmt = db.prepare(`INSERT INTO forecast_snapshots (station_id, source, provider, run_at_utc, target_date, horizon_days, predicted_high_c, predicted_low_c, raw_json) VALUES (?, 'AIWP', ?, ?, ?, ?, ?, NULL, ?)`);

function parts(t, tz) {
  const f = new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  const o = {};
  for (const x of f.formatToParts(t)) if (x.type !== 'literal') o[x.type] = x.value;
  return { y: +o.year, mo: +o.month, d: +o.day, H: +o.hour, M: +o.minute };
}
// yerel (y,mo,d,H:M) -> UTC ms
function localToUtc(tz, y, mo, d, H, M = 0) {
  const target = Date.UTC(y, mo - 1, d, H, M);
  let t = target;
  for (let it = 0; it < 3; it++) {
    const p = parts(t, tz);
    const guess = Date.UTC(p.y, p.mo - 1, p.d, p.H, p.M);
    t += (target - guess);
  }
  return t;
}
const MS6H = 6 * 3600 * 1000;
const HR9 = 9 * 3600 * 1000, HR22 = 22 * 3600 * 1000;

async function fetchPlane(file, off, size) {
  let lastErr;
  for (let attempt = 0; attempt < 4; attempt++) {
    try {
      const r = await fetch(`${BUCKET}/${file}`, { headers: { Range: `bytes=${off}-${off + size - 1}` }, signal: AbortSignal.timeout(120000) });
      if (!r.ok) throw new Error(`range HTTP ${r.status} ${file} ${off}`);
      const buf = Buffer.from(await r.arrayBuffer());
      let out = zlib.inflateSync(buf);
      // HDF5 shuffle inverse
      if (out.length % 4 === 0) {
        const E = 4, n = out.length;
        const b = Buffer.alloc(n);
        for (let e = 0; e < E; e++) {
          for (let i = 0; i < n / E; i++) {
            b[i * E + e] = out[e * (n / E) + i];
          }
        }
        out = b;
      }
      const nlat = 721, nlon = 1440;
      const f32 = new Float32Array(out.buffer, out.byteOffset, nlat * nlon);
      return { f32, nlat, nlon };
    } catch (e) {
      lastErr = e;
      const wait = 400 * Math.pow(2, attempt) + Math.round(Math.random() * 300);
      await new Promise((res) => setTimeout(res, wait));
    }
  }
  throw lastErr;
}
// sinirli es-zamanli map (socket yorgunlugunu onler)
async function mapLimit(items, limit, fn) {
  const out = new Array(items.length);
  let i = 0;
  const nw = Math.min(limit, items.length);
  await Promise.all(Array.from({ length: nw }, async () => {
    while (true) {
      const idx = i++;
      if (idx >= items.length) return;
      out[idx] = await fn(items[idx], idx);
    }
  }));
  return out;
}
// bilinear, eksenler: latAzal (90->-90, adim negatif), lonArtis (0->360)
function bilin(f32, nlat, nlon, latArrStep, lonArrStep, lat, lon) {
  const li = (90 - lat) / latArrStep;      // latArrStep = 0.25
  let lj = ((lon % 360) + 360) % 360 / lonArrStep;
  let i0 = Math.floor(li), j0 = Math.floor(lj);
  const fi = li - i0, fj = lj - j0;
  i0 = Math.max(0, Math.min(nlat - 1, i0)); j0 = Math.max(0, Math.min(nlon - 1, j0));
  const i1 = Math.min(nlat - 1, i0 + 1);
  const j1 = j0 === nlon - 1 ? j0 : j0 + 1; // kenarda clamp (lon 360 degil ama kucuk etki)
  const idx = (i, j) => i * nlon + j;
  return (f32[idx(i0, j0)] * (1 - fi) * (1 - fj) + f32[idx(i0, j1)] * (1 - fi) * fj
    + f32[idx(i1, j0)] * fi * (1 - fj) + f32[idx(i1, j1)] * fi * fj) - 273.15;
}

async function runCatalog(catFile) {
  const cat = JSON.parse(fs.readFileSync(catFile, 'utf8'));
  const run = { code: cat.code, date: cat.date, hh: cat.hh, file: cat.file };
  const stepList = cat.steps; // [k, off, size]
  const nSteps = cat.n;
  const latStep = 0.25, lonStep = 0.25;
  // targets from runs dosyasindan (env AIWP_RUNS_FILE veya catalog/runs.json)
  const runsJson = JSON.parse(fs.readFileSync(RUNS_PATH, 'utf8'));
  const runMeta = runsJson.runs.find((r) => `${r.code}|${r.date}|${r.hh}` === `${run.code}|${run.date}|${run.hh}`);
  if (!runMeta) return { code: run.code, date: run.date, results: [] };
  const initMs = Date.UTC(+run.date.slice(0, 4), +run.date.slice(5, 7) - 1, +run.date.slice(8, 10), +run.hh);
  // her (target station) icin gerekli adimlar: [start..end] arasi k
  const out = [];
  // hedefler -> kumenin k listesi
  for (const tg of runMeta.targets) {
    const st = stations[tg.st];
    if (!st) continue;
    const D = tg.D, y = +D.slice(0, 4), mo = +D.slice(5, 7), d = +D.slice(8, 10);
    const startUtc = localToUtc(st.timezone, y, mo, d, 0, 0); // D yerel 00:00 = UTC ms
    // gecerli valid: [start+9h, start+22h)
    const kLo = Math.ceil((startUtc + HR9 - initMs) / MS6H);
    const kHi = Math.floor((startUtc + HR22 - 1 - initMs) / MS6H);
    if (kLo < 0 || kHi > nSteps - 1) continue; // hedef gun init tahmin araliginda degil -> kismi-gun max uretme, atla
    const ks = [];
    for (let k = kLo; k <= kHi; k++) ks.push(stepList[k][0]);
    if (ks.length === 0) continue;
    out.push({ tg, st, ks, D, horizon: tg.horizon });
  }
  if (out.length === 0) return { code: run.code, date: run.date, results: [] };
  // gerekli k seti -> düzlem fetch
  const need = new Map();
  for (const o of out) for (const k of o.ks) need.set(k, stepList.find((s) => s[0] === k));
  const planes = {};
  const fetched = await mapLimit([...need.keys()], 8, async (k) => {
    const [, off, size] = need.get(k);
    const { f32, nlat, nlon } = await fetchPlane(cat.file, off, size);
    return [k, { f32, nlat, nlon }];
  });
  for (const [k, p] of fetched) planes[k] = p;
  const results = [];
  for (const o of out) {
    let vmax = -Infinity;
    for (const k of o.ks) {
      const p = planes[k];
      vmax = Math.max(vmax, bilin(p.f32, p.nlat, p.nlon, latStep, lonStep, o.st.latitude, o.st.longitude));
    }
    const actual = acts[`${o.tg.st}|${o.D}`];
    results.push({ code: run.code, date: run.date, hh: run.hh, st: o.tg.st, D: o.D, horizon: o.horizon, pred: vmax, actual: actual ?? null });
  }
  return { code: run.code, date: run.date, hh: run.hh, results };
}

async function main() {
  const catFiles = fs.readdirSync(CAT).filter((x) => x.startsWith('cat_')).sort();
  console.log(`${catFiles.length} katalog bulundu`);
  let all = [];
  const failed = [];
  let iFile = 0;
  for (const f of catFiles) {
    iFile++;
    try {
      const { results } = await runCatalog(path.join(CAT, f));
      all = all.concat(results);
    } catch (e) {
      failed.push(`${f}: ${e.message || e}`);
      console.log(`[${iFile}/${catFiles.length}] HATA ${f} — ${e.message || e}`);
      continue;
    }
    if (iFile % 20 === 0) console.log(`[${iFile}/${catFiles.length}] islendi...`);
  }
  // yazma modunda DB'ye upsert
  if (!DRY) {
    if (failed.length > 0) {
      console.error(`KISMI YAZMA ENGELLENDI: ${failed.length} dosya fetch hatasi yasadigi icin DB'ye hic yazilmadi.`);
      for (const s of failed) console.error('  ' + s);
      process.exit(1);
    }
    let n = 0;
    for (const r of all) {
      const prov = MODEL_PROVIDER[r.code] || r.code;
      const runAt = `${r.date}T${r.hh}:00:00.000Z`;
      delStmt.run(prov, r.st, r.D, r.horizon, runAt);
      const meta = { init: runAt, ...(process.env.AIWP_VIA ? { via: process.env.AIWP_VIA } : {}) };
      insStmt.run(r.st, prov, runAt, r.D, r.horizon, Math.round(r.pred * 100) / 100, JSON.stringify(meta));
      n++;
    }
    console.log(`DB'ye yazildi: ${n} satir`);
  } else {
    // dry: model bazli MAE / bias
    const byM = {};
    for (const r of all) {
      if (r.actual == null) continue;
      const e = r.pred - r.actual;
      const g = byM[r.code] || (byM[r.code] = { n: 0, ae: 0, b: 0, worst: null });
      g.n++; g.ae += Math.abs(e); g.b += e;
      if (!g.worst || Math.abs(e) > g.worst.abs) g.worst = { st: r.st, D: r.D, pred: r.pred, act: r.actual, e: e };
    }
    console.log('== MODEL BAZLI DOGRULUK (dry, vs settlement) ==');
    for (const [code, g] of Object.entries(byM)) {
      console.log(`${code.padEnd(16)} n=${g.n} MAE=${(g.ae / g.n).toFixed(2)} bias=${(g.b / g.n).toFixed(2)} enKotu=${g.worst.st} ${g.worst.D} pred=${g.worst.pred.toFixed(1)} act=${g.worst.act} (${g.worst.e.toFixed(1)})`);
    }
    console.log(`toplam hesaplanan: ${all.length}`);
  }
  db.close();
}
main().catch((e) => { console.error('FATAL', e); process.exit(1); });
