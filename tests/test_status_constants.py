"""Tests for status constants (OPEN_BET_STATUSES) and text encoding integrity."""

# ── OPEN_BET_STATUSES ──────────────────────────────────────────────────────


def test_open_bet_statuses_defined():
    """OPEN_BET_STATUSES is a tuple with the expected values."""
    from database.models import OPEN_BET_STATUSES

    assert isinstance(OPEN_BET_STATUSES, tuple)
    assert "active" in OPEN_BET_STATUSES
    assert "open" in OPEN_BET_STATUSES
    assert "placed" in OPEN_BET_STATUSES
    assert "pending" in OPEN_BET_STATUSES


def test_no_literal_open_status_tuples():
    """
    All source files must use OPEN_BET_STATUSES constant instead
    of hardcoding the literal tuple ("active", "open", "placed", "pending").
    """
    import os

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    violations = []

    # Skip the definition file (database/models.py) and this test file itself
    # Use os.path.normpath for cross-platform path comparison
    skip_files = {
        os.path.normpath("database/models.py"),
        os.path.normpath("tests/test_status_constants.py"),
    }

    for root, _dirs, files in os.walk(project_root):
        # Skip venv and __pycache__
        if "venv" in root or "__pycache__" in root or ".git" in root:
            continue
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, project_root)
            if rel in skip_files:
                continue
            try:
                with open(fpath, encoding="utf-8") as fh:
                    content = fh.read()
            except Exception:
                continue

            # Check for the string literal
            if ('"active", "open", "placed", "pending"') in content:
                violations.append(rel)

    assert not violations, (
        f"Literal open-status tuples found in: {violations}. Replace with OPEN_BET_STATUSES from database.models."
    )


# ── No mojibake ────────────────────────────────────────────────────────────


def test_no_mojibake_in_python_files():
    """No Python file contains broken UTF-8 byte sequences (mojibake)."""
    import os

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bad_sequences = [
        b"\xe2\x80\x99",  # 3-byte right single quotation mark (sometimes double-encoded)
    ]

    for root, _dirs, files in os.walk(project_root):
        if "venv" in root or "__pycache__" in root or ".git" in root:
            continue
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "rb") as fh:
                    raw = fh.read()
            except Exception:
                continue
            for seq in bad_sequences:
                if seq in raw:
                    rel = os.path.relpath(fpath, project_root)
                    assert False, (
                        f"Mojibake detected in {rel}: "
                        f"found byte sequence {seq.hex()} "
                        f"(often double-encoded Unicode). "
                        "Replace with proper UTF-8 characters."
                    )
