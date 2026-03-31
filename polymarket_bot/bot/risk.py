"""
Edge-based position sizing (fractional Kelly criterion).

edge = estimated_win_probability - contract_price
kelly_stake = bankroll * kelly_fraction * edge / (1 - contract_price)
clamped to [MIN_STAKE_USD, MAX_STAKE_USD]

Win probability is estimated from confluence score + quality modifiers.
"""

import logging

from bot.config import (
    BANKROLL_USD,
    KELLY_FRACTION,
    MAX_OPEN_POSITIONS,
    MAX_STAKE_USD,
    MIN_STAKE_USD,
)

logger = logging.getLogger(__name__)


def estimate_win_probability(signal: dict) -> float:
    """
    Оцінити ймовірність перемоги сигналу.

    Базується на confluence (3/5 → ~60%, 4/5 → ~70%, 5/5 → ~80%)
    з модифікаторами за ATR zone та contract price zone.
    """
    confluence = signal.get("confluence", 3)

    base_map = {1: 0.50, 2: 0.52, 3: 0.60, 4: 0.70, 5: 0.80}
    prob = base_map.get(confluence, 0.55)

    atr_zone = signal.get("atr_zone", "quiet")
    zone_mod = {
        "dead": -0.05,
        "quiet": 0.0,
        "golden": +0.05,
        "high": +0.02,
        "extreme": -0.02,
    }
    prob += zone_mod.get(atr_zone, 0)

    cp = signal.get("clob_ask") or signal.get("contract_price", 0.5)
    if 0.50 <= cp <= 0.55:
        prob += 0.02
    elif cp > 0.68:
        prob -= 0.02

    time_left = signal.get("time_left", 5)
    if 5 <= time_left <= 9:
        prob += 0.02
    elif time_left < 3:
        prob -= 0.03

    return max(0.50, min(0.90, prob))


def calculate_edge(signal: dict) -> float:
    """edge = win_probability - contract_price (може бути від'ємний)."""
    win_prob = estimate_win_probability(signal)
    contract_price = signal.get("contract_price", 0.5)
    return win_prob - contract_price


def calculate_stake(signal: dict, bankroll: float | None = None) -> dict:
    """
    Розрахувати ставку за Kelly criterion.

    Returns:
        {
            "stake_usd": float,
            "edge": float,
            "win_prob": float,
            "kelly_raw": float,
            "shares": float,
            "reason": str,  # "kelly" | "min_cap" | "max_cap" | "no_edge" | "bankroll_limit"
        }
    """
    from bot.state import state
    from bot.config import STAKE_USD as FIXED_STAKE

    # Test mode: fixed stake, no Kelly
    if state.mode == "test":
        cp = signal.get("contract_price", 0.5)
        return {
            "win_prob": 0.60,
            "edge": 0.10,
            "kelly_raw": 0.0,
            "stake_usd": FIXED_STAKE,
            "shares": round(FIXED_STAKE / cp, 2) if cp > 0 else 0,
            "reason": "test_fixed",
        }

    br = bankroll if bankroll is not None else BANKROLL_USD
    win_prob = estimate_win_probability(signal)
    # Використовуємо CLOB ask якщо є — це реальна ціна купівлі
    cp = signal.get("clob_ask") or signal.get("contract_price", 0.5)
    edge = win_prob - cp

    result = {
        "win_prob": round(win_prob, 4),
        "edge": round(edge, 4),
        "kelly_raw": 0.0,
        "stake_usd": 0.0,
        "shares": 0.0,
        "reason": "no_edge",
    }

    if edge <= 0:
        # Edge check disabled — fixed $10 stake
        result["stake_usd"] = 10.0
        result["shares"] = round(10.0 / cp, 2) if cp > 0 else 0
        return result

    kelly_full = edge / (1 - cp) if (1 - cp) > 0 else 0
    kelly_stake = br * KELLY_FRACTION * kelly_full

    result["kelly_raw"] = round(kelly_full, 4)

    if kelly_stake < MIN_STAKE_USD:
        kelly_stake = MIN_STAKE_USD
        result["reason"] = "min_cap"
    elif kelly_stake > MAX_STAKE_USD:
        kelly_stake = MAX_STAKE_USD
        result["reason"] = "max_cap"
    else:
        result["reason"] = "kelly"

    per_position_limit = br / max(MAX_OPEN_POSITIONS, 1)
    if kelly_stake > per_position_limit:
        kelly_stake = per_position_limit
        result["reason"] = "bankroll_limit"

    kelly_stake = max(kelly_stake, MIN_STAKE_USD)
    if kelly_stake > MAX_STAKE_USD:
        kelly_stake = MAX_STAKE_USD
        result["reason"] = "max_cap"

    result["stake_usd"] = round(kelly_stake, 2)
    result["shares"] = round(kelly_stake / cp, 2) if cp > 0 else 0
    return result


def format_risk_line(risk: dict) -> str:
    """Форматований рядок для Telegram алерта."""
    edge_pct = risk["edge"] * 100
    wp_pct = risk["win_prob"] * 100
    return (
        f"Edge: {edge_pct:+.1f}% | WinProb: {wp_pct:.0f}% | "
        f"Stake: ${risk['stake_usd']:.2f} ({risk['reason']})"
    )
