import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);
import { closeDb, getAccuracyStatus, getAccuracyTrends, getBiasParams, getCityProviderBreakdown, getEvaluationDaily, getLatestReports, getReport, getSourceTimeline, getStations, insertSettlement, logPredictions, seedStations } from './db.mjs';
import { collectAviationMetar, collectIemActuals, collectNwsForecast, collectOpenMeteoForecast, collectOpenWeatherForecast, collectPolymarketRules, collectPolymarketSettlement, collectVisualCrossingForecast, collectWeatherApiForecast, collectWeatherNextEnsemble, discoverPolymarketTemperatureEvents } from './sources.mjs';
import { buildReport } from './report.mjs';
import { omOnbellekBuda } from './om_cache.mjs';
import { isDue, markDone, readGateState, writeGateState } from './gates.mjs';
import { getDriftStatus, getModelRegistry, getEnsembleWeights, getKalmanStates, getStationsMeta, getQuantileMaps } from './mlops.mjs';

const configPath = process.env.TEMP_MARKET_CONFIG || path.resolve('windows-runner/config.json');
const fallbackConfigPath = path.resolve('windows-runner/config.example.json');
let config = JSON.parse(fs.readFileSync(fs.existsSync(configPath) ? configPath : fallbackConfigPath, 'utf8'));
const intervalMinutes = Math.max(5, Number(config.intervalMinutes || 60));
const forecastDays = Math.max(1, Math.min(7, Number(config.forecastDays || 3)));
const userAgent = config.userAgent || 'TempMarketEdge/1.0 (local-monitor)';
const apiPort = Number(process.env.TEMP_MARKET_API_PORT || config.apiPort || 8787);
const settlementInbox = process.env.TEMP_MARKET_SETTLEMENT_INBOX || path.resolve('data/inbox/settlements.jsonl');

// File log: the hidden scheduled task discards stdout/stderr, which made the 2026-09-06 crash
// invisible. Tee console output to windows-runner/logs/runner.log (plain UTF-8, single file with
// timestamps) so any later crash is diagnosable.
const logDir = path.resolve('windows-runner/logs');
fs.mkdirSync(logDir, { recursive: true });
const logPath = path.join(logDir, 'runner.log');
const logStream = fs.createWriteStream(logPath, { flags: 'a', encoding: 'utf8' });
function fmtArgs(args) {
  return args.map((x) => typeof x === 'string' ? x : (x instanceof Error ? (x.stack || x.message || String(x)) : JSON.stringify(x))).join(' ');
}
const _log = console.log; const _err = console.error;
console.log = (...args) => { const line = `[${new Date().toISOString()}] ${fmtArgs(args)}`; try { logStream.write(line + '\n'); } catch {} _log(...args); };
console.error = (...args) => { const line = `[${new Date().toISOString()}] ERR ${fmtArgs(args)}`; try { logStream.write(line + '\n'); } catch {} _err(...args); };
process.on('exit', () => { try { logStream.end(); } catch {} });

// Boot: if station seeding fails (e.g. DB momentarily locked) log instead of crashing; cycles retry later.
try {
  seedStations(config.stations);
} catch (error) {
  console.error('[runner] seedStations at boot failed (continuing):', error);
}

// config.json'u her döngü başında yeniden oku (Kural 13, 2026-09-15). NEDEN: `_sehir_cozucu.mjs --uygula`
// yeni bir istasyonu hem `stations` tablosuna hem config.json'a yazar, ama runner config'i YALNIZ açılışta
// okuyordu — yeni şehir bir sonraki restart'a kadar toplanmazdı ve "otomatik ekleme" yarım kalırdı.
// `seedStations` yalnız istasyon KÜNYESİ değişince çağrılır: her döngüde onlarca satır upsert etmek
// gereksiz yazma olurdu. Okuma başarısız olursa eski config korunur — runner durmaz.
const configImza = (list) => list.map((x) => `${x.id}:${x.icao}`).join(',');
function configTazele() {
  try {
    const taze = JSON.parse(fs.readFileSync(fs.existsSync(configPath) ? configPath : fallbackConfigPath, 'utf8'));
    if (!Array.isArray(taze.stations) || !taze.stations.length) {
      console.error('[runner] config tazeleme atlandı: stations boş/bozuk — eski config korunuyor');
      return;
    }
    const degisti = configImza(taze.stations) !== configImza(config.stations);
    config = taze;
    if (degisti) {
      console.log(`[runner] config yeniden okundu: istasyon ${config.stations.length} (değişti) → seedStations`);
      try { seedStations(config.stations); } catch (error) { console.error('[runner] seedStations (tazeleme) başarısız:', error); }
    }
  } catch (error) {
    console.error('[runner] config okunamadı, eski config ile devam:', error instanceof Error ? error.message : error);
  }
}

function writeJson(response, status, body) {
  response.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'access-control-allow-origin': '*' });
  response.end(JSON.stringify(body));
}

function getQuery(url) {
  return new URL(url, `http://127.0.0.1:${apiPort}`).searchParams;
}

