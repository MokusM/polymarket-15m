import sqlite3
import os
import json

db = os.path.join(os.path.dirname(__file__), "signals.db")
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=== MEDIUM ONLY (bez test) ===")
cur.execute("""
    SELECT COUNT(*) as total,
           SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,
           SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) as losses,
           ROUND(SUM(pnl),2) as pnl
    FROM signals WHERE bot_mode='medium' AND result IS NOT NULL
""")
r = dict(cur.fetchone())
total = r["total"] or 0
wins = r["wins"] or 0
losses = r["losses"] or 0
pnl = r["pnl"] or 0
print(f"  Total: {total}, Wins: {wins}, Losses: {losses}, PnL: {pnl}")
if total > 0:
    print(f"  Winrate: {wins/total*100:.1f}%")

print()
print("=== PnL BY HOUR (UTC) ===")
cur.execute("""
    SELECT strftime('%H', timestamp) as hour,
           COUNT(*) as cnt,
           SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,
           SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) as losses,
           ROUND(SUM(pnl),2) as pnl
    FROM signals WHERE result IS NOT NULL
    GROUP BY hour ORDER BY hour
""")
for row in cur.fetchall():
    r = dict(row)
    cnt = r["cnt"] or 0
    w = r["wins"] or 0
    l = r["losses"] or 0
    wr = w / cnt * 100 if cnt > 0 else 0
    print(f"  {r['hour']}:00 UTC | {cnt:>2} signals | {w}W/{l}L | wr:{wr:.0f}% | PnL: {r['pnl']}")

print()
print("=== ATR VALUES (from payload) ===")
cur.execute("SELECT id, result, payload_json FROM signals WHERE payload_json IS NOT NULL AND result IS NOT NULL")
atr_win = []
atr_loss = []
for row in cur.fetchall():
    try:
        p = json.loads(row["payload_json"])
        atr = p.get("atr", 0)
        if row["result"] == "WIN":
            atr_win.append(atr)
        else:
            atr_loss.append(atr)
    except Exception:
        pass
if atr_win:
    print(f"  WIN  ATR: avg={sum(atr_win)/len(atr_win):.1f}, min={min(atr_win):.1f}, max={max(atr_win):.1f} (n={len(atr_win)})")
if atr_loss:
    print(f"  LOSS ATR: avg={sum(atr_loss)/len(atr_loss):.1f}, min={min(atr_loss):.1f}, max={max(atr_loss):.1f} (n={len(atr_loss)})")

print()
print("=== CONTRACT PRICE ANALYSIS ===")
cur.execute("""
    SELECT
        CASE WHEN contract_price < 0.45 THEN '<0.45'
             WHEN contract_price <= 0.55 THEN '0.45-0.55'
             ELSE '>0.55' END as price_bucket,
        COUNT(*) as cnt,
        SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,
        ROUND(SUM(pnl),2) as pnl
    FROM signals WHERE result IS NOT NULL
    GROUP BY price_bucket ORDER BY price_bucket
""")
for row in cur.fetchall():
    r = dict(row)
    cnt = r["cnt"] or 0
    w = r["wins"] or 0
    wr = w / cnt * 100 if cnt > 0 else 0
    print(f"  cp {r['price_bucket']:>9} | {cnt:>2} signals | {w}W | wr:{wr:.0f}% | PnL: {r['pnl']}")

print()
print("=== TIME LEFT ANALYSIS ===")
cur.execute("""
    SELECT
        CASE WHEN time_left < 5 THEN '<5m'
             WHEN time_left <= 10 THEN '5-10m'
             ELSE '>10m' END as tl_bucket,
        COUNT(*) as cnt,
        SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,
        ROUND(SUM(pnl),2) as pnl
    FROM signals WHERE result IS NOT NULL AND time_left IS NOT NULL
    GROUP BY tl_bucket ORDER BY tl_bucket
""")
for row in cur.fetchall():
    r = dict(row)
    cnt = r["cnt"] or 0
    w = r["wins"] or 0
    wr = w / cnt * 100 if cnt > 0 else 0
    print(f"  time_left {r['tl_bucket']:>5} | {cnt:>2} signals | {w}W | wr:{wr:.0f}% | PnL: {r['pnl']}")

print()
print("=== DELTA % ANALYSIS ===")
cur.execute("""
    SELECT
        CASE WHEN ABS(delta_percent) < 0.15 THEN '<0.15pct'
             WHEN ABS(delta_percent) <= 0.25 THEN '0.15-0.25pct'
             ELSE '>0.25pct' END as delta_bucket,
        COUNT(*) as cnt,
        SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,
        ROUND(SUM(pnl),2) as pnl
    FROM signals WHERE result IS NOT NULL
    GROUP BY delta_bucket ORDER BY delta_bucket
""")
for row in cur.fetchall():
    r = dict(row)
    cnt = r["cnt"] or 0
    w = r["wins"] or 0
    wr = w / cnt * 100 if cnt > 0 else 0
    print(f"  |delta| {r['delta_bucket']:>12} | {cnt:>2} signals | {w}W | wr:{wr:.0f}% | PnL: {r['pnl']}")

print()
print("=== CONSECUTIVE RESULTS (last 26) ===")
cur.execute("SELECT id, result, direction FROM signals WHERE result IS NOT NULL ORDER BY id")
rows = [dict(r) for r in cur.fetchall()]
streak = ""
for r in rows:
    streak += "W" if r["result"] == "WIN" else "L"
print(f"  {streak}")

conn.close()
