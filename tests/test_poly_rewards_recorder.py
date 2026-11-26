"""Polymarket odul kaydinin dakikalik turu (research/poly_rewards_recorder.py; 2026-11-26 aksam duzeltmesi).

Canli kayitta olculen iki kayip:
  1. Her hedef yenilemesinde 1 dakika bos: yenileme ornekten once calisiyor, ornek sonraki dakikanin etiketini
     aliyor, sonraki tur da ayni etiketin ustune yaziyordu (17:30 oncesi 31 kayip dakikanin 27'si).
  2. WARP kopuklugunda basarisiz tur 6 deneme + son denemeden sonra bosuna bekleme ile ~95 sn suruyor, gorev
     IgnoreNew oldugu icin sonraki dakikanin tetigi de atlaniyordu.
Ag cagrisi YOK: istekler ve saat sahte.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

RESEARCH = Path(__file__).resolve().parent.parent / "research"
sys.path.insert(0, str(RESEARCH))

import poly_rewards_recorder as rec  # noqa: E402
import poly_rewards_scan as scan  # noqa: E402

T0 = 1_795_651_247.0  # bir dakikanin 47. saniyesi (gorev tetigi boyle duser)
MINUTE = int(T0 // 60 * 60)
_BOOK = {
    "bids": [{"price": "0.49", "size": "100"}],
    "asks": [{"price": "0.51", "size": "100"}],
    "tick_size": "0.01",
}


class _Clock:
    """Sahte saat: sleep() ve sahte istekler saati ilerletir."""

    def __init__(self, now: float) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = _Clock(T0)
    monkeypatch.setattr(scan, "time", c)
    monkeypatch.setattr(rec, "time", c)
    monkeypatch.setattr(scan, "_DEADLINE", None)
    return c


def _seed(con, refreshed_at: float) -> None:
    con.execute(
        "INSERT INTO markets (id, cid, yes_token, question, event_slug, category, end_date, first_seen) "
        "VALUES (1, '0xabc', 'tok', 'q', 'e', 'weather', '', 0)"
    )
    con.execute("INSERT INTO targets VALUES (1, 100.0, 4.5, 20.0)")
    con.execute("INSERT OR REPLACE INTO meta VALUES ('targets_refreshed_at', ?)", (str(int(refreshed_at)),))
    con.commit()


def _no_refresh(con_, top):
    pytest.fail("bu turda hedef yenileme beklenmiyordu")


def test_failing_books_request_ends_within_turn_budget(clock, monkeypatch):
    """WARP kopukken tur butceyi asmaz -> sonraki dakikanin tetigi atlanmaz (eskiden ~95 sn)."""
    timeouts: list[float] = []

    def down(url, json=None, proxies=None, headers=None, timeout=None):
        timeouts.append(timeout)
        clock.now += min(5.0, timeout)  # baglanti hatasi en gec zaman asiminda doner
        raise requests.ConnectionError("SOCKS kopuk")

    monkeypatch.setattr(scan.requests, "post", down)
    monkeypatch.setattr(scan, "_DEADLINE", T0 + rec._BUDGET_S)
    with pytest.raises(RuntimeError, match="tur suresi doldu"):
        scan._post_books(["tok"])
    assert clock.now - T0 <= rec._BUDGET_S
    assert max(timeouts) <= rec._BUDGET_S


def test_without_deadline_all_tries_run_and_no_sleep_after_last(clock, monkeypatch):
    """Tarama betigi (son tarih yok): tum denemeler yapilir; son denemeden sonra bosuna beklenmez."""

    def down(*args, **kwargs):
        raise requests.ConnectionError("kopuk")

    monkeypatch.setattr(scan.requests, "get", down)
    with pytest.raises(RuntimeError, match=r"\(3/3 deneme\)"):
        scan._get("/rewards/markets/current", tries=3)
    assert clock.sleeps == [3.0, 6.0]


def test_tick_samples_first_and_keeps_its_minute_when_refresh_is_slow(clock, monkeypatch, tmp_path):
    """Yenileme 45 sn surse de ornek turun basladigi dakikaya yazilir (eskiden sonraki dakikaya kayiyordu)."""
    con = rec._db(tmp_path / "r.db")
    _seed(con, T0 - rec._REFRESH_S - 60)  # yenileme zamani gelmis
    order: list[str] = []
    deadlines: list[float | None] = []

    def books(tokens):
        order.append("ornek")
        deadlines.append(scan._DEADLINE)
        return {"tok": _BOOK}

    def refresh(con_, top):
        order.append("yenileme")
        deadlines.append(scan._DEADLINE)
        clock.now += 45.0
        con_.execute("INSERT OR REPLACE INTO meta VALUES ('targets_refreshed_at', ?)", (str(int(clock.now)),))

    monkeypatch.setattr(scan, "_fetch_books", books)
    monkeypatch.setattr(rec, "_refresh_targets", refresh)
    assert rec._tick(con, 300, T0) == (1, 1, [])
    assert order == ["ornek", "yenileme"]
    assert [r[0] for r in con.execute("SELECT ts FROM samples")] == [MINUTE]
    # ornek sonraki tetikten once biter; yenileme ertelenmesin diye daha uzun surebilir
    assert deadlines == [T0 + rec._BUDGET_S, T0 + rec._REFRESH_BUDGET_S]
    assert rec._BUDGET_S < 60 - 2.5 and rec._REFRESH_BUDGET_S < 120 - 2.5  # import ~2.3 sn; en fazla 1 tetik
    assert scan._DEADLINE is None


def test_tick_keeps_sample_when_refresh_fails(clock, monkeypatch, tmp_path):
    """Yenileme hatasi o dakikanin ornegini dusurmez; eski hedeflerle devam, sonraki tur yeniden dener."""
    con = rec._db(tmp_path / "r.db")
    _seed(con, T0 - rec._REFRESH_S - 60)
    monkeypatch.setattr(scan, "_fetch_books", lambda tokens: {"tok": _BOOK})

    def refresh(con_, top):
        raise RuntimeError("GET /rewards/markets/current basarisiz (4/6 deneme, tur suresi doldu)")

    monkeypatch.setattr(rec, "_refresh_targets", refresh)
    n_t, n_s, errors = rec._tick(con, 300, T0)
    assert (n_t, n_s) == (1, 1)
    assert len(errors) == 1 and errors[0].startswith("yenileme RuntimeError")
    assert rec._refresh_due(con)  # zaman damgasi eski kaldi -> sonraki tur yeniden dener
    assert scan._DEADLINE is None


def test_tick_on_empty_db_refreshes_once_before_first_sample(clock, monkeypatch, tmp_path):
    """Bos DB: once yenileme (uzun butceyle; 2026-11-26 elle denemede ilk yenileme 50 sn'ye sigmadi), sonra ornek."""
    con = rec._db(tmp_path / "r.db")
    order: list[str] = []
    deadlines: list[float | None] = []

    def refresh(con_, top):
        order.append("yenileme")
        deadlines.append(scan._DEADLINE)
        _seed(con_, clock.now)

    def books(tokens):
        order.append("ornek")
        return {"tok": _BOOK}

    monkeypatch.setattr(rec, "_refresh_targets", refresh)
    monkeypatch.setattr(scan, "_fetch_books", books)
    assert rec._tick(con, 300, T0) == (1, 1, [])
    assert order == ["yenileme", "ornek"]
    assert deadlines == [T0 + rec._REFRESH_BUDGET_S]


def test_tick_reports_sample_failure_and_clears_deadline(clock, monkeypatch, tmp_path):
    con = rec._db(tmp_path / "r.db")
    _seed(con, T0)  # yenileme zamani gelmemis
    msg = "POST /books (1 token) basarisiz (5/6 deneme, tur suresi doldu)"

    def books(tokens):
        raise RuntimeError(msg)

    monkeypatch.setattr(scan, "_fetch_books", books)
    monkeypatch.setattr(rec, "_refresh_targets", _no_refresh)
    assert rec._tick(con, 300, T0) == (0, 0, [f"ornek RuntimeError: {msg}"])
    assert con.execute("SELECT count(*) FROM samples").fetchone()[0] == 0
    assert scan._DEADLINE is None
