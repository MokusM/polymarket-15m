"""
Position manager — трекінг відкритих позицій, partial exit, emulated stop-loss.

Позиції зберігаються в SQLite (таблиця positions).
Фоновий цикл моніторить ціну і тригерить SL / partial exit через ExecutionClient.
"""

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone

from bot.config import (
    BREAKEVEN_AFTER_ROI_PCT,
    POSITION_MONITOR_INTERVAL,
    POSITION_MONITOR_INTERVAL_ACTIVE,
    SL_PERCENT,
    TP_FULL_PRICE,
    TP_PARTIAL_PRICE,
    TP_PARTIAL_SELL_PCT,
)
from bot.storage import get_db_path

PENDING_POLL_INTERVAL = 2      # секунди між перевірками pending ордерів
PENDING_ORDER_EXPIRY_MIN = 16  # скасовуємо трекінг після N хвилин (ринок закрився)

logger = logging.getLogger(__name__)


def _pnl_partial_leg_usd(
    stake_usd: float,
    shares_init: float,
    sold: float,
    exit_price: float,
) -> float:
    """PnL від проданого шматка: виручка мінус пропорційна собівартість від початкової ставки."""
    if shares_init <= 0 or sold <= 0:
        return 0.0
    return exit_price * sold - stake_usd * (sold / shares_init)


def _pnl_total_on_full_close(
    stake_usd: float,
    shares_init: float,
    remaining: float,
    exit_price: float,
    realized_before: float,
) -> float:
    """Повний PnL при закритті всього залишку: уже зафіксоване з partial + нога залишку."""
    if shares_init <= 0:
        return float(realized_before or 0)
    cost_rem = stake_usd * (remaining / shares_init)
    proceeds = exit_price * remaining
    return float(realized_before or 0) + (proceeds - cost_rem)


def _get_conn(db_path: str | None = None):
    return sqlite3.connect(db_path if db_path is not None else get_db_path())


def _migrate_positions_columns(cursor: sqlite3.Cursor) -> None:
    cursor.execute("PRAGMA table_info(positions)")
    existing = {row[1] for row in cursor.fetchall()}
    if "realized_pnl" not in existing:
        cursor.execute(
            "ALTER TABLE positions ADD COLUMN realized_pnl REAL DEFAULT 0",
        )


def init_pending_orders_table(db_path: str | None = None):
    try:
        conn = _get_conn(db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT UNIQUE,
                signal_id INTEGER,
                market_id TEXT,
                market_slug TEXT,
                token_id TEXT,
                direction TEXT,
                side TEXT,
                limit_price REAL,
                expected_shares REAL,
                stake_usd REAL,
                neg_risk INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    except Exception as e:
        logger.error("Помилка init_pending_orders_table: %s", e)
    finally:
        conn.close()


def save_pending_order(
    order_id: str,
    signal_id: int,
    market_id: str,
    market_slug: str,
    token_id: str,
    direction: str,
    limit_price: float,
    expected_shares: float,
    stake_usd: float,
    neg_risk: bool = False,
) -> None:
    side = "YES" if direction == "UP" else "NO"
    try:
        conn = _get_conn()
        conn.execute(
            """
            INSERT OR IGNORE INTO pending_orders
                (order_id, signal_id, market_id, market_slug, token_id,
                 direction, side, limit_price, expected_shares, stake_usd, neg_risk)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (order_id, signal_id, market_id, market_slug, token_id,
             direction, side, limit_price, expected_shares, stake_usd, int(neg_risk)),
        )
        conn.commit()
        logger.info("Pending order saved: %s (signal #%s)", order_id[:16], signal_id)
    except Exception as e:
        logger.error("Помилка save_pending_order: %s", e)
    finally:
        conn.close()


def get_pending_orders() -> list[dict]:
    try:
        conn = _get_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM pending_orders").fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("Помилка get_pending_orders: %s", e)
        return []
    finally:
        conn.close()


def remove_pending_order(pending_id: int) -> None:
    try:
        conn = _get_conn()
        conn.execute("DELETE FROM pending_orders WHERE id = ?", (pending_id,))
        conn.commit()
    except Exception as e:
        logger.error("Помилка remove_pending_order: %s", e)
    finally:
        conn.close()


def init_positions_table(db_path: str | None = None):
    try:
        conn = _get_conn(db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                market_id TEXT,
                market_slug TEXT,
                token_id TEXT,
                direction TEXT,
                side TEXT,
                entry_price REAL,
                shares REAL,
                stake_usd REAL,
                remaining_shares REAL,
                status TEXT DEFAULT 'open',
                sl_price REAL,
                partial_exit_done INTEGER DEFAULT 0,
                order_result TEXT,
                pnl REAL,
                opened_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                closed_at DATETIME,
                close_reason TEXT
            )
            """
        )
        _migrate_positions_columns(cursor)
        conn.commit()
    except Exception as e:
        logger.error("Помилка створення таблиці positions: %s", e)
    finally:
        conn.close()


def open_position(
    signal_id: int,
    market_id: str,
    market_slug: str,
    token_id: str,
    direction: str,
    entry_price: float,
    shares: float,
    stake_usd: float,
    order_result: dict | None = None,
) -> int | None:
    """Зберегти нову відкриту позицію."""
    side = "YES" if direction == "UP" else "NO"

    sl_price = entry_price * (1 - SL_PERCENT / 100)

    order_json = json.dumps(order_result, default=str) if order_result else None

    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO positions (
                signal_id, market_id, market_slug, token_id, direction, side,
                entry_price, shares, stake_usd, remaining_shares,
                sl_price, order_result, realized_pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                signal_id, market_id, market_slug, token_id,
                direction, side, entry_price, shares, stake_usd,
                shares, sl_price, order_json,
            ),
        )
        conn.commit()
        pos_id = cur.lastrowid
        logger.info(
            "Position #%s OPEN: %s %s @ %.2f, %s shares, SL @ %.2f",
            pos_id, direction, side, entry_price, shares, sl_price,
        )
        return pos_id
    except Exception as e:
        logger.error("Помилка open_position: %s", e)
        return None
    finally:
        conn.close()


