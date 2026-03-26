import logging

from bot.config import CIRCUIT_BREAKER_LOSSES, LIVE_TRADING, DEFAULT_MODE

logger = logging.getLogger(__name__)


class BotState:
    def __init__(self):
        self.mode = DEFAULT_MODE
        self._live_enabled = LIVE_TRADING
        self._consecutive_losses = 0
        self._circuit_breaker_triggered = False

    # ── Live trading guard ──

    @property
    def is_live_allowed(self) -> bool:
        """Live trading дозволено тільки якщо: env=true + mode!=test + circuit breaker не спрацював."""
        if not self._live_enabled:
            return False
        if self.mode == "test":
            return False
        if self._circuit_breaker_triggered:
            return False
        return True

    def record_win(self):
        self._consecutive_losses = 0

    def record_loss(self):
        self._consecutive_losses += 1
        if self._consecutive_losses >= CIRCUIT_BREAKER_LOSSES:
            self._circuit_breaker_triggered = True
            logger.warning(
                "CIRCUIT BREAKER: %s consecutive losses — live trading DISABLED",
                self._consecutive_losses,
            )

    def reset_circuit_breaker(self):
        self._circuit_breaker_triggered = False
        self._consecutive_losses = 0
        logger.info("Circuit breaker reset — live trading re-enabled")

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def circuit_breaker_active(self) -> bool:
        return self._circuit_breaker_triggered

    # ── Mode thresholds ──

    def get_thresholds(self):
        modes = {
            "test": {
                "TIME_LEFT_MIN_MINUTES": -100000,
                "TIME_LEFT_MAX_MINUTES": 999999,
                "CONTRACT_PRICE_MIN": 0.0,
                "CONTRACT_PRICE_MAX": 1.0,
                "ATR_MIN_USD": 0,
                "MIN_CONFLUENCE": 1,
                "ALLOW_DEAD_ZONE": True,
            },
            "light": {
                "TIME_LEFT_MIN_MINUTES": 2,
                "TIME_LEFT_MAX_MINUTES": 12,
                "CONTRACT_PRICE_MIN": 0.45,
                "CONTRACT_PRICE_MAX": 0.75,
                "ATR_MIN_USD": 25,
                "MIN_CONFLUENCE": 3,
                "ALLOW_DEAD_ZONE": False,
            },
            "medium": {
                "TIME_LEFT_MIN_MINUTES": 3,
                "TIME_LEFT_MAX_MINUTES": 10,
                "CONTRACT_PRICE_MIN": 0.50,
                "CONTRACT_PRICE_MAX": 0.72,
                "ATR_MIN_USD": 30,
                "MIN_CONFLUENCE": 3,
                "ALLOW_DEAD_ZONE": False,
            },
            "strict": {
                "TIME_LEFT_MIN_MINUTES": 4,
                "TIME_LEFT_MAX_MINUTES": 9,
                "CONTRACT_PRICE_MIN": 0.52,
                "CONTRACT_PRICE_MAX": 0.68,
                "ATR_MIN_USD": 40,
                "MIN_CONFLUENCE": 4,
                "ALLOW_DEAD_ZONE": False,
            },
        }
        return modes.get(self.mode, modes["medium"])


state = BotState()
