// Ne işe yarar: ÜÇ PROJENİN ORTAK Open-Meteo önbelleği. Aynı OM isteği bir kez çekilir, yanıtı diskte
// tutulur; başka bir süreç (ya da ileride başka bir proje) aynı isteği sorduğunda ağa hiç çıkılmaz.
//
// NEDEN: Open-Meteo kotaları **IP başına** ve bu makinede Heat + ASIAbot + junbo aynı IP'yi paylaşıyor.
// Ölçüm (2026-09-20): OM "Daily API request limit exceeded" döndü, ama Heat'in o günkü TÜM OM trafiği
// 416 istek — kotanın (10.000) yanında hiçbir şey. Yani kotayı başkaları dolduruyor, faturayı biz
// ödüyoruz: canlı OM günde 4 turun 2'sini kaybediyordu. Ayrıntı: ORTAK_OM_ONBELLEK.md
//
// DB: C:/HEAT data/db/om_cache.sqlite (OM_CACHE_DB ile değiştirilebilir). Üç proje okur; şimdilik
// yalnız Heat yazar. Şema ve ANAHTAR KURALI ORTAK_OM_ONBELLEK.md §2'de — kardeş proje aynı anahtarı
// üretebilsin diye kural orada birebir yazılı. Buradaki omAnahtar() değişirse orası da değişmeli.
//
// 2026-10-09 TAŞINDI: D:/ORTAK data/db → C:/HEAT data/db. SEBEP ÖLÇÜLDÜ: D: fiziksel arızalı USB
// disk (30.09'da MAIN ve kardesler.db bu yüzden C:'ye taşınmıştı; bu dosya geride kalmıştı). Belirti:
// 25.09'dan itibaren yalnız bu önbelleği KULLANAN iki kaynak (Open-Meteo canlı + WeatherNext) SQLite
// "disk I/O error" veriyordu — 751 hata (450 Open-Meteo + 300 WeatherNext + 1 budama adımı); başka
// hiçbir kaynak etkilenmiyordu, çünkü omMu() yalnız open-meteo adreslerinde devreye girer.
// LIVE DB yazılabilir durumdaydı, yani hata bu dosyadan geliyordu.
// 07.10 12:43 → 09.10 arası Open-Meteo + WeatherNext HİÇ toplanamadı. Kardeş projeler kendi kodlarında
// hâlâ D:'yi gösteriyor olabilir; Heat artık D:'ye HİÇ dokunmaz.
//
// İKİ DÜRÜSTLÜK KURALI (ORTAK_OM_ONBELLEK.md §3):
//   1) Önbellekten gelen istek source_requests'e YAZILMAZ — yazılırsa "gerçek OM kullanımı" ölçümü
//      şişer (o tablo 2026-09-20'de kotayı teşhis etmek için kullanıldı). Kanıt om_cache_gun'da durur.
//   2) Çağıran, verinin GERÇEK çekim anını alır ("şimdi" değil): önbellekten gelen tahmin
//      forecast_snapshots'a yazılırken run_at_utc = ilk çekim anı olur. "O bilgiyi ne zaman edindik"
//      sorusuna muhafazakâr cevap — bu depoda bayat veri affedilmez.
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { createHash } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';

const dbYolu = process.env.OM_CACHE_DB || 'C:/HEAT data/db/om_cache.sqlite';
fs.mkdirSync(path.dirname(dbYolu), { recursive: true });

let db = null;
function ac() {
  if (db) return db;
  db = new DatabaseSync(dbYolu);
  // WAL + busy_timeout: aynı dosyayı üç süreç okuyacak/yazacak; kilit yüzünden çökmemeli.
  db.exec(`
    PRAGMA journal_mode = WAL;
    PRAGMA busy_timeout = 5000;
    CREATE TABLE IF NOT EXISTS om_cache (
      key            TEXT PRIMARY KEY,
      url            TEXT NOT NULL,
      aile           TEXT NOT NULL,
      model          TEXT,
      lat_r          REAL,
      lon_r          REAL,
      fetched_at_utc TEXT NOT NULL,
      status_code    INTEGER,
      payload        BLOB NOT NULL,
      payload_bayt   INTEGER NOT NULL,
      hit            INTEGER NOT NULL DEFAULT 0,
      son_hit_utc    TEXT,
      kaynak         TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_om_cache_cekim ON om_cache(fetched_at_utc);
    CREATE TABLE IF NOT EXISTS om_cache_gun (
      gun        TEXT PRIMARY KEY,
      kurtarilan INTEGER NOT NULL DEFAULT 0,
      yazilan    INTEGER NOT NULL DEFAULT 0
    );
  `);
  return db;
}

