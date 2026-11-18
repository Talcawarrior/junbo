"""Replay backtest motoru (CLI).

Kullanim::

    python -m replay.main --strategy both --stake-mode both `
      --window 2026-08-04..2026-08-22 --tick-minutes 5 --out-dir reports/replay/final

Kurallar (CLAUDE.md §7):

* Botun karar KURALLARI arsivlenmis gercek veri uzerinde calisir; sahte veri
  uretilmez, `scripts/backtest.py` gibi yeniden yazim modeli KULLANILMAZ.
* **Look-ahead YASAK** - T aninda yalnizca T'den once var olan veri gorunur.
* Production DB'lere HICBIR yazi gitmez: arsivler `mode=ro`, bot yalnizca
  gecici DB'ye yazar (`replay.seeding.temp_db`).

Ciktilar (`<out-dir>/<mode>/`):

* ``rapor.md``            - gun-gun ledger (acilan/kazanan/kaybeden/PnL/nakit)
* ``ledger_bets.csv``     - bet-bet ayrinti
* ``mechanism_trace.csv`` - mekanizma izi (acilis nedeni, bias, peak kilidi)
* ``activity_events.csv`` - botun `log_event` karsiligi
* ``fidelity.md``         - `--fidelity` verilirse replay-vs-canli karsilastirma
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

from replay.archive import Archive
from replay.clock import ReplayClock
from replay.price_index import PriceIndex
from replay.scheduler import ReplayScheduler, RunResult
from replay.seeding import temp_db

#: Fixed stake modunda kullanilan canli degerler.
SPREAD_FIXED_STAKE = 2.0
METAR_FIXED_STAKE = 3.0

#: Compound modunda bankroll'un bu kadari stake edilir.
COMPOUND_RATE = 0.03

STRATEGIES = ("spread", "metar", "metar_peak", "both")
STAKE_MODES = ("fixed", "compound", "both")


# ---------------------------------------------------------------------------
# CLI yardimcilari
# ---------------------------------------------------------------------------
def _parse_window(text: str) -> tuple[datetime, datetime]:
    """'YYYY-MM-DD..YYYY-MM-DD' -> (baslangic, bitis+1 gun).

    Ust sinir HARIC tutulur; boylece `for day in range(start, end)` gibi
    kapsayici pencere elde edilir.
    """
    if not text or ".." not in text:
        raise ValueError(f"pencere formati 'YYYY-MM-DD..YYYY-MM-DD' olmali: {text!r}")
    left, right = text.split("..", 1)
    start = datetime.strptime(left.strip(), "%Y-%m-%d")
    last = datetime.strptime(right.strip(), "%Y-%m-%d")
    if last < start:
        raise ValueError(f"pencere ters: {text!r}")
    return start, last + timedelta(days=1)


def _apply_stake_mode(mode: str, bankroll: float) -> float:
    """Stake modunu canli config'e uygula; secilen stake degerini dondur.

    * ``fixed``    - canli degerler (spread $2, METAR $3).
    * ``compound`` - bankroll * 0.03.

    NOT: yalnizca `spread_stake_usd` ve `jobs.metar_peak.METAR_STAKE`
    degistirilir. `metar_stake_usd` BILEREK dokunulmaz; caller bunlari geri
    almakla yukumludur (bkz. `_snapshot_stakes` / `_restore_stakes`).
    """
    from config.settings import bot_config
    import jobs.metar_peak as mp

    if mode == "fixed":
        stake = SPREAD_FIXED_STAKE
        mp.METAR_STAKE = METAR_FIXED_STAKE
    elif mode == "compound":
        stake = float(bankroll) * COMPOUND_RATE
        mp.METAR_STAKE = stake
    else:
        raise ValueError(f"bilinmeyen stake modu: {mode!r}")
    bot_config.strategy.spread_stake_usd = stake
    return stake


def _snapshot_stakes() -> tuple[float, float]:
    from config.settings import bot_config
    import jobs.metar_peak as mp

    return float(bot_config.strategy.spread_stake_usd), float(mp.METAR_STAKE)


def _restore_stakes(snapshot: tuple[float, float]) -> None:
    from config.settings import bot_config
    import jobs.metar_peak as mp

    bot_config.strategy.spread_stake_usd, mp.METAR_STAKE = snapshot


# ---------------------------------------------------------------------------
# Rapor yazicilari
# ---------------------------------------------------------------------------
LEDGER_HEADER = [
    "day",
    "opened_at_utc",
    "strategy",
    "market_id",
    "city",
    "city_code",
    "side",
    "stake_usd",
    "entry_price",
    "shares",
    "threshold_c",
    "status",
    "selection_mechanism",
    "data_source",
    "open_trigger",
    "peak_bucket",
    "winner_bucket",
    "close_kind",
    "closed_at_utc",
    "close_price",
    "settle_outcome",
    "realized_pnl",
    "entry_fee",
]

TRACE_HEADER = ["iso", "day", "kind", "city_code", "trigger", "reason"]
EVENT_HEADER = ["ts", "iso", "type", "city", "detail"]


def _fmt_money(value: float) -> str:
    return f"${value:+.2f}" if value < 0 else f"$+{value:.2f}" if value > 0 else "$+0.00"


def _write_rapor(path: str, start: datetime, end: datetime, result: RunResult, bankroll: float) -> None:
    days = []
    day = start
    while day < end:
        days.append(day.strftime("%Y-%m-%d"))
        day += timedelta(days=1)
    by_day: dict[str, list] = {d: [] for d in days}
    for bet in result.bets:
        by_day.setdefault(bet.day, []).append(bet)

    cash = float(bankroll)
    lines = [
        "# Junbo Replay Backtest Raporu",
        "",
        f"Pencere: **{start.strftime('%Y-%m-%d')} -> {end.strftime('%Y-%m-%d')}** (UTC)",
        "",
        "## Gunluk Ozet",
        "",
        "| Gun | Acilan | Kazanan | Kaybeden | PnL | Nakit | Toplam Deger |",
        "|-----|--------|---------|----------|-----|------|--------------|",
    ]
    for d in days:
        bets = by_day.get(d, [])
        won = sum(1 for b in bets if b.won)
        lost = len(bets) - won
        pnl = sum(b.realized_pnl for b in bets)
        cash += pnl
        lines.append(f"| {d} | {len(bets)} | {won} | {lost} | {_fmt_money(pnl)} | ${cash:.2f} | ${cash:.2f} |")

    totals = sum(b.realized_pnl for b in result.bets)
    lines += [
        "",
        "## Toplam",
        "",
        f"- Bet: **{len(result.bets)}**",
        f"- Net PnL: **{_fmt_money(totals)}**",
        f"- Baslangic bankroll: ${bankroll:.2f}",
        f"- Bitis nakit: ${bankroll + totals:.2f}",
        "",
        "## Bet Detaylari (ilk 50)",
        "",
    ]
    for b in result.bets[:50]:
        lines.append(
            f"- **{b.strategy}** {b.opened_at.strftime('%Y-%m-%d %H:%M:%S')} | {b.city} "
            f"{b.threshold_c:.1f}C | entry={b.entry_price:.3f} stake=${b.stake_usd:.2f} "
            f"| close={b.close_kind} outcome={'YES' if b.won else 'NO'} pnl={b.realized_pnl:+.4f}"
        )
    lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


def _write_ledger(path: str, result: RunResult) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(LEDGER_HEADER)
        for b in result.bets:
            writer.writerow(
                [
                    b.day,
                    b.opened_at.strftime("%Y-%m-%d %H:%M:%S"),
                    b.strategy,
                    b.market_id,
                    b.city,
                    b.city_code,
                    "YES",
                    f"{b.stake_usd:.2f}",
                    b.entry_price,
                    b.shares,
                    b.threshold_c,
                    "closed",
                    "",
                    b.price_source,
                    b.open_trigger,
                    b.bucket if b.strategy == "metar_peak" else "",
                    b.bucket,
                    b.close_kind,
                    b.closed_at.strftime("%Y-%m-%d %H:%M:%S") if b.closed_at else "",
                    "",
                    "YES" if b.won else "NO",
                    b.realized_pnl,
                    b.entry_fee,
                ]
            )


def _write_trace(path: str, result: RunResult) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(TRACE_HEADER)
        for row in result.trace:
            writer.writerow([row.get(k, "") for k in TRACE_HEADER])


def _write_events(path: str, result: RunResult) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(EVENT_HEADER)
        for row in sorted(result.events, key=lambda r: r["ts"]):
            writer.writerow([row.get(k, "") for k in EVENT_HEADER])


def _write_fidelity(path: str, result: RunResult) -> None:
    """Replay betlerini canli `bot.db.bets` ile karsilastir."""
    from replay.fidelity import compare_replay_vs_live

    replay_rows = [
        {
            "market_id": b.market_id,
            "placed_at": b.opened_at,
            "entry": b.entry_price,
            "order_id": f"{b.strategy}_replay",
        }
        for b in result.bets
    ]
    live_rows: list[dict] = []
    try:
        import sqlite3

        from replay.archive import BOT_DB

        con = sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True, timeout=60)
        try:
            for mid, placed, entry, order_id in con.execute(
                "SELECT market_id, placed_at, entry_price, order_id FROM bets "
                "WHERE placed_at IS NOT NULL AND entry_price IS NOT NULL"
            ):
                live_rows.append({"market_id": str(mid), "placed_at": placed, "entry": entry, "order_id": order_id})
        finally:
            con.close()
    except Exception:
        live_rows = []

    cmp = compare_replay_vs_live(replay_rows, live_rows, window_days=3)
    lines = [
        "# Replay vs Canli Fidelity",
        "",
        f"- Eslesen: **{cmp['matched']}**",
        f"- Precision (replay): **{cmp['precision']:.3f}**",
        f"- Recall (canli): **{cmp['recall']:.3f}**",
        "",
        "Eslesme kurali: market_id esit + `|placed_at| <= 15 dk` + `|entry| <= 0.05`.",
        "",
        "| Strateji | Eslesen | Replay | Canli |",
        "|----------|---------|--------|-------|",
    ]
    for name, stats in cmp["strategy"].items():
        lines.append(f"| {name} | {stats['matched']} | {stats['replay']} | {stats['live']} |")
    lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


# ---------------------------------------------------------------------------
# Ana giris
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="replay.main", description="Junbo tipa-tipa replay backtest")
    p.add_argument("--window", required=True, help="YYYY-MM-DD..YYYY-MM-DD (kapsayici)")
    p.add_argument("--strategy", default="both", choices=STRATEGIES)
    p.add_argument("--stake-mode", default="fixed", choices=STAKE_MODES)
    p.add_argument("--tick-minutes", type=int, default=5)
    p.add_argument("--out-dir", default=os.path.join("reports", "replay", "run"))
    p.add_argument("--bankroll", type=float, default=1000.0)
    p.add_argument(
        "--spread-max-cities",
        type=int,
        default=None,
        help="config drift override (canli botun o donem kullandigi deger)",
    )
    p.add_argument("--fidelity", action="store_true", help="canli betlerle karsilastir")
    p.add_argument(
        "--allow-price-after", action="store_true", help="fiyat yoksa SONRAKI ilk fiyati kullan (look-ahead riski)"
    )
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    start, end = _parse_window(args.window)
    start_day = start.strftime("%Y-%m-%d")
    end_day = (end - timedelta(days=1)).strftime("%Y-%m-%d")

    modes = ["fixed", "compound"] if args.stake_mode == "both" else [args.stake_mode]
    snapshot = _snapshot_stakes()
    tmp_dir = tempfile.mkdtemp(prefix="junbo_replay_")
    archive = Archive(start_day, end_day)
    try:
        prices = PriceIndex().load_from_archive(archive)
        clock = ReplayClock(start, end, tick_minutes=args.tick_minutes)
        with temp_db(os.path.join(tmp_dir, "replay.db")):
            for mode in modes:
                _apply_stake_mode(mode, args.bankroll)
                scheduler = ReplayScheduler(
                    archive,
                    prices,
                    clock,
                    strategy=args.strategy,
                    max_cities=args.spread_max_cities,
                    allow_price_after=args.allow_price_after,
                )
                result = scheduler.run()
                _write_outputs(args.out_dir, mode, start, end, result, args)
    finally:
        archive.close()
        _restore_stakes(snapshot)
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return 0


def _write_outputs(out_dir: str, mode: str, start: datetime, end: datetime, result: RunResult, args) -> None:
    mode_dir = os.path.join(out_dir, mode)
    os.makedirs(mode_dir, exist_ok=True)
    _write_rapor(os.path.join(mode_dir, "rapor.md"), start, end, result, args.bankroll)
    _write_ledger(os.path.join(mode_dir, "ledger_bets.csv"), result)
    _write_trace(os.path.join(mode_dir, "mechanism_trace.csv"), result)
    _write_events(os.path.join(mode_dir, "activity_events.csv"), result)
    if args.fidelity:
        _write_fidelity(os.path.join(mode_dir, "fidelity.md"), result)


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
