import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import TELEGRAM_TOKEN, CHAT_ID, STAKE_USD
from bot.storage import update_decision, update_telegram_message_id
from bot.state import state
from bot.alert_text import format_signal_alert_html

logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None
dp = Dispatcher()

@dp.message(Command("mode"))
async def cmd_mode(message: types.Message):
    """Обробник команди /mode для зміни жорсткості фільтрів просто з Telegram."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🟢 Light", callback_data="set_mode|light")
    builder.button(text="🟡 Medium", callback_data="set_mode|medium")
    builder.button(text="🔴 Strict", callback_data="set_mode|strict")
    builder.button(text="🔥 Test", callback_data="set_mode|test")
    builder.adjust(3, 1)
    
    text = (
        f"Поточний режим: <b>{state.mode.upper()}</b>\n\n"
        "🟢 <b>Light</b>: Дуже гнучкі налаштування, багато сигналів.\n"
        "🟡 <b>Medium</b>: Збалансовані умови (рекомендовано).\n"
        "🔴 <b>Strict</b>: Снайперський підхід, жорсткі відхилення та RSI.\n"
        "🔥 <b>Test</b>: Абсолютно всі ринки пропускаються, щоб перевірити зв'язок.\n\n"
        "Виберіть новий рівень складності (діє миттєво):"
    )
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(lambda c: c.data and c.data.startswith('set_mode|'))
async def process_mode_change(callback_query: types.CallbackQuery):
    """Обробка вибору нового режиму."""
    new_mode = callback_query.data.split('|')[1]
    state.mode = new_mode
    
    text = f"✅ Режим роботи бота успішно змінено на: <b>{new_mode.upper()}</b>. Всі нові сигнали будуть фільтруватись за цією стратегією."
    try:
        await bot.edit_message_text(
            chat_id=callback_query.message.chat.id,
            message_id=callback_query.message.message_id,
            text=text,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Помилка реакції на кнопку mode: {e}")
        
    await bot.answer_callback_query(callback_query.id)

async def send_alert(signal_id: int, signal: dict):
    if not bot or not CHAT_ID:
        logger.warning("Telegram не налаштований. Пропуск надсилання алерта.")
        return

    text = format_signal_alert_html(signal, state.mode, STAKE_USD)

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Approve", callback_data=f"decision|{signal_id}|approve")
    builder.button(text="❌ Reject", callback_data=f"decision|{signal_id}|reject")
    builder.button(text="⏭ Skip", callback_data=f"decision|{signal_id}|skip")
    builder.adjust(3)

    try:
        msg = await bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        update_telegram_message_id(signal_id, msg.message_id)
    except Exception as e:
        logger.error(f"Помилка надсилання Telegram повідомлення: {e}")

@dp.callback_query(lambda c: c.data and c.data.startswith('decision|'))
async def process_decision(callback_query: types.CallbackQuery):
    parts = callback_query.data.split('|')
    if len(parts) == 3:
        _, sig_id_str, action = parts
        try:
            signal_id = int(sig_id_str)
            update_decision(signal_id, action)
            
            emojis = {"approve": "✅", "reject": "❌", "skip": "⏭"}
            action_text = f"Вибрано: {emojis.get(action, '')} {action.capitalize()}"
            
            original_text = callback_query.message.html_text
            new_text = f"{original_text}\n\n<b>{action_text}</b>"
            
            await bot.edit_message_text(
                chat_id=callback_query.message.chat.id,
                message_id=callback_query.message.message_id,
                text=new_text,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Помилка обробки decision: {e}")

    try:
        await bot.answer_callback_query(callback_query.id)
    except Exception:
        pass

async def edit_signal_result(
    telegram_message_id: int,
    alert_html: str,
    decision: str,
    result: str,
    pnl: float,
):
    """Редагує оригінальне повідомлення сигналу, додаючи результат settlement."""
    if not bot or not CHAT_ID or not telegram_message_id:
        return

    result_icon = "\u2705" if result == "WIN" else "\u274c"
    pnl_sign = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"

    decision_icons = {"approve": "\u2705", "reject": "\u274c", "skip": "\u23ed"}
    decision_line = ""
    if decision and decision != "pending":
        d_icon = decision_icons.get(decision, "")
        decision_line = f"\n<b>\u0412\u0438\u0431\u0440\u0430\u043d\u043e: {d_icon} {decision.capitalize()}</b>"

    separator = "\u2500" * 18
    result_line = (
        f"\n\n{separator}\n"
        f"{result_icon} <b>{result}</b> | PnL: <b>{pnl_sign} USD</b>"
    )

    new_text = (alert_html or "") + decision_line + result_line

    try:
        await bot.edit_message_text(
            chat_id=CHAT_ID,
            message_id=telegram_message_id,
            text=new_text,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Помилка редагування результату #%s: %s", telegram_message_id, e)


async def send_info_message(text: str):
    """Інформаційне повідомлення без кнопок (сесії, попередження)."""
    if not bot or not CHAT_ID:
        return
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML")
    except Exception as e:
        logger.error("Помилка інфо-повідомлення: %s", e)


async def start_telegram_polling():
    if bot:
        logger.info("Запуск Telegram бота (polling)...")
        await dp.start_polling(bot)
