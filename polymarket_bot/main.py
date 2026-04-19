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
from bot.telegram_bot import start_telegram_polling, set_execution_client, set_execution_clients, set_scanner, daily_report_scheduler
from bot.storage import init_db, init_pending_orders_table, init_pending_hedges_table
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
    init_pending_hedges_table(DB_PATH_TEST)
    init_pending_hedges_table(DB_PATH_LIVE)

    execution_client = None
    execution_clients = {}
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
        # Multi-strategy: create clients per wallet key
        execution_clients["POLYMARKET_PRIVATE_KEY"] = execution_client
        # Additional wallets (V2 = delta_pct, V3 = data_collector)
        for label, key_env, funder_env in [
            ("V2", "POLYMARKET_PRIVATE_KEY_V2", "POLYMARKET_FUNDER_ADDRESS_V2"),
            ("V3", "POLYMARKET_PRIVATE_KEY_V3", "POLYMARKET_FUNDER_ADDRESS_V3"),
        ]:
            _key = os.getenv(key_env)
            if _key:
                try:
                    ec = ExecutionClient(private_key=_key, funder_address=os.getenv(funder_env))
                    if ec.ready:
                        execution_clients[key_env] = ec
                        logger.info("🟢 Wallet %s ready", label)
                    else:
                        logger.warning("⚠️ Wallet %s not ready", label)
                except Exception as e:
                    logger.warning("⚠️ Wallet %s init error: %s", label, e)
    else:
        logger.info("📋 Paper trading mode")

    # Log enabled strategies (all share live.db with strategy_id column)
    from bot.strategies import get_enabled_strategies
    for strat in get_enabled_strategies():
        logger.info("📋 Strategy '%s' enabled: %s assets, $%.0f stake",
                     strat["id"], strat["assets"], strat["stake_usd"])

    # WebSocket Binance price feed (BTC/ETH/SOL)
    from bot import ws_binance
    ws_binance.start()
    logger.info("📡 WS Binance price feed запущено (BTC/ETH/SOL)")

    set_execution_clients(execution_clients)
    scanner = Scanner(execution_clients=execution_clients)
    set_scanner(scanner)
    logger.info("🚀 Запуск Polymarket BTC 15m Scanner Bot...")

    tasks = [
        asyncio.create_task(scanner.run()),
        asyncio.create_task(settle_markets(execution_client if LIVE_TRADING else None)),
        asyncio.create_task(start_telegram_polling()),
        asyncio.create_task(daily_report_scheduler()),
    ]

    if LIVE_TRADING and execution_client and execution_client.ready:
        from bot.position_manager import monitor_positions_loop, monitor_pending_orders_loop
        tasks.append(asyncio.create_task(monitor_positions_loop(execution_client, execution_clients)))
        tasks.append(asyncio.create_task(monitor_pending_orders_loop(execution_client, execution_clients)))

    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                logger.error("Задача %d впала: %s", i, res, exc_info=res)
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
