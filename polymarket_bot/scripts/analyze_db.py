import sqlite3
import os

db = os.path.join(os.path.dirname(__file__), "signals.db")
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=== ЗАГАЛЬНА СТАТИСТИКА ===")
cur.execute("""
    SELECT
        COUNT(*) as total,
        SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END) as resolved,
        SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) as losses,
        ROUND(SUM(pnl), 2) as total_pnl,
        ROUND(SUM(stake_usd), 2) as total_staked
    FROM signals
""")
r = dict(cur.fetchone())
for k, v in r.items():
    print(f"  {k}: {v}")
if r["resolved"] and r["resolved"] > 0:
    wr = (r["wins"] or 0) / r["resolved"] * 100
    print(f"  winrate: {wr:.1f}%")

print()
print("=== ОСТАННІ 25 ЗАПИСІВ ===")
cur.execute("""
    SELECT id, timestamp, market_id, direction, contract_price,
           start_price, current_price, delta_percent, time_left,
           result, pnl, stake_usd, bot_mode, decision
    FROM signals ORDER BY id DESC LIMIT 25
""")
for r in cur.fetchall():
    r = dict(r)
    res = r["result"] or "pending"
    pnl = f'{r["pnl"]:.2f}' if r["pnl"] is not None else "-"
    print(
        f'  #{r["id"]:>3} | {r["timestamp"]} | mkt:{r["market_id"]} '
        f'| {r["direction"]:>4} | cp:{r["contract_price"]:.2f} '
        f'| start:{r["start_price"]:.2f} | cur:{r["current_price"]:.2f} '
        f'| d%:{r["delta_percent"]:+.3f}% | left:{r["time_left"]}m '
        f'| {res:<7} | pnl:{pnl:>7} | ${r["stake_usd"]} {r["bot_mode"]} {r["decision"]}'
    )

print()
print("=== ДУБЛІКАТИ: однаковий market_id + direction (>1 запис) ===")
cur.execute("""
    SELECT market_id, direction, COUNT(*) as cnt,
           MIN(timestamp) as first_ts, MAX(timestamp) as last_ts,
           GROUP_CONCAT(id) as ids
    FROM signals
    GROUP BY market_id, direction
    HAVING cnt > 1
    ORDER BY cnt DESC
    LIMIT 15
""")
rows = cur.fetchall()
if not rows:
    print("  (немає дублікатів)")
else:
    for r in rows:
        r = dict(r)
        print(
            f'  mkt:{r["market_id"]} {r["direction"]:>4} x{r["cnt"]} '
            f'| {r["first_ts"]} -- {r["last_ts"]} | ids: {r["ids"]}'
        )

print()
print("=== PnL ПО НАПРЯМКАХ ===")
cur.execute("""
    SELECT direction,
           COUNT(*) as total,
           SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins,
           SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) as losses,
           ROUND(SUM(pnl), 2) as pnl
    FROM signals
    WHERE result IS NOT NULL
    GROUP BY direction
""")
for r in cur.fetchall():
    r = dict(r)
    wr = r["wins"] / r["total"] * 100 if r["total"] > 0 else 0
    print(f'  {r["direction"]}: {r["wins"]}W / {r["losses"]}L (winrate {wr:.0f}%) | PnL: ${r["pnl"]}')

conn.close()
