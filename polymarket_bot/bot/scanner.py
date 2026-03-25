import asyncio
import logging
from datetime import datetime

import pandas as pd

from bot.config import (
    SCAN_INTERVAL_SECONDS,
    COOLDOWN_SECONDS,
    MAX_SIGNALS_PER_ROUND_PER_SIDE,
    REPEAT_ALERTS_AFTER_COOLDOWN,
    NOTIFY_SESSION_CHANGE,
    OBI_MIN_RATIO,
    OBI_LEVELS,
)
from bot.exchange_client import ExchangeClient
from bot.execution_client import get_order_book_imbalance
from bot.polymarket_client import PolymarketClient
from bot.indicators import add_indicators
from bot.signals import check_signals
from bot.state import state
from bot.storage import save_signal
from bot.telegram_bot import send_alert, send_info_message
from bot.alert_text import get_current_session_key, format_session_alert_html

logger = logging.getLogger(__name__)


def _parse_signal_key(key: str) -> tuple[str, str] | None:
    """Ключ виду '{market_id}_{UP|DOWN}'."""
    if "_" not in key:
        return None
    direction = key.rsplit("_", 1)[-1]
    if direction not in ("UP", "DOWN"):
        return None
    market_id = key[: -(len(direction) + 1)]
    return market_id, direction


class Scanner:
    def __init__(self):
        self.exchange = ExchangeClient()
        self.poly = PolymarketClient()
        self.last_signal_time = {}
        self.signal_counts = {}
        self._last_session_key: str | None = None

    async def run(self):
        logger.info("Пошук активних ринків BTC...")
        
        while True:
            try:
                # 1. Оновлюємо список подій Polymarket 
                markets_with_prices = await self.poly.get_active_btc_markets()
                
                active_ids = {str(m["market_id"]) for m in markets_with_prices}

                if len(markets_with_prices) == 0:
                    logger.info("❌ Активних 15-хвилинних BTC маркетів на даний момент немає або вони відфільтровані.")
                else:
                    logger.info(f"✅ В логіку індикаторів передано {len(markets_with_prices)} маркетів.")

                # Маркет випав зі списку (нове 15-хв вікно) — скидаємо кулдаун/лічильники для старого id
                for k in list(self.last_signal_time.keys()):
                    parsed = _parse_signal_key(k)
                    if parsed and parsed[0] not in active_ids:
                        self.last_signal_time.pop(k, None)
                        self.signal_counts.pop(k, None)

                # 2. Отримуємо свіжі свічки
                df = await self.exchange.get_btc_1m_candles(limit=100)
                
                if df.empty:
                    logger.warning("Не вдалось отримати свічки зі біржі. Чекаємо...")
                    await asyncio.sleep(SCAN_INTERVAL_SECONDS)
                    continue

                # 3. Рахуємо індикатори
                df_with_indicators = add_indicators(df)

                # 3.5 Сесійний алерт при зміні торгової сесії
                if NOTIFY_SESSION_CHANGE:
                    await self._check_session_change(df_with_indicators)

                # 4. Перевіряємо ринки
                for market_prices in markets_with_prices:
                    signal = check_signals(market_prices, df_with_indicators)
                    
                    if signal:
                        direction = signal["direction"]
                        market_id = str(market_prices["market_id"])

                        # ── OBI filter (skip in test mode) ──
                        token_id_for_obi = (
                            market_prices.get("token_yes_id")
                            if direction == "UP"
                            else market_prices.get("token_no_id")
                        )
                        obi = 1.0
                        if token_id_for_obi and state.mode != "test":
                            obi = await get_order_book_imbalance(
                                token_id_for_obi, OBI_LEVELS
                            )
                            if obi < OBI_MIN_RATIO:
                                logger.debug(
                                    "OBI %.3f < %.2f for %s %s — skip",
                                    obi, OBI_MIN_RATIO, direction, market_id,
                                )
                                continue
                        signal["obi"] = obi

                        key = f"{market_id}_{direction}"
                        now = datetime.now().timestamp()
                        last_time = self.last_signal_time.get(key, 0)
                        count = self.signal_counts.get(key, 0)

                        cooldown_ok = now - last_time >= COOLDOWN_SECONDS
                        if REPEAT_ALERTS_AFTER_COOLDOWN:
                            can_send = cooldown_ok
                        else:
                            can_send = cooldown_ok and (
                                count < MAX_SIGNALS_PER_ROUND_PER_SIDE
                            )

                        if can_send:
                            signal["market_id"] = market_id
                            signal["market_slug"] = market_prices.get("market_slug") or ""
                            signal["neg_risk"] = bool(
                                market_prices.get("neg_risk", False)
                            )
                            signal["market_title"] = (
                                market_prices.get("title")
                                or market_prices.get("question")
                                or ""
                            )

                            sig_id = save_signal(signal)
                            if sig_id:
                                asyncio.create_task(send_alert(sig_id, signal))
                                
                                self.last_signal_time[key] = now
                                self.signal_counts[key] = count + 1
                                logger.info(f"✅ Згенеровано сигнал #{sig_id}: {direction} для маркету {market_id}")

            except Exception as e:
                logger.error(f"Непередбачена помилка в циклі сканування: {e}", exc_info=True)

            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    async def _check_session_change(self, df: pd.DataFrame) -> None:
        current = get_current_session_key()
        if current == self._last_session_key:
            return
        self._last_session_key = current

        btc_price = atr = atr_zone = chg_1h = None
        if not df.empty:
            last = df.iloc[-1]
            btc_price = float(last.get("close", 0)) or None
            raw_atr = last.get("atr", None)
            if raw_atr is not None and not pd.isna(raw_atr):
                atr = float(raw_atr)
            atr_zone = last.get("atr_zone", None)
            raw_chg = last.get("chg_1h", None)
            if raw_chg is not None and not pd.isna(raw_chg):
                chg_1h = float(raw_chg)

        text = format_session_alert_html(current, btc_price, atr, atr_zone, chg_1h)
        asyncio.create_task(send_info_message(text))
        logger.info("Session change → %s", current)

    async def close(self):
        await self.exchange.close()
        await self.poly.close()
