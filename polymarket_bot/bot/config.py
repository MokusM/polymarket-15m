import os
from dotenv import load_dotenv

dotenv_path = os.path.join(os.path.dirname(__file__), "..", ".env")
load_dotenv(dotenv_path)

# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

# Contract price zone (course: 50-72¢)
CONTRACT_PRICE_MIN = 0.50
CONTRACT_PRICE_MAX = 0.72

# Time left window (minutes)
TIME_LEFT_MIN_MINUTES = 3
TIME_LEFT_MAX_MINUTES = 10

# RSI
RSI_PERIOD = 14

# EMA
EMA_SHORT_PERIOD = 9
EMA_LONG_PERIOD = 21
EMA_SLOPE_LOOKBACK_BARS = 3

# MACD (course: 12 / 26 / 9)
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# Pivots HL (course: 10-bar)
PIVOT_HL_PERIOD = 10

# Volume
VOLUME_AVG_PERIOD = 10
VOLUME_SPIKE_MULTIPLIER = 1.35

# ATR
ATR_PERIOD = 14
ATR_MIN_USD = float(os.getenv("ATR_MIN_USD", "30"))

# ATR zone thresholds ($)
ATR_ZONE_DEAD = 30
ATR_ZONE_QUIET = 60
ATR_ZONE_GOLDEN = 100
ATR_ZONE_HIGH = 120

# Confluence: min indicators agreeing for a signal (3 of 5)
MIN_CONFLUENCE = 3

# Bot settings
COOLDOWN_SECONDS = 60
SCAN_INTERVAL_SECONDS = 3
MAX_SIGNALS_PER_ROUND_PER_SIDE = 1
REPEAT_ALERTS_AFTER_COOLDOWN = False
IGNORE_IF_TIME_LEFT_LT_MIN = 2
IGNORE_IF_CONTRACT_PRICE_GT = 0.75

# Paper trading
STAKE_USD = float(os.getenv("STAKE_USD", "10"))
AUTO_APPROVE_PAPER = os.getenv("AUTO_APPROVE_PAPER", "true").lower() in (
    "1",
    "true",
    "yes",
)

# Session change notifications
NOTIFY_SESSION_CHANGE = True

# DB
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "signals.db")
