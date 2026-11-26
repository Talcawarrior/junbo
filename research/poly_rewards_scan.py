"""Polymarket likidite odulu taramasi: hangi market ne kadar oduyor + anlik rekabet (2026-11-26).

SALT-OKUMA arastirma betigi. Emir gondermez, hesap/cuzdan bilgisi istemez ve saklamaz.
Botun proxy + DNS yolu (config.settings -> WARP SOCKS) kullanilir; proxy degeri yazdirilmaz.

Kaynaklar (public, kimlik dogrulamasi YOK):
  clob /rewards/markets/current  aktif odul konfigurasyonu (native + toplam gunluk oran, max spread, min size)
  clob /rewards/markets/multi    market adi, event, token, hacim, competitiveness; tag_slug ile kategori
  clob POST /books               anlik emir defteri. YES defteri NO defterinin aynasidir (2026-11-26
                                 olcumu: 52/52 seviye ayni boyut) -> yalnizca YES defteri okunur.

Odul formulu (docs.polymarket.com/programs/liquidity-rewards, 2026-11-26 okundu):
  S(v, s) = ((v - s) / v)^2      v = max spread (sent), s = emrin duzeltilmis mid'e uzakligi (sent)
  Q_one = YES alis tarafi (+ NO satis), Q_two = YES satis tarafi (+ NO alis)
  Q_min = max(min(Q1, Q2), max(Q1, Q2) / 3)   mid [0.10, 0.90] icindeyse (tek tarafli 1/3 sayilir)
  Q_min = min(Q1, Q2)                          disindaysa (iki taraf sart)
  pay = Q_min / toplam(Q_min); gunde 1440 ornek; odul = gunluk pay ortalamasi x gunluk havuz
Yaklasimlar:
  * Duzeltilmis mid: boyutu >= min_size olan en iyi alis/satis seviyelerinin ortasi (defter seviye
    bazinda toplu oldugu icin tek tek emir ayrilamaz; seviye boyutu emir boyutu sayilir).
  * Rakiplerin toplam Q_min'i emir sahibi bilinmedigi icin UST SINIR alinir:
    [0.10, 0.90]: min(Q1, Q2) + |Q1 - Q2| / 3 ; disinda: min(Q1, Q2). Bizim pay icin TEMKINLI.
  * Tahmin "defter gun boyu bu anki gibi kalir ve emrimiz hic dolmaz" varsayar. Dolum / ters secim
    kaybi bu taramada YOKTUR (research/poly_mm_reward_backtest.py ayri olcer).

Kullanim:  python research/poly_rewards_scan.py [--capital 100] [--out-dir reports]
Cikti:     <out-dir>/poly_rewards_scan_YYYYMMDD.csv ve .md
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import requests  # noqa: E402

from config.settings import bot_config  # noqa: E402  (proxy + DNS duzeltmesi; deger yazdirilmaz)

CLOB = "https://clob.polymarket.com"
_PROXY = bot_config.polymarket.proxy_url
_PX = {"http": _PROXY, "https": _PROXY} if _PROXY else None
_UA = {"User-Agent": "research-script/1.0"}
_END = {"", "LTE="}
_C = 3.0  # tek tarafli olcek faktoru (belgede: tum marketlerde 3.0)
# Kategori onceligi: bir market birden cok etikette olabilir; ilk eslesen kazanir.
_TAGS = [
    "weather",
    "crypto",
    "esports",
    "sports",
    "elections",
    "politics",
    "geopolitics",
    "economy",
    "finance",
    "business",
    "tech",
    "science",
    "culture",
    "pop-culture",
    "mentions",
    "world",
]


def _get(path: str, params: dict | None = None, tries: int = 6):
    for attempt in range(tries):
        try:
            r = requests.get(CLOB + path, params=params, proxies=_PX, headers=_UA, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(3 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            time.sleep(3 * (attempt + 1))  # WARP anlik kopuklugu / gecici hata
    raise RuntimeError(f"GET {path} {tries} denemede basarisiz")


def _post_books(token_ids: list[str], tries: int = 6) -> list[dict]:
    for attempt in range(tries):
        try:
            r = requests.post(
                CLOB + "/books", json=[{"token_id": t} for t in token_ids], proxies=_PX, headers=_UA, timeout=90
            )
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(3 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"POST /books ({len(token_ids)} token) basarisiz")


def _pages(path: str, params: dict, stop=None):
    """next_cursor ile sayfala. stop(page) True donerse durur."""
    cur = ""
    while True:
        prm = dict(params)
        if cur:
            prm["next_cursor"] = cur
        j = _get(path, prm)
        data = j.get("data") or []
        yield data
        cur = j.get("next_cursor") or ""
        if not data or cur in _END or (stop and stop(data)):
            return


def _fetch_current() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for page in _pages("/rewards/markets/current", {}):
        for m in page:
            out[m["condition_id"]] = m
    return out


def _rate(m: dict) -> float:
    return sum(float(c.get("rate_per_day") or 0) for c in (m.get("rewards_config") or []))


def _fetch_multi(params: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    base = {"order_by": "rate_per_day", "position": "DESC", "page_size": 500, **params}
    for page in _pages("/rewards/markets/multi", base, stop=lambda d: max((_rate(m) for m in d), default=0) <= 0):
        for m in page:
            out[m["condition_id"]] = m
    return out


def _fetch_books(token_ids: list[str], batch: int = 500) -> dict[str, dict]:
    books: dict[str, dict] = {}
    for i in range(0, len(token_ids), batch):
        for b in _post_books(token_ids[i : i + batch]):
            books[str(b.get("asset_id"))] = b
    return books


def _levels(book: dict, side: str) -> list[tuple[float, float]]:
    return [(float(x["price"]), float(x["size"])) for x in (book.get(side) or [])]


def _adjusted_mid(bids, asks, min_size: float):
    """Boyutu >= min_size olan en iyi alis/satis seviyelerinin ortasi; yoksa ham en iyi seviyeler."""
    qb = [p for p, s in bids if s >= min_size]
    qa = [p for p, s in asks if s >= min_size]
    bb = max(qb) if qb else (max(p for p, _ in bids) if bids else None)
    ba = min(qa) if qa else (min(p for p, _ in asks) if asks else None)
    if bb is None or ba is None or ba <= bb:
        return None, bb, ba
    return (bb + ba) / 2.0, bb, ba


def _score(v: float, s_cents: float) -> float:
    return ((v - s_cents) / v) ** 2 if 0 <= s_cents < v else 0.0


def _side_q(levels, mid: float, v: float, min_size: float) -> float:
    return sum(_score(v, abs(mid - p) * 100.0) * s for p, s in levels if s >= min_size)


def _qmin(q1: float, q2: float, mid: float) -> float:
    if 0.10 <= mid <= 0.90:
        return max(min(q1, q2), max(q1, q2) / _C)
    return min(q1, q2)


def _others_upper(q1: float, q2: float, mid: float) -> float:
    """Rakiplerin toplam Q_min'i icin ust sinir (kitap duzeyinden; emir sahibi bilinmez)."""
    if 0.10 <= mid <= 0.90:
        return min(q1, q2) + abs(q1 - q2) / _C
    return min(q1, q2)


