// Ne işe yarar: junbo + ASI-Evolve "kardeş" DB'lerindeki birikmiş veriyi (orderbook derinliği, market
// fiyat zaman çizgisi, model tahminleri, Polymarket market/çözümleri, bahis/analiz geçmişi) tek sahipli
// aynaya (C:/HEAT data/db/kardesler.db) kopyalar. Amaç: kardeş sistemler dursa/sıfırlansa/silinse veri
// Heat'te kalır; isabet/kilit/strateji testleri oradan okur. Artımlıdır: id'si olan tabloda id>son-id
// satırlar INSERT OR IGNORE; id'siz tablo varsa tamamen yeniden kurulur (küçük beklenir). Kaynağa YALNIZ
// SELECT atar (salt-okunur davranış). Çıktı: tablo başına önce/sonra/eklenen. `--dry-run` yazmaz.
// Kullanım: node --experimental-sqlite scripts/_mirror_kardesler.mjs [--dry-run]
// Kural: yeni dosya → envanter tazele + codegraph sync . (CLAUDE.md dosya envanteri âdeti)

import { DatabaseSync } from 'node:sqlite';
import path from 'node:path';
import fs from 'node:fs';

const DRY = process.argv.includes('--dry-run');
const TARGET = process.env.KARDESLER_DB || 'C:/HEAT data/db/kardesler.db';
const RUN_START = nowIso();

// Kaynaklar: { dosya, önek } — aynada her tablo <önek>_<tabloadı> olur.
const SOURCES = [
  { file: 'C:/Users/fdemir/Documents/New project/junbo/data/bot.db',       prefix: 'junbo' },
  { file: 'C:/Users/fdemir/Documents/New project/junbo/data/orderbook.db', prefix: 'junbo' },
  { file: 'D:/JUNBO data/heat/upstream/asi_import/asi_bot_for_heat.db',   prefix: 'asi'   },
];
// Aynalanmayacak tablolar (anlık durum/çalışma kaydı — geriye dönük veri değil):
const SKIP = new Set(['activity_events', 'peak_watch', 'portfolio', 'bet_snapshots']);
// >= bu satır sayısı VE düzgün integer id PK varsa artımlı (id>son) kopya; yoksa her koşuda birebir yeniden kur.
// (Seyrek/metin id'li veya küçük tablolar tam kopya olur → ayna daima kaynağın gerçeği; dev tablolar ekleme
//  yapar → kaynak ileride satır budasa bile ayna onları saklar = dayanıklı arşiv.)
const INCREMENTAL_MIN = 200000;

function nowIso() { return new Date().toISOString(); }

