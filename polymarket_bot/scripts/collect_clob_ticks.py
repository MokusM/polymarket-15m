"""
CLOB tick collector for H35 (spread arbitrage research).

Subscribes to Polymarket CLOB WebSocket for active 5m/15m markets
(BTC/ETH/SOL/XRP/DOGE) and stores every orderbook update to clob_ticks.db.

Runs independently — does NOT touch main bot.

Run:
    python scripts/collect_clob_ticks.py

DB schema:
    ticks(id, ts, market_id, asset, window, time_left_sec,
          token_up, token_down, ask_up, bid_up, ask_down, bid_down)
"""

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import httpx
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "clob_ticks.db"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA = "https://gamma-api.polymarket.com"

ET_TZ = "America/New_York"

# slug format: {asset_prefix}-updown-{window}-{unix}
ASSETS = {
    "BTC": "btc",
    "ETH": "eth",
    "SOL": "sol",
    "XRP": "xrp",
    "DOGE": "doge",
}
WINDOWS = {
    "5m": 5,   # 5-min windows, unix aligned to every :00, :05, :10...
    "15m": 15, # 15-min windows, unix aligned to every :00, :15, :30, :45
}
MARKET_REFRESH_SEC = 30  # re-scan active markets every 30s

_tracked: dict[str, dict] = {}  # token_id → market info
_conn: Optional[sqlite3.Connection] = None


def init_db():
    global _conn
    _conn = sqlite3.connect(str(DB_PATH))
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            market_id TEXT NOT NULL,
            asset TEXT,
            window TEXT,
            time_left_sec INTEGER,
            token_up TEXT,
            token_down TEXT,
            ask_up REAL,
            bid_up REAL,
            ask_down REAL,
            bid_down REAL,
            total_ask REAL
        )
    """)
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_ticks_market ON ticks(market_id, ts)")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_ticks_spread ON ticks(total_ask, ts)")
    _conn.commit()


def save_tick(market_id: str, asset: str, window: str, time_left: int,
              token_up: str, token_down: str,
              ask_up: float, bid_up: float, ask_down: float, bid_down: float):
    total = (ask_up or 0) + (ask_down or 0)
    try:
        _conn.execute(
            "INSERT INTO ticks (ts, market_id, asset, window, time_left_sec, "
            "token_up, token_down, ask_up, bid_up, ask_down, bid_down, total_ask) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), market_id, asset, window, time_left,
             token_up, token_down, ask_up, bid_up, ask_down, bid_down, total),
        )
        _conn.commit()
    except Exception as e:
        logger.error("save_tick error: %s", e)


def _candidate_unix_starts(window_min: int) -> list[int]:
    """Unix timestamps for current + next windows aligned to ET minutes."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo(ET_TZ)
    now = datetime.now(et)
    minute_floor = (now.minute // window_min) * window_min
    base = now.replace(minute=minute_floor, second=0, microsecond=0)
    # current window + next 3 windows
    return sorted({
        int((base + timedelta(minutes=window_min * i)).timestamp())
        for i in range(0, 4)
    })


async def find_active_markets(client: httpx.AsyncClient) -> list[dict]:
    """Fetch active 5m/15m markets by constructing slugs from unix window starts."""
    found = []
    for asset_name, asset_slug in ASSETS.items():
        for window_name, window_min in WINDOWS.items():
            for unix_start in _candidate_unix_starts(window_min):
                slug = f"{asset_slug}-updown-{window_name}-{unix_start}"
                try:
                    r = await client.get(f"{GAMMA}/markets/slug/{slug}", timeout=5)
                    if r.status_code != 200:
                        continue
                    m = r.json()
                    if m.get("closed"):
                        continue
                    tokens = m.get("clobTokenIds") or []
                    if isinstance(tokens, str):
                        try:
                            tokens = json.loads(tokens)
                        except Exception:
                            continue
                    if len(tokens) < 2:
                        continue
                    end_iso = m.get("endDate") or m.get("end_date_iso") or ""
                    try:
                        end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                        time_left = int((end_dt - datetime.now(timezone.utc)).total_seconds())
                    except Exception:
                        time_left = 0
                    if time_left <= 10:
                        continue
                    found.append({
                        "market_id": str(m.get("id", "")),
                        "asset": asset_name,
                        "window": window_name,
                        "token_up": str(tokens[0]),
                        "token_down": str(tokens[1]),
                        "time_left": time_left,
                        "end_iso": end_iso,
                        "slug": slug,
                    })
                except Exception:
                    pass
    return found


def _best_from_book(book_side: list) -> float:
    """Get best price from book side ([{price, size}, ...])."""
    if not book_side:
        return 0.0
    # CLOB returns asks sorted asc, bids desc — but safer to compute
    prices = []
    for lvl in book_side:
        try:
            p = float(lvl.get("price", 0))
            if p > 0:
                prices.append(p)
        except Exception:
            pass
    if not prices:
        return 0.0
    return min(prices)  # for asks we want min (caller will pass asks)


def _best_bid(book_side: list) -> float:
    prices = []
    for lvl in book_side:
        try:
            p = float(lvl.get("price", 0))
            if p > 0:
                prices.append(p)
        except Exception:
            pass
    return max(prices) if prices else 0.0


async def subscribe_and_collect():
    """WebSocket: subscribe to all tracked tokens, save every update."""
    while True:
        if not _tracked:
            await asyncio.sleep(3)
            continue

        asset_ids = list(_tracked.keys())
        sub_msg = {
            "type": "market",
            "assets_ids": asset_ids,
        }

        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps(sub_msg))
                logger.info("WS CLOB connected, tracking %d tokens", len(asset_ids))

                # Track current book state per token
                books: dict[str, dict] = {t: {"asks": [], "bids": []} for t in asset_ids}

                async for raw in ws:
                    try:
                        msgs = json.loads(raw)
                        if not isinstance(msgs, list):
                            msgs = [msgs]

                        # Tokens updated in this batch
                        touched = set()
                        for m in msgs:
                            event_type = m.get("event_type") or m.get("type")

                            # book snapshot — has asset_id at top level
                            if "asks" in m or "bids" in m:
                                asset_id = m.get("asset_id")
                                if asset_id and asset_id in books:
                                    books[asset_id]["asks"] = m.get("asks") or []
                                    books[asset_id]["bids"] = m.get("bids") or []
                                    touched.add(asset_id)
                                continue

                            # price_change — has price_changes[] with asset_id inside each
                            price_changes = m.get("price_changes") or m.get("changes") or []
                            if price_changes:
                                for ch in price_changes:
                                    ch_asset = ch.get("asset_id")
                                    if not ch_asset or ch_asset not in books:
                                        continue
                                    try:
                                        price = float(ch.get("price", 0))
                                        size = float(ch.get("size", 0))
                                    except Exception:
                                        continue
                                    side = (ch.get("side", "") or "").upper()
                                    key = "asks" if side == "SELL" else "bids"
                                    book_list = [
                                        x for x in books[ch_asset][key]
                                        if float(x.get("price", 0)) != price
                                    ]
                                    if size > 0:
                                        book_list.append({"price": str(price), "size": str(size)})
                                    books[ch_asset][key] = book_list
                                    touched.add(ch_asset)

                        # For each touched token, find paired market and save
                        for token_id in touched:
                            info = _tracked.get(token_id)
                            if not info:
                                continue
                            up_book = books.get(info["token_up"], {"asks": [], "bids": []})
                            down_book = books.get(info["token_down"], {"asks": [], "bids": []})
                            if not up_book["asks"] and not down_book["asks"]:
                                continue

                            ask_up = _best_from_book(up_book["asks"])
                            bid_up = _best_bid(up_book["bids"])
                            ask_down = _best_from_book(down_book["asks"])
                            bid_down = _best_bid(down_book["bids"])

                            # Recalculate time_left from end_iso
                            try:
                                end_dt = datetime.fromisoformat(info["end_iso"].replace("Z", "+00:00"))
                                time_left = int((end_dt - datetime.now(timezone.utc)).total_seconds())
                            except Exception:
                                time_left = 0

                            save_tick(
                                info["market_id"], info["asset"], info["window"], time_left,
                                info["token_up"], info["token_down"],
                                ask_up, bid_up, ask_down, bid_down,
                            )

                    except Exception as e:
                        logger.debug("WS parse: %s", e)

        except Exception as e:
            logger.warning("WS CLOB error: %s — reconnect in 3s", e)
            await asyncio.sleep(3)


