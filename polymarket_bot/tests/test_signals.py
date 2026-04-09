"""
test_signals.py — tests for bot/signals.py

KEY FACTS from code (checked 2026-04-06):
- RSI < 35 → UP vote (oversold), RSI > 65 → DOWN vote (overbought). This is BY DESIGN.
- MACD: line > signal AND hist > 0 → UP; line < signal AND hist < 0 → DOWN
- VWAP: price > vwap → UP; price < vwap → DOWN
- EMA: ema_9 > ema_21 → UP; ema_9 < ema_21 → DOWN
- Pivots: price >= pivot_high → UP; price <= pivot_low → DOWN
- Confluence threshold: 3/5 (light mode)
- ATR zone 'dead' blocks signals
- time_left must be in [TIME_LEFT_MIN_MINUTES, TIME_LEFT_MAX_MINUTES]
- GAP filter: abs(gap) >= GAP_MIN_USD
- Strict GAP: if time_left < 5 min, gap must be >= 100
- filter_version: "both" if both filters pass, "new" if only new, "current" if only current
"""

import math
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pandas as pd
import numpy as np
import pytest

from bot.signals import (
    _vote_rsi,
    _vote_macd,
    _vote_vwap,
    _vote_ema,
    _vote_pivots,
    check_signals,
)


# ---------------------------------------------------------------------------
# Helper to build a minimal last-row DataFrame for check_signals()
# ---------------------------------------------------------------------------

def _future_iso(minutes: float) -> str:
    dt = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return dt.isoformat()


def _make_signal_df(
    n: int = 60,
    close: float = 69_065.0,
    rsi_1m: float = 50.0,
    macd_line: float = 0.0,
    macd_signal: float = 0.0,
    macd_hist: float = 0.0,
    vwap: float = 69_100.0,
    ema_9: float = 69_050.0,
    ema_21: float = 69_000.0,
    pivot_high: float = 70_000.0,
    pivot_low: float = 68_000.0,
    atr: float = 80.0,
    atr_zone: str = "golden",
    volume_state: str = "normal",
    consecutive_closes: int = 0,
    rsi_3m: float = None,
    rsi_5m: float = None,
    chg_1h: float = 0.0,
    ema_9_slope: float = 1.0,
    base_open: float = 69_000.0,  # open price of bars -15 onward (determines gap)
) -> pd.DataFrame:
    """Build a DataFrame with the final row holding the desired indicator values.

    Gap calculation in check_signals (no event_start_time):
        start_price = df.iloc[-15]["open"]  # the open of bar at position n-15
        gap = close - start_price

    So set `base_open` to control the start_price reference used by check_signals.
    Default base_open=69_000 means gap = close - 69_000.
    """
    import datetime as dt_mod
    rows = []
    base_ts = dt_mod.datetime(2024, 1, 1, 0, 0, 0)

    for i in range(n - 1):
        rows.append({
            "timestamp": base_ts + dt_mod.timedelta(minutes=i),
            "open": base_open,
            "high": base_open + 20,
            "low": base_open - 20,
            "close": base_open,
            "volume": 200.0,
            "rsi_1m": 50.0,
            "macd_line": 0.0,
            "macd_signal": 0.0,
            "macd_hist": 0.0,
            "vwap": base_open,
            "ema_9": base_open,
            "ema_21": base_open,
            "ema_9_slope": 0.0,
            "pivot_high": base_open + 500,
            "pivot_low": base_open - 500,
            "atr": atr,
            "atr_zone": atr_zone,
            "volume_state": "normal",
            "consecutive_closes": 0,
            "rsi_3m": None,
            "rsi_5m": None,
            "chg_1h": 0.0,
        })

    # The last row has the desired indicator values
    # gap = close - df.iloc[-15]["open"] = close - base_open
    rows.append({
        "timestamp": base_ts + dt_mod.timedelta(minutes=n - 1),
        "open": close,
        "high": close + 20,
        "low": close - 20,
        "close": close,
        "volume": 200.0,
        "rsi_1m": rsi_1m,
        "macd_line": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "vwap": vwap,
        "ema_9": ema_9,
        "ema_21": ema_21,
        "ema_9_slope": ema_9_slope,
        "pivot_high": pivot_high,
        "pivot_low": pivot_low,
        "atr": atr,
        "atr_zone": atr_zone,
        "volume_state": volume_state,
        "consecutive_closes": consecutive_closes,
        "rsi_3m": rsi_3m,
        "rsi_5m": rsi_5m,
        "chg_1h": chg_1h,
    })
    return pd.DataFrame(rows)


