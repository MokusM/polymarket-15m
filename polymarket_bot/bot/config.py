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

# ── Live trading ──
LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() in ("1", "true", "yes")
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_CHAIN_ID = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
AUTO_APPROVE_LIVE = os.getenv("AUTO_APPROVE_LIVE", "false").lower() in ("1", "true", "yes")

# CLOB: для BUY підняти ліміт до best ask (side=SELL у API), щоб не «висіти» нижче ринку
CLOB_CROSS_SPREAD_BUY = os.getenv("CLOB_CROSS_SPREAD_BUY", "true").lower() in (
    "1", "true", "yes",
)
# Поріг «зсуву від сигналу» для логу-попередження; жорстка межа BUY = CONTRACT_PRICE_MAX (сканер)
CLOB_MAX_BUY_SLIPPAGE_ABS = float(os.getenv("CLOB_MAX_BUY_SLIPPAGE_ABS", "0.05"))

# Bankroll & Kelly sizing
BANKROLL_USD = float(os.getenv("BANKROLL_USD", "20"))
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))
MIN_STAKE_USD = float(os.getenv("MIN_STAKE_USD", "1"))
MAX_STAKE_USD = float(os.getenv("MAX_STAKE_USD", "5"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "1"))

# Stop-loss & take-profit levels (absolute contract price)
SL_PERCENT = float(os.getenv("SL_PERCENT", "40"))
TP_PARTIAL_PRICE = float(os.getenv("TP_PARTIAL_PRICE", "0.90"))
TP_PARTIAL_SELL_PCT = float(os.getenv("TP_PARTIAL_SELL_PCT", "50"))
TP_FULL_PRICE = float(os.getenv("TP_FULL_PRICE", "0.95"))
POSITION_MONITOR_INTERVAL = int(os.getenv("POSITION_MONITOR_INTERVAL", "10"))

# Скільки останніх угод показує /history (CLOB /data/trades)
CLOB_TRADE_HISTORY_LIMIT = int(os.getenv("CLOB_TRADE_HISTORY_LIMIT", "10"))
# Скільки сторінок CLOB зчитати (кожна — пачка угод; більше = точніші «останні N» при великій історії)
CLOB_TRADE_HISTORY_MAX_PAGES = int(os.getenv("CLOB_TRADE_HISTORY_MAX_PAGES", "3"))

# Якщо нереалізований PnL >= цей % від ставки на залишок — SL піднімається до ціни входу (беззбиток)
BREAKEVEN_AFTER_ROI_PCT = float(os.getenv("BREAKEVEN_AFTER_ROI_PCT", "50"))

# Circuit breaker: auto-disable live trading after N consecutive losses
CIRCUIT_BREAKER_LOSSES = int(os.getenv("CIRCUIT_BREAKER_LOSSES", "3"))

# GAP filter: |current_price - start_price| must exceed this threshold (USD)
GAP_MIN_USD = float(os.getenv("GAP_MIN_USD", "50"))

# OBI filter: bid_volume / ask_volume must exceed this ratio (1.0 = neutral)
OBI_MIN_RATIO = float(os.getenv("OBI_MIN_RATIO", "1.2"))
# Number of top orderbook levels to sum for OBI calculation
OBI_LEVELS = int(os.getenv("OBI_LEVELS", "5"))

# Session change notifications
NOTIFY_SESSION_CHANGE = True

# DB — two databases: test mode vs live/other modes
_BOT_DIR = os.path.dirname(__file__)
DB_PATH_TEST = os.path.join(_BOT_DIR, "..", "test.db")
DB_PATH_LIVE = os.path.join(_BOT_DIR, "..", "live.db")

# Position monitor: 1s when position is open, POSITION_MONITOR_INTERVAL when idle
POSITION_MONITOR_INTERVAL_ACTIVE = 1

# Kyiv timezone for daily report
KYIV_TZ_STR = "Europe/Kyiv"
