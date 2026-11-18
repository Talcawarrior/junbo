// Ne işe yarar: KAYIT SÜREKLİLİĞİ + KAPSAM denetçisi (ANAYASA §9 Kural 10 ve Kural 12).
//   BÖLÜM 1 — TAZELİK: her ailenin son (run/target) tarihini beklentiyle karşılaştırır; "kaç gündür
//     beslenmiyor" der. Artık GERÇEK (settlement) hattını da denetler: 2026-09-11'de settlement 5 gün
//     sessizce durduğu halde denetçi "0 aile STALE" diyordu (kör nokta) — o kör nokta kapatıldı.
//   BÖLÜM 2 — KAPSAM: her aile için "kaç gün skorlanabilir" (tahmin VAR + gerçek VAR) tablosu.
//     Kullanıcının istediği rapor: aile × ufuk (t0/t1/t2) → gün sayısı ve şehir sayısı. Bir daha
//     "kaç günümüz var" sorusu elle sorguyla değil, bu raporun okunmasıyla cevaplanır.
//   BÖLÜM 1 ayrıca WEATHERNEXT 2'yi (`wn_ensemble`) denetler — o aile `forecast_snapshots`'a değil kendi
//     tablosuna yazar, bu yüzden aile taramasında görünmez (2026-10-06 eklendi; kör nokta kapatma).
//   Aileler ANAYASA §3 ile aynı adları kullanır. Salt-okunur (MAIN + LIVE + kardesler). Exit 0.
// Kullanım: node scripts/_tazelik_denetimi.mjs   [OUT=tazelik_raporu.md ister yazar]
import { DatabaseSync } from "node:sqlite";
import { existsSync } from "node:fs";

const MAIN = process.env.HEAT_MAIN_DB || "C:/HEAT data/db/tempmarket-edge.sqlite";
const LIVE = process.env.HEAT_LIVE_DB || "data/tempmarket-edge.sqlite";
const KARDESLER = process.env.KARDESLER_DB || "C:/HEAT data/db/kardesler.db";
const OUT = process.env.OUT || null; // verilirse md yazar

const today = new Date();
const todayIso = today.toISOString().slice(0, 10);
const dayMs = 86400000;

// ---- Aile tanımları (tazelik) ----
//  on: "target" → bu ailenin tahmini en geç şu hedef güne kadar olmalı
//      "run"    → son kayıt (run_at) en geç şu kadar önce olmalı
const FAMILIES = [
  { key: "canli", name: "Canlı 7 (VC/WA/OWM/NWS + OM-ICON/GFS/IFS025) — runner saatlik",
    providers: ["Visual Crossing daily", "WeatherAPI daily", "OpenWeatherMap 3-hour", "NWS forecastHourly",
      "Open-Meteo icon_seamless", "Open-Meteo gfs_seamless", "Open-Meteo ecmwf_ifs025"],
    on: "run", gap: 2 },
  { key: "om_archive8", name: "OM arşiv 8 model (t0/h0) — besleyici: _om_archive_gunluk.mjs (08:00)",
    providers: ["cma_grapes_global", "ecmwf_ifs025", "gem_global", "gfs_seamless", "icon_seamless",
      "jma_seamless", "meteofrance_seamless", "ukmo_seamless"],
    on: "target", gap: 3, note: "günlük besleyici hedefi 'dün'e kadar doldurur; gap>3 = besleyici durmuş" },
  { key: "om_prev", name: "Open-Meteo previous (t1/t2) — besleyici: om_previous_backfill.mjs TAIL (08:00)",
    providers: ["Open-Meteo previous"], on: "target", gap: 3, note: "previous-run API ~1-2 gün gecikir; günlük TAIL besleme gerekir" },
  { key: "ai", name: "NOAA AI (Pangu / FourCastNet v2 / GraphCast) — besleyici: _aiwp_forward.mjs (08:00)",
    providers: ["Pangu-Weather", "FourCastNet v2", "GraphCast"], on: "run", gap: 3, note: "NOAA AIWP yayını ~1-2 gün gecikir; günlük ileri koşu gerekir" },
];

