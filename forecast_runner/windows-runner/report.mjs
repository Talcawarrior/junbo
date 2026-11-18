import { getActuals, getBiasFor, getForecastHistory, getSettlements, writeReport } from './db.mjs';

const COMPATIBILITY_TOLERANCE_C = 0.5;
// MOS (Model Output Statistics): bias_params hücresi (şehir×kaynak×ufuk) eğitilmişse o düzeltmeyi uygula.
// Yalnız n>=MOS_MIN_N hücreye güven (OLS a·x+b varsa; yoksa tahmin−bias_shrunk). Naif ortalama en kötüydü (P1),
// bu yüzden düzeltilmiş-ensemble AYRI alan olarak üretilir; mevcut meanForecastC bozulmaz.
// GÜVEN KURALI: <MOS_TRUST_MIN eğitimli kaynak varsa MOS sonucu güvenilir değildir → buildReport mosForecastC'yi
// null (ya da tek kaynaktaysa mevcut naif) bırakır. Canlı hücreler n≈2-6 iken MOS'u zorlamak naifi bile geçemiyordu;
// hücreler ~10+ settled çifte ulaşınca düzeltme otomatik devreye girer (rapor_mos_deney.md'nin üretim karşılığı).
const MOS_MIN_N = 10;
const MOS_TRUST_MIN = 2;
const MOS_RMSE_FLOOR = 0.3;

// Bir hedef için her kaynağın EN TAZE tahminini (en geç run) seç; bias_params'taki hücreyle düzelt;
// yalnız EĞİTİMLİ kaynaklar rmse-skill ile konsensüse girer. Sonuç güven eşiğini buildReport kararlaştırır.
export function computeMosEnsemble(stationId, forecasts) {
  const freshest = new Map(); // provider -> { valueC, horizonDays }
  for (const forecast of forecasts) {
    if (!Number.isFinite(forecast.predicted_high_c) || forecast.predicted_high_c < -40 || forecast.predicted_high_c > 60) continue; // çöp tahmini ele
    const cur = freshest.get(forecast.provider);
    if (!cur || forecast.run_at_utc > cur.runAtUtc || (forecast.run_at_utc === cur.runAtUtc && forecast.horizon_days < cur.horizonDays)) {
      freshest.set(forecast.provider, { valueC: forecast.predicted_high_c, horizonDays: forecast.horizon_days, runAtUtc: forecast.run_at_utc });
    }
  }
  const providers = [];
  let trainedWeightSum = 0;
  let correctedWeightedSum = 0;
  let trainedCount = 0;
  for (const [provider, f] of freshest) {
    const bias = getBiasFor(stationId, provider, f.horizonDays);
    const trained = !!bias && Number(bias.n) >= MOS_MIN_N;
    let correctedC = f.valueC;
    let rmse = 2.5;
    if (trained) {
      rmse = Number.isFinite(Number(bias.rmse_c)) ? Number(bias.rmse_c) : 2.5;
      if (bias.a != null && Number.isFinite(Number(bias.a))) correctedC = Number(bias.a) * f.valueC + Number(bias.b);
      else correctedC = f.valueC - Number(bias.bias_shrunk_c);
      trainedCount += 1;
      const weight = 1 / Math.max(MOS_RMSE_FLOOR, rmse) ** 2;
      trainedWeightSum += weight;
      correctedWeightedSum += weight * correctedC;
    }
    providers.push({
      provider,
      horizonDays: f.horizonDays,
      rawC: round(f.valueC),
      correctedC: trained ? round(correctedC) : round(f.valueC),
      trained,
      biasN: bias ? Number(bias.n) : 0,
      rmseC: trained ? round(rmse) : null,
    });
  }
  if (!providers.length) return null;
  return {
    minN: MOS_MIN_N,
    trainedProviderCount: trainedCount,
    totalProviderCount: providers.length,
    weightedCorrectedC: trainedWeightSum ? round(correctedWeightedSum / trainedWeightSum) : null,
    providers,
  };
}

function round(value) {
  return Math.round(value * 10) / 10;
}

