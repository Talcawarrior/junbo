"""scripts/backup_databases.py -- gunluk calisma + sayi bazli saklama (2026-11-18).

Olay: gorev 6 saatte bir 4 DB'nin tamamini (~7 GB) kopyaliyor ve 90 gun
sakliyordu -> C: ~5 gunde dolacakti. Artik: UTC gununde en fazla 1 kosu
(--force haric) ve prefix basina en yeni N kopya. db_backup.py dosyalarina
(bot_startup_*, bot_scheduled_*, bot_pre_test_*) dokunulmaz.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backup_databases.py"


@pytest.fixture
def bdb(tmp_path, monkeypatch):
    """Load the script with JUNBO_DATA_DIR pointing at a temp dir."""
    monkeypatch.setenv("JUNBO_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("JUNBO_BACKUP_DIR", raising=False)
    monkeypatch.delenv("JUNBO_BOT_DB", raising=False)
    name = "backup_databases_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop(name, None)


def _touch(p: Path) -> Path:
    p.write_bytes(b"x")
    return p


def test_prune_keeps_newest_n_and_ignores_foreign_files(bdb):
    bdir = bdb.BACKUP_DIR
    own = [_touch(bdir / f"orderbook_202611{d:02d}_120000.db") for d in range(1, 11)]  # 10 copies
    sidecar = _touch(bdir / "orderbook_20261101_120000.db-shm")
    foreign = [
        _touch(bdir / "bot_startup_20261101_000000_000001.db"),
        _touch(bdir / "bot_scheduled_20261101_000000_000001.db"),
        _touch(bdir / "bot_pre_test_20261101_000000.db"),
    ]

    deleted = bdb._prune_by_count()

    keep = bdb.KEEP_LAST_BY_PREFIX["orderbook"]
    assert sorted(p.name for p in deleted) == sorted(p.name for p in own[: len(own) - keep])
    assert all(p.exists() for p in own[len(own) - keep :])
    assert not sidecar.exists(), "budanan yedegin -shm dosyasi da silinmeli"
    assert all(p.exists() for p in foreign), "db_backup.py dosyalarina dokunulmamali"


def test_main_runs_once_per_utc_day_unless_forced(bdb, monkeypatch):
    calls = []
    monkeypatch.setattr(bdb, "backup_once", lambda: calls.append(1) or {"failed": []})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    bdb.main([])
    assert calls == [1]
    assert bdb.RUN_MARKER.read_text(encoding="utf-8").strip() == today

    bdb.main([])  # ayni gun: atlanir
    assert calls == [1]

    bdb.main(["--force"])
    assert calls == [1, 1]


def test_marker_written_before_copy_so_failures_do_not_retry_storm(bdb, monkeypatch):
    def _boom():
        raise OSError("disk full")

    monkeypatch.setattr(bdb, "backup_once", _boom)
    with pytest.raises(OSError):
        bdb.main([])
    # Marker kopyadan once yazildi -> watchdog her tick'te 7 GB'i yeniden denemez.
    assert bdb.RUN_MARKER.exists()
