// Ne işe yarar: GÜNLÜK besleyicileri tek noktadan koşturan orchestrator (Kural 10, 2026-09-08).
// Runner'ın UTC 08:00 bloğu bunu çağırır (isabet raporundan ÖNCE) ki rapor en taze veriyi görsün.
// Sırayla koşar, her adım ayrı çocuk süreç (hata diğerlerini durdurmaz):
//   1) OM arşiv 8 model  → scripts/_om_archive_gunluk.mjs        (t0/h0, hedef dün'e kadar)
//   2) Open-Meteo önceki → scripts/om_previous_backfill.mjs      (t1/t2, OM_PREV_TAIL_DAYS son penceresi)
//   3) NOAA AI (AIWP)    → scripts/_aiwp_forward.mjs             (en yeni init'ten ileri t1..t9)
// Hepsi MAIN DB'ye yazar (script→MAIN kuralı). Betikler idempotent: her gün yeniden koşmak güvenli.
// Kullanım: node --experimental-sqlite scripts/_gunluk_besleyiciler.mjs   [GUNLUK_ONLY=omarchive|omprev|ai]
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const execFileAsync = promisify(execFile);
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');

const ONLY = process.env.GUNLUK_ONLY ? process.env.GUNLUK_ONLY.split(',') : null;
const opts = { cwd: ROOT, windowsHide: true, maxBuffer: 16 * 1024 * 1024 };

async function run(script, extraArgs = [], timeoutMs = 15 * 60 * 1000, env = {}) {
  console.log(`\n=== GÜNLÜK BESLEYİCİ: ${script} ${extraArgs.join(' ')} ===`);
  try {
    const r = await execFileAsync(process.execPath, ['--experimental-sqlite', path.resolve(script), ...extraArgs],
      { ...opts, env: { ...process.env, ...env }, timeout: timeoutMs });
    console.log(r.stdout);
    if (r.stderr) console.error(r.stderr);
    return true;
  } catch (e) {
    console.error(`[HATA] ${script}:`, e.message);
    if (e.stdout) console.log(e.stdout);
    if (e.stderr) console.error(e.stderr);
    return false;
  }
}

const want = (k) => !ONLY || ONLY.includes(k);

const results = {};
if (want('omarchive')) results.omarchive = await run('scripts/_om_archive_gunluk.mjs', [], 25 * 60 * 1000);
// OM önceki günlük: yalnız son 15 günü tazele (tam arşivi her gün çekme). Tahmin hedefi "dün" — önceki-koşu
// API ~1 gün gecikmeli, bu yüzden hedef en taze dün olur; gerisi zaten backfill'de var.
if (want('omprev')) results.omprev = await run('scripts/om_previous_backfill.mjs', [], 25 * 60 * 1000, { OM_PREV_TAIL_DAYS: '15' });
// NOAA AI ileri: en güncel init'i (bugünden geriye 12Z>00Z) bulup ileri hedefler için kaydeder.
// FROM=bugün (varsayılan 'yarın' DEĞİL): en yeni init ~dün olduğundan hedef=bugün → horizon_days=1 (t1)
// satırı yazılır; yarın settlement gelince skorlanır. 'yarın' kalsaydı t1 hep atlanırdı (ölçüldü: init 09-03'ün h1'i yok).
const todayIso = new Date().toISOString().slice(0, 10);
if (want('ai')) results.ai = await run('scripts/_aiwp_forward.mjs', [], 55 * 60 * 1000, { AIWP_FROM: todayIso });

console.log('\n--- GÜNLÜK BESLEYİCİ ÖZETİ ---');
for (const [k, ok] of Object.entries(results)) console.log(`${k.padEnd(10)} ${ok ? 'TAMAM' : 'HATA'}`);
const allOk = Object.values(results).every(Boolean);
console.log(allOk ? 'Bitti (hepsi tamam).' : 'Bitti (en az bir adım HATA — yukarıdaki loga bak).');
process.exit(allOk ? 0 : 1);
