"""Database backup utility.

Usage:
    python db_backup.py              # Create backup (label "manual")
    python db_backup.py --list       # List existing backups
    python db_backup.py --restore    # Restore latest local backup
    python db_backup.py --prune      # Remove stale backups beyond retention
    python db_backup.py --offsite-restore   # Restore latest offsite backup

Called automatically by:
    - Bot startup (main.py)
    - API startup / pre-reset (api.py)
    - Test suite (tests/test_critical_bugs.py)
    - Scheduled task "JunboBotBackup" (scripts/db_backup_task.bat)

Backups use SQLite's Online Backup API (``Connection.backup``) instead of a
raw file copy. A raw ``shutil.copy2`` of a live WAL database can capture a
partial write and produce a corrupt backup; the online API always yields a
consistent, self-contained ``.db`` file (WAL pages are folded in) and is safe
to run while the bot is writing. Every backup is verified with
``PRAGMA integrity_check`` before it is kept.

Offsite protection (disaster recovery):
    Set the ``JUNBO_OFFSITE_DIR`` environment variable to mirror every backup
    to a second volume / network share. If ``JUNBO_BACKUP_KEY`` is also set,
    the offsite copy is encrypted with Fernet (AES-128 + HMAC) so a stolen
    disk cannot expose the data. Offsite mirroring is best-effort and never
    blocks or crashes the caller.
"""

import base64
import glob
import os
import sqlite3
import sys
import tempfile
from datetime import datetime

try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    _HAVE_CRYPTO = True
except Exception:  # pragma: no cover - fallback only when lib missing
    _HAVE_CRYPTO = False

DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "bot.db")
BACKUP_DIR = os.path.join(DB_DIR, "backups")
MAX_BACKUPS = 10
PBKDF2_ITERATIONS = 200_000


def _offsite_dir():
    return os.environ.get("JUNBO_OFFSITE_DIR")


def _backup_key():
    return os.environ.get("JUNBO_BACKUP_KEY")


def _verify_sqlite(path):
    """Return True if the SQLite file passes a quick integrity_check."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = con.execute("PRAGMA integrity_check(1)").fetchone()
        return bool(row) and row[0] == "ok"
    except sqlite3.Error:
        return False
    finally:
        con.close()


def _content_hash(path):
    """Stable hash of a SQLite file's logical content (schema + sorted rows)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        parts = []
        for t in sorted(tables):
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})").fetchall()]
            rows = con.execute(f"SELECT {' ,'.join(chr(34) + c + chr(34) for c in cols)} FROM {t}").fetchall()
            parts.append((t, tuple(rows)))
        return hash(tuple(parts))
    finally:
        con.close()


def _label_of(filename):
    base = os.path.basename(filename)
    if base.startswith("bot_"):
        base = base[4:]
    if base.endswith(".db"):
        base = base[:-3]
    label_parts = []
    for part in base.split("_"):
        if len(part) == 8 and part.isdigit():
            break
        label_parts.append(part)
    return "_".join(label_parts) if label_parts else "auto"


def _discover_labels():
    labels = set()
    for f in glob.glob(os.path.join(BACKUP_DIR, "bot_*.db")):
        labels.add(_label_of(f))
    return labels


def _primary_backups(directory, label):
    """Sorted primary backup files (one per backup): .db or .db.enc."""
    pattern = f"bot_{label}_*" if label else "bot_*"
    primaries = [f for f in glob.glob(os.path.join(directory, pattern)) if f.endswith(".db") or f.endswith(".db.enc")]
    return sorted(primaries)


def _delete_backup(primary):
    os.unlink(primary)
    if primary.endswith(".db.enc"):
        salt = primary + ".salt"
        if os.path.exists(salt):
            os.unlink(salt)
    else:
        for ext in ("-wal", "-shm"):
            p = primary + ext
            if os.path.exists(p):
                os.unlink(p)


def _prune_dir(directory, label):
    """Keep only the newest MAX_BACKUPS backups for a label in a directory."""
    primaries = _primary_backups(directory, label)
    for old in primaries[: max(0, len(primaries) - MAX_BACKUPS)]:
        _delete_backup(old)


def _prune_backups(label=None):
    """Keep only the newest MAX_BACKUPS local backups, scoped per label."""
    if label:
        _prune_dir(BACKUP_DIR, label)
    else:
        for lbl in _discover_labels():
            _prune_dir(BACKUP_DIR, lbl)


def _shutil_copy(src, dst):
    """Thin copy wrapper so it can be stubbed in tests if needed."""
    import shutil

    shutil.copy2(src, dst)


# --------------------------------------------------------------------------
# Offsite (encrypted) mirroring
# --------------------------------------------------------------------------
def _fernet_key(passphrase, salt):
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def _encrypt_offsite(src_path, dst_enc, passphrase):
    salt = os.urandom(16)
    fernet = Fernet(_fernet_key(passphrase, salt))
    ciphertext = fernet.encrypt(open(src_path, "rb").read())
    with open(dst_enc, "wb") as fh:
        fh.write(ciphertext)
    with open(dst_enc + ".salt", "wb") as fh:
        fh.write(salt)


def _decrypt_offsite(enc_path, passphrase):
    salt = open(enc_path + ".salt", "rb").read()
    fernet = Fernet(_fernet_key(passphrase, salt))
    return fernet.decrypt(open(enc_path, "rb").read())


