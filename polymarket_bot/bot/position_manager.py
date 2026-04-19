"""
Position manager — трекінг відкритих позицій, partial exit, emulated stop-loss.

Позиції зберігаються в SQLite (таблиця positions).
Фоновий цикл моніторить ціну і тригерить SL / partial exit через ExecutionClient.
"""

import asyncio
import json
import logging
import math
import sqlite3
from datetime import datetime, timezone

import httpx as _httpx

from bot.config import (
    BREAKEVEN_AFTER_ROI_PCT,
    POSITION_MONITOR_INTERVAL,
    SL_PERCENT,
    TP_FINAL_PRICE,
    TP_FULL_PRICE,
    TP_MID_PRICE,
    TP_PARTIAL_PRICE,
    TP_PARTIAL_SELL_PCT,
    TRAILING_BE_TRIGGER,
    TRAILING_BE_SL_PCT,
)
from bot.storage import get_db_path

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
    conn = sqlite3.connect(db_path if db_path is not None else get_db_path())
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _migrate_positions_columns(cursor: sqlite3.Cursor) -> None:
    cursor.execute("PRAGMA table_info(positions)")
    existing = {row[1] for row in cursor.fetchall()}
    if "realized_pnl" not in existing:
        cursor.execute(
            "ALTER TABLE positions ADD COLUMN realized_pnl REAL DEFAULT 0",
        )
    if "price_low" not in existing:
        cursor.execute("ALTER TABLE positions ADD COLUMN price_low REAL")
    if "price_high" not in existing:
        cursor.execute("ALTER TABLE positions ADD COLUMN price_high REAL")
    if "market_expires_at" not in existing:
        cursor.execute(
            "ALTER TABLE positions ADD COLUMN market_expires_at TEXT",
        )
    if "btc_strike" not in existing:
        cursor.execute(
            "ALTER TABLE positions ADD COLUMN btc_strike REAL",
        )
    if "strategy_id" not in existing:
        cursor.execute(
            "ALTER TABLE positions ADD COLUMN strategy_id TEXT",
        )


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
    market_expires_at: str | None = None,
    btc_strike: float | None = None,
    strategy_id: str | None = None,
) -> int | None:
    """Зберегти нову відкриту позицію."""
    side = "YES" if direction == "UP" else "NO"

    sl_price = entry_price * (1 - SL_PERCENT / 100) if SL_PERCENT > 0 else 0.0

    order_json = json.dumps(order_result, default=str) if order_result else None

    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO positions (
                signal_id, market_id, market_slug, token_id, direction, side,
                entry_price, shares, stake_usd, remaining_shares,
                sl_price, order_result, realized_pnl, market_expires_at, btc_strike, strategy_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                signal_id, market_id, market_slug, token_id,
                direction, side, entry_price, shares, stake_usd,
                shares, sl_price, order_json, market_expires_at, btc_strike, strategy_id,
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


def get_open_positions(db_path: str | None = None) -> list[dict]:
    try:
        conn = _get_conn(db_path)
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


def get_open_positions_from_db(db_path: str) -> list[dict]:
    """Get open positions from a specific DB file."""
    return get_open_positions(db_path)


def count_open_positions(strategy_id: str | None = None) -> int:
    try:
        conn = _get_conn()
        if strategy_id:
            row = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE status = 'open' AND strategy_id = ?",
                (strategy_id,),
            ).fetchone()
        else:
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
        if pnl < 0:
            state.record_loss()
        else:
            state.record_win()
    except Exception as e:
        logger.error("Помилка close_position: %s", e)
    finally:
        conn.close()


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


def update_price_extremes(pos_id: int, price: float):
    try:
        conn = _get_conn()
        conn.execute(
            """
            UPDATE positions SET
                price_low  = MIN(COALESCE(price_low,  ?), ?),
                price_high = MAX(COALESCE(price_high, ?), ?)
            WHERE id = ?
            """,
            (price, price, price, price, pos_id),
        )
        conn.commit()
    except Exception as e:
        logger.error("Помилка update_price_extremes: %s", e)
    finally:
        conn.close()


