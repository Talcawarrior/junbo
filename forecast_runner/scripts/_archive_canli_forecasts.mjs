// Ne işe yarar: canlı tahmin sağlayıcılarının forecast_snapshots satırlarını LIVE'dan MAIN arşiv DB'sine kopyalar
// (uzun vadeli isabet ölçümü için). Idempotent: INSERT OR IGNORE, unique (station_id, source, provider,
// run_at_utc, target_date) anahtarına güvenir — tekrar koşmak 0 ekler, atlanan günü sonraki koşu yakalar.
// - Kaynak LIVE: repo/data/tempmarket-edge.sqlite (runner yazar). Hedef MAIN: HEAT_MAIN_DB (script'ler → MAIN, kural 9).
// - raw_json HEDEFE YAZILMAZ -> '{}' (NOT NULL); isabet/bias yalnız predicted_high_c/low okur; LIVE'ın 3.4GB'ı raw'dur,
//   SELECT'te raw_json asla çekilmez (8 sütun açıkça listelenir).
// - Provider kümesi = rapor_iskabet_t02.mjs canlı seti (7 ad); provider adına göre filtre (source'a göre değil).
// - Eksik station satırları (gelecekte config'e şehir eklenirse) MAIN.stations'a INSERT OR IGNORE ile tamamlanır.
// Kullanim: node --experimental-sqlite scripts/_archive_canli_forecasts.mjs [--dry-run] [--since=YYYY-MM-DD]
// Runner tetikler: windows-runner/index.mjs runCycle sonu, UTC 08:00 (LIVE→MAIN arşiv köprüsü).
import { DatabaseSync } from 'node:sqlite';
import { existsSync } from 'node:fs';
import path from 'node:path';

const LIVE_DB = path.resolve('data/tempmarket-edge.sqlite');
const MAIN_DB = process.env.HEAT_MAIN_DB || 'C:/HEAT data/db/tempmarket-edge.sqlite';

// rapor_iskabet_t02.mjs canlı/LIVE kümesiyle birebir (7 ad). OM-ECMWF04 (bozuk akış) de ayna olarak arşivlenir;
// skorlama zaten BROKEN setiyle hariç tutar.
// 2026-09-15: `Open-Meteo ecmwf_ifs04` ÇIKARILDI — ad ölü, dönen veri tamamen null, satırlar 0°C olarak
// yazılıyordu (6.021/6.021). Yerine canlı `ecmwf_ifs025` geldi. Eski adın MAIN'deki tarihsel satırları
// silinene kadar `train_bias_online` onu dışlamaya devam eder.
const PROVIDERS = [
  'Visual Crossing daily', 'WeatherAPI daily', 'OpenWeatherMap 3-hour', 'NWS forecastHourly',
  'Open-Meteo ecmwf_ifs025', 'Open-Meteo gfs_seamless', 'Open-Meteo icon_seamless',
];

const args = process.argv.slice(2);
const dryRun = args.includes('--dry-run');
const sinceArg = args.find((a) => a.startsWith('--since='));
const SINCE = sinceArg ? sinceArg.slice('--since='.length) : null;

// LIVE settlements'ın canlı/gerçek penceresi (MAIN derin geçmişine DOKUNULMAZ — yalnız bu aralıktaki satırlar
// LIVE'dan upsert edilir). Gerekçe: skorlama "EXISTS settlements" ister; MAIN settlement'ı tek seferlik
// _settlement_live_fix'ten sonra LIVE'ın yeni çözdükleriyle güncellenmezse MAIN'den okunan canlı isabet eksik kalır.
const SETTLE_SYNC_SINCE = '2026-08-25';

if (!existsSync(LIVE_DB) || !existsSync(MAIN_DB)) { console.error('DB yok:', LIVE_DB, MAIN_DB); process.exit(1); }

const l = new DatabaseSync(LIVE_DB, { readOnly: true });
const m = new DatabaseSync(MAIN_DB); // yazma
l.exec('PRAGMA busy_timeout=10000');
m.exec('PRAGMA busy_timeout=10000');

const ph = PROVIDERS.map(() => '?').join(',');
const key = (r) => `${r.station_id}\x1f${r.source}\x1f${r.provider}\x1f${r.run_at_utc}\x1f${r.target_date}`;

// ---- 0) LIVE canlı adayları (raw_json HARİÇ; 8 sütun) ----
const sinceSql = SINCE ? ' AND run_at_utc >= ?' : '';
const liveSel = l.prepare(
  `SELECT station_id, source, provider, run_at_utc, target_date, horizon_days, predicted_high_c, predicted_low_c
     FROM forecast_snapshots WHERE provider IN (${ph})${sinceSql} ORDER BY run_at_utc`);
const liveRows = SINCE ? liveSel.all(...PROVIDERS, SINCE) : liveSel.all(...PROVIDERS);

// ---- 1) MAIN'de zaten var olan canlı anahtarlar (küme) + istasyon id seti ----
const mainKeys = new Set();
for (const r of m.prepare(`SELECT station_id, source, provider, run_at_utc, target_date FROM forecast_snapshots
                            WHERE provider IN (${ph})`).all(...PROVIDERS)) mainKeys.add(key(r));
