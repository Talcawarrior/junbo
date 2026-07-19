"""Tests for the SQLite backup/restore pipeline (db_backup.py).

These exercise the real db_backup functions against throwaway databases,
so the production data/bot.db and data/backups are never touched.
"""

import os
import sqlite3

import pytest

import db_backup


def _seed(db_path, n=5, wal=True):
    if os.path.exists(db_path):
        os.unlink(db_path)
    con = sqlite3.connect(db_path)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE bets(id INTEGER PRIMARY KEY, market TEXT, stake REAL)")
    con.executemany(
        "INSERT INTO bets(market, stake) VALUES(?,?)",
        [(f"M{i}", float(i)) for i in range(n)],
    )
    con.commit()
    con.close()


def _rows(db_path):
    con = sqlite3.connect(db_path)
    try:
        return con.execute("SELECT id, market, stake FROM bets ORDER BY id").fetchall()
    finally:
        con.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "live.db"
    bdir = tmp_path / "backups"
    _seed(str(db))
    monkeypatch.setattr(db_backup, "DB_PATH", str(db))
    monkeypatch.setattr(db_backup, "BACKUP_DIR", str(bdir))
    return db_backup, str(db), str(bdir)


def test_backup_creates_verified_file(env):
    mod, db, bdir = env
    path = mod.create_backup("test")
    assert path is not None
    assert os.path.exists(path)
    assert mod._verify_sqlite(path) is True


def test_backup_consistent_under_concurrent_writes(env):
    mod, db, bdir = env
    writer = sqlite3.connect(db)
    writer.execute("INSERT INTO bets(market, stake) VALUES('writer', 9.0)")
    writer.commit()
    path = mod.create_backup("concurrent")
    writer.execute("INSERT INTO bets(market, stake) VALUES('writer2', 8.0)")
    writer.commit()
    writer.close()
    assert path is not None
    assert mod._verify_sqlite(path) is True
    assert any(r[1] == "writer" for r in _rows(path))


def test_restore_roundtrip(env):
    mod, db, bdir = env
    original = _rows(db)
    path = mod.create_backup("rt")
    con = sqlite3.connect(db)
    con.execute("DELETE FROM bets")
    con.commit()
    con.close()
    assert _rows(db) == []
    assert mod.restore_backup(path) is True
    assert _rows(db) == original


def test_checksum_content_stable(env):
    mod, db, bdir = env
    path = mod.create_backup("cs")
    h1 = mod._content_hash(path)
    assert mod.restore_backup(path) is True
    path2 = mod.create_backup("cs2")
    h2 = mod._content_hash(path2)
    assert h1 == h2


def test_prune_keeps_max_per_label(env):
    mod, db, bdir = env
    for _ in range(mod.MAX_BACKUPS + 5):
        mod.create_backup("prune")
    kept = sorted(f for f in os.listdir(bdir) if f.startswith("bot_prune_") and f.endswith(".db"))
    assert len(kept) == mod.MAX_BACKUPS


def test_backup_missing_db_returns_none(env):
    mod, db, bdir = env
    os.unlink(db)
    assert mod.create_backup("missing") is None


def test_offsite_mirror_plaintext(env, tmp_path, monkeypatch):
    mod, db, bdir = env
    off = tmp_path / "offsite"
    monkeypatch.setenv("JUNBO_OFFSITE_DIR", str(off))
    monkeypatch.delenv("JUNBO_BACKUP_KEY", raising=False)
    mod.create_backup("os")
    kept = [f for f in os.listdir(off) if f.startswith("bot_os_") and f.endswith(".db")]
    assert kept, "offsite copy missing"
    assert mod._verify_sqlite(os.path.join(off, kept[0])) is True


def test_offsite_mirror_encrypted_roundtrip(env, tmp_path, monkeypatch):
    mod, db, bdir = env
    off = tmp_path / "offsite"
    monkeypatch.setenv("JUNBO_OFFSITE_DIR", str(off))
    monkeypatch.setenv("JUNBO_BACKUP_KEY", "super-secret-passphrase")
    original = _rows(db)
    mod.create_backup("ose")
    enc = [f for f in os.listdir(off) if f.startswith("bot_ose_") and f.endswith(".enc")]
    assert len(enc) == 1
    assert os.path.exists(os.path.join(off, enc[0] + ".salt"))

    fresh = tmp_path / "restored.db"
    monkeypatch.setattr(mod, "DB_PATH", str(fresh))
    assert mod.restore_backup(offsite=True) is True
    assert _rows(str(fresh)) == original


def test_prune_all_keeps_max_per_label(env, tmp_path, monkeypatch):
    mod, db, bdir = env
    for lbl in ("a", "b"):
        for _ in range(mod.MAX_BACKUPS + 3):
            mod.create_backup(lbl)
    mod._prune_backups()
    for lbl in ("a", "b"):
        kept = sorted(f for f in os.listdir(bdir) if f.startswith(f"bot_{lbl}_") and f.endswith(".db"))
        assert len(kept) == mod.MAX_BACKUPS
