import { logSourceRequest, insertForecastSnapshot, insertMarketRule, insertObservation, insertSettlement, insertWnEnsemble } from './db.mjs';
import { omMu, omOnbellekAl, omOnbellekKoy } from './om_cache.mjs';

// Ortak Open-Meteo önbelleğinin tazelik penceresi (bkz. ORTAK_OM_ONBELLEK.md §3).
// 60 dakika bilinçli: amaç ÜÇ projenin AYNI saat içinde aynı isteği tekrarlamasını engellemek.
// Daha uzun tutmak Heat'in kendi koşularını önbellekten beslerdi — bu depoda bayat tahmin
// affedilmez, o yüzden pencere kısa. SATIR YİNE DE gerçek çekim anını taşır; karar çağıranın.
const OM_ONBELLEK_MS = Number(process.env.OM_CACHE_TTL_MS || 60 * 60 * 1000);

import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);
const requestCache = new Map();
const polymarketProxy = process.env.POLYMARKET_SOCKS5_PROXY || 'socks5h://127.0.0.1:40000';

async function requestViaPolymarketProxy(url, { parse, userAgent }) {
  const args = ['--silent', '--show-error', '--max-time', '20', '--proxy', polymarketProxy, '--header', `accept: ${parse === 'text' ? 'text/plain,text/csv' : 'application/json'}`, '--header', `user-agent: ${userAgent}`, '--write-out', `${String.fromCharCode(10)}__HTTP_STATUS__%{http_code}`, url];
  const { stdout } = await execFileAsync('curl.exe', args, { windowsHide: true, maxBuffer: 8 * 1024 * 1024 });
  const marker = `${String.fromCharCode(10)}__HTTP_STATUS__`;
  const markerIndex = stdout.lastIndexOf(marker);
  if (markerIndex < 0) throw new Error('Polymarket proxy response did not include HTTP status');
  const body = stdout.slice(0, markerIndex);
  const statusCode = Number(stdout.slice(markerIndex + marker.length).trim());
  if (statusCode < 200 || statusCode >= 300) throw new Error(`${statusCode} ${body.slice(0, 180)}`);
  return { body, statusCode };
}

async function request(url, { source, stationId, userAgent, parse = 'json', cacheTtlMs = 0, diskCacheMs = 0, cekimKutusu = null }) {
  const cacheKey = `${parse}:${url}`;
  const cached = requestCache.get(cacheKey);
  if (cacheTtlMs > 0 && cached && Date.now() - cached.at < cacheTtlMs) {
    // Bellek önbelleği de GERÇEK çekim anını taşır: aksi hâlde bu yol run_at_utc'yi "şimdi"ye
    // düşürür ve 2. dürüstlük kuralı (ORTAK_OM_ONBELLEK.md §3) sessizce delinirdi.
    if (cekimKutusu) { cekimKutusu.cekimAni = cached.cekimAni || null; cekimKutusu.onbellek = true; }
    return cached.value;
  }
  // ORTAK DİSK ÖNBELLEĞİ (yalnız Open-Meteo — bkz. om_cache.mjs ve ORTAK_OM_ONBELLEK.md).
  // Önbellekten gelen istek source_requests'e YAZILMAZ; yazılsaydı "gerçek OM kullanımı" ölçümü
  // şişerdi (o tablo 2026-09-20'de kotayı teşhis etmek için kullanıldı). Kanıt om_cache_gun'da.
  if (diskCacheMs > 0 && parse === 'json' && omMu(url)) {
    const v = omOnbellekAl(url, { azamiYasMs: diskCacheMs });
    if (v) {
      if (cekimKutusu) { cekimKutusu.cekimAni = v.fetchedAtUtc; cekimKutusu.onbellek = true; }
      return v.veri;
    }
  }
  const started = Date.now();
  let statusCode = null;
  try {
    let text;
    if (source.startsWith('Polymarket')) {
      const proxied = await requestViaPolymarketProxy(url, { parse, userAgent });
      statusCode = proxied.statusCode;
      text = proxied.body;
    } else {
      const response = await fetch(url, { headers: { accept: parse === 'text' ? 'text/plain,text/csv' : 'application/json', 'user-agent': userAgent }, signal: AbortSignal.timeout(20000) });
      statusCode = response.status;
      text = await response.text();
    }
    if (statusCode < 200 || statusCode >= 300) throw new Error(`${statusCode} ${text.slice(0, 180)}`);
    const data = parse === 'text' ? text : (text ? JSON.parse(text) : null);
    const cekimAni = new Date().toISOString();
    logSourceRequest({ stationId, source, endpoint: url, requestedAt: cekimAni, statusCode, responseMs: Date.now() - started, ok: true });
    if (cacheTtlMs > 0) requestCache.set(cacheKey, { at: Date.now(), value: data, cekimAni });
    if (diskCacheMs > 0 && parse === 'json' && omMu(url)) omOnbellekKoy(url, data, { statusCode });
    // Çağırana verinin GERÇEK çekim anını ver: önbellekten gelseydi "şimdi" yazmak yalan olurdu.
    if (cekimKutusu) { cekimKutusu.cekimAni = cekimAni; cekimKutusu.onbellek = false; }
    return data;
  } catch (error) {
    logSourceRequest({ stationId, source, endpoint: url, requestedAt: new Date().toISOString(), statusCode, responseMs: Date.now() - started, ok: false, error: error instanceof Error ? error.message : String(error) });
    throw error;
  }
}