const api = http.createServer((request, response) => {
  const url = request.url || '/';
  if (request.method === 'GET' && url === '/health') return writeJson(response, 200, { ok: true, service: 'tempmarket-edge-runner', intervalMinutes, forecastDays, nowUtc: new Date().toISOString() });
  if (request.method === 'GET' && url === '/api/stations') return writeJson(response, 200, getStations());
  if (request.method === 'GET' && url === '/api/reports') return writeJson(response, 200, getLatestReports());
  if (request.method === 'GET' && url.startsWith('/api/report')) {
    const query = getQuery(url);
    const stationId = query.get('stationId');
    const targetDate = query.get('targetDate');
    if (!stationId || !targetDate) return writeJson(response, 400, { error: 'stationId and targetDate are required' });
    return writeJson(response, 200, getReport(stationId, targetDate) || { error: 'report-not-found' });
  }
  if (request.method === 'GET' && url.startsWith('/api/sources')) {
    const query = getQuery(url);
    return writeJson(response, 200, getSourceTimeline(query.get('stationId'), Number(query.get('limit') || 120)));
  }
  if (request.method === 'GET' && url === '/api/accuracy/status') {
    return writeJson(response, 200, getAccuracyStatus());
  }
  if (request.method === 'GET' && url.startsWith('/api/accuracy/trends')) {
    const query = getQuery(url);
    const granularity = query.get('granularity') === 'weekly' ? 'weekly' : 'daily';
    const limit = Math.max(1, Math.min(90, Number(query.get('limit') || (granularity === 'weekly' ? 12 : 14))));
    return writeJson(response, 200, getAccuracyTrends(granularity, limit));
  }
  if (request.method === 'GET' && url === '/api/accuracy/breakdown') {
    return writeJson(response, 200, getCityProviderBreakdown());
  }
  if (request.method === 'GET' && url.startsWith('/api/accuracy/bias')) {
    const q = getQuery(url);
    return writeJson(response, 200, getBiasParams(q.get('provider') || undefined, q.get('horizon') ? Number(q.get('horizon')) : undefined));
  }
  if (request.method === 'GET' && url.startsWith('/api/accuracy/evaluation')) {
    const q = getQuery(url);
    return writeJson(response, 200, getEvaluationDaily(Number(q.get('limit') || 100)));
  }
  if (request.method === 'POST' && url === '/api/accuracy/train') {
    // trigger online bias retrain (used by cron or manual)
    execFileAsync(process.execPath, [path.resolve('scripts/train_bias_online.mjs')], { cwd: path.resolve('.'), windowsHide: true, maxBuffer: 8*1024*1024 }).then(()=> console.log('[runner] train_bias_online done')).catch(e=> console.error('[runner] train failed', e.message));
    return writeJson(response, 202, { ok: true, job: 'train_bias_online' });
  }
  if (request.method === 'GET' && url === '/api/mlops/drift') {
    return writeJson(response, 200, getDriftStatus());
  }
  if (request.method === 'GET' && url === '/api/mlops/models') {
    return writeJson(response, 200, getModelRegistry());
  }
  if (request.method === 'GET' && url === '/api/mlops/ensemble') {
    return writeJson(response, 200, getEnsembleWeights());
  }
  if (request.method === 'GET' && url === '/api/mlops/kalman') {
    return writeJson(response, 200, getKalmanStates());
  }
  if (request.method === 'GET' && url === '/api/mlops/stations') {
    return writeJson(response, 200, getStationsMeta());
  }
  if (request.method === 'GET' && url === '/api/mlops/quantiles') {
    return writeJson(response, 200, getQuantileMaps());
  }
  return writeJson(response, 404, { error: 'not-found' });
});

async function withRetry(label, task, attempts = 3) {
  let lastError;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try { return await task(); } catch (error) {
      lastError = error;
      const msg = error instanceof Error ? error.message : String(error);
      if (msg.includes('429')) { console.error(`[runner] ${label} rate limited (429), skipping retries`); break; }
      console.error(`[runner] ${label} failed attempt ${attempt}/${attempts}:`, msg);
      if (attempt < attempts) await new Promise((resolve) => setTimeout(resolve, 1000 * 2 ** (attempt - 1)));
    }
  }
  throw lastError;
}

async function importSettlementInbox() {
  if (!fs.existsSync(settlementInbox)) return 0;
  const stationIds = new Set(config.stations.map((station) => station.id));
  const lines = fs.readFileSync(settlementInbox, 'utf8').split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  let imported = 0;
  for (const line of lines) {
    try {
      const item = JSON.parse(line);
      const highC = Number(item.highC);
      if (!stationIds.has(item.stationId) || !/^\d{4}-\d{2}-\d{2}$/.test(item.targetDate) || !Number.isFinite(highC) || !item.source) continue;
      if (insertSettlement({ stationId: item.stationId, targetDate: item.targetDate, source: item.source, highC, recordedAtUtc: item.recordedAtUtc || new Date().toISOString(), raw: item })) imported += 1;
    } catch (error) {
      console.error('[runner] settlement inbox line ignored:', error instanceof Error ? error.message : error);
    }
  }
  return imported;
}

