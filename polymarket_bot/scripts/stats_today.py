"""Quick stats: today + overall."""
import sys, io, sqlite3, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

DB = os.path.join(os.path.dirname(__file__), "..", "live.db")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

all_sigs = conn.execute(
    "SELECT id, direction, decision, result, pnl, stake_usd, "
    "contract_price, live_entry_status, timestamp FROM signals ORDER BY id"
).fetchall()

def stats(sigs, label):
    approved = [s for s in sigs if s["decision"] == "approve"]
    wins  = [s for s in approved if s["result"] == "WIN"]
    losses = [s for s in approved if s["result"] == "LOSS"]
    no_ent = [s for s in approved if s["result"] == "NO_ENTRY"]
    pending = [s for s in approved if not s["result"]]
    pnl = sum(s["pnl"] or 0 for s in approved)
    trades = len(wins) + len(losses)
    wr = (len(wins) / trades * 100) if trades else 0

    print(f"\n{'='*40}")
    print(f"  {label}")
    print(f"{'='*40}")
    print(f"  Signals total:  {len(sigs)}")
    print(f"  Approved:       {len(approved)}")
    print(f"  Trades (W/L):   {len(wins)}W / {len(losses)}L  ({wr:.1f}%)")
    print(f"  NO_ENTRY:       {len(no_ent)}")
    print(f"  Pending:        {len(pending)}")
    print(f"  Total PnL:      ${pnl:+.2f}")
    if trades:
        print(f"  Avg PnL/trade:  ${pnl/trades:+.2f}")

    # live vs paper
    live_trades = [s for s in approved if (s["live_entry_status"] or "") == "opened"]
    paper_trades = [s for s in approved if not s["live_entry_status"]]
    if live_trades:
        lw = sum(1 for s in live_trades if s["result"] == "WIN")
        ll = sum(1 for s in live_trades if s["result"] == "LOSS")
        lp = sum(s["pnl"] or 0 for s in live_trades)
        lt = lw + ll
        print(f"  --- Live:  {lw}W/{ll}L ({lw/lt*100:.0f}%) PnL=${lp:+.2f}" if lt else "")
    if paper_trades:
        pw = sum(1 for s in paper_trades if s["result"] == "WIN")
        pl = sum(1 for s in paper_trades if s["result"] == "LOSS")
        pp = sum(s["pnl"] or 0 for s in paper_trades)
        pt = pw + pl
        print(f"  --- Paper: {pw}W/{pl}L ({pw/pt*100:.0f}%) PnL=${pp:+.2f}" if pt else "")

stats(all_sigs, "OVERALL")

today = [s for s in all_sigs if s["timestamp"] and "2026-04-06" in str(s["timestamp"])]
stats(today, "TODAY (2026-04-06)")

# Details today
print(f"\n{'='*40}")
print("  TODAY DETAILS")
print(f"{'='*40}")
for s in today:
    ts = str(s["timestamp"])[11:16] if s["timestamp"] else "?"
    d = (s["direction"] or "?")[:4]
    res = s["result"] or "-"
    pnl = s["pnl"] or 0
    le = s["live_entry_status"] or "paper"
    cp = s["contract_price"] or 0
    print(f"  #{s['id']:3d} | {ts} | {d:4s} | {res:8s} | ${pnl:+.2f} | cp={cp:.2f} | {le}")

conn.close()
