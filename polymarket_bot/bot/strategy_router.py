"""
Strategy router — routes raw signals to matching strategies.
Each strategy independently decides to trade or skip.
"""

import asyncio
import logging
import os
from pathlib import Path

from bot.strategies import get_enabled_strategies

logger = logging.getLogger(__name__)

# DB base path
_DB_DIR = Path(__file__).parent.parent / "db"


def get_db_path(strategy_id: str, mode: str = "live") -> str:
    """Return DB path for strategy: db/live_{strategy_id}.db"""
    _DB_DIR.mkdir(exist_ok=True)
    return str(_DB_DIR / f"{mode}_{strategy_id}.db")


async def route_signal(
    signal: dict,
    scanner,
    execution_clients: dict,
    cooldowns: dict,
    cooldown_seconds: float = 60,
) -> None:
    """
    Route a raw signal to all matching strategies.

    signal: dict with all indicator data (confluence, votes, gap, atr, etc.)
    scanner: Scanner instance (for CLOB fetch)
    execution_clients: {wallet_key: ExecutionClient}
    cooldowns: {strategy_id_market_direction: timestamp} — shared mutable dict
    """
    import time
    from bot.storage import save_signal as _save_signal
    from bot.storage import init_db as _init_db
    from bot.position_manager import init_positions_table as _init_pos

    now = time.time()
    asset = signal.get("asset", "BTC").upper()
    direction = signal.get("direction", "UP")
    market_id = signal.get("market_id", "")

    for strategy in get_enabled_strategies():
        sid = strategy["id"]

        # Asset check
        if asset not in strategy["assets"]:
            continue

        # Filter check
        filter_fn = strategy.get("filter")
        if filter_fn is None:
            continue
        filter_result = filter_fn(signal)
        if filter_result is None:
            continue

        # Cooldown per strategy+market+direction
        cool_key = f"{sid}_{market_id}_{direction}"
        last_time = cooldowns.get(cool_key, 0)
        if now - last_time < cooldown_seconds:
            continue

        # Position limit per strategy+asset
        db_path = get_db_path(sid)
        try:
            from bot.position_manager import get_open_positions_from_db
            open_pos = get_open_positions_from_db(db_path)
            asset_open = sum(
                1 for p in open_pos
                if asset.lower() in (p.get("market_slug") or "").lower()
            )
            if asset_open >= strategy["max_positions_per_asset"]:
                logger.debug("Strategy %s: position limit for %s", sid, asset)
                continue
        except Exception:
            pass

        # Determine stake
        if filter_result.get("stake") == "min":
            stake = strategy["min_stake_usd"]
        else:
            stake = strategy["stake_usd"]

        # Prepare signal for this strategy
        sig = {**signal}
        sig["_strategy_id"] = sid
        sig["_strategy_name"] = strategy["name"]
        sig["_stake_usd"] = stake
        sig["_db_path"] = db_path
        sig["_wallet_key"] = strategy["wallet_key"]
        sig["_telegram_token_key"] = strategy["telegram_token_key"]
        sig["_telegram_chat_key"] = strategy["telegram_chat_key"]

        # Save signal to strategy-specific DB
        try:
            _init_db(db_path)
            _init_pos(db_path)
            sig_id = _save_signal(sig, db_path=db_path)
            if not sig_id:
                continue
        except Exception as e:
            logger.error("Strategy %s save error: %s", sid, e)
            continue

        # Execute
        cooldowns[cool_key] = now
        logger.info(
            "[%s] Signal #%s %s %s | stake=$%.2f | %s",
            strategy["name"], sig_id, asset, direction, stake, market_id[:12],
        )

        # Send alert + execute (async)
        asyncio.create_task(
            _execute_strategy_signal(sig_id, sig, strategy, execution_clients)
        )


async def _execute_strategy_signal(
    signal_id: int,
    signal: dict,
    strategy: dict,
    execution_clients: dict,
) -> None:
    """Execute signal for a specific strategy."""
    from bot.telegram_bot import send_alert_for_strategy

    try:
        await send_alert_for_strategy(signal_id, signal, strategy, execution_clients)
    except Exception as e:
        logger.error("[%s] Execute error #%s: %s", strategy["id"], signal_id, e)