async function discoverAndPersistPolymarketEvents() {
  const discovered = await withRetry('Polymarket dynamic discovery', () => discoverPolymarketTemperatureEvents(userAgent));
  const byStation = new Map(config.stations.map((station) => [String(station.id).toLowerCase(), station]));
  let added = 0;
  for (const event of discovered) {
    const station = byStation.get(String(event.stationId).toLowerCase());
    if (!station) continue;
    const slugs = Array.isArray(station.marketSlugs) ? station.marketSlugs : (station.marketSlug ? [station.marketSlug] : []);
    if (slugs.includes(event.marketSlug)) continue;
    station.marketSlugs = [...slugs, event.marketSlug];
    station.marketSlug = station.marketSlug || event.marketSlug;
    added += 1;
  }
  if (added > 0) {
    fs.writeFileSync(configPath, `${JSON.stringify(config, null, 2)}\n`, 'utf8');
    seedStations(config.stations);
  }
  console.log(`[runner] Polymarket dynamic discovery: events=${discovered.length}, newSlugs=${added}`);
  return { discovered: discovered.length, added };
}

async function refreshProviderAccuracyReport() {
  await execFileAsync(process.execPath, [path.resolve('windows-runner/generate-provider-accuracy-report.mjs')], { cwd: path.resolve('.'), windowsHide: true, maxBuffer: 8 * 1024 * 1024 });
  console.log('[runner] provider accuracy report refreshed after settlement change');
}

async function refreshBiasParams() {
  try {
    await execFileAsync(process.execPath, [path.resolve('scripts/train_bias_online.mjs')], { cwd: path.resolve('.'), windowsHide: true, maxBuffer: 8*1024*1024 });
    console.log('[runner] bias_params refreshed');
  } catch (e) { console.error('[runner] bias refresh failed', e.message); }
}

async function archiveCanliForecasts() {
  try {
    await execFileAsync(process.execPath, [path.resolve('scripts/_archive_canli_forecasts.mjs')], { cwd: path.resolve('.'), windowsHide: true, maxBuffer: 8 * 1024 * 1024 });
    console.log('[runner] canlı forecast arşivi (LIVE→MAIN) tamam');
  } catch (e) { console.error('[runner] canlı forecast arşivi başarısız', e.message); }
}

// Günde 1× (UTC 08:00): junbo/ASI "kardeş" verisini kendi aynamıza (kardesler.db) çoğalt. Artımlı/idempotent
// betik; kaynağa yalnız SELECT atar. Amaç: kardeş sistemler dursa/sıfırlansa veri Heat'te kalsın.
async function syncKardesler() {
  try {
    await execFileAsync(process.execPath, [path.resolve('scripts/_mirror_kardesler.mjs')], { cwd: path.resolve('.'), windowsHide: true, maxBuffer: 8 * 1024 * 1024 });
    console.log('[runner] kardesler aynası (junbo/ASI → kardesler.db) senkron tamam');
  } catch (e) { console.error('[runner] kardesler aynası başarısız', e.message); }
}

// Günde 1× (UTC 08:00, kardesler aynasından HEMEN SONRA): kardeş aynasındaki ÇÖZÜLMÜŞ Polymarket
// sıcaklık marketlerini (gerçek = settlement) MAIN'e taşır. Tahmin tarafının _archive_canli_forecasts.mjs
// ile MAIN'e akması gibi, gerçek tarafı da her gün akar. Neden: 2026-09-07'de settlement MAIN'e yalnız
// TEK SEFERLİK backfill ile yazılmıştı; köprü olmadığı için 09-06'da dondu ve 3 günlük tahmin
// skorlanamaz hale geldi (2026-09-11'de tespit + _settlement_bridge.mjs ile kapatıldı). Idempotent;
// kaynak salt-okunur; var olan (şehir, gün) asla üzerine yazılmaz.
async function bridgeSettlements() {
  try {
    await execFileAsync(process.execPath, [path.resolve('scripts/_settlement_bridge.mjs')], { cwd: path.resolve('.'), windowsHide: true, maxBuffer: 8 * 1024 * 1024 });
    console.log('[runner] settlement köprüsü (kardesler → MAIN) tamam');
  } catch (e) { console.error('[runner] settlement köprüsü başarısız', e.message); }
}

// Günde 1× (UTC 08:00, arşivden hemen sonra): isabet raporunu MAIN'den üret ki "kaynak×şehir
// doğruluk" dosyası elle koşulmadan her gün tazelensin. Rapor salt-okur (MAIN), yalnız markdown yazar;
// repo-kök rapor_iskabet_t02.md güncel kopya, data/rapor_arsivi/ altına tarihli kopya birikir (data/ envanter dışı).
async function regenerateIsabetReport() {
  try {
    await execFileAsync(process.execPath, [path.resolve('scripts/rapor_iskabet_t02.mjs')], { cwd: path.resolve('.'), windowsHide: true, maxBuffer: 16 * 1024 * 1024 });
    const src = path.resolve('rapor_iskabet_t02.md');
    if (fs.existsSync(src)) {
      const today = new Date().toISOString().slice(0, 10);
      const dir = path.resolve('data/rapor_arsivi');
      fs.mkdirSync(dir, { recursive: true });
      fs.copyFileSync(src, path.join(dir, `rapor_iskabet_${today}.md`));
    }
    console.log('[runner] isabet raporu (MAIN) üretildi + arşivlendi');
  } catch (e) { console.error('[runner] isabet raporu başarısız', e.message); }
}

