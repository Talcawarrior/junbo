import fs from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

const mainDbPath = process.env.HEAT_MAIN_DB || 'C:\\HEAT data\\db\\tempmarket-edge.sqlite';
const db = new DatabaseSync(mainDbPath);

db.exec(`
  CREATE TABLE IF NOT EXISTS model_registry (
    model_id TEXT PRIMARY KEY,
    model_type TEXT NOT NULL,
    version TEXT,
    trained_at TEXT NOT NULL,
    metrics_json TEXT,
    status TEXT NOT NULL DEFAULT 'challenger',
    mae REAL,
    file_path TEXT
  );

  CREATE TABLE IF NOT EXISTS kalman_state (
    station_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    bias_mean REAL NOT NULL DEFAULT 0,
    bias_var REAL NOT NULL DEFAULT 4.0,
    obs_var REAL NOT NULL DEFAULT 2.0,
    process_var REAL NOT NULL DEFAULT 0.1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (station_id, provider)
  );

  CREATE TABLE IF NOT EXISTS quantile_map (
    station_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    quantile_key TEXT NOT NULL,
    raw_value REAL NOT NULL,
    mapped_value REAL NOT NULL,
    n INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (station_id, provider, quantile_key)
  );

  CREATE TABLE IF NOT EXISTS ensemble_weights (
    station_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    inv_mae REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (station_id, provider)
  );

  CREATE TABLE IF NOT EXISTS station_meta (
    station_id TEXT PRIMARY KEY,
    uhi_factor REAL DEFAULT 0,
    city TEXT,
    country TEXT
  );

  CREATE TABLE IF NOT EXISTS prediction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    target_date TEXT NOT NULL,
    raw_prediction REAL,
    kalman_corrected REAL,
    quantile_corrected REAL,
    lgbm_corrected REAL,
    final_prediction REAL,
    confidence_lower REAL,
    confidence_upper REAL,
    model_version TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
  );
`);

function ensureColumn(table, column, definition) {
  const columns = db.prepare(`PRAGMA table_info(${table})`).all();
  if (!columns.some((item) => item.name === column)) db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${definition}`);
}

try { ensureColumn('forecast_snapshots', 'bias_lag1', 'REAL'); } catch {}
try { ensureColumn('forecast_snapshots', 'bias_lag3', 'REAL'); } catch {}
try { ensureColumn('forecast_snapshots', 'bias_lag7', 'REAL'); } catch {}
try { ensureColumn('forecast_snapshots', 'bias_std7', 'REAL'); } catch {}
try { ensureColumn('forecast_snapshots', 'month_sin', 'REAL'); } catch {}
try { ensureColumn('forecast_snapshots', 'month_cos', 'REAL'); } catch {}
try { ensureColumn('forecast_snapshots', 'day_of_year', 'REAL'); } catch {}

export function getDriftStatus() {
  // models dizini MAIN DB'nin ustundeki klasorde: C:\HEAT data\db\... -> D:\HEAT data
  const dataDir = path.resolve(path.dirname(mainDbPath), '..');
  const statusPath = path.join(dataDir, 'models/lgbm/drift_status.json');
  if (!fs.existsSync(statusPath)) return { status: 'unknown', drift_ratio: 1.0, recent_mae: 0, older_mae: 0 };
  try {
    return JSON.parse(fs.readFileSync(statusPath, 'utf8'));
  } catch {
    return { status: 'error', drift_ratio: 1.0, recent_mae: 0, older_mae: 0 };
  }
}

export function getModelRegistry() {
  return db.prepare('SELECT model_id, model_type, version, trained_at, metrics_json, status, mae, file_path FROM model_registry ORDER BY trained_at DESC').all();
}

export function getEnsembleWeights() {
  return db.prepare('SELECT station_id, provider, weight, inv_mae FROM ensemble_weights ORDER BY weight DESC').all();
}

export function getKalmanStates() {
  return db.prepare('SELECT station_id, provider, bias_mean, bias_var FROM kalman_state ORDER BY bias_mean DESC').all();
}

export function logPrediction(input) {
  db.prepare(`INSERT INTO prediction_log (station_id, target_date, raw_prediction, kalman_corrected, quantile_corrected, lgbm_corrected, final_prediction, confidence_lower, confidence_upper, model_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
    .run(input.stationId, input.targetDate, input.rawPrediction, input.kalmanCorrected, input.quantileCorrected, input.lgbmCorrected, input.finalPrediction, input.confidenceLower, input.confidenceUpper, input.modelVersion);
}

export function getPredictionLog(stationId, targetDate, limit = 10) {
  return db.prepare('SELECT * FROM prediction_log WHERE station_id=? AND target_date=? ORDER BY created_at DESC LIMIT ?').all(stationId, targetDate, limit);
}

export function getStationsMeta() {
  return db.prepare('SELECT station_id, uhi_factor, city, country FROM station_meta').all();
}

export function getQuantileMaps() {
  return db.prepare('SELECT station_id, provider, quantile_key, raw_value, mapped_value, n FROM quantile_map WHERE quantile_key = ? ORDER BY raw_value', '0.5').all();
}
