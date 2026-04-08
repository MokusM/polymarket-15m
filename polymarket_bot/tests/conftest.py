"""
conftest.py — shared fixtures for all tests.
"""

import sqlite3
import sys
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import numpy as np
import pytest

# Make sure 'polymarket_bot' package root is on sys.path
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Ignore legacy ad-hoc scripts that are not proper pytest tests
collect_ignore = [
    "test_buy_1usd.py",
    "test_live.py",
    "test_poly.py",
    "test_poly2.py",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_candles(
    n: int = 60,
    start_price: float = 69_000.0,
    trend: float = 0.0,        # price drift per bar (dollars)
    noise: float = 50.0,       # random noise amplitude
    seed: int = 42,
    start_ts: datetime | None = None,
) -> pd.DataFrame:
    """Generate synthetic 1-min OHLCV candles."""
    rng = np.random.default_rng(seed)
    if start_ts is None:
        start_ts = datetime(2024, 1, 1, 0, 0, 0)

    closes = [start_price]
    for _ in range(n - 1):
        closes.append(closes[-1] + trend + rng.uniform(-noise, noise))

    closes = np.array(closes)
    opens = np.concatenate([[start_price], closes[:-1]])
    highs = np.maximum(opens, closes) + rng.uniform(0, noise * 0.5, n)
    lows = np.minimum(opens, closes) - rng.uniform(0, noise * 0.5, n)
    volumes = rng.uniform(100, 500, n)
    timestamps = [start_ts + timedelta(minutes=i) for i in range(n)]

    return pd.DataFrame({
        "timestamp": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


# ---------------------------------------------------------------------------
# Candle fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_candles_bullish() -> pd.DataFrame:
    """100 candles with a clear uptrend (+150 per bar)."""
    return _make_candles(n=100, start_price=69_000.0, trend=150.0, noise=30.0)


@pytest.fixture
def sample_candles_bearish() -> pd.DataFrame:
    """100 candles with a clear downtrend (-150 per bar)."""
    return _make_candles(n=100, start_price=69_000.0, trend=-150.0, noise=30.0)


@pytest.fixture
def sample_candles_sideways() -> pd.DataFrame:
    """100 candles with no trend, moderate noise."""
    return _make_candles(n=100, start_price=69_000.0, trend=0.0, noise=50.0)


@pytest.fixture
def sample_df() -> pd.DataFrame:
    """60 BTC 1m candles with realistic ~69000 price (no strong trend)."""
    return _make_candles(n=60, start_price=69_000.0, trend=0.0, noise=40.0)


# ---------------------------------------------------------------------------
# Market info fixtures
# ---------------------------------------------------------------------------

def _future_iso(minutes: float) -> str:
    dt = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return dt.isoformat()


@pytest.fixture
def market_info_active() -> dict:
    """Active market with time_left ~8 min, price_yes=0.30, price_no=0.70."""
    return {
        "market_id": "test-market-001",
        "price_yes": 0.30,
        "price_no": 0.70,
        "end_date_iso": _future_iso(8),
        "event_start_time": None,
    }


@pytest.fixture
def sample_market() -> dict:
    """Full market_info dict with YES/NO tokens and outcome prices."""
    return {
        "market_id": "test-market-002",
        "market_slug": "btc-up-down-15m",
        "market_title": "Will BTC go up in next 15 min?",
        "token_yes_id": "token_yes_abc123",
        "token_no_id": "token_no_def456",
        "price_yes": 0.30,
        "price_no": 0.70,
        "end_date_iso": _future_iso(8),
        "event_start_time": None,
    }


# ---------------------------------------------------------------------------
# Signal fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_signal() -> dict:
    """A realistic DOWN signal: confluence=4, GAP=135, ATR=82, cp=0.76."""
    return {
        "market_id": "test-market-001",
        "direction": "DOWN",
        "confluence": 4,
        "contract_price": 0.76,
        "start_price": 69_200.0,
        "current_price": 69_065.0,
        "delta": -135.0,
        "delta_percent": -0.195,
        "gap": -135.0,
        "atr": 82.0,
        "atr_zone": "golden",
        "rsi_1m": 68.0,
        "rsi_3m": None,
        "rsi_5m": None,
        "ema_position": "below EMA9 (-12.5)",
        "volume_state": "normal",
        "time_left": 7.5,
        "filter_version": "current",
        "consecutive_closes": -3,
        "chg_1h": -0.5,
        "ptb": 69_200.0,
        "obi": 1.5,
        "votes": {
            "RSI":    {"direction": "DOWN", "label": "RSI 68.0 overbought"},
            "MACD":   {"direction": "DOWN", "label": "bearish crossover"},
            "VWAP":   {"direction": "DOWN", "label": "below VWAP (-0.10%)"},
            "EMA":    {"direction": "DOWN", "label": "EMA9 < EMA21 (-12.5)"},
            "Pivots": {"direction": "DOWN", "label": "breakdown (<69100)"},
        },
    }


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_memory(tmp_path):
    """Temporary file-based SQLite DB (in tmp_path) for storage tests.
    Returns the db_path string after init_db() has been called.
    """
    from bot.storage import init_db
    db_path = str(tmp_path / "test_memory.db")
    init_db(db_path)
    return db_path


@pytest.fixture
def test_db(tmp_path):
    """Alias for db_memory — isolated DB per test."""
    from bot.storage import init_db
    db_path = str(tmp_path / "test_isolated.db")
    init_db(db_path)
    return db_path


# ---------------------------------------------------------------------------
# State / mode fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_state_light(monkeypatch):
    """Patch state.mode to 'light' for the duration of the test."""
    from bot import state as state_module
    monkeypatch.setattr(state_module.state, "mode", "light")
    return state_module.state


@pytest.fixture
def mock_state_test(monkeypatch):
    """Patch state.mode to 'test' for the duration of the test."""
    from bot import state as state_module
    monkeypatch.setattr(state_module.state, "mode", "test")
    return state_module.state


@pytest.fixture
def mock_state_medium(monkeypatch):
    """Patch state.mode to 'medium' for the duration of the test."""
    from bot import state as state_module
    monkeypatch.setattr(state_module.state, "mode", "medium")
    return state_module.state


# ---------------------------------------------------------------------------
# Indicators-applied candles (handy for signal tests)
# ---------------------------------------------------------------------------

@pytest.fixture
def df_with_indicators_bullish(sample_candles_bullish):
    """Bullish candles with full indicator set applied."""
    from bot.indicators import add_indicators
    return add_indicators(sample_candles_bullish.copy())


@pytest.fixture
def df_with_indicators_bearish(sample_candles_bearish):
    """Bearish candles with full indicator set applied."""
    from bot.indicators import add_indicators
    return add_indicators(sample_candles_bearish.copy())