def _our_quote(mid: float, v: float, min_size: float, tick: float, capital: float, d_cents: float):
    """Iki tarafli kotasyon: mid -/+ d. Donus (pay hesaplamasi icin Q_min, sermaye, pay sayisi)."""
    bid = math.floor((mid - d_cents / 100.0) / tick + 1e-9) * tick
    ask = math.ceil((mid + d_cents / 100.0) / tick - 1e-9) * tick
    if bid < tick or ask > 1.0 - tick:
        return 0.0, 0.0, 0.0
    width = ask - bid
    n = max(min_size, capital / max(1.0 - width, 1e-6))
    q1 = _score(v, (mid - bid) * 100.0) * n
    q2 = _score(v, (ask - mid) * 100.0) * n
    return _qmin(q1, q2, mid), n * (1.0 - width), n


def _fmt(x: float, nd: int = 2) -> str:
    return f"{x:,.{nd}f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--capital", type=float, default=100.0, help="market basina varsayimsal iki tarafli sermaye ($)")
    ap.add_argument("--out-dir", default=str(REPO / "reports"))
    args = ap.parse_args()
    t0 = time.time()
    today = dt.datetime.now(dt.timezone.utc).date()

    cur = _fetch_current()
    meta = _fetch_multi({})
    print(f"aktif odullu market: {len(cur)} | multi meta: {len(meta)}", flush=True)
    cat: dict[str, str] = {}
    for tag in _TAGS:
        ids = _fetch_multi({"tag_slug": tag})
        for cid in ids:
            cat.setdefault(cid, tag)
        print(f"  etiket {tag}: {len(ids)}", flush=True)

    yes_tok: dict[str, str] = {}
    for cid in cur:
        toks = (meta.get(cid) or {}).get("tokens") or []
        yt = next((t for t in toks if str(t.get("outcome", "")).lower() == "yes"), toks[0] if toks else None)
        if yt and yt.get("token_id"):
            yes_tok[cid] = str(yt["token_id"])
    books = _fetch_books(sorted(set(yes_tok.values())))
    print(f"defter: {len(books)} / {len(yes_tok)} | {time.time() - t0:.0f} sn", flush=True)

    rows: list[dict[str, Any]] = []
    for cid, c in cur.items():
        m = meta.get(cid) or {}
        total = float(c.get("total_daily_rate") or 0)
        native = float(c.get("native_daily_rate") or 0)
        v = float(c.get("rewards_max_spread") or 0)
        min_size = float(c.get("rewards_min_size") or 0)
        row = {
            "condition_id": cid,
            "question": (m.get("question") or "").replace("\n", " "),
            "event_slug": m.get("event_slug") or "",
            "category": cat.get(cid, "other"),
            "total_daily_rate": total,
            "native_daily_rate": native,
            "sponsored_daily_rate": max(total - native, 0.0),
            "max_spread_c": v,
            "min_size": min_size,
            "competitiveness": m.get("market_competitiveness"),
            "volume_24hr": m.get("volume_24hr"),
            "end_date": m.get("end_date") or "",
        }
        book = books.get(yes_tok.get(cid, ""))
        if book and v > 0:
            bids, asks = _levels(book, "bids"), _levels(book, "asks")
            tick = float(book.get("tick_size") or 0.01)
            mid, bb, ba = _adjusted_mid(bids, asks, min_size)
            if mid is not None:
                q1 = _side_q(bids, mid, v, min_size)
                q2 = _side_q(asks, mid, v, min_size)
                oth = _others_upper(q1, q2, mid)
                row.update(best_bid=bb, best_ask=ba, adj_mid=round(mid, 4), q_one=round(q1, 2), q_two=round(q2, 2))
                row["others_qmin_upper"] = round(oth, 2)
                row["empty_window"] = int(oth <= 0)
                for label, d in (("half", v / 2.0), ("1c", min(1.0, v * 0.75))):
                    ours, cap, _n = _our_quote(mid, v, min_size, tick, args.capital, d)
                    share = ours / (ours + oth) if ours > 0 else 0.0
                    row[f"share_{label}"] = round(share, 4)
                    row[f"capital_{label}"] = round(cap, 2)
                    row[f"est_day_{label}"] = round(total * share, 2)
                    row[f"yield_{label}_pct"] = round(100.0 * total * share / cap, 2) if cap > 0 else 0.0
        rows.append(row)
    rows.sort(key=lambda r: -r["total_daily_rate"])

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / f"poly_rewards_scan_{today:%Y%m%d}"
    fields: list[str] = []
    for r in rows:
        fields += [k for k in r if k not in fields]
    with open(f"{stem}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    _write_md(f"{stem}.md", rows, args.capital, today, time.time() - t0)
    print(f"yazildi: {stem}.csv / .md | {len(rows)} market | {time.time() - t0:.0f} sn")


def _write_md(path: str, rows: list[dict], capital: float, today, secs: float) -> None:
    tot = sum(r["total_daily_rate"] for r in rows)
    nat = sum(r["native_daily_rate"] for r in rows)
    priced = [r for r in rows if "share_half" in r]
    L = [
        f"# Polymarket likidite odulu taramasi ({today:%Y-%m-%d})",
        "",
        f"Aktif odullu market: **{len(rows):,}**; gunluk toplam havuz **${_fmt(tot, 0)}** "
        f"(Polymarket native ${_fmt(nat, 0)}, sponsorlu ${_fmt(tot - nat, 0)}). Defteri okunan: {len(priced):,}. "
        f"Sure {secs:.0f} sn.",
        "",
        f"Tahmin sutunlari: market basina **${capital:,.0f}** iki tarafli kotasyon, mid -/+ (max spread / 2); "
        "rakip toplami ust sinir (temkinli); defter gun boyu sabit ve emir hic dolmuyor varsayimi. "
        "Dolum / ters secim kaybi DAHIL DEGIL.",
        "",
        "## Kim ne kadar oduyor: kategori",
        "",
        "| kategori | market | gunluk havuz | pay | medyan market | bos pencere | tahmini $/gun (her markete "
        f"${capital:,.0f}) | medyan getiri %/gun |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["category"], []).append(r)
    for k, rs in sorted(by.items(), key=lambda kv: -sum(r["total_daily_rate"] for r in kv[1])):
        s = sum(r["total_daily_rate"] for r in rs)
        pr = [r for r in rs if "share_half" in r]
        est = sum(r["est_day_half"] for r in pr)
        ys = [r["yield_half_pct"] for r in pr if r["capital_half"] > 0 and not r["empty_window"]]
        L.append(
            f"| {k} | {len(rs):,} | ${_fmt(s, 0)} | %{100 * s / tot:.1f} | "
            f"${_fmt(statistics.median(r['total_daily_rate'] for r in rs), 1)} | "
            f"{sum(r.get('empty_window', 0) for r in pr):,} | ${_fmt(est, 0)} | "
            f"{_fmt(statistics.median(ys), 2) if ys else '-'} |"
        )
    L += [
        "",
        "## En cok odeyen 30 market",
        "",
        f"| gunluk | kategori | market | max spread | min size | rakip Q | tahmini $/gun (${capital:,.0f}) |",
        "|---:|---|---|---:|---:|---:|---:|",
    ]
    for r in rows[:30]:
        L.append(
            f"| ${_fmt(r['total_daily_rate'], 0)} | {r['category']} | {r['question'][:70]} | {r['max_spread_c']} | "
            f"{r['min_size']:.0f} | {_fmt(r.get('others_qmin_upper', float('nan')), 0)} | "
            f"${_fmt(r.get('est_day_half', 0.0), 2)} |"
        )
    comp = [r for r in priced if not r["empty_window"] and r["capital_half"] > 0]
    comp.sort(key=lambda r: -r["yield_half_pct"])
    L += [
        "",
        f"## Rekabete gore en verimli 30 market (bos pencere haric, ${capital:,.0f} ile)",
        "",
        "| getiri %/gun | tahmini $/gun | gunluk havuz | kategori | market | mid | rakip Q | pay |",
        "|---:|---:|---:|---|---|---:|---:|---:|",
    ]
    for r in comp[:30]:
        L.append(
            f"| {_fmt(r['yield_half_pct'], 2)} | ${_fmt(r['est_day_half'], 2)} | ${_fmt(r['total_daily_rate'], 0)} | "
            f"{r['category']} | {r['question'][:60]} | {r['adj_mid']:.3f} | {_fmt(r['others_qmin_upper'], 0)} | "
            f"%{100 * r['share_half']:.1f} |"
        )
    w = [r for r in rows if r["category"] == "weather"]
    if w:
        ws = sum(r["total_daily_rate"] for r in w)
        dist = sorted(_count((r["max_spread_c"], r["min_size"]) for r in w).items())
        L += [
            "",
            "## Hava marketleri",
            "",
            f"{len(w):,} market, gunluk ${_fmt(ws, 0)} "
            f"(toplamin %{100 * ws / tot:.1f}'i). Max spread / min size dagilimi: "
            + ", ".join(f"{k}: {n}" for k, n in dist),
            "",
            "| event (sehir-gun) | market | gunluk toplam | en yuksek kova | tahmini $/gun (her kovaya "
            f"${capital:,.0f}) |",
            "|---|---:|---:|---:|---:|",
        ]
        ev: dict[str, list[dict]] = {}
        for r in w:
            ev.setdefault(r["event_slug"], []).append(r)
        for slug, rs in sorted(ev.items(), key=lambda kv: -sum(r["total_daily_rate"] for r in kv[1]))[:25]:
            L.append(
                f"| {slug[:60]} | {len(rs)} | ${_fmt(sum(r['total_daily_rate'] for r in rs), 0)} | "
                f"${_fmt(max(r['total_daily_rate'] for r in rs), 0)} | "
                f"${_fmt(sum(r.get('est_day_half', 0.0) for r in rs), 2)} |"
            )
    L += [
        "",
        "## Notlar",
        "",
        "- Kaynak: clob `/rewards/markets/current` (aktif oran), `/rewards/markets/multi` (ad, etiket), "
        "`POST /books` (anlik defter). Hesap/cuzdan verisi kullanilmadi.",
        "- `bos pencere`: max spread icinde min size'i gecen rakip emir yok; tahmin havuzun tamamini verir ama "
        "fiyat kesfi zayif, ters secim riski yuksek -> getiri siralamasindan cikarildi.",
        "- Gercek odul, gun icindeki 1.440 ornegin ortalamasidir; tek anlik defter gun ici degisimi gormez. "
        "Bunun icin `research/poly_rewards_recorder.py` dakikalik kayit tutar.",
        "- Odul tahmini dolum kaybini icermez; hava marketleri icin dolum / ters secim olcumu "
        "`research/poly_mm_reward_backtest.py` raporundadir.",
    ]
    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")


def _count(it) -> dict:
    out: dict = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return out


if __name__ == "__main__":
    main()
