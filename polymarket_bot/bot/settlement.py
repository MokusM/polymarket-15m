import asyncio
import logging

from bot.config import STAKE_USD
from bot.polymarket_client import PolymarketClient
from bot.state import state
from bot.storage import (
    get_unresolved_signals,
    signal_live_position_already_closed,
    update_result,
)
from bot.telegram_bot import edit_signal_result, send_info_message

_NO_POSITION = "no_position"

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
                    if sig.get("live_entry_status") == _NO_POSITION:
                        update_result(sig["id"], "NO_ENTRY", 0.0)
                        logger.info(
                            "Сигнал %s: маркет закрито, позиція live не відкривалась — без paper PnL",
                            sig["id"],
                        )
                        tg_msg_id = sig.get("telegram_message_id")
                        alert_html = sig.get("alert_html") or ""
                        decision = sig.get("decision") or ""
                        if tg_msg_id:
                            await edit_signal_result(
                                tg_msg_id,
                                alert_html,
                                decision,
                                "NO_ENTRY",
                                0.0,
                                stake_usd=0,
                                contract_price=0,
                                direction=sig.get("direction") or "",
                            )
                        continue

                    if (
                        sig.get("live_entry_status") == "opened"
                        and signal_live_position_already_closed(sig["id"])
                    ):
                        update_result(sig["id"], "CLOSED_EARLY", 0.0)
                        logger.info(
                            "Сигнал %s: live позицію вже закрито монітором — без paper WIN/LOSS і без record_*",
                            sig["id"],
                        )
                        tg_msg_id = sig.get("telegram_message_id")
                        alert_html = sig.get("alert_html") or ""
                        decision = sig.get("decision") or ""
                        if tg_msg_id:
                            await edit_signal_result(
                                tg_msg_id,
                                alert_html,
                                decision,
                                "CLOSED_EARLY",
                                0.0,
                                stake_usd=0,
                                contract_price=0,
                                direction=sig.get("direction") or "",
                            )
                        continue

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

                    if pnl < 0:
                        state.record_loss()
                    else:
                        state.record_win()

                    tg_msg_id = sig.get("telegram_message_id")
                    alert_html = sig.get("alert_html") or ""
                    decision = sig.get("decision") or ""
                    if tg_msg_id:
                        await edit_signal_result(
                            tg_msg_id, alert_html, decision, result_str, pnl,
                            stake_usd=stake,
                            contract_price=contract_price,
                            direction=direction,
                        )

                    if state.circuit_breaker_active:
                        await send_info_message(
                            f"\U0001f6a8 <b>CIRCUIT BREAKER!</b>\n"
                            f"{state.consecutive_losses} losses підряд \u2014 "
                            f"live trading вимкнено.\n/reset щоб відновити."
                        )

        except Exception as e:
            logger.error("Помилка в циклі settlement: %s", e, exc_info=True)

        await asyncio.sleep(SETTLEMENT_INTERVAL_SECONDS)

    await poly.close()