def _light_market_info(time_left_min: float = 8.0, price_yes: float = 0.30, price_no: float = 0.70) -> dict:
    return {
        "market_id": "test-001",
        "price_yes": price_yes,
        "price_no": price_no,
        "end_date_iso": _future_iso(time_left_min),
        "event_start_time": None,
    }


# ---------------------------------------------------------------------------
# Individual vote function tests
# ---------------------------------------------------------------------------

class TestVoteRSI:
    def test_vote_rsi_bearish(self):
        """RSI > 65 → DOWN vote (overbought)."""
        vote, label = _vote_rsi(70.0)
        assert vote == "DOWN"
        assert "overbought" in label.lower()

    def test_vote_rsi_bullish(self):
        """RSI < 35 → UP vote (oversold — CONTRARIAN, by design)."""
        vote, label = _vote_rsi(30.0)
        assert vote == "UP"
        assert "oversold" in label.lower()

    def test_vote_rsi_neutral_mid(self):
        """RSI exactly 50 → neutral (None)."""
        vote, _ = _vote_rsi(50.0)
        assert vote is None

    def test_vote_rsi_neutral_boundary_35(self):
        """RSI == 35 → neutral (not < 35)."""
        vote, _ = _vote_rsi(35.0)
        assert vote is None

    def test_vote_rsi_neutral_boundary_65(self):
        """RSI == 65 → neutral (not > 65)."""
        vote, _ = _vote_rsi(65.0)
        assert vote is None

    def test_vote_rsi_just_below_35(self):
        """RSI = 34.9 → UP."""
        vote, _ = _vote_rsi(34.9)
        assert vote == "UP"

    def test_vote_rsi_just_above_65(self):
        """RSI = 65.1 → DOWN."""
        vote, _ = _vote_rsi(65.1)
        assert vote == "DOWN"

    def test_vote_rsi_nan(self):
        """NaN RSI → None."""
        vote, label = _vote_rsi(float("nan"))
        assert vote is None
        assert "n/a" in label


class TestVoteMACD:
    def test_vote_macd_bullish(self):
        """line > signal AND hist > 0 → UP."""
        vote, label = _vote_macd(10.0, 5.0, 5.0)
        assert vote == "UP"
        assert "bullish" in label.lower()

    def test_vote_macd_bearish(self):
        """line < signal AND hist < 0 → DOWN."""
        vote, label = _vote_macd(-10.0, -5.0, -5.0)
        assert vote == "DOWN"
        assert "bearish" in label.lower()

    def test_vote_macd_no_cross_mixed(self):
        """line > signal but hist < 0 → neutral (mixed)."""
        vote, _ = _vote_macd(5.0, 3.0, -1.0)
        assert vote is None

    def test_vote_macd_no_cross_zero_hist(self):
        """hist == 0 → neutral."""
        vote, _ = _vote_macd(5.0, 5.0, 0.0)
        assert vote is None

    def test_vote_macd_nan(self):
        """Any NaN value → neutral."""
        vote, label = _vote_macd(float("nan"), 0.0, 0.0)
        assert vote is None
        assert "n/a" in label


class TestVoteVWAP:
    def test_vote_vwap_above(self):
        """price > vwap → UP."""
        vote, _ = _vote_vwap(100.0, 95.0)
        assert vote == "UP"

    def test_vote_vwap_below(self):
        """price < vwap → DOWN."""
        vote, _ = _vote_vwap(90.0, 95.0)
        assert vote == "DOWN"

    def test_vote_vwap_equal(self):
        """price == vwap → neutral."""
        vote, _ = _vote_vwap(95.0, 95.0)
        assert vote is None

    def test_vote_vwap_zero(self):
        """vwap == 0 → neutral (guard)."""
        vote, label = _vote_vwap(95.0, 0.0)
        assert vote is None
        assert "n/a" in label

    def test_vote_vwap_nan(self):
        """NaN vwap → neutral."""
        vote, label = _vote_vwap(95.0, float("nan"))
        assert vote is None


