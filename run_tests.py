"""Test runner script - Tüm testleri çalıştır.

Kullanım:
    python run_tests.py
    python run_tests.py --unit
    python run_tests.py --integration
    python run_tests.py --verbose
"""

import sys
import pytest
from pathlib import Path


def run_tests(unit: bool = True, integration: bool = True, verbose: bool = False):
    """Tüm testleri çalıştır."""
    test_dir = Path(__file__).parent / "tests"

    pytest_args = [
        str(test_dir),
        "-v",
    ]

    if verbose:
        pytest_args.append("-s")

    if unit:
        pytest_args.extend(["test_units.py"])
    if integration:
        pytest_args.extend(["test_integration.py"])

    exit_code = pytest.main(pytest_args)
    sys.exit(exit_code)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Junbo Test Runner")
    parser.add_argument(
        "--unit",
        action="store_true",
        default=True,
        help="Run unit tests only",
    )
    parser.add_argument(
        "--integration",
        action="store_true",
        default=True,
        help="Run integration tests only",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--no-unit",
        action="store_false",
        dest="unit",
        help="Skip unit tests",
    )
    parser.add_argument(
        "--no-integration",
        action="store_false",
        dest="integration",
        help="Skip integration tests",
    )

    args = parser.parse_args()

    run_tests(
        unit=args.unit,
        integration=args.integration,
        verbose=args.verbose,
    )