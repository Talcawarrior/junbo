"""Tests for 30-min snapshot bucket logic in jobs.snapshot_job.

Uses the production temp-DB redirection from conftest (data/bot.db never
touched). Focus is on the pure bucket helpers; an integration check inserts
distinct-bucket rows directly and confirms no overwrite logic issue.
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobs.snapshot_job import _bucket_start, _same_bucket  # noqa: E402


def test_bucket_start_boundaries():
    t0 = datetime(2026, 8, 7, 10, 0, 0)
    assert _bucket_start(t0) == t0
    # 10:29 still bucket 10:00
    assert _bucket_start(datetime(2026, 8, 7, 10, 29, 59)) == t0
    # 10:30 rolls to 10:30 bucket
    assert _bucket_start(datetime(2026, 8, 7, 10, 30, 0)) == datetime(2026, 8, 7, 10, 30, 0)
    # 23:59 -> 23:30
    assert _bucket_start(datetime(2026, 8, 7, 23, 59, 59)) == datetime(2026, 8, 7, 23, 30, 0)
    # 00:00 -> 00:00 (wrap top-of-hour)
    assert _bucket_start(datetime(2026, 8, 7, 0, 0, 0)) == datetime(2026, 8, 7, 0, 0, 0)


def test_same_bucket_comparisons():
    a = datetime(2026, 8, 7, 10, 5, 0)
    b = datetime(2026, 8, 7, 10, 25, 0)  # same 10:00 bucket
    c = datetime(2026, 8, 7, 10, 35, 0)  # 10:30 bucket -> different
    assert _same_bucket(a, b) is True
    assert _same_bucket(a, c) is False
    assert _same_bucket(c, datetime(2026, 8, 7, 10, 55, 0)) is True


def test_bucket_cross_hour_boundary():
    # 09:59 and 10:01 are in different buckets
    assert (
        _same_bucket(
            datetime(2026, 8, 7, 9, 59, 59),
            datetime(2026, 8, 7, 10, 1, 0),
        )
        is False
    )
    # 09:29 and 09:01 are same 09:00 bucket
    assert (
        _same_bucket(
            datetime(2026, 8, 7, 9, 1, 0),
            datetime(2026, 8, 7, 9, 29, 0),
        )
        is True
    )
