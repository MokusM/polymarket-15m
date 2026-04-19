"""
Sequential hedge pattern analyzer.

Simulates: detect one side dropping to trigger_level,
buy leg1 (cheap side), then wait N seconds to see if opposite side also drops.
If yes -> buy leg2 cheap -> guaranteed profit.
If no -> hold leg1 (directional risk).
"""

import sqlite3
import time
from pathlib import Path
from datetime import datetime
from collections import defaultdict

DB_PATH = Path(__file__).parent.parent / "clob_ticks.db"

# Parameters to test
TRIGGER_LEVELS = [0.30, 0.25, 0.20]  # leg1 entry: ask <= trigger
LEG2_TARGETS = [0.50, 0.45, 0.40]  # leg2 entry target
MAX_WAIT_SEC = [120, 180, 300]  # max wait for leg2

STAKE_SHARES = 10  # shares per leg


def analyze(trigger: float, leg2_target: float, max_wait: int):
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Group by market
    rows = conn.execute(
        "SELECT ts, market_id, asset, window, ask_up, ask_down, time_left_sec "
        "FROM ticks WHERE ask_up > 0 AND ask_down > 0 ORDER BY market_id, ts"
    ).fetchall()

    markets = defaultdict(list)
    for r in rows:
        markets[r["market_id"]].append(dict(r))

    opportunities = 0
    leg2_filled = 0
    total_pnl = 0
    leg1_only = 0  # cases where leg2 didn't fill

    seen_markets = set()

    for mid, ticks in markets.items():
        # Find first moment when ask_up <= trigger or ask_down <= trigger
        for i, t in enumerate(ticks):
            if mid in seen_markets:
                break

            # leg1: buy cheap side
            leg1_side = None
            leg1_price = None
            if t["ask_up"] <= trigger and t["ask_up"] > 0:
                leg1_side = "UP"
                leg1_price = t["ask_up"]
                opposite_ask_key = "ask_down"
            elif t["ask_down"] <= trigger and t["ask_down"] > 0:
                leg1_side = "DOWN"
                leg1_price = t["ask_down"]
                opposite_ask_key = "ask_up"
            else:
                continue

            seen_markets.add(mid)
            opportunities += 1
            leg1_ts = t["ts"]

            # Now wait up to max_wait for opposite side to reach leg2_target
            leg2_price = None
            for t2 in ticks[i+1:]:
                if t2["ts"] - leg1_ts > max_wait:
                    break
                opp_ask = t2[opposite_ask_key]
                if opp_ask > 0 and opp_ask <= leg2_target:
                    leg2_price = opp_ask
                    break

            if leg2_price:
                leg2_filled += 1
                cost = (leg1_price + leg2_price) * STAKE_SHARES
                payout = STAKE_SHARES * 1.00  # one side always wins
                pnl = payout - cost
                total_pnl += pnl
            else:
                leg1_only += 1
                # Assume leg1 wins 50% on average (directional coin flip)
                expected_pnl = (0.5 * STAKE_SHARES) - (leg1_price * STAKE_SHARES)
                total_pnl += expected_pnl

    conn.close()

    return {
        "trigger": trigger,
        "leg2_target": leg2_target,
        "max_wait": max_wait,
        "opportunities": opportunities,
        "leg2_filled": leg2_filled,
        "leg1_only": leg1_only,
        "fill_rate": leg2_filled / opportunities * 100 if opportunities > 0 else 0,
        "total_pnl": total_pnl,
        "avg_pnl_per_opp": total_pnl / opportunities if opportunities > 0 else 0,
    }


def main():
    print("=" * 90)
    print(f"Sequential hedge simulation on clob_ticks.db")
    print(f"Stake: {STAKE_SHARES} shares per leg")
    print("=" * 90)
    print(f"{'trigger':>8} {'target':>7} {'wait':>5}  {'opps':>5} {'leg2 fill':>10} {'fill%':>6}  {'PnL':>8} {'avg/opp':>8}")
    print("-" * 90)

    for trigger in TRIGGER_LEVELS:
        for target in LEG2_TARGETS:
            for wait in MAX_WAIT_SEC:
                r = analyze(trigger, target, wait)
                print(f"{r['trigger']:>8.2f} {r['leg2_target']:>7.2f} {r['max_wait']:>4}s  "
                      f"{r['opportunities']:>5d} {r['leg2_filled']:>10d} {r['fill_rate']:>5.1f}%  "
                      f"${r['total_pnl']:>+7.2f} ${r['avg_pnl_per_opp']:>+7.2f}")


if __name__ == "__main__":
    main()
