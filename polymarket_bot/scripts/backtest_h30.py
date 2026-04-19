"""
H30 Backtest: Early Entry Scalping
---
Hypothesis: Buy cheap contracts (0.30-0.50) early in the window (tl>=10 min),
when BTC/ETH/SOL barely moved. Hold to settlement.
WIN pays ~2-3x, LOSS costs the stake.

Uses: alt_signals (ETH/SOL) + shadow_signals (BTC) from live.db
"""

import sqlite3
import json
import sys
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "live.db"


def analyze_alt_early_entry(conn):
    """Analyze ETH/SOL early entries from alt_signals."""
    print("=" * 60)
    print("H30: EARLY ENTRY — ALT (ETH/SOL)")
    print("=" * 60)

    rows = conn.execute("""
        SELECT asset, direction, result, contract_price, clob_ask,
               delta_percent, consecutive_closes, confluence,
               time_left, volume_state, btc_aligned, atr, gap_pct,
               start_price, current_price
        FROM alt_signals
        WHERE result IN ('WIN', 'LOSS')
        ORDER BY id
    """).fetchall()

    stats = []
    for r in rows:
        d = dict(r)
        d['dp'] = abs(d['delta_percent'] or 0)
        d['cc'] = abs(d['consecutive_closes'] or 0)
        d['cp'] = d['contract_price'] or 0
        d['ask'] = d['clob_ask'] or d['cp']
        d['tl'] = d['time_left'] or 0
        stats.append(d)

    def show(label, subset):
        w = sum(1 for s in subset if s['result'] == 'WIN')
        l = sum(1 for s in subset if s['result'] == 'LOSS')
        if w + l == 0:
            return
        wr = w / (w + l) * 100
        # EV: WIN pays (1 - entry_price), LOSS costs entry_price
        avg_cp = sum(s['cp'] for s in subset) / len(subset)
        ev = wr / 100 * (1 - avg_cp) - (1 - wr / 100) * avg_cp
        roi_per_trade = ev / avg_cp * 100 if avg_cp > 0 else 0
        per_day = len(subset) / 4  # ~4 days of ALT data
        print(f"  {label:50s} {w:4d}W/{l:3d}L  WR={wr:5.1f}%  avgCP={avg_cp:.3f}  EV={ev:+.3f}  ROI={roi_per_trade:+.0f}%  ~{per_day:.0f}/day")

    # === EARLY ENTRY: tl >= 10 (first 5 min of window) ===
    print("\n--- Early entry (tl >= 10 min, first 5 min of 15-min window) ---\n")

    early = [s for s in stats if s['tl'] >= 10]

    show("ALL early (tl>=10)", early)
    print()

    # By contract price
    print("  By contract price:")
    for cp_lo, cp_hi in [(0.40, 0.45), (0.45, 0.50), (0.50, 0.55), (0.55, 0.65)]:
        show(f"    cp {cp_lo:.2f}-{cp_hi:.2f}", [s for s in early if cp_lo <= s['cp'] < cp_hi])

    print()

    # By delta_percent
    print("  By |delta_percent|:")
    for dp_min in [0, 0.02, 0.05, 0.10]:
        show(f"    dp>={dp_min:.2f}", [s for s in early if s['dp'] >= dp_min])

    print()

    # By confluence
    print("  By confluence:")
    for conf in [3, 4]:
        show(f"    conf>={conf}", [s for s in early if s['confluence'] >= conf])

    print()

    # Key combos for early entry
    print("  --- Best combos (early entry) ---")
    show("cp<=0.50", [s for s in early if s['cp'] <= 0.50])
    show("cp<=0.50 + dp>=0.02", [s for s in early if s['cp'] <= 0.50 and s['dp'] >= 0.02])
    show("cp<=0.50 + dp>=0.05", [s for s in early if s['cp'] <= 0.50 and s['dp'] >= 0.05])
    show("cp<=0.50 + conf>=3", [s for s in early if s['cp'] <= 0.50 and s['confluence'] >= 3])
    show("cp<=0.50 + conf>=4", [s for s in early if s['cp'] <= 0.50 and s['confluence'] >= 4])
    show("cp<=0.50 + btc_aligned", [s for s in early if s['cp'] <= 0.50 and s['btc_aligned']])
    show("cp<=0.50 + cc>=1", [s for s in early if s['cp'] <= 0.50 and s['cc'] >= 1])
    show("cp<=0.50 + dp>=0.02 + btc_aligned", [s for s in early if s['cp'] <= 0.50 and s['dp'] >= 0.02 and s['btc_aligned']])
    show("cp<=0.50 + dp>=0.02 + conf>=3", [s for s in early if s['cp'] <= 0.50 and s['dp'] >= 0.02 and s['confluence'] >= 3])

    print()

    # === LATE ENTRY comparison (current strategy: tl 4-9) ===
    print("--- Late entry comparison (tl 4-9, current strategy zone) ---\n")
    late = [s for s in stats if 4 <= s['tl'] <= 9]
    show("ALL late (tl 4-9)", late)
    show("late + conf>=4", [s for s in late if s['confluence'] >= 4])
    show("late + conf>=4 + dp>=0.10", [s for s in late if s['confluence'] >= 4 and s['dp'] >= 0.10])

    print()

    # === $ simulation: $4 stake ===
    print("--- $ simulation ($4 stake per trade) ---\n")
    stake = 4.0

    for label, subset in [
        ("Current: late conf>=4 dp>=0.10", [s for s in late if s['confluence'] >= 4 and s['dp'] >= 0.10]),
        ("H30: early cp<=0.50", [s for s in early if s['cp'] <= 0.50]),
        ("H30: early cp<=0.50 + dp>=0.02", [s for s in early if s['cp'] <= 0.50 and s['dp'] >= 0.02]),
        ("H30: early cp<=0.50 + dp>=0.05", [s for s in early if s['cp'] <= 0.50 and s['dp'] >= 0.05]),
        ("H30: early cp<=0.50 + btc_aligned", [s for s in early if s['cp'] <= 0.50 and s['btc_aligned']])
    ]:
        w = sum(1 for s in subset if s['result'] == 'WIN')
        l = sum(1 for s in subset if s['result'] == 'LOSS')
        if w + l == 0:
            continue
        # WIN: shares * 1.0 - stake; LOSS: -stake
        total_pnl = 0
        for s in subset:
            shares = stake / s['cp'] if s['cp'] > 0 else 0
            if s['result'] == 'WIN':
                total_pnl += shares * 1.0 - stake
            else:
                total_pnl -= stake
        avg_pnl = total_pnl / (w + l)
        wr = w / (w + l) * 100
        per_day = (w + l) / 4
        print(f"  {label:45s} {w}W/{l}L WR={wr:.0f}%  totalPnL=${total_pnl:+.0f}  avg=${avg_pnl:+.2f}  ~{per_day:.0f}/day")


