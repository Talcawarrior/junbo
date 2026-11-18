// Ne işe yarar: Kardeş aynasındaki (kardesler.db → junbo_weather_markets) ÇÖZÜLMÜŞ Polymarket
//   sıcaklık marketlerini MAIN.settlements'a taşıyan GÜNLÜK ve ARTIMLI köprü. Tahmin tarafının
//   `_archive_canli_forecasts.mjs` ile MAIN'e akması gibi, gerçek (settlement) tarafı da her gün aksın.
//   Neden gerekli: 2026-09-07'de settlement MAIN'e yalnız TEK SEFERLİK backfill ile yazıldı
//   (_settlement_backfill_junbo.mjs, "idempotent değil — bir kez koş"); o günden sonra köprü olmadığı
//   için MAIN.settlements 09-06'da dondu, oysa kaynakta 09-07/08/09 çözülmüş durumdaydı → tahmin
//   birikiyor ama skorlanamıyordu. Bu betik o boşluğu kalıcı olarak kapatır.
//   Yöntem (kanıtlanmış): kazanan market = outcomePrices ["1","0"]; değeri = threshold (tam derece).
//   Örtüşen 297 şehir-günde MAIN ile birebir doğrulandı (bkz. _settlement_backfill_junbo.mjs başlığı).
//   GÜVENLİK: kaynak DB SALT-OKUNUR (ANAYASA §9 Kural 11 — kardeş verisi kesinlikle bozulmaz).
//   Yazma yalnız MAIN'e; önce settlements tablosunun yedeği alınır. Var olan (şehir, gün) ASLA üzerine
//   yazılmaz — hangi kaynak adıyla yazılmış olursa olsun atlanır (unique: station_id+target_date+source).
//   İdempotent: ikinci koşu 0 ekler. MAIN settlement'ı geriye gitmez (yeni gün ekler).
// Kullanım: node scripts/_settlement_bridge.mjs [--dry-run] [--since=YYYY-MM-DD]
//   --dry-run : hiçbir şey yazmaz, yalnız kaç satır geleceğini basar
//   --since   : bu hedef günden eskisini yok sayar (varsayılan: son 45 gün — backfill zaten derini kapattı)

import { DatabaseSync } from "node:sqlite";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
// İstasyon çözümleme ORTAK mantık (_sehir_cozucu.mjs ile aynı) — kardeşin city_code'una körü körüne güvenme.
import { istasyonCoz } from "./_istasyon_coz.mjs";

const DRY = process.argv.includes("--dry-run");
const sinceArg = process.argv.find((a) => a.startsWith("--since="));
const SINCE = sinceArg ? sinceArg.slice(8) : new Date(Date.now() - 45 * 86400000).toISOString().slice(0, 10);

const MAIN = process.env.HEAT_MAIN_DB || "C:/HEAT data/db/tempmarket-edge.sqlite";
const KARDESLER = process.env.KARDESLER_DB || "C:/HEAT data/db/kardesler.db";
const SOURCE = "Polymarket settlement (kardesler koprusu)";
const dateStr = new Date().toISOString().slice(0, 10).replace(/-/g, "");
const BACKUP = `${dirname(MAIN)}/backups/settlements_pre_bridge_${dateStr}.sqlite`;
// Kural 13: eşleşmeyen şehir kodlarının kalıcı defteri (devlet dosyası HEAT_DATA yanında; repo dışı).
const UNMATCHED_JSON = process.env.HEAT_UNMATCHED_JSON || "C:/HEAT data/state/unmatched_cities.json";

if (!existsSync(MAIN) || !existsSync(KARDESLER)) {
  console.error(`DB yok: MAIN=${existsSync(MAIN)} KARDESLER=${existsSync(KARDESLER)}`);
  process.exit(1);
}

const k = new DatabaseSync(KARDESLER, { readOnly: true }); // KESİNLİKLE BOZMA
const m = new DatabaseSync(MAIN, { readOnly: DRY });

