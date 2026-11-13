"""Single-source DB path regression tests.

The live bot DB lives outside the repo (DB_PATH in .env). Hardcoded
``data/bot.db`` reads silently return an empty/stale DB, so every runtime
and audit module must go through ``utils.db_paths``.
"""

from pathlib import Path

from utils.activity_log import _db_path as activity_log_db_path
from utils.db_paths import get_data_dir, get_db_path

REPO_ROOT = Path(__file__).resolve().parent.parent

HARDCODED_SNIPPETS = [
    'sqlite3.connect("data/bot.db")',
    'sqlite3.connect("file:data/bot.db',
    'sqlite3.connect("data/actuals.db")',
    'sqlite3.connect("data/orderbook.db")',
    'sqlite3.connect("data/backtest_prices.db")',
    'sqlite3.connect(r"data\\bot.db"',
    'os.path.join(_REPO_ROOT, "data", "bot.db")',
    'os.path.join(ROOT, "data", "bot.db")',
    'str(ROOT / "data" / "bot.db")',
    "str(ROOT / 'data' / 'bot.db')",
    'BOT_DB = r"C:\\Users',
    'DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bot.db")',
    '"data/actuals.db"',
]


def test_activity_log_uses_configured_db_path():
    assert Path(activity_log_db_path()) == Path(get_db_path())


def test_data_dir_is_db_parent():
    assert Path(get_data_dir()) == Path(get_db_path()).parent


def test_backup_uses_configured_db_path():
    import db_backup

    assert Path(db_backup._db_path()) == Path(get_db_path())
    assert Path(db_backup._db_dir()) == Path(get_data_dir())


def test_paths_follow_runtime_config_swap(monkeypatch):
    """The test harness swaps config.DB_PATH mid-suite; cached paths must not win."""
    monkeypatch.delenv("JUNBO_BOT_DB", raising=False)
    monkeypatch.delenv("JUNBO_DATA_DIR", raising=False)
    from config.settings import bot_config

    old_path = bot_config.db_path
    swapped = str(Path(get_db_path()).with_name("swap-test.db"))
    try:
        bot_config.db_path = swapped
        import db_backup

        assert Path(activity_log_db_path()) == Path(swapped)
        assert Path(db_backup._db_path()) == Path(swapped)
        assert Path(db_backup._db_dir()) == Path(swapped).parent
    finally:
        bot_config.db_path = old_path


def test_no_hardcoded_runtime_db_paths():
    files = list((REPO_ROOT / "scripts").glob("*.py"))
    files += list((REPO_ROOT / "jobs").glob("*.py"))
    files += [REPO_ROOT / "executor" / "settler.py", REPO_ROOT / "db_backup.py"]

    offenders = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for snippet in HARDCODED_SNIPPETS:
            if snippet in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {snippet}")

    assert not offenders, "Hardcoded DB paths found:\n" + "\n".join(offenders)