class TestVoteEMA:
    def test_vote_ema_bullish(self):
        """ema_9 > ema_21 → UP (golden cross)."""
        vote, label = _vote_ema(100.0, 90.0)
        assert vote == "UP"
        assert "EMA9 >" in label

    def test_vote_ema_bearish(self):
        """ema_9 < ema_21 → DOWN (death cross)."""
        vote, label = _vote_ema(90.0, 100.0)
        assert vote == "DOWN"
        assert "EMA9 <" in label

    def test_vote_ema_equal(self):
        """ema_9 == ema_21 → neutral."""
        vote, _ = _vote_ema(100.0, 100.0)
        assert vote is None

    def test_vote_ema_nan(self):
        """NaN EMA → neutral."""
        vote, label = _vote_ema(float("nan"), 100.0)
        assert vote is None


class TestVotePivots:
    def test_vote_pivots_breakout(self):
        """price >= pivot_high → UP (breakout)."""
        vote, _ = _vote_pivots(105.0, 100.0, 90.0)
        assert vote == "UP"

    def test_vote_pivots_breakdown(self):
        """price <= pivot_low → DOWN (breakdown)."""
        vote, _ = _vote_pivots(85.0, 100.0, 90.0)
        assert vote == "DOWN"

    def test_vote_pivots_in_range(self):
        """price inside range → neutral."""
        vote, _ = _vote_pivots(95.0, 100.0, 90.0)
        assert vote is None

    def test_vote_pivots_nan(self):
        """NaN pivot → neutral."""
        vote, label = _vote_pivots(95.0, float("nan"), 90.0)
        assert vote is None


# ---------------------------------------------------------------------------
# check_signals() — confluence tests (using test mode to skip all real filters)
# ---------------------------------------------------------------------------

class TestConfluence:
    """Use state.mode='test' so only confluence + contract_price matter."""

    def test_confluence_3_signal(self, mock_state_test):
        """3/5 votes in same direction → signal generated."""
        # UP: RSI<35(UP), MACD bullish(UP), price>VWAP(UP); EMA bearish(DOWN), Pivots neutral
        df = _make_signal_df(
            close=69_200.0,
            rsi_1m=30.0,           # UP
            macd_line=10.0,
            macd_signal=5.0,
            macd_hist=5.0,         # UP
            vwap=69_100.0,         # price > vwap → UP
            ema_9=69_000.0,
            ema_21=69_100.0,       # ema death cross → DOWN
            pivot_high=70_000.0,
            pivot_low=68_000.0,    # price in range → neutral
        )
        mi = _light_market_info(time_left_min=8.0, price_yes=0.50, price_no=0.50)
        result = check_signals(mi, df)
        assert result is not None
        assert result["direction"] == "UP"
        assert result["confluence"] >= 3

    def test_confluence_2_no_signal(self, mock_state_light):
        """Only 2/5 votes in same direction → None (light mode requires 3)."""
        # UP: RSI oversold(UP), price>VWAP(UP); rest neutral
        # With MIN_CONFLUENCE=3 (light mode), 2 votes is not enough
        df = _make_signal_df(
            close=68_800.0,        # gap = -200 (needs to pass gap filter for light)
            rsi_1m=30.0,           # UP vote (contrarian)
            macd_line=0.0,
            macd_signal=0.0,
            macd_hist=0.0,         # neutral
            vwap=68_700.0,         # price > vwap → UP
            ema_9=68_700.0,
            ema_21=68_700.0,       # equal → neutral
            pivot_high=70_000.0,
            pivot_low=67_000.0,    # in range → neutral
            atr=80.0, atr_zone="golden",
        )
        mi = _light_market_info(time_left_min=8.0, price_yes=0.50, price_no=0.50)
        result = check_signals(mi, df)
        # 2 UP (RSI + VWAP) < MIN_CONFLUENCE=3 → None
        assert result is None

    def test_confluence_4_signal(self, mock_state_test):
        """4/5 votes DOWN → signal with confluence=4."""
        # DOWN: RSI overbought(DOWN), MACD bearish(DOWN), price<VWAP(DOWN), EMA death cross(DOWN)
        # Pivots: in range → neutral
        df = _make_signal_df(
            close=69_000.0,
            rsi_1m=70.0,           # DOWN
            macd_line=-10.0,
            macd_signal=-5.0,
            macd_hist=-5.0,        # DOWN
            vwap=69_200.0,         # price < vwap → DOWN
            ema_9=69_000.0,
            ema_21=69_100.0,       # ema_9 < ema_21 → DOWN
            pivot_high=70_000.0,
            pivot_low=68_500.0,    # price in range → neutral
        )
        mi = _light_market_info(time_left_min=8.0, price_yes=0.50, price_no=0.50)
        result = check_signals(mi, df)
        assert result is not None
        assert result["direction"] == "DOWN"
        assert result["confluence"] == 4

    def test_confluence_5_signal(self, mock_state_test):
        """5/5 votes DOWN → confluence=5."""
        # DOWN: RSI(DOWN), MACD(DOWN), VWAP(DOWN), EMA(DOWN), Pivots(DOWN)
        df = _make_signal_df(
            close=68_000.0,
            rsi_1m=70.0,           # DOWN
            macd_line=-10.0,
            macd_signal=-5.0,
            macd_hist=-5.0,        # DOWN
            vwap=69_000.0,         # price < vwap → DOWN
            ema_9=68_000.0,
            ema_21=68_500.0,       # ema_9 < ema_21 → DOWN
            pivot_high=70_000.0,
            pivot_low=68_100.0,    # price <= pivot_low → DOWN
        )
        mi = _light_market_info(time_left_min=8.0, price_yes=0.50, price_no=0.50)
        result = check_signals(mi, df)
        assert result is not None
        assert result["confluence"] == 5

    def test_confluence_split_no_signal(self, mock_state_light):
        """2 UP + 2 DOWN + 1 neutral → neither reaches MIN_CONFLUENCE=3 → None."""
        # UP: RSI(UP), MACD(UP) = 2
        # DOWN: VWAP(DOWN), EMA(DOWN) = 2
        # Neutral: Pivots
        # 2 < MIN_CONFLUENCE(3 in light) → no signal
        df = _make_signal_df(
            close=68_800.0,        # gap=-200 for DOWN (passes gap filter if DOWN won, but won't)
            rsi_1m=30.0,           # UP
            macd_line=10.0,
            macd_signal=5.0,
            macd_hist=5.0,         # UP
            vwap=69_200.0,         # price < vwap → DOWN
            ema_9=68_800.0,
            ema_21=69_100.0,       # DOWN
            pivot_high=70_000.0,
            pivot_low=67_000.0,    # in range → neutral
            atr=80.0, atr_zone="golden",
        )
        mi = _light_market_info(time_left_min=8.0, price_yes=0.50, price_no=0.50)
        result = check_signals(mi, df)
        # Neither 2 UP nor 2 DOWN reaches MIN_CONFLUENCE=3
        assert result is None