def get_open_positions() -> list[dict]:
    try:
        conn = _get_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM positions WHERE status = 'open'"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("Помилка get_open_positions: %s", e)
        return []
    finally:
        conn.close()


def count_open_positions() -> int:
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status = 'open'"
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0
    finally:
        conn.close()


def close_position(pos_id: int, reason: str, pnl: float = 0.0):
    from bot.state import state

    try:
        conn = _get_conn()
        conn.execute(
            """
            UPDATE positions
            SET status = 'closed', close_reason = ?, pnl = ?,
                closed_at = ?, remaining_shares = 0
            WHERE id = ?
            """,
            (reason, pnl, datetime.now(timezone.utc).isoformat(), pos_id),
        )
        conn.commit()
        logger.info("Position #%s CLOSED: %s, PnL: %.2f", pos_id, reason, pnl)
    except Exception as e:
        logger.error("Помилка close_position: %s", e)
    finally:
        conn.close()

    if pnl < 0:
        state.record_loss()
    else:
        state.record_win()


def update_sl_price(pos_id: int, new_sl: float):
    try:
        conn = _get_conn()
        conn.execute(
            "UPDATE positions SET sl_price = ? WHERE id = ?",
            (new_sl, pos_id),
        )
        conn.commit()
        logger.info("Position #%s: SL -> %.4f (breakeven)", pos_id, new_sl)
    except Exception as e:
        logger.error("Помилка update_sl_price: %s", e)
    finally:
        conn.close()


def update_partial_exit(
    pos_id: int,
    shares_sold: float,
    remaining: float,
    add_realized_pnl: float,
):
    try:
        conn = _get_conn()
        conn.execute(
            """
            UPDATE positions
            SET remaining_shares = ?, partial_exit_done = 1,
                realized_pnl = COALESCE(realized_pnl, 0) + ?
            WHERE id = ?
            """,
            (remaining, add_realized_pnl, pos_id),
        )
        conn.commit()
        logger.info(
            "Position #%s PARTIAL EXIT: sold %.2f, remaining %.2f, +realized %.2f",
            pos_id, shares_sold, remaining, add_realized_pnl,
        )
    except Exception as e:
        logger.error("Помилка update_partial_exit: %s", e)
    finally:
        conn.close()


