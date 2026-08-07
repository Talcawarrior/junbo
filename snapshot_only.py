"""Tek seferlik snapshot alma script'i — Task Scheduler'dan (JunboSnapshot) cagrilir.

Amac: Bot process'i calismasa bile (makine uyandiginda / bot coktugunde) piyasa
snapshot'larinin alinmasini garanti etmek. JunboSnapshot task'i her 30 dakikada
bir (WakeToRun=true ile uykudan uyandirarak) bu script'i calistirir.

Kullanim:
    python snapshot_only.py

Not: 30dk bucket dedup (jobs/snapshot_job.py::_same_bucket) zaten var — ayni
30dk penceresinde bot icindeki snapshot_loop ile cakisma olsa bile ikinci kayit
yazilmaz, guvenli calisir.

Cikis kodu: 0 basari, 1 hata (Task Scheduler loguna yansir).
"""

import sys
from pathlib import Path

# Repo kokunu path'e ekle (task calisma dizini repo kokudur ama garanti olsun).
REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from jobs.snapshot_job import take_market_snapshots, cleanup_old_snapshots  # noqa: E402


def main() -> int:
    try:
        saved = take_market_snapshots()
        # Gunluk temizlik de buradan yapilabilir (bot acikken bot_loop yapar,
        # burada yalniz bot kapaliyken devreye girmis olur — idempotent).
        cleanup_old_snapshots(days=365)  # backtest verisi 1 yil korunur
        print(f"snapshot_only: {saved} snapshots saved")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"snapshot_only ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
