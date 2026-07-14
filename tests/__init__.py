"""Junbo Test Suite - Vibe Coding Test Framework

Test katmanları:
1. Regression tests (en kritik)
2. Unit tests (pytest + parametrize)
3. Property-based testing (Hypothesis)
4. Golden/snapshot tests
5. Static analysis (mypy, ruff)
6. Integration/E2E tests

Kullanım:
    pytest tests/ -v
    pytest tests/ -v --cov=.
    pytest tests/test_regression.py -v
    pytest tests/test_property_based.py -v
"""
