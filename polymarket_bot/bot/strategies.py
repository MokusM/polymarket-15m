"""
Multi-strategy configuration.
One scanner → multiple strategies → each with own filter/stake/wallet/DB.
"""

import os
import logging

logger = logging.getLogger(__name__)


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _confluence_filter(signal: dict) -> dict | None:
    """conf>=4, non-stabilization → full stake; conf>=3 → min stake."""
    conf = signal.get("confluence", 0)
    vol = signal.get("volume_state", "")
    if conf >= 4 and vol != "stabilization":
        return {"stake": "full"}
    if conf >= 4 and vol == "stabilization":
        return {"stake": "min"}
    if conf >= 3:
        return {"stake": "min"}
    return None


def _data_collector_filter(signal: dict) -> dict | None:
    """conf>=3 → always min stake."""
    conf = signal.get("confluence", 0)
    if conf >= 3:
        return {"stake": "min"}
    return None


def _delta_pct_filter(signal: dict) -> dict | None:
    """delta_pct >= 0.10% + cc >= 2."""
    dp = abs(signal.get("delta_percent", 0))
    cc = abs(signal.get("consecutive_closes", 0))
    if dp >= 0.10 and cc >= 2:
        return {"stake": "full"}
    return None


# Strategy registry
STRATEGIES = [
    {
        "id": "confluence",
        "name": "Confluence",
        "enabled": _env("STRAT_CONFLUENCE_ENABLED", "true").lower() in ("1", "true"),
        "filter": _confluence_filter,
        "assets": ["BTC"],
        "stake_usd": float(_env("STRAT_CONFLUENCE_STAKE", "5")),
        "min_stake_usd": float(_env("STRAT_CONFLUENCE_MIN_STAKE", "1")),
        "wallet_key": _env("STRAT_CONFLUENCE_WALLET", "POLYMARKET_PRIVATE_KEY"),
        "telegram_token_key": _env("STRAT_CONFLUENCE_TG_TOKEN", "TELEGRAM_TOKEN"),
        "telegram_chat_key": _env("STRAT_CONFLUENCE_TG_CHAT", "CHAT_ID"),
        "db_label": "confluence",
        "max_positions_per_asset": 1,
    },
    {
        "id": "data_collector",
        "name": "Data",
        "enabled": _env("STRAT_DATA_ENABLED", "true").lower() in ("1", "true"),
        "filter": _data_collector_filter,
        "assets": ["BTC", "ETH", "SOL"],
        "stake_usd": 1,
        "min_stake_usd": 1,
        "wallet_key": _env("STRAT_DATA_WALLET", "POLYMARKET_PRIVATE_KEY"),
        "telegram_token_key": _env("STRAT_DATA_TG_TOKEN", "TELEGRAM_TOKEN"),
        "telegram_chat_key": _env("STRAT_DATA_TG_CHAT", "CHAT_ID"),
        "db_label": "data",
        "max_positions_per_asset": 1,
    },
    {
        "id": "delta_pct",
        "name": "Delta",
        "enabled": _env("STRAT_DELTA_ENABLED", "false").lower() in ("1", "true"),
        "filter": _delta_pct_filter,
        "assets": ["BTC"],
        "stake_usd": float(_env("STRAT_DELTA_STAKE", "5")),
        "min_stake_usd": 1,
        "wallet_key": _env("STRAT_DELTA_WALLET", "POLYMARKET_PRIVATE_KEY_V2"),
        "telegram_token_key": _env("STRAT_DELTA_TG_TOKEN", "TELEGRAM_TOKEN_V2"),
        "telegram_chat_key": _env("STRAT_DELTA_TG_CHAT", "CHAT_ID_V2"),
        "db_label": "delta_pct",
        "max_positions_per_asset": 1,
    },
]


def get_strategy(strategy_id: str) -> dict | None:
    return next((s for s in STRATEGIES if s["id"] == strategy_id), None)


def get_enabled_strategies() -> list[dict]:
    return [s for s in STRATEGIES if s["enabled"]]
