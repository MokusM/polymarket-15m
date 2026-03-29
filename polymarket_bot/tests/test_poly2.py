import httpx
import asyncio
import json

async def main():
    async with httpx.AsyncClient() as c:
        r1 = await c.get('https://gamma-api.polymarket.com/events?series_slug=btc-up-or-down-15m')
        events = r1.json()
        print(f'By series_slug: {len(events)} events')

        r2 = await c.get('https://gamma-api.polymarket.com/events?slug=btc-up-or-down-15m')
        events2 = r2.json()
        print(f'By slug: {len(events2)} events')
        if len(events2) > 0:
             print(f'Found: {events2[0].get("title")}')
             markets = [m for m in events2[0].get("markets", []) if m.get("active") and not m.get("closed")]
             print(f'Active markets within event: {len(markets)}')

asyncio.run(main())
