// Ne işe yarar: Bir Polymarket sıcaklık marketinin **hangi havalimanı istasyonunda çözüldüğünü** market
//   metninden çıkarır. `_sehir_cozucu.mjs` (keşif/öneri) ve `_sehir_geri_doldur.mjs` (geçmiş doldurma)
//   bu tek mantığı paylaşır — iki yerde ayrı ayrı yazılıp birbirinden sapmasın.
//
// ÖNCELİK SIRASI (en güvenilir önce) — sabit şehir listesine güvenilmez, her market için yeniden çözülür:
//   1) resolutionSource URL'inin SON SEGMENTİ (Weather Underground: .../history/daily/<ülke>/<şehir>/<ICAO>)
//   2) resolutionSource'taki ?site=<kod> (NWS WRH timeseries)
//   3) description/rules metnindeki ?site=<kod>
//   4) description'da havalimanı/istasyon bağlamına yakın 4 harfli büyük kod
//   5) kardeş botun city_code alanı — YALNIZ SON ÇARE (yanlış olabildiği ölçüldü: MPTO → gerçekte MPMG)
//
// NEDEN SON SEGMENT: WU yolu ülke/bölge içerir ve segment sayısı şehirden şehire değişir —
//   /history/daily/us/fl/miami/KMIA (4 segment) · /history/daily/gb/london/EGLC (3) · /history/daily/pa/panama-city/MPMG (3).
//   2026-09-15'te sabit "iki segment sonra" varsayımı ABD URL'lerinde KMIA yerine **miam**ı yakaladı ve
//   8 sahte "eksik şehir" önerisi üretti. Sabit konum varsayımı bu yüzden yasak.
export const SITE_RE = /[?&]site=([A-Za-z0-9]{4})/;

export function wuSonSegment(url) {
  try {
    const u = new URL(url);
    if (!/wunderground\.com$/i.test(u.hostname) || !/^\/history\/daily\//i.test(u.pathname)) return null;
    const parcalar = u.pathname.split('/').filter(Boolean);
    const son = parcalar[parcalar.length - 1] || '';
    return /^[A-Za-z]{4}$/.test(son) ? son.toUpperCase() : null;
  } catch { return null; }
}

// WU yolu ÜLKE kodunu da taşır: /history/daily/<ülke>/…/<ICAO>  → 'us' · 'gb' · 'pa'.
// Yeni istasyon eklerken `stations.country` NOT NULL olduğu ve ülke hangi toplayıcıların koşacağını
// belirlediği için (ör. `country==='US'` → NWS) bu şart: ülke bilinmiyorsa istasyon EKLENMEZ.
export function wuUlke(url) {
  try {
    const u = new URL(url);
    if (!/wunderground\.com$/i.test(u.hostname) || !/^\/history\/daily\//i.test(u.pathname)) return null;
    const parcalar = u.pathname.split('/').filter(Boolean); // ['history','daily','pa','panama-city','MPMG']
    const c = parcalar[2] || '';
    return /^[A-Za-z]{2}$/.test(c) ? c.toUpperCase() : null;
  } catch { return null; }
}

// Dönen: { icao, kaynak, ulke } veya null (hiçbir yöntem çözemedi → çağıran "belirsiz" sayar, TAHMİN ETMEZ).
export function istasyonCoz(m) {
  const rs = String(m.resolutionSource || '');
  const wu = wuSonSegment(rs);
  if (wu) return { icao: wu, kaynak: 'resolutionSource (Weather Underground URL son segmenti)', ulke: wuUlke(rs) };
  let x = rs.match(SITE_RE);
  // DİKKAT: `weather.gov` GÖRÜNCE "ABD" demek YANLIŞ olur — Panama'nın çözüm kaynağı da
  // `weather.gov/wrh/timeseries?site=mpmg` idi (2026-09-15 MPTO→MPMG vakası). Bu yüzden NWS dalında
  // ülke ÇIKARILMAZ; ülke bilinmiyorsa istasyon otomatik eklenmez (şüpheli listesine düşer).
  if (x) return { icao: x[1].toUpperCase(), kaynak: 'resolutionSource (NWS WRH site=)', ulke: null };
  const metin = `${m.description || ''} ${m.question || ''}`;
  x = metin.match(SITE_RE);
  if (x) return { icao: x[1].toUpperCase(), kaynak: 'description (site=)', ulke: null };
  const ctx = metin.match(/(?:airport|station|havaliman|istasyon)[^A-Z]{0,40}([A-Z]{4})\b/);
  if (ctx) return { icao: ctx[1], kaynak: 'description (havalimanı bağlamı)', ulke: null };
  if (m.city_code && /^[A-Za-z]{4}$/.test(m.city_code)) return { icao: String(m.city_code).toUpperCase(), kaynak: 'kardeş city_code (son çare)', ulke: null };
  return null;
}