// ---- Kapsam aileleri (Kim hangi ufka tahmin veriyor) ----
// Derin aile: OM arşiv 8 model YALNIZ t0; OM-önceki + 3 AI YALNIZ t1/t2.
// Canlı aile: 7 kaynağın hepsi t0/t1/t2 verir (NWS yalnız ABD, 11-12 şehir).
const DERIN_T0 = ["cma_grapes_global", "ecmwf_ifs025", "gem_global", "gfs_seamless", "icon_seamless",
  "jma_seamless", "meteofrance_seamless", "ukmo_seamless"];
const DERIN_T12 = ["Open-Meteo previous", "Pangu-Weather", "FourCastNet v2", "GraphCast"];
// NOT: eski `Open-Meteo ecmwf_ifs04` listede YOK — ad Open-Meteo'nun forecast ucunda ölü (2026-09-15:
// dizi tamamen null dönüyordu), yerini `ecmwf_ifs025` aldı. Ölü adı beklentiye yazmak her gün sahte
// STALE alarmı üretirdi.
const CANLI7 = ["Visual Crossing daily", "OpenWeatherMap 3-hour", "WeatherAPI daily", "NWS forecastHourly",
  "Open-Meteo gfs_seamless", "Open-Meteo icon_seamless", "Open-Meteo ecmwf_ifs025"];

const COVERAGE = [
  { name: "DERİN AİLE", providers: DERIN_T0, horizons: [0], full: DERIN_T0, label: "OM arşiv 8 model (t0)" },
  { name: "DERİN AİLE", providers: DERIN_T12, horizons: [1, 2], full: DERIN_T12, label: "OM-önceki + 3 AI (t1/t2)" },
  { name: "CANLI AİLE", providers: CANLI7, horizons: [0, 1, 2], full: CANLI7, label: "7 canlı kaynak (t0/t1/t2)" },
];
const MIN_CITY = 45;

const daysBehind = (d) => Math.round((Date.parse(todayIso + "T00:00:00Z") - Date.parse(d + "T00:00:00Z")) / dayMs);

function probe(db, providers, on) {
  const list = providers.map(() => "?").join(",");
  const col = on === "run" ? "run_at_utc" : "target_date";
  const rows = db.prepare(`SELECT provider, MAX(${col}) mx FROM forecast_snapshots WHERE provider IN (${list}) GROUP BY provider`).all(...providers);
  return rows.map((r) => ({ provider: String(r.provider), mx: String(r.mx || "").slice(0, 10) }));
}

const main = new DatabaseSync(MAIN, { readOnly: true });
let live = null; try { live = new DatabaseSync(LIVE, { readOnly: true }); } catch { live = null; }

const lines = [];
lines.push(`# Kayıt sürekliliği + kapsam denetimi — ${todayIso}`);
lines.push("");
lines.push("## 1. TAZELİK — aileler besleniyor mu?");
lines.push("");
lines.push("> Her aile için **beklenen**: GÜNLÜK beslenme. STALE = beklenenden fazla gündür tazelenmemiş. Tolerans kaynağın doğal gecikmesini içerir.");
lines.push("");
lines.push("| Aile | Durum | Son veri | Gün geride | Beklenti | Kaynak ayrıntı |");
lines.push("|---|---|---|---|---|---|");
let staleCount = 0;
for (const fam of FAMILIES) {
  const mainRows = probe(main, fam.providers, fam.on);
  const liveRows = live ? probe(live, fam.providers, fam.on) : [];
  const byP = new Map(mainRows.map((r) => [r.provider, r]));
  for (const r of liveRows) { const m = byP.get(r.provider); if (!m || (m.mx || "") < (r.mx || "")) byP.set(r.provider, { provider: r.provider, mx: r.mx }); }
  let newest = ""; for (const r of byP.values()) if ((r.mx || "") > newest) newest = r.mx;
  const gap = newest ? daysBehind(newest) : null;
  const ok = gap !== null && gap <= fam.gap;
  if (!ok) staleCount++;
  const detail = [...byP.values()].sort().map((r) => `${r.provider}:${r.mx || "-"}`).join(" · ");
  lines.push(`| ${fam.name} | **${ok ? "OK" : "STALE"}** | ${newest || "-"} | ${gap === null ? "-" : gap + " gün"} | ≤${fam.gap} gün | ${detail} |`);
  if (!ok && fam.note) lines.push(`| ↳ uyarı | | | | | ${fam.note} |`);
}

