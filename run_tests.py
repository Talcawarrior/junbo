"""Junbo Test Runner - Tüm testleri çalıştır.

Kullanım:
    python run_tests.py                    # Tüm testler
    python run_tests.py --unit             # Sadece unit testler
    python run_tests.py --regression       # Sadece regression testler
    python run_tests.py --property         # Sadece property-based testler
    python run_tests.py --golden           # Sadece golden/snapshot testler
    python run_tests.py --e2e              # Sadece E2E testler
    python run_tests.py --coverage         # Coverage ile çalıştır
    python run_tests.py --lint             # Sadece lint (ruff + mypy)
    python run_tests.py --all              # Her şeyi çalıştır
"""

import sys
import subprocess
from pathlib import Path


def run_command(cmd: list[str], description: str) -> int:
    """Run a command and return exit code."""
    print(f"\n{'='*60}")
    print(f"  {description}")
    print(f"{'='*60}")

    result = subprocess.run(cmd, cwd=Path(__file__).parent)
    return result.returncode


def main():
    """Main test runner."""
    import argparse

    parser = argparse.ArgumentParser(description="Junbo Test Runner")
    parser.add_argument("--unit", action="store_true", help="Run unit tests")
    parser.add_argument("--regression", action="store_true", help="Run regression tests")
    parser.add_argument("--property", action="store_true", help="Run property-based tests")
    parser.add_argument("--golden", action="store_true", help="Run golden/snapshot tests")
    parser.add_argument("--e2e", action="store_true", help="Run integration/E2E tests")
    parser.add_argument("--coverage", action="store_true", help="Run with coverage")
    parser.add_argument("--lint", action="store_true", help="Run lint (ruff + mypy)")
    parser.add_argument("--all", action="store_true", help="Run everything")

    args = parser.parse_args()

    # Default: run all if no specific option
    if not any([args.unit, args.regression, args.property, args.golden, args.e2e, args.coverage, args.lint]):
        args.all = True

    exit_codes = []

    # Lint
    if args.lint or args.all:
        # Ruff
        code = run_command(["ruff", "check", "."], "Running Ruff linter")
        exit_codes.append(("Ruff", code))

        # Mypy
        code = run_command(["mypy", "--ignore-missing-imports", "."], "Running mypy type checker")
        exit_codes.append(("Mypy", code))

    # Unit tests
    if args.unit or args.all:
        code = run_command(
            ["pytest", "tests/test_units.py", "-v", "--tb=short"],
            "Running unit tests"
        )
        exit_codes.append(("Unit tests", code))

    # Regression tests
    if args.regression or args.all:
        code = run_command(
            ["pytest", "tests/test_regression.py", "-v", "--tb=short"],
            "Running regression tests"
        )
        exit_codes.append(("Regression tests", code))

    # Property-based tests
    if args.property or args.all:
        code = run_command(
            ["pytest", "tests/test_property_based.py", "-v", "--tb=short"],
            "Running property-based tests (Hypothesis)"
        )
        exit_codes.append(("Property-based tests", code))

    # Golden/snapshot tests
    if args.golden or args.all:
        code = run_command(
            ["pytest", "tests/test_golden_snapshot.py", "-v", "--tb=short"],
            "Running golden/snapshot tests"
        )
        exit_codes.append(("Golden/snapshot tests", code))

    # Integration/E2E tests
    if args.e2e or args.all:
        code = run_command(
            ["pytest", "tests/test_integration_e2e.py", "-v", "--tb=short"],
            "Running integration/E2E tests"
        )
        exit_codes.append(("Integration/E2E tests", code))

    # Coverage
    if args.coverage or args.all:
        code = run_command(
            ["pytest", "tests/", "-v", "--cov=.", "--cov-report=term-missing", "--tb=short"],
            "Running all tests with coverage"
        )
        exit_codes.append(("Coverage", code))

    # Summary
    print(f"\n{'='*60}")
    print("  TEST SUMMARY")
    print(f"{'='*60}")

    all_passed = True
    for name, code in exit_codes:
        status = "✓ PASSED" if code == 0 else "✗ FAILED"
        print(f"  {name}: {status}")
        if code != 0:
            all_passed = False

    print(f"\n{'='*60}")
    if all_passed:
        print("  ALL TESTS PASSED ✓")
    else:
        print("  SOME TESTS FAILED ✗")
    print(f"{'='*60}\n")

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
