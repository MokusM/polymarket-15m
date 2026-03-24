class BotState:
    def __init__(self):
        self.mode = "medium"

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