const mainStationIds = new Set(m.prepare('SELECT id FROM stations').all().map((r) => String(r.id)));

// ---- 2) eksikleri ayır + istasyon tamamla ----
const toAdd = liveRows.filter((r) => !mainKeys.has(key(r)));
const byProv = {};
for (const r of toAdd) { byProv[r.provider] = (byProv[r.provider] || 0) + 1; }

const summary = (label, n) => console.log(`${label}: ${n.toLocaleString('en-US')} satır`);
summary(`LIVE canlı aday${SINCE ? ` (run>=${SINCE})` : ''}`, liveRows.length);
summary('MAIN canlı mevcut', mainKeys.size);
summary('MAIN eklenecek', toAdd.length);
if (Object.keys(byProv).length) console.log('Provider dağılımı (eklenecek):', JSON.stringify(byProv));

// Tahmin defteri önizlemesi (dry-run'da da görünsün — asıl rapor bu satırlardan çıkıyor).
if (dryRun) {
  try {
    const livePl = l.prepare('SELECT COUNT(*) c FROM prediction_log').get().c;
    const cols = m.prepare(`PRAGMA table_info(prediction_log)`).all();
    const mainPl = cols.some((c) => c.name === 'run_at_utc') ? m.prepare('SELECT COUNT(*) c FROM prediction_log').get().c : 0;
    console.log(`tahmin defteri [dry-run]: LIVE ${livePl} satır, MAIN ${mainPl} satır${cols.length && !mainPl ? ' (MAIN şeması eski, koşuda yeniden kurulacak)' : ''}`);
  } catch { /* tablo henüz yoksa sessiz geç */ }
  console.log('[dry-run] yazma yapılmadı.');
  m.close(); l.close(); process.exit(0);
}

// ---- 3) eksik station satırlarını MAIN.stations'a tamamla (INSERT OR IGNORE) ----
const missingStations = liveRows.map((r) => r.station_id).filter((id, i, a) => a.indexOf(id) === i && !mainStationIds.has(String(id)));
if (missingStations.length) {
  const insSt = m.prepare(`INSERT OR IGNORE INTO stations
    (id, icao, city, country, latitude, longitude, elevation_m, timezone, settlement_source, market_slug, enabled, created_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`);
  m.exec('BEGIN');
  let stN = 0;
  for (const sid of missingStations) {
    const s = l.prepare('SELECT * FROM stations WHERE id = ?').get(sid);
    if (!s) continue;
    insSt.run(s.id, s.icao, s.city, s.country, s.latitude, s.longitude, s.elevation_m, s.timezone,
      s.settlement_source, s.market_slug, s.enabled, s.created_at);
    stN++;
  }
  m.exec('COMMIT');
  console.log(`MAIN.stations eklendi (eksik): ${stN}`);
} else {
  console.log('MAIN.stations eksik yok (canlı station id seti MAIN\'de tam).');
}

// ---- 3.5) settlement senkronu: LIVE'ın >= SETTLE_SYNC_SINCE gerçek satırlarını MAIN'e upsert et ----
// (MAIN settlements 09-05 tek seferlik fix sonrası LIVE'ın yeni çözümleriyle geride kalıyor; skorlama
// "EXISTS settlements" istediği için canlı isabetin MAIN'den doğru okunması bu senkronu gerektirir.)
{
  const liveSettles = l.prepare('SELECT station_id, target_date, source, high_c, recorded_at_utc, raw_json FROM settlements WHERE target_date >= ?').all(SETTLE_SYNC_SINCE);
  // adet + MAIN'de eksik/aynı olanlar (upsert etkisini raporlamak için)
  const ups = m.prepare(`INSERT INTO settlements (station_id, target_date, source, high_c, recorded_at_utc, raw_json)
    VALUES (?,?,?,?,?,?)
    ON CONFLICT(station_id, target_date, source) DO UPDATE SET
      high_c = excluded.high_c, recorded_at_utc = excluded.recorded_at_utc, raw_json = excluded.raw_json`);
  let changed = 0;
  if (!dryRun) {
    m.exec('BEGIN');
    try {
      for (const s of liveSettles) ups.run(s.station_id, s.target_date, s.source, s.high_c, s.recorded_at_utc ?? '', s.raw_json ?? '');
      m.exec('COMMIT');
      changed = liveSettles.length;
    } catch (e) {
      try { m.exec('ROLLBACK'); } catch { /* yoksay */ }
      console.error('HATA (settlement senkron ROLLBACK):', e.message);
      m.close(); l.close(); process.exit(1);
    }
  }
  console.log(`settlement senkron (>=${SETTLE_SYNC_SINCE}): LIVE aday ${liveSettles.length}${dryRun ? ' [dry-run]' : ` — upsert edildi ${changed}`}`);
}

// ---- 4) canlı satırları batch transaction ile INSERT OR IGNORE (raw_json='{}') ----
const ins = m.prepare(`INSERT OR IGNORE INTO forecast_snapshots
  (station_id, source, provider, run_at_utc, target_date, horizon_days, predicted_high_c, predicted_low_c, raw_json)
  VALUES (?,?,?,?,?,?,?,?,?)`);
