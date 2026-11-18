import fs from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

const dataDir = process.env.TEMP_MARKET_DATA_DIR || path.resolve('data');
const dbPath = process.env.TEMP_MARKET_DB || path.join(dataDir, 'tempmarket-edge.sqlite');
const reportDir = path.join(dataDir, 'reports');
fs.mkdirSync(reportDir, { recursive: true });
const toleranceC = 0.5;
const db = new DatabaseSync(dbPath);

const stations = db.prepare('SELECT id, icao, city FROM stations WHERE enabled = 1 ORDER BY city, icao').all();
const forecasts = db.prepare(`
  SELECT station_id, source, provider, run_at_utc, target_date, horizon_days, predicted_high_c
  FROM forecast_snapshots
  WHERE horizon_days IN (0, 1, 2)
  ORDER BY target_date, station_id, run_at_utc
`).all();
const actuals = db.prepare(`
  SELECT station_id, target_date, MAX(actual_max_c) AS actual_max_c, MAX(actual_source) AS actual_source
  FROM observations
  WHERE actual_max_c IS NOT NULL
  GROUP BY station_id, target_date
`).all();
const settlements = db.prepare(`
  SELECT station_id, target_date, MAX(high_c) AS high_c, GROUP_CONCAT(DISTINCT source) AS sources
  FROM settlements
  GROUP BY station_id, target_date
`).all();

const stationMap = new Map(stations.map((row) => [row.id, row]));
const actualMap = new Map(actuals.map((row) => [`${row.station_id}|${row.target_date}`, { valueC: Number(row.actual_max_c), source: row.actual_source || 'IEM actual' }]));
const settlementMap = new Map(settlements.map((row) => [`${row.station_id}|${row.target_date}`, { valueC: Number(row.high_c), source: row.sources || 'Polymarket settlement' }]));
for (const [key, reference] of settlementMap) {
  if (!actualMap.has(key)) actualMap.set(key, reference);
}

const provider = new Map();
const horizon = new Map();
const matches = [];
const polymarketComparison = [];
for (const row of forecasts) {
  const reference = actualMap.get(`${row.station_id}|${row.target_date}`);
  if (!reference || !Number.isFinite(Number(row.predicted_high_c))) continue;
  const predictedC = Number(row.predicted_high_c);
  const deltaC = Math.abs(predictedC - reference.valueC);
  const isMatch = deltaC <= toleranceC;
  const providerKey = `${row.source}|${row.provider}`;
  const horizonKey = `${providerKey}|t${row.horizon_days}`;
  for (const [key, bucket] of [[providerKey, provider], [horizonKey, horizon]]) {
    const item = bucket.get(key) || { source: row.source, provider: row.provider, horizon: bucket === horizon ? row.horizon_days : null, evaluations: 0, matches: 0, matchRate: null, meanAbsoluteErrorC: null, errors: [] };
    item.evaluations += 1;
    if (isMatch) item.matches += 1;
    item.errors.push(deltaC);
    item.meanAbsoluteErrorC = Number((item.errors.reduce((sum, value) => sum + value, 0) / item.errors.length).toFixed(3));
    item.matchRate = Number((item.matches / item.evaluations).toFixed(4));
    bucket.set(key, item);
  }
  const result = { stationId: row.station_id, city: stationMap.get(row.station_id)?.city || row.station_id, source: row.source, provider: row.provider, runAtUtc: row.run_at_utc, targetDate: row.target_date, horizonDays: row.horizon_days, predictedHighC: predictedC, referenceHighC: reference.valueC, referenceSource: reference.source, absoluteErrorC: Number(deltaC.toFixed(3)), isMatch };
  matches.push(result);
  const settlementReference = settlementMap.get(`${row.station_id}|${row.target_date}`);
  if (settlementReference) polymarketComparison.push({ ...result, referenceHighC: settlementReference.valueC, referenceSource: settlementReference.source, absoluteErrorC: Number(Math.abs(predictedC - settlementReference.valueC).toFixed(3)), isMatch: Math.abs(predictedC - settlementReference.valueC) <= toleranceC });
}
for (const bucket of [provider, horizon]) for (const item of bucket.values()) delete item.errors;

const output = {
  generatedAtUtc: new Date().toISOString(),
  database: dbPath,
  toleranceC,
  enabledStations: stations.length,
  forecastRowsInScope: forecasts.length,
  referenceDays: actualMap.size,
  settlementDays: settlements.length,
  evaluatedRows: matches.length,
  providerAccuracy: [...provider.values()].sort((a, b) => a.source.localeCompare(b.source) || a.provider.localeCompare(b.provider)),
  horizonAccuracy: [...horizon.values()].sort((a, b) => a.source.localeCompare(b.source) || a.provider.localeCompare(b.provider) || a.horizon - b.horizon),
  matches,
  polymarketComparison,
};
fs.writeFileSync(path.join(reportDir, 'provider-accuracy.json'), JSON.stringify(output, null, 2) + '\n');
const lines = [
  '# Provider Accuracy Report',
  '',
  `Generated: ${output.generatedAtUtc}`,
  `Tolerance: ±${toleranceC}°C`,
  `Enabled stations: ${stations.length}`,
  `Settlement days: ${settlements.length}`,
  `Evaluated forecast rows: ${matches.length}`,
  '',
  '| Source | Provider | Evaluations | Matches | Match rate | Mean absolute error °C |',
  '|---|---|---:|---:|---:|---:|',
];
for (const item of output.providerAccuracy) lines.push(`| ${item.source} | ${item.provider} | ${item.evaluations} | ${item.matches} | ${(item.matchRate * 100).toFixed(1)}% | ${item.meanAbsoluteErrorC.toFixed(3)} |`);
lines.push('', '## Horizon breakdown', '', '| Source | Provider | Horizon | Evaluations | Matches | Match rate | Mean absolute error °C |', '|---|---|---:|---:|---:|---:|---:|');
for (const item of output.horizonAccuracy) lines.push(`| ${item.source} | ${item.provider} | t${item.horizon} | ${item.evaluations} | ${item.matches} | ${(item.matchRate * 100).toFixed(1)}% | ${item.meanAbsoluteErrorC.toFixed(3)} |`);
lines.push('', '## Settlement comparison', '', '| Station | Date | Source | Predicted °C | Reference °C | Error °C | Match |', '|---|---|---|---:|---:|---:|---|');
for (const row of output.polymarketComparison) lines.push(`| ${row.city} (${row.stationId}) | ${row.targetDate} | ${row.source} | ${row.predictedHighC.toFixed(1)} | ${row.referenceHighC.toFixed(1)} | ${row.absoluteErrorC.toFixed(1)} | ${row.isMatch ? 'Yes' : 'No'} |`);
lines.push('', '> Accuracy is descriptive only. A provider with very few evaluated rows must not be interpreted as a stable performance estimate.');
fs.writeFileSync(path.join(reportDir, 'provider-accuracy.md'), lines.join('\n') + '\n');
console.log(JSON.stringify({ reportDir, enabledStations: stations.length, settlementDays: settlements.length, evaluatedRows: matches.length, providers: output.providerAccuracy }, null, 2));
db.close();