function localDate(date, timezone) {
  return new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(date);
}

function nextDates(timezone, days) {
  const now = new Date();
  const base = localDate(now, timezone);
  const dates = [];
  const cursor = new Date(`${base}T12:00:00Z`);
  for (let index = 0; index < days; index += 1) {
    const day = new Date(cursor);
    day.setUTCDate(cursor.getUTCDate() + index);
    dates.push(new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(day));
  }
  return dates;
}

function horizon(runAtUtc, targetDate, timezone) {
  const runDate = localDate(new Date(runAtUtc), timezone);
  return Math.round((Date.parse(`${targetDate}T12:00:00Z`) - Date.parse(`${runDate}T12:00:00Z`)) / 86400000);
}

function fahrenheitToC(value) {
  return (Number(value) - 32) * 5 / 9;
}

// DİKKAT: `Number(null) === 0` — bu yüzden null/undefined/'' AÇIKÇA elenir. Elemezsek API'nin "bu model bu
// noktada yok" diye döndürdüğü null, "0°C tahmini" gibi kaydedilir (2026-09-15'te bulundu: Open-Meteo
// ecmwf_ifs04 `[null,null,...]` dönüyordu ve 3.320 sahte sıfır satırı ortalamayı 0.81°C SOĞUĞA çekiyordu:
// MAE 1.21 → 1.81). Çağıranlardaki `if (high === null && low === null) continue;` koruması ancak bu
// düzeltmeyle çalışır.
function celsius(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export async function collectNwsForecast(station, runAtUtc, userAgent, days = 3) {
  const points = await request(`https://api.weather.gov/points/${station.latitude},${station.longitude}`, { source: 'NWS points', stationId: station.id, userAgent });
  const hourly = await request(points.properties.forecastHourly, { source: 'NWS forecastHourly', stationId: station.id, userAgent });
  const targetDates = new Set(nextDates(station.timezone, days));
  const groups = new Map();
  for (const period of hourly.properties.periods || []) {
    const rawTemperature = Number(period.temperature);
    if (!Number.isFinite(rawTemperature)) continue;
    const temperature = period.temperatureUnit === 'F' ? fahrenheitToC(rawTemperature) : rawTemperature;
    const targetDate = localDate(new Date(period.startTime), station.timezone);
    if (!targetDates.has(targetDate)) continue;
    const current = groups.get(targetDate) || { high: -Infinity, low: Infinity, raw: [] };
    current.high = Math.max(current.high, temperature);
    current.low = Math.min(current.low, temperature);
    current.raw.push(period);
    groups.set(targetDate, current);
  }
  for (const [targetDate, group] of groups) {
    insertForecastSnapshot({ stationId: station.id, source: 'NWS', provider: 'NWS forecastHourly', runAtUtc, targetDate, horizonDays: horizon(runAtUtc, targetDate, station.timezone), predictedHighC: group.high, predictedLowC: group.low, raw: { periods: group.raw } });
  }
  return groups.size;
}

export async function collectVisualCrossingForecast(station, runAtUtc, userAgent, days = 3) {
  const key = process.env.VISUAL_CROSSING_API_KEY;
  if (!key) return 0;
  const url = `https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline/${station.latitude},${station.longitude}/next${days}days?unitGroup=metric&include=days,hours&key=${encodeURIComponent(key)}&contentType=json`;
  const data = await request(url, { source: 'Visual Crossing', stationId: station.id, userAgent });
  let count = 0;
  for (const day of data.days || []) {
    const high = celsius(day.tempmax);
    const low = celsius(day.tempmin);
    if (high === null && low === null) continue;
    insertForecastSnapshot({ stationId: station.id, source: 'Visual Crossing', provider: 'Visual Crossing daily', runAtUtc, targetDate: day.datetime, horizonDays: horizon(runAtUtc, day.datetime, station.timezone), predictedHighC: high ?? low, predictedLowC: low ?? high, raw: day });
    count += 1;
  }
  return count;
}

async function collectDailyForecast(station, runAtUtc, source, provider, data, dates, maxKey = 'temperature_2m_max', minKey = 'temperature_2m_min') {
  const highs = data.daily?.[maxKey] || [];
  const lows = data.daily?.[minKey] || [];
  let count = 0;
  for (let index = 0; index < (data.daily?.time || []).length; index += 1) {
    const targetDate = data.daily.time[index];
    if (dates && !dates.has(targetDate)) continue;
    const high = celsius(highs[index]);
    const low = celsius(lows[index]);
    if (high === null && low === null) continue;
    insertForecastSnapshot({ stationId: station.id, source, provider, runAtUtc, targetDate, horizonDays: horizon(runAtUtc, targetDate, station.timezone), predictedHighC: high ?? low, predictedLowC: low ?? high, raw: { daily: data.daily, index } });
    count += 1;
  }
  return count;
}

// Model adları: `ecmwf_ifs04` Open-Meteo'nun forecast ucunda ÖLÜ (2026-09-15 ölçümü: 200 döner ama
// temperature_2m_max dizisi tamamen null; 6.021 satırın 6.021'i bundan zehirlenmişti). Doğrusu bugün
// `ecmwf_ifs025` — canlı değer veriyor. Adı değiştirmek yeni bir kaynak açar (n>=10 olunca MOS'a girer);
// eski ad tarihsel kayıtlarda kalır ve `train_bias_online` onu dışlamaya devam eder.
export async function collectOpenMeteoForecast(station, runAtUtc, userAgent, days = 5, models = ['ecmwf_ifs025', 'gfs_seamless', 'icon_seamless']) {
  const targets = new Set(nextDates(station.timezone, days));
  let count = 0;
  for (const model of models) {
    const url = `https://api.open-meteo.com/v1/forecast?latitude=${station.latitude}&longitude=${station.longitude}&daily=temperature_2m_max,temperature_2m_min&forecast_days=${days}&models=${model}&timezone=${encodeURIComponent(station.timezone)}`;
    // kutu.cekimAni: ağdan geldiyse ŞİMDİ, önbellekten geldiyse İLK ÇEKİM ANI. run_at_utc bu olur —
    // önbellekten gelen tahmini "şimdi öğrendik" diye yazmak bu depoda yasak (bayat veri kuralı).
    const kutu = {};
    const data = await request(url, { source: 'Open-Meteo', stationId: station.id, userAgent, cacheTtlMs: 20 * 60 * 1000, diskCacheMs: OM_ONBELLEK_MS, cekimKutusu: kutu });
    count += await collectDailyForecast(station, kutu.cekimAni || runAtUtc, 'Open-Meteo', `Open-Meteo ${model}`, data, targets);
  }
  return count;
}

// ---------------------------------------------------------------------------------------------
// WEATHERNEXT 2 ENSEMBLE — 64 üyeli dağılım (2026-09-19 eklendi)
//
// Ne işe yarar: bugüne kadar bütün kaynaklar TEK bir sıcaklık veriyordu; kova olasılığı `round(tahmin)`
// ile türetiliyordu. Bu kaynak NOKTA değil DAĞILIM verir — 64 üyenin her biri için o yerel günün
// maksimumu hesaplanır, kova olasılığı doğrudan üye sayımından çıkar. Bugüne kadar denenmemiş tek
// girdi sınıfı budur (bkz. §3g: bizim olasılığımız kötü kalibre, ucuz bantta aşırı özgüvenli).
//
// NEDEN AYRI TABLO (`wn_ensemble`, `forecast_snapshots` DEĞİL):
//   1) Orada anahtar `run_at_utc` = ÇEKİM ANI → her tur yeni satır. Burada anahtar modelin KOŞU anı
//      (00/12 UTC) → günde en çok 2 satır, üzerine yazılır. Sınırlı büyüme.
//   2) Ham yük satır başına BİR KEZ. Ölçüm (2026-09-19): `forecast_snapshots` 2.200 MB, bunun
//      2.003 MB'ı (%91) `raw_json` — aynı yanıtın kopyaları. Aynı hatayı burada tekrarlamıyoruz.
//   3) Ham saatlik seri saklanırsa 64 üye × 120 saat × 50 şehir = tur başına 384.000 sayı, ayda ~3 GB.
//      Üye başına GÜNLÜK MAKS saklanır (~100 kat küçük) ve zaten ihtiyaç duyduğumuz büyüklük budur.
//
// LİSANS: Open-Meteo bu modeli "Non-Commercial / Commercial" seçeneğiyle sunuyor; ticari koşul
// netleşene kadar bu kaynak KARAR için değil ÖLÇÜM için kullanılır.
const WN_MODEL = 'google_weathernext2_ensemble';

// Modelin koşu anı. Open-Meteo WeatherNext'i 00 ve 12 UTC'de işler; çekim anını 12 saatlik kovaya
// yuvarlarız ki aynı koşuyu saatlerce tekrar çekmek yeni satır üretmesin (yalnız üzerine yazar).
function wnRunKey(runAtUtc) {
  const t = new Date(runAtUtc);
  return new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), t.getUTCDate(), t.getUTCHours() < 12 ? 0 : 12, 0, 0)).toISOString();
}

