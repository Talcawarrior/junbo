"""Quick CI — Her kod değişikliği sonrası çalıştır.

4 test (toplam ~15 saniye):
  1. ruff lint     — syntax hataları, undefined names, bare except
  2. unit tests    — formül, kelly, risk manager, take profit
  3. regression    — bilinen hataların tekrarlamaması
  4. import check  — tüm modüller import edilebilir mi

Kullanım:
    python quick_check.py          # 4 testi çalıştır
    python quick_check.py --fast   # sadece lint + import (5 sn)

Çıkış kodu: 0 = tümü geçti, 1 = hata var
"""

import subprocess
import sys
import time


def run(name: str, cmd: list[str], fast_only: bool = False) -> bool:
    """Tek test çalıştır, sonucu yazdır."""
    if fast_only and name not in ("LINT", "IMPORT"):
        return True

    print(f"\n{'='*50}")
    print(f"  {name}")
    print(f"{'='*50}")

    start = time.time()
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    elapsed = time.time() - start

    if result.returncode == 0:
        # Son satırı göster (test counts vs)
        lines = result.stdout.strip().split("\n")
        summary = lines[-1] if lines else "OK"
        print(f"  PASS ({elapsed:.1f}s): {summary}")
        return True
    else:
        # Hata detaylarını göster
        lines = result.stdout.strip().split("\n") + result.stderr.strip().split("\n")
        for line in lines[-15:]:
            print(f"  {line}")
        print(f"\n  FAIL ({elapsed:.1f}s)")
        return False


def main() -> int:
    fast = "--fast" in sys.argv
    passed = 0
    total = 4

    start_all = time.time()

    tests = [
        ("LINT", ["python", "-m", "ruff", "check", ".", "--select", "F821,E722"]),
        ("UNIT", ["python", "-m", "pytest",
                  "tests/test_take_profit_comprehensive.py",
                  "tests/test_active_risk_management.py",
                  "tests/test_units.py",
                  "-q", "--tb=line", "--no-header"]),
        ("REGRESSION", ["python", "-m", "pytest",
                        "tests/test_regression.py",
                        "-q", "--tb=line", "--no-header"]),
        ("IMPORT", ["python", "-c",
                    "import engine.strategy; import engine.calculator; "
                    "import executor.bet_placer; import executor.settler; "
                    "import api; import utils.formulas; import utils.kelly; "
                    "import utils.slippage; "
                    "print('All imports OK')"]),
    ]

    for name, cmd in tests:
        ok = run(name, cmd, fast)
        if ok:
            passed += 1

    elapsed_all = time.time() - start_all

    print(f"\n{'='*50}")
    if passed == total:
        print(f"  ALL {total} PASSED ({elapsed_all:.1f}s)")
    else:
        print(f"  {passed}/{total} PASSED, {total - passed} FAILED ({elapsed_all:.1f}s)")
    print(f"{'='*50}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
