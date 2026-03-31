"""
WebSocket-based market settlement.

Підписується на CLOB market channel для token IDs активних сигналів.
При market_resolved — миттєво закриває позиції (замість polling кожні 60с).

Fallback polling у settlement.py залишається для:
- сигналів до перезапуску бота
- випадків коли WS недоступний
"""
import asyncio
import json
import logging

import aiohttp

logger = logging.getLogger(__name__)

WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WS_RECONNECT_DELAY = 5   # секунд між reconnect
WS_HEARTBEAT = 30        # aiohttp ping інтервал


class MarketWatcher:
    """
    Відстежує CLOB WS market channel.
    Зберігає mapping: token_id → (signal_id, direction, yes_token_id, no_token_id)
    """

    def __init__(self):
        # token_id → dict з даними сигналу (і YES і NO токен вказують на той самий сигнал)
        self._tokens: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._on_resolved_cb = None  # async callback(signal_info, winning_asset_id)

    def set_resolved_callback(self, cb):
        """Встановити async callback що викликається при market_resolved."""
        self._on_resolved_cb = cb

    async def watch(self, signal: dict):
        """Додати сигнал до списку спостереження."""
        try:
            payload = json.loads(signal.get("payload_json") or "{}")
        except Exception:
            payload = {}

        yes_id = payload.get("token_yes_id") or ""
        no_id = payload.get("token_no_id") or ""

        if not yes_id and not no_id:
            logger.warning("MarketWatcher: сигнал #%s без token_ids — пропущено", signal.get("id"))
            return

        info = {
            "signal_id": signal["id"],
            "direction": signal.get("direction", "UP"),
            "yes_token_id": yes_id,
            "no_token_id": no_id,
        }

        async with self._lock:
            if yes_id:
                self._tokens[yes_id] = info
            if no_id:
                self._tokens[no_id] = info

        # Якщо WS вже підключено — надіслати додаткову підписку
        if self._ws and not self._ws.closed:
            tokens = [t for t in [yes_id, no_id] if t]
            try:
                await self._ws.send_json({
                    "assets_ids": tokens,
                    "type": "market",
                    "custom_feature_enabled": True,
                })
            except Exception as e:
                logger.warning("MarketWatcher: не вдалось підписатись на %s: %s", tokens, e)

    async def unwatch(self, signal_id: int):
        """Прибрати сигнал зі списку після settlement."""
        async with self._lock:
            to_del = [k for k, v in self._tokens.items() if v["signal_id"] == signal_id]
            for k in to_del:
                del self._tokens[k]

    async def run(self):
        """Основний цикл: підключення, підписка, обробка подій, reconnect."""
        while True:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("MarketWatcher: WS розірвано (%s) — reconnect через %ds", e, WS_RECONNECT_DELAY)
            await asyncio.sleep(WS_RECONNECT_DELAY)

    async def _connect_and_listen(self):
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                WS_MARKET_URL,
                heartbeat=WS_HEARTBEAT,
                max_msg_size=0,
            ) as ws:
                self._ws = ws
                logger.info("MarketWatcher: WS підключено до %s", WS_MARKET_URL)

                # Підписуємось на всі токени що вже є у watched
                async with self._lock:
                    tokens = list(self._tokens.keys())

                if tokens:
                    await ws.send_json({
                        "assets_ids": tokens,
                        "type": "market",
                        "custom_feature_enabled": True,
                    })
                    logger.info("MarketWatcher: підписано на %d токенів", len(tokens))
                else:
                    logger.info("MarketWatcher: поки немає активних сигналів для підписки")

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_message(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.warning("MarketWatcher: WS error: %s", ws.exception())
                        break
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        break

                self._ws = None

    async def _handle_message(self, raw: str):
        try:
            data = json.loads(raw)
        except Exception:
            return

        # CLOB може надсилати або об'єкт або масив
        events = data if isinstance(data, list) else [data]

        for ev in events:
            if ev.get("event_type") == "market_resolved":
                await self._handle_resolved(ev)

    async def _handle_resolved(self, ev: dict):
        winning_asset_id = ev.get("winning_asset_id") or ""
        market_cond = ev.get("market") or ""

        logger.info(
            "MarketWatcher: market_resolved — winning_asset=%s market=%s",
            winning_asset_id[:16] if winning_asset_id else "?",
            market_cond[:16] if market_cond else "?",
        )

        if not winning_asset_id:
            return

        async with self._lock:
            info = self._tokens.get(winning_asset_id)
            if info is None:
                # Спробуємо знайти по другому токену того ж маркету
                for token, inf in self._tokens.items():
                    if inf["yes_token_id"] == winning_asset_id or inf["no_token_id"] == winning_asset_id:
                        info = inf
                        break

        if info is None:
            logger.debug("MarketWatcher: market_resolved для невідомого токена %s", winning_asset_id[:16])
            return

        if self._on_resolved_cb:
            try:
                await self._on_resolved_cb(info, winning_asset_id)
            except Exception as e:
                logger.error("MarketWatcher: помилка в resolved callback: %s", e, exc_info=True)


# Глобальний екземпляр
_watcher: MarketWatcher | None = None


def get_watcher() -> MarketWatcher:
    global _watcher
    if _watcher is None:
        _watcher = MarketWatcher()
    return _watcher