export async function collectWeatherNextEnsemble(station, runAtUtc, userAgent, days = 3, pastDays = 0) {
  const url = `https://ensemble-api.open-meteo.com/v1/ensemble?latitude=${station.latitude}&longitude=${station.longitude}`
    + `&hourly=temperature_2m&models=${WN_MODEL}&forecast_days=${days}&past_days=${pastDays}`
    + `&timezone=${encodeURIComponent(station.timezone)}`;
  const kutu = {};
  const data = await request(url, { source: 'Open-Meteo', stationId: station.id, userAgent, cacheTtlMs: 20 * 60 * 1000, diskCacheMs: OM_ONBELLEK_MS, cekimKutusu: kutu });
  const saatlik = data.hourly;
  if (!saatlik || !Array.isArray(saatlik.time)) return 0;
  // Üye sütunları: `temperature_2m_member01`..`member63`. Çıplak `temperature_2m` ortalamadır, üye değil.
  const uyeKolonlari = Object.keys(saatlik).filter((k) => /^temperature_2m_member\d+$/.test(k)).sort();
  if (!uyeKolonlari.length) return 0;

  // Yerel güne göre üye başına maksimum. `timezone` verildiği için damgalar YERELdir ("2026-09-19T00:00").
  const gunluk = new Map();
  for (let i = 0; i < saatlik.time.length; i += 1) {
    const targetDate = String(saatlik.time[i]).slice(0, 10);
    let satir = gunluk.get(targetDate);
    if (!satir) { satir = new Array(uyeKolonlari.length).fill(null); gunluk.set(targetDate, satir); }
    for (let m = 0; m < uyeKolonlari.length; m += 1) {
      const v = celsius(saatlik[uyeKolonlari[m]]?.[i]);
      if (v === null) continue;
      if (satir[m] === null || v > satir[m]) satir[m] = v;
    }
  }

  // Koşu etiketi ve çekim anı ÖNBELLEK-DOĞRU olmalı: önbellekten gelen veri 11:50'de çekilmişse
  // onu 12Z koşusu diye etiketlemek yanlış olurdu (12Z koşusu henüz çıkmamış olabilir).
  const cekimAni = kutu.cekimAni || runAtUtc;
  const runKey = wnRunKey(cekimAni);
  const fetchedAtUtc = cekimAni;
  const hedefler = new Set(nextDates(station.timezone, days));
  let count = 0;
  for (const [targetDate, uyeler] of gunluk) {
    const dolu = uyeler.filter((v) => v !== null);
    if (!dolu.length) continue;
    // Normal turlarda yalnız bugün ve sonrası yazılır. `pastDays` tohumlama koşusuna aittir: geçmiş
    // günler için de yazar, ama o satırlar "o an ne biliyorduk" özelliğini TAŞIMAZ (arşivden gelir).
    if (!pastDays && !hedefler.has(targetDate)) continue;
    const ortalama = dolu.reduce((s, v) => s + v, 0) / dolu.length;
    const varyans = dolu.reduce((s, v) => s + (v - ortalama) ** 2, 0) / dolu.length;
    insertWnEnsemble({
      stationId: station.id,
      model: WN_MODEL,
      runAtUtc: runKey,
      targetDate,
      memberCount: dolu.length,
      meanC: ortalama,
      spreadC: Math.sqrt(varyans),
      members: uyeler,
      fetchedAtUtc,
    });
    count += 1;
  }
  return count;
}

