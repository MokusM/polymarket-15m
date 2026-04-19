import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Secrets ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN            = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID                   = os.getenv("CHAT_ID", "")
POLYMARKET_PRIVATE_KEY    = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_CHAIN_ID       = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
POLYMARKET_SIGNATURE_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
POLYMARKET_FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")

# ── Instance settings ─────────────────────────────────────────────────────────
DEFAULT_MODE     = os.getenv("DEFAULT_MODE", "medium")
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "true").lower() in ("1", "true", "yes")
LIVE_TRADING     = os.getenv("LIVE_TRADING", "false").lower() in ("1", "true", "yes")
AUTO_APPROVE_PAPER = os.getenv("AUTO_APPROVE_PAPER", "true").lower() in ("1", "true", "yes")
AUTO_APPROVE_LIVE  = os.getenv("AUTO_APPROVE_LIVE", "false").lower() in ("1", "true", "yes")

# ── CLOB execution ────────────────────────────────────────────────────────────
CLOB_CROSS_SPREAD_BUY     = os.getenv("CLOB_CROSS_SPREAD_BUY", "true").lower() in ("1", "true", "yes")
CLOB_MAX_BUY_SLIPPAGE_ABS = float(os.getenv("CLOB_MAX_BUY_SLIPPAGE_ABS", "0.05"))
CLOB_BUY_BUFFER           = float(os.getenv("CLOB_BUY_BUFFER", "0.02"))
CLOB_SPREAD_MAX           = float(os.getenv("CLOB_SPREAD_MAX", "1.0"))

# ── GTC (limit) orders ───────────────────────────────────────────────────────
GTC_PRICE_OFFSET          = float(os.getenv("GTC_PRICE_OFFSET", "0.02"))   # place limit at ask - offset
GTC_MAX_ENTRY_PRICE       = float(os.getenv("GTC_MAX_ENTRY_PRICE", "0.75")) # skip if best ask > this
GTC_ORDER_TTL_SECONDS     = int(os.getenv("GTC_ORDER_TTL_SECONDS", "300"))  # auto-cancel after 5 min

# ── Bankroll & Kelly ──────────────────────────────────────────────────────────
BANKROLL_USD       = float(os.getenv("BANKROLL_USD", "20"))
KELLY_FRACTION     = float(os.getenv("KELLY_FRACTION", "0.25"))
MIN_STAKE_USD      = float(os.getenv("MIN_STAKE_USD", "1"))
MAX_STAKE_USD      = float(os.getenv("MAX_STAKE_USD", "5"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "1"))
STAKE_USD          = float(os.getenv("STAKE_USD", "10"))

# ── SL / TP ───────────────────────────────────────────────────────────────────
SL_PERCENT                = float(os.getenv("SL_PERCENT", "25"))
TP_PARTIAL_PRICE          = float(os.getenv("TP_PARTIAL_PRICE", "0.90"))
TP_PARTIAL_SELL_PCT       = float(os.getenv("TP_PARTIAL_SELL_PCT", "25"))
TP_MID_PRICE              = float(os.getenv("TP_MID_PRICE", "0.93"))
TP_FULL_PRICE             = float(os.getenv("TP_FULL_PRICE", "0.95"))
TP_FINAL_PRICE            = float(os.getenv("TP_FINAL_PRICE", "0.97"))
POSITION_MONITOR_INTERVAL = int(os.getenv("POSITION_MONITOR_INTERVAL", "10"))
BREAKEVEN_AFTER_ROI_PCT   = float(os.getenv("BREAKEVEN_AFTER_ROI_PCT", "50"))
TRAILING_BE_TRIGGER       = float(os.getenv("TRAILING_BE_TRIGGER", "0"))
TRAILING_BE_SL_PCT        = float(os.getenv("TRAILING_BE_SL_PCT", "10"))

# ── Misc ──────────────────────────────────────────────────────────────────────
CLOB_TRADE_HISTORY_LIMIT     = int(os.getenv("CLOB_TRADE_HISTORY_LIMIT", "10"))
CLOB_TRADE_HISTORY_MAX_PAGES = int(os.getenv("CLOB_TRADE_HISTORY_MAX_PAGES", "3"))
CIRCUIT_BREAKER_LOSSES       = int(os.getenv("CIRCUIT_BREAKER_LOSSES", "3"))

# ── Константи (не конфігуруються) ────────────────────────────────────────────
RSI_PERIOD                     = 14
EMA_SHORT_PERIOD               = 9
EMA_LONG_PERIOD                = 21
EMA_SLOPE_LOOKBACK_BARS        = 3
MACD_FAST                      = 12
MACD_SLOW                      = 26
MACD_SIGNAL                    = 9
PIVOT_HL_PERIOD                = 10
VOLUME_AVG_PERIOD              = 10
VOLUME_SPIKE_MULTIPLIER        = 1.35
ATR_PERIOD                     = 14
ATR_ZONE_DEAD                  = 30
ATR_ZONE_QUIET                 = 60
ATR_ZONE_GOLDEN                = 100
ATR_ZONE_HIGH                  = 120
COOLDOWN_SECONDS               = 300  # 5 min — one order per market per wallet
SCAN_INTERVAL_SECONDS          = 3
MAX_SIGNALS_PER_ROUND_PER_SIDE = 1
REPEAT_ALERTS_AFTER_COOLDOWN   = False
IGNORE_IF_TIME_LEFT_LT_MIN     = 2
IGNORE_IF_CONTRACT_PRICE_GT    = 0.75
NOTIFY_SESSION_CHANGE          = True
POSITION_MONITOR_INTERVAL_ACTIVE = 1
KYIV_TZ_STR                    = "Europe/Kyiv"

# ── ALT assets (ETH, SOL): збір даних паралельно з BTC ──
ALT_GAP_MIN_PCT    = float(os.getenv("ALT_GAP_MIN_PCT", "0.06"))
ALT_GAP_STRICT_PCT = float(os.getenv("ALT_GAP_STRICT_PCT", "0.06"))
ALT_ATR_MIN_PCT    = float(os.getenv("ALT_ATR_MIN_PCT", "0.05"))
ALT_SCAN_ENABLED   = os.getenv("ALT_SCAN_ENABLED", "true").lower() in ("1", "true", "yes")

# ── DB paths ──────────────────────────────────────────────────────────────────
_ROOT      = Path(__file__).parent.parent
_DB_LABEL  = os.getenv("DB_LABEL", "")
_db_suffix = f"_{_DB_LABEL}" if _DB_LABEL else ""
DB_PATH_TEST = str(_ROOT / f"test{_db_suffix}.db")
DB_PATH_LIVE = str(_ROOT / f"live{_db_suffix}.db")