// Günlük besleyiciler (ANAYASA Kural 10): derin arşiv ailelerini (OM arşiv 8 model t0, Open-Meteo önceki
// t1/t2, NOAA AI ileri) her gün MAIN'e işleyen tek orchestrator. Runner saatlik canlı akışı yapmaz — bu
// aileler ayrı betiklerle günlük beslenir (tek-seferlik backfill kalıntısıydı, bu yüzden kesiliyordu).
async function runGunlukBesleyiciler() {
  try {
    await execFileAsync(process.execPath, [path.resolve('scripts/_gunluk_besleyiciler.mjs')],
      { cwd: path.resolve('.'), windowsHide: true, maxBuffer: 16 * 1024 * 1024, timeout: 65 * 60 * 1000 });
    console.log('[runner] günlük besleyiciler (OM arşiv8 + OM önceki + NOAA AI) tamam');
  } catch (e) { console.error('[runner] günlük besleyiciler başarısız', e.message); }
}

// Yeni şehir keşfi (Kural 13, 2026-09-15): Polymarket'te yeni bir sıcaklık marketi çıktığında Heat onu
// SESSİZCE atlamasın. Gamma API'den marketleri çeker, her marketin ÇÖZÜM İSTASYONUNU metinden çıkarır
// (kardeşin city_code'una güvenilmez — MPTO→MPMG dersi) ve Heat'te karşılığı yoksa ekler/önerir.
// `--uygula` ARTIK GEÇİLİYOR: istasyon yalnız **5 kapının tamamı** geçilirse eklenir (çözüm kaynağı +
// ülke biliniyor · kardeş kodu çelişmiyor · 150 km içinde mevcut istasyon yok · kalıcı market ≥3 ·
// zaman dilimi/yükseklik alınabiliyor). Kapı geçmeyen aday EKLENMEZ, şüpheli listesine düşer.
// Ekleme öncesi `stations` tablosu otomatik yedeklenir (db/backups/stations_pre_ekleme_*.sqlite).
// Köprüden sonra koşar ki eşleşmeyen kod defteri (unmatched_cities.json) taze olsun.
async function kesfetSehirler() {
  try {
    await execFileAsync(process.execPath, [path.resolve('scripts/_sehir_cozucu.mjs'), '--kaynak=gamma', '--uygula'],
      { cwd: path.resolve('.'), windowsHide: true, maxBuffer: 8 * 1024 * 1024, timeout: 5 * 60 * 1000 });
    console.log('[runner] yeni şehir keşfi tamam → eklenen + şüpheli: C:/JUNBO data/state/sehir_onerileri.json');
  } catch (e) { console.error('[runner] yeni şehir keşfi başarısız', e.message); }
}

// Günlük bias raporu (2026-09-15): "bugün hangi kaynak hangi şehri tutturuyor, bias düzeltmesi ne kadar
// düzeltiyor". `rapor_iskabet_t02` ufuk×kaynak geneli verir ama kaynak×şehir matrisini ve düzeltmenin
// kazandırıp kazandırmadığını vermez. SIZINTISIZ: öğrenme penceresi testten kesin önce; `bias_params`
// KULLANILMAZ (o aynı pencereyi de görür). Köprüden SONRA koşar → en taze settlement'ı görür.
async function regenerateBiasGunluk() {
  try {
    await execFileAsync(process.execPath, [path.resolve('scripts/rapor_bias_gunluk.mjs')],
      { cwd: path.resolve('.'), windowsHide: true, maxBuffer: 16 * 1024 * 1024 });
    console.log('[runner] günlük bias raporu üretildi + arşivlendi → rapor_bias_gunluk.md');
  } catch (e) { console.error('[runner] günlük bias raporu başarısız', e.message); }
}

// "Heat kendi tahminini ne kadar tutturuyor?" (2026-09-15). Diğer raporlar KAYNAKLARI ölçer; bu rapor
// HEAT'İN NİHAİ tahminini (`mosForecastC`) gerçek settlement'la karşılaştırır — ufuk bazında. Tahmin defteri
// (`prediction_log`) üzerinden okur; defter her döngüde ayrı satır yazar, `reports` gibi ezmez.
// Settlement köprüsünden SONRA koşar → taze gerçeği görür.
async function regenerateHeatIsabet() {
  try {
    await execFileAsync(process.execPath, [path.resolve('scripts/rapor_heat_isabet.mjs')],
      { cwd: path.resolve('.'), windowsHide: true, maxBuffer: 16 * 1024 * 1024 });
    console.log('[runner] Heat isabet raporu üretildi + arşivlendi → rapor_heat_isabet.md');
  } catch (e) { console.error('[runner] Heat isabet raporu başarısız', e.message); }
}