export async function collectWeatherApiForecast(station, runAtUtc, userAgent, days = 3) {
  const key = process.env.WEATHERAPI_KEY;
  if (!key) {
    logSourceRequest({ stationId: station.id, source: 'WeatherAPI.com', endpoint: 'not configured', requestedAt: new Date().toISOString(), ok: false, error: 'WEATHERAPI_KEY is not configured' });
    return 0;
  }
  const url = `https://api.weatherapi.com/v1/forecast.json?key=${encodeURIComponent(key)}&q=${station.latitude},${station.longitude}&days=${days}&aqi=no&alerts=no`;
  const data = await request(url, { source: 'WeatherAPI.com', stationId: station.id, userAgent });
  const selectedDates = new Set(nextDates(station.timezone, days));
  let count = 0;
  for (const day of data.forecast?.forecastday || []) {
    if (!selectedDates.has(day.date)) continue;
    const high = celsius(day.day?.maxtemp_c);
    const low = celsius(day.day?.mintemp_c);
    if (high === null && low === null) continue;
    insertForecastSnapshot({ stationId: station.id, source: 'WeatherAPI.com', provider: 'WeatherAPI daily', runAtUtc, targetDate: day.date, horizonDays: horizon(runAtUtc, day.date, station.timezone), predictedHighC: high ?? low, predictedLowC: low ?? high, raw: day });
    count += 1;
  }
  return count;
}

