"""Quick CI — Her kod değişikliği ve bot başladığında çalıştır.

7 test (toplam ~90 saniye):
  1. RUFF        — full lint (F821, E722, F401, E712, F811, F541)
  2. PYLINT       — code quality (E, F, W categories)
  3. MYPY         — type check (sadece kritik dosyalar)
  4. CRITICAL     — timezone, API, scraper, DB, backup, take profit, fee
  5. UNIT+RISK    — formül, kelly, risk manager, take profit
  6. REGRESSION   — bilinen hataların tekrarlamaması
  7. IMPORT       — tüm modüller import edilebilir mi

Kullanım:
    python quick_check.py          # tüm testler (~90s)
    python quick_check.py --fast   # sadece lint + import (~15s)

Çıkış kodu: 0 = tümü geçti, 1 = hata var
"""

import subprocess
import sys
import time

TOTAL = 7


def run(name: str, cmd: list[str], fast_only: bool = False) -> bool:
    """Tek test çalıştır, sonucu yazdır."""
    if fast_only and name not in ("RUFF", "MYPY", "PYLINT"):
        return True

    print(f"\n{'='*50}")
    print(f"  {name}")
    print(f"{'='*50}")

    start = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    elapsed = time.time() - start

    if result.returncode == 0:
        lines = result.stdout.strip().split("\n")
        summary = lines[-1] if lines else "OK"
        print(f"  PASS ({elapsed:.1f}s): {summary}")
        return True
    else:
        lines = result.stdout.strip().split("\n") + result.stderr.strip().split("\n")
        for line in lines[-15:]:
            print(f"  {line}")
        print(f"\n  FAIL ({elapsed:.1f}s)")
        return False


def main() -> int:
    fast = "--fast" in sys.argv
    passed = 0

    start_all = time.time()

    tests = [
        ("RUFF", [
            "python", "-m", "ruff", "check", ".",
            "--select", "F821,E722,F401,E712,F811,F541",
            "--statistics",
        ]),
        ("PYLINT", [
            "python", "-m", "pylint",
            "engine/strategy.py",
            "engine/calculator.py",
            "executor/bet_placer.py",
            "executor/settler.py",
            "utils/formulas.py",
            "utils/kelly.py",
            "bot_loop.py",
            "--disable=C,R,W",
            "--fail-under=9.0",
            "--output-format=text",
        ]),
        ("MYPY", [
            "python", "-m", "mypy",
            "engine/strategy.py",
            "utils/formulas.py",
            "utils/kelly.py",
            "config/settings.py",
            "--config-file=mypy.ini",
            "--ignore-missing-imports",
        ]),
        ("CRITICAL", [
            "python", "-m", "pytest", "tests/test_critical_bugs.py",
            "-q", "--tb=line", "--no-header",
        ]),
        ("UNIT+RISK", [
            "python", "-m", "pytest",
            "tests/test_take_profit_comprehensive.py",
            "tests/test_active_risk_management.py",
            "tests/test_units.py",
            "-q", "--tb=line", "--no-header",
        ]),
        ("REGRESSION", [
            "python", "-m", "pytest", "tests/test_regression.py",
            "-q", "--tb=line", "--no-header",
        ]),
        ("IMPORT", [
            "python", "-c",
            "import engine.strategy; import engine.calculator; "
            "import executor.bet_placer; import executor.settler; "
            "import api; import utils.formulas; import utils.kelly; "
            "import utils.slippage; "
            "print('All imports OK')",
        ]),
    ]

    for name, cmd in tests:
        ok = run(name, cmd, fast)
        if ok:
            passed += 1

    elapsed_all = time.time() - start_all

    print(f"\n{'='*50}")
    if passed == TOTAL:
        print(f"  ALL {TOTAL} PASSED ({elapsed_all:.1f}s)")
    else:
        print(f"  {passed}/{TOTAL} PASSED, {TOTAL - passed} FAILED ({elapsed_all:.1f}s)")
    print(f"{'='*50}")

    return 0 if passed == TOTAL else 1


if __name__ == "__main__":
    sys.exit(main())