// NİHAİ ÇIKTI (ANAYASA §0): "hangi kaynak hangi şehirde hangi ufukta KÂRLI?" — isabet + piyasa fiyatı
// birleşik "oynanır hücre" tablosu ve sızıntısız para backtesti. Fiyatı kardeş junbo'dan SALT-OKUNUR okur
// (Kural 11); junbo yoksa rapor yine üretilir, yalnız fiyat bölümleri boş kalır. Günde 1× (UTC 08:00).
async function regenerateOynanirTablo() {
  try {
    await execFileAsync(process.execPath, ['--experimental-sqlite', path.resolve('scripts/rapor_oynanir_tablo.mjs')],
      { cwd: path.resolve('.'), windowsHide: true, maxBuffer: 16 * 1024 * 1024 });
    console.log('[runner] Oynanır tablo üretildi + arşivlendi → rapor_oynanir_tablo.md');
  } catch (e) { console.error('[runner] Oynanır tablo başarısız', e.message); }
}

// Kayıt sürekliliği denetimi (ANAYASA Kural 10): her tahmin ailesinin son run/target tarihini beklentiyle
// karşılaştırır; STALE aileleri repo-kök tazelik_raporu.md'ye yazar ki kesinti insan aramasın. Günde 1× (UTC 08:00).
async function runTazelikDenetimi() {
  try {
    await execFileAsync(process.execPath, [path.resolve('scripts/_tazelik_denetimi.mjs')],
      { cwd: path.resolve('.'), windowsHide: true, maxBuffer: 4 * 1024 * 1024, env: { ...process.env, OUT: path.resolve('tazelik_raporu.md') } });
    console.log('[runner] tazelik denetimi (Kural 10) tamam → tazelik_raporu.md');
  } catch (e) { console.error('[runner] tazelik denetimi başarısız', e.message); }
}

// Günlük blok (UTC 08:00) — günde BİR kez çalıştırılır. Normal pencere 08:00'de; makine o saatte uykuda
// kaldıysa runner geç ayağa kalktığında (saat>=8 ve iz boşsa) aynı gün GECİKMELİ koşar, gün atlanmaz.
// İz = kalıcı dosya (D:\HEAT data) — runner yeniden başlasa da günde 1× garantisi. Besleyiciler idempotent.
const DAILY_BLOCK_STATE = process.env.HEAT_DAILY_BLOCK_STATE || 'C:/JUNBO data/state/last_daily_block_utc.txt';
let lastDailyBlockDate = '';
try { if (fs.existsSync(DAILY_BLOCK_STATE)) lastDailyBlockDate = fs.readFileSync(DAILY_BLOCK_STATE, 'utf8').trim(); } catch {}

// 2026-11-18: slot gates for hour-bound sources (see gates.mjs). Persisted next to the daily block state.
const GATE_STATE = process.env.HEAT_GATE_STATE || path.join(path.dirname(DAILY_BLOCK_STATE), 'gated_slots_utc.json');
const GATE_HOURS = { openMeteo: 6, weatherNext: 12, iemActuals: 24 };

async function runDailyBlock(reason) {
  console.log(`[runner] günlük blok (08:00) başlıyor — ${reason}`);
  await archiveCanliForecasts();
  await syncKardesler();
  await bridgeSettlements(); // Kural 12: gerçek (settlement) hattı da her gün MAIN'e aksın — denetimden ÖNCE
  await kesfetSehirler(); // Kural 13: yeni market/şehir sessizce atlanmasın — köprüden SONRA (defter taze olsun)
  await runGunlukBesleyiciler(); // Kural 10: OM arşiv8 + OM önceki + NOAA AI — rapor öncesi, en taze veri için
  await regenerateIsabetReport();
  await regenerateBiasGunluk(); // kaynak×şehir bias raporu — isabet raporuyla aynı taze MAIN üzerinden
  await regenerateHeatIsabet(); // bizim nihai tahminimiz gerçeği ne kadar tutuyor (ufuk bazında) — aynı taze MAIN
  await regenerateOynanirTablo(); // nihai çıktı: kârlılık/seçim tablosu + fiyatlı backtest (isabet + odds)
  // Ortak OM önbelleğini buda (yaş 14 gün + boyut tavanı 500 MB). Günlük bloğa bağlı çünkü bu blok
  // zaten "günde bir, uykudan sonra telafi ederek" koşuyor — ayrı bir zamanlayıcı gerekmez.
  try {
    const b = omOnbellekBuda();
    console.log(`[runner] ortak OM önbelleği budandı: yaştan ${b.yaslaSilinen}, boyuttan ${b.boyuttanSilinen} satır → ${b.kalanMb.toFixed(1)} MB`);
  } catch (e) {
    console.error('[runner] ortak OM önbelleği budanamadı:', e instanceof Error ? e.message : e);
  }
  await runTazelikDenetimi();
  lastDailyBlockDate = new Date().toISOString().slice(0, 10);
  try { fs.mkdirSync(path.dirname(DAILY_BLOCK_STATE), { recursive: true }); fs.writeFileSync(DAILY_BLOCK_STATE, lastDailyBlockDate, 'utf8'); } catch {}
  console.log(`[runner] günlük blok tamam (${lastDailyBlockDate})`);
}

// Visual Crossing kotası günde 1000 istek; çekim aralığını bu zaman damgası yönetir (bkz. runCycle).
// 0 ile başlar → süreç her ayağa kalktığında ilk tur VC'yi tazeler; sonra 2 saatte bir.
let sonVcCekimi = 0;