export async function collectOpenWeatherForecast(station, runAtUtc, userAgent, days = 5) {
  const key = process.env.OPENWEATHERMAP_API_KEY;
  if (!key) {
    logSourceRequest({ stationId: station.id, source: 'OpenWeatherMap', endpoint: 'not configured', requestedAt: new Date().toISOString(), ok: false, error: 'OPENWEATHERMAP_API_KEY is not configured' });
    return 0;
  }
  const url = `https://api.openweathermap.org/data/2.5/forecast?lat=${station.latitude}&lon=${station.longitude}&appid=${encodeURIComponent(key)}&units=metric`;
  const data = await request(url, { source: 'OpenWeatherMap', stationId: station.id, userAgent });
  const groups = new Map();
  for (const item of data.list || []) {
    const targetDate = localDate(new Date((item.dt || 0) * 1000), station.timezone);
    const current = groups.get(targetDate) || { high: -Infinity, low: Infinity, raw: [] };
    const temperature = celsius(item.main?.temp);
    if (temperature === null) continue;
    current.high = Math.max(current.high, temperature);
    current.low = Math.min(current.low, temperature);
    current.raw.push(item);
    groups.set(targetDate, current);
  }
  const selectedDates = new Set(nextDates(station.timezone, days));
  let count = 0;
  for (const [targetDate, group] of groups) {
    if (!selectedDates.has(targetDate)) continue;
    insertForecastSnapshot({ stationId: station.id, source: 'OpenWeatherMap', provider: 'OpenWeatherMap 3-hour', runAtUtc, targetDate, horizonDays: horizon(runAtUtc, targetDate, station.timezone), predictedHighC: group.high, predictedLowC: group.low, raw: { list: group.raw } });
    count += 1;
  }
  return count;
}

