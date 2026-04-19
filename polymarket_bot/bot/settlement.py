import asyncio
import json
import logging
import sqlite3 as _sqlite3
import time
from collections import defaultdict
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
from bot.ws_settlement import get_watcher

_NO_POSITION = "no_position"

logger = logging.getLogger(__name__)

# Збираємо результати по маркетах для зведеного звіту
_market_results: dict[str, list[dict]] = defaultdict(list)
_market_titles: dict[str, str] = {}
_market_signal_counts: dict[str, int] = defaultdict(int)

SETTLEMENT_INTERVAL_SECONDS = 300  # fallback polling: кожні 5 хв (WS handles most cases)



def _count_unresolved_for_market(market_id: str) -> int:
    """Скільки ще не settled сигналів для цього маркету."""
    try:
        conn = _sqlite3.connect(get_db_path())
        conn.execute("PRAGMA busy_timeout=3000")
        row = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE market_id = ? AND result IS NULL",
            (market_id,),
        ).fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        return 0


def _get_position_id(signal_id: int) -> int | None:
    """Знайти position ID для сигналу."""
    try:
        conn = _sqlite3.connect(get_db_path())
        conn.execute("PRAGMA busy_timeout=3000")
        row = conn.execute(
            "SELECT id FROM positions WHERE signal_id = ? LIMIT 1",
            (signal_id,),
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


async def _send_market_summary(market_id: str):
    """Зведений звіт по маркету після того як всі сигнали settled."""
    results = _market_results.pop(market_id, [])
    title = _market_titles.pop(market_id, market_id[:24])
    _market_signal_counts.pop(market_id, None)

    if not results:
        return

    wins = [r for r in results if r["result"] == "WIN"]
    losses = [r for r in results if r["result"] == "LOSS"]
    no_entry = [r for r in results if r["result"] == "NO_ENTRY"]
    total_pnl = sum(r.get("pnl", 0) for r in results)

    lines = [f"\U0001f4ca <b>{title}</b>", ""]

    for r in results:
        sid = r["id"]
        direction = r["direction"]
        side = "YES" if direction == "UP" else "NO"
        conf = r.get("confluence", "?")
        fv = r.get("filter_version", "")
        fv_tag = f" [{fv.upper()}]" if fv == "both" else ""
        entry = r.get("entry_price", r.get("cp", 0))
        pos_id = _get_position_id(sid)

        if r["result"] in ("WIN", "LOSS"):
            pnl = r.get("pnl", 0)
            result_icon = "\u2705" if r["result"] == "WIN" else "\u274c"
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"${pnl:.2f}"
            pos_label = f"Pos #{pos_id}" if pos_id else f"#{sid}"
            lines.append(f"{pos_label} | {direction} | {side} @ {entry:.2f} | conf={conf}{fv_tag}")
            lines.append(f"{result_icon} PnL: <b>{pnl_str}</b>")
        else:
            lines.append(f"#{sid} | {direction} | {side} @ {r.get('cp', 0):.2f} | conf={conf}")
            lines.append(f"\U0001f4a4 no entry")
        lines.append("")

    # Підсумковий рядок тільки якщо > 1 сигнал
    if len(results) > 1:
        pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"${total_pnl:.2f}"
        summary_icon = "\U0001f7e2" if total_pnl > 0 else ("\U0001f534" if total_pnl < 0 else "\u26aa")
        lines.append(f"{summary_icon} W:{len(wins)} L:{len(losses)} NE:{len(no_entry)} | PnL: <b>{pnl_str}</b>")

    await send_info_message("\n".join(lines))


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


async def _settle_one(sig: dict, price_yes: float, price_no: float, execution_client=None):
    """
    Закриває один сигнал після резолюції маркету.
    Викликається і з WS callback і з polling loop.
    """
    # Якщо вже є результат — пропустити (міг бути оброблений WS раніше)
    if sig.get("result"):
        return

    direction = sig.get("direction") or "UP"
    side_label = "YES" if direction == "UP" else "NO"
    cp = float(sig.get("contract_price") or 0.5)

    market_id = sig.get("market_id", "")
    market_title = _market_title(sig)
    _market_titles[market_id] = market_title

    # Confluence та filter_version з payload
    try:
        _payload = json.loads(sig.get("payload_json") or "{}")
        _conf = _payload.get("confluence", "?")
        _fv = _payload.get("filter_version", "")
    except Exception:
        _conf = "?"
        _fv = ""

    def _record_result(result: str, pnl: float, entry_price: float = 0.0):
        _market_results[market_id].append({
            "id": sig["id"], "direction": direction, "result": result,
            "pnl": pnl, "cp": cp, "entry_price": entry_price or cp,
            "confluence": _conf, "filter_version": _fv,
        })

    async def _try_send_summary():
        remaining = _count_unresolved_for_market(market_id)
        if remaining == 0:
            await _send_market_summary(market_id)

    # --- NO_POSITION ---
    if sig.get("live_entry_status") == _NO_POSITION:
        update_result(sig["id"], "NO_ENTRY", 0.0)
        logger.info("Сигнал %s: no_position → NO_ENTRY", sig["id"])
        _record_result("NO_ENTRY", 0.0)
        await get_watcher().unwatch(sig["id"])
        await _try_send_summary()
        return

    # --- Позиція закрита монітором ---
    live_status = sig.get("live_entry_status")
    if live_status == "opened" and signal_live_position_already_closed(sig["id"]):
        try:
            _conn = _sqlite3.connect(get_db_path())
            _row = _conn.execute(
                "SELECT pnl, entry_price FROM positions WHERE signal_id = ? AND status = 'closed' LIMIT 1",
                (sig["id"],),
            ).fetchone()
            _conn.close()
            real_pnl = float(_row[0] or 0) if _row else 0.0
            entry_price = float(_row[1] or cp) if _row and len(_row) > 1 else cp
        except Exception:
            real_pnl = 0.0
            entry_price = cp
        result_str = "WIN" if real_pnl > 0 else "LOSS"
        update_result(sig["id"], result_str, real_pnl)
        logger.info("Сигнал %s: закрито монітором — %s, PnL: %.2f", sig["id"], result_str, real_pnl)
        _record_result(result_str, real_pnl, entry_price)
        await get_watcher().unwatch(sig["id"])
        await _try_send_summary()
        return

    # --- Верифікація через Polymarket trades ---
    if execution_client and getattr(execution_client, "ready", False) and live_status in ("pending_fill", "opened"):
        token_id = _token_id_for_signal(sig)
        po = None
        if not token_id:
            po = await asyncio.to_thread(get_pending_order_by_signal, sig["id"])
            if po:
                token_id = po.get("token_id")

        if token_id:
            try:
                sig_ts_str = sig.get("timestamp") or ""
                sig_ts = datetime.strptime(sig_ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                sig_ts = time.time() - 3600
            trade = await _find_buy_trade(execution_client, token_id, sig_ts)

            if trade is None:
                if live_status == "pending_fill":
                    try:
                        po = po or await asyncio.to_thread(get_pending_order_by_signal, sig["id"])
                        if po:
                            await execution_client.cancel_order(po["order_id"])
                            await asyncio.to_thread(delete_pending_order, po["order_id"])
                    except Exception as _e:
                        logger.warning("cancel pending order: %s", _e)
                update_result(sig["id"], "NO_ENTRY", 0.0)
                logger.info("Сигнал %s: trade не знайдено → NO_ENTRY", sig["id"])
                _record_result("NO_ENTRY", 0.0)
                await get_watcher().unwatch(sig["id"])
                await _try_send_summary()
                return

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

            logger.info("Сигнал %s: fill підтверджено — %s, PnL: %.2f", sig["id"], result_str, pnl)
            _record_result(result_str, pnl, real_price)
            if state.circuit_breaker_active:
                await send_info_message(
                    f"\U0001f6a8 <b>CIRCUIT BREAKER!</b>\n"
                    f"{state.consecutive_losses} losses підряд — live trading вимкнено.\n/reset щоб відновити."
                )
            await get_watcher().unwatch(sig["id"])
            await _try_send_summary()
            return

    # --- Fallback: paper PnL ---
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
    _record_result(result_str, pnl)
    if state.circuit_breaker_active:
        await send_info_message(
            f"\U0001f6a8 <b>CIRCUIT BREAKER!</b>\n"
            f"{state.consecutive_losses} losses підряд — live trading вимкнено.\n/reset щоб відновити."
        )
    await get_watcher().unwatch(sig["id"])
    await _try_send_summary()


async def settle_markets(execution_client=None):
    """
    Фоновий процес: WS-підписка + fallback polling кожні 5 хв.
    Якщо execution_client передано — верифікує реальний fill через Polymarket trades.
    """
    poly = PolymarketClient()
    watcher = get_watcher()
    logger.info("Запущено Settlement (WS + fallback polling %ds)", SETTLEMENT_INTERVAL_SECONDS)

    # --- WS callback: викликається миттєво при market_resolved ---
    async def on_ws_resolved(info: dict, winning_asset_id: str):
        sig_id = info["signal_id"]
        direction = info["direction"]

        # price_yes/price_no з winning_asset_id
        if winning_asset_id == info["yes_token_id"]:
            price_yes, price_no = 1.0, 0.0
        else:
            price_yes, price_no = 0.0, 1.0

        # Перечитуємо сигнал з БД щоб мати актуальні дані
        unresolved = get_unresolved_signals()
        sig = next((s for s in unresolved if s["id"] == sig_id), None)
        if sig is None:
            logger.debug("WS resolved: сигнал #%s вже закритий або не знайдений", sig_id)
            return

        logger.info("WS market_resolved → settlement сигналу #%s (direction=%s)", sig_id, direction)
        await _settle_one(sig, price_yes, price_no, execution_client)

    watcher.set_resolved_callback(on_ws_resolved)

    # Завантажити всі активні сигнали в watcher при старті
    for sig in get_unresolved_signals():
        await watcher.watch(sig)

    # Запустити WS watcher як окрему задачу
    asyncio.create_task(watcher.run())

    # --- Fallback polling ---
    try:
        while True:
            await asyncio.sleep(SETTLEMENT_INTERVAL_SECONDS)
            try:
                unresolved = get_unresolved_signals()
                # Підписати нові сигнали (якщо з'явились поки спали)
                for sig in unresolved:
                    await watcher.watch(sig)

                # Перевірити через REST чи якийсь маркет вже закрито (WS міг пропустити)
                for sig in unresolved:
                    if sig.get("result"):
                        continue
                    info = await poly.get_market_prices(sig["market_id"])
                    if not info:
                        continue
                    price_yes = info.get("price_yes", 0.0)
                    price_no = info.get("price_no", 0.0)
                    if price_yes in [0.0, 1.0] and price_no in [0.0, 1.0]:
                        logger.info("Polling fallback: маркет %s закрито → settle сигналу #%s",
                                    sig["market_id"][:12], sig["id"])
                        await _settle_one(sig, price_yes, price_no, execution_client)
                # --- Safety net: close orphan positions (expired but not settled) ---
                await _close_orphan_positions(poly, execution_client)

            except Exception as e:
                logger.error("Помилка в fallback polling: %s", e, exc_info=True)
    finally:
        await poly.close()


async def _close_orphan_positions(poly, execution_client=None):
    """Close positions where market expired but settlement missed them."""
    from bot.strategies import get_strategy

    try:
        positions = get_open_positions()
        now = datetime.now(timezone.utc)

        for pos in positions:
            expires_str = pos.get("market_expires_at") or ""
            if not expires_str:
                continue
            try:
                exp = datetime.fromisoformat(expires_str).replace(tzinfo=timezone.utc)
            except Exception:
                continue

            # Only process if expired > 5 min ago (give normal settlement time)
            if (now - exp).total_seconds() < 300:
                continue

            market_id = pos.get("market_id") or ""
            if not market_id:
                continue

            # Check market resolution via API
            info = await poly.get_market_prices(market_id)
            if not info:
                continue
            price_yes = info.get("price_yes", 0.0)
            price_no = info.get("price_no", 0.0)
            if price_yes not in [0.0, 1.0] or price_no not in [0.0, 1.0]:
                continue  # not yet resolved

            direction = pos.get("direction", "UP")
            is_win = (direction == "UP" and price_yes == 1.0) or (direction == "DOWN" and price_no == 1.0)

            entry = float(pos.get("entry_price", 0))
            shares = float(pos.get("remaining_shares") or pos.get("shares") or 0)
            stake = float(pos.get("stake_usd", 0))
            realized = float(pos.get("realized_pnl") or 0)

            if is_win:
                pnl = round(shares * 1.0 - stake + realized, 2)
                result = "WIN"
            else:
                pnl = round(-stake + realized, 2)
                result = "LOSS"

            close_position(pos["id"], f"orphan_settlement_{result}", pnl)

            # Also fix the signal if it exists
            sig_id = pos.get("signal_id")
            if sig_id:
                from bot.storage import get_connection
                try:
                    conn = get_connection()
                    conn.execute(
                        "UPDATE signals SET result = ?, pnl = ? WHERE id = ? AND (result IS NULL OR result = 'NO_ENTRY')",
                        (result, pnl, sig_id),
                    )
                    conn.commit()
                    conn.close()
                except Exception:
                    pass

            logger.warning(
                "ORPHAN SETTLEMENT #%s: %s %s, PnL: %.2f (market %s expired %s)",
                pos["id"], direction, result, pnl, market_id[:12], expires_str,
            )
            await send_info_message(
                f"🔧 <b>Orphan Settlement #{pos['id']}</b>\n"
                f"{direction} {result} | PnL: <b>{pnl:+.2f}</b>\n"
                f"Market expired {expires_str[:16]}"
            )
    except Exception as e:
        logger.error("_close_orphan_positions error: %s", e)
