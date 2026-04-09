"""
Видаляє дублікати з signals: для кожної пари (market_id, direction)
залишає лише перший запис (MIN(id)), решту видаляє.
Потім виводить чистий PnL.
"""
import sqlite3
import os

db = os.path.join(os.path.dirname(__file__), "signals.db")
conn = sqlite3.connect(db)
cur = conn.cursor()

# Знаходимо id, які треба залишити (перший запис на market_id + direction)
cur.execute("""
    SELECT MIN(id) as keep_id
    FROM signals
    GROUP BY market_id, direction
""")
keep_ids = {row[0] for row in cur.fetchall()}

# Знаходимо дублікати
cur.execute("SELECT id FROM signals")
all_ids = {row[0] for row in cur.fetchall()}
to_delete = all_ids - keep_ids

print(f"Всього записів: {len(all_ids)}")
print(f"Унікальних (market_id+direction): {len(keep_ids)}")
print(f"Дублікатів до видалення: {len(to_delete)}")

if to_delete:
    placeholders = ",".join("?" for _ in to_delete)
    cur.execute(f"DELETE FROM signals WHERE id IN ({placeholders})", list(to_delete))
    conn.commit()
    print(f"Видалено {cur.rowcount} дублікатів.")

# Чистий PnL
print()
print("=== ЧИСТИЙ PnL (після дедуплікації) ===")
cur.execute("""
    SELECT
        COUNT(*) as total,
        SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END) as resolved,
        SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) as losses,
        ROUND(SUM(pnl), 2) as total_pnl
    FROM signals
""")
r = cur.fetchone()
total, resolved, wins, losses, pnl = r
print(f"  Total: {total}  |  Resolved: {resolved}  |  W: {wins}  L: {losses}")
if resolved and resolved > 0:
    print(f"  Winrate: {wins/resolved*100:.1f}%")
print(f"  PnL: ${pnl}")

print()
cur.execute("""
    SELECT direction,
           COUNT(*) as n,
           SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as w,
           SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) as l,
           ROUND(SUM(pnl), 2) as pnl
    FROM signals WHERE result IS NOT NULL GROUP BY direction
""")
for row in cur.fetchall():
    d, n, w, lo, p = row
    wr = w / n * 100 if n > 0 else 0
    print(f"  {d}: {w}W / {lo}L (winrate {wr:.0f}%) | PnL: ${p}")

conn.close()