const BATCH = 2000;
let added = 0;
try {
  for (let i = 0; i < toAdd.length; i += BATCH) {
    m.exec('BEGIN');
    const chunk = toAdd.slice(i, i + BATCH);
    for (const r of chunk) {
      ins.run(r.station_id, r.source, r.provider, r.run_at_utc, r.target_date, r.horizon_days,
        r.predicted_high_c, r.predicted_low_c ?? null, '{}');
    }
    m.exec('COMMIT');
    added += chunk.length;
  }
} catch (e) {
  try { m.exec('ROLLBACK'); } catch { /* yoksay */ }
  console.error('HATA (ROLLBACK):', e.message);
  m.close(); l.close(); process.exit(1);
}

// ---- 4b) TAHMİN DEFTERİ (prediction_log) LIVE -> MAIN ----------------------------------------------
// Neden: runner her döngüde "o an ne tahmin ettik" satırı yazar (reports tablosu eziyor, bu ezmiyor).
// Raporlar MAIN okuduğu için defterin MAIN'de olması gerekir. Şema LIVE ile BİREBİR aynı olmalı.
// MAIN'de eski/terk edilmiş bir prediction_log şeması varsa yalnız **boşsa** yeniden kurulur (veri kaybı yok).
{
  const PL_SEMA = `CREATE TABLE prediction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL, target_date TEXT NOT NULL, run_at_utc TEXT NOT NULL,
    horizon_days INTEGER, raw_prediction REAL, final_prediction REAL,
    confidence_lower REAL, confidence_upper REAL, trained_count INTEGER, total_count INTEGER,
    model_version TEXT, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')))`;
  const mainCols = m.prepare(`PRAGMA table_info(prediction_log)`).all();
  const mainEski = mainCols.length > 0 && !mainCols.some((c) => c.name === 'run_at_utc');
  if (mainEski) {
    const n = m.prepare('SELECT COUNT(*) c FROM prediction_log').get().c;
    if (n === 0) { m.exec('DROP TABLE prediction_log'); m.exec(PL_SEMA); console.log('MAIN.prediction_log eski şemadan yeni şemaya çevrildi (boştu).'); }
    else console.warn(`MAIN.prediction_log dolu (${n} satır) ve şeması farklı — dokunulmadı, tahmin defteri arşivlenemedi.`);
  } else if (mainCols.length === 0) {
    m.exec(PL_SEMA);
  }
  if (m.prepare(`PRAGMA table_info(prediction_log)`).all().some((c) => c.name === 'run_at_utc')) {
    const livePl = l.prepare(`SELECT station_id, target_date, run_at_utc, horizon_days, raw_prediction, final_prediction,
      confidence_lower, confidence_upper, trained_count, total_count, model_version, created_at FROM prediction_log`).all();
    const insPl = m.prepare(`INSERT OR IGNORE INTO prediction_log
      (station_id, target_date, run_at_utc, horizon_days, raw_prediction, final_prediction,
       confidence_lower, confidence_upper, trained_count, total_count, model_version, created_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`);
    const knownPl = new Set(m.prepare('SELECT station_id, target_date, run_at_utc FROM prediction_log')
      .all().map((r) => `${r.station_id}\x1f${r.target_date}\x1f${r.run_at_utc}`));
    const plAdd = livePl.filter((r) => !knownPl.has(`${r.station_id}\x1f${r.target_date}\x1f${r.run_at_utc}`));
    if (dryRun) {
      console.log(`tahmin defteri [dry-run]: LIVE ${livePl.length} satır, MAIN'e eklenecek ${plAdd.length}`);
    } else {
      let plAdded = 0;
      m.exec('BEGIN');
      try {
        for (const r of plAdd) {
          insPl.run(r.station_id, r.target_date, r.run_at_utc, r.horizon_days, r.raw_prediction, r.final_prediction,
            r.confidence_lower, r.confidence_upper, r.trained_count, r.total_count, r.model_version, r.created_at);
          plAdded += 1;
        }
        m.exec('COMMIT');
      } catch (e) { m.exec('ROLLBACK'); throw e; }
      console.log(`tahmin defteri arşivi: LIVE ${livePl.length} satır, MAIN'e eklendi ${plAdded}`);
    }
  }
}

// ---- 5) doğrula ----
const afterMain = m.prepare(`SELECT COUNT(*) n FROM forecast_snapshots WHERE provider IN (${ph})`).get(...PROVIDERS).n;
const afterLive = liveRows.length > 0 && !SINCE
  ? l.prepare(`SELECT COUNT(*) n FROM forecast_snapshots WHERE provider IN (${ph})`).get(...PROVIDERS).n : null;
summary('MAIN canlı toplam (sonrası)', afterMain);
if (afterLive != null) summary('LIVE canlı toplam', afterLive);
console.log(`EKLENDİ: ${added} (yok sayılan: ${toAdd.length - added})`);
m.close(); l.close();
