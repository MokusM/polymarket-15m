"""
Test: Polymarket CLOB API connection, balance, market token IDs, prices.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from bot.config import LIVE_TRADING, POLYMARKET_PRIVATE_KEY


async def main():
    print("=" * 50)
    print("POLYMARKET LIVE TRADING TEST")
    print("=" * 50)

    print(f"\nLIVE_TRADING = {LIVE_TRADING}")
    pk_display = "***" + POLYMARKET_PRIVATE_KEY[-8:] if POLYMARKET_PRIVATE_KEY else "NOT SET"
    print(f"PRIVATE_KEY  = {pk_display}")

    # 1. ExecutionClient
    print("\n--- 1. ExecutionClient init ---")
    from bot.execution_client import ExecutionClient
    client = ExecutionClient()
    print(f"Ready: {client.ready}")

    if not client.ready:
        print("FAILED: ExecutionClient not initialized.")
        return

    # 2. Balance
    print("\n--- 2. Balance ---")
    balance = await client.get_balance()
    print(f"USDC Balance: ${balance:.2f}")

    # 3. Active market
    print("\n--- 3. Active BTC 15m market ---")
    from bot.polymarket_client import PolymarketClient
    poly = PolymarketClient()
    markets = await poly.get_active_btc_markets()
    print(f"Active markets: {len(markets)}")

    if not markets:
        print("No active markets right now")
        await poly.close()
        return

    m = markets[0]
    print(f"  Market ID: {m['market_id']}")
    print(f"  Slug: {m.get('market_slug', 'N/A')}")
    print(f"  Title: {m['title']}")
    print(f"  Price YES: {m['price_yes']}")
    print(f"  Price NO: {m['price_no']}")

    slug = m.get("market_slug", "")

    # 4. Token IDs
    print(f"\n--- 4. Token IDs for: {slug} ---")
    token_ids = await client.get_market_token_ids(slug)
    if not token_ids:
        print("  FAILED: cannot get token IDs for this slug")
        print("  Trying with direct Gamma API clobTokenIds...")

        import httpx
        import json
        async with httpx.AsyncClient() as hc:
            r = await hc.get(f"https://gamma-api.polymarket.com/markets/slug/{slug}")
            if r.status_code == 200:
                data = r.json()
                raw = data.get("clobTokenIds")
                print(f"  Raw clobTokenIds: {raw}")
                if raw:
                    if isinstance(raw, str):
                        raw = json.loads(raw)
                    if isinstance(raw, list) and len(raw) >= 2:
                        token_ids = (raw[0], raw[1])
                        print(f"  Parsed OK!")

    if token_ids:
        yes_token, no_token = token_ids
        print(f"  YES token: {yes_token[:30]}...")
        print(f"  NO  token: {no_token[:30]}...")

        # 5. Prices
        print("\n--- 5. CLOB prices ---")
        yes_price = await client.get_token_price(yes_token, "BUY")
        no_price = await client.get_token_price(no_token, "BUY")
        print(f"  YES BUY price: {yes_price:.4f}")
        print(f"  NO  BUY price: {no_price:.4f}")

        print("\n[OK] CLOB API connected, market found, tokens resolved.")
        print(f"     Balance: ${balance:.2f}")
    else:
        print("  [FAIL] Could not resolve token IDs")

    await poly.close()
    print("\n" + "=" * 50)


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
