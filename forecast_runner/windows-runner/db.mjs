import fs from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

const dataDir = process.env.TEMP_MARKET_DATA_DIR || path.resolve('data');
fs.mkdirSync(dataDir, { recursive: true });
const dbPath = process.env.TEMP_MARKET_DB || path.join(dataDir, 'tempmarket-edge.sqlite');
const db = new DatabaseSync(dbPath);

db.exec(`
  PRAGMA journal_mode = WAL;
  PRAGMA foreign_keys = ON;
  CREATE TABLE IF NOT EXISTS stations (
    id TEXT PRIMARY KEY,
    icao TEXT NOT NULL UNIQUE,
    city TEXT NOT NULL,
    country TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    elevation_m REAL NOT NULL,
    timezone TEXT NOT NULL,
    settlement_source TEXT NOT NULL,
    market_slug TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS market_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    market_slug TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    source TEXT NOT NULL,
    rule_text TEXT,
    raw_json TEXT NOT NULL,
    UNIQUE(station_id, market_slug),
    FOREIGN KEY(station_id) REFERENCES stations(id)
  );
  CREATE TABLE IF NOT EXISTS source_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT,
    source TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    status_code INTEGER,
    response_ms INTEGER,
    ok INTEGER NOT NULL,
    error TEXT,
    FOREIGN KEY(station_id) REFERENCES stations(id)
  );
  CREATE TABLE IF NOT EXISTS forecast_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    source TEXT NOT NULL,
    provider TEXT NOT NULL,
    run_at_utc TEXT NOT NULL,
    target_date TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    predicted_high_c REAL NOT NULL,
    predicted_low_c REAL,
    raw_json TEXT NOT NULL,
    UNIQUE(station_id, source, provider, run_at_utc, target_date),
    FOREIGN KEY(station_id) REFERENCES stations(id)
  );
  -- WEATHERNEXT 2 ENSEMBLE (2026-09-19). Nokta tahmini değil DAĞILIM: 64 üyenin günlük maksimumu.
  -- Bu tablo AYRI durur çünkü forecast_snapshots'a yazılsaydı iki hata birden tekrarlanırdı:
  --   (1) ANAHTAR: oradaki run_at_utc ÇEKİM ANI'dır (index.mjs new Date().toISOString()) → her turda
  --       yeni satır. Buranın anahtarı modelin KOŞU anıdır (00/12 UTC, wnRunKey) → günde en çok 2 satır
  --       ve üzerine yazılır. Sınırlı büyüme — bu, 2026-09-17'de market_rules'ta düzeltilen hatanın aynısı.
  --   (2) HAM YÜK: forecast_snapshots 2026-09-19'da 2.200 MB ölçüldü ve 2.003 MB'ı (%91) raw_json,
  --       yani aynı yanıtın kopyaları. Burada üyeler satır başına BİR KEZ, sıkışık JSON dizisi olarak durur.
  -- fetched_at_utc bilerek TEKİLLİK ANAHTARININ DIŞINDA: uçucu sütun anahtara girerse kuyruk sonsuz olur.
  CREATE TABLE IF NOT EXISTS wn_ensemble (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    model TEXT NOT NULL,
    run_at_utc TEXT NOT NULL,
    target_date TEXT NOT NULL,
    member_count INTEGER NOT NULL,
    mean_c REAL,
    spread_c REAL,
    members_json TEXT NOT NULL,
    fetched_at_utc TEXT,
    UNIQUE(station_id, model, run_at_utc, target_date),
    FOREIGN KEY(station_id) REFERENCES stations(id)
  );
  CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    source TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    target_date TEXT NOT NULL,
    temperature_c REAL,
    running_high_c REAL,
    actual_max_c REAL,
    actual_min_c REAL,
    actual_source TEXT,
    raw_json TEXT NOT NULL,
    UNIQUE(station_id, source, observed_at_utc),
    FOREIGN KEY(station_id) REFERENCES stations(id)
  );
  CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    target_date TEXT NOT NULL,
    source TEXT NOT NULL,
    high_c REAL NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE(station_id, target_date, source),
    FOREIGN KEY(station_id) REFERENCES stations(id)
  );
  CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    target_date TEXT NOT NULL,
    generated_at_utc TEXT NOT NULL,
    report_json TEXT NOT NULL,
    UNIQUE(station_id, target_date)
  );
  -- TAHMİN DEFTERİ (2026-09-15). NEDEN AYRI TABLO: reports tablosu UNIQUE(station_id,target_date) yüzünden her
  --   saatlik döngü aynı şehir-günün raporunu EZİYOR → geriye hedef günün kendisinde yazılmış rapor kalıyor,
  --   "dün yarın için ne demiştik" (t1/t2) kayboluyordu. Ölçüm: 824 dolu MOS raporundan t1'e yalnız 2 tane
  --   düşüyordu. Bu tabloda TEKİL KISIT YOK: her döngü yeni satır yazar → tahminin zaman içindeki izi korunur
  --   ve ufuk (t0/t1/t2) geriye dönük güvenle hesaplanabilir. Kural 9: runner LIVE'a yazar, günlük blokta
  --   _archive_canli_forecasts.mjs MAIN'e kopyalar (forecast_snapshots ile aynı desen).
  CREATE TABLE IF NOT EXISTS prediction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    target_date TEXT NOT NULL,
    run_at_utc TEXT NOT NULL,
    horizon_days INTEGER,
    raw_prediction REAL,      -- ham ortalama (naif ensemble) — MOS'un kıyas tabanı
    final_prediction REAL,    -- Heat'in nihai tahmini: MOS varsa MOS, yoksa naif ortalama
    confidence_lower REAL,    -- o anki tahminlerin en düşüğü
    confidence_upper REAL,    -- en yükseği
    trained_count INTEGER,    -- MOS'a giren eğitimli kaynak sayısı
    total_count INTEGER,      -- toplam kaynak sayısı
    model_version TEXT,       -- 'mos-v1' | 'naive-v1'
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
  );
  CREATE INDEX IF NOT EXISTS idx_forecast_target ON forecast_snapshots(station_id, target_date, run_at_utc);
  CREATE INDEX IF NOT EXISTS idx_observation_target ON observations(station_id, target_date, observed_at_utc);
  CREATE TABLE IF NOT EXISTS training_pairs (
    station_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    target_date TEXT NOT NULL,
    predicted_high_c REAL NOT NULL,
    actual_high_c REAL NOT NULL,
    actual_source TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (station_id, provider, horizon_days, target_date)
  );
  CREATE TABLE IF NOT EXISTS bias_params (
    station_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    n INTEGER NOT NULL,
    bias_c REAL NOT NULL,
    bias_shrunk_c REAL NOT NULL,
    a REAL,
    b REAL,
    mae_c REAL NOT NULL,
    rmse_c REAL NOT NULL,
    hit_rate REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (station_id, provider, horizon_days)
  );
  CREATE TABLE IF NOT EXISTS climatology (
    station_id TEXT NOT NULL,
    doy INTEGER NOT NULL,
    mean_c REAL NOT NULL,
    std_c REAL NOT NULL,
    p5_c REAL,
    p95_c REAL,
    PRIMARY KEY (station_id, doy)
  );
  CREATE TABLE IF NOT EXISTS evaluation_daily (
    evaluated_at TEXT NOT NULL,
    horizon_days INTEGER,
    ensemble_type TEXT NOT NULL,
    provider TEXT,
    mae REAL NOT NULL,
    rmse REAL NOT NULL,
    hit_rate REAL NOT NULL,
    n INTEGER NOT NULL,
    PRIMARY KEY (evaluated_at, horizon_days, ensemble_type, provider)
  );
  CREATE TABLE IF NOT EXISTS model_versions (
    model_name TEXT NOT NULL,
    horizon_days INTEGER,
    version TEXT NOT NULL,
    trained_at TEXT NOT NULL,
    train_rows INTEGER NOT NULL,
    val_mae REAL,
    params_json TEXT,
    artifact_path TEXT,
    PRIMARY KEY (model_name, version)
  );
  CREATE INDEX IF NOT EXISTS idx_training_pairs_target ON training_pairs(target_date, provider);
  CREATE INDEX IF NOT EXISTS idx_bias_params_provider ON bias_params(provider, horizon_days);
`);