def update_partial_exit(
    pos_id: int,
    shares_sold: float,
    remaining: float,
    add_realized_pnl: float,
    level: int = 1,
):
    try:
        conn = _get_conn()
        conn.execute(
            """
            UPDATE positions
            SET remaining_shares = ?, partial_exit_done = ?,
                realized_pnl = COALESCE(realized_pnl, 0) + ?
            WHERE id = ?
            """,
            (remaining, level, add_realized_pnl, pos_id),
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


async def monitor_positions_loop(execution_client, execution_clients: dict | None = None):
    """
    Фоновий цикл: перевіряє ціни відкритих позицій.

    Логіка виходу по абсолютній ціні контракту:
      1. SL — ціна впала на SL_PERCENT% від entry → продати все
      2. TP_PARTIAL_PRICE (0.90) → продати TP_PARTIAL_SELL_PCT% shares
      3. TP_FULL_PRICE (0.95) → продати все що залишилось
    """
    from bot.state import state
    from bot.telegram_bot import send_info_message as _send_info_message

    logger.info(
        "Position monitor запущено (інтервал %ss, TP: %.2f/%.2f/%.2f/%.2f)",
        POSITION_MONITOR_INTERVAL, TP_PARTIAL_PRICE, TP_MID_PRICE, TP_FULL_PRICE, TP_FINAL_PRICE,
    )

    def _get_client_for_position(pos: dict):
        """Resolve the correct ExecutionClient for a position's strategy."""
        if not execution_clients:
            return execution_client
        sid = pos.get("strategy_id") or ""
        if sid:
            from bot.strategies import get_strategy
            strat = get_strategy(sid)
            if strat:
                wk = strat.get("wallet_key", "POLYMARKET_PRIVATE_KEY")
                client = execution_clients.get(wk)
                if client and client.ready:
                    return client
        return execution_client

    while True:
        try:
            positions = get_open_positions()
            for pos in positions:
                if not pos.get("token_id"):
                    continue
                pos_client = _get_client_for_position(pos)
                _pos_sid = pos.get("strategy_id")

                async def send_info_message(text: str, _sid=_pos_sid):
                    return await _send_info_message(text, strategy_id=_sid)
                try:
                    # Використовуємо CLOB REST bid — реальна ціна продажу (те що ми отримаємо)
                    try:
                        async with _httpx.AsyncClient(timeout=3.0) as _c:
                            _r = await _c.get(
                                "https://clob.polymarket.com/book",
                                params={"token_id": pos["token_id"]},
                            )
                            if _r.status_code == 200:
                                _bids = _r.json().get("bids") or []
                                _bids = sorted(_bids, key=lambda x: float(x.get("price", 0)), reverse=True)
                                current_price = float(_bids[0]["price"]) if _bids else 0.0
                            else:
                                current_price = None
                    except Exception:
                        current_price = await pos_client.get_token_price(
                            pos["token_id"], "SELL",
                        )
                    if current_price is None or current_price <= 0:
                        continue  # API помилка або порожній book — пропускаємо, не закриваємо
                    logger.debug("Position #%s current bid: %.4f", pos.get("id"), current_price)

                    update_price_extremes(pos_id=pos["id"], price=current_price)

                    entry = pos["entry_price"]
                    remaining = float(pos["remaining_shares"] or 0)
                    pos_id = pos["id"]
                    if remaining <= 0:
                        continue
                    shares_init = float(pos["shares"] or 0)
                    stake_u = float(pos["stake_usd"] or 0)
                    realized_accum = float(pos.get("realized_pnl") or 0)
                    sl = float(pos.get("sl_price") or 0)
                    partial_level = int(pos.get("partial_exit_done") or 0)

                    # ── Time-based exit: ВИМКНЕНО — settlement закриє по 1.00 ──
                    # Дані показали що time exit коштує -$30 на 54 угодах (всі WIN)
                    expires_str = pos.get("market_expires_at") or ""
                    if False and expires_str and current_price >= 0.95 and remaining > 0:
                        try:
                            exp = datetime.fromisoformat(expires_str).replace(tzinfo=timezone.utc)
                            secs_left = (exp - datetime.now(timezone.utc)).total_seconds()
                            if secs_left < 180:
                                logger.info(
                                    "TIME EXIT #%s: price %.2f >= %.2f, %.0fs left — sell all",
                                    pos_id, current_price, TP_FULL_PRICE, secs_left,
                                )
                                sell_result = await pos_client.sell_shares(
                                    pos["token_id"], current_price, remaining,
                                )
                                if sell_result and sell_result.get("success") is not False:
                                    _te_fill = current_price
                                    try:
                                        _te_avg = sell_result.get("average_price") or sell_result.get("price")
                                        if _te_avg: _te_fill = float(_te_avg)
                                    except Exception: pass
                                    pnl = _pnl_total_on_full_close(
                                        stake_u, shares_init, remaining, _te_fill, realized_accum,
                                    )
                                    close_position(pos_id, "time_exit", pnl)
                                    await send_info_message(
                                        f"⏰ <b>Time Exit #{pos_id} @ {_te_fill:.2f}</b>\n"
                                        f"{pos['direction']} {pos['side']} | "
                                        f"{remaining:.0f} shares | {secs_left:.0f}с до закриття\n"
                                        f"PnL: <b>{pnl:+.2f} USD</b>"
                                    )
                                    continue
                        except Exception:
                            pass

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

                    # ── Trailing breakeven: ціна досягла TRIGGER → SL на фіксований рівень ──
                    if (
                        TRAILING_BE_TRIGGER > 0
                        and current_price >= TRAILING_BE_TRIGGER
                        and remaining > 0
                    ):
                        be_sl = TRAILING_BE_TRIGGER * (1 - TRAILING_BE_SL_PCT / 100) if TRAILING_BE_SL_PCT > 0 else 0.40
                        if sl < be_sl:
                            update_sl_price(pos_id, be_sl)
                            logger.info(
                                "TRAILING BE #%s: price %.2f >= %.2f → SL %.2f → %.2f",
                                pos_id, current_price, TRAILING_BE_TRIGGER, sl, be_sl,
                            )
                            await send_info_message(
                                f"\U0001f512 <b>SL locked #{pos_id}</b>\n"
                                f"Price {current_price:.2f} hit {TRAILING_BE_TRIGGER:.2f} → "
                                f"SL raised to {be_sl:.2f}"
                            )
                            sl = be_sl

                    # ── Stop-loss (BTC price guard) ──
                    # Якщо є btc_strike — перевіряємо чи BTC реально розвернувся
                    # Якщо BTC все ще в нашому боці → не тригерити SL (CLOB bid шумить)
                    _btc_strike = float(pos.get("btc_strike") or 0)
                    if sl > 0 and current_price <= sl and _btc_strike > 0:
                        from bot import ws_binance
                        _btc_now = ws_binance.get_price("BTCUSDT")
                        if _btc_now:
                            _btc_in_our_favor = (
                                (pos["direction"] == "UP" and _btc_now > _btc_strike)
                                or (pos["direction"] == "DOWN" and _btc_now < _btc_strike)
                            )
                            if _btc_in_our_favor:
                                logger.info(
                                    "SL SKIP #%s: CLOB bid %.2f <= SL %.2f BUT BTC $%.0f still %s strike $%.0f",
                                    pos_id, current_price, sl, _btc_now, pos["direction"], _btc_strike,
                                )
                                continue  # BTC в нашу сторону — не тригерити SL

                    # ── Stop-loss ──
                    if sl > 0 and current_price <= sl:
                        logger.warning(
                            "SL TRIGGERED #%s: %.2f <= %.2f", pos_id, current_price, sl,
                        )
                        # Quasi market order: crosses any real bid.
                        # Must satisfy CLOB $1 notional min (price * shares >= 1.0)
                        _min_p = math.ceil(100.0 / max(remaining, 0.01)) / 100.0
                        sell_price = min(0.99, max(0.10, _min_p))
                        sell_result = await pos_client.sell_shares(
                            pos["token_id"], sell_price, remaining,
                        )
                        sell_ok = bool(sell_result and sell_result.get("success") is True)
                        if not sell_ok:
                            if sell_result and sell_result.get("_market_resolved"):
                                # Не можемо продати (мінімум CLOB або маркет закрився)
                                # Закриваємо в БД — settlement запише фінальний PnL
                                pnl = _pnl_total_on_full_close(
                                    stake_u, shares_init, remaining, current_price, realized_accum,
                                )
                                close_position(pos_id, "stop_loss_no_fill", pnl)
                                logger.warning(
                                    "SL #%s: CLOB sell неможливий (мінімум/резолв) — закрито в БД, settlement підтвердить",
                                    pos_id,
                                )
                                await send_info_message(
                                    f"\U0001f6d1 <b>Stop-Loss #{pos_id} (no fill)</b>\n"
                                    f"{pos['direction']} {pos['side']} | "
                                    f"Entry: {entry:.2f} \u2192 {current_price:.2f}\n"
                                    f"CLOB sell неможливий ({remaining:.1f} shares < мінімум)\n"
                                    f"PnL: <b>{pnl:+.2f} USD</b>"
                                )
                            else:
                                logger.error(
                                    "SL SELL failed #%s (ціна %.2f, shares %.2f) — %s. Повторимо наступного циклу.",
                                    pos_id, sell_price, remaining, sell_result,
                                )
                            continue
                        # Реальна ціна продажу з fill, fallback на current_price
                        _fill_price = current_price
                        try:
                            _avg = sell_result.get("average_price") or sell_result.get("price")
                            if _avg:
                                _fill_price = float(_avg)
                        except Exception:
                            pass
                        pnl = _pnl_total_on_full_close(
                            stake_u, shares_init, remaining, _fill_price, realized_accum,
                        )
                        close_position(pos_id, "stop_loss", pnl)

                        sl_text = (
                            f"\U0001f6d1 <b>Stop-Loss #{pos_id}</b>\n"
                            f"{pos['direction']} {pos['side']} | "
                            f"Entry: {entry:.2f} \u2192 Exit: {_fill_price:.2f}\n"
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

                    # ── Take-Profit FINAL @ 0.97 — sell all remaining ──
                    if current_price >= TP_FINAL_PRICE:
                        logger.info(
                            "TP FINAL #%s: price %.2f >= %.2f",
                            pos_id, current_price, TP_FINAL_PRICE,
                        )
                        sell_result = await pos_client.sell_shares(
                            pos["token_id"], current_price, remaining,
                        )
                        if not (sell_result and sell_result.get("success") is True):
                            if sell_result and sell_result.get("_market_resolved"):
                                logger.info("TP FINAL #%s: маркет вже резолвнувся — settlement закриє позицію", pos_id)
                            else:
                                logger.error("TP FINAL SELL failed #%s: %s — позиція залишається відкритою", pos_id, sell_result)
                            continue
                        _tp_fill = current_price
                        try:
                            _tp_avg = sell_result.get("average_price") or sell_result.get("price")
                            if _tp_avg: _tp_fill = float(_tp_avg)
                        except Exception: pass
                        pnl = _pnl_total_on_full_close(
                            stake_u, shares_init, remaining, _tp_fill, realized_accum,
                        )
                        close_position(pos_id, "tp_final", pnl)

                        await send_info_message(
                            f"\U0001f3af <b>Full Exit #{pos_id} @ {_tp_fill:.2f}</b>\n"
                            f"{pos['direction']} {pos['side']} | "
                            f"Entry: {entry:.2f} \u2192 {current_price:.2f}\n"
                            f"Sold {remaining:.0f} shares\n"
                            f"PnL: <b>{pnl:+.2f} USD</b>"
                        )
                        continue

                    # ── Take-Profit levels — перевіряємо послідовно L1→L2→L3 в одному циклі ──
                    # Якщо ціна стрибнула через кілька рівнів — всі спрацьовують по черзі.
                    _pos_closed = False

                    # ── Take-Profit LEVEL 1 @ 0.90 — sell 25% of remaining ──
                    if not _pos_closed and partial_level < 1 and current_price >= TP_PARTIAL_PRICE:
                        sell_amount = remaining * (TP_PARTIAL_SELL_PCT / 100)
                        if remaining - sell_amount < 1:
                            sell_amount = remaining
                        if sell_amount >= 1:
                            logger.info(
                                "TP L1 #%s: price %.2f >= %.2f, selling %.1f",
                                pos_id, current_price, TP_PARTIAL_PRICE, sell_amount,
                            )
                            sell_result = await pos_client.sell_shares(
                                pos["token_id"], current_price, sell_amount,
                                fallback_size=remaining,
                            )
                            if not (sell_result and sell_result.get("success") is True):
                                logger.error("TP L1 SELL failed #%s: %s — позиція залишається відкритою", pos_id, sell_result)
                                continue
                            if sell_result.get("_used_fallback_size"):
                                sell_amount = remaining
                            new_remaining = remaining - sell_amount
                            leg_pnl = _pnl_partial_leg_usd(
                                stake_u, shares_init, sell_amount, current_price,
                            )
                            if new_remaining <= 0:
                                total_pnl = _pnl_total_on_full_close(
                                    stake_u, shares_init, remaining, current_price, realized_accum,
                                )
                                close_position(pos_id, "tp_l1_full", total_pnl)
                                await send_info_message(
                                    f"\U0001f3af <b>Full Exit (TP L1) #{pos_id} @ {current_price:.2f}</b>\n"
                                    f"{pos['direction']} {pos['side']} | "
                                    f"Entry: {entry:.2f} \u2192 {current_price:.2f}\n"
                                    f"Sold all {sell_amount:.0f} shares\n"
                                    f"PnL: <b>{total_pnl:+.2f} USD</b>"
                                )
                                _pos_closed = True
                            else:
                                update_partial_exit(pos_id, sell_amount, new_remaining, leg_pnl, level=1)
                                await send_info_message(
                                    f"\U0001f4b0 <b>TP L1 #{pos_id} @ {current_price:.2f}</b>\n"
                                    f"Sold {sell_amount:.0f} / {remaining:.0f} shares\n"
                                    f"Locked: <b>{leg_pnl:+.2f} USD</b>\n"
                                    f"Remaining: {new_remaining:.0f} shares \u2192 "
                                    f"L2 exit @ {TP_MID_PRICE:.2f}"
                                )
                                partial_level = 1
                                remaining = new_remaining
                                realized_accum += leg_pnl

                    # ── Take-Profit LEVEL 2 @ 0.93 — sell 33% of remaining ──
                    if not _pos_closed and partial_level < 2 and current_price >= TP_MID_PRICE:
                        sell_amount = remaining / 3
                        if remaining - sell_amount < 1:
                            sell_amount = remaining
                        if sell_amount >= 1:
                            logger.info(
                                "TP L2 #%s: price %.2f >= %.2f, selling %.1f",
                                pos_id, current_price, TP_MID_PRICE, sell_amount,
                            )
                            sell_result = await pos_client.sell_shares(
                                pos["token_id"], current_price, sell_amount,
                                fallback_size=remaining,
                            )
                            if not (sell_result and sell_result.get("success") is True):
                                logger.error("TP L2 SELL failed #%s: %s — позиція залишається відкритою", pos_id, sell_result)
                                continue
                            if sell_result.get("_used_fallback_size"):
                                sell_amount = remaining
                            new_remaining = remaining - sell_amount
                            leg_pnl = _pnl_partial_leg_usd(
                                stake_u, shares_init, sell_amount, current_price,
                            )
                            if new_remaining <= 0:
                                total_pnl = _pnl_total_on_full_close(
                                    stake_u, shares_init, remaining, current_price, realized_accum,
                                )
                                close_position(pos_id, "tp_l2_full", total_pnl)
                                await send_info_message(
                                    f"\U0001f3af <b>Full Exit (TP L2) #{pos_id} @ {current_price:.2f}</b>\n"
                                    f"{pos['direction']} {pos['side']} | "
                                    f"Entry: {entry:.2f} \u2192 {current_price:.2f}\n"
                                    f"Sold all {sell_amount:.0f} shares\n"
                                    f"PnL: <b>{total_pnl:+.2f} USD</b>"
                                )
                                _pos_closed = True
                            else:
                                update_partial_exit(pos_id, sell_amount, new_remaining, leg_pnl, level=2)
                                await send_info_message(
                                    f"\U0001f4b0 <b>TP L2 #{pos_id} @ {current_price:.2f}</b>\n"
                                    f"Sold {sell_amount:.0f} / {remaining:.0f} shares\n"
                                    f"Locked: <b>{leg_pnl:+.2f} USD</b>\n"
                                    f"Remaining: {new_remaining:.0f} shares \u2192 "
                                    f"L3 exit @ {TP_FULL_PRICE:.2f}"
                                )
                                partial_level = 2
                                remaining = new_remaining
                                realized_accum += leg_pnl

                    # ── Take-Profit LEVEL 3 @ 0.95 — sell 50% of remaining ──
                    if not _pos_closed and partial_level < 3 and current_price >= TP_FULL_PRICE:
                        sell_amount = remaining * 0.5
                        # Якщо залишок після продажу < 1 share — продаємо все (реальний CLOB мінімум ~$1 notional)
                        if remaining - sell_amount < 1:
                            sell_amount = remaining
                        if sell_amount >= 1:
                            logger.info(
                                "TP L3 #%s: price %.2f >= %.2f, selling %.1f",
                                pos_id, current_price, TP_FULL_PRICE, sell_amount,
                            )
                            sell_result = await pos_client.sell_shares(
                                pos["token_id"], current_price, sell_amount,
                                fallback_size=remaining,
                            )
                            if not (sell_result and sell_result.get("success") is True):
                                logger.error("TP L3 SELL failed #%s: %s — позиція залишається відкритою", pos_id, sell_result)
                                continue
                            if sell_result.get("_used_fallback_size"):
                                sell_amount = remaining
                            new_remaining = remaining - sell_amount
                            leg_pnl = _pnl_partial_leg_usd(
                                stake_u, shares_init, sell_amount, current_price,
                            )
                            if new_remaining <= 0:
                                total_pnl = _pnl_total_on_full_close(
                                    stake_u, shares_init, remaining, current_price, realized_accum,
                                )
                                close_position(pos_id, "tp_l3_full", total_pnl)
                                await send_info_message(
                                    f"\U0001f3af <b>Full Exit (TP L3) #{pos_id} @ {current_price:.2f}</b>\n"
                                    f"{pos['direction']} {pos['side']} | "
                                    f"Entry: {entry:.2f} \u2192 {current_price:.2f}\n"
                                    f"Sold all {sell_amount:.0f} shares\n"
                                    f"PnL: <b>{total_pnl:+.2f} USD</b>"
                                )
                            else:
                                update_partial_exit(pos_id, sell_amount, new_remaining, leg_pnl, level=3)
                                await send_info_message(
                                    f"\U0001f4b0 <b>TP L3 #{pos_id} @ {current_price:.2f}</b>\n"
                                    f"Sold {sell_amount:.0f} / {remaining:.0f} shares\n"
                                    f"Locked: <b>{leg_pnl:+.2f} USD</b>\n"
                                    f"Remaining: {new_remaining:.0f} shares \u2192 "
                                    f"final exit @ {TP_FINAL_PRICE:.2f}"
                                )

                except Exception as e:
                    logger.error("Помилка обробки позиції #%s: %s", pos.get("id"), e, exc_info=True)

        except Exception as e:
            logger.error("Помилка monitor_positions: %s", e, exc_info=True)

        sleep_sec = 1 if count_open_positions() > 0 else POSITION_MONITOR_INTERVAL
        await asyncio.sleep(sleep_sec)


PENDING_POLL_INTERVAL = 15  # fallback poll — рідко, основне через WS
WS_USER_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


async def monitor_pending_orders_loop(execution_client, execution_clients: dict | None = None) -> None:
    """WebSocket + fallback polling monitor for pending limit orders."""
    from bot.storage import (
        get_pending_orders, delete_pending_order,
        mark_signal_live_no_position, update_signal_live_fill,
    )
    from bot.telegram_bot import send_info_message as _send_info_message_base

    def _get_client_for_order(po: dict):
        """Resolve ExecutionClient for a pending order's strategy."""
        if not execution_clients:
            return execution_client
        sid = po.get("strategy_id") or ""
        if sid:
            from bot.strategies import get_strategy
            strat = get_strategy(sid)
            if strat:
                wk = strat.get("wallet_key", "POLYMARKET_PRIVATE_KEY")
                client = execution_clients.get(wk)
                if client and client.ready:
                    return client
        return execution_client

    async def _process_order(po: dict, info: dict | None = None) -> None:
        """Process a single pending order — check status and act."""
        order_id = po["order_id"]
        signal_id = po["signal_id"]
        po_client = _get_client_for_order(po)
        _po_sid = po.get("strategy_id")

        async def send_info_message(text: str, _sid=_po_sid):
            return await _send_info_message_base(text, strategy_id=_sid)

        # Check expiry
        now = datetime.now(timezone.utc)
        expires_at_str = po.get("expires_at") or ""
        if expires_at_str:
            try:
                exp = datetime.fromisoformat(expires_at_str).replace(tzinfo=timezone.utc)
                if now > exp:
                    logger.info("Pending order %s expired — cancel", order_id[:12])
                    await po_client.cancel_order(order_id)
                    await asyncio.to_thread(delete_pending_order, order_id)
                    await asyncio.to_thread(mark_signal_live_no_position, signal_id)
                    await send_info_message(
                        f"⏱ <b>Ордер скасовано</b> (час вийшов)\n"
                        f"Сигнал #{signal_id} | orderID: <code>{order_id[:16]}</code>"
                    )
                    return
            except Exception:
                pass

        # Get status (from WS event or REST fallback)
        if not info:
            info = await po_client.get_order_status(order_id)
        if not info:
            return

        status = (info.get("status") or "").upper()

        if status == "CANCELLED":
            await asyncio.to_thread(delete_pending_order, order_id)
            await asyncio.to_thread(mark_signal_live_no_position, signal_id)
            await send_info_message(
                f"❌ <b>Ордер скасовано на біржі</b>\n"
                f"Сигнал #{signal_id} | orderID: <code>{order_id[:16]}</code>"
            )
            return

        if status == "LIVE":
            return  # still waiting

        if status == "MATCHED":
            await asyncio.to_thread(delete_pending_order, order_id)

            ep = float(info.get("price") or po["limit_price"])
            size_matched = float(info.get("size_matched") or po["shares"])
            stake_eff = round(ep * size_matched, 2)

            pos_id = open_position(
                signal_id=signal_id,
                market_id=po["market_id"] or "",
                market_slug=po["market_slug"] or "",
                token_id=po["token_id"],
                direction=po["direction"],
                entry_price=ep,
                shares=size_matched,
                stake_usd=stake_eff,
                order_result=info,
                market_expires_at=po.get("expires_at"),
                strategy_id=po.get("strategy_id"),
            )
            await asyncio.to_thread(update_signal_live_fill, signal_id, stake_eff, ep)

            side = "YES" if po["direction"] == "UP" else "NO"
            logger.info(
                "Pending order FILLED: signal #%s pos #%s %s @ %.2f %s shares",
                signal_id, pos_id, side, ep, size_matched,
            )
            await send_info_message(
                f"✅ <b>LIMIT FILL!</b>\n"
                f"Pos #{pos_id} | {side} @ {ep:.2f} | "
                f"{size_matched:.1f} shares | ~${stake_eff:.2f}\n"
                f"Сигнал #{signal_id}"
            )

    async def _check_safety_fill(order_id: str, event: dict):
        """Check if a filled order is an arb safety net — auto-hedge if so."""
        from bot.storage import get_connection
        from bot.arb_handler import start_exit_monitor
        try:
            conn = get_connection()
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM pending_hedges WHERE safety_order_id=? AND status='open'",
                (order_id,),
            ).fetchone()
            conn.close()
            if not row:
                return
            h = dict(row)
            ep = float(event.get("price") or 0)
            shares = float(event.get("size_matched") or h.get("leg1_shares", 0))
            if ep <= 0:
                return
            cost = round(shares * ep, 2)
            opp_side = "DOWN" if h["leg1_side"] == "UP" else "UP"

            from bot.storage import mark_hedge_filled
            mark_hedge_filled(h["id"], opp_side, shares, ep, cost, order_id)

            leg1_cost = h.get("leg1_cost", 0) or 0
            total = leg1_cost + cost
            profit = min(h.get("leg1_shares", 0), shares) * 1.0 - total

            logger.info("SAFETY NET FILLED #%d: %s %.1fsh @ %.3f profit=$%.2f", h["id"], opp_side, shares, ep, profit)
            await _send_info_message_base(
                f"🛡 <b>Safety hedge #{h['id']}</b>\n"
                f"L2: {opp_side} {shares:.1f}sh @ {ep:.3f} = ${cost:.2f}\n"
                f"Total: ${total:.2f} | <b>Profit: ${profit:+.2f}</b>"
            )
        except Exception as e:
            logger.warning("_check_safety_fill: %s", e)

    # --- WS listener task ---
    async def _ws_listener():
        """Subscribe to user channel for instant order fill events."""
        import websockets
        import json

        # Collect API creds from all execution clients
        creds_list = []
        clients_to_check = [execution_client] if execution_client else []
        if execution_clients:
            clients_to_check.extend(execution_clients.values())
        for ec in clients_to_check:
            if ec and ec.ready and hasattr(ec, 'client') and hasattr(ec.client, 'creds'):
                cr = ec.client.creds
                if cr and cr.api_key and cr.api_key not in [c.api_key for c in creds_list]:
                    creds_list.append(cr)

        if not creds_list:
            logger.warning("No CLOB creds for WS user channel — WS disabled")
            return

        while True:
            try:
                async with websockets.connect(WS_USER_URL, ping_interval=15, ping_timeout=10) as ws:
                    # Subscribe for each wallet
                    for cr in creds_list:
                        sub = json.dumps({
                            "auth": {
                                "apiKey": cr.api_key,
                                "secret": cr.api_secret,
                                "passphrase": cr.api_passphrase,
                            },
                            "type": "user",
                        })
                        await ws.send(sub)
                    logger.info("WS user channel connected (%d wallets)", len(creds_list))

                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                            if not isinstance(data, list):
                                data = [data]

                            for event in data:
                                oid = event.get("id") or event.get("order_id") or event.get("orderID") or ""
                                status = (event.get("status") or "").upper()
                                if not oid or status not in ("MATCHED", "CANCELLED"):
                                    continue

                                logger.info("WS order event: %s %s", oid[:12], status)
                                # Find matching pending order (strategy GTC)
                                pending = await asyncio.to_thread(get_pending_orders)
                                matched_po = False
                                for po in pending:
                                    if po["order_id"] == oid:
                                        await _process_order(po, info=event)
                                        matched_po = True
                                        break

                                # Check if it's an arb safety net order
                                if not matched_po and status == "MATCHED":
                                    await _check_safety_fill(oid, event)
                        except Exception as e:
                            logger.debug("WS user parse error: %s", e)

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("WS user channel error: %s — reconnect in 5s", e)
                await asyncio.sleep(5)

    # --- Start WS listener in background ---
    ws_task = asyncio.create_task(_ws_listener())
    logger.info("Запущено моніторинг pending ордерів (WS + fallback %ss)", PENDING_POLL_INTERVAL)

    # --- Fallback polling loop (catches expiry + missed WS events) ---
    try:
        while True:
            try:
                pending = await asyncio.to_thread(get_pending_orders)
                for po in pending:
                    await _process_order(po)
            except Exception as e:
                logger.error("Помилка monitor_pending_orders: %s", e, exc_info=True)

            await asyncio.sleep(PENDING_POLL_INTERVAL)
    finally:
        ws_task.cancel()