async def market_refresh_loop():
    """Periodically refresh active markets and update _tracked dict."""
    async with httpx.AsyncClient() as client:
        while True:
            try:
                markets = await find_active_markets(client)
                new_tracked = {}
                for m in markets:
                    new_tracked[m["token_up"]] = m
                    new_tracked[m["token_down"]] = m

                added = set(new_tracked) - set(_tracked)
                removed = set(_tracked) - set(new_tracked)

                _tracked.clear()
                _tracked.update(new_tracked)

                if added or removed:
                    logger.info(
                        "Markets: %d tracked (+%d/-%d), markets=%d",
                        len(_tracked), len(added), len(removed), len(markets),
                    )
            except Exception as e:
                logger.error("market_refresh: %s", e)

            await asyncio.sleep(MARKET_REFRESH_SEC)


async def stats_loop():
    """Log stats every 60s: ticks count, active markets."""
    while True:
        await asyncio.sleep(60)
        try:
            row = _conn.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM ticks").fetchone()
            total, first, last = row
            spread_row = _conn.execute(
                "SELECT COUNT(*) FROM ticks WHERE total_ask < 0.95 AND ts > ?",
                (time.time() - 3600,),
            ).fetchone()
            logger.info(
                "Stats: total=%d ticks, last hour spread<0.95: %d, tracked=%d",
                total or 0, spread_row[0] or 0, len(_tracked) // 2,
            )
        except Exception as e:
            logger.error("stats: %s", e)


async def main():
    init_db()
    logger.info("CLOB tick collector starting — DB: %s", DB_PATH)

    tasks = [
        asyncio.create_task(market_refresh_loop()),
        asyncio.create_task(subscribe_and_collect()),
        asyncio.create_task(stats_loop()),
    ]

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("Stopped")
    finally:
        for t in tasks:
            t.cancel()
        if _conn:
            _conn.close()


if __name__ == "__main__":
    import os
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