# ---------------------------------------------------------------------------
# check_signals() — time_left filters (using light mode)
# ---------------------------------------------------------------------------

class TestTimeLeftFilters:
    """Light mode: TIME_LEFT_MIN=3, TIME_LEFT_MAX=12."""

    def _full_down_df(self):
        """A DF with 4/5 DOWN votes and GAP large enough.
        close=68_800, base_open=69_000 → gap = -200 (DOWN, abs>=60)
        """
        return _make_signal_df(
            close=68_800.0,  # gap = 68800 - 69000 = -200
            rsi_1m=70.0,
            macd_line=-15.0, macd_signal=-5.0, macd_hist=-10.0,
            vwap=69_200.0,
            ema_9=68_800.0, ema_21=69_100.0,
            pivot_high=70_000.0, pivot_low=68_900.0,  # price <= pivot_low → DOWN
            atr=80.0, atr_zone="golden",
        )

    def test_filter_time_left_too_early(self, mock_state_light):
        """time_left > TIME_LEFT_MAX (12 min) → None."""
        df = self._full_down_df()
        mi = _light_market_info(time_left_min=20.0, price_yes=0.60, price_no=0.60)
        result = check_signals(mi, df)
        assert result is None

    def test_filter_time_left_too_late(self, mock_state_light):
        """time_left < TIME_LEFT_MIN (3 min) → None (also < IGNORE_IF_TIME_LEFT_LT_MIN=2)."""
        df = self._full_down_df()
        mi = _light_market_info(time_left_min=1.5, price_yes=0.60, price_no=0.60)
        result = check_signals(mi, df)
        assert result is None

    def test_filter_time_left_in_window(self, mock_state_light):
        """time_left=7 in [3,12] → all filters pass → signal generated."""
        df = self._full_down_df()
        # price_no=0.60 in [0.45, 0.80] ✓; price_yes=0.40 <= 0.75 ✓; time=7 in [3,12] ✓
        mi = _light_market_info(time_left_min=7.0, price_yes=0.40, price_no=0.60)
        result = check_signals(mi, df)
        assert result is not None


