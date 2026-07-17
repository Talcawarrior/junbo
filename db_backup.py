"""Database backup utility.

Usage:
    python db_backup.py              # Create backup
    python db_backup.py --list       # List existing backups
    python db_backup.py --restore    # Restore latest backup

Called automatically by:
    - Bot startup (main.py)
    - Test suite (conftest.py)
"""

import os
import shutil
import sys
import glob
from datetime import datetime

DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "bot.db")
BACKUP_DIR = os.path.join(DB_DIR, "backups")
MAX_BACKUPS = 10


def create_backup(label="auto"):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"bot_{label}_{timestamp}.db"
    backup_path = os.path.join(BACKUP_DIR, backup_name)

    if not os.path.exists(DB_PATH):
        print(f"WARNING: {DB_PATH} not found, skipping backup")
        return None

    shutil.copy2(DB_PATH, backup_path)
    for ext in ["-wal", "-shm"]:
        src = DB_PATH + ext
        if os.path.exists(src):
            shutil.copy2(src, backup_path + ext)

    size_kb = os.path.getsize(backup_path) / 1024
    print(f"Backup created: {backup_name} ({size_kb:.0f} KB)")

    # Eski backup'ları temizle
    backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "bot_*.db")))
    for old in backups[:len(backups) - MAX_BACKUPS]:
        os.unlink(old)
        for ext in ["-wal", "-shm"]:
            p = old + ext
            if os.path.exists(p):
                os.unlink(p)

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


def restore_backup(backup_path=None):
    if backup_path is None:
        backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "bot_*.db")))
        if not backups:
            print("No backups to restore")
            return False
        backup_path = backups[-1]

    if not os.path.exists(backup_path):
        print(f"Backup not found: {backup_path}")
        return False

    # Mevcut DB'yi koru
    if os.path.exists(DB_PATH):
        fallback = DB_PATH + f".pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(DB_PATH, fallback)
        print(f"Current DB saved as: {os.path.basename(fallback)}")

    shutil.copy2(backup_path, DB_PATH)
    for ext in ["-wal", "-shm"]:
        src = backup_path + ext
        if os.path.exists(src):
            shutil.copy2(src, DB_PATH + ext)

    print(f"Restored from: {os.path.basename(backup_path)}")
    return True


if __name__ == "__main__":
    if "--list" in sys.argv:
        list_backups()
    elif "--restore" in sys.argv:
        restore_backup()
    else:
        create_backup("manual")
