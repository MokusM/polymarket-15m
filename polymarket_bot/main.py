import asyncio
import logging
import logging.handlers
import os
import sys

_log_file = os.path.join(os.path.dirname(__file__), "bot.log")
_file_handler = logging.handlers.RotatingFileHandler(
    _log_file, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), _file_handler],
)
logger = logging.getLogger(__name__)

from bot.config import LIVE_TRADING, DB_PATH_TEST, DB_PATH_LIVE
from bot.scanner import Scanner
from bot.settlement import settle_markets
from bot.telegram_bot import start_telegram_polling, set_execution_client, set_scanner, daily_report_scheduler
from bot.storage import init_db, init_pending_orders_table
from bot.position_manager import init_positions_table


async def main():
    logger.info("Python: %s", sys.executable)

    # Remove old DB if exists
    old_db = os.path.join(os.path.dirname(__file__), "signals.db")
    if os.path.exists(old_db):
        os.remove(old_db)
        logger.info("Видалено стару БД: signals.db")

    # Init both databases
    logger.info("Ініціалізація баз даних (test.db + live.db)...")
    init_db(DB_PATH_TEST)
    init_db(DB_PATH_LIVE)
    init_positions_table(DB_PATH_TEST)
    init_positions_table(DB_PATH_LIVE)
    init_pending_orders_table(DB_PATH_TEST)
    init_pending_orders_table(DB_PATH_LIVE)

    execution_client = None
    if LIVE_TRADING:
        from bot.execution_client import ExecutionClient
        execution_client = ExecutionClient()
        set_execution_client(execution_client)
        if execution_client.ready:
            logger.info("🟢 LIVE TRADING увімкнено")
        else:
            reason = getattr(execution_client, "not_ready_reason", None) or "невідомо"
            logger.warning(
                "⚠️ LIVE_TRADING=true, але ExecutionClient не готовий. Причина: %s",
                reason,
            )
    else:
        logger.info("📋 Paper trading mode")

    scanner = Scanner()
    set_scanner(scanner)
    logger.info("🚀 Запуск Polymarket BTC 15m Scanner Bot...")

    tasks = [
        asyncio.create_task(scanner.run()),
        asyncio.create_task(settle_markets()),
        asyncio.create_task(start_telegram_polling()),
        asyncio.create_task(daily_report_scheduler()),
    ]

    if LIVE_TRADING and execution_client and execution_client.ready:
        from bot.position_manager import monitor_positions_loop, monitor_pending_orders_loop
        tasks.append(asyncio.create_task(monitor_positions_loop(execution_client)))
        tasks.append(asyncio.create_task(monitor_pending_orders_loop(execution_client)))

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except KeyboardInterrupt:
        logger.info("Зупинка бота (KeyboardInterrupt).")
    finally:
        for t in tasks:
            t.cancel()
        await scanner.close()
        logger.info("Бот зупинено.")


if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
