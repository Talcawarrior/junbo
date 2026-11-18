#!/usr/bin/env node
// Ne işe yarar: ŞEHİR × KAYNAK × UFUK bazında bias'ı (sistematik sapma) günlük öğrenir ve `bias_params`a
//   yazar; MOS düzeltmesi bu tablodan beslenir (`windows-runner/report.mjs`, MOS_MIN_N=10).
//   Girdi: MAIN.forecast_snapshots ⨝ MAIN.settlements (son koşu değil, TÜM koşular çiftlenir).
//   Çıktı: LIVE.bias_params (MOS buradan okur, Kural 9) + MAIN.bias_params (arşiv/rapor tarafı) —
//   ikisi de aynı koşuda tazelenir ki raporlar bayat bias görmesin.
//
// KAYNAK KAPSAMI (2026-09-15 kullanıcı kararı): ESKİDEN yalnız 4 ticari sağlayıcı eğitiliyordu
//   (VC/WeatherAPI/OWM/NWS), Open-Meteo bilerek dışarıdaydı. Oysa Open-Meteo canlı modelleri (OM-GFS,
//   OM-ICON) en iyi iki canlı kaynak ve düzeltmeden açıkça kazanıyorlar (bkz. rapor_kaynak_sehir_bias.md
//   §5-§6). Artık "izinli liste" yerine "AÇIKÇA DIŞLANAN" liste var: yalnız bilinen-bozuk kaynak dışarıda.
//   `--with-openmeteo` geriye dönük uyumluluk için kabul edilir ama artık işlevsizdir (uyarı basar).
import path from 'node:path';
import fs from 'node:fs';

const K_SHRINK = 12; // hiyerarşik büzülme (az veri → kaynak geneline çek)
const MIN_N = 15;    // güven eşiği: altındaki hücre MAIN'e YAZILMAZ (tek günlük veriden bias olmaz)
const WITH_OM = process.argv.includes('--with-openmeteo'); // no-op, geriye dönük uyumluluk
// Bias EĞİTİMİNE girmeyecek kaynaklar — TAM AD eşleşmesi (kısmi eşleşme "ecmwf" arşiv modelini de elerdi).
//   Open-Meteo ecmwf_ifs04: canlı akış bozuk, tahmini 0 geliyor (ANAYASA §3.4; rapor_iskabet_t02 BROKEN).
const EXCLUDED_PROVIDERS = new Set(['Open-Meteo ecmwf_ifs04']);
// BAYAT AKIŞ KURALI (2026-09-15): eğitim sorgusunun tarih filtresi YOK — bu yüzden runner'ın artık hiç
//   TOPLAMADIĞI eski akışlar sonsuza dek eğitimde kalıyordu. Ölçüm: `NOAA GFS daily` en son **2025-04-03**'te
//   veri üretmiş (bir buçuk yıl önce) ama hâlâ 2 hücre n=137 ile eğitiliyordu (MAE 5.50); `openmeteo_* daily`
//   deney artıkları 2026-07-28'de durmuş. Bunlar canlı MOS'a hiç girmez (gelecek tarihte snapshot'ları yok)
//   ama tabloyu şişirir ve GEÇMİŞ skorlamada aynı modeli iki kez oylatır (ör. `gfs_seamless` + onun ölü
//   `gfs_seamless daily` ikizi = aynı Open-Meteo modeli). Kural: son snapshot'ı BAYAT_AKIS_GUN günden eski
//   olan sağlayıcı eğitilmez. Akış geri gelirse kendiliğinden yeniden girer — elle liste bakımı gerekmez.
//   Ölçtüğümüz canlı kaynaklar bugün 0-3 günlük; kural onlara dokunmaz (bkz. TEST_DEFTERI).
const BAYAT_AKIS_GUN = 14;
const isExcluded = (prov, bayatKumesi) => EXCLUDED_PROVIDERS.has(String(prov)) || bayatKumesi.has(String(prov));