async def monitor_positions_loop(execution_client):
    """
    Фоновий цикл: перевіряє ціни відкритих позицій.

    Логіка виходу по абсолютній ціні контракту:
      1. SL — ціна впала на SL_PERCENT% від entry → продати все
      2. TP_PARTIAL_PRICE (0.90) → продати TP_PARTIAL_SELL_PCT% shares
      3. TP_FULL_PRICE (0.95) → продати все що залишилось
    """
    from bot.state import state
    from bot.telegram_bot import send_info_message

    logger.info(
        "Position monitor запущено (інтервал %ss, TP: %.2f/%.2f)",
        POSITION_MONITOR_INTERVAL, TP_PARTIAL_PRICE, TP_FULL_PRICE,
    )

    while True:
        try:
            positions = get_open_positions()
            for pos in positions:
                if not pos.get("token_id"):
                    continue

                current_price = await execution_client.get_token_price(
                    pos["token_id"], "SELL",
                )
                if current_price <= 0:
                    continue

                entry = pos["entry_price"]
                remaining = pos["remaining_shares"]
                pos_id = pos["id"]
                shares_init = float(pos["shares"] or 0)
                stake_u = float(pos["stake_usd"] or 0)
                realized_accum = float(pos.get("realized_pnl") or 0)
                sl = float(pos.get("sl_price") or 0)

                # ── Після +X% нереалізованого прибутку (від ставки на залишок) — SL на вхід ──
                if (
                    BREAKEVEN_AFTER_ROI_PCT > 0
                    and shares_init > 0
                    and remaining > 0
                    and sl + 1e-9 < entry
                ):
                    alloc_stake = stake_u * (remaining / shares_init)
                    unreal = (current_price - entry) * remaining
                    if alloc_stake > 1e-9:
                        roi_frac = unreal / alloc_stake
                        if roi_frac >= BREAKEVEN_AFTER_ROI_PCT / 100.0:
                            update_sl_price(pos_id, entry)
                            await send_info_message(
                                f"\U0001f504 <b>SL \u2192 беззбиток #{pos_id}</b>\n"
                                f"{pos['direction']} {pos['side']} | "
                                f"~{BREAKEVEN_AFTER_ROI_PCT:.0f}%+ від ставки на залишок, "
                                f"SL @ {entry:.2f}"
                            )
                            sl = entry

                # ── Stop-loss ──
                if sl > 0 and current_price <= sl:
                    logger.warning(
                        "SL TRIGGERED #%s: %.2f <= %.2f", pos_id, current_price, sl,
                    )
                    sell_result = await execution_client.sell_shares(
                        pos["token_id"], current_price, remaining,
                    )
                    if not sell_result or sell_result.get("success") is False:
                        logger.error("SL SELL failed #%s: %s — позиція залишається відкритою", pos_id, sell_result)
                        continue
                    pnl = _pnl_total_on_full_close(
                        stake_u, shares_init, remaining, current_price, realized_accum,
                    )
                    close_position(pos_id, "stop_loss", pnl)

                    sl_text = (
                        f"\U0001f6d1 <b>Stop-Loss #{pos_id}</b>\n"
                        f"{pos['direction']} {pos['side']} | "
                        f"Entry: {entry:.2f} \u2192 Exit: {current_price:.2f}\n"
                        f"PnL: <b>{pnl:+.2f} USD</b>"
                    )
                    if state.circuit_breaker_active:
                        sl_text += (
                            f"\n\n\U0001f6a8 <b>CIRCUIT BREAKER!</b>\n"
                            f"{state.consecutive_losses} losses підряд \u2014 "
                            f"live trading вимкнено.\n/reset щоб відновити."
                        )
                    await send_info_message(sl_text)
                    continue

                # ── Take-Profit FULL @ 0.95 ──
                if current_price >= TP_FULL_PRICE:
                    logger.info(
                        "TP FULL #%s: price %.2f >= %.2f",
                        pos_id, current_price, TP_FULL_PRICE,
                    )
                    sell_result = await execution_client.sell_shares(
                        pos["token_id"], current_price, remaining,
                    )
                    if not sell_result or sell_result.get("success") is False:
                        logger.error("TP FULL SELL failed #%s: %s — позиція залишається відкритою", pos_id, sell_result)
                        continue
                    pnl = _pnl_total_on_full_close(
                        stake_u, shares_init, remaining, current_price, realized_accum,
                    )
                    close_position(pos_id, "tp_full", pnl)

                    await send_info_message(
                        f"\U0001f3af <b>Full Exit #{pos_id} @ {current_price:.2f}</b>\n"
                        f"{pos['direction']} {pos['side']} | "
                        f"Entry: {entry:.2f} \u2192 {current_price:.2f}\n"
                        f"Sold {remaining:.0f} shares\n"
                        f"PnL: <b>{pnl:+.2f} USD</b>"
                    )
                    continue

                # ── Take-Profit PARTIAL @ 0.90 ──
                if not pos.get("partial_exit_done") and current_price >= TP_PARTIAL_PRICE:
                    sell_amount = remaining * (TP_PARTIAL_SELL_PCT / 100)
                    if sell_amount >= 1:
                        logger.info(
                            "TP PARTIAL #%s: price %.2f >= %.2f, selling %.1f",
                            pos_id, current_price, TP_PARTIAL_PRICE, sell_amount,
                        )
                        sell_result = await execution_client.sell_shares(
                            pos["token_id"], current_price, sell_amount,
                        )
                        if not sell_result or sell_result.get("success") is False:
                            logger.error("TP PARTIAL SELL failed #%s: %s — позиція залишається відкритою", pos_id, sell_result)
                            continue
                        new_remaining = remaining - sell_amount
                        leg_pnl = _pnl_partial_leg_usd(
                            stake_u, shares_init, sell_amount, current_price,
                        )
                        update_partial_exit(pos_id, sell_amount, new_remaining, leg_pnl)

                        await send_info_message(
                            f"\U0001f4b0 <b>Partial TP #{pos_id} @ {current_price:.2f}</b>\n"
                            f"Sold {sell_amount:.0f} / {remaining:.0f} shares\n"
                            f"Locked: <b>{leg_pnl:+.2f} USD</b>\n"
                            f"Remaining: {new_remaining:.0f} shares \u2192 "
                            f"full exit @ {TP_FULL_PRICE:.2f}"
                        )

        except Exception as e:
            logger.error("Помилка monitor_positions: %s", e, exc_info=True)

        sleep_sec = POSITION_MONITOR_INTERVAL_ACTIVE if count_open_positions() > 0 else POSITION_MONITOR_INTERVAL
        await asyncio.sleep(sleep_sec)


