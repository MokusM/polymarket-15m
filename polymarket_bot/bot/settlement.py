import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from bot.config import STAKE_USD
from bot.polymarket_client import PolymarketClient
from bot.state import state
from bot.storage import (
    get_unresolved_signals,
    get_db_path,
    get_pending_order_by_signal,
    delete_pending_order,
    signal_live_position_already_closed,
    update_result,
)
from bot.position_manager import get_open_positions, close_position
from bot.telegram_bot import send_info_message

_NO_POSITION = "no_position"

logger = logging.getLogger(__name__)

SETTLEMENT_INTERVAL_SECONDS = 60


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


def _token_id_for_signal(sig: dict) -> str | None:
    """Повертає token_id для сигналу: YES-токен для UP, NO-токен для DOWN."""
    try:
        payload = json.loads(sig.get("payload_json") or "{}")
        direction = sig.get("direction") or payload.get("direction", "UP")
        if direction == "UP":
            return payload.get("token_yes_id") or payload.get("clob_token_id")
        else:
            return payload.get("token_no_id") or payload.get("clob_token_id")
    except Exception:
        return None


async def _find_buy_trade(execution_client, token_id: str, since_ts: float) -> dict | None:
    """
    Шукає BUY-трейд з asset_id == token_id після since_ts.
    Повертає перший знайдений трейд або None.
    """
    try:
        trades = await execution_client.get_recent_trades()
        for tr in trades:
            if tr.get("asset_id") != token_id:
                continue
            if tr.get("side", "").upper() != "BUY":
                continue
            match_ts = float(tr.get("match_time") or tr.get("timestamp") or 0)
            if match_ts >= since_ts:
                return tr
        return None
    except Exception as e:
        logger.warning("_find_buy_trade: %s", e)
        return None