export function omOnbellekKapat() {
  try { if (db) db.close(); } catch {}
  db = null;
}

export function omDbYolu() {
  return dbYolu;
}

const OM_KONAK = /(^|\.)open-meteo\.com$/i;

export function omMu(url) {
  try { return OM_KONAK.test(new URL(url).host); } catch { return false; }
}

export function omAile(url) {
  const h = new URL(url).host.toLowerCase();
  if (h.startsWith('ensemble-api')) return 'ensemble';
  if (h.startsWith('archive-api')) return 'archive';
  if (h.startsWith('customer-api')) return 'customer';
  if (h.startsWith('api')) return 'forecast';
  return 'diger';
}

// ANAHTAR KURALI — ORTAK_OM_ONBELLEK.md §2 ile BİREBİR aynı olmalı:
//   host (küçük harf) + path + "?" + parametreler
//   parametreler: ad küçük harfe indirilir, `apikey`/`key` ATILIR, değerler yüzde-ÇÖZÜLMÜŞ hâlde,
//   ADA GÖRE sıralanır (eşitlikte değere göre), `latitude`/`longitude` 2 ondalığa yuvarlanır.
//   Yuvarlama bilinçli (~1,1 km): OM ızgarası 0,25° ≈ 25 km, yani bu fark gürültü — ve kardeş
//   projeler farklı koordinat yazdığı için paylaşımı mümkün kılan tek şey bu.
export function omAnahtar(url) {
  const u = new URL(url);
  const p = [];
  for (const [k, v] of u.searchParams) {
    const ad = k.toLowerCase();
    if (ad === 'apikey' || ad === 'key') continue;
    let deger = v;
    if (ad === 'latitude' || ad === 'longitude') {
      const n = Number(v);
      if (Number.isFinite(n)) deger = n.toFixed(2);
    }
    p.push([ad, deger]);
  }
  p.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0));
  const sorgu = p.map(([k, v]) => `${k}=${v}`).join('&');
  return createHash('sha256').update(`${u.host.toLowerCase()}${u.pathname}?${sorgu}`).digest('hex');
}

function gunSayaci(tur, n = 1) {
  const kolon = tur === 'hit' ? 'kurtarilan' : 'yazilan';
  ac().prepare(`INSERT INTO om_cache_gun (gun, kurtarilan, yazilan) VALUES (?, ?, ?)
      ON CONFLICT(gun) DO UPDATE SET ${kolon} = ${kolon} + ?`)
    .run(new Date().toISOString().slice(0, 10), tur === 'hit' ? n : 0, tur === 'hit' ? 0 : n, n);
}

// Sütun tanımı gereği UTC'dir. AMA kardeş bir proje damgayı saat dilimi EKLEMEDEN yazarsa
// (`2026-09-20T19:07:36.610253`) JS onu **yerel saat** sayar → 3 saat kayma → taze satır "bayat"
// görünür ve önbellek sessizce hiç tutmaz. Ölçüldü 2026-09-20: ASIAbot'un yazdığı satır kendisine
// 10.800 sn (tam 3 saat) yaşlı görünüyordu. Damgayı açıkça UTC'ye sabitliyoruz.
function cekimAniMs(damga) {
  let s = String(damga ?? '').trim().replace(' ', 'T');
  if (!s) return NaN;
  if (!/([zZ]|[+-]\d{2}:?\d{2})$/.test(s)) s += 'Z';
  return Date.parse(s);
}

// Taze veri varsa döner; yoksa null (çağıran ağa çıkar). `azamiYasMs` çağıranın kararı — önbellek
// yaşı kendi başına "taze" demez, sadece gerçek çekim anını bildirir.
export function omOnbellekAl(url, { azamiYasMs = 60 * 60 * 1000 } = {}) {
  if (!omMu(url)) return null;
  const d = ac();
  const k = omAnahtar(url);
  const satir = d.prepare('SELECT payload, fetched_at_utc, hit FROM om_cache WHERE key = ?').get(k);
  if (!satir) return null;
  const yasMs = Date.now() - cekimAniMs(satir.fetched_at_utc);
  if (!Number.isFinite(yasMs) || yasMs > azamiYasMs) return null;
  let veri;
  try { veri = JSON.parse(zlib.gunzipSync(satir.payload).toString('utf8')); } catch { return null; }
  d.prepare('UPDATE om_cache SET hit = hit + 1, son_hit_utc = ? WHERE key = ?').run(new Date().toISOString(), k);
  gunSayaci('hit');
  return { veri, fetchedAtUtc: satir.fetched_at_utc, yasMs };
}

