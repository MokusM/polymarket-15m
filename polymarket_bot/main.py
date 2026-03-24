import asyncio
import logging
import os

# Налаштування логування формату
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

from bot.scanner import Scanner
from bot.settlement import settle_markets
from bot.telegram_bot import start_telegram_polling
from bot.storage import init_db

async def main():
    logger.info("Ініціалізація бази даних SQLite...")
    init_db()

    scanner = Scanner()
    
    logger.info("🚀 Запуск Polymarket BTC 15m Scanner Bot...")
    
    # Запускаємо всі 3 фонові задачі паралельно:
    # 1. Цикл сканування та генерації сигналів
    # 2. Цикл перевірки закриття ринків (Settlement)
    # 3. Слухання подій Telegram для Inline кнопок Approve/Reject
    tasks = [
        asyncio.create_task(scanner.run()),
        asyncio.create_task(settle_markets()),
        asyncio.create_task(start_telegram_polling())
    ]
    
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("Отримано сигнал зупинки бота (KeyboardInterrupt).")
    except Exception as e:
        logger.error(f"Критична помилка виконання: {e}", exc_info=True)
    finally:
        await scanner.close()
        logger.info("Бот успішно зупинено.")

if __name__ == "__main__":
    # Захист запуску asyncio на Windows
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    asyncio.run(main())
