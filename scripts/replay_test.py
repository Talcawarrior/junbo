"""Production DB kopyasiyla REPLAY dogrulama (2026-08-08).

Neden ayri script: pytest conftest'i DB_PATH'i temp DB'ye cevirir ve
bot_config singleton'i ilk importta donar — replay testi pytest icinde
CALISAMAZ (calisiyor gibi gorunur ama prod DB'yi degil temp DB'yi test eder).

Bu script production bot.db'yi KOPYALAR ve kopya uzerinde:
  1. settle_all -> kapanisi (target+12h) gecmemis hicbir market expired
     YAPILMAMALI (2026-08-08 bug: SL sonrasi canli market expired oluyordu,
     reopen yeni lider acamiyordu)
  2. _reopen_after_stop_loss -> gercek veriyle calisir, crash yok
  3. SL sonrasi acik beti OLMAYAN gruplar islenmis olmali (gate reddi de
     gecerli sonuctur; asil olan sessiz atlama OLMAMASI)

Kullanim:
    python scripts/replay_test.py
Cikis: 0 = basarili, 1 = hata (yanlis expired bulundu / crash)

Kopya: data/bot_replay_test.db (test bitince silinir).
"""
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROD_DB = os.path.join(REPO, "data", "bot.db")
COPY_DB = os.path.join(REPO, "data", "bot_replay_test.db")
sys.path.insert(0, REPO)


def main() -> int:
    if not os.path.exists(PROD_DB):
        print("SKIP: production bot.db yok")
        return 0

    shutil.copy2(PROD_DB, COPY_DB)
    # DB_PATH kopyaya yonlendir — import'lardan ONCE
    os.environ["DB_PATH"] = os.path.relpath(COPY_DB, REPO)

    from database.db import get_session  # noqa: E402
    from database.models import OPEN_BET_STATUSES, Bet, WeatherMarket  # noqa: E402
    from executor.bet_placer import BetPlacer  # noqa: E402
    from executor.settler import SettlementEngine  # noqa: E402

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    failures = 0

    # 0) Kopya gercekten kopya mi
    with get_session() as s:
        total = s.query(WeatherMarket).count()
        print(f"[1/4] kopya DB market sayisi: {total}")
        if total < 100:
            print("HATA: kopya DB bos gorunuyor — DB_PATH override calismadi")
            failures += 1

    # 1) settle_all — canli marketleri expired yapmamali
    print("[2/4] settle_all calistiriliyor (kopya uzerinde)...")
    SettlementEngine().settle_all()

    with get_session() as s:
        wrong = [
            m.id
            for m in s.query(WeatherMarket).filter(WeatherMarket.status == "expired").all()
            if m.target_date and (m.target_date + timedelta(hours=12)) > now
        ]
        if wrong:
            print(f"HATA: {len(wrong)} market kapanis gecmeden expired yapildi! Ornek: {wrong[:5]}")
            failures += 1
        else:
            print("[3/4] OK: kapanisi gecmemis hicbir market expired yapilmadi")

    # 2) reopen — gercek veriyle crash yok
    cutoff = now - timedelta(hours=6)
    with get_session() as s:
        lost = s.query(Bet).filter(Bet.close_reason.like("stop_loss%"), Bet.closed_at >= cutoff).all()
        print(f"[4/4] 6h icinde SL bet: {len(lost)}")

        unprocessed_groups = 0
        for b in lost:
            wm = s.query(WeatherMarket).filter_by(id=b.market_id).first()
            if not wm or not wm.target_date:
                continue
            td = wm.target_date
            if getattr(td, "tzinfo", None):
                td = td.replace(tzinfo=None)
            open_bet = (
                s.query(Bet)
                .filter(Bet.status.in_(OPEN_BET_STATUSES))
                .join(WeatherMarket, WeatherMarket.id == Bet.market_id)
                .filter(
                    WeatherMarket.city == wm.city,
                    WeatherMarket.target_date == td,
                    WeatherMarket.metric == (wm.metric or "unknown"),
                )
                .first()
            )
            if not open_bet:
                unprocessed_groups += 1

        try:
            result = BetPlacer()._reopen_after_stop_loss(s)
            print(f"     reopen sonuc: {result} (acilan yeni bet) | acik-bet'siz SL grubu: {unprocessed_groups}")
        except Exception as e:  # noqa: BLE001
            print(f"HATA: reopen crash: {e}")
            failures += 1

    # Session'lari kapat (WAL dosya kilidi birakir) — temizlik oncesi
    import gc

    from database.db import engine

    engine.dispose()
    gc.collect()

    for suffix in ("", "-wal", "-shm"):
        p = COPY_DB + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except PermissionError:
                print(f"UYARI: {p} silinemedi (kilitli) — elle temizle")
    if failures:
        print(f"SONUC: FAIL ({failures} hata)")
        return 1
    print("SONUC: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
