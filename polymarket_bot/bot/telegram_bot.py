import asyncio
import html
import logging
import sqlite3
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import (
    TELEGRAM_TOKEN,
    CHAT_ID,
    STAKE_USD,
    MAX_OPEN_POSITIONS,
    AUTO_APPROVE_LIVE,
    CLOB_TRADE_HISTORY_LIMIT,
    CLOB_TRADE_HISTORY_MAX_PAGES,
)
from bot.execution_client import trade_timestamp
from bot.storage import (
    mark_signal_live_no_position,
    update_decision,
    update_signal_live_fill,
    update_telegram_message_id,
)
from bot.state import state
from bot.alert_text import format_signal_alert_html

logger = logging.getLogger(__name__)

_SEP = "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"


def _history_ts_human(raw: float) -> str:
    if raw <= 0:
        return "\u2014"
    ts = raw / 1000.0 if raw > 1e12 else raw
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%d.%m.%Y  %H:%M  UTC")
    except (OSError, ValueError, OverflowError):
        return "\u2014"


def _history_price_txt(price: float) -> str:
    if 0 < price < 1:
        return f"{price * 100:.0f}\u00a2"
    return f"${price:.2f}"


def _format_clob_trade_html(idx: int, tr: dict) -> str:
    side_raw = str(tr.get("side", "?")).upper()
    if side_raw == "BUY":
        head = "\U0001f7e2 <b>\u041a\u0443\u043f\u0456\u0432\u043b\u044f</b>"
    elif side_raw == "SELL":
        head = "\U0001f534 <b>\u041f\u0440\u043e\u0434\u0430\u0436</b>"
    else:
        head = f"\u26aa <b>{html.escape(side_raw)}</b>"

    try:
        price = float(tr.get("price", 0))
        size = float(tr.get("size", 0))
    except (TypeError, ValueError):
        price, size = 0.0, 0.0
    notional = price * size
    outcome = html.escape(str(tr.get("outcome") or "").strip())
    title_raw = (tr.get("title") or tr.get("slug") or "").strip()
    title = html.escape(title_raw[:75])
    t_human = _history_ts_human(trade_timestamp(tr))

    parts = [
        f"<b>#{idx}</b>  {head}",
        f"\U0001f551 <code>{html.escape(t_human)}</code>",
        (
            f"\U0001f4b0 \u041a\u0456\u043b\u044c\u043a\u0456\u0441\u0442\u044c: <b>{size:.2f}</b> shares"
            f"  \u00b7  \u0426\u0456\u043d\u0430: <b>{_history_price_txt(price)}</b>"
            f"  \u00b7  \u0412\u0430\u0440\u0442\u0456\u0441\u0442\u044c: <b>~${notional:.2f}</b>"
        ),
    ]
    if outcome:
        parts.append(
            f"\U0001f3af \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 (\u0441\u0442\u043e\u0440\u043e\u043d\u0430): {outcome}",
        )
    if title:
        parts.append(f"\U0001f4cc {title}")
    return _SEP + "\n".join(parts)


