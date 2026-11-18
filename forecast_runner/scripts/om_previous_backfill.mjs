// Open-Meteo Previous Runs backfill / GÜNLÜK besleyici (tek anonim arşiv modeli)
// ------------------------------------------------------------
// Kaynak: https://previous-runs-api.open-meteo.com/v1/forecast
// Not (ölçüldü 2026-09-05): `model=` parametresi şu an YOKSAYILIYOR — tüm model adları aynı
// arşivi döndürüyor. Bu yüzden tek provider 'Open-Meteo previous' olarak saklıyoruz (model kimliği
// anonim; OM varsayılan arşivi). horizon_days=1/2 → temperature_2m_previous_day1..2 (24/48s
// önceki koşunun tahmini). Günlük max = istenen yerel saat dilimindeki saatlik max.
//
// Kapsam: config'deki istasyonlar; her istasyon için pencere = MAIN'deki ilk GERÇEKLEŞEN settlement
// tarihi .. dün. OM_PREV_TAIL_DAYS=N verilirse yalnız son N günü çeker (GÜNLÜK besleme; eskisini silmez).
// ÖLÇÜLDÜ (2026-10-13): API `start_date`'e tam uymuyor — arşivinde daha eski gün varsa onu da döndürüyor,
// bu yüzden MIN(target_date) istenen pencereden eski çıkabiliyor (2025-10-05'e kadar görüldü). Zararsız:
// o günlerin settlement'ı yok, skorlanamaz. Ama "neden eski tarih var" diye şaşırma.
//
// Yazma: MAIN DB (C:\HEAT data\db — 2026-09-30'da D:'den taşındı) forecast_snapshots, provider='Open-Meteo previous', source='Open-Meteo',
// run_at_utc = (hedef - N gün)T12:00:00Z (mevcut OM arşivi konvansiyonu). Delete-then-insert:
// yeniden çalıştırmak güvenli (idempotent).
//
// İzin: 2026-09-05 kullanıcı onayı (Track A); günlük besleyici 2026-09-08 (Kural 10). MAIN'e yazar.

import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const cfg = JSON.parse(readFileSync(path.join(__dirname, '..', 'windows-runner', 'config.json'), 'utf8'));
const stations = cfg.stations;

const MAIN = process.env.HEAT_MAIN_DB || 'file:C:/HEAT data/db/tempmarket-edge.sqlite';
const PROVIDER = 'Open-Meteo previous';
const SOURCE = 'Open-Meteo';
const LEADS = [1, 2]; // T3 üretilmiyor (ANAYASA §3: yalnız t1/t2)
const CHUNK_DAYS = 60;
// GÜNLÜK mod: OM_PREV_TAIL_DAYS=N verilirse yalnız son N günü (dün dahil) tazele; geçmişe dokunma.
const TAIL_DAYS = process.env.OM_PREV_TAIL_DAYS ? Number(process.env.OM_PREV_TAIL_DAYS) : null;
const HOST = 'https://previous-runs-api.open-meteo.com/v1/forecast';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// "dün" = son tamamlanmış UTC günü; settlement tarafında güvenli kesim
const END = (() => { const d = new Date(Date.now() - 86400000); return d.toISOString().slice(0, 10); })();

function ymdShift(dateStr, days) {
  const [y, m, d] = dateStr.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d) + days * 86400000);
  return dt.toISOString().slice(0, 10);
}

async function fetchChunk(st, startDate, endDate) {
  const vars = LEADS.map((n) => `temperature_2m_previous_day${n}`).join(',');
  const url = `${HOST}?latitude=${st.latitude}&longitude=${st.longitude}&hourly=${vars}` +
    `&start_date=${startDate}&end_date=${endDate}&timezone=${encodeURIComponent(st.timezone)}&temperature_unit=celsius`;
  let lastErr;
  for (let attempt = 1; attempt <= 4; attempt++) {
    try {
      const res = await fetch(url);
      const j = await res.json();
      if (j.error) throw Object.assign(new Error(j.reason), { apiErr: true });
      return j;
    } catch (e) {
      if (e.apiErr && /limit/i.test(e.message)) throw e; // kota hatası → dur, devam etme
      lastErr = e;
      await sleep(1000 * attempt);
    }
  }
  throw lastErr;
}