function parseIemCsv(text) {
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter((line) => line && !line.startsWith('#'));
  if (lines.length < 2) return [];
  const headers = lines[0].split(',').map((header) => header.trim());
  const validIndex = headers.indexOf('valid');
  const tempIndex = headers.indexOf('tmpf');
  if (validIndex < 0 || tempIndex < 0) return [];
  return lines.slice(1).map((line) => {
    const cells = line.split(',');
    return { valid: cells[validIndex], tmpf: cells[tempIndex] };
  }).filter((row) => row.valid && row.tmpf && row.tmpf !== 'M');
}

export async function collectIemActuals(station, runAtUtc, userAgent, pastDays = 92) {
  const end = new Date(runAtUtc);
  const start = new Date(end.getTime() - pastDays * 86400000);
  const parts = (date) => ({ year: date.getUTCFullYear(), month: date.getUTCMonth() + 1, day: date.getUTCDate() });
  const from = parts(start);
  const to = parts(end);
  const query = new URLSearchParams({ station: station.icao, data: 'tmpf', year1: String(from.year), month1: String(from.month), day1: String(from.day), year2: String(to.year), month2: String(to.month), day2: String(to.day), tz: 'Etc/UTC', format: 'comma', latlon: 'no', elev: 'no', missing: 'M', trace: 'T', direct: 'no' });
  query.append('report_type', '1');
  query.append('report_type', '2');
  const url = `https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?${query.toString()}`;
  const rows = parseIemCsv(await request(url, { source: 'Iowa Mesonet IEM ASOS', stationId: station.id, userAgent, parse: 'text' }));
  const byDate = new Map();
  for (const row of rows) {
    const valueC = fahrenheitToC(Number(row.tmpf));
    if (!Number.isFinite(valueC)) continue;
    const observed = new Date(row.valid.replace(' ', 'T') + 'Z');
    const targetDate = localDate(observed, station.timezone);
    const current = byDate.get(targetDate) || { max: -Infinity, min: Infinity, observedAt: observed.toISOString(), samples: 0 };
    current.max = Math.max(current.max, valueC);
    current.min = Math.min(current.min, valueC);
    current.observedAt = observed.toISOString();
    current.samples += 1;
    byDate.set(targetDate, current);
  }
  for (const [targetDate, actual] of byDate) {
    insertObservation({ stationId: station.id, source: 'Iowa Mesonet IEM ASOS', observedAtUtc: actual.observedAt, targetDate, temperatureC: actual.max, runningHighC: actual.max, actualMaxC: actual.max, actualMinC: actual.min, actualSource: 'IEM ASOS tmpf', raw: { actualMaxC: actual.max, actualMinC: actual.min, sampleCount: actual.samples, source: 'IEM ASOS tmpf' } });
  }
  return byDate.size;
}