// Bir sağlayıcının son snapshot'ı kaç gün önce? (BUGÜNE göre; hedef gün değil KOŞU tarihi esas alınır —
//   yoksa gelecek hedefli ama artık üretilmeyen akış "taze" görünürdü.)
// Döner: bayat = eğitilmeyecek adlar, taze = son BAYAT_AKIS_GUN günde veri üretmiş adlar.
async function bayatAksilariBul(db) {
  const esik = new Date(Date.now() - BAYAT_AKIS_GUN * 86400000).toISOString();
  const taze = new Set(
    db.prepare('SELECT DISTINCT provider FROM forecast_snapshots WHERE run_at_utc >= ?').all(esik).map((r) => String(r.provider)),
  );
  const hepsi = db.prepare('SELECT DISTINCT provider FROM forecast_snapshots').all().map((r) => String(r.provider));
  return { bayat: new Set(hepsi.filter((p) => !taze.has(p) && p !== '')), taze };
}

async function main() {
  if (WITH_OM) console.warn('[uyarı] --with-openmeteo artık gereksiz: Open-Meteo zaten kapsam içinde (2026-09-15).');
  const { DatabaseSync } = await import('node:sqlite');
  const dataDir = process.env.TEMP_MARKET_DATA_DIR || path.join('D:\\HEAT data', 'db');
  const dbPath = process.env.TEMP_MARKET_DB || path.join(dataDir, 'tempmarket-edge.sqlite');
  // ensure migration
  await import('../windows-runner/db.mjs');
  const db = new DatabaseSync(dbPath);

  // Build training_pairs from forecast_snapshots vs actual (settlement).
  // PERFORMANS (2026-09-15): eskiden her satır tek tek (transaction'sız) yazılıyordu ve gerçek, satır başına
  // korelasyonlu alt-sorguyla çekiliyordu → 562k snapshot'ta dakikalar sürüyordu (Open-Meteo kapsama
  // girdikten sonra ~2 saate çıkıyordu). İki değişiklik: (1) çiftleme SQL JOIN'e çevrildi, (2) yazma tek
  // transaction'a alındı. Semantik aynı: gerçek = o (şehir,gün) için en son recorded_at_utc'li settlement.
  const insertPairs = db.prepare(`INSERT INTO training_pairs (station_id, provider, horizon_days, target_date, predicted_high_c, actual_high_c, actual_source) VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(station_id, provider, horizon_days, target_date) DO UPDATE SET predicted_high_c=excluded.predicted_high_c, actual_high_c=excluded.actual_high_c, actual_source=excluded.actual_source`);
  db.prepare('DELETE FROM training_pairs').run();
  const rows = db.prepare(`
    WITH truth AS (
      SELECT station_id, target_date, high_c,
             ROW_NUMBER() OVER (PARTITION BY station_id, target_date ORDER BY recorded_at_utc DESC) rn
      FROM settlements
    )
    SELECT fs.station_id, fs.provider, fs.horizon_days, fs.target_date, fs.predicted_high_c,
           t.high_c AS actual_high_c, 'settlement' AS actual_source
    FROM forecast_snapshots fs
    JOIN truth t ON t.station_id = fs.station_id AND t.target_date = fs.target_date AND t.rn = 1
    WHERE fs.horizon_days IN (0,1,2,3)
  `).all();
  const { bayat, taze } = await bayatAksilariBul(db);
  if (bayat.size) {
    console.log(`bayat akış (son ${BAYAT_AKIS_GUN} gündür snapshot yok, eğitime alınmadı): ${[...bayat].sort().join(', ')}`);
  }
  // Eğitilmeyen akışın ESKİ bias satırları da temizlenir: akış gidince hücreleri anlamsız kalır ve
  // geçmiş skorlamada aynı modeli iki kez oylatır. Akış geri gelirse satırlar yeniden üretilir.
  const temizlenecek = [...bayat].filter((p) => !EXCLUDED_PROVIDERS.has(p));
  let pairCnt = 0;
  let excludedCnt = 0;
  db.exec('BEGIN');
  try {
    for (const r of rows) {
      if (r.actual_high_c == null) continue;
      if (isExcluded(r.provider, bayat)) { excludedCnt++; continue; }
      insertPairs.run(r.station_id, r.provider, r.horizon_days, r.target_date, r.predicted_high_c, r.actual_high_c, r.actual_source);
      pairCnt++;
    }
    db.exec('COMMIT');
  } catch (e) {
    db.exec('ROLLBACK');
    throw e;
  }
  console.log(`training_pairs upserted ${pairCnt} (settlement'lı snapshot'lar; ${excludedCnt} bozuk/bayat kaynak atlandı)`);

  // Aggregate per station×provider×horizon
  const pairsAll = db.prepare(`SELECT station_id, provider, horizon_days, predicted_high_c, actual_high_c FROM training_pairs`).all();
  const pairs = pairsAll.filter(p => !isExcluded(p.provider, bayat));
  const groups = new Map();
  for (const p of pairs) {
    const key = `${p.station_id}|${p.provider}|${p.horizon_days}`;
    const g = groups.get(key) || { station_id: p.station_id, provider: p.provider, horizon_days: p.horizon_days, deltas: [], absErrs: [], sqErrs: [], hits: 0, n: 0, xs: [], ys: [] };
    const delta = p.predicted_high_c - p.actual_high_c;
    const absErr = Math.abs(delta);
    g.deltas.push(delta); g.absErrs.push(absErr); g.sqErrs.push(delta*delta);
    if (absErr <= 0.5) g.hits++;
    g.n++; g.xs.push(p.predicted_high_c); g.ys.push(p.actual_high_c);
    groups.set(key, g);
  }
  // Global per provider×horizon for shrinkage parent
  const parentByProvH = new Map();
  for (const g of groups.values()) {
    const pk = `${g.provider}|${g.horizon_days}`;
    const pg = parentByProvH.get(pk) || { deltas: [], n: 0 };
    pg.deltas.push(...g.deltas); pg.n += g.n;
    parentByProvH.set(pk, pg);
  }
  const parentMean = new Map();
  for (const [k, v] of parentByProvH) parentMean.set(k, v.deltas.reduce((a,b)=>a+b,0)/v.deltas.length);

  const biasRows = [];
  for (const g of groups.values()) {
    const bias = g.deltas.reduce((a,b)=>a+b,0)/g.n;
    const parentKey = `${g.provider}|${g.horizon_days}`;
    const parentBias = parentMean.get(parentKey) ?? 0;
    const biasShrunk = (g.n * bias + K_SHRINK * parentBias) / (g.n + K_SHRINK);
    const mae = g.absErrs.reduce((a,b)=>a+b,0)/g.n;
    const rmse = Math.sqrt(g.sqErrs.reduce((a,b)=>a+b,0)/g.n);
    const hitRate = g.hits / g.n;
    // linear a,b (OLS) y = a*x + b
    let a = null, b = null;
    if (g.n >= MIN_N) {
      const mx = g.xs.reduce((s,v)=>s+v,0)/g.n;
      const my = g.ys.reduce((s,v)=>s+v,0)/g.n;
      let num = 0, den = 0;
      for (let i=0;i<g.n;i++) { num += (g.xs[i]-mx)*(g.ys[i]-my); den += (g.xs[i]-mx)**2; }
      a = den ? num/den : 1;
      b = my - a*mx;
      if (!Number.isFinite(a) || !Number.isFinite(b)) { a = 1; b = 0; }
      // clamp a to [0.5,1.5] to avoid wild extrapolation with 15 samples
      a = Math.max(0.5, Math.min(1.5, a));
    }
    biasRows.push({ station_id: g.station_id, provider: g.provider, horizon_days: g.horizon_days, n: g.n, bias_c: bias, bias_shrunk_c: biasShrunk, a, b, mae_c: mae, rmse_c: rmse, hit_rate: hitRate, updated_at: new Date().toISOString() });
  }
  // Upsert
  const { upsertBiasParams, deleteBiasParamsForProviders } = await import('../windows-runner/db.mjs');
  upsertBiasParams(biasRows);
  const liveSilinen = temizlenecek.length ? deleteBiasParamsForProviders(temizlenecek) : 0;
  console.log(`bias_params upserted ${biasRows.length} groups (K=${K_SHRINK}) — LIVE (MOS buradan okur)`
    + (liveSilinen ? `; ${liveSilinen} eski satır silindi (öğrenilmeyen ${temizlenecek.length} akış)` : ''));

  // ---- Aynı sonucu MAIN'e de yaz (arşiv/rapor tarafı) --------------------------------------------
  // Neden: LIVE yalnız çalışan runner'ın deposu; raporlar (rapor_iskabet_t02, rapor_kalibrasyon_ml)
  // MAIN okur. Yalnız LIVE tazelenirse raporlar bayat bias görür — 2026-09-15'te tam bu olmuştu:
  // MAIN.bias_params 09-05'te donmuş, içinde tek günlük veriden hesaplanmış çöp satır vardı
  // (ör. ltfm/Visual Crossing h=1 → n=1). `db` zaten MAIN'e açık (yukarıda dbPath).
  const bkDir = process.env.HEAT_BACKUP_DIR || 'D:/JUNBO data/heat/main/db/backups';
  const bkPath = path.join(bkDir, `bias_params_pre_main_write_${new Date().toISOString().slice(0,10).replace(/-/g,'')}.sqlite`);
  try {
    fs.mkdirSync(bkDir, { recursive: true });
    const before = db.prepare('SELECT COUNT(*) n FROM bias_params').get().n;
    db.exec(`ATTACH DATABASE '${bkPath.replace(/'/g, "''")}' AS bk`);
    db.exec('DROP TABLE IF EXISTS bk.bias_params'); // aynı gün ikinci koşu kendini onarsın
    db.exec('CREATE TABLE bk.bias_params AS SELECT * FROM main.bias_params');
    const bkCount = db.prepare('SELECT COUNT(*) n FROM bk.bias_params').get().n;
    db.exec('DETACH DATABASE bk');
    if (bkCount !== before) throw new Error(`yedek sayısı uyuşmuyor (${bkCount} != ${before})`);
    console.log(`YEDEK: ${bkPath} (${bkCount} satır)`);
  } catch (e) {
    // Yedek alınamazsa MAIN'e yazma — arşivi yedeksiz bozmaktansa bayat bırakmak yeğdir.
    console.error(`MAIN yedeği alınamadı, MAIN'e yazılmadı: ${e.message}`);
    db.close();
    return;
  }
  // Tek günlük veriden doğmuş çöp satırları temizle (n < MIN_N). Bunlar hem yanıltıcı hem de MOS
  // kapalı olduğu için işlevsizdir; silinince "kaç hücre gerçekten öğrenildi" sorusu netleşir.
  const delStale = db.prepare('DELETE FROM bias_params WHERE n < ?').run(MIN_N);
  let delOlu = { changes: 0 };
  if (temizlenecek.length) {
    delOlu = db.prepare(`DELETE FROM bias_params WHERE provider IN (${temizlenecek.map(() => '?').join(',')})`).run(...temizlenecek);
  }
  const upMain = db.prepare(`INSERT INTO bias_params (station_id, provider, horizon_days, n, bias_c, bias_shrunk_c, a, b, mae_c, rmse_c, hit_rate, updated_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(station_id, provider, horizon_days) DO UPDATE SET
      n=excluded.n, bias_c=excluded.bias_c, bias_shrunk_c=excluded.bias_shrunk_c, a=excluded.a, b=excluded.b,
      mae_c=excluded.mae_c, rmse_c=excluded.rmse_c, hit_rate=excluded.hit_rate, updated_at=excluded.updated_at`);
  let mainWritten = 0;
  db.exec('BEGIN');
  try {
    for (const r of biasRows) {
      if (r.n < MIN_N) continue; // güven eşiğinin altındaki hücre MAIN'e yazılmaz
      upMain.run(r.station_id, r.provider, r.horizon_days, r.n, r.bias_c, r.bias_shrunk_c, r.a, r.b, r.mae_c, r.rmse_c, r.hit_rate, r.updated_at);
      mainWritten++;
    }
    db.exec('COMMIT');
  } catch (e) {
    db.exec('ROLLBACK');
    console.error('MAIN yazımı geri alındı:', e.message);
    db.close();
    return;
  }
  const staleNote = delStale.changes ? `, ${delStale.changes} zayıf (n<${MIN_N}) satır silindi` : '';
  const oluNote = delOlu.changes ? `, ${delOlu.changes} ölü akış satırı silindi` : '';
  console.log(`MAIN.bias_params: ${mainWritten} hücre yazıldı${staleNote}${oluNote}`);

  // Also compute global A weights (per provider) for ensemble_fixed vs B already per station
  const byProvider = new Map();
  for (const g of biasRows) {
    const p = g.provider;
    const e = byProvider.get(p) || { n:0, maeSum:0, hitSum:0 };
    e.n += g.n; e.maeSum += g.mae_c * g.n; e.hitSum += g.hit_rate * g.n;
    byProvider.set(p, e);
  }
  console.log('Global provider stats (for fixed ensemble):');
  for (const [prov, e] of byProvider) {
    const mae = e.maeSum / e.n;
    const hit = e.hitSum / e.n;
    const w = hit / Math.max(0.5, mae);
    console.log(`  ${prov}: n=${e.n} mae=${mae.toFixed(2)} hit=${(hit*100).toFixed(1)}% w~${w.toFixed(3)}`);
  }

  // Insert evaluation for both ensembles on last 14 days (simple)
  const evalRows = [];
  const today = new Date().toISOString().slice(0,10);
  for (const g of biasRows) {
    evalRows.push({ evaluated_at: today, horizon_days: g.horizon_days, ensemble_type: 'per_station', provider: g.provider, mae: g.mae_c, rmse: g.rmse_c, hit_rate: g.hit_rate, n: g.n });
  }
  // fixed ensemble evaluation: aggregate per horizon
  for (const horizon of [0,1,2,3]) {
    const slice = biasRows.filter(r=>r.horizon_days===horizon);
    if (!slice.length) continue;
    const mae = slice.reduce((s,r)=>s+r.mae_c*r.n,0)/slice.reduce((s,r)=>s+r.n,0);
    const hit = slice.reduce((s,r)=>s+r.hit_rate*r.n,0)/slice.reduce((s,r)=>s+r.n,0);
    const rmse = Math.sqrt(slice.reduce((s,r)=>s+r.rmse_c*r.rmse_c*r.n,0)/slice.reduce((s,r)=>s+r.n,0));
    const n = slice.reduce((s,r)=>s+r.n,0);
    evalRows.push({ evaluated_at: today, horizon_days: horizon, ensemble_type: 'fixed', provider: 'ensemble', mae, rmse, hit_rate: hit, n });
    evalRows.push({ evaluated_at: today, horizon_days: horizon, ensemble_type: 'learned_per_station', provider: 'ensemble', mae, rmse, hit_rate: hit, n });
  }
  const { insertEvaluationDaily } = await import('../windows-runner/db.mjs');
  insertEvaluationDaily(evalRows);
  console.log(`evaluation_daily upserted ${evalRows.length}`);

  // Write json artifact for runner API
  const outDir = process.env.HEAT_MODELS_DIR || path.join(path.resolve('D:\\JUNBO data'), 'models');
  fs.mkdirSync(outDir, { recursive: true });
  const outPath = path.join(outDir, `bias_params_${today}.json`);
  fs.writeFileSync(outPath, JSON.stringify({ generatedAt: new Date().toISOString(), k: K_SHRINK, providers: [...byProvider.keys()], bias_params: biasRows }, null, 2));
  console.log(`Wrote ${outPath}`);

  db.close();
}

main().catch(e=>{ console.error(e); process.exit(1); });