// ---- GERÇEK (settlement) hattı — 2026-09-11'de eklenen kör-nokta kapatma ----
{
  const r = main.prepare("SELECT MAX(target_date) mx, COUNT(*) n FROM settlements").get();
  const mx = String(r.mx || "").slice(0, 10);
  const gap = mx ? daysBehind(mx) : null;
  // Polymarket gün bittikten sonra çözer; 1-2 gün gecikme NORMAL, >3 gün = köprü durmuş
  const ok = gap !== null && gap <= 3;
  if (!ok) staleCount++;
  const son = main.prepare("SELECT target_date td, COUNT(*) n FROM settlements GROUP BY target_date ORDER BY target_date DESC LIMIT 1").get();
  const kaynak = main.prepare("SELECT source s, COUNT(*) n FROM settlements WHERE target_date >= ? GROUP BY source ORDER BY n DESC LIMIT 1").get(todayIso.slice(0, 8) + "01");
  lines.push(`| GERÇEK (settlement) — köprü: _settlement_bridge.mjs (08:00, kardesler.db→MAIN) | **${ok ? "OK" : "STALE"}** | ${mx || "-"} (${son ? son.n : 0} şehir) | ${gap === null ? "-" : gap + " gün"} | ≤3 gün | toplam ${r.n} satır · son ayın ana kaynağı: ${kaynak ? kaynak.s : "-"} (${kaynak ? kaynak.n : 0}) |`);
  if (!ok) lines.push(`| ↳ uyarı | | | | | Settlement köprüsü durmuş olabilir: kaynakta (kardesler.db junbo_weather_markets) çözülmüş gün var mı bak, sonra \`node scripts/_settlement_bridge.mjs\` koş. |`);
}

// ---- WEATHERNEXT 2 (wn_ensemble) — ayrı tablo, o yüzden ayrı denetim (2026-10-06 eklendi) ----
// NEDEN GEREKLİ: WN2 `forecast_snapshots`'a YAZILMAZ, kendi `wn_ensemble` tablosuna yazar → yukarıdaki
// aile taraması onu hiç görmez (kör nokta). 2026-09-26..10-04 arası runner ölüydü, 6 gün hiç çekim
// olmadı ve hiçbir rapor uyarmadı. Kritik: Open-Meteo WN2 üyelerini yalnız ~3-4 gün saklar → gecikme
// KALICI kayıp (kurtarma yalnız GCS'ten). Bu yüzden hem burada hem bekçide (watchdog.ps1) izlenir.
{
  const src = live || main;                 // runner LIVE'a yazar; MAIN kopyası donmuş olabilir
  const hangi = live ? "LIVE" : "MAIN";
  let mx = "", nSt = 0, nGun = 0;
  try {
    const r = src.prepare("SELECT MAX(fetched_at_utc) mx, COUNT(DISTINCT station_id) s, COUNT(DISTINCT target_date) g FROM wn_ensemble").get();
    mx = String(r?.mx || ""); nSt = Number(r?.s || 0); nGun = Number(r?.g || 0);
  } catch { /* tablo yoksa boş kalır */ }
  const saat = mx ? Math.round((Date.now() - Date.parse(mx)) / 3600000) : null;
  // Normal: günde BİR kez ~00:00-01:00 UTC çekim → denetim 08:00'de koşunca ~8 saat. Bir günlük tur
  // kaçarsa ≥32 saat olur; eşik 30 saat bu ikisini temiz ayırır (ne sahte alarm ne sessiz kalma).
  const ok = saat !== null && saat <= 30;
  if (!ok) staleCount++;
  lines.push(`| WEATHERNEXT 2 (wn_ensemble) — yalnız runner yazar, model günde 1 kez (${hangi}) | **${ok ? "OK" : "STALE"}** | ${mx ? mx.slice(0, 16).replace("T", " ") : "-"} | ${saat === null ? "-" : saat + " saat"} | ≤30 saat | ${nSt} şehir · ${nGun} hedef gün |`);
  if (!ok) lines.push(`| ↳ uyarı | | | | | WN2 çekimi durmuş olabilir. **Bu aile \`forecast_snapshots\`'ta DEĞİL**, ayrı \`wn_ensemble\` tablosunda — 2026-09-26..10-04'te 6 gün tam böyle kaybedildi. Open-Meteo üyeleri yalnız ~3-4 gün saklar: **gecikme = kalıcı kayıp** (kurtarma GCS'ten). Bekçi \`windows-runner/watchdog.ps1\` bu sinyali her saat ayrıca kontrol eder. |`);
}