export function buildReport(stationId, targetDate) {
  const forecasts = getForecastHistory(stationId, targetDate);
  const settlements = getSettlements(stationId, targetDate);
  const actuals = getActuals(stationId, targetDate);
  const byRun = new Map();
  for (const forecast of forecasts) {
    const run = byRun.get(forecast.run_at_utc) || { runAtUtc: forecast.run_at_utc, horizonDays: forecast.horizon_days, values: [] };
    run.values.push({ source: forecast.source, provider: forecast.provider, valueC: forecast.predicted_high_c, lowC: forecast.predicted_low_c ?? null });
    byRun.set(forecast.run_at_utc, run);
  }
  const runs = [...byRun.values()].sort((a, b) => a.runAtUtc.localeCompare(b.runAtUtc));
  const values = forecasts.map((item) => item.predicted_high_c).filter(Number.isFinite);
  const meanForecastC = values.length ? round(values.reduce((sum, value) => sum + value, 0) / values.length) : null;
  const minForecastC = values.length ? Math.min(...values) : null;
  const maxForecastC = values.length ? Math.max(...values) : null;
  const settlementHighC = settlements.length ? settlements[settlements.length - 1].high_c : null;
  const actualMaxC = actuals.length ? actuals[0].actual_max_c : settlementHighC;
  const actualMinC = actuals.length ? actuals[0].actual_min_c : null;
  const actualSource = actuals.length ? actuals[0].actual_source || actuals[0].source : settlements.length ? settlements[settlements.length - 1].source : null;
  const referenceHighC = actualMaxC;
  const coverageMap = new Map();
  for (const forecast of forecasts) {
    const key = `${forecast.source}|${forecast.provider}`;
    const coverage = coverageMap.get(key) || { source: forecast.source, provider: forecast.provider, runCount: 0, targetDates: new Set(), horizonDays: new Set() };
    coverage.runCount += 1;
    coverage.targetDates.add(forecast.target_date);
    coverage.horizonDays.add(forecast.horizon_days);
    coverageMap.set(key, coverage);
  }
  const providerMatches = forecasts.map((forecast) => ({
    source: forecast.source,
    provider: forecast.provider,
    runAtUtc: forecast.run_at_utc,
    horizonDays: forecast.horizon_days,
    predictedMaxC: round(forecast.predicted_high_c),
    actualMaxC: referenceHighC === null ? null : round(referenceHighC),
    actualSource,
    isMatch: referenceHighC === null ? null : Math.abs(forecast.predicted_high_c - referenceHighC) <= COMPATIBILITY_TOLERANCE_C,
  }));
  const compatible = referenceHighC === null || meanForecastC === null ? null : Math.abs(meanForecastC - referenceHighC) <= COMPATIBILITY_TOLERANCE_C;
  // MOS düzeltmesi yalnız güvenilir kaynak sayısı eşikteyse anlamlı. Aksi halde üretimdeki en iyi mevcut değer olan
  // meanForecastC korunur (mosForecastC=null veya tek kaynaktaysa naif) — kötüleştirmez, eğitim olgunlaşınca açar.
  const mos = computeMosEnsemble(stationId, forecasts);
  const mosTrainedCount = mos?.trainedProviderCount ?? 0;
  const mosTrusted = mosTrainedCount >= MOS_TRUST_MIN;
  const mosForecastC = mos ? (mosTrusted ? mos.weightedCorrectedC : (mosTrainedCount === 1 ? meanForecastC : null)) : null;
  const report = {
    mosForecastC,
    mosTrusted,
    mosCorrectedProviderCount: mosTrainedCount,
    mosTotalProviderCount: mos?.totalProviderCount ?? 0,
    mosMinN: MOS_MIN_N,
    mos,
    stationId,
    targetDate,
    generatedAtUtc: new Date().toISOString(),
    forecastRunCount: runs.length,
    providerObservationCount: forecasts.length,
    overlap: runs.length >= 2,
    firstRunAtUtc: runs[0]?.runAtUtc || null,
    latestRunAtUtc: runs.at(-1)?.runAtUtc || null,
    firstRunHorizonDays: runs[0]?.horizonDays ?? null,
    latestRunHorizonDays: runs.at(-1)?.horizonDays ?? null,
    minForecastC: minForecastC === null ? null : round(minForecastC),
    maxForecastC: maxForecastC === null ? null : round(maxForecastC),
    meanForecastC,
    actualMaxC: actualMaxC === null ? null : round(actualMaxC),
    actualMinC: actualMinC === null ? null : round(actualMinC),
    actualSource,
    isMatch: compatible,
    settlementHighC,
    settlementSources: settlements.map((item) => item.source),
    sourceCoverage: [...coverageMap.values()].map((coverage) => ({ source: coverage.source, provider: coverage.provider, runCount: coverage.runCount, targetDates: [...coverage.targetDates], horizonDays: [...coverage.horizonDays].sort((a, b) => a - b) })),
    providerMatches,
    wuCompatible: compatible,
    compatibilityToleranceC: COMPATIBILITY_TOLERANCE_C,
    runs: runs.map((run) => ({ ...run, meanC: round(run.values.reduce((sum, item) => sum + item.valueC, 0) / run.values.length) })),
  };
  writeReport(stationId, targetDate, report);
  return report;
}

export function rebuildReports(stations, targetDates) {
  return stations.flatMap((station) => targetDates.map((targetDate) => buildReport(station.id, targetDate)));
}