// ---- 1) Kaynakta çözülmüş (kazananı belli) şehir-günleri oku ----
const winners = new Map(); // "ICAO|YYYY-MM-DD" -> derece
let dup = 0;
for (const r of k
  .prepare(
    `SELECT question, city_code, substr(target_date,1,10) d, threshold, raw_data
       FROM junbo_weather_markets
      WHERE metric='temperature_max' AND substr(target_date,1,10) >= ?`,
  )
  .all(SINCE)) {
  let val = null;
  let j = {};
  try {
    j = JSON.parse(r.raw_data);
    const p = JSON.parse(j.outcomePrices || "[]");
    if (Array.isArray(p) && p.length === 2 && p[0] === "1" && p[1] === "0") val = r.threshold;
  } catch {}
  if (val == null) continue;
  // KURAL 13 / 2026-09-15: kardeşin `city_code` alanı YANLIŞ olabiliyor. Panama'da 3 gün `MPTO` yazılıydı,
  // oysa marketin kendi resolutionSource'u MPMG'yi gösteriyordu → Heat o günleri "istasyon yok" diye
  // SESSİZCE atlıyordu. Artık istasyon metinden çözülür (WU URL son segmenti), `city_code` yalnız son çare.
  const cozum = istasyonCoz({ resolutionSource: j.resolutionSource, description: j.description, question: r.question, city_code: r.city_code });
  const key = `${cozum ? cozum.icao : r.city_code}|${r.d}`;
  if (winners.has(key) && winners.get(key) !== val) {
    dup++;
    winners.delete(key); // aynı şehir-gün için iki farklı kazanan = belirsiz, hiç yazma
    continue;
  }
  winners.set(key, val);
}
console.log(`kaynakta çözülmüş şehir-gün (>= ${SINCE}): ${winners.size}${dup ? `  (${dup} belirsiz atlandı)` : ""}`);

// ---- 2) İstasyon eşlemesi: MAIN.stations.icao -> id; yoksa settlements'ta görülen küçük-harf id ----
const icaoToId = new Map();
for (const s of m.prepare("SELECT id, icao FROM stations").all()) {
  if (s.icao) icaoToId.set(String(s.icao).toUpperCase(), s.id);
}
const knownIds = new Set(m.prepare("SELECT DISTINCT station_id FROM settlements").all().map((r) => r.station_id));
const mapStation = (city) =>
  icaoToId.get(String(city).toUpperCase()) ?? (knownIds.has(String(city).toLowerCase()) ? String(city).toLowerCase() : null);

// ---- 3) MAIN'de zaten olan (şehir, gün) — kaynak adından BAĞIMSIZ ----
const existing = new Set(
  m.prepare("SELECT station_id || '|' || target_date k FROM settlements").all().map((r) => r.k),
);

// ---- 4) Adaylar ----
const cand = [];
const byDay = {};
const noStn = new Set();
const unmatched = new Map(); // KURAL 13: tanınmayan şehir kodu SESSİZCE geçilmez — kalıcı dosyaya birikir
let noStation = 0;
let already = 0;
for (const [key, val] of winners) {
  const [city, d] = key.split("|");
  const sid = mapStation(city);
  if (!sid) {
    noStation++;
    if (noStn.size < 12) noStn.add(city);
    const u = unmatched.get(city) || { kod: city, gunSayisi: 0, ilkGun: d, sonGun: d, ornekGunler: [] };
    u.gunSayisi++;
    if (d < u.ilkGun) u.ilkGun = d;
    if (d > u.sonGun) u.sonGun = d;
    if (u.ornekGunler.length < 5) u.ornekGunler.push(d);
    unmatched.set(city, u);
    continue;
  }
  if (existing.has(`${sid}|${d}`)) {
    already++;
    continue;
  }
  cand.push({ sid, d, val });
  byDay[d] = (byDay[d] || 0) + 1;
}

const days = Object.keys(byDay).sort();
console.log(`yazılacak: ${cand.length}  |  zaten var (atlandı): ${already}  |  istasyon eşleşmedi: ${noStation}`);
if (noStn.size) console.log(`  istasyonu olmayan şehirler: ${[...noStn].join(", ")}`);

