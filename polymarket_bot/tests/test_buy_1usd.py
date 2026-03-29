"""
Тестова купівля з мінімальним ризиком на активному BTC 15m (реальний CLOB).

На біржі одночасно діють:
- мінімум notional ~$1;
- minimum_order_size у shares (для BTC 15m зазвичай 5) — див. CLOB /markets/{condition}.

Запуск: cd polymarket_bot && python test_buy_1usd.py
"""

import asyncio
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


def _shares_for_min_usd(price: float, usd: float = 1.0) -> float:
    if price <= 0:
        return 0.0
    s = math.ceil((usd / price) * 100) / 100.0
    while s * price < usd - 1e-6:
        s = round(s + 0.01, 2)
    return s


async def main():
    import httpx
    from bot.execution_client import ExecutionClient
    from bot.polymarket_client import PolymarketClient

    print("=" * 50)
    print("TEST BUY min notional + CLOB min shares (live)")
    print("=" * 50)

    ec = ExecutionClient()
    if not ec.ready:
        print("FAIL: ExecutionClient not ready:", getattr(ec, "not_ready_reason", "?"))
        return

    poly = PolymarketClient()
    markets = await poly.get_active_btc_markets()
    await poly.close()

    if not markets:
        print("No active BTC 15m market.")
        return

    m = markets[0]
    slug = m.get("market_slug") or ""
    mid = str(m.get("market_id", ""))
    print(f"Market: {m.get('title', m.get('question', ''))}")
    print(f"slug={slug} id={mid}")

    token_ids = await ec.get_market_token_ids(slug, mid or None)
    if not token_ids:
        print("FAIL: no clobTokenIds")
        return

    yes_token, no_token = token_ids
    neg_risk = False
    async with httpx.AsyncClient() as hc:
        url = f"https://gamma-api.polymarket.com/markets/slug/{slug}" if slug else None
        if url:
            r = await hc.get(url)
            if r.status_code == 200:
                neg_risk = bool(r.json().get("negRisk", False))
        else:
            r = await hc.get(f"https://gamma-api.polymarket.com/markets/{mid}")
            if r.status_code == 200:
                neg_risk = bool(r.json().get("negRisk", False))

    # Купуємо сторону з меншою ціною (менший notional при тих самих $1)
    py, pn = float(m.get("price_yes", 0.5)), float(m.get("price_no", 0.5))
    if py <= pn:
        side, token_id, price = "YES", yes_token, round(py, 2)
    else:
        side, token_id, price = "NO", no_token, round(pn, 2)

    if price < 0.01:
        price = 0.01

    min_sh = await ec.get_clob_minimum_order_size(slug, mid or None)
    shares = max(_shares_for_min_usd(price, 1.0), min_sh)
    notional = shares * price
    print(
        f"CLOB min shares: {min_sh} | BUY {side} @ {price:.2f} x {shares} "
        f"shares ~= ${notional:.2f}"
    )

    result = await ec.buy_shares(
        token_id=token_id,
        price=price,
        size=shares,
        neg_risk=neg_risk,
        market_slug=slug or None,
        market_id=mid or None,
    )
    print("Result:", json.dumps(result, default=str, ensure_ascii=False) if isinstance(result, dict) else result)
    print("=" * 50)


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
