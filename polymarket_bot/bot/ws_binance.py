"""
Binance WebSocket price feed — real-time BTC/ETH/SOL prices.

Підписується на kline_1m стріми, зберігає поточну ціну в пам'яті.
Position monitor використовує цю ціну для SL по BTC замість CLOB bid.
"""

import asyncio
import json
import logging
import time

import websockets

logger = logging.getLogger(__name__)

WS_URL = "wss://stream.binance.com:9443/ws"

# Поточні ціни — оновлюються кожні 1-2 сек
_prices: dict[str, float] = {}
_last_update: dict[str, float] = {}
_klines: dict[str, dict] = {}  # symbol → {open, high, low, close, volume}
_ws_task: asyncio.Task | None = None
_running = False


def get_price(symbol: str = "BTCUSDT") -> float | None:
    """Повернути поточну ціну. None якщо WS не підключений або дані старі (>30 сек)."""
    symbol = symbol.upper()
    price = _prices.get(symbol)
    if price is None:
        return None
    last = _last_update.get(symbol, 0)
    if time.time() - last > 30:
        logger.warning("WS price stale: %s last update %.0fs ago", symbol, time.time() - last)
        return None
    return price


def get_all_prices() -> dict[str, float]:
    """Повернути всі поточні ціни."""
    return dict(_prices)


def get_kline(symbol: str = "BTCUSDT") -> dict | None:
    """Повернути поточну kline {open, high, low, close, volume}. None якщо немає даних."""
    symbol = symbol.upper()
    kline = _klines.get(symbol)
    if kline is None:
        return None
    last = _last_update.get(symbol, 0)
    if time.time() - last > 30:
        return None
    return dict(kline)


def is_connected() -> bool:
    """Чи WS підключений і дані свіжі."""
    if not _running:
        return False
    btc_last = _last_update.get("BTCUSDT", 0)
    return time.time() - btc_last < 10


async def _ws_loop(symbols: list[str]):
    """Основний WS loop з reconnect."""
    global _running
    _running = True

    streams = [f"{s.lower()}@kline_1m" for s in symbols]
    url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"

    while _running:
        try:
            logger.info("WS Binance connecting: %s", streams)
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                logger.info("WS Binance connected")
                async for msg in ws:
                    if not _running:
                        break
                    try:
                        data = json.loads(msg)
                        stream = data.get("stream", "")
                        kline = data.get("data", {}).get("k", {})
                        if not kline:
                            continue

                        symbol = kline.get("s", "").upper()
                        close_price = float(kline.get("c", 0))

                        if symbol and close_price > 0:
                            _prices[symbol] = close_price
                            _last_update[symbol] = time.time()
                            _klines[symbol] = {
                                "open": float(kline.get("o", 0)),
                                "high": float(kline.get("h", 0)),
                                "low": float(kline.get("l", 0)),
                                "close": close_price,
                                "volume": float(kline.get("v", 0)),
                            }

                    except Exception as e:
                        logger.debug("WS parse error: %s", e)

        except asyncio.CancelledError:
            logger.info("WS Binance cancelled")
            break
        except Exception as e:
            logger.warning("WS Binance error: %s — reconnect in 3s", e)
            await asyncio.sleep(3)

    _running = False
    logger.info("WS Binance stopped")


def start(symbols: list[str] | None = None):
    """Запустити WS feed як asyncio task."""
    global _ws_task
    if _ws_task and not _ws_task.done():
        return _ws_task

    if symbols is None:
        symbols = ["btcusdt", "ethusdt", "solusdt"]

    _ws_task = asyncio.create_task(_ws_loop(symbols))
    return _ws_task


def stop():
    """Зупинити WS feed."""
    global _running
    _running = False
    if _ws_task and not _ws_task.done():
        _ws_task.cancel()