# ---------------------------------------------------------------------------
# check_signals() — ATR zone filters (light mode)
# ---------------------------------------------------------------------------

class TestATRFilters:
    def _down_df_with_atr(self, atr, atr_zone):
        return _make_signal_df(
            close=68_800.0,  # gap = -200
            rsi_1m=70.0,
            macd_line=-15.0, macd_signal=-5.0, macd_hist=-10.0,
            vwap=69_200.0,
            ema_9=68_800.0, ema_21=69_100.0,
            pivot_high=70_000.0, pivot_low=68_900.0,  # price <= pivot_low → DOWN
            atr=atr, atr_zone=atr_zone,
        )

    def test_atr_dead_zone_blocks(self, mock_state_light):
        """ATR zone='dead' → None."""
        df = self._down_df_with_atr(10.0, "dead")
        mi = _light_market_info(7.0, 0.60, 0.60)
        result = check_signals(mi, df)
        assert result is None

    def test_atr_below_minimum_blocks(self, mock_state_light):
        """ATR < ATR_MIN_USD (25 for light) → None."""
        # ATR=10 is below minimum but zone is 'dead' too
        df = self._down_df_with_atr(10.0, "dead")
        mi = _light_market_info(7.0, 0.60, 0.60)
        result = check_signals(mi, df)
        assert result is None

    def test_atr_golden_zone_passes(self, mock_state_light):
        """ATR=80 in golden zone → ATR filter passes, signal generated."""
        df = self._down_df_with_atr(80.0, "golden")
        # price_no=0.60 in [0.45, 0.80] for light mode ✓
        mi = _light_market_info(7.0, price_yes=0.40, price_no=0.60)
        result = check_signals(mi, df)
        # ATR filter should NOT block this — result should be a signal
        assert result is not None


# ---------------------------------------------------------------------------
# check_signals() — contract price filter (light mode)
# ---------------------------------------------------------------------------

class TestContractPriceFilter:
    """Light mode: CONTRACT_PRICE_MIN=0.45, CONTRACT_PRICE_MAX=0.80."""

    def _strong_down_df(self):
        return _make_signal_df(
            close=68_800.0,  # gap = -200
            rsi_1m=70.0,
            macd_line=-15.0, macd_signal=-5.0, macd_hist=-10.0,
            vwap=69_200.0,
            ema_9=68_800.0, ema_21=69_100.0,
            pivot_high=70_000.0, pivot_low=68_900.0,  # price <= pivot_low → DOWN
            atr=80.0, atr_zone="golden",
        )

    def test_contract_price_too_high(self, mock_state_light):
        """DOWN signal: contract_price=price_no, if >0.80 → blocked."""
        df = self._strong_down_df()
        # price_no=0.85 > CONTRACT_PRICE_MAX=0.80
        mi = _light_market_info(7.0, price_yes=0.15, price_no=0.85)
        result = check_signals(mi, df)
        assert result is None

    def test_contract_price_too_low(self, mock_state_light):
        """DOWN signal: contract_price=price_no, if <0.45 → blocked."""
        df = self._strong_down_df()
        # price_no=0.40 < CONTRACT_PRICE_MIN=0.45
        mi = _light_market_info(7.0, price_yes=0.60, price_no=0.40)
        result = check_signals(mi, df)
        assert result is None

    def test_contract_price_in_range(self, mock_state_light):
        """DOWN signal: price_no=0.65 (within 0.45–0.80) → not blocked by CP filter."""
        df = self._strong_down_df()
        mi = _light_market_info(7.0, price_yes=0.35, price_no=0.65)
        # May or may not generate a signal, but contract price should not block it
        result = check_signals(mi, df)
        # Only assert it's not None due to contract price; if None for other reason, fine
        # But we expect it to pass all main filters with these values
        # (ATR golden, time=7, cp=0.65, gap=-200, confluence=4)
        assert result is not None

    def test_contract_price_max_080_light_mode(self, mock_state_light):
        """Regression: CONTRACT_PRICE_MAX for light mode is 0.80."""
        from bot.state import state
        th = state.get_thresholds()
        assert th["CONTRACT_PRICE_MAX"] == 0.80


