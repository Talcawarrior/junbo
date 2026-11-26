"""Polymarket odul rekabeti kaydedici: hedef marketlerin defterinden dakikalik Q ozeti (2026-11-26).

SALT-OKUMA arastirma betigi: emir gondermez, hesap/cuzdan verisi istemez ve saklamaz.
Amac: gecmis defter goruntusu yayinlanmadigi icin odul payi ancak ileriye donuk kayitla olculur.
Polymarket puanlamasi dakikada bir (gunde 1.440) ornekle calisir; bu betik de dakikada bir ornek alir.

Her ornekte market basina SAKLANAN (ham defter saklanmaz, yalnizca odul hesabina yeten ozet):
  ts, market no, duzeltilmis mid, en iyi alis/satis, Q_one, Q_two (rakip; v icinde, >= min size),
  gunluk oran, v (max spread), min size, tick. Degerler olcekli tamsayi (satir ~45 bayt, ~50 MB/gun).
Formul ve yaklasimlar: research/poly_rewards_scan.py basligi (resmi belge 2026-11-26).

Hedef kume (her 30 dk yenilenir): odullu TUM hava marketleri + diger kategorilerde gunluk orani en yuksek
--top market (varsayilan 300). Tum 8.5 bin marketi dakikada bir cekmek ~46 MB/dk indirme eder; bu yuzden secim.
Tur sirasi (2026-11-26 aksam duzeltmesi): ONCE ornek (etiket = turun basladigi dakika; istekler _BUDGET_S = 50 sn
icinde biter, cunku gorev 1 dk'da bir tetiklenir ve IgnoreNew), SONRA gerekiyorsa hedef yenileme (_REFRESH_BUDGET_S).

Kullanim:
  python research/poly_rewards_recorder.py --once              # tek ornek (zamanlanmis gorev icin)
  python research/poly_rewards_recorder.py --loop --minutes 5  # elle deneme
  python research/poly_rewards_recorder.py --report [--capital 100]   # biriken kayittan odul payi raporu
DB: <JUNBO_DATA_DIR>/poly_rewards_recorder.db   Rapor: reports/poly_rewards_recorder_YYYYMMDD.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

import poly_rewards_scan as _scan  # noqa: E402  (ayni klasor; proxy + DNS ayari onun icinde)

from utils.db_paths import get_data_dir  # noqa: E402

_REFRESH_S = 1800
# Tur butceleri (sn, t0'dan itibaren). Gorev 60 sn'de bir tetiklenir ve IgnoreNew: tur (import ~2.3 sn dahil)
# 60 sn'yi asarsa sonraki tetik atlanir ve o dakika da kaybolur.
#   ornek: _BUDGET_S icinde biter -> WARP kopuklugunda bile sonraki dakika calisir.
#   yenileme (30 dk'da bir, ornekten SONRA): ~15-55 sn surer; 50 sn ile sinirlanirsa yavas anlarda surekli
#   ertelenip gunluk yeni hava marketleri hedefe hic girmeyebilirdi -> _REFRESH_BUDGET_S'e kadar surebilir
#   (uzun surerse yalnizca sonraki dakika atlanir).
_BUDGET_S = 50.0
_REFRESH_BUDGET_S = 110.0
# Olcekler: fiyat x10000, Q x10, oran x100, v x10, tick x10000
_PX, _QS, _RS, _VS = 10000, 10, 100, 10
_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
  id INTEGER PRIMARY KEY, cid TEXT UNIQUE, yes_token TEXT, question TEXT, event_slug TEXT, category TEXT,
  end_date TEXT, first_seen INTEGER);
CREATE TABLE IF NOT EXISTS targets (id INTEGER PRIMARY KEY, rate REAL, v REAL, min_size REAL);
CREATE TABLE IF NOT EXISTS samples (
  ts INTEGER, mk INTEGER, mid INTEGER, bb INTEGER, ba INTEGER, q1 INTEGER, q2 INTEGER, rate INTEGER,
  v INTEGER, min_size INTEGER, tick INTEGER, PRIMARY KEY (ts, mk)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def _log(msg: str) -> None:
    """Gozetimsiz calisma icin yalniz hedef yenileme ve hata satirlari (pythonw altinda stdout yok)."""
    try:
        path = Path(get_data_dir()) / "logs" / "poly_rewards_recorder.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


def _db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_SCHEMA)
    return con


def _refresh_targets(con: sqlite3.Connection, top: int) -> int:
    cur = _scan._fetch_current()
    weather = _scan._fetch_multi({"tag_slug": "weather"})
    meta = _scan._fetch_multi({})
    meta.update(weather)
    wset = {cid for cid in weather if cid in cur}
    others = sorted((cid for cid in cur if cid not in wset), key=lambda c: -float(cur[c].get("total_daily_rate") or 0))
    now = int(time.time())
    _log(f"hedef yenileme: aktif odullu {len(cur)}, hava {len(wset)}, diger ilk {top}")
    chosen = []
    with con:
        for cid in list(wset) + others[:top]:
            m = meta.get(cid) or {}
            toks = m.get("tokens") or []
            yt = next((t for t in toks if str(t.get("outcome", "")).lower() == "yes"), toks[0] if toks else None)
            if not yt or not yt.get("token_id"):
                continue
            con.execute(
                "INSERT OR IGNORE INTO markets (cid, yes_token, question, event_slug, category, end_date, first_seen) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    cid,
                    str(yt["token_id"]),
                    (m.get("question") or "").replace("\n", " "),
                    m.get("event_slug") or "",
                    "weather" if cid in wset else "top",
                    m.get("end_date") or "",
                    now,
                ),
            )
            mk = con.execute("SELECT id FROM markets WHERE cid=?", (cid,)).fetchone()[0]
            c = cur[cid]
            chosen.append(
                (
                    mk,
                    float(c.get("total_daily_rate") or 0),
                    float(c.get("rewards_max_spread") or 0),
                    float(c.get("rewards_min_size") or 0),
                )
            )
        con.execute("DELETE FROM targets")
        con.executemany("INSERT INTO targets VALUES (?,?,?,?)", chosen)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('targets_refreshed_at', ?)", (str(now),))
    return len(chosen)


def _refresh_due(con: sqlite3.Connection) -> bool:
    row = con.execute("SELECT v FROM meta WHERE k='targets_refreshed_at'").fetchone()
    return not row or time.time() - int(row[0]) > _REFRESH_S


def _sample_minute(con: sqlite3.Connection, ts: int) -> tuple[int, int]:
    """Hedef marketlerin defterini tek seferde ceker ve `ts` dakikasina yazar. Donus: (hedef, kayit)."""
    targets = con.execute(
        "SELECT t.id, m.yes_token, t.rate, t.v, t.min_size FROM targets t JOIN markets m ON m.id = t.id"
    ).fetchall()
    books = _scan._fetch_books(sorted({t[1] for t in targets}))
    out = []
    for mk, tok, rate, v, min_size in targets:
        b = books.get(tok)
        if not b or v <= 0:
            continue
        bids, asks = _scan._levels(b, "bids"), _scan._levels(b, "asks")
        mid, bb, ba = _scan._adjusted_mid(bids, asks, min_size)
        if mid is None or bb is None or ba is None:
            continue
        q1 = _scan._side_q(bids, mid, v, min_size)
        q2 = _scan._side_q(asks, mid, v, min_size)
        tick = float(b.get("tick_size") or 0.01)
        out.append(
            (
                ts,
                mk,
                round(mid * _PX),
                round(bb * _PX),
                round(ba * _PX),
                round(q1 * _QS),
                round(q2 * _QS),
                round(rate * _RS),
                round(v * _VS),
                round(min_size),
                round(tick * _PX),
            )
        )
    out.sort(key=lambda r: r[1])  # (ts, mk) sirali ekleme -> B-agac sayfalari dolu kalir
    with con:
        con.executemany("INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?)", out)
    return len(targets), len(out)


def _tick(con: sqlite3.Connection, top: int, t0: float) -> tuple[int, int, list[str]]:
    """Bir dakikalik tur: ONCE ornek (etiket = turun basladigi dakika), SONRA gerekiyorsa hedef yenileme.

    2026-11-26 aksam duzeltmesi: yenileme (~15-55 sn) ornekten once calisinca ornek sonraki dakikanin etiketini
    aliyor, sonraki tur da ayni etiketin ustune yaziyordu -> her yenilemede 1 dakika bos kaliyordu.
    Yenileme basarisiz olursa eski hedeflerle devam edilir; zamani gecmis kaldigi icin sonraki tur (orneginden
    sonra) yeniden dener. Ag istekleri ornekte t0 + _BUDGET_S'e, yenilemede t0 + _REFRESH_BUDGET_S'e kadar
    (poly_rewards_scan._DEADLINE). Donus: (hedef, kayit, hata satirlari).
    """
    errors: list[str] = []

    def note(step: str, exc: Exception) -> None:
        errors.append(f"{step} {type(exc).__name__}: {str(exc)[:300]}")

    def refresh() -> None:
        _scan._DEADLINE = t0 + _REFRESH_BUDGET_S
        try:
            _refresh_targets(con, top)
        except Exception as exc:  # noqa: BLE001 - eski hedeflerle devam; log + LastTaskResult ile gorunur
            note("yenileme", exc)

    n_t = n_s = 0
    _scan._DEADLINE = t0 + _BUDGET_S
    try:
        first = con.execute("SELECT 1 FROM targets LIMIT 1").fetchone() is None
        if first:
            refresh()  # bos DB: hedef listesi olmadan ornek alinamaz (bu turun dakikasi zaten gecikti)
        try:
            n_t, n_s = _sample_minute(con, int(t0 // 60 * 60))
        except Exception as exc:  # noqa: BLE001 - kayip dakika log + LastTaskResult ile gorunur
            note("ornek", exc)
        if not first and _refresh_due(con):
            refresh()
    finally:
        _scan._DEADLINE = None
    return n_t, n_s, errors


def _report(con: sqlite3.Connection, capital: float, out_path: Path) -> None:
    mkt = {r[0]: r for r in con.execute("SELECT id, question, event_slug, category FROM markets")}
    # (mk, gun) -> [n, pay toplami, oran toplami, bos pencere]
    per: defaultdict[tuple[int, str], list[float]] = defaultdict(lambda: [0, 0.0, 0.0, 0])
    n_rows = 0
    for ts, mk, mid, q1, q2, rate, v, min_size, tick in con.execute(
        "SELECT ts, mk, mid, q1, q2, rate, v, min_size, tick FROM samples"
    ):
        n_rows += 1
        mid_f, v_f, rate_f = mid / _PX, v / _VS, rate / _RS
        oth = _scan._others_upper(q1 / _QS, q2 / _QS, mid_f)
        ours, _cap, _n = _scan._our_quote(mid_f, v_f, float(min_size), tick / _PX, capital, v_f / 2.0)
        share = ours / (ours + oth) if ours > 0 else 0.0
        day = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()
        p = per[(mk, day)]
        p[0] += 1
        p[1] += share
        p[2] += rate_f
        p[3] += int(oth <= 0)
    lines = [
        f"# Odul rekabeti kaydi raporu ({dt.date.today():%Y-%m-%d})",
        "",
        f"Ornek satiri: {n_rows:,}; market-gun: {len(per):,}. ${capital:,.0f} iki tarafli kotasyon, mid -/+ v/2; "
        "odul = gun icindeki orneklerin ortalama payi x ortalama gunluk oran (eksik dakikalar ortalamaya girmez). "
        "Dolum kaybi dahil degil. `bos pencere`: rakipsiz ornek orani (o dakikada havuzun tamami bize yazilir; "
        "fiyat kesfi zayif, ters secim riski yuksek).",
        "",
        "| gun (UTC) | kategori | market-gun | ort. ornek | bos pencere | tahmini odul $/gun (toplam) | "
        "medyan $/gun/market |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    agg = defaultdict(list)
    for (mk, day), (n, ssum, rsum, empty) in per.items():
        cat = (mkt.get(mk) or (None, "", "", "?"))[3]
        agg[(day, cat)].append((n, (ssum / n) * (rsum / n), empty))
    for (day, cat), vals in sorted(agg.items()):
        n_all = sum(v[0] for v in vals)
        lines.append(
            f"| {day} | {cat} | {len(vals)} | {statistics.fmean(v[0] for v in vals):.0f} | "
            f"%{100 * sum(v[2] for v in vals) / max(n_all, 1):.0f} | ${sum(v[1] for v in vals):,.2f} | "
            f"${statistics.median(v[1] for v in vals):,.2f} |"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"yazildi: {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--minutes", type=int, default=5)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--top", type=int, default=300)
    ap.add_argument("--capital", type=float, default=100.0)
    ap.add_argument("--db", default=str(Path(get_data_dir()) / "poly_rewards_recorder.db"))
    args = ap.parse_args()
    con = _db(Path(args.db))
    if args.report:
        _report(con, args.capital, REPO / "reports" / f"poly_rewards_recorder_{dt.date.today():%Y%m%d}.md")
        return
    runs = 1 if args.once or not args.loop else max(1, args.minutes)
    failed = 0
    for i in range(runs):
        t0 = time.time()
        n_t, n_s, errors = _tick(con, args.top, t0)
        for err in errors:
            _log(f"HATA {err}")  # gorev sonucu (LastTaskResult=1) + log ile gorunur
        if errors:
            failed += 1
        print(
            f"{dt.datetime.now():%H:%M:%S} ornek {i + 1}/{runs}: hedef {n_t}, kayit {n_s}, {time.time() - t0:.1f} sn"
            + (f", hata {len(errors)}" if errors else ""),
            flush=True,
        )
        if i + 1 < runs:
            time.sleep(max(0.0, 60.0 - (time.time() - t0)))
    con.close()
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
