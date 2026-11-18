"""Replay DB izolasyonu.

Iki is yapar:

1. ``temp_db`` - botun DB yolunu GECICI bir dosyaya cevirir, `database.*`
   modullerini yeniden yukler ve semayi kurar. Production DB'ye HICBIR yazi
   gitmez (CLAUDE.md §7).
2. ``rebind_get_session`` - yeniden yukleme sonrasi modul-seviyesi
   ``from database.db import get_session`` baglarini guncel engine'e baglar.

2. madde olmadan su tuzak olusuyor (2026-10-27 olculdu): `sys.modules`'tan
`database.db` silinip yeniden import edilince YENI bir engine olusuyor, ama
`executor.bet_placer` gibi moduller ESKI fonksiyona bagli kaliyor. Sonuc:
temp DB'de 1 market varken bet_placer 0 goruyor.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager

#: Yeniden yuklenecek moduller (test teardown'i da ayni ikiliyi kullanir).
DB_MODULES = ("database.db", "database.models")

#: `get_session` bagini tasiyan modulleri bulmak icin kullanilan isaret.
_OWNER = "database.db"


def rebind_get_session() -> int:
    """Modul-seviyesi ``get_session`` baglarini ``database.db.get_session``e bagla.

    Donen deger: yeniden baglanan modul sayisi (teshis icin).
    """
    import database.db as db_mod

    current = getattr(db_mod, "get_session")
    rebound = 0
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_session", None)
        if bound is None or bound is current:
            continue
        if getattr(bound, "__module__", None) == _OWNER:
            setattr(mod, "get_session", current)
            rebound += 1
    return rebound


def _reload_db_modules() -> None:
    """`database.db` / `database.models`'i at ve yeniden import et."""
    for name in DB_MODULES:
        sys.modules.pop(name, None)
    from database.db import init_db

    init_db()


@contextmanager
def temp_db(path: str):
    """Botun DB yolunu gecici dosyaya al; cikista production yoluna doner.

    Kullanim::

        with temp_db(str(tmp_path / "replay.db")) as db_path:
            ...  # tum yazmalar db_path'e gider
    """
    import config.settings as cfg_mod

    prev_path = cfg_mod.config.DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        cfg_mod.config.DB_PATH = path
        _reload_db_modules()
        rebind_get_session()
        yield path
    finally:
        cfg_mod.config.DB_PATH = prev_path
        _reload_db_modules()
        rebind_get_session()