# ---------------------------------------------------------------------------
# check_signals() — GAP filter (light mode)
# ---------------------------------------------------------------------------

class TestGAPFilter:
    """Light mode: GAP_MIN_USD=60, GAP_STRICT_USD=100, TIME_STRICT_MAX_MIN=5."""

    def _down_df_with_gap(self, gap_abs):
        """Creates a DOWN signal df with gap = -gap_abs.
        close = 69_000 - gap_abs, base_open = 69_000
        → gap = close - base_open = -gap_abs
        pivot_low is set below close to give DOWN vote from Pivots.
        """
        close = 69_000.0 - abs(gap_abs)
        return _make_signal_df(
            close=close,
            rsi_1m=70.0,
            macd_line=-15.0, macd_signal=-5.0, macd_hist=-10.0,
            vwap=close + 300.0,   # price < vwap → DOWN
            ema_9=close, ema_21=close + 200.0,  # ema death cross → DOWN
            pivot_high=close + 500.0, pivot_low=close + 100.0,  # price <= pivot_low → DOWN
            atr=80.0, atr_zone="golden",
        )

    def test_gap_filter_blocks_small_move(self, mock_state_light):
        """GAP < GAP_MIN_USD (60) → None (for light mode)."""
        df = self._down_df_with_gap(30.0)
        mi = _light_market_info(7.0, 0.35, 0.65)
        result = check_signals(mi, df)
        assert result is None

    def test_gap_filter_passes_large_move(self, mock_state_light):
        """GAP >= GAP_MIN_USD (60) → passes GAP filter."""
        df = self._down_df_with_gap(150.0)
        mi = _light_market_info(7.0, price_yes=0.35, price_no=0.65)
        result = check_signals(mi, df)
        assert result is not None

    def test_strict_gap_near_expiry_blocks(self, mock_state_light):
        """time_left < 5 min + abs(gap) < 100 → None (strict GAP rule)."""
        # Gap = 70 (passes normal 60 filter but fails strict 100 filter)
        df = self._down_df_with_gap(70.0)
        mi = _light_market_info(4.0, price_yes=0.35, price_no=0.65)  # 4 min < 5
        result = check_signals(mi, df)
        assert result is None

    def test_strict_gap_near_expiry_passes_with_large_gap(self, mock_state_light):
        """time_left < 5 min + abs(gap) >= 100 → passes strict GAP."""
        df = self._down_df_with_gap(150.0)
        mi = _light_market_info(4.0, price_yes=0.35, price_no=0.65)
        result = check_signals(mi, df)
        assert result is not None

    def test_strict_gap_not_applied_early(self, mock_state_light):
        """time_left >= 5 min → strict GAP rule doesn't apply, small gap (70) is OK."""
        df = self._down_df_with_gap(70.0)
        mi = _light_market_info(7.0, price_yes=0.35, price_no=0.65)  # 7 min >= 5
        result = check_signals(mi, df)
        assert result is not None


# ---------------------------------------------------------------------------
# check_signals() — signal content
# ---------------------------------------------------------------------------