async def settle_markets(execution_client=None):
    """
    Фоновий процес: перевіряє сигнали з decision='approve' без result,
    розраховує PnL після закриття маркету і надсилає повідомлення в Telegram.

    Якщо execution_client передано — верифікує реальний fill через Polymarket trades.
    """
    poly = PolymarketClient()
    logger.info("Запущено фоновий процес розрахунку (Settlement) для завершених маркетів.")

    try:
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

                if not (price_yes in [0.0, 1.0] and price_no in [0.0, 1.0]):
                    continue  # маркет ще не закрито

                live_status = sig.get("live_entry_status")
                direction = sig.get("direction") or "UP"
                side_label = "YES" if direction == "UP" else "NO"
                cp = float(sig.get("contract_price") or 0.5)

                # --- NO_POSITION: ніколи не було ордера ---
                if live_status == _NO_POSITION:
                    update_result(sig["id"], "NO_ENTRY", 0.0)
                    logger.info("Сигнал %s: no_position → NO_ENTRY", sig["id"])
                    await send_info_message(
                        f"💤 <b>Сигнал #{sig['id']} — без позиції</b>\n"
                        f"{direction} {side_label} @ {cp:.2f}\n"
                        f"CLOB ціна перевищила ліміт або ордер не виконано.\n"
                        f"<i>{_market_title(sig)}</i>"
                    )
                    continue

                # --- Позиція вже закрита монітором → беремо реальний PnL з positions ---
                if live_status == "opened" and signal_live_position_already_closed(sig["id"]):
                    try:
                        import sqlite3 as _sqlite3
                        _conn = _sqlite3.connect(get_db_path())
                        _row = _conn.execute(
                            "SELECT pnl FROM positions WHERE signal_id = ? AND status = 'closed' LIMIT 1",
                            (sig["id"],),
                        ).fetchone()
                        _conn.close()
                        real_pnl = float(_row[0] or 0) if _row else 0.0
                    except Exception:
                        real_pnl = 0.0

                    result_str = "WIN" if real_pnl > 0 else "LOSS"
                    update_result(sig["id"], result_str, real_pnl)
                    # state.record_win/loss вже викликано в close_position (position_manager)
                    logger.info("Сигнал %s: закрито монітором — %s, PnL: %.2f", sig["id"], result_str, real_pnl)
                    continue

                # --- Верифікація через Polymarket trades (якщо є execution_client) ---
                if execution_client and getattr(execution_client, "ready", False) and live_status in ("pending_fill", "opened"):
                    token_id = _token_id_for_signal(sig)

                    # Також перевіряємо pending_orders — там теж є token_id
                    po = None
                    if not token_id:
                        po = await asyncio.to_thread(get_pending_order_by_signal, sig["id"])
                        if po:
                            token_id = po.get("token_id")

                    if token_id:
                        # since_ts = час створення сигналу (точніше ніж 24h)
                        try:
                            sig_ts_str = sig.get("timestamp") or ""
                            sig_ts = datetime.strptime(sig_ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
                        except Exception:
                            sig_ts = time.time() - 3600
                        trade = await _find_buy_trade(execution_client, token_id, sig_ts)

                        if trade is None:
                            # Ордер не виконано — скасовуємо якщо ще є у стакані
                            if live_status == "pending_fill":
                                try:
                                    po = await asyncio.to_thread(get_pending_order_by_signal, sig["id"])
                                    if po:
                                        await execution_client.cancel_order(po["order_id"])
                                        await asyncio.to_thread(delete_pending_order, po["order_id"])
                                        logger.info("Сигнал %s: ліміт-ордер скасовано (маркет закрито)", sig["id"])
                                except Exception as _e:
                                    logger.warning("cancel pending order: %s", _e)

                            update_result(sig["id"], "NO_ENTRY", 0.0)
                            logger.info("Сигнал %s: Polymarket trade не знайдено → NO_ENTRY", sig["id"])
                            await send_info_message(
                                f"💤 <b>Сигнал #{sig['id']} — без позиції</b>\n"
                                f"{direction} {side_label} @ {cp:.2f}\n"
                                f"Ордер не виконано (ліміт не заповнений).\n"
                                f"<i>{_market_title(sig)}</i>"
                            )
                            continue

                        # Трейд знайдено — реальний PnL
                        real_price = float(trade.get("price") or cp)
                        real_shares = float(trade.get("size") or 0)
                        real_stake = round(real_price * real_shares, 2)
                        is_win = (direction == "UP" and price_yes == 1.0) or (direction == "DOWN" and price_no == 1.0)
                        pnl = round((real_shares * 1.0) - real_stake, 2) if is_win else round(-real_stake, 2)
                        result_str = "WIN" if is_win else "LOSS"

                        update_result(sig["id"], result_str, pnl)
                        for pos in get_open_positions():
                            if pos.get("signal_id") == sig["id"]:
                                close_position(pos["id"], f"settlement_{result_str}", pnl)
                                break

                        pnl_sign = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
                        payout = round(real_shares * 1.0, 2) if is_win else 0
                        result_icon = "✅" if is_win else "❌"
                        logger.info("Сигнал %s: Polymarket fill підтверджено — %s, PnL: %.2f", sig["id"], result_str, pnl)
                        await send_info_message(
                            f"{result_icon} <b>Сигнал #{sig['id']} — {result_str}</b>\n"
                            f"<i>{_market_title(sig)}</i>\n"
                            f"{direction} {side_label} @ {real_price:.2f} | "
                            f"${real_stake:.2f} → ${payout:.2f} ({real_shares:.1f} shares)\n"
                            f"PnL: <b>{pnl_sign} USD</b>"
                        )
                        if state.circuit_breaker_active:
                            await send_info_message(
                                f"🚨 <b>CIRCUIT BREAKER!</b>\n"
                                f"{state.consecutive_losses} losses підряд — "
                                f"live trading вимкнено.\n/reset щоб відновити."
                            )
                        continue

                # --- Fallback: paper PnL (немає execution_client або token_id не знайдено) ---
                raw_stake = sig.get("stake_usd")
                stake = float(raw_stake) if raw_stake is not None else float(STAKE_USD)
                shares = stake / cp if cp > 0 else 0
                is_win = (direction == "UP" and price_yes == 1.0) or (direction == "DOWN" and price_no == 1.0)
                pnl = round((shares * 1.0) - stake, 2) if is_win else round(-stake, 2)
                result_str = "WIN" if is_win else "LOSS"

                update_result(sig["id"], result_str, pnl)
                for pos in get_open_positions():
                    if pos.get("signal_id") == sig["id"]:
                        close_position(pos["id"], f"settlement_{result_str}", pnl)
                        break

                logger.info("Сигнал %s: paper settlement — %s, PnL: %.2f", sig["id"], result_str, pnl)
                pnl_sign = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
                payout = round(shares * 1.0, 2) if is_win else 0
                result_icon = "✅" if is_win else "❌"
                await send_info_message(
                    f"{result_icon} <b>Сигнал #{sig['id']} — {result_str}</b> (paper)\n"
                    f"<i>{_market_title(sig)}</i>\n"
                    f"{direction} {side_label} @ {cp:.2f} | "
                    f"${stake:.2f} → ${payout:.2f} ({shares:.1f} shares)\n"
                    f"PnL: <b>{pnl_sign} USD</b>"
                )
                if state.circuit_breaker_active:
                    await send_info_message(
                        f"🚨 <b>CIRCUIT BREAKER!</b>\n"
                        f"{state.consecutive_losses} losses підряд — "
                        f"live trading вимкнено.\n/reset щоб відновити."
                    )

        except Exception as e:
            logger.error("Помилка в циклі settlement: %s", e, exc_info=True)

        await asyncio.sleep(SETTLEMENT_INTERVAL_SECONDS)

    finally:
        await poly.close()
