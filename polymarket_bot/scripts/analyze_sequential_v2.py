"""
Sequential hedge v2 — with market entry time + price stability filters.

Filters:
  1. time_in_market: enter only after N seconds from start (not immediately)
     e.g., for 15min market wait 2-5 min after opening
  2. stability: check that CLOB ask was in "sideways range" (both sides 0.35-0.65)
     for at least M minutes BEFORE the drop happened
  3. Once stable condition met, enter on next drop to trigger level
"""

import asyncio
import sqlite3
import json
from pathlib import Path
from collections import defaultdict

import httpx

DB_PATH = Path(__file__).parent.parent / "clob_ticks.db"
GAMMA = "https://gamma-api.polymarket.com"

STAKE_SHARES = 10

# Market age filter: enter only if time_elapsed in these ranges (seconds into market)
# For 5m (300s) and 15m (900s) markets separately
ENTRY_WINDOWS = {
    "5m":  [(60, 240)],    # 1-4 min into 5-min market
    "15m": [(120, 720)],   # 2-12 min into 15-min market
}

# Stability window — in SECONDS before drop
STABILITY_DURATION = 120  # 2 min
STABILITY_RANGE = (0.30, 0.70)  # both sides must be in this range

TRIGGERS = [0.30, 0.25, 0.20]
LEG2_TARGETS = [0.50, 0.45, 0.40]
MAX_WAIT = [180, 300, 600]


async def fetch_outcomes(market_ids):
    results = {}
    async with httpx.AsyncClient(timeout=10) as client:
        sem = asyncio.Semaphore(10)
        async def one(mid):
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
                    if p0 == 1.0:
                        return mid, 0
                    if p1 == 1.0:
                        return mid, 1
                    return mid, None
                except Exception:
                    return mid, None
        tasks = [one(m) for m in market_ids]
        for coro in asyncio.as_completed(tasks):
            mid, winner = await coro
            results[mid] = winner
    return results


def in_entry_window(time_left_sec: int, window_str: str) -> bool:
    """Check if we're in the allowed entry window based on market age."""
    total = 300 if window_str == "5m" else 900
    elapsed = total - time_left_sec
    ranges = ENTRY_WINDOWS.get(window_str, [])
    return any(lo <= elapsed <= hi for lo, hi in ranges)


def check_stability(ticks_before, duration_sec: int, range_lo: float, range_hi: float):
    """Both ask_up and ask_down must be in [range_lo, range_hi] for entire duration."""
    if not ticks_before:
        return False
    # ticks_before is the recent history ending at current time
    # Take only ticks within duration_sec window
    if len(ticks_before) < 2:
        return False
    newest_ts = ticks_before[-1]["ts"]
    window_start = newest_ts - duration_sec
    in_window = [t for t in ticks_before if t["ts"] >= window_start]
    if len(in_window) < 5:  # need some density
        return False
    for t in in_window:
        au, ad = t["ask_up"], t["ask_down"]
        if au <= 0 or ad <= 0:
            return False
        if not (range_lo <= au <= range_hi):
            return False
        if not (range_lo <= ad <= range_hi):
            return False
    return True


