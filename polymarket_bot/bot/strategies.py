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
    """conf>=4 only. Non-stabilization → full stake; stabilization → min stake."""
    conf = signal.get("confluence", 0)
    vol = signal.get("volume_state", "")
    if conf < 4:
        return None
    if vol == "stabilization":
        return {"stake": "min"}
    return {"stake": "full"}


def _data_collector_filter(signal: dict) -> dict | None:
    """conf>=3 → always min stake."""
    conf = signal.get("confluence", 0)
    if conf >= 3:
        return {"stake": "min"}
    return None


def _delta_pct_filter(signal: dict) -> dict | None:
    """delta_pct >= 0.10% + cc >= 2 + skip golden/high + skip tl<7."""
    dp = abs(signal.get("delta_percent", 0))
    cc = abs(signal.get("consecutive_closes", 0))
    atr_zone = signal.get("atr_zone", "")
    tl = signal.get("time_left", 0) or 0

    if dp < 0.10:
        return None
    if cc < 2:
        return None
    if atr_zone in ("golden", "high", "extreme"):
        return None
    if tl < 7:
        return None
    return {"stake": "full"}


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
