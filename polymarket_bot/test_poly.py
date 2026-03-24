import httpx
import asyncio
import json

async def main():
    async with httpx.AsyncClient() as c:
        r = await c.get('https://gamma-api.polymarket.com/events?slug=btc-updown-15m-1774120500')
        print(json.dumps(r.json(), indent=2))

asyncio.run(main())
