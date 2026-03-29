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
    TELEGRAM_ENABLED,
)
from bot.execution_client import trade_timestamp
from bot.storage import (
    get_recent_signals,
    mark_signal_live_no_position,
    mark_signal_live_pending,
    save_pending_order,
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


def _format_signal_history_html(idx: int, sig: dict) -> str:
    direction = sig.get("direction", "?")
    result = sig.get("result")
    pnl = sig.get("pnl")
    contract_price = sig.get("contract_price") or 0.0
    stake_usd = sig.get("stake_usd") or 0.0
    title_raw = (sig.get("market_title") or "").strip()
    title = html.escape(title_raw[:80]) if title_raw else "—"
    ts_raw = sig.get("timestamp", "")
    gap = sig.get("gap")
    confluence = sig.get("confluence")

    # Direction icon
    dir_icon = "⬆️" if direction == "UP" else "⬇️"

    # Result with color circle
    result_up = (result or "").upper()
    if result_up == "WIN":
        status_icon = "🟢"
        result_line = f"WIN  <b>+${pnl:.2f}</b>" if pnl is not None else "WIN"
    elif result_up == "LOSS":
        status_icon = "🔴"
        result_line = f"LOSS  <b>-${abs(pnl):.2f}</b>" if pnl is not None else "LOSS"
    elif result_up == "CLOSED_EARLY":
        status_icon = "🔴"
        result_line = html.escape(result)
    elif result is not None:
        status_icon = "🟡"
        result_line = html.escape(result)
    else:
        status_icon = "🟡"
        result_line = "pending"

    # Details
    details = []
    if gap is not None:
        details.append(f"gap={gap:+.0f}$")
    if confluence is not None:
        details.append(f"conf={confluence}/5")
    details_str = "  ·  ".join(details)

    parts = [
        f"{status_icon} <b>#{sig['id']}</b>  {dir_icon} <b>{direction}</b>  @{_history_price_txt(contract_price)}  ·  {result_line}",
        f"📌 {title}",
        f"🕑 <code>{html.escape(str(ts_raw)[:16])}</code>  ·  ставка ${stake_usd:.2f}",
    ]
    if details_str:
        parts.append(f"<i>{html.escape(details_str)}</i>")
    return _SEP + "\n".join(parts)


bot = Bot(token=TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None
dp = Dispatcher()

_execution_client = None
_scanner = None
_pending_signals: dict[int, dict] = {}


def set_execution_client(client):
    global _execution_client
    _execution_client = client


def set_scanner(scanner):
    global _scanner
    _scanner = scanner


def store_pending_signal(signal_id: int, signal: dict):
    _pending_signals[signal_id] = signal


# ── Commands ──

@dp.message(Command("diagnose"))
async def cmd_diagnose(message: types.Message):
    """Показати стан всіх фільтрів на основі останніх даних сканера."""
    from bot.signals import diagnose_signals

    try:
        if _scanner is None or _scanner.last_df.empty:
            await message.answer("⏳ Сканер ще не запустив перший цикл. Зачекай кілька секунд.")
            return

        df = _scanner.last_df
        markets = _scanner.last_markets

        if not markets:
            await message.answer(
                "❌ Активних BTC 15m маркетів не знайдено.\n"
                "<i>Ринок відкривається кожні 15 хв.</i>",
                parse_mode="HTML",
            )
            return

        from bot.config import OBI_MIN_RATIO, OBI_LEVELS, CLOB_SPREAD_MAX, CONTRACT_PRICE_HIGH_MIN, GAP_STRICT_USD

        # Fetch OBI with more levels for stable reading
        obi = None
        try:
            obi = await _scanner.exchange.get_order_book_imbalance(levels=20)
        except Exception:
            pass

        parts = []
        for market in markets:
            title = market.get("title") or market.get("market_id", "?")
            report = diagnose_signals(market, df)

            obi_lines = []

            # OBI line
            if obi is not None and OBI_MIN_RATIO > 0:
                direction_guess = "UP" if market.get("price_yes", 0.5) > 0.5 else "DOWN"
                obi_ok = obi >= OBI_MIN_RATIO if direction_guess == "UP" else obi <= (1.0 / OBI_MIN_RATIO)
                obi_needed = f"≥{OBI_MIN_RATIO}" if direction_guess == "UP" else f"≤{1.0/OBI_MIN_RATIO:.2f}"
                obi_lines.append(
                    f"{'✅' if obi_ok else '❌'} OBI (Binance top-20): <b>{obi:.3f}</b> (потрібно {obi_needed} для {direction_guess})"
                )
            elif obi is not None:
                obi_lines.append(f"➖ OBI (Binance top-20): <b>{obi:.3f}</b> (фільтр вимкнено)")

            # CLOB spread — check both tokens, pick the one that matches Gamma price
            gamma_yes = market.get("price_yes", 0.5)
            for token_label, token_id in [("YES", market.get("token_yes_id", "")), ("NO", market.get("token_no_id", ""))]:
                if not token_id:
                    continue
                try:
                    clob_ask, clob_bid = await _scanner._fetch_clob_best_prices(token_id)
                    if clob_ask <= 0 or clob_bid <= 0:
                        obi_lines.append(f"➖ CLOB {token_label}: немає даних")
                        continue
                    spread = clob_ask - clob_bid
                    # Spread > 0.20 = empty/illiquid book (nominal 0.01/0.99 orders)
                    if spread > 0.20:
                        gamma_ref = gamma_yes if token_label == "YES" else (1 - gamma_yes)
                        obi_lines.append(f"⚠️ CLOB {token_label}: порожній стакан (Gamma {gamma_ref:.2f})")
                        continue
                    spread_ok = spread <= CLOB_SPREAD_MAX
                    obi_lines.append(
                        f"{'✅' if spread_ok else '❌'} CLOB {token_label}: bid <b>{clob_bid:.2f}</b> ask <b>{clob_ask:.2f}</b>"
                        f" спред <b>{spread:.3f}</b> (макс {CLOB_SPREAD_MAX})"
                    )
                    if clob_ask > CONTRACT_PRICE_HIGH_MIN:
                        obi_lines.append(
                            f"  ⚠️ Ask {clob_ask:.2f} &gt; {CONTRACT_PRICE_HIGH_MIN} → потрібен GAP ≥ {GAP_STRICT_USD:.0f}$"
                        )
                except Exception:
                    pass

            extra = "\n" + "\n".join(obi_lines) if obi_lines else ""
            parts.append(f"<b>📌 {html.escape(str(title)[:60])}</b>\n{report}{extra}")

        text = "\n\n─────────────────────\n\n".join(parts)
        if len(text) > 4000:
            text = text[:4000] + "\n<i>...обрізано</i>"
        await message.answer(text, parse_mode="HTML")

    except Exception as e:
        logger.error("cmd_diagnose error: %s", e, exc_info=True)
        await message.answer(f"❌ Помилка діагностики:\n<code>{html.escape(str(e))}</code>", parse_mode="HTML")


@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    """Список всіх доступних команд."""
    text = (
        "🤖 <b>Команди бота</b>\n"
        "\n"
        "📋 <b>Інформація</b>\n"
        "/status — повний статус бота (режим, live, баланс, позиції)\n"
        "/diagnose — перевірити всі фільтри для поточного маркету\n"
        "/positions — відкриті позиції\n"
        "/balance — баланс Polymarket USDC\n"
        "/history — остання угоди з CLOB API\n"
        "\n"
        "⚙️ <b>Управління</b>\n"
        "/buy — ручна купівля UP/DOWN для поточного маркету\n"
        "/mode — змінити режим (Light / Medium / Strict / Test)\n"
        "/reset — скинути circuit breaker і відновити live trading\n"
        "\n"
        "📖 <b>Режими сканера</b>\n"
        "🟢 <b>Light</b> — гнучкі фільтри, більше сигналів\n"
        "🟡 <b>Medium</b> — збалансовані (рекомендовано)\n"
        "🔴 <b>Strict</b> — снайперський підхід, мінімум сигналів\n"
        "🔥 <b>Test</b> — всі фільтри вимкнено, live trading недоступний\n"
        "\n"
        "/list — ця довідка"
    )
    await message.answer(text, parse_mode="HTML")


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


@dp.message(Command("buy"))
async def cmd_buy(message: types.Message):
    """Ручна купівля: показує поточний маркет і кнопки UP/DOWN."""
    if _scanner is None or _scanner.last_df.empty:
        await message.answer("⏳ Сканер ще не запустив перший цикл.")
        return

    markets = _scanner.last_markets
    if not markets:
        await message.answer("❌ Немає активних BTC 15m маркетів.")
        return

    market = markets[0]
    df = _scanner.last_df
    last = df.iloc[-1]
    btc_price = float(last.get("close", 0))

    from datetime import timezone as _tz
    from bot.signals import _binance_open_at_polymarket_window_start
    time_left_min = 0.0
    end_date_str = market.get("end_date_iso")
    if end_date_str:
        try:
            dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            time_left_min = (dt - datetime.now(_tz.utc)).total_seconds() / 60.0
        except Exception:
            pass

    yes_ask = no_ask = 0.0
    yes_tid = market.get("token_yes_id", "")
    no_tid = market.get("token_no_id", "")
    if yes_tid:
        yes_ask, _ = await _scanner._fetch_clob_best_prices(yes_tid)
    if no_tid:
        no_ask, _ = await _scanner._fetch_clob_best_prices(no_tid)

    title = market.get("title") or market.get("market_id", "?")
    mid = str(market.get("market_id", ""))

    text = (
        f"📈 <b>Ручна купівля</b>\n"
        f"📌 {html.escape(str(title)[:60])}\n"
        f"⏱ Час: <b>{time_left_min:.1f} хв</b>  ·  BTC: <b>${btc_price:,.0f}</b>\n"
        f"CLOB  YES ask: <b>{yes_ask:.2f}</b>  ·  NO ask: <b>{no_ask:.2f}</b>\n\n"
        f"Вибери напрямок:"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="⬆️ BUY UP", callback_data=f"manual_buy|{mid}|UP")
    builder.button(text="⬇️ BUY DOWN", callback_data=f"manual_buy|{mid}|DOWN")
    builder.adjust(2)
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.callback_query(lambda c: c.data and c.data.startswith("manual_buy|"))
async def process_manual_buy(callback_query: types.CallbackQuery):
    parts = callback_query.data.split("|")
    if len(parts) != 3:
        await bot.answer_callback_query(callback_query.id)
        return

    _, market_id, direction = parts

    try:
        await bot.answer_callback_query(callback_query.id)

        if _scanner is None or _scanner.last_df.empty:
            await bot.send_message(callback_query.message.chat.id, "⏳ Немає даних сканера.")
            return

        markets = _scanner.last_markets
        market = next((m for m in markets if str(m.get("market_id", "")) == market_id), None)
        if not market:
            market = markets[0] if markets else None
        if not market:
            await bot.send_message(callback_query.message.chat.id, "❌ Маркет не знайдено.")
            return

        df = _scanner.last_df
        last = df.iloc[-1]
        btc_price = float(last.get("close", 0))

        from datetime import timezone as _tz
        time_left_min = 5.0
        end_date_str = market.get("end_date_iso")
        if end_date_str:
            try:
                dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_tz.utc)
                time_left_min = max(0.5, (dt - datetime.now(_tz.utc)).total_seconds() / 60.0)
            except Exception:
                pass

        # Fetch CLOB ask for the selected side
        token_id = market.get("token_yes_id") if direction == "UP" else market.get("token_no_id")
        clob_ask = 0.0
        if token_id:
            clob_ask, _ = await _scanner._fetch_clob_best_prices(token_id)

        # Fallback to Gamma price
        if clob_ask <= 0:
            clob_ask = market.get("price_yes") if direction == "UP" else market.get("price_no")
            clob_ask = float(clob_ask or 0.5)

        slug = market.get("market_slug") or ""
        neg_risk = bool(market.get("neg_risk", False))
        market_title = market.get("title") or market.get("question") or ""

        from bot.signals import _binance_open_at_polymarket_window_start
        event_start_iso = market.get("event_start_time")
        start_price = _binance_open_at_polymarket_window_start(df, event_start_iso)
        if start_price is None:
            start_price = float(df.iloc[-15]["open"]) if len(df) >= 15 else btc_price
        delta = btc_price - start_price

        signal = {
            "direction": direction,
            "market_id": market_id,
            "market_slug": slug,
            "neg_risk": neg_risk,
            "market_title": market_title,
            "contract_price": clob_ask,
            "time_left": round(time_left_min, 1),
            "start_price": start_price,
            "current_price": btc_price,
            "delta": round(delta, 2),
            "delta_percent": round((delta / start_price * 100) if start_price > 0 else 0, 3),
            "gap": round(delta, 2),
            "ptb": start_price,
            "confluence": 0,
            "votes": {},
            "rsi_1m": float(last.get("rsi_1m", 50) or 50),
            "rsi_3m": None,
            "rsi_5m": None,
            "ema_position": "manual",
            "volume_state": str(last.get("volume_state", "normal")),
            "atr": round(float(last.get("atr") or 0), 2),
            "atr_zone": str(last.get("atr_zone", "golden")),
            "chg_1h": round(float(last.get("chg_1h") or 0), 3),
            "obi": 1.0,
            "_manual": True,
        }

        from bot.storage import save_signal, update_decision as _upd_dec
        from bot.risk import calculate_stake

        sig_id = save_signal(signal)
        if not sig_id:
            await bot.send_message(callback_query.message.chat.id, "❌ Помилка збереження сигналу в БД.")
            return

        _upd_dec(sig_id, "approve")

        side = "YES" if direction == "UP" else "NO"
        dir_icon = "⬆️" if direction == "UP" else "⬇️"

        if not (state.is_live_allowed and _execution_client and _execution_client.ready):
            await bot.send_message(
                callback_query.message.chat.id,
                f"{dir_icon} <b>Ручний сигнал #{sig_id} збережено (paper)</b>\n"
                f"BUY {side} @ {clob_ask:.2f}  ·  {time_left_min:.1f} хв\n"
                f"<i>Settlement запише результат після закриття маркету.</i>",
                parse_mode="HTML",
            )
            return

        # Live: force risk with positive edge so _execute_live_order proceeds
        bankroll = None
        try:
            bankroll = await _execution_client.get_balance()
        except Exception:
            pass
        risk = calculate_stake(signal, bankroll=bankroll)
        risk["edge"] = max(risk.get("edge", 0), 0.01)
        risk["stake_usd"] = 1.0

        store_pending_signal(sig_id, {**signal, "_risk": risk})
        order_text = await _execute_live_order(sig_id, skip_min_size=True)

        await bot.send_message(
            callback_query.message.chat.id,
            f"{dir_icon} <b>Ручна купівля #{sig_id}</b>\n{order_text}",
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error("process_manual_buy error: %s", e, exc_info=True)
        await bot.send_message(
            callback_query.message.chat.id,
            f"❌ Помилка: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    """Повний статус бота."""
    from bot.position_manager import get_open_positions

    client_ready = bool(_execution_client and _execution_client.ready)
    is_live = state.is_live_allowed and client_ready
    cb_active = state.circuit_breaker_active
    positions = get_open_positions()

    # ── Core status ──
    live_icon = "🟢" if is_live else "🔴"
    client_icon = "🟢" if client_ready else "🔴"
    cb_icon = "🚨" if cb_active else "✅"
    approve_icon = "🟢" if AUTO_APPROVE_LIVE else "🔴"

    lines = [
        f"🤖 <b>Bot Status — {state.mode.upper()}</b>",
        "",
        f"{live_icon} Live trading: <b>{'ON' if is_live else 'OFF'}</b>  ·  {client_icon} Client: <b>{'READY' if client_ready else 'NOT READY'}</b>",
        f"{approve_icon} Auto-approve: <b>{'ON' if AUTO_APPROVE_LIVE else 'OFF'}</b>  ·  {cb_icon} Circuit breaker: <b>{'ACTIVE' if cb_active else 'OK'}</b>",
        f"📉 Loss streak: <b>{state.consecutive_losses}</b>",
        f"📂 Open positions: <b>{len(positions)}/{MAX_OPEN_POSITIONS}</b>",
    ]

    if client_ready:
        try:
            balance = await _execution_client.get_balance()
            lines.insert(2, f"💰 Balance: <b>${balance:.2f} USDC</b>")
        except Exception:
            pass

    # ── Open positions detail ──
    if positions:
        lines.append("")
        lines.append("──────────────────")
        lines.append("📍 <b>Відкриті позиції</b>")
        for pos in positions:
            direction = pos.get("direction", "?")
            dir_icon = "⬆️" if direction == "UP" else "⬇️"
            slug = html.escape((pos.get("market_slug") or "")[:50])
            entry = pos.get("entry_price", 0)
            sl = pos.get("sl_price", 0)
            shares = pos.get("remaining_shares") or pos.get("shares", 0)
            expires = (pos.get("market_expires_at") or "")[:16]
            lines.append("")
            lines.append(f"{dir_icon} <b>{direction}</b>  @{_history_price_txt(entry)}  ·  SL: {_history_price_txt(sl)}  ·  {shares:.1f} shares")
            if slug:
                lines.append(f"📌 {slug}")
            if expires:
                lines.append(f"⏱ Expires: <code>{html.escape(expires)}</code>")

    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("history"))
async def cmd_history(message: types.Message):
    """Останні сигнали з локальної БД з назвою ринку, напрямком та результатом."""
    signals = get_recent_signals(limit=CLOB_TRADE_HISTORY_LIMIT)
    if not signals:
        await message.answer("📋 Немає сигналів у локальній БД.")
        return

    wins = sum(1 for s in signals if s.get("result") == "win")
    losses = sum(1 for s in signals if s.get("result") == "loss")
    total_pnl = sum(s.get("pnl") or 0.0 for s in signals if s.get("result") in ("win", "loss"))
    pnl_sign = "+" if total_pnl >= 0 else ""

    intro = (
        f"📊 <b>Історія сигналів</b> (останні {len(signals)})\n"
        f"✅ {wins}W / ❌ {losses}L  ·  PnL: <b>{pnl_sign}${total_pnl:.2f}</b>"
    )
    blocks = [_format_signal_history_html(i, sig) for i, sig in enumerate(signals, start=1)]
    text = intro + "".join(blocks)

    if len(text) <= 4096:
        await message.answer(text, parse_mode="HTML")
        return

    half = max(1, len(blocks) // 2)
    await message.answer(intro + "".join(blocks[:half]), parse_mode="HTML")
    await message.answer(
        f"<b>Продовження ({half + 1}–{len(signals)})</b>" + "".join(blocks[half:]),
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
    if not TELEGRAM_ENABLED:
        update_decision(signal_id, "approve")
        logger.debug("Telegram вимкнено — сигнал #%s авто-approve (DB only)", signal_id)
        return
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
        # Edge ≤ 0 — ордер не відкриється, Telegram не турбуємо
        mark_signal_live_no_position(signal_id)
        update_decision(signal_id, "approve")
        logger.info("Сигнал #%s: edge ≤ 0 в live режимі — Telegram не надсилається", signal_id)
        return
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


async def _execute_live_order(signal_id: int, skip_min_size: bool = False) -> str:
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
        market_slug=None if skip_min_size else (slug or None),
        market_id=None if skip_min_size else (mid or None),
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
        oid = str(result.get("orderID", ""))
        ep = float(result.get("_effective_price", cp))
        shares_pending = float(result.get("_order_size", 0)) or round(stake / ep, 2)
        time_left_min = float(signal.get("time_left", 5) or 5)
        from datetime import timedelta
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=time_left_min * 60)
        ).strftime("%Y-%m-%d %H:%M:%S")
        await asyncio.to_thread(
            save_pending_order,
            signal_id, oid, token_id, market_id, slug or "",
            direction, ep, shares_pending, stake, neg_risk, expires_at,
        )
        await asyncio.to_thread(mark_signal_live_pending, signal_id)
        return (
            f"⏳ <b>Ліміт у стакані</b> — очікує fill\n"
            f"orderID: <code>{html.escape(oid)}</code> | ціна: {ep:.2f}\n"
            f"Бот перевірить fill кожні 5с до {expires_at[11:16]} UTC"
        )

    ep = float(result.get("_effective_price", cp)) if isinstance(result, dict) else cp
    shares = float(result.get("_order_size", 0)) if isinstance(result, dict) else 0.0
    if shares <= 0 and ep > 0:
        shares = round(stake / ep, 2)
    stake_eff = round(shares * ep, 2)

    time_left_min = float(signal.get("time_left", 10) or 10)
    from datetime import timedelta
    mkt_expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=time_left_min * 60)
    ).strftime("%Y-%m-%d %H:%M:%S")

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
        market_expires_at=mkt_expires_at,
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
    if not TELEGRAM_ENABLED or not bot or not CHAT_ID:
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
    if not TELEGRAM_ENABLED:
        logger.info("Telegram вимкнено (TELEGRAM_ENABLED=false) — polling не запускається.")
        return
    if bot:
        logger.info("Запуск Telegram бота (polling)...")
        await dp.start_polling(bot)