async def poll_pending_orders_loop(execution_client):
    """
    Поллінг pending ордерів кожні 2с.
    Статус MATCHED → open_position(); CANCELLED/expired → no_position.
    """
    from datetime import datetime, timezone
    from bot.state import state
    from bot.storage import mark_signal_live_no_position, update_decision, update_signal_live_fill
    from bot.telegram_bot import send_info_message

    logger.info("Pending orders poller запущено (інтервал %ss)", PENDING_POLL_INTERVAL)

    while True:
        try:
            pending = get_pending_orders()
            for po in pending:
                order_id = po["order_id"]

                # Перевірка віку — ринок міг закритися
                try:
                    created = datetime.fromisoformat(po["created_at"])
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    age_min = (datetime.now(timezone.utc) - created).total_seconds() / 60
                except Exception:
                    age_min = 0

                if age_min > PENDING_ORDER_EXPIRY_MIN:
                    remove_pending_order(po["id"])
                    mark_signal_live_no_position(po["signal_id"])
                    update_decision(po["signal_id"], "approve")
                    logger.info("Pending order %s expired after %.1f min", order_id[:16], age_min)
                    await send_info_message(
                        f"⏰ <b>Ліміт не заповнився</b> (ринок закрився)\n"
                        f"orderID: <code>{order_id[:20]}</code>"
                    )
                    continue

                # Запит статусу ордера з CLOB
                order_data = await execution_client.get_order_status(order_id)
                if not order_data:
                    continue

                status = (order_data.get("status") or "").upper()

                if status == "MATCHED":
                    fill_price = float(order_data.get("price", po["limit_price"]))
                    size_matched = float(order_data.get("size_matched", po["expected_shares"]))
                    if size_matched <= 0:
                        size_matched = po["expected_shares"]
                    stake_eff = round(size_matched * fill_price, 2)

                    pos_id = open_position(
                        signal_id=po["signal_id"],
                        market_id=po["market_id"],
                        market_slug=po["market_slug"],
                        token_id=po["token_id"],
                        direction=po["direction"],
                        entry_price=fill_price,
                        shares=size_matched,
                        stake_usd=stake_eff,
                    )
                    remove_pending_order(po["id"])
                    update_signal_live_fill(po["signal_id"], stake_eff, fill_price)

                    logger.info(
                        "Pending %s FILLED → Position #%s @ %.2f x %.2f shares",
                        order_id[:16], pos_id, fill_price, size_matched,
                    )
                    await send_info_message(
                        f"🚀 <b>Ордер виконано → Позиція #{pos_id}</b>\n"
                        f"{po['direction']} {po['side']} @ {fill_price:.2f} | "
                        f"{size_matched:.1f} shares | ~${stake_eff:.2f}\n"
                        f"SL/TP моніторинг активовано."
                    )

                elif status == "CANCELLED":
                    remove_pending_order(po["id"])
                    mark_signal_live_no_position(po["signal_id"])
                    update_decision(po["signal_id"], "approve")
                    logger.info("Pending order %s CANCELLED", order_id[:16])
                    await send_info_message(
                        f"❌ <b>Ліміт скасовано</b>\n"
                        f"orderID: <code>{order_id[:20]}</code>"
                    )

        except Exception as e:
            logger.error("poll_pending_orders: %s", e, exc_info=True)

        await asyncio.sleep(PENDING_POLL_INTERVAL)
