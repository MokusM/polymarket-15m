import os
import tomllib
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).parent.parent  # polymarket_bot/
load_dotenv(_ROOT / ".env")

# ── Load TOML config ──────────────────────────────────────────────────────────
_toml_path = _ROOT / "config.toml"
if _toml_path.exists():
    with open(_toml_path, "rb") as _f:
        _cfg = tomllib.load(_f)
else:
    _cfg = {}


def _g(section: str, key: str, default):
    return _cfg.get(section, {}).get(key, default)


# ── Secrets — тільки з .env ───────────────────────────────────────────────────
TELEGRAM_TOKEN            = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID                   = os.getenv("CHAT_ID", "")
POLYMARKET_PRIVATE_KEY    = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_CHAIN_ID       = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
POLYMARKET_SIGNATURE_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
POLYMARKET_FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")

# ── Mode ──────────────────────────────────────────────────────────────────────
DEFAULT_MODE     = _g("mode", "default", "medium")
DB_LABEL         = _g("mode", "db_label", "")
TELEGRAM_ENABLED = _g("mode", "telegram_enabled", True)

# ── Trading ───────────────────────────────────────────────────────────────────
LIVE_TRADING              = _g("trading", "live_trading", False)
AUTO_APPROVE_PAPER        = _g("trading", "auto_approve_paper", True)
AUTO_APPROVE_LIVE         = _g("trading", "auto_approve_live", False)
STAKE_USD                 = _g("trading", "stake_usd", 10.0)
CLOB_CROSS_SPREAD_BUY     = _g("trading", "clob_cross_spread_buy", True)
CLOB_MAX_BUY_SLIPPAGE_ABS = _g("trading", "clob_max_buy_slippage_abs", 0.05)
CLOB_BUY_BUFFER           = _g("trading", "clob_buy_buffer", 0.02)

# ── Sizing ────────────────────────────────────────────────────────────────────
BANKROLL_USD       = _g("sizing", "bankroll_usd", 20.0)
KELLY_FRACTION     = _g("sizing", "kelly_fraction", 0.25)
MIN_STAKE_USD      = _g("sizing", "min_stake_usd", 1.0)
MAX_STAKE_USD      = _g("sizing", "max_stake_usd", 5.0)
MAX_OPEN_POSITIONS = _g("sizing", "max_open_positions", 1)

# ── Filters ───────────────────────────────────────────────────────────────────
CONTRACT_PRICE_MIN      = _g("filters", "contract_price_min", 0.35)
CONTRACT_PRICE_MAX      = _g("filters", "contract_price_max", 0.72)
TIME_LEFT_MIN_MINUTES   = _g("filters", "time_left_min_minutes", 3)
TIME_LEFT_MAX_MINUTES   = _g("filters", "time_left_max_minutes", 10)
ATR_MIN_USD             = _g("filters", "atr_min_usd", 30.0)
GAP_MIN_USD             = _g("filters", "gap_min_usd", 50.0)
GAP_STRICT_USD          = _g("filters", "gap_strict_usd", 100.0)
CONTRACT_PRICE_HIGH_MIN = _g("filters", "contract_price_high_min", 0.75)
TIME_STRICT_MAX_MIN     = _g("filters", "time_strict_max_min", 5.0)
OBI_MIN_RATIO           = _g("filters", "obi_min_ratio", 1.2)
OBI_LEVELS              = _g("filters", "obi_levels", 5)
CLOB_SPREAD_MAX         = _g("filters", "clob_spread_max", 0.03)
MTF_RSI_FILTER_ENABLED  = _g("filters", "mtf_rsi_filter_enabled", False)
MIN_CONFLUENCE          = _g("filters", "min_confluence", 3)

# ── SL / TP ───────────────────────────────────────────────────────────────────
SL_PERCENT                = _g("sl_tp", "sl_percent", 40.0)
TP_PARTIAL_PRICE          = _g("sl_tp", "tp_partial_price", 0.90)
TP_PARTIAL_SELL_PCT       = _g("sl_tp", "tp_partial_sell_pct", 25.0)
TP_MID_PRICE              = _g("sl_tp", "tp_mid_price", 0.93)
TP_FULL_PRICE             = _g("sl_tp", "tp_full_price", 0.95)
TP_FINAL_PRICE            = _g("sl_tp", "tp_final_price", 0.97)
POSITION_MONITOR_INTERVAL = _g("sl_tp", "position_monitor_interval", 10)
BREAKEVEN_AFTER_ROI_PCT   = _g("sl_tp", "breakeven_after_roi_pct", 50.0)

# ── Misc ──────────────────────────────────────────────────────────────────────
CLOB_TRADE_HISTORY_LIMIT     = _g("misc", "clob_trade_history_limit", 10)
CLOB_TRADE_HISTORY_MAX_PAGES = _g("misc", "clob_trade_history_max_pages", 3)
CIRCUIT_BREAKER_LOSSES       = _g("misc", "circuit_breaker_losses", 3)

# ── Константи (не конфігуруються) ────────────────────────────────────────────
RSI_PERIOD                   = 14
EMA_SHORT_PERIOD             = 9
EMA_LONG_PERIOD              = 21
EMA_SLOPE_LOOKBACK_BARS      = 3
MACD_FAST                    = 12
MACD_SLOW                    = 26
MACD_SIGNAL                  = 9
PIVOT_HL_PERIOD              = 10
VOLUME_AVG_PERIOD            = 10
VOLUME_SPIKE_MULTIPLIER      = 1.35
ATR_PERIOD                   = 14
ATR_ZONE_DEAD                = 30
ATR_ZONE_QUIET               = 60
ATR_ZONE_GOLDEN              = 100
ATR_ZONE_HIGH                = 120
COOLDOWN_SECONDS             = 60
SCAN_INTERVAL_SECONDS        = 3
MAX_SIGNALS_PER_ROUND_PER_SIDE = 1
REPEAT_ALERTS_AFTER_COOLDOWN = False
IGNORE_IF_TIME_LEFT_LT_MIN   = 2
IGNORE_IF_CONTRACT_PRICE_GT  = 0.75
NOTIFY_SESSION_CHANGE        = True
POSITION_MONITOR_INTERVAL_ACTIVE = 1
KYIV_TZ_STR                  = "Europe/Kyiv"

# ── DB paths ──────────────────────────────────────────────────────────────────
_db_suffix  = f"_{DB_LABEL}" if DB_LABEL else ""
DB_PATH_TEST = str(_ROOT / f"test{_db_suffix}.db")
DB_PATH_LIVE = str(_ROOT / f"live{_db_suffix}.db")