async function runCycle() {
  configTazele();
  const runAtUtc = new Date().toISOString();
  const cycleLog = { started: runAtUtc, sources: {}, errors: [] };
  let settlementsChanged = (await importSettlementInbox()) > 0;
  console.log(`[runner] cycle started ${runAtUtc}`);
  // --- KAYNAK KAPILARI (2026-09-20 düzeltmesi) --------------------------------------------------
  // 2026-11-18: hour-modulo gates (utcHour % N === 0) replaced by UTC slot gates (gates.mjs).
  // A step is due until it succeeds once inside its slot; FORCE env vars still override.
  const runAtMs = new Date(runAtUtc).getTime();
  const gateState = readGateState(GATE_STATE);
  const omDue = isDue(gateState, 'openMeteo', runAtMs, GATE_HOURS.openMeteo) || process.env.TEMP_MARKET_FORCE_OPEN_METEO === '1';
  const wnDue = isDue(gateState, 'weatherNext', runAtMs, GATE_HOURS.weatherNext) || process.env.TEMP_MARKET_FORCE_WEATHERNEXT === '1';
  const iemDue = isDue(gateState, 'iemActuals', runAtMs, GATE_HOURS.iemActuals) || process.env.TEMP_MARKET_FORCE_HISTORY === '1';
  const gateOk = { openMeteo: 0, weatherNext: 0, iemActuals: 0 };
  // VISUAL CROSSING — saatlik çekim kotayı gün bitmeden tüketiyordu.
  // Ölçüm (09-14/15/16): 1050/1150/1152 istek → ÜÇÜNDE DE tam 1000 "ok", kalanı 429. Yani kota
  // günde tam 1000 BAŞARILI istek. Saatlik çekim 24 tur × 50 şehir = 1.200 istek ediyor, kota her
  // gün ~20. turda bitiyordu ve VC o günün AKŞAMINI tamamen kaybediyordu (09-19: son yazım 19:29Z,
  // akranları 23:29Z). Akşam kaybı asıl ABD şehirlerini vuruyor: onların karar anı (yerel 13-16h)
  // = 17-20 UTC, yani tam ölü pencere. Artık 2 saatte bir: en fazla 12 tur × 50 = 600 istek
  // (kotanın %60'ı) ve gün boyu EŞİT yayılmış örnek.
  // Kapı "saat çift mi" DEĞİL, "son çekimden bu yana 2 saat geçti mi": bu makine uyuyor (ölçüm
  // 2026-09-20: 07:40 → 10:42 arası hiç tur yok) ve modulo kapısı uykudan sonra 4-6 saatlik delik
  // bırakırdı. Süre kapısı kendini toparlar, yine de günde en çok 12 kez açılır.
  // Kayıp ihmal edilebilir: ardışık saatlik çekimlerin %62,9'u birebir aynı, ortalama fark
  // 0,07-0,12 °C, yalnız %1,3'ü ≥1 °C değişiyor.
  const VC_ARALIK_MS = 2 * 60 * 60 * 1000;
  const vcZamani = (Date.now() - sonVcCekimi) >= VC_ARALIK_MS || process.env.TEMP_MARKET_FORCE_VISUAL_CROSSING === '1';
  // OPEN-METEO — 429'un sebebi ölçüldü (2026-09-20): GÜNLÜK KOTA TÜKENMESİ, ve sebebi BİZ DEĞİLİZ.
  // OM'nin kendi yanıtı: "Daily API request limit exceeded. Please try again tomorrow." Tek bir istek
  // bile 429 yiyor. Ama Heat'in o günkü TÜM OM trafiği 416 istek (315 canlı + 101 WeatherNext),
  // ağırlıklı hesapta ~1.000-2.500 çağrı — kota ise 10.000. Yani kotayı Heat doldurmuyor.
  // Sebep: OM kotaları **IP başına** ve bu makinede kardeş projeler (ASIAbot, junbo) da Open-Meteo
  // çağırıyor (settings.py / weather_ensemble.py / scrapers/meteo.py / t_horizon_collector.py).
  // Ortak IP'nin kotası öğleden sonra tükeniyor → canlı OM'nin blok SONRASI turları (12:00, 18:00)
  // sistematik olarak 429; blok ÖNCESİ turlar (00:00, 06:00) her gün sorunsuz. Günlükte hep CYYZ
  // görünmesi yanıltıcıydı: eski kod ilk 429'da turu kapattığı için diğer istasyonlar hiç denenmiyordu.
  // Bu yüzden eşik küçük: kota tükendiyse ısrar etmenin faydası yok (istekler reddediliyor), ama tek
  // bir geçici 429'un 50 şehri birden götürmesi de pahalıya gelir — eşik bunun sigortası.
  const OM_KOTA_ESIGI = 3;
  const openMeteoDisabled = new Set();
  let openMeteoAtlanan = 0;
  const hasWeatherApi = !!process.env.WEATHERAPI_KEY;
  const hasOpenWeather = !!process.env.OPENWEATHERMAP_API_KEY;
  const hasVisualCrossing = !!process.env.VISUAL_CROSSING_API_KEY;
  if (!hasWeatherApi) console.log('[runner] WeatherAPI: SKIPPED (no key)');
  if (!hasOpenWeather) console.log('[runner] OpenWeatherMap: SKIPPED (no key)');
  for (const station of config.stations.filter((item) => item.enabled !== false)) {
    try {
      if (station.country === 'US') await withRetry(`${station.icao} NWS`, () => collectNwsForecast(station, runAtUtc, userAgent, forecastDays)).catch(() => {});
      if (hasVisualCrossing && vcZamani) await withRetry(`${station.icao} Visual Crossing`, () => collectVisualCrossingForecast(station, runAtUtc, userAgent, forecastDays)).catch(() => {});
      if (!openMeteoDisabled.has('global') && omDue) {
        try {
          const omCount = await withRetry(`${station.icao} Open-Meteo`, () => collectOpenMeteoForecast(station, runAtUtc, userAgent, 5));
          if (omCount > 0) gateOk.openMeteo += 1;
        } catch (e) {
          if (String(e?.message || '').includes('429')) {
            openMeteoAtlanan += 1;
            if (openMeteoAtlanan >= OM_KOTA_ESIGI) {
              openMeteoDisabled.add('global');
              console.log(`[runner] Open-Meteo: DISABLED (${openMeteoAtlanan} istasyon üst üste 429 — günlük kota tükendi, IP paylaşımlı)`);
            }
          }
        }
      }
      // WeatherNext 2 ensemble (64 üye → dağılım). Model 12 saatte bir yenilenir (00/12 UTC), o yüzden
      // saatlik değil 12 saatlik çekilir; anahtar koşu anı olduğu için tekrar çekmek satır çoğaltmaz.
      if (wnDue) {
        const wnCount = await withRetry(`${station.icao} WeatherNext`, () => collectWeatherNextEnsemble(station, runAtUtc, userAgent, 3)).catch(() => 0);
        if (wnCount > 0) gateOk.weatherNext += 1;
      }
      if (hasWeatherApi) await withRetry(`${station.icao} WeatherAPI`, () => collectWeatherApiForecast(station, runAtUtc, userAgent, forecastDays)).catch(() => {});
      if (hasOpenWeather) await withRetry(`${station.icao} OpenWeatherMap`, () => collectOpenWeatherForecast(station, runAtUtc, userAgent, 5)).catch(() => {});
      if (iemDue) {
        const iemOk = await withRetry(`${station.icao} IEM actuals`, () => collectIemActuals(station, runAtUtc, userAgent, 92)).then(() => true).catch(() => false);
        if (iemOk) gateOk.iemActuals += 1;
      }
      await withRetry(`${station.icao} METAR`, () => collectAviationMetar(station, runAtUtc, userAgent)).catch(() => {});
      await withRetry(`${station.icao} Polymarket rules`, () => collectPolymarketRules(station, userAgent)).catch(() => false);
      const settlementResult = await withRetry(`${station.icao} Polymarket settlement`, () => collectPolymarketSettlement(station, userAgent)).catch(() => ({ dates: [], changed: false }));
      const settledTargetDates = settlementResult.dates || [];
      settlementsChanged = settlementsChanged || settlementResult.changed === true;
      const targetDates = Array.from({ length: forecastDays }, (_, offset) => offset).map((offset) => {
        const base = new Date(`${new Intl.DateTimeFormat('en-CA', { timeZone: station.timezone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(runAtUtc))}T12:00:00Z`);
        base.setUTCDate(base.getUTCDate() + offset);
        return new Intl.DateTimeFormat('en-CA', { timeZone: station.timezone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(base);
      });
      // TAHMİN DEFTERİ: `reports` tablosu UNIQUE(station_id,target_date) olduğu için her döngü önceki raporu
      // eziyordu → "dün yarın için ne demiştik" kayboluyordu (ölçüm: 824 MOS raporundan t1'e 2 tane düşüyor).
      // Burada her döngü ayrı satır yazılır; `targetDates` dizisinin İNDEKSİ ufuktur (0=bugün, istasyonun
      // kendi saat dilimine göre kurulur) — bu yüzden ufuk ayrıca hesaplanmaz, doğrudan indeksten gelir.
      const tahminSatirlari = [];
      targetDates.forEach((targetDate, horizon) => {
        const rapor = buildReport(station.id, targetDate);
        if (!rapor || rapor.meanForecastC == null) return;
        tahminSatirlari.push({
          stationId: station.id,
          targetDate,
          runAtUtc,
          horizonDays: horizon,
          rawPrediction: rapor.meanForecastC,
          finalPrediction: rapor.mosForecastC != null ? rapor.mosForecastC : rapor.meanForecastC,
          confidenceLower: rapor.minForecastC,
          confidenceUpper: rapor.maxForecastC,
          trainedCount: rapor.mosCorrectedProviderCount ?? 0,
          totalCount: rapor.mosTotalProviderCount ?? 0,
          modelVersion: rapor.mosForecastC != null ? 'mos-v1' : 'naive-v1',
        });
      });
      if (tahminSatirlari.length) {
        try { logPredictions(tahminSatirlari); } catch (e) { cycleLog.errors.push(`prediction_log (${station.icao}): ${e.message}`); }
      }
      // Geçmiş (çözülmüş) hedef günler yalnız rapor tazelemesi için — tahmin defterine YAZILMAZ (tahmin değil, arşiv).
      for (const settledTargetDate of settledTargetDates) {
        if (!targetDates.includes(settledTargetDate)) buildReport(station.id, settledTargetDate);
      }
    } catch (error) {
      console.error(`[runner] station ${station.icao} cycle failed; continuing with next station:`, error instanceof Error ? error.message : error);
    }
  }
  // VC zaman damgası tur bitince güncellenir: kota istek atıldığı anda harcanır, yanıt başarılı
  // olmasa da bekleme süresi başlamalı (yoksa başarısız tur her turda yeniden dener = kota yanar).
  if (hasVisualCrossing && vcZamani) sonVcCekimi = Date.now();
  // 2026-11-18: slot gates -- mark a step done only if at least one station succeeded; otherwise the
  // next cycle in the same slot retries (self-healing after sleep / outage / restart).
  let gateChanged = false;
  for (const key of Object.keys(GATE_HOURS)) {
    if (gateOk[key] > 0) { markDone(gateState, key, runAtMs, GATE_HOURS[key]); gateChanged = true; }
  }
  if (gateChanged) writeGateState(GATE_STATE, gateState);
  console.log(`[runner] gates om=${omDue ? gateOk.openMeteo : '-'} wn=${wnDue ? gateOk.weatherNext : '-'} iem=${iemDue ? gateOk.iemActuals : '-'}`);
  // Tur özeti: tek istasyonun 429'u ile gerçek kota tükenmesini ayırt etmek için (eskiden ikisi de
  // aynı "DISABLED" satırını basıyordu ve hangisi olduğu anlaşılmıyordu).
  if (openMeteoAtlanan) {
    console.log(`[runner] Open-Meteo: ${openMeteoAtlanan} istasyon 429 ile atlandı`
      + `${openMeteoDisabled.has('global') ? ' — tur kapatıldı (kota)' : ' — tur sürdü, diğer istasyonlar alındı'}`);
  }
  if (settlementsChanged) await withRetry('provider accuracy report refresh', refreshProviderAccuracyReport).catch(() => {});
  const now = new Date();
  if (now.getUTCDay()===1 && now.getUTCHours()===2) await refreshBiasParams().catch(() => {});
  if (now.getUTCHours()===2) await refreshBiasParams().catch(()=>{});
  // LIVE→MAIN canlı forecast arşivi + Kural 10 günlük besleyiciler (script→MAIN köprüsü, kural 9 İstisna-2).
  // Normal pencere UTC 08:00 (son batı-ABD yerel gece yarısından ~07Z sonra → hedef günün SON koşularını da
  // yakalar). Makine o saatte uykudaysa runner geç ayağa kalkınca (saat≥8 ve bugün henüz koşmadıysa) GECİKMELİ
  // koşar — gün atlanmaz. lastDailyBlockDate + kalıcı iz dosyası günde 1× garantisi verir.
  if (now.getUTCHours() >= 8 && lastDailyBlockDate !== now.toISOString().slice(0, 10)) {
    await runDailyBlock(now.getUTCHours() === 8 ? '08:00 penceresi' : 'GECİKMELİ (uykudan sonra yakalama)');
  }
  console.log(`[runner] cycle completed ${new Date().toISOString()}`);
}

let cycleRunning = false;
async function guardedCycle() {
  if (cycleRunning) return;
  cycleRunning = true;
  try { await runCycle(); } catch (error) { console.error('[runner] cycle failed:', error); } finally { cycleRunning = false; }
}

// Port already taken (another Heat runner alive?) must not silently kill us with exit 1.
api.on('error', (error) => {
  if (error && error.code === 'EADDRINUSE') {
    console.error(`[runner] FATAL: port ${apiPort} already in use - another runner instance is alive. Exiting cleanly (0), no restart.`);
    try { closeDb(); } catch {}
    process.exit(0);
  }
  console.error('[runner] API server error:', error);
});

api.listen(apiPort, '0.0.0.0', () => console.log(`[runner] local API listening on port ${apiPort}`));
await guardedCycle();
const timer = setInterval(guardedCycle, intervalMinutes * 60 * 1000);

let shuttingDown = false;
function shutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`[runner] ${signal} received, closing`);
  clearInterval(timer);
  try { api.close(); } catch {}
  try { closeDb(); } catch {}
  process.exit(0);
}
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));

// Visibility + self-recovery: with no handlers an uncaught error / unhandled rejection kills the
// process silently and the logon-triggered task never restarts it (this is why it stayed down ~12h).
// exit 1 here is intentional: start-runner.ps1 (görev RestartCount) + "TempMarket Edge Watchdog"
// (saatlik, port boşsa başlatır) onu geri ayağa kaldırır.
process.on('uncaughtException', (err) => {
  console.error('[runner] UNCAUGHT EXCEPTION (logged, exiting 1 so wrapper restarts):', err);
  try { closeDb(); } catch {}
  process.exit(1);
});
process.on('unhandledRejection', (reason) => {
  console.error('[runner] UNHANDLED REJECTION (logged, continuing):', reason);
});
