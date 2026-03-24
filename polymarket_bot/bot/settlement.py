import asyncio
import logging

from bot.config import STAKE_USD
from bot.polymarket_client import PolymarketClient
from bot.storage import get_unresolved_signals, update_result
from bot.telegram_bot import edit_signal_result

logger = logging.getLogger(__name__)

SETTLEMENT_INTERVAL_SECONDS = 60


async def settle_markets():
    """
    Фоновий процес: перевіряє сигнали з decision='approve' без result,
    розраховує PnL після закриття маркету і редагує оригінальне повідомлення в Telegram.
    """
    poly = PolymarketClient()
    logger.info("Запущено фоновий процес розрахунку (Settlement) для завершених маркетів.")

    while True:
        try:
            unresolved = get_unresolved_signals()
            for sig in unresolved:
                market_id = sig["market_id"]

                info = await poly.get_market_prices(market_id)
                if not info:
                    continue

                price_yes = info.get("price_yes", 0.0)
                price_no = info.get("price_no", 0.0)

                if price_yes in [0.0, 1.0] and price_no in [0.0, 1.0]:
                    contract_price = sig["contract_price"]
                    direction = sig["direction"]
                    raw_stake = sig.get("stake_usd")
                    stake = (
                        float(raw_stake)
                        if raw_stake is not None
                        else float(STAKE_USD)
                    )

                    shares = stake / contract_price if contract_price > 0 else 0

                    is_win = (
                        (direction == "UP" and price_yes == 1.0)
                        or (direction == "DOWN" and price_no == 1.0)
                    )

                    if is_win:
                        pnl = (shares * 1.0) - stake
                        result_str = "WIN"
                    else:
                        pnl = -stake
                        result_str = "LOSS"

                    update_result(sig["id"], result_str, pnl)
                    logger.info(
                        "Маркет #%s (Сигнал %s) закрито. %s, PnL: %.2f",
                        market_id, sig["id"], result_str, pnl,
                    )

                    tg_msg_id = sig.get("telegram_message_id")
                    alert_html = sig.get("alert_html") or ""
                    decision = sig.get("decision") or ""
                    if tg_msg_id:
                        await edit_signal_result(
                            tg_msg_id, alert_html, decision, result_str, pnl,
                        )

        except Exception as e:
            logger.error("Помилка в циклі settlement: %s", e, exc_info=True)

        await asyncio.sleep(SETTLEMENT_INTERVAL_SECONDS)

    await poly.close()
