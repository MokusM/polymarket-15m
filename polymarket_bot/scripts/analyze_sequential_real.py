"""
Real sequential hedge analysis — uses actual market outcomes.

For each market in clob_ticks.db:
1. Determine the winner from Gamma API (outcomePrices = ["1","0"] or ["0","1"])
2. For each "drop trigger" tick, simulate buying leg1 at that price
3. Check if leg2 filled within max_wait
4. Calculate REAL PnL based on actual resolution
"""

import asyncio
import sqlite3
import time
from pathlib import Path
from collections import defaultdict

import httpx

DB_PATH = Path(__file__).parent.parent / "clob_ticks.db"
GAMMA = "https://gamma-api.polymarket.com"

STAKE_SHARES = 10
TRIGGER_LEVELS = [0.30, 0.25, 0.20, 0.15, 0.10]
LEG2_TARGETS = [0.50, 0.40, 0.30]
MAX_WAIT_SEC = [60, 180, 300, 600]


async def fetch_outcomes(market_ids: list[str]) -> dict[str, int | None]:
    """
    Return {market_id: winner_side} where
      winner_side = 0 (UP won), 1 (DOWN won), None (not resolved)
    """
    results = {}
    async with httpx.AsyncClient(timeout=10) as client:
        sem = asyncio.Semaphore(10)

        async def fetch_one(mid: str):
            async with sem:
                try:
                    r = await client.get(f"{GAMMA}/markets/{mid}")
                    if r.status_code != 200:
                        return mid, None
                    d = r.json()
                    if not d.get("closed"):
                        return mid, None
                    prices = d.get("outcomePrices") or []
                    if isinstance(prices, str):
                        import json
                        try:
                            prices = json.loads(prices)
                        except Exception:
                            return mid, None
                    if len(prices) < 2:
                        return mid, None
                    try:
                        p0, p1 = float(prices[0]), float(prices[1])
                    except Exception:
                        return mid, None
                    if p0 == 1.0 and p1 == 0.0:
                        return mid, 0  # UP won
                    if p0 == 0.0 and p1 == 1.0:
                        return mid, 1  # DOWN won
                    return mid, None
                except Exception:
                    return mid, None

        tasks = [fetch_one(m) for m in market_ids]
        for coro in asyncio.as_completed(tasks):
            mid, winner = await coro
            results[mid] = winner
    return results


def simulate(trigger: float, leg2_target: float, max_wait: int, markets_data, outcomes):
    """Simulate strategy with real outcomes."""
    opps = 0
    leg2_filled = 0
    leg1_only = 0
    leg1_wins = 0
    leg1_losses = 0
    total_pnl = 0.0

    seen_markets = set()

    for mid, ticks in markets_data.items():
        winner = outcomes.get(mid)
        if winner is None:
            continue  # skip unresolved

        for i, t in enumerate(ticks):
            if mid in seen_markets:
                break

            ask_up = t["ask_up"]
            ask_down = t["ask_down"]
            if ask_up <= 0 or ask_down <= 0:
                continue

            # leg1: cheap side
            if ask_up <= trigger:
                leg1_side = 0  # UP
                leg1_price = ask_up
                opp_key = "ask_down"
            elif ask_down <= trigger:
                leg1_side = 1  # DOWN
                leg1_price = ask_down
                opp_key = "ask_up"
            else:
                continue

            seen_markets.add(mid)
            opps += 1
            leg1_ts = t["ts"]
            leg1_cost = leg1_price * STAKE_SHARES

            # Wait for leg2
            leg2_price = None
            for t2 in ticks[i+1:]:
                if t2["ts"] - leg1_ts > max_wait:
                    break
                opp = t2[opp_key]
                if 0 < opp <= leg2_target:
                    leg2_price = opp
                    break

            if leg2_price is not None:
                # Both legs filled — guaranteed: one of them wins $10
                leg2_filled += 1
                cost = leg1_cost + leg2_price * STAKE_SHARES
                payout = STAKE_SHARES * 1.00
                pnl = payout - cost
            else:
                # Only leg1 — check real outcome
                leg1_only += 1
                if winner == leg1_side:
                    leg1_wins += 1
                    pnl = STAKE_SHARES * 1.00 - leg1_cost
                else:
                    leg1_losses += 1
                    pnl = -leg1_cost
            total_pnl += pnl

    leg1_wr = leg1_wins / (leg1_wins + leg1_losses) * 100 if (leg1_wins + leg1_losses) > 0 else 0
    fill_rate = leg2_filled / opps * 100 if opps > 0 else 0
    avg_pnl = total_pnl / opps if opps > 0 else 0

    return {
        "trigger": trigger,
        "target": leg2_target,
        "max_wait": max_wait,
        "opps": opps,
        "leg2_filled": leg2_filled,
        "leg1_only": leg1_only,
        "leg1_wins": leg1_wins,
        "leg1_losses": leg1_losses,
        "fill_rate": fill_rate,
        "leg1_wr": leg1_wr,
        "total_pnl": total_pnl,
        "avg_pnl": avg_pnl,
    }