// Eski/terk edilmiş bir `prediction_log` şeması varsa (2026-09-15 öncesi iki farklı tasarım, ikisi de 0 satır)
// yeni şemaya çevir. Yalnız **boşsa** düşürülür — dolu bir tabloyu asla silmeyiz, veri kaybı olur.
{
  const info = db.prepare(`PRAGMA table_info(prediction_log)`).all();
  const eskiSema = info.length > 0 && !info.some((c) => c.name === 'run_at_utc');
  if (eskiSema) {
    const n = db.prepare('SELECT COUNT(*) c FROM prediction_log').get().c;
    if (n === 0) {
      db.exec('DROP TABLE prediction_log');
      db.exec(`CREATE TABLE prediction_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        station_id TEXT NOT NULL, target_date TEXT NOT NULL, run_at_utc TEXT NOT NULL,
        horizon_days INTEGER, raw_prediction REAL, final_prediction REAL,
        confidence_lower REAL, confidence_upper REAL, trained_count INTEGER, total_count INTEGER,
        model_version TEXT, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')))`);
      db.exec('CREATE INDEX IF NOT EXISTS idx_prediction_log_lookup ON prediction_log(station_id, target_date, run_at_utc)');
      console.log('[db] prediction_log eski şemadan yeni şemaya çevrildi (tablo boştu)');
    } else {
      // Doluysa dokunma; eksik sütunları ekle (veri korunur, yalnız yeni alanlar null kalır).
      for (const [col, def] of [['run_at_utc', 'TEXT'], ['horizon_days', 'INTEGER'], ['trained_count', 'INTEGER'], ['total_count', 'INTEGER']]) {
        if (!info.some((c) => c.name === col)) db.exec(`ALTER TABLE prediction_log ADD COLUMN ${col} ${def}`);
      }
      console.warn(`[db] prediction_log dolu (${n} satır) — şema değiştirilmedi, yalnız eksik sütunlar eklendi`);
    }
  }
  // İndeks göçten SONRA kurulur: eski şemada run_at_utc yoktu, önce kurulsaydı "no such column" verirdi.
  // UNIQUE: bir döngü bir (şehir, hedef gün) için tek satır yazar → arşiv kopyası INSERT OR IGNORE ile
  // idempotent olur (aynı satır iki kez arşivlenmez).
  db.exec('CREATE UNIQUE INDEX IF NOT EXISTS idx_prediction_log_key ON prediction_log(station_id, target_date, run_at_utc)');
}

