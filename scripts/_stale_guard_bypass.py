# Stale Guard bypass backtest - 2026-08-20
# Soru: STALE PRICE GUARD rotasyon aninda kaldirilsaydi (bypass) ne olurdu?
# Iki senaryo: (A) bot'un koduyla DB fiyatindan giris, (B) gercekci CLOB ask fiyatindan.
import sqlite3
import re

con = sqlite3.connect("file:data/bot.db?mode=ro", uri=True)
cur = con.cursor()

cur.execute(
    "SELECT ts, city, detail FROM activity_events "
    "WHERE detail LIKE '%STALE%GUARD%' OR detail LIKE '%STALE PRICE%' ORDER BY ts"
)
blocks = cur.fetchall()


def _bucket_before(city, ts):
    cur.execute(
        "SELECT detail FROM activity_events WHERE city=? AND category='peak_found' AND ts<=? ORDER BY ts DESC LIMIT 1",
        (city, ts),
    )
    r = cur.fetchone()
    if r:
        m = re.search(r"bucket=([0-9.]+)C", r[0])
        if m:
            return int(float(m.group(1)))
    return None


def winner_bucket(city, day):
    cur.execute(
        "SELECT threshold FROM weather_markets WHERE city=? AND target_date LIKE ?||'%' "
        "AND yes_price>=0.9999 ORDER BY target_date LIMIT 1",
        (city, day),
    )
    r = cur.fetchone()
    return int(float(r[0])) if r else None


STAKE = 3.0
uniq: dict[tuple[str, int | None], dict] = {}
for ts, city, detail in blocks:
    day = ts[:10]
    b = _bucket_before(city, ts)
    w = winner_bucket(city, day)
    won = (b == w) if (b is not None and w is not None) else None
    m = re.search(r"DB=([0-9.]+)", detail)
    db_p = float(m.group(1)) if m else None
    a = re.search(r"(?:CLOB ask|orderbook ask)=([0-9.]+)", detail)
    ask = float(a.group(1)) if a else None
    key = (city, b)
    v = uniq.setdefault(key, {"ts": ts, "won": won, "db": db_p, "ask": ask, "n": 0})
    v["n"] += 1
    if v["db"] is None:
        v["db"] = db_p
    elif db_p is not None and db_p < v["db"]:
        v["db"] = db_p
    if v["ask"] is None:
        v["ask"] = ask
    elif ask is not None and ask < v["ask"]:
        v["ask"] = ask


def pnl(entry):
    if entry is None or entry <= 0:
        return None
    return STAKE / entry - STAKE  # kazanan 1.0'a cikar, stake $3


tot_db = 0.0
tot_ask = 0.0
n_win = n_lose = n_und = 0
print("=== STALE GUARD BYPASS PNL (stake $3, 52 blok -> benzersiz betler) ===")
for (city, b), v in sorted(uniq.items(), key=lambda x: x[1]["ts"]):
    if v["won"] is None:
        print(
            "%-10s %-14s %2dC  BELIRSIZ (bugun, cozulmedi)  DB@%s ask@%s"
            % (v["ts"][:10], city, b, ("%.3f" % v["db"]) if v["db"] else "-", ("%.3f" % v["ask"]) if v["ask"] else "-")
        )
        n_und += 1
        continue
    g_db = pnl(v["db"]) if v["won"] else -STAKE
    g_ask = pnl(v["ask"]) if v["won"] else -STAKE
    tot_db += g_db if g_db is not None else -STAKE
    tot_ask += g_ask if g_ask is not None else -STAKE
    if v["won"]:
        tag = "KAZANIR"
        n_win += 1
    else:
        tag = "kaybeder"
        n_lose += 1
    print(
        "%-10s %-14s %2dC  %-9s DB@%-6s->%+7.2f  ask@%-6s->%+7.2f"
        % (
            v["ts"][:10],
            city,
            b,
            tag,
            ("%.3f" % v["db"]) if v["db"] else "-",
            g_db,
            ("%.3f" % v["ask"]) if v["ask"] else "-",
            g_ask,
        )
    )

print()
print("KAZANIR: %d | KAYBEDER: %d | BELIRSIZ: %d" % (n_win, n_lose, n_und))
print("NET (A) DB fiyatindan (bot kodunun yapacagi): %+8.2f USD" % tot_db)
print("NET (B) CLOB ask fiyatindan (gercekci):       %+8.2f USD" % tot_ask)