export async function collectAviationMetar(station, runAtUtc, userAgent) {
  const url = `https://aviationweather.gov/api/data/metar?ids=${encodeURIComponent(station.icao)}&format=json&hours=2`;
  const data = await request(url, { source: 'AviationWeather METAR', stationId: station.id, userAgent });
  const item = Array.isArray(data) ? data[0] : data?.data?.[0];
  if (!item) return false;
  const observedAtUtc = item.reportTime || item.obsTime || runAtUtc;
  const temperatureC = Number(item.temp ?? item.temperature);
  insertObservation({ stationId: station.id, source: 'AviationWeather METAR', observedAtUtc: new Date(observedAtUtc).toISOString(), targetDate: localDate(new Date(observedAtUtc), station.timezone), temperatureC: Number.isFinite(temperatureC) ? temperatureC : null, runningHighC: null, raw: item });
  return true;
}

function parseOutcomeTemperatureC(label) {
  const normalized = String(label || '').trim().replace(/\s+/g, ' ');
  // Single value: "30°C", "91°F"
  const exact = normalized.match(/^(-?\d+(?:\.\d+)?)°\s*([CF])$/i);
  if (exact) {
    const value = Number(exact[1]);
    if (!Number.isFinite(value)) return null;
    return exact[2].toUpperCase() === 'F' ? fahrenheitToC(value) : value;
  }
  // Range: "90-91°F", "66-67°F" — take midpoint
  const range = normalized.match(/^(-?\d+)\s*-\s*(-?\d+)°\s*([CF])$/i);
  if (range) {
    const low = Number(range[1]);
    const high = Number(range[2]);
    if (!Number.isFinite(low) || !Number.isFinite(high)) return null;
    const avgF = (low + high) / 2;
    return range[3].toUpperCase() === 'F' ? fahrenheitToC(avgF) : avgF;
  }
  // "X°C or higher" / "X°C or below" — take the value
  const orMore = normalized.match(/^(-?\d+(?:\.\d+)?)°\s*[CF]\s*or\s*(higher|below)/i);
  if (orMore) {
    const value = Number(orMore[1]);
    if (!Number.isFinite(value)) return null;
    return orMore[0].toUpperCase().includes('F') ? fahrenheitToC(value) : value;
  }
  return null;
}

function isWinningYes(market) {
  try {
    const outcomes = JSON.parse(market.outcomes || '[]');
    const prices = JSON.parse(market.outcomePrices || '[]');
    const yesIndex = outcomes.findIndex((outcome) => String(outcome).toLowerCase() === 'yes');
    return yesIndex >= 0 && Number(prices[yesIndex]) >= 0.999;
  } catch {
    return false;
  }
}

