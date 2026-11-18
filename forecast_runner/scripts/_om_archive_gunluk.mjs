// Ne işe yarar: OM arşiv 8 modelini GÜNLÜK besleyen betik (t0/aynı-gün h0 kaydı).
// archive-api.open-meteo.com her model için hedef günün günlük maks tahminini verir;
// bu betik "dün"e kadar olan hedefleri forecast_snapshots'a yazar (konvansiyon: source='Open-Meteo',
// provider=model adı, horizon_days=0, run_at_utc = hedef gün T12:00:00Z). Mevcut 2024-09..2026-09-02
// bloğunun birebir devamıdır; bu yüzden Kural 10 listesindeki "OM arşiv 8" ailesi kesintisiz kalır.
//
// GÜNLÜK mod (varsayılan): yalnız son OM_ARCHIVE_TAIL_DAYS (12) günü sil+ekle (idempotent; geçmişe dokunmaz).
// Kaynak: https://archive-api.open-meteo.com/v1/archive?daily=temperature_2m_max&models=<model>
// Yazma: MAIN DB (HEAT_MAIN_DB). Onay: 2026-09-08 Kural 10 günlük besleyici.
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const cfg = JSON.parse(readFileSync(path.join(__dirname, '..', 'windows-runner', 'config.json'), 'utf8'));
const stations = cfg.stations;

const MAIN = process.env.HEAT_MAIN_DB || 'file:C:/HEAT data/db/tempmarket-edge.sqlite';
const MODELS = ['cma_grapes_global', 'ecmwf_ifs025', 'gem_global', 'gfs_seamless',
  'icon_seamless', 'jma_seamless', 'meteofrance_seamless', 'ukmo_seamless'];
const TAIL = Number(process.env.OM_ARCHIVE_TAIL_DAYS || 12);
const HOST = 'https://archive-api.open-meteo.com/v1/archive';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const todayIso = new Date().toISOString().slice(0, 10);
const END = (() => { const d = new Date(Date.now() - 86400000); return d.toISOString().slice(0, 10); })(); // dün
function ymdShift(dateStr, days) {
  const [y, m, d] = dateStr.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d) + days * 86400000);
  return dt.toISOString().slice(0, 10);
}
const START = ymdShift(END, -(TAIL - 1));

async function fetchModel(st, model, startDate, endDate) {
  const url = `${HOST}?latitude=${st.latitude}&longitude=${st.longitude}&daily=temperature_2m_max` +
    `&models=${model}&start_date=${startDate}&end_date=${endDate}&timezone=${encodeURIComponent(st.timezone)}&temperature_unit=celsius`;
  let lastErr;
  for (let attempt = 1; attempt <= 4; attempt++) {
    try {
      const res = await fetch(url);
      const j = await res.json();
      if (j.error) throw Object.assign(new Error(j.reason), { apiErr: true });
      return j;
    } catch (e) {
      if (e.apiErr && /limit/i.test(e.message)) throw e;
      lastErr = e;
      await sleep(1000 * attempt);
    }
  }
  throw lastErr;
}

const db = new DatabaseSync(MAIN);
db.exec('PRAGMA busy_timeout=8000');
db.exec('PRAGMA journal_mode=WAL');

const delStmt = db.prepare(
  "DELETE FROM forecast_snapshots WHERE provider=? AND station_id=? AND target_date BETWEEN ? AND ?"
);
const insStmt = db.prepare(
  `INSERT INTO forecast_snapshots
     (station_id, source, provider, run_at_utc, target_date, horizon_days, predicted_high_c, raw_json)
   VALUES (?, 'Open-Meteo', ?, ?, ?, 0, ?, '{}')`
);

let totalRows = 0, quotaHit = false, failed = [];
for (const st of stations) {
  if (quotaHit) break;
  for (const model of MODELS) {
    let j;
    try { j = await fetchModel(st, model, START, END); }
    catch (e) {
      if (e.apiErr && /limit/i.test(e.message)) { quotaHit = true; console.log(`[KOTA] ${st.id}/${model}`); break; }
      failed.push(`${st.id}/${model}: ${e.message}`);
      continue;
    }
    const times = j.daily?.time || [];
    const vals = j.daily?.temperature_2m_max || [];
    let n = 0;
    db.exec('BEGIN');
    try {
      delStmt.run(model, st.id, START, END);
      for (let i = 0; i < times.length; i++) {
        const target = times[i].slice(0, 10);
        if (target > END) continue; // "bugün" kısmi olabilir → atla
        const v = vals[i];
        if (v === null || v === undefined) continue;
        const runAt = `${target}T12:00:00Z`;
        insStmt.run(st.id, model, runAt, target, v);
        n++;
      }
      db.exec('COMMIT');
    } catch (e) { db.exec('ROLLBACK'); failed.push(`${st.id}/${model}: ${e.message}`); continue; }
    totalRows += n;
    await sleep(250);
  }
  console.log(`${st.id} ${st.city ?? ''} | pencere ${START}..${END} tamam`);
}

console.log('---');
console.log(`BİTTİ: satır=${totalRows} hata=${failed.length}`);
if (failed.length) console.log('hatalar:', failed.slice(0, 15).join(' | '));
db.close();