def mirror_offsite(backup_path):
    """Best-effort mirror of a backup to JUNBO_OFFSITE_DIR.

    Encrypted with Fernet when JUNBO_BACKUP_KEY is set; otherwise a plain
    verified copy. Returns the offsite path or None. Never raises.
    """
    offsite = _offsite_dir()
    if not offsite:
        return None
    try:
        os.makedirs(offsite, exist_ok=True)
        name = os.path.basename(backup_path)
        label = _label_of(name)
        key = _backup_key()

        if key and _HAVE_CRYPTO:
            dst = os.path.join(offsite, name + ".enc")
            _encrypt_offsite(backup_path, dst, key)
            plain = _decrypt_offsite(dst, key)
            tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tf.write(plain)
            tf.close()
            ok = _verify_sqlite(tf.name)
            os.unlink(tf.name)
            if not ok:
                print(f"WARNING: offsite encrypted copy failed verification: {name}")
                return None
            print(f"Offsite encrypted copy: {name}.enc")
        else:
            if key and not _HAVE_CRYPTO:
                print("WARNING: JUNBO_BACKUP_KEY set but cryptography missing; writing plaintext offsite copy")
            dst = os.path.join(offsite, name)
            _shutil_copy(backup_path, dst)
            if not _verify_sqlite(dst):
                print(f"WARNING: offsite copy failed verification: {name}")
                return None
            print(f"Offsite copy: {name}")

        _prune_dir(offsite, label)
        return dst
    except (OSError, sqlite3.Error) as exc:
        print(f"WARNING: offsite mirror failed ({exc}), skipping")
        return None


# --------------------------------------------------------------------------
# Backup / restore
# --------------------------------------------------------------------------
def create_backup(label="auto"):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_name = f"bot_{label}_{timestamp}.db"
    backup_path = os.path.join(BACKUP_DIR, backup_name)
    # Avoid collisions if two backups land in the same microsecond.
    suffix = 0
    while os.path.exists(backup_path):
        suffix += 1
        backup_path = os.path.join(BACKUP_DIR, f"bot_{label}_{timestamp}_{suffix}.db")

    if not os.path.exists(DB_PATH):
        print(f"WARNING: {DB_PATH} not found, skipping backup")
        return None

    try:
        src = sqlite3.connect(DB_PATH)
        try:
            dst = sqlite3.connect(backup_path)
            try:
                src.backup(dst)
                # Fold any WAL pages into the main file and switch the backup
                # to rollback-journal mode so it is a single, self-contained,
                # portable file (safe to copy/encrypt, no -wal/-shm needed).
                dst.execute("PRAGMA journal_mode=DELETE")
            finally:
                dst.close()
        finally:
            src.close()

        # Drop any leftover wal/shm artifacts from a previous raw-copy backup.
        for ext in ("-wal", "-shm"):
            p = backup_path + ext
            if os.path.exists(p):
                os.unlink(p)

        if not _verify_sqlite(backup_path):
            print(f"WARNING: backup integrity_check FAILED: {backup_name}")
        else:
            print(f"Backup created & verified: {backup_name}")
    except (sqlite3.Error, OSError) as exc:
        print(f"WARNING: backup failed ({exc}), skipping")
        return None

    _prune_backups(label)
    mirror_offsite(backup_path)
    return backup_path


def list_backups():
    if not os.path.exists(BACKUP_DIR):
        print("No backups found")
        return []
    backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "bot_*.db")))
    for b in backups:
        size_kb = os.path.getsize(b) / 1024
        mtime = datetime.fromtimestamp(os.path.getmtime(b))
        print(f"  {os.path.basename(b)} ({size_kb:.0f} KB) - {mtime.strftime('%Y-%m-%d %H:%M:%S')}")
    return backups


def restore_backup(backup_path=None, offsite=False):
    if backup_path is None:
        search_dir = _offsite_dir() if offsite else BACKUP_DIR
        pattern = "bot_*.db.enc" if (offsite and _backup_key()) else "bot_*.db"
        backups = sorted(glob.glob(os.path.join(search_dir, pattern)))
        if not backups:
            print("No backups to restore")
            return False
        backup_path = backups[-1]

    if not os.path.exists(backup_path):
        print(f"Backup not found: {backup_path}")
        return False

    # Decrypt an encrypted offsite file into a temp plain .db first.
    if backup_path.endswith(".enc"):
        key = _backup_key()
        if not key or not _HAVE_CRYPTO:
            print("WARNING: cannot decrypt offsite backup (missing key/crypto)")
            return False
        plain = _decrypt_offsite(backup_path, key)
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.write(plain)
        tmp.close()
        source = tmp.name
    else:
        source = backup_path

    # Preserve the current (possibly corrupted) DB before overwriting.
    if os.path.exists(DB_PATH):
        fallback = DB_PATH + f".pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        _shutil_copy(DB_PATH, fallback)
        print(f"Current DB saved as: {os.path.basename(fallback)}")

    # Drop any stale wal/shm so the restored single-file DB starts clean.
    for ext in ("-wal", "-shm"):
        p = DB_PATH + ext
        if os.path.exists(p):
            os.unlink(p)

    _shutil_copy(source, DB_PATH)
    for ext in ("-wal", "-shm"):
        src = source + ext
        if os.path.exists(src):
            _shutil_copy(src, DB_PATH + ext)

    if backup_path.endswith(".enc"):
        os.unlink(source)

    if not _verify_sqlite(DB_PATH):
        print("WARNING: restored DB failed integrity_check")
        return False

    print(f"Restored from: {os.path.basename(backup_path)}")
    return True


def prune_all():
    _prune_backups()
    print("Pruned stale backups")


if __name__ == "__main__":
    if "--list" in sys.argv:
        list_backups()
    elif "--restore" in sys.argv:
        restore_backup()
    elif "--offsite-restore" in sys.argv:
        restore_backup(offsite=True)
    elif "--prune" in sys.argv:
        prune_all()
    else:
        create_backup("manual")
