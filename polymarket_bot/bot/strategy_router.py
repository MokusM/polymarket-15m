"""
Strategy router — routes raw signals to matching strategies.
Each strategy independently decides to trade or skip.
All strategies share one DB (live.db) with strategy_id column.
"""

import asyncio
import logging
import time

from bot.strategies import get_enabled_strategies

logger = logging.getLogger(__name__)

def _wallet_for_position(pos: dict) -> str:
    """Resolve wallet key for an open position by its strategy_id."""
    sid = pos.get("strategy_id") or ""
    if sid:
        strat = next((s for s in get_enabled_strategies() if s["id"] == sid), None)
        if strat:
            return strat["wallet_key"]
    return "POLYMARKET_PRIVATE_KEY"


# Per-strategy lock to prevent race conditions on position limit checks
_strategy_locks: dict[str, asyncio.Lock] = {}


def _get_lock(strategy_id: str) -> asyncio.Lock:
    if strategy_id not in _strategy_locks:
        _strategy_locks[strategy_id] = asyncio.Lock()
    return _strategy_locks[strategy_id]


async def route_signal(
    signal: dict,
    scanner,
    execution_clients: dict,
    cooldowns: dict,
    cooldown_seconds: float = 60,
) -> None:
    """
    Route a raw signal to all matching strategies.
    Each strategy saves to the same DB with strategy_id tag.
    """
    from bot.storage import save_signal as _save_signal
    from bot.position_manager import get_open_positions

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

        # Cooldown per market_id (shared across strategies on same wallet)
        # Prevents spamming the same market from multiple strategies
        wallet_key = strategy["wallet_key"]
        cool_key = f"{wallet_key}_{market_id}"
        last_time = cooldowns.get(cool_key, 0)
        if now - last_time < cooldown_seconds:
            continue

        # Locked: position check + save + execute (prevents race condition)
        lock = _get_lock(sid)
        async with lock:
            try:
                open_pos = get_open_positions()

                # Check 1: no duplicate on same market_id for same wallet
                wallet_key = strategy["wallet_key"]
                market_already_open = any(
                    p for p in open_pos
                    if p.get("market_id") == market_id
                    and _wallet_for_position(p) == wallet_key
                )
                if market_already_open:
                    logger.debug("Strategy %s: market %s already open on wallet %s", sid, market_id[:12], wallet_key)
                    continue

                # Check 2: position limit per strategy+asset
                asset_open = sum(
                    1 for p in open_pos
                    if asset.lower() in (p.get("market_slug") or "").lower()
                    and p.get("strategy_id") == sid
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
            sig["_wallet_key"] = strategy["wallet_key"]
            sig["_telegram_token_key"] = strategy["telegram_token_key"]
            sig["_telegram_chat_key"] = strategy["telegram_chat_key"]

            # Save to shared DB with strategy_id
            try:
                sig_id = _save_signal(sig)
                if not sig_id:
                    continue
            except Exception as e:
                logger.error("Strategy %s save error: %s", sid, e)
                continue

            cooldowns[cool_key] = now
            logger.info(
                "[%s] Signal #%s %s %s | stake=$%.2f | %s",
                strategy["name"], sig_id, asset, direction, stake, market_id[:12],
            )

            # Execute (inside lock to prevent duplicate position opens)
            await _execute_strategy_signal(sig_id, sig, strategy, execution_clients)


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
