#!/usr/bin/env node
// Ne işe yarar: Yeni bir Polymarket sıcaklık marketi çıktığında **hangi havalimanı istasyonunda çözüldüğünü**
//   bulur ve Heat'te karşılığı yoksa ÖNERİ üretir. Kural 13: "yeni şehir sessizce atlanmaz."
//
// NEDEN GEREKLİ (2026-09-15 ölçümü): kardeş botun `city_code` alanı tek başına güvenilir DEĞİL. Panama örneği:
//   kardeşte `MPTO` kodlu 3 max-sıcaklık marketi var, ama kendi `resolutionSource`'u
//   `https://www.weather.gov/wrh/timeseries?site=mpmg` diyor → gerçek istasyon **MPMG**, ki Heat'te zaten var.
//   Yani "eşleşmeyen kod" sanılan şey bozuk bir koddur, eksik şehir değil. Bu yüzden öncelik sırası:
//     1) resolutionSource URL'i  (WU: .../<ICAO>  ·  NWS WRH: ?site=<icao>)   ← en güvenilir
//     2) description/rules metni (site=xxxx veya bağlamında 4 harfli ICAO)
//     3) kardeşin city_code'u                                                ← yalnız son çare
//   Profesyonel botların yaptığı da budur: her market için yeniden parse et, sabit şehir listesine güvenme.
//   Aynı şehirde istasyon değişebilir (London EGLC vs EGLL, Paris LFPB vs LFPG) — bu yüzden karar ŞEHİR
//   adına değil ÇÖZÜM İSTASYONUNA göre verilir.
//
// KURALLAR: kardeş DB'ler SALT-OKUNUR (Kural 11) — onlara hiçbir şey yazılmaz. Yazma yalnız Heat'in
//   kendi tarafına: MAIN.stations (yedekli, tek INSERT, var olanı ezmez) ve config.json. Runner bir
//   sonraki döngüde config'i yeniden okuyup LIVE'a seed eder (Kural 9).
//
// **OTOMATİK EKLEME — VARSAYILAN OLARAK KAPALI.** İstasyon ekleme yalnız `--uygula` verildiğinde VE
//   aday BEŞ KAPININ HEPSİNDEN geçtiğinde yapılır. Tek kapı düşerse kayıt "şüpheli" olur, dosyaya
//   sebebiyle yazılır ve insan kararı bekler. Kapılar (her biri bir bozulma sınıfını kapatır):
//     G1 çözüm istasyonu `resolutionSource` URL'inden geldi + ÜLKE kodu o URL'den okundu
//        (ülke bilinmiyorsa eklenmez: `country` hangi toplayıcıların koşacağını belirler, ör. US→NWS)
//     G2 kardeşin `city_code`'u çözüm istasyonuyla ÇELİŞMİYOR (aynı şehirde iki farklı kod = MPTO/MPMG
//        sınıfı belirsizlik → otomatik ekleme yok)
//     G3 koordinat var ve en yakın Heat istasyonu ≥150 km (yakınsa "yeni şehir" değil, kapsanıyor)
//     G4 künye tamamlandı: zaman dilimi + yükseklik Open-Meteo'dan alındı (ikisi de NOT NULL)
//     G5 kalıcı market: ≥3 market kaydı (tek seferlik market yeni şehir taahhüdü değildir)
//
// Kullanım: node scripts/_sehir_cozucu.mjs [--kaynak gamma|kardesler] [--limit N] [--uygula]
//   --kaynak gamma     (varsayılan) canlı Gamma API: /events?active=true&closed=false&tag=weather
//   --kaynak kardesler  çevrimdışı: C:/HEAT data/db/kardesler.db junbo_weather_markets (test/doğrulama için)
//   --uygula            beş kapıyı geçenleri GERÇEKTEN ekler; verilmezse kuru koşu
//   HEAT_UNMATCHED_JSON, HEAT_ONERI_JSON, TEMP_MARKET_CONFIG ile dosya yolları değiştirilebilir.
import fs from 'node:fs';
import path from 'node:path';