// KURAL 13 (2026-09-15): "yeni şehir sessizce atlanmaz". Eskiden eşleşmeyen kod yalnız stdout'a yazılıp
// kayboluyordu → kimse fark etmiyordu. Artık kalıcı dosyaya birikir; `_sehir_cozucu.mjs` bunu okur.
// Dosya birikimli tutulur (ilk görülme silinmez) ki bir kodun ne zamandır geldiği görünsün.
if (unmatched.size) {
  try {
    let onceki = {};
    try { onceki = JSON.parse(readFileSync(UNMATCHED_JSON, "utf8")).kodlar || {}; } catch {}
    const kodlar = { ...onceki };
    for (const [k, u] of unmatched) {
      const o = kodlar[k] || { kod: k, ilkGorulme: u.ilkGun, gunSayisi: 0, ornekGunler: [] };
      o.sonGorulme = u.sonGun;
      if (u.ilkGun < o.ilkGorulme) o.ilkGorulme = u.ilkGun;
      o.gunSayisi = Math.max(o.gunSayisi, u.gunSayisi);
      o.ornekGunler = [...new Set([...(o.ornekGunler || []), ...u.ornekGunler])].slice(-10);
      kodlar[k] = o;
    }
    mkdirSync(dirname(UNMATCHED_JSON), { recursive: true });
    writeFileSync(UNMATCHED_JSON, JSON.stringify({
      guncelleme: new Date().toISOString(),
      aciklama: "Heat'te istasyonu olmayan kardeş şehir kodları — _sehir_cozucu.mjs bunları çözer/önerir (Kural 13)",
      kodlar,
    }, null, 2), "utf8");
    console.log(`  eşleşmeyen kodlar dosyaya işlendi: ${UNMATCHED_JSON} (${Object.keys(kodlar).length} kod birikmiş)`);
  } catch (e) { console.error(`  eşleşmeyen kod dosyası yazılamadı: ${e.message}`); }
}
if (days.length) {
  console.log(`  hedef gün aralığı: ${days[0]}..${days[days.length - 1]}`);
  for (const d of days) console.log(`    ${d}  ${byDay[d]} şehir`);
}

if (DRY) {
  console.log("\n--dry-run: MAIN'e yazılmadı.");
  m.close();
  k.close();
  process.exit(0);
}
if (!cand.length) {
  console.log("\nEklenecek yeni settlement yok — köprü zaten güncel.");
  m.close();
  k.close();
  process.exit(0);
}

// ---- 5) Yedek + yazma (tek transaction) ----
const before = m.prepare("SELECT COUNT(*) n FROM settlements").get().n;
mkdirSync(dirname(BACKUP), { recursive: true });
const bkName = BACKUP.replace(/'/g, "''");
m.exec("PRAGMA busy_timeout=30000;");
m.exec(`ATTACH DATABASE '${bkName}' AS bk`);
// Aynı UTC gününde İKİNCİ koşu: yedek dosyası zaten var ve içinde settlements duruyor → CREATE TABLE
// "table settlements already exists" ile düşer (2026-09-11'de runner'da tam bu oldu, 09-10 yazılamadı).
// DROP ile kendi kendini onarır; yazma yalnız INSERT OR IGNORE (asla üzerine yazmaz) olduğu için yedeğin
// "son yazma öncesi hâl"i göstermesi yeterlidir — yedek gün adıyla tek dosya kalır, birikmez.
m.exec("DROP TABLE IF EXISTS bk.settlements");
m.exec("CREATE TABLE bk.settlements AS SELECT * FROM main.settlements");
const bkCount = m.prepare("SELECT COUNT(*) n FROM bk.settlements").get().n;
m.exec("DETACH DATABASE bk");
console.log(`YEDEK: ${BACKUP} (${bkCount} satır)`);
if (bkCount !== before) {
  console.error("YEDEK SAYISI UYUŞMUYOR — yazma iptal.");
  process.exit(1);
}

const ins = m.prepare(
  "INSERT OR IGNORE INTO settlements (station_id, target_date, source, high_c, recorded_at_utc, raw_json) VALUES (?,?,?,?,?,?)",
);
const nowIso = new Date().toISOString();
m.exec("BEGIN");
let inserted = 0;
try {
  for (const c of cand) inserted += ins.run(c.sid, c.d, SOURCE, c.val, nowIso, "{}").changes;
  m.exec("COMMIT");
} catch (e) {
  m.exec("ROLLBACK");
  console.error("HATA, geri alındı:", e.message);
  process.exit(1);
}
const after = m.prepare("SELECT COUNT(*) n FROM settlements").get().n;
console.log(`\nEKLENDİ: ${inserted}  |  settlements: ${before} -> ${after}`);
if (after !== before + inserted) {
  console.error("SAYI UYUŞMAZLIĞI!");
  process.exit(1);
}
console.log("\nMAIN settlement — son 12 hedef gün (şehir sayısı):");
for (const r of m
  .prepare(`SELECT target_date td, COUNT(*) n FROM settlements WHERE target_date >= ? GROUP BY target_date ORDER BY target_date DESC LIMIT 12`)
  .all(SINCE))
  console.log(`  ${r.td}  ${String(r.n).padStart(3)} şehir`);

m.close();
k.close();