function parsePolymarketPageMarkets(html) {
  const markets = [];
  const pattern = /\\"groupItemTitle\\"\\s*:\\s*\\"([^\\"]+)\\"[\\s\\S]{0,2600}?\\"outcomePrices\\"\\s*:\\s*\\"(\\[[^\\"]+\\])\\"/g;
  for (const match of html.matchAll(pattern)) markets.push({ groupItemTitle: match[1], outcomes: '[\\"Yes\\",\\"No\\"]', outcomePrices: match[2], resolved: true });
  return markets;
}

async function fetchPolymarketEvent(station, userAgent, source, marketSlug = station.marketSlug) {
  const encoded = encodeURIComponent(marketSlug);
  try {
    return await request(`https://gamma-api.polymarket.com/events/slug/${encoded}`, { source, stationId: station.id, userAgent });
  } catch (gammaError) {
    const html = await request(`https://polymarket.com/event/${encoded}`, { source: `${source} page fallback`, stationId: station.id, userAgent, parse: 'text' });
    return { description: null, rules: null, markets: parsePolymarketPageMarkets(html), pageFallback: true, gammaError: gammaError instanceof Error ? gammaError.message : String(gammaError) };
  }
}

function marketSlugsForStation(station) {
  if (Array.isArray(station.marketSlugs) && station.marketSlugs.length > 0) return station.marketSlugs;
  return station.marketSlug ? [station.marketSlug] : [];
}

const TEMPERATURE_EVENT_RE = /^Highest temperature in .+? on .+\?$/i;
const SETTLEMENT_SITE_RE = /site=([a-z0-9]{4})/i;

function eventRuleText(event) {
  const parts = [event?.description, event?.rules, event?.resolutionSource];
  for (const market of event?.markets || []) parts.push(market?.description, market?.rules, market?.resolutionSource);
  return parts.filter(Boolean).join('\\n');
}

export async function discoverPolymarketTemperatureEvents(userAgent, maxPages = 10) {
  const discovered = [];
  let cursor = null;
  for (let page = 0; page < maxPages; page += 1) {
    const params = new URLSearchParams({ active: 'true', closed: 'false', tag_id: '84', limit: '100' });
    if (cursor) params.set('after_cursor', cursor);
    const data = await request(`https://gamma-api.polymarket.com/events/keyset?${params.toString()}`, { source: 'Polymarket Gamma discovery', stationId: null, userAgent });
    for (const event of data?.events || []) {
      const title = String(event?.title || '').trim();
      if (!TEMPERATURE_EVENT_RE.test(title)) continue;
      const site = eventRuleText(event).match(SETTLEMENT_SITE_RE)?.[1]?.toLowerCase();
      const slug = String(event?.slug || '').trim();
      if (!site || !slug) continue;
      discovered.push({ stationId: site, marketSlug: slug, title, eventId: String(event?.id || '') });
    }
    const next = data?.next_cursor;
    if (!next || next === 'LTE=' || !Array.isArray(data?.events) || data.events.length === 0) break;
    cursor = String(next);
  }
  return discovered;
}

export async function collectPolymarketRules(station, userAgent) {
  const marketSlugs = marketSlugsForStation(station);
  if (marketSlugs.length === 0) return false;
  for (const marketSlug of marketSlugs) {
    const encoded = encodeURIComponent(marketSlug);
    const data = await fetchPolymarketEvent(station, userAgent, 'Polymarket Gamma', marketSlug);
    insertMarketRule({ stationId: station.id, marketSlug, fetchedAt: new Date().toISOString(), source: 'Polymarket Gamma', ruleText: data.description || data.rules || null, raw: data });
  }
  return true;
}

export async function collectPolymarketSettlement(station, userAgent) {
  const marketSlugs = marketSlugsForStation(station);
  const settledDates = [];
  let changed = false;
  for (const marketSlug of marketSlugs) {
    const encoded = encodeURIComponent(marketSlug);
    const data = await fetchPolymarketEvent(station, userAgent, 'Polymarket Gamma settlement', marketSlug);
    const markets = Array.isArray(data.markets) ? data.markets : [];
    const resolvedWinner = markets.find((market) => (market.resolved || market.umaResolutionStatus === 'resolved') && isWinningYes(market));
    if (!resolvedWinner) continue;
    const highC = parseOutcomeTemperatureC(resolvedWinner.groupItemTitle);
    if (highC === null) {
      console.warn(`[runner] Polymarket outcome is a range/non-exact label for ${station.icao}: ${resolvedWinner.groupItemTitle}`);
      continue;
    }
    const targetDate = resolvedWinner.endDateIso || String(resolvedWinner.endDate || '').slice(0, 10) || localDate(new Date(), station.timezone);
    changed = insertSettlement({ stationId: station.id, targetDate, source: 'Polymarket Gamma settlement', highC, recordedAtUtc: new Date().toISOString(), raw: { eventSlug: marketSlug, marketSlug: resolvedWinner.slug, outcomeLabel: resolvedWinner.groupItemTitle, resolutionSource: resolvedWinner.resolutionSource, settlement: true, event: data } }) || changed;
    settledDates.push(targetDate);
  }
  return { dates: [...new Set(settledDates)], changed };
}
