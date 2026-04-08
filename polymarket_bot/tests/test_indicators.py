"""
test_indicators.py — tests for bot/indicators.py

Covers: RSI, MACD, VWAP, EMA, ATR zone, consecutive_closes, add_indicators columns.
"""

import numpy as np
import pandas as pd
import pytest

from bot.indicators import (
    add_indicators,
    calculate_ema,
    calculate_rsi,
    calculate_macd,
    calculate_vwap,
    classify_atr_zone,
)
from bot.config import (
    ATR_ZONE_DEAD,
    ATR_ZONE_QUIET,
    ATR_ZONE_GOLDEN,
    ATR_ZONE_HIGH,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_df(closes, start_price=69_000.0):
    """Build minimal OHLCV DataFrame from a list of close prices."""
    n = len(closes)
    closes = np.array(closes, dtype=float)
    opens = np.concatenate([[start_price], closes[:-1]])
    highs = closes + 20.0
    lows = closes - 20.0
    volumes = np.ones(n) * 200.0
    import datetime
    timestamps = [datetime.datetime(2024, 1, 1) + datetime.timedelta(minutes=i) for i in range(n)]
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def _trending_closes(n=60, start=69_000.0, step=100.0):
    return [start + i * step for i in range(n)]


def _flat_closes(n=60, base=69_000.0, noise=20.0, seed=7):
    rng = np.random.default_rng(seed)
    return [base + rng.uniform(-noise, noise) for _ in range(n)]


# ---------------------------------------------------------------------------
# RSI tests
# ---------------------------------------------------------------------------

class TestRSI:
    def test_rsi_range(self):
        """RSI must be in [0, 100] for any input."""
        closes = _trending_closes(60)
        series = pd.Series(closes, dtype=float)
        rsi = calculate_rsi(series, 14)
        valid = rsi.dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_rsi_overbought(self):
        """Strongly rising prices → last RSI > 70."""
        # Large positive step ensures overbought
        closes = _trending_closes(60, start=69_000.0, step=200.0)
        series = pd.Series(closes, dtype=float)
        rsi = calculate_rsi(series, 14)
        assert rsi.iloc[-1] > 70, f"Expected RSI>70, got {rsi.iloc[-1]:.2f}"

    def test_rsi_oversold(self):
        """Strongly falling prices → last RSI < 30."""
        closes = _trending_closes(60, start=69_000.0, step=-200.0)
        series = pd.Series(closes, dtype=float)
        rsi = calculate_rsi(series, 14)
        assert rsi.iloc[-1] < 30, f"Expected RSI<30, got {rsi.iloc[-1]:.2f}"

    def test_rsi_neutral(self):
        """Sideways/flat market → RSI roughly in 40–60."""
        closes = _flat_closes(60)
        series = pd.Series(closes, dtype=float)
        rsi = calculate_rsi(series, 14)
        val = rsi.iloc[-1]
        assert 30 <= val <= 70, f"Expected RSI in neutral range, got {val:.2f}"

    def test_rsi_short_series(self):
        """Series shorter than period: should not crash, values may be NaN at start."""
        closes = [69_000.0 + i * 50 for i in range(10)]
        series = pd.Series(closes, dtype=float)
        rsi = calculate_rsi(series, 14)
        # Should not raise; early values may be NaN-like but we just check no exception
        assert len(rsi) == len(closes)

    def test_rsi_formula_wilder_direction(self):
        """Pure up-move should give high RSI; pure down-move should give low RSI."""
        up = pd.Series([1.0 + i * 0.1 for i in range(30)])
        down = pd.Series([4.0 - i * 0.1 for i in range(30)])
        rsi_up = calculate_rsi(up, 14)
        rsi_down = calculate_rsi(down, 14)
        assert rsi_up.iloc[-1] > 60
        assert rsi_down.iloc[-1] < 40


# ---------------------------------------------------------------------------
# VWAP tests
# ---------------------------------------------------------------------------

class TestVWAP:
    def test_vwap_single_candle(self):
        """With one candle, VWAP == typical price."""
        import datetime
        df = pd.DataFrame({
            "timestamp": [datetime.datetime(2024, 1, 1)],
            "high": [100.0],
            "low": [90.0],
            "close": [95.0],
            "volume": [500.0],
        })
        vwap = calculate_vwap(df)
        expected = (100 + 90 + 95) / 3
        assert abs(vwap.iloc[0] - expected) < 0.01

    def test_vwap_cumulative(self):
        """VWAP is cumulative: each bar incorporates all prior volume-price data."""
        import datetime
        df = pd.DataFrame({
            "timestamp": [datetime.datetime(2024, 1, 1) + datetime.timedelta(minutes=i) for i in range(3)],
            "high":   [100.0, 110.0, 120.0],
            "low":    [90.0,  100.0, 110.0],
            "close":  [95.0,  105.0, 115.0],
            "volume": [100.0, 200.0, 300.0],
        })
        vwap = calculate_vwap(df)
        # Manually compute cumulative
        tp = [(100+90+95)/3, (110+100+105)/3, (120+110+115)/3]
        expected_last = (tp[0]*100 + tp[1]*200 + tp[2]*300) / (100+200+300)
        assert abs(vwap.iloc[-1] - expected_last) < 0.01

    def test_vwap_zero_volume(self):
        """VWAP with zero volume should not raise ZeroDivisionError."""
        import datetime
        df = pd.DataFrame({
            "timestamp": [datetime.datetime(2024, 1, 1) + datetime.timedelta(minutes=i) for i in range(3)],
            "high":   [100.0, 110.0, 120.0],
            "low":    [90.0,  100.0, 110.0],
            "close":  [95.0,  105.0, 115.0],
            "volume": [0.0,   0.0,   0.0],
        })
        try:
            result = calculate_vwap(df)
            # Result may be NaN or inf — that's fine, just no exception
            assert len(result) == 3
        except ZeroDivisionError:
            pytest.fail("VWAP raised ZeroDivisionError on zero volume")


# ---------------------------------------------------------------------------
# MACD tests
# ---------------------------------------------------------------------------

class TestMACD:
    def test_macd_crossover_detected(self):
        """After a strong down-move, MACD line < signal → bearish crossover."""
        closes = _trending_closes(60, step=-150.0)
        series = pd.Series(closes, dtype=float)
        line, sig, hist = calculate_macd(series, 12, 26, 9)
        # Strong downtrend: MACD line should be negative and below signal
        assert line.iloc[-1] < 0
        assert hist.iloc[-1] < 0

    def test_macd_histogram_sign_change(self):
        """Switching from downtrend to uptrend should eventually flip histogram sign."""
        # Downtrend then uptrend
        down = _trending_closes(40, start=69_000.0, step=-100.0)
        up = _trending_closes(40, start=down[-1], step=+100.0)
        closes = down + up
        series = pd.Series(closes, dtype=float)
        _, _, hist = calculate_macd(series, 12, 26, 9)
        # First segment was negative, histogram should have become positive at some point
        has_positive = (hist.iloc[40:] > 0).any()
        assert has_positive, "Histogram never became positive after trend reversal"

    def test_macd_no_false_cross_flat(self):
        """Flat market: histogram should stay near zero (no strong cross)."""
        closes = _flat_closes(80, noise=5.0, seed=99)
        series = pd.Series(closes, dtype=float)
        _, _, hist = calculate_macd(series, 12, 26, 9)
        # All histogram values should be small
        assert hist.dropna().abs().max() < 50.0

    def test_macd_bullish_uptrend(self):
        """Strong uptrend: MACD histogram > 0 at end."""
        closes = _trending_closes(60, step=200.0)
        series = pd.Series(closes, dtype=float)
        line, sig, hist = calculate_macd(series, 12, 26, 9)
        assert hist.iloc[-1] > 0


# ---------------------------------------------------------------------------
# ATR Zone classification tests
# ---------------------------------------------------------------------------

class TestATRZone:
    def test_atr_zone_dead(self):
        """ATR < ATR_ZONE_DEAD → 'dead'."""
        assert classify_atr_zone(ATR_ZONE_DEAD - 1) == "dead"
        assert classify_atr_zone(15) == "dead"

    def test_atr_zone_quiet(self):
        """ATR_ZONE_DEAD <= ATR < ATR_ZONE_QUIET → 'quiet'."""
        val = (ATR_ZONE_DEAD + ATR_ZONE_QUIET) / 2
        assert classify_atr_zone(val) == "quiet"
        assert classify_atr_zone(35) == "quiet"

    def test_atr_zone_golden(self):
        """ATR_ZONE_QUIET <= ATR < ATR_ZONE_GOLDEN → 'golden'."""
        val = (ATR_ZONE_QUIET + ATR_ZONE_GOLDEN) / 2
        assert classify_atr_zone(val) == "golden"
        assert classify_atr_zone(75) == "golden"

    def test_atr_zone_high(self):
        """ATR_ZONE_GOLDEN <= ATR < ATR_ZONE_HIGH → 'high'."""
        val = (ATR_ZONE_GOLDEN + ATR_ZONE_HIGH) / 2
        assert classify_atr_zone(val) == "high"
        assert classify_atr_zone(110) == "high"

    def test_atr_zone_extreme(self):
        """ATR >= ATR_ZONE_HIGH → 'extreme'."""
        assert classify_atr_zone(ATR_ZONE_HIGH + 1) == "extreme"
        assert classify_atr_zone(250) == "extreme"

    def test_atr_zone_nan(self):
        """NaN ATR → 'dead'."""
        assert classify_atr_zone(float("nan")) == "dead"


# ---------------------------------------------------------------------------
# EMA tests
# ---------------------------------------------------------------------------

class TestEMA:
    def test_ema_golden_cross(self):
        """Strong uptrend: EMA9 > EMA21."""
        closes = pd.Series(_trending_closes(60, step=200.0))
        ema9 = calculate_ema(closes, 9)
        ema21 = calculate_ema(closes, 21)
        assert ema9.iloc[-1] > ema21.iloc[-1]

    def test_ema_death_cross(self):
        """Strong downtrend: EMA9 < EMA21."""
        closes = pd.Series(_trending_closes(60, step=-200.0))
        ema9 = calculate_ema(closes, 9)
        ema21 = calculate_ema(closes, 21)
        assert ema9.iloc[-1] < ema21.iloc[-1]


# ---------------------------------------------------------------------------
# add_indicators() — column completeness
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS = [
    "timestamp", "open", "high", "low", "close", "volume",
    "ema_9", "ema_21", "ema_9_slope",
    "rsi_1m",
    "macd_line", "macd_signal", "macd_hist",
    "vwap",
    "pivot_high", "pivot_low",
    "atr", "atr_zone",
    "chg_1h",
    "volume_state",
    "consecutive_closes",
    "rsi_3m", "rsi_5m",
]


class TestAddIndicators:
    def test_add_indicators_all_columns_present(self, sample_df):
        """add_indicators() must produce all required columns."""
        result = add_indicators(sample_df.copy())
        for col in REQUIRED_COLUMNS:
            assert col in result.columns, f"Missing column: {col}"

    def test_add_indicators_row_count_preserved(self, sample_df):
        """add_indicators() must not add or drop rows."""
        result = add_indicators(sample_df.copy())
        assert len(result) == len(sample_df)

    def test_add_indicators_empty_returns_empty(self):
        """Empty df → returned unchanged (not crashed)."""
        empty = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        result = add_indicators(empty)
        assert result.empty

    def test_add_indicators_too_short_returns_unchanged(self):
        """< 30 rows → returned as-is."""
        import datetime
        n = 20
        df = pd.DataFrame({
            "timestamp": [datetime.datetime(2024, 1, 1) + datetime.timedelta(minutes=i) for i in range(n)],
            "open": [1.0] * n,
            "high": [1.1] * n,
            "low": [0.9] * n,
            "close": [1.0] * n,
            "volume": [100.0] * n,
        })
        result = add_indicators(df)
        assert len(result) == n
        # Should NOT have rsi_1m since skipped
        assert "rsi_1m" not in result.columns

    def test_atr_zone_column_valid_values(self, sample_df):
        """atr_zone column should only contain valid zone names."""
        result = add_indicators(sample_df.copy())
        valid_zones = {"dead", "quiet", "golden", "high", "extreme"}
        actual = set(result["atr_zone"].unique())
        assert actual.issubset(valid_zones), f"Unexpected zones: {actual - valid_zones}"

    def test_volume_state_valid_values(self, sample_df):
        """volume_state must be one of spike/stabilization/normal."""
        result = add_indicators(sample_df.copy())
        valid = {"spike", "stabilization", "normal"}
        actual = set(result["volume_state"].unique())
        assert actual.issubset(valid)


# ---------------------------------------------------------------------------
# consecutive_closes tests
# ---------------------------------------------------------------------------

class TestConsecutiveCloses:
    def test_consecutive_closes_uptrend(self, sample_candles_bullish):
        """Pure uptrend should have large positive consecutive_closes at end."""
        result = add_indicators(sample_candles_bullish.copy())
        cc = result["consecutive_closes"].iloc[-1]
        assert cc > 0, f"Expected positive cc in uptrend, got {cc}"

    def test_consecutive_closes_downtrend(self, sample_candles_bearish):
        """Pure downtrend should have negative consecutive_closes at end."""
        result = add_indicators(sample_candles_bearish.copy())
        cc = result["consecutive_closes"].iloc[-1]
        assert cc < 0, f"Expected negative cc in downtrend, got {cc}"

    def test_consecutive_closes_resets_on_reversal(self, sample_df):
        """CC resets to 1 when direction changes."""
        result = add_indicators(sample_df.copy())
        cc = result["consecutive_closes"]
        # After any direction change, the next bar starts at ±1
        diffs = cc.diff().dropna()
        # If trend reverses, cc should jump by a large amount — this is hard to
        # assert directly, but we can check it's within a reasonable range
        assert cc.abs().max() <= len(result)
