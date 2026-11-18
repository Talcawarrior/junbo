import sqlite3
from pathlib import Path

BOT_DB = Path(r"C:\Users\fdemir\Documents\New project\junbo\data\bot.db")

con = sqlite3.connect(BOT_DB)
cur = con.cursor()

print("Bets by status:")
cur.execute("SELECT status, count(*) FROM bets GROUP BY status")
for row in cur.fetchall():
    print(f"  {row[0]}: {row[1]}")

print("\nPlaced bets sample:")
cur.execute(
    "SELECT substr(order_id,1,10), status, count(*) FROM bets "
    "WHERE status='placed' GROUP BY substr(order_id,1,10), status"
)
for row in cur.fetchall():
    print(f"  {row[0]}: {row[2]} bets")

print("\nAll order_id prefixes for placed bets:")
cur.execute("SELECT DISTINCT substr(order_id,1,6) FROM bets WHERE status='placed'")
prefixes = cur.fetchall()
print([p[0] for p in prefixes])

con.close()