// Kaynağı salt-okunur eklemeyi dene; URI mode=ro olmazsa düz ekle (yine de sadece SELECT).
function attachSource(conn, file, alias) {
  const abs = path.resolve(file).replace(/\\/g, '/');
  try {
    conn.prepare(`ATTACH DATABASE ? AS ${alias}`).run(`file:${abs}?mode=ro`);
  } catch {
    conn.prepare(`ATTACH DATABASE ? AS ${alias}`).run(abs);
  }
}
function q(s) { return '"' + s.replace(/"/g, '""') + '"'; }
function hasIdPk(conn, db, table) {
  return conn.prepare(`PRAGMA ${db}.table_info(${q(table)})`).all().some(c => c.name === 'id' && c.pk === 1);
}
function doCount(conn, table) {
  const r = conn.prepare(`SELECT COUNT(*) c FROM ${table}`).get();
  return r ? r.c : 0;
}
function doMax(conn, table, col = 'id') {
  const r = conn.prepare(`SELECT COALESCE(MAX(${col}),0) m FROM ${table}`).get();
  return r ? r.m : 0;
}
function existsDst(conn, table) {
  const r = conn.prepare(`SELECT name FROM dst.sqlite_master WHERE type='table' AND name=?`).get(table);
  return !!r;
}

// Hedef fiziksel olarak aç (şema + özet okuma için)
const target = new DatabaseSync(TARGET);
target.exec('PRAGMA busy_timeout=30000;');
target.exec(`CREATE TABLE IF NOT EXISTS mirror_log (
  dst_table TEXT, run_at_utc TEXT, rows_before INTEGER, rows_added INTEGER, strategy TEXT)`);

const totalAdded = 0;
const report = [];
const tmp = new DatabaseSync(':memory:');
tmp.exec('PRAGMA busy_timeout=30000;');
tmp.prepare('ATTACH DATABASE ? AS dst').run(TARGET);

for (const src of SOURCES) {
  const abs = path.resolve(src.file);
  if (!fs.existsSync(abs)) { report.push(`[kaynak yok] ${src.file}`); continue; }
  const alias = 'src';
  attachSource(tmp, abs, alias);

  const tables = tmp.prepare(`SELECT name FROM ${alias}.sqlite_master
                              WHERE type='table' AND name NOT LIKE 'sqlite_%'`).all()
    .map(r => r.name).filter(t => !SKIP.has(t)).sort();

  for (const t of tables) {
    const dst = `${src.prefix}_${t}`;
    const nSrc = doCount(tmp, `${alias}.${q(t)}`);
    const already = existsDst(tmp, dst);
    const nDst = already ? doCount(tmp, `dst.${q(dst)}`) : 0;
    const hasId = hasIdPk(tmp, alias, t);
    const srcMax = hasId ? doMax(tmp, `${alias}.${q(t)}`) : 0;
    const doInc = hasId && nSrc >= INCREMENTAL_MIN;

    if (DRY) {
      const strat = !doInc ? 'tam-kopya(her kosu)' : (!already ? 'ilk-kopya' : 'artimli(id>son)');
      report.push(`[kuru] ${dst}: kaynak ${nSrc} (sonId ${srcMax}) | hedef ${already ? nDst : 'yok'} | ${strat}`);
      continue;
    }

    let before = nDst, finalC = 0, strat = '';
    const fullReplace = () => {
      tmp.exec('BEGIN');
      tmp.exec(`DROP TABLE IF EXISTS dst.${q(dst)}`);
      tmp.exec(`CREATE TABLE dst.${q(dst)} AS SELECT * FROM ${alias}.${q(t)}`);
      tmp.exec('COMMIT');
      return doCount(tmp, `dst.${q(dst)}`);
    };
    if (!doInc) {
      strat = 'tam-kopya';
      finalC = fullReplace();
    } else if (!already) {
      strat = 'ilk-kopya';
      tmp.exec(`CREATE TABLE dst.${q(dst)} AS SELECT * FROM ${alias}.${q(t)} WHERE 0`);
      const r = tmp.prepare(`INSERT OR IGNORE INTO dst.${q(dst)} SELECT * FROM ${alias}.${q(t)}`).run();
      finalC = before + Number(r.changes);
    } else {
      const last = doMax(tmp, `dst.${q(dst)}`);
      strat = `artimli(id>${last})`;
      const r = tmp.prepare(`INSERT OR IGNORE INTO dst.${q(dst)} SELECT * FROM ${alias}.${q(t)} WHERE id>?`).run(last);
      finalC = before + Number(r.changes);
    }
    const net = finalC - before; // bu koşunun net büyümesi
    tmp.prepare(`INSERT INTO dst.mirror_log (dst_table, run_at_utc, rows_before, rows_added, strategy)
                 VALUES (?,?,?,?,?)`).run(dst, nowIso(), before, net, strat);
    report.push(`[${strat}] ${dst}: ${before} -> ${finalC} (net ${net > 0 ? '+' : ''}${net})`);
  }
  try { tmp.prepare(`DETACH DATABASE ${alias}`).run(); } catch {}
}
tmp.close();

console.log(`KARDESLER AYNASI ${DRY ? '(KURU — yazma yok)' : 'tamam'}`);
console.log(report.join('\n'));
if (!DRY) {
  const row = target.prepare('SELECT COUNT(*) c, COALESCE(SUM(rows_added),0) a FROM mirror_log WHERE run_at_utc>=?').get(RUN_START);
  console.log(`bu koşu: ${row.c} tablo, +${row.a} satır → ${TARGET}`);
}
target.close();