lines.push("");
lines.push(`**Özet: ${staleCount} aile STALE.** Kesinti gören aile = günlük beslenmeyi durdurmuş demektir; besleyicisini/köprüsünü çalıştır.`);

// ---- BÖLÜM 2: KAPSAM ----
lines.push("");
lines.push("## 2. KAPSAM — kaç gün skorlanabilir? (tahmin VAR **ve** gerçek VAR)");
lines.push("");
lines.push(`> Ölçüt: hücre başına **en son koşu**, ufuk t0/t1/t2, o gün için **settlement mevcut**. ` +
  `"Gün" = en az ${MIN_CITY} şehirde o kaynağın tahmini + gerçeği olan hedef gün sayısı.`);
lines.push("");

const cov = main
  .prepare(
    `WITH l AS (
       SELECT fs.station_id st, fs.target_date td, fs.horizon_days h, fs.provider pr,
              ROW_NUMBER() OVER (PARTITION BY fs.provider, fs.station_id, fs.target_date, fs.horizon_days
                                 ORDER BY fs.run_at_utc DESC) rn
         FROM forecast_snapshots fs
        WHERE fs.target_date <= ? AND fs.horizon_days IN (0,1,2)
          AND EXISTS (SELECT 1 FROM settlements s WHERE s.station_id = fs.station_id AND s.target_date = fs.target_date))
     SELECT st, td, h, pr FROM l WHERE rn = 1`,
  )
  .all(todayIso);

// provider → horizon → (gün kümesi, en fazla şehir)
const agg = new Map();
for (const r of cov) {
  let byH = agg.get(r.pr); if (!byH) { byH = new Map(); agg.set(r.pr, byH); }
  let e = byH.get(r.h); if (!e) { e = { days: new Set(), cities: new Map() }; byH.set(r.h, e); }
  e.days.add(r.td);
  e.cities.set(r.td, (e.cities.get(r.td) || 0) + 1);
}
// Kaynak başına eşik. Global kaynaklar için 45 şehir. NWS gibi YAPISAL OLARAK DAR kaynaklar (yalnız ABD,
// 11-12 şehir) kendi ölçeğinde değerlendirilir — yoksa tabloda hiç görünmez ve "veri yok" sanılır.
// eşik(kaynak) = min(45, ceil(0.9 × o kaynağın ulaştığı en yüksek şehir sayısı))
const maxCities = (pr) => {
  let mx = 0;
  for (const e of (agg.get(pr) ?? new Map()).values()) for (const n of e.cities.values()) if (n > mx) mx = n;
  return mx;
};
const thresh = new Map();
for (const pr of agg.keys()) thresh.set(pr, Math.max(1, Math.min(MIN_CITY, Math.ceil(0.9 * maxCities(pr)))));
const thr = (pr) => thresh.get(pr) ?? MIN_CITY;

const dayCount = (pr, h) => {
  const e = agg.get(pr)?.get(h);
  if (!e) return { g: 0, cities: 0, from: null, to: null, t: thr(pr) };
  const ok = [...e.cities.entries()].filter(([, n]) => n >= thr(pr)).map(([d]) => d).sort();
  return { g: ok.length, cities: Math.max(0, ...ok.map((d) => e.cities.get(d))), from: ok[0] ?? null, to: ok[ok.length - 1] ?? null, t: thr(pr) };
};
// aile birlikte: her hedef günde TÜM kaynaklar kendi eşiğini geçiyor mu
const familyFull = (providers, h) => {
  const perDay = new Map();
  for (const pr of providers) {
    const e = agg.get(pr)?.get(h); if (!e) continue;
    for (const [d, n] of e.cities) if (n >= thr(pr)) perDay.set(d, (perDay.get(d) || 0) + 1);
  }
  return [...perDay.entries()].filter(([, k]) => k === providers.length).map(([d]) => d).sort();
};