// Ham yük gzip'lenerek saklanır — düz JSON yazsaydık `forecast_snapshots`'ın 2 GB'lık (%
// 91'i raw_json) hâline dönerdik; aynı hatayı önbellekte tekrarlamıyoruz.
export function omOnbellekKoy(url, veri, { kaynak = 'Heat', statusCode = 200 } = {}) {
  if (!omMu(url) || veri == null) return false;
  const d = ac();
  const k = omAnahtar(url);
  const u = new URL(url);
  const metin = JSON.stringify(veri);
  const govde = zlib.gzipSync(Buffer.from(metin, 'utf8'));
  const enlem = Number(u.searchParams.get('latitude'));
  const boylam = Number(u.searchParams.get('longitude'));
  d.prepare(`INSERT INTO om_cache (key, url, aile, model, lat_r, lon_r, fetched_at_utc, status_code, payload, payload_bayt, kaynak)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(key) DO UPDATE SET
        url = excluded.url, aile = excluded.aile, model = excluded.model,
        lat_r = excluded.lat_r, lon_r = excluded.lon_r,
        fetched_at_utc = excluded.fetched_at_utc, status_code = excluded.status_code,
        payload = excluded.payload, payload_bayt = excluded.payload_bayt, kaynak = excluded.kaynak`)
    .run(k, url, omAile(url), u.searchParams.get('models'),
      Number.isFinite(enlem) ? enlem : null, Number.isFinite(boylam) ? boylam : null,
      new Date().toISOString(), statusCode, govde, Buffer.byteLength(metin, 'utf8'), kaynak);
  gunSayaci('put');
  return true;
}

export function omOnbellekBoyutMb() {
  const g = ac().prepare('SELECT SUM(length(payload)) n FROM om_cache').get();
  return Number(g?.n || 0) / 1048576;
}

// Yaş sınırı + boyut tavanı. Boyut aşımında önce ÇOK VURMAYAN ve ESKİ satırlar atılır — çok vuran
// satırlar tam da kotayı kurtaranlar, onları silmek kendi ayağına sıkmak olurdu.
export function omOnbellekBuda({ azamiYasGun = 14, azamiMb = 500 } = {}) {
  const d = ac();
  const sinir = new Date(Date.now() - azamiYasGun * 86400000).toISOString();
  const yas = d.prepare('DELETE FROM om_cache WHERE fetched_at_utc < ?').run(sinir);
  let boyut = omOnbellekBoyutMb();
  let boyutAtilan = 0;
  if (boyut > azamiMb) {
    const del = d.prepare('DELETE FROM om_cache WHERE key = ?');
    for (const r of d.prepare('SELECT key, length(payload) n FROM om_cache ORDER BY hit ASC, fetched_at_utc ASC LIMIT 5000').all()) {
      if (boyut <= azamiMb) break;
      del.run(r.key);
      boyut -= Number(r.n) / 1048576;
      boyutAtilan += 1;
    }
  }
  return { yaslaSilinen: Number(yas.changes || 0), boyuttanSilinen: boyutAtilan, kalanMb: omOnbellekBoyutMb() };
}

export function omOnbellekIstatistik() {
  const d = ac();
  const g = d.prepare(`SELECT COUNT(*) satir,
      COALESCE(SUM(hit), 0) hit, COALESCE(SUM(payload_bayt), 0) hamBayt,
      COALESCE(SUM(length(payload)), 0) sikisikBayt,
      MIN(fetched_at_utc) ilk, MAX(fetched_at_utc) son FROM om_cache`).get();
  const aileler = d.prepare('SELECT aile, COUNT(*) n, COALESCE(SUM(hit),0) hit FROM om_cache GROUP BY aile ORDER BY n DESC').all();
  const modeller = d.prepare('SELECT model, COUNT(*) n, COALESCE(SUM(hit),0) hit FROM om_cache WHERE model IS NOT NULL GROUP BY model ORDER BY n DESC LIMIT 15').all();
  const gunler = d.prepare('SELECT gun, kurtarilan, yazilan FROM om_cache_gun ORDER BY gun DESC LIMIT 14').all();
  const yazanlar = d.prepare('SELECT kaynak, COUNT(*) n FROM om_cache GROUP BY kaynak ORDER BY n DESC').all();
  return {
    dbYolu, satir: Number(g?.satir || 0), hit: Number(g?.hit || 0),
    hamBayt: Number(g?.hamBayt || 0), sikisikBayt: Number(g?.sikisikBayt || 0),
    ilk: g?.ilk || null, son: g?.son || null, aileler, modeller, gunler, yazanlar,
  };
}