def analyze_btc_early_entry(conn):
    """Analyze BTC early entries from shadow_signals."""
    print("\n" + "=" * 60)
    print("H30: EARLY ENTRY — BTC (shadow_signals)")
    print("=" * 60)

    rows = conn.execute("""
        SELECT direction, contract_price, clob_ask, confluence,
               time_left, gap, atr, reject_reason, resolved_direction
        FROM shadow_signals
        WHERE resolved_direction IS NOT NULL AND time_left IS NOT NULL
        ORDER BY id
    """).fetchall()

    stats = []
    for r in rows:
        d = dict(r)
        d['cp'] = d['contract_price'] or 0
        d['tl'] = d['time_left'] or 0
        d['result'] = 'WIN' if d['direction'] == d['resolved_direction'] else 'LOSS'
        stats.append(d)

    if not stats:
        print("  No shadow_signals with resolved_direction")
        return

    def show(label, subset):
        w = sum(1 for s in subset if s['result'] == 'WIN')
        l = sum(1 for s in subset if s['result'] == 'LOSS')
        if w + l == 0:
            return
        wr = w / (w + l) * 100
        avg_cp = sum(s['cp'] for s in subset) / len(subset)
        ev = wr / 100 * (1 - avg_cp) - (1 - wr / 100) * avg_cp
        per_day = len(subset) / 13  # ~13 days of shadow data
        print(f"  {label:50s} {w:4d}W/{l:3d}L  WR={wr:5.1f}%  avgCP={avg_cp:.3f}  EV={ev:+.3f}  ~{per_day:.0f}/day")

    print(f"\n  Total shadow signals with resolution: {len(stats)}\n")

    early = [s for s in stats if s['tl'] >= 10]
    show("ALL early (tl>=10)", early)

    print("\n  By contract price (early):")
    for cp_lo, cp_hi in [(0.40, 0.45), (0.45, 0.50), (0.50, 0.55), (0.55, 0.65)]:
        show(f"    cp {cp_lo:.2f}-{cp_hi:.2f}", [s for s in early if cp_lo <= s['cp'] < cp_hi])

    print("\n  By confluence (early):")
    for conf in [0, 1, 2, 3]:
        show(f"    conf>={conf}", [s for s in early if s['confluence'] >= conf])

    print("\n  Cheap + some filter (early):")
    show("cp<=0.50", [s for s in early if s['cp'] <= 0.50])
    show("cp<=0.50 + conf>=2", [s for s in early if s['cp'] <= 0.50 and s['confluence'] >= 2])
    show("cp<=0.50 + conf>=3", [s for s in early if s['cp'] <= 0.50 and s['confluence'] >= 3])
    show("cp<=0.50 + gap>=30", [s for s in early if s['cp'] <= 0.50 and abs(s['gap'] or 0) >= 30])
    show("cp<=0.50 + gap>=50", [s for s in early if s['cp'] <= 0.50 and abs(s['gap'] or 0) >= 50])


def main():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    analyze_alt_early_entry(conn)
    analyze_btc_early_entry(conn)

    conn.close()


if __name__ == "__main__":
    main()
