"""Hava marketlerinde iki tarafli kotasyon: dolum + ters secim kaybi backtest'i (2026-11-26, SALT-OKUMA).

Sorular: (1) "Odul olmasa bu kotasyon ne kadar kaybettirirdi?"  (2) "Mid yerine modelin fair value'su
etrafinda kotasyon yapmak kaybi ne kadar azaltir?"  Odul PAYI gecmis veriyle hesaplanamaz: orderbook.db
yalnizca en iyi alis/satis ve toplam derinlik tutar (seviye boyutu yok). Bu betik dolum PnL'ini ve odul
penceresindeki Q-dakikayi olcer; odul geliri tarama (poly_rewards_scan.py) ve dakikalik kayit
(poly_rewards_recorder.py) ile karsilastirilir. Emir gondermez; hesap/cuzdan verisi kullanmaz.

Veri:
  bot.db weather_markets (temperature_max, resmi sonuc), metar_observations (gun ici max, recorded_at)
  orderbook.db orderbook_snapshots (best_bid / best_ask, ~11.5 dk) -> mid yolu ve kotasyon yenileme anlari
  tempmarket-edge.sqlite prediction_log (run_at_utc ile sizintisiz nokta tahmin), observations (kalibrasyon)
  islem onbellegi: data-api.polymarket.com/v2/trades, yalnizca TAKER kayitlari [t, side, outcome, price, size]
Dolum: kotasyon her snapshot'ta yenilenir, aradaki gercek taker islemleri dolum tetikler (YES esdegeri).
  temkinli: islem fiyati bizim fiyatimizi ASAR (alis icin < bid, satis icin > ask) -- kuyruk arkasi
  iyimser : islem fiyati bizim fiyatimiza ESIT ya da asar -- kuyruk onu
FV: kova olasiligi, Polymarket yuvarlamasi (tam derece, floor(x+0.5)); T ~ N(mu, sigma_h).
  mu = son prediction_log.final_prediction (run_at <= t) - kalibrasyon donemi sapmasi (ufka gore);
  sigma_h = kalibrasyon donemi sapma duzeltilmis hata std (ufka gore). Kalibrasyon test doneminden ONCE.
  Yerel hedef gunde: final max = max(m, R), m = METAR gun ici max (recorded_at <= t), R ~ N(mu, sigma_0).
Odul parametresi (gecmis bilinmez): bugunku hava marketleri gibi v = 4.5 sent, min size = 20.

Kullanim: python research/poly_mm_reward_backtest.py [--start 2026-11-12] [--end 2026-11-24]
          [--cache <islem onbellegi>] [--scan reports/poly_rewards_scan_YYYYMMDD.csv]
Cikti:    reports/poly_mm_reward_backtest_YYYYMMDD.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
import re
import sqlite3
import statistics
import sys
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scrapers.metar import city_utc_offset  # noqa: E402
from utils.db_paths import get_data_dir, get_db_path  # noqa: E402
from utils.market_outcome import parse_resolved_outcome  # noqa: E402
from utils.probability import normal_cdf  # noqa: E402

V_CENTS, MIN_SIZE, N_SHARES, MAX_POS, TICK = 4.5, 20.0, 100.0, 300.0, 0.01
_EPS = 1e-9
_STATION_ALIAS = {"MPTO": "MPMG"}  # Panama: market kodu MPTO, cozum istasyonu MPMG
# (ad, merkez, d sent, taraf kurali, yalniz gun-oncesi)
VARIANTS = [
    ("MID d=1c", "mid", 1.0, "both", False),
    ("MID d=v/2", "mid", V_CENTS / 2, "both", False),
    ("FV-SKEW d=v/2", "fvskew", V_CENTS / 2, "both", False),
    ("FV-TEK-TARAF d=v/2", "mid", V_CENTS / 2, "fv", False),
    ("MID d=v/2 gun-oncesi", "mid", V_CENTS / 2, "both", True),
    ("FV-SKEW d=v/2 gun-oncesi", "fvskew", V_CENTS / 2, "both", True),
]
RULES = ("temkinli", "iyimser")


def _ts(s) -> float:
    s = str(s).replace("T", " ").replace("Z", "").split("+")[0].strip()
    return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()


def _edges_c(question: str, mtype: str):
    """Sorudan kova kenarlari (C). Donus (lo, hi); acik uc icin -inf / +inf. Ayristirilamazsa None."""
    q = question.replace(chr(0xB0), " ").replace(chr(0xBA), " ")  # derece isareti -> bosluk
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:-\s*(-?\d+(?:\.\d+)?))?\s*([CF])\b", q)
    if not m:
        return None
    a = float(m.group(1))
    b = float(m.group(2)) if m.group(2) else a
    unit = m.group(3)
    low_q = "or below" in q or "or lower" in q
    high_q = "or higher" in q or "or above" in q
    if mtype == "LOW" or low_q:
        lo, hi = -math.inf, b + 0.5
    elif mtype == "HIGH" or high_q:
        lo, hi = a - 0.5, math.inf
    else:
        lo, hi = a - 0.5, b + 0.5
    if unit == "F":
        lo = (lo - 32.0) * 5.0 / 9.0 if math.isfinite(lo) else lo
        hi = (hi - 32.0) * 5.0 / 9.0 if math.isfinite(hi) else hi
    return lo, hi


def _cdf_max(x: float, mu: float, sigma: float, m: float | None) -> float:
    """P(final max < x); gun icindeyse final = max(m, R)."""
    if not math.isfinite(x):
        return 1.0 if x > 0 else 0.0
    if m is not None and m >= x:
        return 0.0
    return normal_cdf((x - mu) / sigma)


def _bucket_prob(edges, mu: float, sigma: float, m: float | None) -> float:
    lo, hi = edges
    return max(0.0, min(1.0, _cdf_max(hi, mu, sigma, m) - _cdf_max(lo, mu, sigma, m)))


def _score(s_cents: float) -> float:
    return ((V_CENTS - s_cents) / V_CENTS) ** 2 if 0 <= s_cents < V_CENTS else 0.0


def _qmin(q1: float, q2: float, mid: float) -> float:
    if 0.10 <= mid <= 0.90:
        return max(min(q1, q2), max(q1, q2) / 3.0)
    return min(q1, q2)


def _calibrate_sigma(te: sqlite3.Connection, start: str, end: str) -> dict[int, tuple[float, float]]:
    act = {
        (s, d): float(v)
        for s, d, v in te.execute(
            "SELECT station_id, target_date, MAX(actual_max_c) FROM observations WHERE actual_max_c IS NOT NULL "
            "AND target_date >= ? AND target_date < ? GROUP BY station_id, target_date",
            (start, end),
        )
    }
    err: dict[int, list[float]] = defaultdict(list)
    for s, d, h, fp in te.execute(
        "SELECT station_id, target_date, horizon_days, final_prediction FROM prediction_log "
        "WHERE target_date >= ? AND target_date < ? AND final_prediction IS NOT NULL",
        (start, end),
    ):
        a = act.get((s, d))
        if a is not None:
            err[int(h)].append(float(fp) - a)
    out: dict[int, tuple[float, float]] = {}
    for h, es in sorted(err.items()):
        if len(es) < 200:
            continue
        bias = statistics.fmean(es)
        out[h] = (statistics.pstdev(es), bias)  # sapma duzeltilmis hatanin standart sapmasi, sapma
        rmse = math.sqrt(statistics.fmean(e * e for e in es))
        print(f"  kalibrasyon ufuk {h}: n={len(es)} bias={bias:+.2f} rmse={rmse:.2f} sd={out[h][0]:.2f}")
    return out


def _load_markets(start: str, end: str, cache: Path):
    con = sqlite3.connect(f"file:{get_db_path()}?mode=ro", uri=True, timeout=120)
    rows = []
    offs: dict[tuple, float] = {}
    for mid, code, td, raw, lon, q, mt in con.execute(
        "SELECT id, city_code, target_date, raw_data, longitude, question, market_type FROM weather_markets "
        "WHERE metric='temperature_max' AND market_type IN ('RANGE','LOW','HIGH') AND target_date >= ? "
        "AND target_date < ?",
        (start, end),
    ):
        res = parse_resolved_outcome(raw)
        p = cache / f"{mid}.json"
        if res is None or not p.exists():
            continue
        day = str(td)[:10]
        key = (str(code), day)
        if key not in offs:
            try:
                offs[key] = float(city_utc_offset(str(code), day, lon))
            except Exception:  # noqa: BLE001 - offset bulunamazsa UTC
                offs[key] = 0.0
        d0 = _ts(day + " 00:00:00") - offs[key] * 3600
        rows.append(dict(mid=str(mid), code=str(code), day=day, y=1.0 if res else 0.0, d0=d0, path=p, q=q or "", mt=mt))
    metar: dict[tuple, list] = defaultdict(list)
    for code, day, temp, rec in con.execute(
        "SELECT city_code, day, temp_c, recorded_at FROM metar_observations WHERE day >= ? AND day < ? "
        "AND temp_c IS NOT NULL",
        (start, end),
    ):
        metar[(str(code), str(day)[:10])].append((_ts(rec), float(temp)))
    con.close()
    for v in metar.values():
        v.sort()
    return rows, metar


def _running_max(obs: list, t: float) -> float | None:
    i = bisect_right([o[0] for o in obs], t)
    return max((o[1] for o in obs[:i]), default=None) if i else None


def _simulate(seq, trades, tkeys, mk, fv_at, variant, rule):
    """Tek market x varyant x dolum kurali. Donus sozluk."""
    _name, center_mode, d, sides, preday = variant
    d_end = mk["d0"] + 86400
    pos = cash = 0.0
    st = dict(fills=0, shares=0.0, quote_min=0.0, two_min=0.0, qmin_min=0.0, pre_pnl=0.0, in_pnl=0.0, active=False)
    for (t, bb, ba), (t2, _b2, _a2) in zip(seq, seq[1:]):
        if t >= d_end or (preday and t >= mk["d0"]):
            break
        mid = (bb + ba) / 2.0
        if not 0.03 < mid < 0.97:
            continue
        fv = fv_at(t) if center_mode == "fvskew" or sides == "fv" else None
        center = mid
        if center_mode == "fvskew" and fv is not None:
            k = (V_CENTS - d - 0.5) / 100.0
            center = mid + max(-k, min(k, fv - mid))
        bid = math.floor((center - d / 100.0) / TICK + _EPS) * TICK
        ask = math.ceil((center + d / 100.0) / TICK - _EPS) * TICK
        bid = min(bid, ba - TICK)
        ask = max(ask, bb + TICK)
        use_bid = bid >= TICK and pos < MAX_POS
        use_ask = ask <= 1.0 - TICK and pos > -MAX_POS
        if sides == "fv" and fv is not None:
            if fv >= mid + d / 100.0:
                use_ask = False
            elif fv <= mid - d / 100.0:
                use_bid = False
        if not (use_bid or use_ask):
            continue
        st["active"] = True
        minutes = (min(t2, d_end) - t) / 60.0
        s_b = (mid - bid) * 100.0 if use_bid else V_CENTS
        s_a = (ask - mid) * 100.0 if use_ask else V_CENTS
        q1, q2 = _score(s_b) * N_SHARES, _score(s_a) * N_SHARES
        st["quote_min"] += minutes
        if q1 > 0 and q2 > 0:
            st["two_min"] += minutes
        st["qmin_min"] += _qmin(q1, q2, mid) * minutes
        rem_b = N_SHARES if use_bid else 0.0
        rem_a = N_SHARES if use_ask else 0.0
        i, j = bisect_right(tkeys, t), bisect_right(tkeys, t2)
        for _tt, ypx, buys_yes, size in trades[i:j]:
            if not buys_yes and rem_b > 0:
                hit = ypx < bid - _EPS if rule == "temkinli" else ypx <= bid + _EPS
                if hit:
                    q = min(rem_b, size, MAX_POS - pos)
                    if q > 0:
                        pos += q
                        cash -= q * bid
                        rem_b -= q
                        st["fills"] += 1
                        st["shares"] += q
                        st["pre_pnl" if t < mk["d0"] else "in_pnl"] += q * (mk["y"] - bid)
            elif buys_yes and rem_a > 0:
                hit = ypx > ask + _EPS if rule == "temkinli" else ypx >= ask - _EPS
                if hit:
                    q = min(rem_a, size, MAX_POS + pos)
                    if q > 0:
                        pos -= q
                        cash += q * ask
                        rem_a -= q
                        st["fills"] += 1
                        st["shares"] += q
                        st["pre_pnl" if t < mk["d0"] else "in_pnl"] += q * (ask - mk["y"])
    st["pnl"] = cash + pos * mk["y"]
    return st


def _boot_ci(cluster_vals: list[float], n_boot: int = 4000, seed: int = 7) -> tuple[float, float]:
    if len(cluster_vals) < 3:
        return float("nan"), float("nan")
    rnd = random.Random(seed)
    k = len(cluster_vals)
    sums = sorted(sum(rnd.choice(cluster_vals) for _ in range(k)) for _ in range(n_boot))
    return sums[int(0.025 * n_boot)], sums[int(0.975 * n_boot)]


def _scan_reward(scan_path: Path | None):
    """Bugunku taramadan hava 'highest temperature' marketlerinde $100 kotasyonun tahmini odulu ($/gun)."""
    if not scan_path or not scan_path.exists():
        return None
    import csv

    vals, empty = [], 0
    for r in csv.DictReader(open(scan_path, encoding="utf-8")):
        if r.get("category") != "weather" or "highest temperature" not in r.get("question", "").lower():
            continue
        if r.get("est_day_half") in (None, ""):
            continue
        if r.get("empty_window") == "1":
            empty += 1
            continue
        vals.append(float(r["est_day_half"]))
    if not vals:
        return None
    vals.sort()
    return dict(
        n=len(vals),
        empty=empty,
        mean=statistics.fmean(vals),
        median=statistics.median(vals),
        p25=vals[len(vals) // 4],
        p75=vals[3 * len(vals) // 4],
    )


def main() -> None:  # noqa: C901
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default="2026-11-12")
    ap.add_argument("--end", default="2026-11-24")
    ap.add_argument("--calib-start", default="2026-10-15")
    ap.add_argument("--cache", default=str(Path(get_data_dir()) / "research" / "mm_trades_cache"))
    ap.add_argument("--scan", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    cache = Path(args.cache)

    te = sqlite3.connect(f"file:{Path(get_data_dir()) / 'tempmarket-edge.sqlite'}?mode=ro", uri=True, timeout=120)
    print("sigma kalibrasyonu:", args.calib_start, "->", args.start)
    sig = _calibrate_sigma(te, args.calib_start, args.start)
    st_alias = {icao.upper(): sid for sid, icao in te.execute("SELECT id, icao FROM stations")}
    preds: dict[tuple, list] = defaultdict(list)
    for s, d, run, h, fp in te.execute(
        "SELECT station_id, target_date, run_at_utc, horizon_days, final_prediction FROM prediction_log "
        "WHERE target_date >= ? AND target_date < ? AND final_prediction IS NOT NULL",
        (args.start, args.end),
    ):
        preds[(str(s), str(d))].append((_ts(run), float(fp), int(h)))
    te.close()
    for runs in preds.values():
        runs.sort()

    markets, metar = _load_markets(args.start, args.end, cache)
    print(f"resmi sonuclu + islem verili market: {len(markets)}")
    ob = sqlite3.connect(f"file:{Path(get_data_dir()) / 'orderbook.db'}?mode=ro", uri=True, timeout=120)
    agg: dict[tuple[str, str], defaultdict[str, float]] = {
        (v[0], r): defaultdict(float) for v in VARIANTS for r in RULES
    }
    clusters: dict[tuple[str, str], defaultdict[tuple[str, str], float]] = {
        (v[0], r): defaultdict(float) for v in VARIANTS for r in RULES
    }
    band_pnl: dict[tuple[str, str], defaultdict[str, list[float]]] = {
        (v[0], r): defaultdict(lambda: [0.0, 0.0]) for v in VARIANTS for r in RULES
    }
    fv_cover = [0, 0]
    for n_mk, mk in enumerate(markets):
        seq = [
            (_ts(t), float(b), float(a))
            for t, b, a in ob.execute(
                "SELECT snapshot_time, best_bid, best_ask FROM orderbook_snapshots WHERE market_id=? "
                "AND best_bid IS NOT NULL AND best_ask IS NOT NULL ORDER BY snapshot_time",
                (mk["mid"],),
            )
            if 0 < float(b) < float(a) < 1
        ]
        if len(seq) < 10:
            continue
        trades = []
        for t, side, outc, price, size in json.loads(mk["path"].read_text(encoding="utf-8")):
            if t is None or price is None or size is None or side not in ("BUY", "SELL") or outc not in ("Yes", "No"):
                continue
            ypx = float(price) if outc == "Yes" else 1.0 - float(price)
            trades.append((float(t), ypx, (side == "BUY") == (outc == "Yes"), float(size)))
        trades.sort()
        tkeys = [x[0] for x in trades]
        station = st_alias.get(_STATION_ALIAS.get(mk["code"], mk["code"]))
        plist = preds.get((station, mk["day"]), []) if station else []
        pkeys = [p[0] for p in plist]
        edges = _edges_c(mk["q"], mk["mt"])
        obs = metar.get((mk["code"], mk["day"]), [])
        fv_cache: dict[float, float | None] = {}

        def fv_at(t: float, plist=plist, pkeys=pkeys, edges=edges, obs=obs, mk=mk, fv_cache=fv_cache):
            if t in fv_cache:
                return fv_cache[t]
            out = None
            i = bisect_right(pkeys, t)
            if i and edges and sig:
                _run, mu, h = plist[i - 1]
                sd, bias = sig.get(min(max(h, 0), max(sig)), max(sig.values()))
                m = _running_max(obs, t) if t >= mk["d0"] else None
                out = _bucket_prob(edges, mu - bias, sd, m)
            fv_cache[t] = out
            return out

        for t, _b, _a in seq:
            fv_cover[1] += 1
            fv_cover[0] += fv_at(t) is not None
        ckey = (mk["code"], mk["day"])
        first_mid = (seq[0][1] + seq[0][2]) / 2.0
        band = "<0.10" if first_mid < 0.10 else ("0.10-0.50" if first_mid < 0.50 else (">=0.50"))
        for v in VARIANTS:
            for r in RULES:
                s = _simulate(seq, trades, tkeys, mk, fv_at, v, r)
                if not s["active"]:
                    continue
                a = agg[(v[0], r)]
                a["markets"] += 1
                for k in ("fills", "shares", "quote_min", "two_min", "qmin_min", "pnl", "pre_pnl", "in_pnl"):
                    a[k] += s[k]
                clusters[(v[0], r)][ckey] += s["pnl"]
                bp = band_pnl[(v[0], r)][band]
                bp[0] += s["pnl"]
                bp[1] += s["quote_min"]
        if n_mk % 1000 == 0:
            print(f"  {n_mk}/{len(markets)}", flush=True)
    ob.close()

    rew = _scan_reward(Path(args.scan)) if args.scan else None
    today = dt.datetime.now(dt.timezone.utc).date()
    out = Path(args.out) if args.out else REPO / "reports" / f"poly_mm_reward_backtest_{today:%Y%m%d}.md"
    L = [
        f"# Hava marketlerinde kotasyon: dolum + ters secim kaybi ({args.start} .. {args.end})",
        "",
        f"Market: {len(markets):,} (resmi sonuclu, gercek taker islem verili). Kotasyon {N_SHARES:.0f} pay/taraf "
        f"(~$100 iki tarafli), net pozisyon siniri {MAX_POS:.0f} pay, tick {TICK}. Odul penceresi v={V_CENTS} sent, "
        f"min size {MIN_SIZE:.0f} (bugunku hava ayari). FV kapsamasi: %{100 * fv_cover[0] / max(fv_cover[1], 1):.1f} "
        "snapshot.",
        "",
        "Dolum PnL'i sonuca kadar tutma ile hesaplandi (ucret yok: maker ucreti 0). Odul geliri DAHIL DEGIL.",
        "",
        "FV kalibrasyonu (" + args.calib_start + " .. " + args.start + ", forecast runner final_prediction - "
        "gerceklesen): "
        + "; ".join(f"ufuk {h}: sapma {b:+.2f} C, sd {s:.2f} C" for h, (s, b) in sorted(sig.items()))
        + ". FV = sapma duzeltilmis N(mu, sd) kova olasiligi.",
        "",
        "| varyant | dolum kurali | market | dolum | pay | PnL | gun-oncesi | gun-ici | kotasyon-gunu | "
        "PnL / kotasyon-gunu | %95 GA (sehir-gun) | iki tarafli pencere saat/market |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|",
    ]
    for v in VARIANTS:
        for r in RULES:
            a = agg[(v[0], r)]
            if not a["markets"]:
                continue
            qd = a["quote_min"] / 1440.0
            lo, hi = _boot_ci(list(clusters[(v[0], r)].values()))
            L.append(
                f"| {v[0]} | {r} | {a['markets']:,.0f} | {a['fills']:,.0f} | {a['shares']:,.0f} | ${a['pnl']:+,.0f} | "
                f"${a['pre_pnl']:+,.0f} | ${a['in_pnl']:+,.0f} | {qd:,.0f} | ${a['pnl'] / max(qd, 1e-9):+.2f} | "
                f"${lo:+,.0f} .. ${hi:+,.0f} | {a['two_min'] / 60.0 / a['markets']:.1f} |"
            )
    L += [
        "",
        "## Ilk mid bandina gore PnL / kotasyon-gunu",
        "",
        "| varyant | kural | <0.10 | 0.10-0.50 | >=0.50 |",
        "|---|---|---:|---:|---:|",
    ]
    for v in VARIANTS:
        for r in RULES:
            bands = band_pnl[(v[0], r)]
            cells = []
            for b in ("<0.10", "0.10-0.50", ">=0.50"):
                p, qm = bands.get(b, [0.0, 0.0])
                cells.append(f"${p / (qm / 1440.0):+.2f}" if qm else "-")
            L.append(f"| {v[0]} | {r} | " + " | ".join(cells) + " |")
    if rew:
        L += [
            "",
            "## Odul ile karsilastirma (bugunku tarama, $100 kotasyon, d=v/2)",
            "",
            f"Bugunku taramada rakipli {rew['n']} 'highest temperature' marketinde $100 kotasyonun tahmini odulu: "
            f"medyan ${rew['median']:.2f}/gun, ortalama ${rew['mean']:.2f}/gun (p25 ${rew['p25']:.2f}, "
            f"p75 ${rew['p75']:.2f}); bos pencereli {rew['empty']} market haric. Tek anlik defterden; gun ici "
            "rekabet degisimi kayit betigiyle olculecek.",
            "",
            "Net = odul - dolum kaybi (kotasyon-gunu basina, $100): yukaridaki PnL / kotasyon-gunu sutunu ile "
            "bu odul medyani toplanir.",
        ]
    L += [
        "",
        "## Notlar",
        "",
        "- Kotasyon yalnizca snapshot anlarinda (~11.5 dk) yenilenir; gercek bir MM botu daha sik yeniler. "
        "Bayat kotasyon ters secimi buyutur -> sonuc temkinli tarafta.",
        "- Dolum yalnizca TAKER islemlerinden; kuyruk sirasi bilinmez (iki kural ust/alt sinir verir).",
        "- FV-SKEW: merkez = mid + kirp(FV - mid, +-(v - d - 0.5 sent)); kotasyon odul penceresinde kalir. "
        "FV-TEK-TARAF: FV mid'den d kadar uzaksa yalnizca FV yonundeki taraf (tek tarafli 1/3 puan).",
        "- Odul parametreleri ve oranlari gecmis gunler icin bilinmiyor; bugunku degerlerle karsilastirilir.",
    ]
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"yazildi: {out}")


if __name__ == "__main__":
    main()