bot = Bot(token=TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None
dp = Dispatcher()

_execution_client = None
_pending_signals: dict[int, dict] = {}


def set_execution_client(client):
    global _execution_client
    _execution_client = client


def store_pending_signal(signal_id: int, signal: dict):
    _pending_signals[signal_id] = signal


# ── Commands ──

@dp.message(Command("mode"))
async def cmd_mode(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="\U0001f7e2 Light", callback_data="set_mode|light")
    builder.button(text="\U0001f7e1 Medium", callback_data="set_mode|medium")
    builder.button(text="\U0001f534 Strict", callback_data="set_mode|strict")
    builder.button(text="\U0001f525 Test", callback_data="set_mode|test")
    builder.adjust(3, 1)

    live_status = "\U0001f7e2 ON" if state.is_live_allowed else "\U0001f534 OFF"
    cb_info = ""
    if state.circuit_breaker_active:
        cb_info = "\n\U0001f6a8 <b>Circuit breaker активний!</b> /reset щоб скинути"

    text = (
        f"Поточний режим: <b>{state.mode.upper()}</b>\n"
        f"Live trading: <b>{live_status}</b>{cb_info}\n"
        f"Losses streak: {state.consecutive_losses}\n\n"
        "\U0001f7e2 <b>Light</b>: Гнучкі налаштування.\n"
        "\U0001f7e1 <b>Medium</b>: Збалансовані (рекомендовано).\n"
        "\U0001f534 <b>Strict</b>: Снайперський підхід.\n"
        "\U0001f525 <b>Test</b>: Все пропускається (live trading вимкнено).\n\n"
        "Виберіть режим:"
    )
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.message(Command("positions"))
async def cmd_positions(message: types.Message):
    from bot.position_manager import get_open_positions

    positions = get_open_positions()
    if not positions:
        await message.answer("\u2705 Немає відкритих позицій.")
        return

    lines = [f"\U0001f4ca <b>Відкриті позиції ({len(positions)}):</b>\n"]
    for p in positions:
        lines.append(
            f"#{p['id']} | {p['direction']} {p['side']} @ {p['entry_price']:.2f} | "
            f"{p['remaining_shares']:.0f} shares | SL: {p.get('sl_price', 0):.2f} | "
            f"${p['stake_usd']:.2f}"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("balance"))
async def cmd_balance(message: types.Message):
    if not _execution_client or not _execution_client.ready:
        await message.answer("\u26a0\ufe0f ExecutionClient не налаштований.")
        return
    balance = await _execution_client.get_balance()
    await message.answer(
        f"\U0001f4b0 Баланс Polymarket: <b>${balance:.2f} USDC</b>",
        parse_mode="HTML",
    )


@dp.message(Command("reset"))
async def cmd_reset(message: types.Message):
    """Скинути circuit breaker і відновити live trading."""
    if not state.circuit_breaker_active:
        await message.answer("\u2705 Circuit breaker не активний.")
        return

    state.reset_circuit_breaker()
    await message.answer(
        "\u2705 Circuit breaker скинуто. Live trading відновлено.\n"
        f"Режим: <b>{state.mode.upper()}</b> | "
        f"Live: <b>{'\U0001f7e2 ON' if state.is_live_allowed else '\U0001f534 OFF'}</b>",
        parse_mode="HTML",
    )


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    """Повний статус бота."""
    from bot.position_manager import count_open_positions

    client_ready = bool(_execution_client and _execution_client.ready)
    live_status = "\U0001f7e2 ON" if (state.is_live_allowed and client_ready) else "\U0001f534 OFF"
    cb = "\U0001f6a8 ACTIVE" if state.circuit_breaker_active else "\u2705 OK"
    open_pos = count_open_positions()

    text = (
        f"\U0001f916 <b>Bot Status</b>\n\n"
        f"Mode: <b>{state.mode.upper()}</b>\n"
        f"Live trading: {live_status}\n"
        f"Live guard (state): {'\U0001f7e2 OK' if state.is_live_allowed else '\U0001f534 BLOCKED'}\n"
        f"Execution client: {'\U0001f7e2 READY' if client_ready else '\U0001f534 NOT READY'}\n"
        f"Auto-approve live: {'\U0001f7e2 ON' if AUTO_APPROVE_LIVE else '\U0001f534 OFF'}\n"
        f"Circuit breaker: {cb}\n"
        f"Losses streak: {state.consecutive_losses}\n"
        f"Open positions: {open_pos}/{MAX_OPEN_POSITIONS}\n"
    )

    if client_ready:
        balance = await _execution_client.get_balance()
        text += f"Balance: ${balance:.2f} USDC\n"

    await message.answer(text, parse_mode="HTML")


@dp.message(Command("history"))
async def cmd_history(message: types.Message):
    """Останні угоди з Polymarket CLOB API (/data/trades), не з локальної БД."""
    if not _execution_client or not _execution_client.ready:
        await message.answer(
            "\u26a0\ufe0f CLOB не налаштований \u2014 потрібні ключі для історії угод.",
        )
        return

    trades = await _execution_client.get_recent_trades(
        CLOB_TRADE_HISTORY_LIMIT,
        max_pages=CLOB_TRADE_HISTORY_MAX_PAGES,
    )
    if not trades:
        await message.answer(
            f"\U0001f4cb \u041d\u0435\u043c\u0430\u0454 \u0443\u0433\u043e\u0434 \u0443 \u0432\u0456\u0434\u043f\u043e\u0432\u0456\u0434\u0456 CLOB "
            f"(\u043b\u0456\u043c\u0456\u0442 {CLOB_TRADE_HISTORY_LIMIT}, "
            f"\u0441\u0442\u043e\u0440\u0456\u043d\u043e\u043a {CLOB_TRADE_HISTORY_MAX_PAGES}).",
        )
        return

    intro = (
        f"\U0001f4ca <b>\u0406\u0441\u0442\u043e\u0440\u0456\u044f \u0443\u0433\u043e\u0434 Polymarket</b>\n"
        f"\u0417\u043d\u0438\u0437\u0443: CLOB API (\u043e\u0441\u0442\u0430\u043d\u043d\u0456 <b>{len(trades)}</b> "
        f"\u0437 \u0434\u043e {CLOB_TRADE_HISTORY_MAX_PAGES} \u0441\u0442\u043e\u0440\u0456\u043d\u043e\u043a). "
        f"\u0426\u0435 \u043d\u0435 paper \u0456 \u043d\u0435 \u0440\u043e\u0437\u0440\u0430\u0445\u0443\u043d\u043e\u043a \u0431\u043e\u0442\u0430 \u0432 SQLite.\n"
        f"<i>\u0427\u0430\u0441 \u2014 UTC. \u00ab\u0412\u0430\u0440\u0442\u0456\u0441\u0442\u044c\u00bb = \u0446\u0456\u043d\u0430 \u00d7 \u043a\u0456\u043b\u044c\u043a\u0456\u0441\u0442\u044c shares.</i>"
    )
    blocks = [_format_clob_trade_html(i, tr) for i, tr in enumerate(trades, start=1)]
    text = intro + "".join(blocks)

    if len(text) <= 4096:
        await message.answer(text, parse_mode="HTML")
        return

    half = max(1, len(blocks) // 2)
    await message.answer(intro + "".join(blocks[:half]), parse_mode="HTML")
    await message.answer(
        f"<b>\u041f\u0440\u043e\u0434\u043e\u0432\u0436\u0435\u043d\u043d\u044f (\u0443\u0433\u043e\u0434\u0438 {half + 1}\u2013{len(trades)})</b>"
        + "".join(blocks[half:]),
        parse_mode="HTML",
    )


# ── Callbacks ──

@dp.callback_query(lambda c: c.data and c.data.startswith('set_mode|'))
async def process_mode_change(callback_query: types.CallbackQuery):
    new_mode = callback_query.data.split('|')[1]
    state.mode = new_mode

    live_note = ""
    if new_mode == "test":
        live_note = "\n\u26a0\ufe0f Live trading вимкнено в режимі Test"

    text = f"\u2705 Режим змінено на: <b>{new_mode.upper()}</b>.{live_note}"
    try:
        await bot.edit_message_text(
            chat_id=callback_query.message.chat.id,
            message_id=callback_query.message.message_id,
            text=text,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error("Помилка mode change: %s", e)

    await bot.answer_callback_query(callback_query.id)


async def send_alert(signal_id: int, signal: dict):
    if not bot or not CHAT_ID:
        logger.warning("Telegram не налаштований.")
        return

    from bot.risk import calculate_stake, format_risk_line

    bankroll = None
    if _execution_client and _execution_client.ready and state.mode != "test":
        bankroll = await _execution_client.get_balance()
    risk = calculate_stake(signal, bankroll=bankroll)
    risk_text = format_risk_line(risk)

    client_ready = bool(_execution_client and _execution_client.ready)

    if state.is_live_allowed and client_ready and risk["edge"] > 0:
        stake_display = risk["stake_usd"]
    else:
        stake_display = STAKE_USD

    text = format_signal_alert_html(signal, state.mode, stake_display)
    text += f"\n\U0001f3af {risk_text}"

    if state.is_live_allowed and client_ready and risk["edge"] > 0:
        cp = signal.get("contract_price", 0.5)
        shares = round(risk["stake_usd"] / cp, 1) if cp > 0 else 0
        side = "YES" if signal.get("direction") == "UP" else "NO"
        text += (
            f"\n\n\U0001f7e2 <b>LIVE MODE</b> \u2014 Approve = \u0440\u0435\u0430\u043b\u044c\u043d\u0438\u0439 \u043e\u0440\u0434\u0435\u0440!\n"
            f"\U0001f4b5 <b>BUY {side} @ {cp:.2f} | ${risk['stake_usd']:.2f} | "
            f"{shares} shares</b>"
        )
    elif state.is_live_allowed and not client_ready:
        detail = ""
        if _execution_client is None:
            detail = " Клієнт не інжектовано (перезапусти main.py з LIVE_TRADING=true)."
        else:
            r = getattr(_execution_client, "not_ready_reason", None)
            if r:
                detail = " " + html.escape(r)
        text += (
            "\n\U0001f7e1 <b>LIVE MODE</b> "
            "(ExecutionClient not ready \u2014 ордер не буде розміщено)."
            f"{detail}"
        )
    elif state.is_live_allowed:
        text += "\n\U0001f7e2 <b>LIVE MODE</b> (edge \u2264 0 \u2014 \u043e\u0440\u0434\u0435\u0440 \u043d\u0435 \u0431\u0443\u0434\u0435 \u0440\u043e\u0437\u043c\u0456\u0449\u0435\u043d\u043e)"
        # Позначаємо no_position зразу — settlement запише NO_ENTRY без PnL
        mark_signal_live_no_position(signal_id)
        update_decision(signal_id, "approve")
    elif state.circuit_breaker_active:
        text += "\n\U0001f6a8 <b>Circuit breaker</b> \u2014 live \u0432\u0438\u043c\u043a\u043d\u0435\u043d\u043e (paper only)"

    builder = InlineKeyboardBuilder()
    builder.button(text="\u2705 Approve", callback_data=f"decision|{signal_id}|approve")
    builder.button(text="\u274c Reject", callback_data=f"decision|{signal_id}|reject")
    builder.button(text="\u23ed Skip", callback_data=f"decision|{signal_id}|skip")
    builder.adjust(3)

    try:
        msg = await bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        update_telegram_message_id(signal_id, msg.message_id)
        store_pending_signal(signal_id, {**signal, "_risk": risk})

        # Auto-approve live orders without manual click.
        if AUTO_APPROVE_LIVE and state.is_live_allowed and client_ready and risk.get("edge", 0) > 0:
            update_decision(signal_id, "approve")
            order_text = await _execute_live_order(signal_id)
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"<b>✅ Approve (AUTO)</b>\n{order_text}",
                parse_mode="HTML",
            )
    except Exception as e:
        logger.error("Помилка send_alert: %s", e)


@dp.callback_query(lambda c: c.data and c.data.startswith('decision|'))
async def process_decision(callback_query: types.CallbackQuery):
    parts = callback_query.data.split('|')
    if len(parts) == 3:
        _, sig_id_str, action = parts
        try:
            signal_id = int(sig_id_str)
            update_decision(signal_id, action)

            emojis = {"approve": "✅", "reject": "❌", "skip": "⏭"}
            action_label = f"{emojis.get(action, '')} {action.capitalize()}"

            order_text = ""
            if action == "approve" and state.is_live_allowed and _execution_client and _execution_client.ready:
                order_text = await _execute_live_order(signal_id)
            else:
                # Clear signal from memory on reject/skip to prevent accidental re-execution
                _pending_signals.pop(signal_id, None)

            msg_text = f"<b>{action_label}</b>"
            if order_text:
                msg_text += f"\n{order_text}"

            await bot.send_message(
                chat_id=callback_query.message.chat.id,
                text=msg_text,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error("Помилка decision: %s", e)

    try:
        await bot.answer_callback_query(callback_query.id)
    except Exception:
        pass


async def _execute_live_order(signal_id: int) -> str:
    from bot.position_manager import open_position, count_open_positions

    async def _mark_no_fill() -> None:
        await asyncio.to_thread(mark_signal_live_no_position, signal_id)

    if not state.is_live_allowed:
        return "\u26a0\ufe0f Live trading вимкнено"

    signal = _pending_signals.pop(signal_id, None)
    if not signal:
        await _mark_no_fill()
        return "\u26a0\ufe0f Сигнал не знайдено в пам\u2019яті"

    if count_open_positions() >= MAX_OPEN_POSITIONS:
        await _mark_no_fill()
        return f"\u26a0\ufe0f Ліміт позицій ({MAX_OPEN_POSITIONS})"

    risk = signal.get("_risk", {})
    if risk.get("edge", 0) <= 0:
        await _mark_no_fill()
        return "\u26a0\ufe0f Edge \u2264 0 \u2014 ордер не розміщено"

    direction = signal.get("direction", "UP")
    market_id = signal.get("market_id", "")

    slug = signal.get("market_slug", "") or ""
    mid = str(market_id) if market_id else ""

    token_ids = await _execution_client.get_market_token_ids(
        slug,
        mid or None,
    )
    if not token_ids:
        await _mark_no_fill()
        return "\u274c Не вдалося отримати token IDs (slug/id)"

    yes_token, no_token = token_ids
    token_id = yes_token if direction == "UP" else no_token

    stake = risk.get("stake_usd", 1.0)
    cp = signal.get("contract_price", 0.5)
    neg_risk = bool(signal.get("neg_risk", False))

    result = await _execution_client.buy_shares(
        token_id=token_id,
        price=cp,
        stake_usd=stake,
        neg_risk=neg_risk,
        market_slug=slug or None,
        market_id=mid or None,
    )
    if not result or (isinstance(result, dict) and result.get("success") is False):
        await _mark_no_fill()
        reason = ""
        if isinstance(result, dict):
            reason = result.get("error", "")
        if reason:
            return f"\u274c Ордер не виконано: <code>{reason}</code>"
        return "\u274c Ордер не виконано"

    st_ord = (result.get("status") or "").lower() if isinstance(result, dict) else ""
    if isinstance(result, dict) and result.get("success") and st_ord == "live":
        await _mark_no_fill()
        oid = html.escape(str(result.get("orderID", "")))
        ep = float(result.get("_effective_price", cp))
        return (
            f"\u26a0\ufe0f <b>\u041b\u0456\u043c\u0456\u0442 \u0443 \u0441\u0442\u0430\u043a\u0430\u043d\u0456</b> "
            f"(\u0449\u0435 \u043d\u0435 \u0432\u0438\u043a\u043e\u043d\u0430\u043d\u043e).\n"
            f"orderID: <code>{oid}</code> | \u0446\u0456\u043d\u0430 \u043b\u0456\u043c\u0456\u0442\u0443: {ep:.2f}\n"
            f"\u041d\u0430 Polymarket: \u0412\u0456\u0434\u043a\u0440\u0438\u0442\u0456 \u0437\u0430\u044f\u0432\u043a\u0438 \u2014 \u043c\u043e\u0436\u043d\u0430 \u0441\u043a\u0430\u0441\u0443\u0432\u0430\u0442\u0438.\n"
            f"\u041f\u043e\u0437\u0438\u0446\u0456\u044e \u0432 \u0431\u043e\u0442\u0456 \u043d\u0435 \u0441\u0442\u0432\u043e\u0440\u0435\u043d\u043e (\u0434\u043e \u0440\u0435\u0430\u043b\u044c\u043d\u043e\u0433\u043e fill)."
        )

    ep = float(result.get("_effective_price", cp)) if isinstance(result, dict) else cp
    shares = float(result.get("_order_size", 0)) if isinstance(result, dict) else 0.0
    if shares <= 0 and ep > 0:
        shares = round(stake / ep, 2)
    stake_eff = round(shares * ep, 2)

    pos_id = open_position(
        signal_id=signal_id,
        market_id=market_id,
        market_slug=slug,
        token_id=token_id,
        direction=direction,
        entry_price=ep,
        shares=shares,
        stake_usd=stake_eff,
        order_result=result,
    )

    if pos_id is None:
        await _mark_no_fill()
        return (
            "\u274c CLOB \u0432\u0456\u0434\u043f\u043e\u0432\u0456\u0434\u044c \u043e\u043a, "
            "\u0430\u043b\u0435 \u043f\u043e\u0437\u0438\u0446\u0456\u044e \u0432 \u0411\u0414 \u043d\u0435 \u0437\u0431\u0435\u0440\u0435\u0436\u0435\u043d\u043e."
        )

    await asyncio.to_thread(update_signal_live_fill, signal_id, stake_eff, ep)

    side = "YES" if direction == "UP" else "NO"
    return (
        f"\U0001f680 <b>ORDER EXECUTED</b>\n"
        f"Pos #{pos_id} | {side} @ {ep:.2f} | "
        f"{shares:.1f} shares | ~${stake_eff:.2f}"
    )


async def send_info_message(text: str):
    if not bot or not CHAT_ID:
        return
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML")
    except Exception as e:
        logger.error("Помилка інфо-повідомлення: %s", e)


async def send_daily_report():
    """Надіслати щоденний звіт о 8:00 Київського часу."""
    from zoneinfo import ZoneInfo
    from datetime import timezone
    from bot.config import DB_PATH_TEST, DB_PATH_LIVE
    from bot.position_manager import count_open_positions

    kyiv_tz = ZoneInfo("Europe/Kyiv")
    now_kyiv = datetime.now(kyiv_tz)
    today_start_kyiv = now_kyiv.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start_kyiv.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    lines = [f"📊 <b>Денний звіт — {now_kyiv.strftime('%d.%m.%Y')}</b>\n"]

    for label, db_path in [("🧪 Test", DB_PATH_TEST), ("💰 Live", DB_PATH_LIVE)]:
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT result, pnl, direction, contract_price FROM signals "
                "WHERE decision='approve' AND result IS NOT NULL "
                "AND result NOT IN ('NO_ENTRY','CLOSED_EARLY') "
                "AND timestamp >= ?",
                (today_start_utc,),
            ).fetchall()
            conn.close()
        except Exception:
            continue

        if not rows:
            lines.append(f"<b>{label}:</b> немає трейдів за сьогодні")
            continue

        total = len(rows)
        wins = sum(1 for r in rows if r["result"] == "WIN")
        total_pnl = sum(float(r["pnl"] or 0) for r in rows)
        wr = wins / total * 100 if total else 0
        pnl_str = f"+{total_pnl:.2f}" if total_pnl >= 0 else f"{total_pnl:.2f}"

        lines.append(
            f"<b>{label}:</b> {total} трейдів | WR: {wr:.0f}% | PnL: <b>{pnl_str} USD</b>"
        )

    open_pos = count_open_positions()
    lines.append(f"\nВідкритих позицій: {open_pos}")

    await send_info_message("\n".join(lines))


async def daily_report_scheduler():
    """Надсилає звіт щодня о 8:00 Київського часу."""
    from zoneinfo import ZoneInfo
    from datetime import timedelta

    kyiv_tz = ZoneInfo("Europe/Kyiv")
    logger.info("Планувальник щоденного звіту запущено (8:00 Київського часу)")

    while True:
        now = datetime.now(kyiv_tz)
        next_8 = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now >= next_8:
            next_8 += timedelta(days=1)
        wait_sec = (next_8 - now).total_seconds()
        logger.info("Наступний звіт через %.0f хвилин", wait_sec / 60)
        await asyncio.sleep(wait_sec)
        try:
            await send_daily_report()
        except Exception as e:
            logger.error("Помилка надсилання денного звіту: %s", e)


async def start_telegram_polling():
    if bot:
        logger.info("Запуск Telegram бота (polling)...")
        await dp.start_polling(bot)