def simulate(trigger, target, max_wait, markets_data, outcomes, with_filters=True):
    opps = 0
    leg2_filled = 0
    leg1_wins = 0
    leg1_losses = 0
    leg1_only = 0
    total_pnl = 0.0

    seen = set()

    for mid, ticks in markets_data.items():
        winner = outcomes.get(mid)
        if winner is None:
            continue
        if mid in seen:
            continue
        window = ticks[0]["window"] if ticks else ""

        for i, t in enumerate(ticks):
            if mid in seen:
                break
            au, ad = t["ask_up"], t["ask_down"]
            if au <= 0 or ad <= 0:
                continue

            # leg1 trigger?
            if au <= trigger:
                leg1_side, leg1_price, opp_key = 0, au, "ask_down"
            elif ad <= trigger:
                leg1_side, leg1_price, opp_key = 1, ad, "ask_up"
            else:
                continue

            if with_filters:
                # Market age check
                if not in_entry_window(t["time_left_sec"], window):
                    continue
                # Stability check on ticks before drop
                before = ticks[:i]
                if not check_stability(before, STABILITY_DURATION, *STABILITY_RANGE):
                    continue

            seen.add(mid)
            opps += 1
            leg1_ts = t["ts"]
            leg1_cost = leg1_price * STAKE_SHARES

            # Wait for leg2
            leg2_price = None
            for t2 in ticks[i+1:]:
                if t2["ts"] - leg1_ts > max_wait:
                    break
                opp = t2[opp_key]
                if 0 < opp <= target:
                    leg2_price = opp
                    break

            if leg2_price is not None:
                leg2_filled += 1
                cost = leg1_cost + leg2_price * STAKE_SHARES
                pnl = STAKE_SHARES * 1.00 - cost
            else:
                leg1_only += 1
                if winner == leg1_side:
                    leg1_wins += 1
                    pnl = STAKE_SHARES * 1.00 - leg1_cost
                else:
                    leg1_losses += 1
                    pnl = -leg1_cost
            total_pnl += pnl

    return {
        "opps": opps, "fill": leg2_filled, "l1_only": leg1_only,
        "l1_w": leg1_wins, "l1_l": leg1_losses,
        "pnl": total_pnl,
        "avg": total_pnl / opps if opps > 0 else 0,
        "fill_rate": leg2_filled / opps * 100 if opps > 0 else 0,
        "l1_wr": leg1_wins / (leg1_wins + leg1_losses) * 100 if (leg1_wins + leg1_losses) > 0 else 0,
    }


async def main():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    print("Loading ticks...")
    rows = conn.execute(
        "SELECT ts, market_id, asset, window, ask_up, ask_down, time_left_sec "
        "FROM ticks WHERE ask_up > 0 AND ask_down > 0 ORDER BY market_id, ts"
    ).fetchall()
    markets = defaultdict(list)
    for r in rows:
        markets[r["market_id"]].append(dict(r))
    conn.close()

    print(f"Markets: {len(markets)}, ticks: {sum(len(v) for v in markets.values())}")

    print("Fetching outcomes...")
    outcomes = await fetch_outcomes(list(markets.keys()))
    resolved = sum(1 for v in outcomes.values() if v is not None)
    print(f"Resolved: {resolved}/{len(markets)}")
    print()

    print("=" * 120)
    print(f"v2: entry_window={ENTRY_WINDOWS}, stability={STABILITY_DURATION}s in {STABILITY_RANGE}")
    print("=" * 120)
    print(f"{'trig':>5} {'tgt':>5} {'wait':>5}  {'opps':>5} {'fill':>5} {'fill%':>6} {'l1only':>7} {'l1 W/L':>8} {'l1_WR':>7}  {'PnL':>9} {'avg':>7}")
    print("-" * 120)

    best = None
    for trig in TRIGGERS:
        for tgt in LEG2_TARGETS:
            if tgt <= trig:
                continue
            for wait in MAX_WAIT:
                r = simulate(trig, tgt, wait, markets, outcomes, with_filters=True)
                if r['opps'] == 0:
                    continue
                print(
                    f"{trig:>5.2f} {tgt:>5.2f} {wait:>4}s  "
                    f"{r['opps']:>5d} {r['fill']:>5d} {r['fill_rate']:>5.1f}%  "
                    f"{r['l1_only']:>7d} {r['l1_w']:>3d}/{r['l1_l']:<3d} "
                    f"{r['l1_wr']:>5.1f}%  "
                    f"${r['pnl']:>+8.2f} ${r['avg']:>+6.2f}"
                )
                if best is None or r['avg'] > best['avg']:
                    best = {**r, "trig": trig, "tgt": tgt, "wait": wait}

    print()
    if best and best["opps"] > 0:
        print("BEST:")
        print(f"  trig={best['trig']} tgt={best['tgt']} wait={best['wait']}s")
        print(f"  {best['opps']} opps, fill={best['fill_rate']:.1f}%, leg1_WR={best['l1_wr']:.1f}%")
        print(f"  Total PnL: ${best['pnl']:+.2f}, Avg: ${best['avg']:+.2f}")


if __name__ == "__main__":
    import os
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