function ensureColumn(table, column, definition) {
  const columns = db.prepare(`PRAGMA table_info(${table})`).all();
  if (!columns.some((item) => item.name === column)) db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${definition}`);
}

ensureColumn('forecast_snapshots', 'predicted_low_c', 'REAL');
ensureColumn('observations', 'actual_max_c', 'REAL');
ensureColumn('observations', 'actual_min_c', 'REAL');
ensureColumn('observations', 'actual_source', 'TEXT');

export function closeDb() {
  db.close();
}

export function seedStations(stations) {
  const existingByIcao = new Map(
    db.prepare('SELECT id, icao FROM stations').all().map((row) => [String(row.icao).toUpperCase(), row.id]),
  );
  const statement = db.prepare(`INSERT INTO stations (id, icao, city, country, latitude, longitude, elevation_m, timezone, settlement_source, market_slug, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET icao=excluded.icao, city=excluded.city, country=excluded.country, latitude=excluded.latitude, longitude=excluded.longitude, elevation_m=excluded.elevation_m, timezone=excluded.timezone, settlement_source=excluded.settlement_source, market_slug=excluded.market_slug, enabled=1`);
  for (const station of stations) {
    const existingId = existingByIcao.get(String(station.icao).toUpperCase());
    if (existingId && existingId !== station.id) station.id = existingId;
    statement.run(station.id, station.icao, station.city, station.country, station.latitude, station.longitude, station.elevationM, station.timezone, station.settlementSource, station.marketSlug || null, new Date().toISOString());
  }
}

export function logSourceRequest(input) {
  db.prepare(`INSERT INTO source_requests (station_id, source, endpoint, requested_at, status_code, response_ms, ok, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`)
    .run(input.stationId || null, input.source, input.endpoint, input.requestedAt, input.statusCode ?? null, input.responseMs ?? null, input.ok ? 1 : 0, input.error || null);
}

export function insertForecastSnapshot(input) {
  db.prepare(`INSERT OR IGNORE INTO forecast_snapshots (station_id, source, provider, run_at_utc, target_date, horizon_days, predicted_high_c, predicted_low_c, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`)
    .run(input.stationId, input.source, input.provider, input.runAtUtc, input.targetDate, input.horizonDays, input.predictedHighC, input.predictedLowC ?? null, JSON.stringify(input.raw));
}

// WeatherNext 2 ensemble — üye başına GÜNLÜK MAKS (bkz. şemadaki açıklama).
// `ON CONFLICT ... DO UPDATE`: aynı koşuyu saatlerce tekrar çekmek yeni satır üretmez, üzerine yazar.
export function insertWnEnsemble(input) {
  db.prepare(`INSERT INTO wn_ensemble (station_id, model, run_at_utc, target_date, member_count, mean_c, spread_c, members_json, fetched_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(station_id, model, run_at_utc, target_date) DO UPDATE SET
      member_count   = excluded.member_count,
      mean_c         = excluded.mean_c,
      spread_c       = excluded.spread_c,
      members_json   = excluded.members_json,
      fetched_at_utc = excluded.fetched_at_utc`)
    .run(input.stationId, input.model, input.runAtUtc, input.targetDate, input.memberCount, input.meanC ?? null, input.spreadC ?? null, JSON.stringify(input.members), input.fetchedAtUtc ?? null);
}

export function insertObservation(input) {
  db.prepare(`INSERT OR REPLACE INTO observations (station_id, source, observed_at_utc, target_date, temperature_c, running_high_c, actual_max_c, actual_min_c, actual_source, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
    .run(input.stationId, input.source, input.observedAtUtc, input.targetDate, input.temperatureC ?? null, input.runningHighC ?? null, input.actualMaxC ?? null, input.actualMinC ?? null, input.actualSource ?? null, JSON.stringify(input.raw));
}

export function insertSettlement(input) {
  const result = db.prepare(`INSERT INTO settlements (station_id, target_date, source, high_c, recorded_at_utc, raw_json) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(station_id, target_date, source) DO UPDATE SET high_c=excluded.high_c, recorded_at_utc=excluded.recorded_at_utc, raw_json=excluded.raw_json`)
    .run(input.stationId, input.targetDate, input.source, input.highC, input.recordedAtUtc, JSON.stringify(input.raw || {}));
  return Number(result.changes || 0) > 0;
}

// PİYASA KURALI — market başına TEK satır (2026-09-17). Eskiden tekillik `(station_id, market_slug,
// fetched_at)` idi ve `fetched_at` her turda yenilendiği için INSERT OR IGNORE hiçbir zaman elemiyordu:
// 21 günde 121.288 satır ama yalnız 397 farklı market (6,0 GB, günde ~430 MB). Artık aynı marketin
// satırı ÜZERİNE yazılır; tablo 397 satırda sabitlenir ve ham belge + kural metni korunur.
export function insertMarketRule(input) {
  db.prepare(`INSERT INTO market_rules (station_id, market_slug, fetched_at, source, rule_text, raw_json) VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(station_id, market_slug) DO UPDATE SET
      fetched_at = excluded.fetched_at,
      source     = excluded.source,
      rule_text  = excluded.rule_text,
      raw_json   = excluded.raw_json`)
    .run(input.stationId, input.marketSlug, input.fetchedAt, input.source, input.ruleText || null, JSON.stringify(input.raw));
}

export function writeReport(stationId, targetDate, report) {
  const generatedAtUtc = new Date().toISOString();
  db.prepare(`INSERT INTO reports (station_id, target_date, generated_at_utc, report_json) VALUES (?, ?, ?, ?) ON CONFLICT(station_id, target_date) DO UPDATE SET generated_at_utc=excluded.generated_at_utc, report_json=excluded.report_json`)
    .run(stationId, targetDate, generatedAtUtc, JSON.stringify(report));
  return generatedAtUtc;
}

// Tahmin defterine toplu yazım (tek transaction — satır başına fsync yok). Runner her döngüde çağırır.
// Tekil kısıt YOK: aynı şehir-gün için her döngü ayrı satır → tahminin zaman içindeki gelişimi korunur.
export function logPredictions(rows) {
  const list = (rows || []).filter((r) => r && r.finalPrediction != null);
  if (!list.length) return 0;
  const stmt = db.prepare(`INSERT INTO prediction_log
    (station_id, target_date, run_at_utc, horizon_days, raw_prediction, final_prediction,
     confidence_lower, confidence_upper, trained_count, total_count, model_version)
    VALUES (?,?,?,?,?,?,?,?,?,?,?)`);
  db.exec('BEGIN');
  try {
    for (const r of list) {
      stmt.run(r.stationId, r.targetDate, r.runAtUtc, r.horizonDays ?? null, r.rawPrediction ?? null,
        r.finalPrediction, r.confidenceLower ?? null, r.confidenceUpper ?? null,
        r.trainedCount ?? null, r.totalCount ?? null, r.modelVersion || null);
    }
    db.exec('COMMIT');
  } catch (e) { db.exec('ROLLBACK'); throw e; }
  return list.length;
}

export function getStations() {
  return db.prepare(`SELECT * FROM stations WHERE enabled = 1 ORDER BY city`).all();
}

export function getLatestReports() {
  return db.prepare(`SELECT r.station_id, r.target_date, r.generated_at_utc, r.report_json, s.city, s.icao FROM reports r JOIN stations s ON s.id=r.station_id ORDER BY r.target_date, s.city`).all().map((row) => {
    const report = JSON.parse(row.report_json);
    // Flatten for frontend RunnerReport compatibility (camelCase)
    return { ...report, report, city: row.city, icao: row.icao, generated_at_utc: row.generated_at_utc, station_id: row.station_id, target_date: row.target_date };
  });
}

export function getReport(stationId, targetDate) {
  const row = db.prepare(`SELECT r.station_id, r.target_date, r.generated_at_utc, r.report_json, s.city, s.icao FROM reports r JOIN stations s ON s.id=r.station_id WHERE r.station_id = ? AND r.target_date = ?`).get(stationId, targetDate);
  if (!row) return null;
  const report = JSON.parse(row.report_json);
  return { ...report, report, city: row.city, icao: row.icao, generated_at_utc: row.generated_at_utc, station_id: row.station_id, target_date: row.target_date };
}

export function getSourceTimeline(stationId, limit = 120) {
  return db.prepare(`SELECT station_id, source, endpoint, requested_at, status_code, response_ms, ok, error FROM source_requests WHERE (? IS NULL OR station_id = ?) ORDER BY requested_at DESC LIMIT ?`).all(stationId || null, stationId || null, limit);
}

export function getForecastHistory(stationId, targetDate) {
  return db.prepare(`SELECT station_id, source, provider, run_at_utc, target_date, horizon_days, predicted_high_c, predicted_low_c FROM forecast_snapshots WHERE station_id = ? AND target_date = ? ORDER BY run_at_utc ASC, source ASC`).all(stationId, targetDate);
}

export function getSettlements(stationId, targetDate) {
  return db.prepare(`SELECT station_id, target_date, source, high_c, recorded_at_utc FROM settlements WHERE station_id = ? AND target_date = ?`).all(stationId, targetDate);
}

export function getActuals(stationId, targetDate) {
  return db.prepare(`SELECT station_id, target_date, source, observed_at_utc, actual_max_c, actual_min_c, actual_source FROM observations WHERE station_id = ? AND target_date = ? AND actual_max_c IS NOT NULL ORDER BY observed_at_utc DESC`).all(stationId, targetDate);
}

export function getAccuracyStatus() {
  const toleranceC = 0.5;
  const lastReport = db.prepare(`SELECT MAX(generated_at_utc) as lastGeneratedAt, COUNT(*) as reportCount FROM reports`).get();
  const lastSettlement = db.prepare(`SELECT MAX(recorded_at_utc) as lastSettlementAt, COUNT(DISTINCT station_id || '|' || target_date) as settlementDays FROM settlements`).get();
  const lastObservation = db.prepare(`SELECT MAX(observed_at_utc) as lastActualAt FROM observations WHERE actual_max_c IS NOT NULL`).get();
  const providerFile = (() => {
    try {
      const p = path.join(dataDir, 'reports', 'provider-accuracy.json');
      if (!fs.existsSync(p)) return null;
      const stat = fs.statSync(p);
      const json = JSON.parse(fs.readFileSync(p, 'utf8'));
      return { generatedAtUtc: json.generatedAtUtc || new Date(stat.mtimeMs).toISOString(), mtimeMs: stat.mtimeMs, evaluatedRows: json.evaluatedRows || 0 };
    } catch { return null; }
  })();
  const forecastScope = db.prepare(`SELECT COUNT(*) as cnt FROM forecast_snapshots WHERE horizon_days IN (0,1,2)`).get();
  return {
    lastReportGeneratedAt: lastReport?.lastGeneratedAt || null,
    reportCount: lastReport?.reportCount || 0,
    lastSettlementAt: lastSettlement?.lastSettlementAt || null,
    settlementDays: lastSettlement?.settlementDays || 0,
    lastActualAt: lastObservation?.lastActualAt || null,
    providerAccuracyGeneratedAt: providerFile?.generatedAtUtc || null,
    providerAccuracyEvaluatedRows: providerFile?.evaluatedRows || 0,
    forecastRowsInScope: forecastScope?.cnt || 0,
    toleranceC,
  };
}

export function getAccuracyTrends(granularity = 'daily', limit = 14) {
  const toleranceC = 0.5;
  const rows = db.prepare(`
    SELECT fs.target_date as targetDate, fs.station_id as stationId, s.city as city,
           fs.source as source, fs.provider as provider,
           fs.predicted_high_c as predictedHighC,
           COALESCE((SELECT actual_max_c FROM observations o WHERE o.station_id = fs.station_id AND o.target_date = fs.target_date AND o.actual_max_c IS NOT NULL ORDER BY o.observed_at_utc DESC LIMIT 1),
                    (SELECT high_c FROM settlements se WHERE se.station_id = fs.station_id AND se.target_date = fs.target_date ORDER BY se.recorded_at_utc DESC LIMIT 1)) as referenceHighC
    FROM forecast_snapshots fs
    JOIN stations s ON s.id = fs.station_id
    WHERE fs.horizon_days IN (0,1,2)
  `).all();
  const byDay = new Map();
  for (const r of rows) {
    if (r.referenceHighC === null || r.referenceHighC === undefined) continue;
    const pred = Number(r.predictedHighC);
    const ref = Number(r.referenceHighC);
    if (!Number.isFinite(pred) || !Number.isFinite(ref)) continue;
    const isMatch = Math.abs(pred - ref) <= toleranceC;
    const day = r.targetDate;
    const entry = byDay.get(day) || { date: day, evaluations: 0, matches: 0 };
    entry.evaluations += 1;
    if (isMatch) entry.matches += 1;
    byDay.set(day, entry);
  }
  const sorted = [...byDay.values()].sort((a, b) => a.date.localeCompare(b.date));
  const daily = sorted.map((d) => ({ date: d.date, evaluations: d.evaluations, matches: d.matches, matchRate: d.evaluations ? d.matches / d.evaluations : 0 }));
  if (granularity === 'weekly') {
    const byWeek = new Map();
    for (const d of daily) {
      const dt = new Date(d.date + 'T12:00:00Z');
      const jan1 = new Date(Date.UTC(dt.getUTCFullYear(), 0, 1));
      const week = Math.ceil((((dt - jan1) / 86400000) + jan1.getUTCDay() + 1) / 7);
      const key = `${dt.getUTCFullYear()}-W${String(week).padStart(2, '0')}`;
      const w = byWeek.get(key) || { date: key, weekStart: d.date, evaluations: 0, matches: 0 };
      w.evaluations += d.evaluations;
      w.matches += d.matches;
      w.weekStart = w.weekStart < d.date ? w.weekStart : d.date;
      byWeek.set(key, w);
    }
    const weekly = [...byWeek.values()].sort((a, b) => a.weekStart.localeCompare(b.weekStart)).map((w) => ({ date: w.date, evaluations: w.evaluations, matches: w.matches, matchRate: w.evaluations ? w.matches / w.evaluations : 0 }));
    return weekly.slice(-limit);
  }
  return daily.slice(-limit);
}

export function upsertTrainingPairs(rows) {
  const stmt = db.prepare(`INSERT INTO training_pairs (station_id, provider, horizon_days, target_date, predicted_high_c, actual_high_c, actual_source) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(station_id, provider, horizon_days, target_date) DO UPDATE SET predicted_high_c=excluded.predicted_high_c, actual_high_c=excluded.actual_high_c, actual_source=excluded.actual_source, created_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')`);
  for (const r of rows) stmt.run(r.station_id, r.provider, r.horizon_days, r.target_date, r.predicted_high_c, r.actual_high_c, r.actual_source);
}

export function getTrainingPairs(limit = 5000) {
  return db.prepare(`SELECT * FROM training_pairs ORDER BY target_date DESC LIMIT ?`).all(limit);
}

export function upsertBiasParams(rows) {
  const stmt = db.prepare(`INSERT INTO bias_params (station_id, provider, horizon_days, n, bias_c, bias_shrunk_c, a, b, mae_c, rmse_c, hit_rate, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(station_id, provider, horizon_days) DO UPDATE SET n=excluded.n, bias_c=excluded.bias_c, bias_shrunk_c=excluded.bias_shrunk_c, a=excluded.a, b=excluded.b, mae_c=excluded.mae_c, rmse_c=excluded.rmse_c, hit_rate=excluded.hit_rate, updated_at=excluded.updated_at`);
  for (const r of rows) stmt.run(r.station_id, r.provider, r.horizon_days, r.n, r.bias_c, r.bias_shrunk_c, r.a, r.b, r.mae_c, r.rmse_c, r.hit_rate, r.updated_at);
}

// Artık veri üretmeyen (ölü) akışların bias satırlarını siler. `train_bias_online.mjs` her koşuda çağırır:
// akış gidince o hücreler anlamsız kalır ve geçmiş skorlamada aynı modeli iki kez oylatır. Akış geri
// gelirse satırlar kendiliğinden yeniden üretilir. Döner: silinen satır sayısı.
export function deleteBiasParamsForProviders(providers) {
  const list = (providers || []).map(String).filter(Boolean);
  if (!list.length) return 0;
  const stmt = db.prepare(`DELETE FROM bias_params WHERE provider IN (${list.map(() => '?').join(',')})`);
  return stmt.run(...list).changes;
}

export function getBiasParams(provider, horizonDays) {
  if (provider) return db.prepare(`SELECT * FROM bias_params WHERE provider=? AND (? IS NULL OR horizon_days=?) ORDER BY station_id`).all(provider, horizonDays ?? null);
  return db.prepare(`SELECT * FROM bias_params ORDER BY provider, horizon_days, station_id`).all();
}

export function getBiasFor(stationId, provider, horizonDays) {
  return db.prepare(`SELECT * FROM bias_params WHERE station_id=? AND provider=? AND horizon_days=?`).get(stationId, provider, horizonDays);
}

export function upsertClimatology(rows) {
  const stmt = db.prepare(`INSERT INTO climatology (station_id, doy, mean_c, std_c, p5_c, p95_c) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(station_id, doy) DO UPDATE SET mean_c=excluded.mean_c, std_c=excluded.std_c, p5_c=excluded.p5_c, p95_c=excluded.p95_c`);
  for (const r of rows) stmt.run(r.station_id, r.doy, r.mean_c, r.std_c, r.p5_c, r.p95_c);
}

export function getClimatology(stationId, doy) {
  return db.prepare(`SELECT * FROM climatology WHERE station_id=? AND doy=?`).get(stationId, doy);
}

export function insertEvaluationDaily(rows) {
  const stmt = db.prepare(`INSERT INTO evaluation_daily (evaluated_at, horizon_days, ensemble_type, provider, mae, rmse, hit_rate, n) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(evaluated_at, horizon_days, ensemble_type, provider) DO UPDATE SET mae=excluded.mae, rmse=excluded.rmse, hit_rate=excluded.hit_rate, n=excluded.n`);
  for (const r of rows) stmt.run(r.evaluated_at, r.horizon_days, r.ensemble_type, r.provider, r.mae, r.rmse, r.hit_rate, r.n);
}

export function getEvaluationDaily(limit = 100) {
  return db.prepare(`SELECT * FROM evaluation_daily ORDER BY evaluated_at DESC LIMIT ?`).all(limit);
}

export function insertModelVersion(row) {
  db.prepare(`INSERT INTO model_versions (model_name, horizon_days, version, trained_at, train_rows, val_mae, params_json, artifact_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(model_name, version) DO UPDATE SET trained_at=excluded.trained_at, train_rows=excluded.train_rows, val_mae=excluded.val_mae, params_json=excluded.params_json, artifact_path=excluded.artifact_path`).run(row.model_name, row.horizon_days, row.version, row.trained_at, row.train_rows, row.val_mae, row.params_json, row.artifact_path);
}

export function getCityProviderBreakdown() {
  const toleranceC = 0.5;
  const rows = db.prepare(`
    SELECT fs.station_id as stationId, s.city as city, s.icao as icao,
            fs.source as source, fs.provider as provider,
            fs.predicted_high_c as predictedHighC,
            COALESCE((SELECT actual_max_c FROM observations o WHERE o.station_id = fs.station_id AND o.target_date = fs.target_date AND o.actual_max_c IS NOT NULL ORDER BY o.observed_at_utc DESC LIMIT 1),
                     (SELECT high_c FROM settlements se WHERE se.station_id = fs.station_id AND se.target_date = fs.target_date ORDER BY se.recorded_at_utc DESC LIMIT 1)) as referenceHighC,
            fs.target_date as targetDate
    FROM forecast_snapshots fs
    JOIN stations s ON s.id = fs.station_id
    WHERE fs.horizon_days IN (0,1,2)
  `).all();
  const map = new Map();
  for (const r of rows) {
    if (r.referenceHighC === null || r.referenceHighC === undefined) continue;
    const pred = Number(r.predictedHighC);
    const ref = Number(r.referenceHighC);
    if (!Number.isFinite(pred) || !Number.isFinite(ref)) continue;
    const key = `${r.stationId}|${r.source}|${r.provider}`;
    const entry = map.get(key) || { stationId: r.stationId, city: r.city, icao: r.icao, source: r.source, provider: r.provider, evaluations: 0, matches: 0, errors: [] };
    const delta = Math.abs(pred - ref);
    entry.evaluations += 1;
    if (delta <= toleranceC) entry.matches += 1;
    entry.errors.push(delta);
    map.set(key, entry);
  }
  return [...map.values()].map((e) => ({
    stationId: e.stationId,
    city: e.city,
    icao: e.icao,
    source: e.source,
    provider: e.provider,
    evaluations: e.evaluations,
    matches: e.matches,
    matchRate: e.evaluations ? e.matches / e.evaluations : 0,
    meanAbsoluteErrorC: e.errors.length ? Number((e.errors.reduce((a, b) => a + b, 0) / e.errors.length).toFixed(3)) : null,
  })).sort((a, b) => b.matchRate - a.matchRate || b.evaluations - a.evaluations);
}