for (const grp of ["DERİN AİLE", "CANLI AİLE"]) {
  const rows = COVERAGE.filter((c) => c.name === grp);
  lines.push(`### ${grp}`);
  lines.push("");
  lines.push("| Kaynak | t0 | t1 | t2 | Şehir | Pencere |");
  lines.push("|---|---|---|---|---|---|");
  for (const c of rows) {
    for (const pr of c.providers) {
      const a = dayCount(pr, 0), b = dayCount(pr, 1), d = dayCount(pr, 2);
      const cities = Math.max(a.cities, b.cities, d.cities);
      const from = [a.from, b.from, d.from].filter(Boolean).sort()[0] ?? "-";
      const to = [a.to, b.to, d.to].filter(Boolean).sort().slice(-1)[0] ?? "-";
      const sparse = thr(pr) < MIN_CITY ? `*` : "";
      lines.push(`| ${pr}${sparse} | ${a.g || "–"} | ${b.g || "–"} | ${d.g || "–"} | ${cities || "-"} | ${from === "-" ? "-" : from + " → " + to} |`);
    }
  }
  lines.push("");
  // aile birlikte
  const parts = [];
  for (const h of [0, 1, 2]) {
    const provs = rows.filter((c) => c.horizons.includes(h)).flatMap((c) => c.providers);
    if (!provs.length) continue;
    const ok = familyFull(provs, h);
    parts.push(`t${h} **${ok.length} gün**${ok.length ? ` (${ok[0]} → ${ok[ok.length - 1]})` : ""}`);
  }
  lines.push(`**${grp} — ${rows.reduce((s, c) => s + c.providers.length, 0)} kaynak, hepsi birlikte ≥${MIN_CITY} şehir:** ${parts.join(" · ")}`);
  lines.push("");
}
lines.push("> **Nasıl okunur:** \"kaynak başına N gün\" = o kaynağı tek başına backtest edebileceğin gün sayısı. " +
  "\"hepsi birlikte\" = tüm kaynakları aynı tabloda kıyaslayabileceğin gün sayısı (her zaman daha azdır; " +
  "kaynakların ufuk kapsamı farklı olduğu için t0/t1/t2 sayıları ayrı verilir).");
lines.push(">");
lines.push(`> \\* işaretli kaynak yapısal olarak dardır (yalnız ABD vb.) → **kendi ölçeğinde** sayılır (ulaştığı en yüksek şehir sayısının %90'ı), ` +
  `işaretsizler ≥${MIN_CITY} şehir. Tablodaki "Şehir" o kaynağın ulaştığı en yüksek şehir sayısıdır.`);
lines.push(">");
// Settlement derinliği: derin ailenin gerçek sınırı
const sett = main.prepare(`SELECT MIN(target_date) a, MAX(target_date) b, COUNT(*) n FROM settlements`).get();
const sett45 = main.prepare(`SELECT MIN(target_date) a, COUNT(*) n FROM (SELECT target_date FROM settlements GROUP BY target_date HAVING COUNT(*) >= ${MIN_CITY})`).get();
lines.push(`> **DİKKAT — derin ailenin gerçek sınırı tahmin değil GERÇEKTİR.** Settlement tablosu ${sett.a} → ${sett.b} arası ` +
  `${sett.n} satır, AMA günde ≥${MIN_CITY} şehir olan gün yalnız **${sett45.n}** ve o pencere **${sett45.a}'dan** başlıyor. ` +
  `Öncesinde günde ~2 şehir vardı (2025 boyunca), yani tahmin arşivi derin olsa da kıyaslanacak gerçek yoktu. ` +
  `Derin ailenin backtest penceresini belirleyen şey budur.`);

console.log(lines.join("\n"));
main.close(); if (live) live.close();

if (OUT) {
  const { writeFileSync } = await import("node:fs");
  writeFileSync(OUT, lines.join("\n") + "\n", "utf8");
  console.log(`\nRapor yazıldı: ${OUT}`);
}