async def main():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Load all ticks grouped by market
    print("Loading ticks...")
    rows = conn.execute(
        "SELECT ts, market_id, asset, window, ask_up, ask_down "
        "FROM ticks WHERE ask_up > 0 AND ask_down > 0 ORDER BY market_id, ts"
    ).fetchall()

    markets_data = defaultdict(list)
    for r in rows:
        markets_data[r["market_id"]].append(dict(r))
    conn.close()

    market_ids = list(markets_data.keys())
    print(f"Markets: {len(market_ids)}, ticks: {sum(len(v) for v in markets_data.values())}")

    # Fetch outcomes for all markets
    print(f"Fetching outcomes from Gamma for {len(market_ids)} markets...")
    outcomes = await fetch_outcomes(market_ids)

    resolved = sum(1 for v in outcomes.values() if v is not None)
    up_wins = sum(1 for v in outcomes.values() if v == 0)
    down_wins = sum(1 for v in outcomes.values() if v == 1)
    print(f"Resolved: {resolved}/{len(market_ids)}  (UP={up_wins}, DOWN={down_wins})")
    print()

    # Run simulations
    print("=" * 110)
    print(f"Sequential hedge simulation — REAL outcomes | Stake: {STAKE_SHARES} shares/leg")
    print("=" * 110)
    print(f"{'trig':>5} {'target':>7} {'wait':>5}  {'opps':>5} {'fill':>5} {'fill%':>6} {'l1_only':>8} {'l1 W/L':>9} {'l1_WR%':>7}  {'PnL':>9} {'avg/opp':>8}")
    print("-" * 110)

    best = None
    for trigger in TRIGGER_LEVELS:
        for target in LEG2_TARGETS:
            if target <= trigger:
                continue
            for wait in MAX_WAIT_SEC:
                r = simulate(trigger, target, wait, markets_data, outcomes)
                print(
                    f"{r['trigger']:>5.2f} {r['target']:>7.2f} {r['max_wait']:>4}s  "
                    f"{r['opps']:>5d} {r['leg2_filled']:>5d} {r['fill_rate']:>5.1f}%  "
                    f"{r['leg1_only']:>8d} {r['leg1_wins']:>3d}/{r['leg1_losses']:<3d} "
                    f"{r['leg1_wr']:>6.1f}%  "
                    f"${r['total_pnl']:>+8.2f} ${r['avg_pnl']:>+7.2f}"
                )
                if best is None or r['avg_pnl'] > best['avg_pnl']:
                    best = r

    print()
    print("=" * 110)
    print("BEST COMBO:")
    print(f"  trigger={best['trigger']} target={best['target']} wait={best['max_wait']}s")
    print(f"  opps={best['opps']} leg2_fill={best['fill_rate']:.1f}% leg1_WR={best['leg1_wr']:.1f}%")
    print(f"  Total PnL: ${best['total_pnl']:+.2f}   Avg/opp: ${best['avg_pnl']:+.2f}")


if __name__ == "__main__":
    import os
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
