import asyncio
import json
import logging

from bot.config import STAKE_USD
from bot.polymarket_client import PolymarketClient
from bot.state import state
from bot.storage import (
    get_unresolved_signals,
    signal_live_position_already_closed,
    update_result,
)
from bot.telegram_bot import send_info_message

_NO_POSITION = "no_position"


def _market_title(sig: dict) -> str:
    """Витягує назву маркету з payload_json, fallback — market_id."""
    try:
        payload = json.loads(sig.get("payload_json") or "{}")
        title = payload.get("market_title") or ""
        if title:
            return title
    except Exception:
        pass
    return sig.get("market_id", "")[:24]

logger = logging.getLogger(__name__)

SETTLEMENT_INTERVAL_SECONDS = 60


async def settle_markets():
    """
    Фоновий процес: перевіряє сигнали з decision='approve' без result,
    розраховує PnL після закриття маркету і надсилає інформаційне повідомлення в Telegram.
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
                        if tg_msg_id:
                            await send_info_message(
                                f"💤 <b>Сигнал #{sig['id']} — без позиції</b>\n"
                                f"Live-ордер не дав fill або скасовано.\n"
                                f"<i>{_market_title(sig)}</i>"
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
                        if tg_msg_id:
                            await send_info_message(
                                f"📋 <b>Сигнал #{sig['id']} — закрито монітором</b>\n"
                                f"PnL вже відображено в повідомленнях SL/TP.\n"
                                f"<i>{_market_title(sig)}</i>"
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

                    result_icon = "✅" if result_str == "WIN" else "❌"
                    pnl_sign = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
                    side = "YES" if direction == "UP" else "NO"
                    payout = shares * 1.0 if result_str == "WIN" else 0
                    await send_info_message(
                        f"{result_icon} <b>Сигнал #{sig['id']} — {result_str}</b>\n"
                        f"<i>{_market_title(sig)}</i>\n"
                        f"{direction} {side} @ {contract_price:.2f} | "
                        f"${stake:.2f} → ${payout:.2f} ({shares:.1f} shares)\n"
                        f"PnL: <b>{pnl_sign} USD</b>"
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