const KAYNAK = (process.argv.find((a) => a.startsWith('--kaynak=')) || '').slice(9) || 'gamma';
const LIMIT = Number((process.argv.find((a) => a.startsWith('--limit=')) || '').slice(8)) || 300;
const MAIN_DB = process.env.HEAT_MAIN_DB || 'C:/HEAT data/db/tempmarket-edge.sqlite';
const KARDESLER_DB = process.env.KARDESLER_DB || 'C:/HEAT data/db/kardesler.db';
const UNMATCHED_JSON = process.env.HEAT_UNMATCHED_JSON || 'C:/HEAT data/state/unmatched_cities.json';
const ONERI_JSON = process.env.HEAT_ONERI_JSON || 'C:/HEAT data/state/sehir_onerileri.json';
// İstasyon çözümleme mantığı _istasyon_coz.mjs'te — geri doldurma betiğiyle ORTAK (tek yerde dursun).
import { istasyonCoz } from './_istasyon_coz.mjs';

const norm = (s) => String(s || '').toLowerCase().replace(/[^a-zçğıöşü ]/gi, ' ').replace(/\s+/g, ' ').trim();

// ---- 1) Market kayıtlarını topla ----
async function gammaMarketleri() {
  const url = 'https://gamma-api.polymarket.com/events?active=true&closed=false&tag=weather&limit=' + LIMIT;
  const r = await fetch(url, { headers: { 'user-agent': 'heat-research/1.0' } });
  if (!r.ok) throw new Error(`Gamma HTTP ${r.status}`);
  const j = await r.json();
  const out = [];
  for (const e of Array.isArray(j) ? j : []) {
    for (const m of e.markets || []) {
      out.push({
        question: m.question || e.title, city: e.title || '', city_code: '',
        resolutionSource: m.resolutionSource || '', description: m.description || '',
        slug: m.slug, conditionId: m.conditionId, endDate: m.endDate, metric: 'bilinmiyor',
      });
    }
  }
  return out;
}
async function kardesMarketleri() {
  const { DatabaseSync } = await import('node:sqlite');
  const k = new DatabaseSync(KARDESLER_DB, { readOnly: true }); // Kural 11: kardeş DB salt-okunur
  const rows = k.prepare(`SELECT question, city, city_code, raw_data, metric, status
                            FROM junbo_weather_markets LIMIT ?`).all(LIMIT * 10);
  k.close();
  const out = [];
  for (const r of rows) {
    let j = {}; try { j = JSON.parse(r.raw_data); } catch {}
    out.push({
      question: r.question, city: r.city, city_code: r.city_code, metric: r.metric,
      resolutionSource: j.resolutionSource || '', description: j.description || '',
      slug: j.slug, conditionId: j.conditionId, endDate: j.endDate,
    });
  }
  return out;
}