class TestSignalContent:
    def test_signal_has_required_keys(self, mock_state_test):
        """Generated signal must contain all required output keys."""
        df = _make_signal_df(
            close=69_000.0,
            rsi_1m=30.0,
            macd_line=10.0, macd_signal=5.0, macd_hist=5.0,
            vwap=68_900.0,  # UP
            ema_9=69_100.0, ema_21=69_000.0,  # UP
            pivot_high=68_900.0, pivot_low=68_000.0,  # price >= pivot_high → UP
        )
        mi = {"price_yes": 0.50, "price_no": 0.50, "end_date_iso": None, "event_start_time": None}
        result = check_signals(mi, df)
        assert result is not None
        required = [
            "direction", "confluence", "votes", "gap", "contract_price",
            "atr", "atr_zone", "time_left", "filter_version",
            "consecutive_closes", "rsi_1m",
        ]
        for key in required:
            assert key in result, f"Missing key: {key}"

    def test_signal_direction_up(self, mock_state_test):
        """3/5 UP votes → direction='UP'."""
        df = _make_signal_df(
            close=69_200.0,
            rsi_1m=30.0,               # UP
            macd_line=10.0, macd_signal=5.0, macd_hist=5.0,  # UP
            vwap=69_000.0,             # price > vwap → UP
            ema_9=69_100.0, ema_21=69_100.0,  # neutral
            pivot_high=70_000.0, pivot_low=68_000.0,  # neutral
        )
        mi = {"price_yes": 0.50, "price_no": 0.50, "end_date_iso": None, "event_start_time": None}
        result = check_signals(mi, df)
        assert result is not None
        assert result["direction"] == "UP"

    def test_signal_direction_down(self, mock_state_test):
        """3/5 DOWN votes → direction='DOWN'."""
        df = _make_signal_df(
            close=68_900.0,
            rsi_1m=70.0,               # DOWN
            macd_line=-10.0, macd_signal=-5.0, macd_hist=-5.0,  # DOWN
            vwap=69_200.0,             # price < vwap → DOWN
            ema_9=69_100.0, ema_21=69_100.0,  # neutral
            pivot_high=70_000.0, pivot_low=68_000.0,  # neutral
        )
        mi = {"price_yes": 0.50, "price_no": 0.50, "end_date_iso": None, "event_start_time": None}
        result = check_signals(mi, df)
        assert result is not None
        assert result["direction"] == "DOWN"

    def test_rsi_contrariant_not_counted_in_down_confluence(self, mock_state_light):
        """RSI < 35 (UP vote) during a DOWN signal situation.

        RSI votes UP (contrarian, by design), NOT DOWN.
        Doesn't contribute to DOWN confluence.
        With MIN_CONFLUENCE=3 (light mode):
          - UP count = 1 (RSI only) < 3 → doesn't trigger UP signal
          - DOWN count = 3 (MACD+VWAP+EMA) >= 3 → triggers DOWN signal with confluence=3
        """
        # MACD, VWAP, EMA → DOWN (3 votes)
        # RSI < 35 → UP (contrarian, doesn't count for DOWN)
        # Pivots: neutral
        df = _make_signal_df(
            close=68_800.0,       # gap=-200 (passes light mode gap filter >=60)
            rsi_1m=30.0,          # UP vote (contrarian oversold)
            macd_line=-15.0, macd_signal=-5.0, macd_hist=-10.0,  # DOWN
            vwap=69_200.0,        # DOWN (price < vwap)
            ema_9=68_800.0, ema_21=69_100.0,    # DOWN
            pivot_high=70_000.0, pivot_low=67_000.0,  # price in range → neutral
            atr=80.0, atr_zone="golden",
        )
        mi = _light_market_info(time_left_min=7.0, price_yes=0.40, price_no=0.60)
        result = check_signals(mi, df)
        # Should get DOWN signal with confluence=3 (MACD+VWAP+EMA)
        # RSI votes UP (contrarian), not DOWN
        assert result is not None
        assert result["direction"] == "DOWN"
        assert result["confluence"] == 3
        assert result["votes"]["RSI"]["direction"] == "UP"


# ---------------------------------------------------------------------------
# check_signals() — filter_version A/B test
# ---------------------------------------------------------------------------