const db = new DatabaseSync(MAIN);
db.exec('PRAGMA busy_timeout=8000');
db.exec('PRAGMA journal_mode=WAL');

// main'in gerçekleşen settlement başlangıcı (okuma amaçlı ro sorgusu ana handle üzerinden)
const getMinSettlement = db.prepare(
  "SELECT MIN(target_date) mn FROM settlements WHERE station_id=? AND target_date<=?"
);
const delStmt = db.prepare(
  "DELETE FROM forecast_snapshots WHERE provider=? AND station_id=? AND target_date BETWEEN ? AND ?"
);
const insStmt = db.prepare(
  `INSERT INTO forecast_snapshots
     (station_id, source, provider, run_at_utc, target_date, horizon_days, predicted_high_c, raw_json)
   VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
);

let totalRows = 0, totalChunks = 0, skipped = [], failed = [];

for (const st of stations) {
  const minRow = getMinSettlement.get(st.id, END);
  if (!minRow || !minRow.mn) { skipped.push(`${st.id}(settlement yok)`); continue; }
  // GÜNLÜK mod: TAIL_DAYS verilirse yalnız son N günlük pencereyi tazele (geçmiş dokunulmaz).
  const startDate = TAIL_DAYS
    ? (minRow.mn < ymdShift(END, -(TAIL_DAYS - 1)) ? ymdShift(END, -(TAIL_DAYS - 1)) : minRow.mn)
    : minRow.mn;

  // pencereyi 60 günlük parçalara böl
  const dates = [];
  for (let d = startDate; d <= END; d = ymdShift(d, 1)) dates.push(d);
  const chunks = [];
  for (let i = 0; i < dates.length; i += CHUNK_DAYS) {
    chunks.push([dates[i], dates[Math.min(i + CHUNK_DAYS - 1, dates.length - 1)]]);
  }

  // saatlik → yerel güne göre günlük max (lead başına)
  const dailyMax = new Map(); // 'date|lead' -> number
  let quotaHit = false;
  for (const [cs, ce] of chunks) {
    let j;
    try { j = await fetchChunk(st, cs, ce); }
    catch (e) {
      if (e.apiErr && /limit/i.test(e.message)) { quotaHit = true; break; }
      failed.push(`${st.id}@${cs}: ${e.message}`);
      break;
    }
    const t = j.hourly.time || [];
    for (const lead of LEADS) {
      const arr = j.hourly[`temperature_2m_previous_day${lead}`] || [];
      for (let i = 0; i < t.length; i++) {
        const dd = t[i].slice(0, 10);
        const key = `${dd}|${lead}`;
        const v = arr[i];
        dailyMax.set(key, Math.max(dailyMax.get(key) ?? -99, v));
      }
    }
    totalChunks++;
    await sleep(150);
  }
  if (quotaHit) { console.log(`[KOTA] ${st.id} — durduruldu. Kalan istasyonlar işlenmedi.`); break; }
  if (failed.length && failed[failed.length - 1].startsWith(st.id)) { console.log(`[HATA] ${failed[failed.length - 1]}`); continue; }

  // delete + insert (pencere içi)
  db.exec('BEGIN');
  try {
    delStmt.run(PROVIDER, st.id, startDate, END);
    let n = 0;
    for (const [key, v] of dailyMax) {
      const [dd, leadStr] = key.split('|');
      const lead = Number(leadStr);
      const runAt = `${ymdShift(dd, -lead)}T12:00:00Z`;
      insStmt.run(st.id, SOURCE, PROVIDER, runAt, dd, lead, v, '{}');
      n++;
    }
    db.exec('COMMIT');
    totalRows += n;
    console.log(`${st.id} ${st.city ?? ''} | pencere ${startDate}..${END} (${dates.length}g) | chunk=${chunks.length} | satir=${n}`);
  } catch (e) {
    db.exec('ROLLBACK');
    failed.push(`${st.id}: ${e.message}`);
    console.log(`[HATA] ${st.id}: ${e.message}`);
  }
}

console.log('---');
console.log(`BİTTİ: satır=${totalRows} chunk=${totalChunks} skip=${skipped.length} hata=${failed.length}`);
if (skipped.length) console.log('skip:', skipped.join(', '));
if (failed.length) console.log('hatalar:', failed.slice(0, 20).join(' | '));
db.close();