async function main() {
  const { DatabaseSync } = await import('node:sqlite');
  let markets;
  if (KAYNAK === "kardesler") markets = await kardesMarketleri();
  else {
    try { markets = await gammaMarketleri(); }
    catch (e) {
      console.error(`Gamma okunamadı (${e.message}) — çevrimdışı kardeş kaynağına düşülüyor.`);
      console.error('NOT: bu bir geri düşüş; runner ağı varsa --kaynak gamma ile canlı çalışır.');
      markets = await kardesMarketleri();
    }
  }
  console.log(`kaynak: ${KAYNAK} · ${markets.length} market kaydı okundu`);

  // ---- 2) Heat istasyonları (salt-okunur) ----
  const db = new DatabaseSync(MAIN_DB, { readOnly: true });
  const byIcao = new Map(), byId = new Map(), byCity = new Map();
  for (const s of db.prepare('SELECT id, icao, city, country, latitude, longitude FROM stations').all()) {
    byIcao.set(String(s.icao).toUpperCase(), s);
    byId.set(String(s.id).toLowerCase(), s);
    const c = norm(s.city);
    if (c) byCity.set(c, [...(byCity.get(c) || []), s]);
  }
  db.close();
  console.log(`Heat: ${byIcao.size} istasyon`);

  // ---- 3) Her marketi sınıflandır (şehir × çözüm istasyonu bazında tekilleştir) ----
  const hucreler = new Map();
  for (const m of markets) {
    const coz = istasyonCoz(m);
    if (!coz) continue;
    const key = `${coz.icao}|${norm(m.city)}`;
    const h = hucreler.get(key) || { icao: coz.icao, cozKaynak: coz.kaynak, ulke: coz.ulke || null, cityAdi: m.city, kodlar: new Set(), sorular: [], n: 0 };
    h.n++;
    if (!h.ulke && coz.ulke) h.ulke = coz.ulke;
    if (m.city_code) h.kodlar.add(String(m.city_code).toUpperCase());
    if (h.sorular.length < 2) h.sorular.push(String(m.question || '').slice(0, 90));
    hucreler.set(key, h);
  }

  const sonuc = { tamam: [], kodYanlis: [], sehirVarIstasyonYok: [], yeniSehir: [] };
  for (const h of hucreler.values()) {
    const hedef = byIcao.get(h.icao) || byId.get(h.icao.toLowerCase());
    const sehirEslesen = byCity.get(norm(h.cityAdi)) || [];
    const kayit = {
      cozumIcao: h.icao, cozumKaynagi: h.cozKaynak, ulke: h.ulke || null, marketSehri: h.cityAdi || '-',
      kardesKodlari: [...h.kodlar].join(',') || '-', marketSayisi: h.n, ornekSoru: h.sorular[0] || '-',
    };
    if (hedef) {
      // Çözüm istasyonu Heat'te VAR. Kardeşin kodu farklıysa kardeş yanlış yazmış (MPTO→MPMG örneği).
      const fark = [...h.kodlar].filter((k) => k !== h.icao);
      if (fark.length) sonuc.kodYanlis.push({ ...kayit, heatIstasyon: hedef.icao, heatId: hedef.id, kardesYanlisKod: fark.join(',') });
      else sonuc.tamam.push({ ...kayit, heatIstasyon: hedef.icao, heatId: hedef.id });
    } else if (sehirEslesen.length) {
      sonuc.sehirVarIstasyonYok.push({ ...kayit, heatSehri: sehirEslesen.map((s) => `${s.icao}(${s.id})`).join('/'),
        mesafeNotu: 'aynı şehir, farklı istasyon olabilir — Polymarket hangisinde çözüyorsa Heat onu izlemeli' });
    } else {
      sonuc.yeniSehir.push(kayit);
    }
  }

  // ---- 3b) YENİ ŞEHİR: BEŞ KAPI + (--uygula ile) OTOMATİK EKLEME ----
  // Kural 13: yeni şehir sessizce atlanmaz. Ama "öneri" ile "ekleme" arasındaki fark bilinçlidir:
  // yanlış istasyon eşlemesi SESSİZ BOZULMA üretir (MPTO→MPMG: kardeşin kodu yanlış, gerçek istasyon
  // başka). Bu yüzden ekleme yalnız BEŞ KAPI BİRDEN geçerse yapılır; tek kapı düşerse kayıt
  // "şüpheli" olur ve insan kararı bekler. Kapıların her biri tek başına bir bozulma sınıfını kapatır.
  const UYGULA = process.argv.includes('--uygula');
  const kd = new DatabaseSync(KARDESLER_DB, { readOnly: true }); // Kural 11: kardeş DB salt-okunur
  const koord = new Map();      // ICAO -> {lat, lon}   (kardeşin kendi koordinatı)
  const kodBySehir = new Map(); // normalize şehir adı -> Set(kardeş city_code)
  for (const r of kd.prepare(`SELECT city, city_code, latitude, longitude FROM junbo_weather_markets
                               WHERE metric='temperature_max' AND city_code IS NOT NULL`).all()) {
    const kod = String(r.city_code).toUpperCase();
    if (Number.isFinite(r.latitude) && Number.isFinite(r.longitude) && (r.latitude || r.longitude) && !koord.has(kod)) {
      koord.set(kod, { lat: r.latitude, lon: r.longitude });
    }
    const n = norm(r.city);
    if (n) kodBySehir.set(n, (kodBySehir.get(n) || new Set()).add(kod));
  }
  kd.close();

  const rad = (d) => (d * Math.PI) / 180;
  function mesafeKm(aLat, aLon, bLat, bLon) {
    const dLat = rad(bLat - aLat); const dLon = rad(bLon - aLon);
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(aLat)) * Math.cos(rad(bLat)) * Math.sin(dLon / 2) ** 2;
    return 2 * 6371 * Math.asin(Math.sqrt(h));
  }
  // Bu mesafeden yakınsa "yeni şehir" değil, zaten izlediğimiz bir istasyonun civarıdır → ekleme.
  const EN_YAKIN_KM = 150;
  function enYakinIstasyon(lat, lon) {
    let en = null;
    for (const s of byIcao.values()) {
      if (!Number.isFinite(s.latitude) || !Number.isFinite(s.longitude)) continue;
      const km = mesafeKm(lat, lon, s.latitude, s.longitude);
      if (!en || km < en.km) en = { km, s };
    }
    return en;
  }
  // `stations.timezone` ve `elevation_m` NOT NULL — künye tamamlanamazsa istasyon EKLENMEZ.
  // ZAMAN DİLİMİ UYDURULAMAZ: `timezone` her tahminin `targetDate` ve `horizonDays`'ini belirler
  // (sources.mjs `localDate`/`horizon`) — yanlış dilim TÜM satırları sessizce yanlış etiketler.
  // Bu yüzden boylamdan kaba tahmin YASAK; yalnız gerçek kaynaktan (Open-Meteo) alınır.
  // 429 burada olağan (ücretsiz katman, runner da aynı IP'den çağırıyor) — geri çekilmeli deneme şart,
  // yoksa her aday "şüpheli"ye düşer ve otomatik ekleme sessizce hiç çalışmaz (2026-09-15'te görüldü).
  // Künye İKİ BAĞIMSIZ servisten alınır. Tek servise bağlanmak yanlış olurdu: Open-Meteo bu projede
  // zaten yoğun kullanılıyor ve 429'u TEKRARLAYAN bir durum (2026-09-12'den beri runner logunda 15 kez
  // "Open-Meteo: DISABLED (429)"). Tek kaynaklı olsaydı otomatik ekleme her gün sessizce düşerdi.
  // Elde edilemezse aday KALICI "şüpheli" olmaz — "beklemede" olur ve bir sonraki koşuda yeniden denenir.
  const BEKLEME = Number(process.env.HEAT_OM_BEKLEME_MS || 2500);
  async function getirJson(url, etiket) {
    let sonHata;
    for (let deneme = 0; deneme < 3; deneme++) {
      if (deneme) await new Promise((r) => setTimeout(r, BEKLEME * deneme));
      try {
        const r = await fetch(url, { headers: { 'user-agent': 'heat-research/1.0' } });
        if (!r.ok) { sonHata = new Error(`${etiket} HTTP ${r.status}`); continue; }
        return await r.json();
      } catch (e) { sonHata = e; }
    }
    throw sonHata || new Error(`${etiket} yanıt vermedi`);
  }
  async function kunye(lat, lon) {
    // (a) Open-Meteo: zaman dilimi + yükseklik BİRLİKTE (asıl kaynak)
    try {
      const j = await getirJson(`https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&timezone=auto&forecast_days=1`, 'Open-Meteo');
      if (j.timezone && Number.isFinite(j.elevation)) return { timezone: j.timezone, elevationM: j.elevation, kaynak: 'Open-Meteo' };
    } catch { /* yedeklere düşülür */ }
    // (b) zaman dilimi yedeği — bağımsız servis
    const j2 = await getirJson(`https://timeapi.io/api/timezone/coordinate?latitude=${lat}&longitude=${lon}`, 'timeapi.io');
    const tz = j2.timeZone || j2.timezone;
    if (!tz || !/^[A-Za-z]+\//.test(tz)) throw new Error('zaman dilimi alınamadı (IANA adı yok)');
    // (c) yükseklik yedeği — bağımsız servis. UYDURULMAZ: alınamazsa aday beklemede kalır.
    const j3 = await getirJson(`https://api.opentopodata.org/v1/srtm90m?locations=${lat},${lon}`, 'opentopodata');
    const el = j3?.results?.[0]?.elevation;
    if (!Number.isFinite(el)) throw new Error('yükseklik alınamadı');
    return { timezone: tz, elevationM: el, kaynak: 'timeapi.io + opentopodata (yedek)' };
  }

  const eklenen = [];    // beş kapıyı geçenler (--uygula ile yazıldı)
  const supheli = [];    // kapı düşenler — SEBEBİYLE birlikte, insan kararı için
  const beklemede = [];  // kapılar geçti ama künye alınamadı (ağ/kota) — bir sonraki koşuda yeniden denenir
  for (const r of sonuc.yeniSehir) {
    const kod = r.cozumIcao;
    const k = koord.get(kod);
    const sehirKodlari = [...(kodBySehir.get(norm(r.marketSehri)) || [])];
    const celisen = sehirKodlari.filter((c) => c !== kod);
    const yakin = k ? enYakinIstasyon(k.lat, k.lon) : null;

    const kapilar = [
      { ad: 'G1 çözüm kaynağı + ülke',
        gecti: r.cozumKaynagi.startsWith('resolutionSource') && !!r.ulke,
        not: r.ulke ? r.cozumKaynagi : `${r.cozumKaynagi} · ülke kodu okunamadı` },
      { ad: 'G2 kardeş kodu çelişmiyor', gecti: celisen.length === 0,
        not: celisen.length ? `aynı şehirde farklı kod: ${celisen.join(',')} (MPTO/MPMG sınıfı belirsizlik)` : 'çelişki yok' },
      { ad: `G3 şehir kapsanmıyor (≥${EN_YAKIN_KM} km)`, gecti: !!k && !!yakin && yakin.km >= EN_YAKIN_KM,
        not: !k ? 'kardeşte koordinat yok' : (yakin ? `en yakın ${yakin.s.icao} ${yakin.km.toFixed(0)} km` : 'Heat istasyonu yok') },
      { ad: 'G5 kalıcı market (≥3)', gecti: r.marketSayisi >= 3, not: `${r.marketSayisi} market` },
    ];
    const dusen = kapilar.filter((x) => !x.gecti);
    if (dusen.length) {
      supheli.push({ ...r, kapilar, dusenKapilar: dusen.map((x) => x.ad) });
      continue;
    }

    // G4 yalnız diğerleri geçince sorulur (ağ çağrısı — boşuna yapılmaz).
    // Ağ/kota hatası burada KALICI şüpheli değil: ertesi koşuda yeniden denenir (bkz. beklemede).
    let kn;
    try { kn = await kunye(k.lat, k.lon); }
    catch (e) { beklemede.push({ ...r, kapilar, sebep: `G4 künye alınamadı: ${e.message}` }); continue; }
    kapilar.push({ ad: 'G4 künye tamam', gecti: true, not: `${kn.timezone} · ${kn.elevationM} m · ${kn.kaynak}` });

    const kayit = {
      icao: kod, city: r.marketSehri || kod, country: r.ulke,
      latitude: k.lat, longitude: k.lon, elevationM: kn.elevationM, timezone: kn.timezone,
      settlementSource: 'Polymarket resolutionSource', marketSlugs: [],
    };
    eklenen.push({ ...kayit, kapilar, marketSayisi: r.marketSayisi });
    if (UYGULA) {
      const sonucYaz = await istasyonEkle(kayit);
      eklenen[eklenen.length - 1].yazildi = sonucYaz;
    }
  }

  async function istasyonEkle(kayit) {
    const id = kayit.icao.toLowerCase();
    // 1) MAIN.stations (Kural 9: script → MAIN). Önce yedek; yazma tek INSERT, var olanı EZMEZ.
    const bk = path.join(path.dirname(MAIN_DB), 'backups', `stations_pre_ekleme_${new Date().toISOString().slice(0, 10).replace(/-/g, '')}.sqlite`);
    const w = new DatabaseSync(MAIN_DB);
    let mainYazildi = false;
    try {
      w.exec('PRAGMA busy_timeout=30000;');
      fs.mkdirSync(path.dirname(bk), { recursive: true });
      w.exec(`ATTACH DATABASE '${bk.replace(/'/g, "''")}' AS bk`);
      w.exec('DROP TABLE IF EXISTS bk.stations');
      w.exec('CREATE TABLE bk.stations AS SELECT * FROM main.stations');
      w.exec('DETACH DATABASE bk');
      const varMi = w.prepare('SELECT 1 x FROM stations WHERE UPPER(icao)=?').get(kayit.icao);
      if (!varMi) {
        w.prepare(`INSERT INTO stations (id, icao, city, country, latitude, longitude, elevation_m, timezone, settlement_source, market_slug, enabled, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,1,?)`).run(
          id, kayit.icao, kayit.city, kayit.country, kayit.latitude, kayit.longitude,
          kayit.elevationM, kayit.timezone, kayit.settlementSource, null, new Date().toISOString());
        mainYazildi = true;
      }
    } catch (e) { console.error(`  MAIN'e yazılamadı (${kayit.icao}): ${e.message}`); }
    finally { w.close(); }
    // 2) config.json — runner bir sonraki döngüde okuyup LIVE'a seed eder (Kural 9: runner → LIVE).
    const cfgYolu = process.env.TEMP_MARKET_CONFIG || path.resolve('windows-runner/config.json');
    try {
      const cfg = JSON.parse(fs.readFileSync(cfgYolu, 'utf8'));
      if (!cfg.stations.some((s) => String(s.icao).toUpperCase() === kayit.icao)) {
        cfg.stations.push({ id, icao: kayit.icao, city: kayit.city, country: kayit.country,
          latitude: kayit.latitude, longitude: kayit.longitude, elevationM: kayit.elevationM,
          timezone: kayit.timezone, settlementSource: kayit.settlementSource, marketSlug: null, marketSlugs: [] });
        fs.writeFileSync(cfgYolu, `${JSON.stringify(cfg, null, 2)}\n`, 'utf8');
      }
    } catch (e) { console.error(`  config.json güncellenemedi (${kayit.icao}): ${e.message}`); }
    return { mainYazildi, yedek: bk };
  }

  // Eşleşmeyen kod defteri (köprüden) hâlâ çözülmemiş mi?
  let oncekiKodlar = {};
  try { oncekiKodlar = JSON.parse(fs.readFileSync(UNMATCHED_JSON, 'utf8')).kodlar || {}; } catch {}
  const cozulmus = new Set([...sonuc.tamam, ...sonuc.kodYanlis].map((r) => r.cozumIcao));
  // "Açıklanmış" kod = kardeşin yanlış yazdığı, gerçeği Heat'te ZATEN olan kod (MPTO→MPMG). Bunlar eksik
  // şehir değil; defterde "çözülmemiş" diye durmaları yanıltıcı olurdu.
  const aciklanan = new Map();
  for (const r of sonuc.kodYanlis) for (const kod of String(r.kardesYanlisKod).split(',')) if (kod) aciklanan.set(kod, r.cozumIcao);
  const halaBilinmeyen = Object.keys(oncekiKodlar).filter((k) => !cozulmus.has(k) && !byIcao.has(k) && !aciklanan.has(k));

  // ---- 4) Rapor + öneri dosyası ----
  console.log('');
  console.log(`  çözüm istasyonu Heat'te VAR        : ${sonuc.tamam.length}`);
  console.log(`  kardeşin kodu YANLIŞ (Heat doğru)  : ${sonuc.kodYanlis.length}`);
  for (const r of sonuc.kodYanlis) console.log(`      ${r.kardesYanlisKod} → gerçek ${r.cozumIcao} (Heat: ${r.heatIstasyon}/${r.heatId}, ${r.marketSayisi} market) · kaynak: ${r.cozumKaynagi}`);
  console.log(`  ŞEHRİ VAR ama istasyonu yok (ÖNERİ): ${sonuc.sehirVarIstasyonYok.length}`);
  for (const r of sonuc.sehirVarIstasyonYok) console.log(`      ${r.marketSehri} → çözüm ${r.cozumIcao} · Heat'te ${r.heatSehri} · ${r.marketSayisi} market`);
  console.log(`  HİÇ OLMAYAN ŞEHİR (ÖNERİ)         : ${sonuc.yeniSehir.length}`);
  for (const r of sonuc.yeniSehir) console.log(`      ${r.marketSehri} → ${r.cozumIcao} · ${r.marketSayisi} market · ${r.ornekSoru}`);
  for (const [kod, gercek] of aciklanan) console.log(`  köprü defterindeki ${kod} AÇIKLANDI: kardeşin kodu yanlış, gerçek istasyon ${gercek} — Heat'te ZATEN var, eksik şehir değil.`);
  if (halaBilinmeyen.length) console.log(`  köprü defterinde hâlâ çözülmemiş kod: ${halaBilinmeyen.join(', ')}`);
  console.log('');
  console.log(`  BEŞ KAPIYI GEÇEN (eklenebilir)    : ${eklenen.length}${UYGULA ? '  → --uygula verildi' : '  (kuru koşu — yazılmadı)'}`);
  for (const r of eklenen) console.log(`      ✓ ${r.icao} ${r.city} (${r.country}) ${r.latitude.toFixed(3)},${r.longitude.toFixed(3)} · ${r.timezone} · ${r.elevationM} m · ${r.marketSayisi} market${r.yazildi ? ` · MAIN:${r.yazildi.mainYazildi ? 'yazıldı' : 'zaten vardı'} config:güncellendi` : ''}`);
  console.log(`  ŞÜPHELİ (insan kararı)            : ${supheli.length}`);
  for (const r of supheli) console.log(`      ? ${r.cozumIcao} ${r.marketSehri} — ${r.dusenKapilar.join(' | ')}`);
  if (beklemede.length) {
    console.log(`  BEKLEMEDE (künye alınamadı)       : ${beklemede.length} — bir sonraki koşuda yeniden denenecek, kaybolmaz`);
    for (const r of beklemede) console.log(`      … ${r.cozumIcao} ${r.marketSehri} — ${r.sebep}`);
  }

  const oneriler = [...sonuc.sehirVarIstasyonYok, ...sonuc.yeniSehir];
  fs.mkdirSync(path.dirname(ONERI_JSON), { recursive: true });
  fs.writeFileSync(ONERI_JSON, JSON.stringify({
    guncelleme: new Date().toISOString(),
    kaynak: KAYNAK,
    aciklama: 'Heat\'te istasyonu olmayan AMA gerçek bir çözüm istasyonu olan marketler. Beş kapıyı geçenler --uygula ile otomatik eklenir; kalanlar insan onayı bekler (yanlış eşleme sessiz bozulma üretir; bkz. MPTO→MPMG).',
    uygulandi: UYGULA,
    oneriSayisi: oneriler.length,
    oneriler,
    besKapiGecenler: eklenen,
    supheliler: supheli,
    beklemede,
    kardesKoduYanlis: sonuc.kodYanlis,
    aciklananKodlar: Object.fromEntries(aciklanan),
    halaBilinmeyenKodlar: halaBilinmeyen,
  }, null, 2), 'utf8');
  console.log(`\nyazıldı: ${ONERI_JSON} (${oneriler.length} öneri · ${eklenen.length} eklenebilir · ${supheli.length} şüpheli · ${beklemede.length} beklemede)`);
}

main().catch((e) => { console.error(e); process.exit(1); });