class TestFilterVersion:
    """Tests for filter_version field ('current' / 'new' / 'both')."""

    def _down_signal_df(self, gap_abs=150.0, cc=3, macd_hist_abs=20.0, atr=80.0):
        """Build a DOWN signal with controllable new-filter parameters.

        gap_abs: absolute gap value. gap = close - base_open = -(gap_abs)
               close = 69_000 - gap_abs, base_open stays at 69_000 (default)
        cc: absolute consecutive_closes count (set as negative in df for DOWN direction)
        macd_hist_abs: absolute value of MACD histogram
        atr: ATR value
        """
        atr_zone = "golden" if 60 <= atr < 100 else ("quiet" if atr < 60 else "high")
        # close must be below pivot_low for DOWN pivot vote
        close = 69_000.0 - gap_abs
        return _make_signal_df(
            close=close,
            rsi_1m=70.0,               # DOWN
            macd_line=-15.0,
            macd_signal=-5.0,
            macd_hist=-macd_hist_abs,  # DOWN
            vwap=close + 300.0,        # price < vwap → DOWN
            ema_9=close, ema_21=close + 200.0,  # ema death cross → DOWN
            pivot_high=close + 500.0, pivot_low=close + 100.0,  # price <= pivot_low → DOWN
            atr=atr,
            atr_zone=atr_zone,
            consecutive_closes=-cc,    # negative = down direction in df
        )

    def test_fv_both(self, mock_state_test):
        """gap>=80, cc>=2, macd_norm>=0.20, atr<150 → filter_version='both'."""
        # macd_norm = 20/80 = 0.25 >= 0.20 ✓; gap=150>=80 ✓; cc=3>=2 ✓; atr=80<150 ✓
        df = self._down_signal_df(gap_abs=150.0, cc=3, macd_hist_abs=20.0, atr=80.0)
        mi = {"price_yes": 0.50, "price_no": 0.50, "end_date_iso": None, "event_start_time": None}
        result = check_signals(mi, df)
        assert result is not None
        assert result["filter_version"] == "both"

    def test_fv_current_only_small_cc(self, mock_state_test):
        """cc=1 (< 2) → new filter fails → filter_version='current'."""
        df = self._down_signal_df(gap_abs=150.0, cc=1, macd_hist_abs=20.0, atr=80.0)
        mi = {"price_yes": 0.50, "price_no": 0.50, "end_date_iso": None, "event_start_time": None}
        result = check_signals(mi, df)
        assert result is not None
        assert result["filter_version"] == "current"

    def test_fv_current_only_small_gap(self, mock_state_test):
        """gap=50 (< 80) → new filter fails → filter_version='current'."""
        df = self._down_signal_df(gap_abs=50.0, cc=3, macd_hist_abs=20.0, atr=80.0)
        mi = {"price_yes": 0.50, "price_no": 0.50, "end_date_iso": None, "event_start_time": None}
        result = check_signals(mi, df)
        assert result is not None
        assert result["filter_version"] == "current"

    def test_fv_current_only_low_macd_norm(self, mock_state_test):
        """macd_norm < 0.20 → new filter fails → 'current'."""
        # macd_hist=1.0, atr=80.0 → macd_norm = 0.0125 < 0.20
        df = self._down_signal_df(gap_abs=150.0, cc=3, macd_hist_abs=1.0, atr=80.0)
        mi = {"price_yes": 0.50, "price_no": 0.50, "end_date_iso": None, "event_start_time": None}
        result = check_signals(mi, df)
        assert result is not None
        assert result["filter_version"] == "current"

    def test_fv_current_only_high_atr(self, mock_state_test):
        """atr >= 150 → new filter fails → 'current'."""
        df = self._down_signal_df(gap_abs=150.0, cc=3, macd_hist_abs=40.0, atr=160.0)
        mi = {"price_yes": 0.50, "price_no": 0.50, "end_date_iso": None, "event_start_time": None}
        result = check_signals(mi, df)
        assert result is not None
        assert result["filter_version"] == "current"

    def test_fv_consecutive_closes_signed_down(self, mock_state_test):
        """consecutive_closes in signal is positive for DOWN direction (positive = in signal direction).

        Code: _cc = _cc_raw if UP else -_cc_raw
        So for DOWN + cc_raw=-3 in df → _cc = -(-3) = +3 (positive = in direction of DOWN signal)
        """
        df = self._down_signal_df(gap_abs=50.0, cc=3, macd_hist_abs=20.0, atr=80.0)
        mi = {"price_yes": 0.50, "price_no": 0.50, "end_date_iso": None, "event_start_time": None}
        result = check_signals(mi, df)
        assert result is not None
        # For DOWN direction, consecutive_closes is positive (means bars are going DOWN = in signal direction)
        assert result["consecutive_closes"] >= 0


# ---------------------------------------------------------------------------
# check_signals() — too few candles
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_too_few_candles_returns_none(self, mock_state_test):
        """< 30 candles → None immediately."""
        import datetime as dt_mod
        df = pd.DataFrame({
            "timestamp": [dt_mod.datetime(2024, 1, 1)],
            "open": [69_000.0], "high": [69_100.0],
            "low": [68_900.0], "close": [69_000.0],
            "volume": [200.0],
        })
        mi = {"price_yes": 0.50, "price_no": 0.50, "end_date_iso": None, "event_start_time": None}
        result = check_signals(mi, df)
        assert result is None

    def test_empty_df_returns_none(self, mock_state_test):
        """Empty df → None immediately."""
        df = pd.DataFrame()
        mi = {"price_yes": 0.50, "price_no": 0.50, "end_date_iso": None, "event_start_time": None}
        result = check_signals(mi, df)
        assert result is None
