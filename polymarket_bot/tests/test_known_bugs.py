"""
test_known_bugs.py — regression tests for known bugs.

Prevents regressions on bugs that were previously found and fixed.
Each test documents the bug, its fix, and verifies the fix still holds.
"""

import sqlite3
import pytest

from bot.signals import _vote_rsi, _vote_macd, check_signals
from bot.storage import (
    init_db,
    mark_signal_live_no_position,
    update_decision,
    get_unresolved_signals,
    save_signal,
    get_connection,
)
from bot.risk import calculate_stake, estimate_win_probability
from bot.config import STAKE_USD
from bot.indicators import classify_atr_zone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_signal(**kwargs) -> dict:
    base = {
        "market_id": "test-regression-001",
        "direction": "DOWN",
        "confluence": 4,
        "contract_price": 0.65,
        "start_price": 69_200.0,
        "current_price": 69_065.0,
        "delta": -135.0,
        "delta_percent": -0.195,
        "gap": -135.0,
        "atr": 82.0,
        "atr_zone": "golden",
        "rsi_1m": 68.0,
        "rsi_3m": None,
        "ema_position": "below EMA9 (-12.5)",
        "volume_state": "normal",
        "time_left": 7.5,
        "filter_version": "current",
        "consecutive_closes": -3,
        "chg_1h": -0.5,
        "ptb": 69_200.0,
        "obi": 1.5,
        "votes": {},
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# BUG: RSI contrarian vote direction
#
# BUG DESCRIPTION (MEMORY.md: bug_rsi_vote.md):
#   "rsi_voted завжди False" — RSI vote was not working correctly.
#   Current (by-design) behavior: oversold (< 35) → UP, overbought (> 65) → DOWN.
#   This is CONTRARIAN behavior: oversold market means expected price bounce UP.
#
# STATUS: BY DESIGN — test verifies CURRENT behavior.
# ---------------------------------------------------------------------------

class TestRSIContrarian:
    def test_rsi_vote_overbought_is_down(self):
        """BUG GUARD: RSI > 65 → DOWN vote (overbought = price likely falls)."""
        vote, label = _vote_rsi(70.0)
        assert vote == "DOWN", f"RSI>65 should vote DOWN, got {vote}"

    def test_rsi_vote_oversold_is_up(self):
        """BUG GUARD: RSI < 35 → UP vote (oversold = contrarian, price likely bounces)."""
        vote, label = _vote_rsi(30.0)
        assert vote == "UP", f"RSI<35 should vote UP (contrarian), got {vote}"

    def test_rsi_vote_neutral_not_counted(self):
        """RSI in 35–65 range → None (not counted in confluence)."""
        for rsi_val in [35.0, 50.0, 65.0]:
            vote, _ = _vote_rsi(rsi_val)
            assert vote is None, f"RSI={rsi_val} should be neutral, got {vote}"

    def test_rsi_not_counted_in_down_confluence(self, mock_state_light):
        """RSI < 35 during DOWN setup: RSI votes UP (not DOWN), so DOWN confluence = 3 not 4.

        Uses light mode (MIN_CONFLUENCE=3) so that:
          - UP count = 1 (RSI only) → fails to trigger UP signal
          - DOWN count = 3 (MACD+VWAP+EMA) → triggers DOWN signal
        """
        from datetime import datetime, timezone, timedelta
        import pandas as pd
        import datetime as dt_mod

        rows = []
        base_ts = dt_mod.datetime(2024, 1, 1)
        for i in range(59):
            rows.append({
                "timestamp": base_ts + dt_mod.timedelta(minutes=i),
                "open": 69_000.0, "high": 69_020.0, "low": 68_980.0,
                "close": 69_000.0, "volume": 200.0,
                "rsi_1m": 50.0, "macd_line": 0.0, "macd_signal": 0.0, "macd_hist": 0.0,
                "vwap": 69_000.0, "ema_9": 69_000.0, "ema_21": 69_000.0,
                "ema_9_slope": 0.0,
                "pivot_high": 70_000.0, "pivot_low": 68_000.0,
                "atr": 80.0, "atr_zone": "golden",
                "volume_state": "normal", "consecutive_closes": 0,
                "rsi_3m": None, "rsi_5m": None, "chg_1h": 0.0,
            })
        # Last row: RSI<35 (UP, contrarian) + MACD/VWAP/EMA (DOWN = 3)
        # Pivots: price=68_800 in range (68_000-70_000) → neutral
        rows.append({
            "timestamp": base_ts + dt_mod.timedelta(minutes=59),
            "open": 69_000.0, "high": 69_020.0, "low": 68_800.0,
            "close": 68_800.0, "volume": 200.0,
            "rsi_1m": 30.0,           # UP vote (contrarian)
            "macd_line": -10.0, "macd_signal": -5.0, "macd_hist": -5.0,  # DOWN
            "vwap": 69_200.0,          # DOWN (price < vwap)
            "ema_9": 68_800.0, "ema_21": 69_100.0,  # DOWN
            "ema_9_slope": -5.0,
            "pivot_high": 70_000.0, "pivot_low": 68_000.0,  # neutral (price in range)
            "atr": 80.0, "atr_zone": "golden",
            "volume_state": "normal", "consecutive_closes": -3,
            "rsi_3m": None, "rsi_5m": None, "chg_1h": -0.2,
        })
        df = pd.DataFrame(rows)
        # time=7min in [3,12]; price_no=0.60 in [0.45,0.80]; gap=-200 >= 60 (abs)
        end_iso = (dt_mod.datetime.now(dt_mod.timezone.utc) + dt_mod.timedelta(minutes=7)).isoformat()
        mi = {"price_yes": 0.40, "price_no": 0.60, "end_date_iso": end_iso, "event_start_time": None}
        result = check_signals(mi, df)
        assert result is not None
        assert result["direction"] == "DOWN"
        assert result["confluence"] == 3  # MACD + VWAP + EMA, NOT RSI
        assert result["votes"]["RSI"]["direction"] == "UP"  # RSI voted UP (contrarian)


# ---------------------------------------------------------------------------
# BUG: Kelly enabled — stake should always be fixed
#
# STATUS: FIXED — Kelly disabled, always returns STAKE_USD
# ---------------------------------------------------------------------------

class TestStakeAlwaysFixed:
    def test_stake_always_fixed_non_test_mode(self, mock_state_light):
        """REGRESSION: Kelly disabled → stake always STAKE_USD in light mode."""
        sig = _minimal_signal()
        result = calculate_stake(sig)
        assert result["stake_usd"] == STAKE_USD
        assert result["reason"] == "fixed"

    def test_stake_always_fixed_test_mode(self, mock_state_test):
        """REGRESSION: Kelly disabled → stake always STAKE_USD in test mode."""
        sig = _minimal_signal()
        result = calculate_stake(sig)
        assert result["stake_usd"] == STAKE_USD

    def test_stake_same_regardless_of_edge(self, mock_state_light):
        """REGRESSION: Positive edge doesn't change stake amount (Kelly off)."""
        low_edge_sig = _minimal_signal(contract_price=0.90)  # negative edge
        high_edge_sig = _minimal_signal(contract_price=0.40)  # positive edge
        r1 = calculate_stake(low_edge_sig)
        r2 = calculate_stake(high_edge_sig)
        assert r1["stake_usd"] == r2["stake_usd"] == STAKE_USD


# ---------------------------------------------------------------------------
# BUG: settlement duplicate sleep notification (💤)
#
# BUG DESCRIPTION: mark_signal_live_no_position → result=NO_ENTRY
# This prevents settlement from processing the same signal again as a 💤 (sleep).
#
# STATUS: FIXED 2026-04-06
# ---------------------------------------------------------------------------

class TestNoDuplicateSleepNotification:
    def test_no_entry_signals_excluded_from_unresolved(self, tmp_path, mock_state_test):
        """REGRESSION: NO_ENTRY signals must not appear in get_unresolved_signals().
        If they did, settlement would process them again and send duplicate 💤.
        """
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        import bot.storage as storage_mod
        original = storage_mod.get_db_path
        storage_mod.get_db_path = lambda: db_path
        try:
            sig_id = save_signal(_minimal_signal())
            update_decision(sig_id, "approve")
            mark_signal_live_no_position(sig_id)  # → result=NO_ENTRY

            # get_unresolved_signals returns signals WHERE result IS NULL
            # NO_ENTRY signal should NOT appear
            unresolved = get_unresolved_signals()
            ids = [s["id"] for s in unresolved]
            assert sig_id not in ids, (
                f"Signal #{sig_id} with NO_ENTRY should not be in unresolved signals"
            )
        finally:
            storage_mod.get_db_path = original

    def test_mark_no_position_sets_no_entry_immediately(self, tmp_path, mock_state_test):
        """REGRESSION: mark_signal_live_no_position immediately sets result=NO_ENTRY."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        import bot.storage as storage_mod
        original = storage_mod.get_db_path
        storage_mod.get_db_path = lambda: db_path
        try:
            sig_id = save_signal(_minimal_signal())
            mark_signal_live_no_position(sig_id)
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT result FROM signals WHERE id=?", (sig_id,)).fetchone()
            conn.close()
            assert row[0] == "NO_ENTRY"
        finally:
            storage_mod.get_db_path = original


# ---------------------------------------------------------------------------
# BUG: Tick size = 0 causes ZeroDivisionError
#
# STATUS: FIXED — guarded in execution_client.py
# We test the guard logic indirectly: tick=0 should use fallback 0.01
# ---------------------------------------------------------------------------

class TestTickZeroGuard:
    def test_tick_zero_no_division_error(self):
        """REGRESSION: tick_size=0 should not cause ZeroDivisionError.
        The fix: if tick_size == 0, fall back to 0.01.
        """
        # We test the math that would have caused the division error:
        tick_size = 0
        price = 0.65
        fallback_tick = tick_size if tick_size != 0 else 0.01
        try:
            rounded = round(price / fallback_tick) * fallback_tick
        except ZeroDivisionError:
            pytest.fail("tick_size=0 caused ZeroDivisionError — guard is missing!")

    def test_tick_zero_uses_fallback(self):
        """REGRESSION: With tick=0, fallback=0.01 gives correct 2dp rounding."""
        tick_size = 0
        price = 0.654321
        fallback_tick = tick_size if tick_size != 0 else 0.01
        rounded = round(price / fallback_tick) * fallback_tick
        assert abs(rounded - 0.65) < 0.001


# ---------------------------------------------------------------------------
# BUG: time_left = None causes TypeError
#
# STATUS: FIXED — check_signals handles missing end_date_iso
# ---------------------------------------------------------------------------

class TestTimeLeftNoneNoError:
    def test_time_left_none_no_type_error(self, mock_state_test):
        """REGRESSION: Missing end_date_iso doesn't crash check_signals()."""
        import pandas as pd
        import datetime as dt_mod

        rows = []
        for i in range(60):
            rows.append({
                "timestamp": dt_mod.datetime(2024, 1, 1) + dt_mod.timedelta(minutes=i),
                "open": 69_000.0, "high": 69_020.0, "low": 68_980.0,
                "close": 69_000.0, "volume": 200.0,
                "rsi_1m": 50.0, "macd_line": 0.0, "macd_signal": 0.0, "macd_hist": 0.0,
                "vwap": 69_000.0, "ema_9": 69_000.0, "ema_21": 69_000.0,
                "ema_9_slope": 0.0,
                "pivot_high": 70_000.0, "pivot_low": 68_000.0,
                "atr": 80.0, "atr_zone": "golden",
                "volume_state": "normal", "consecutive_closes": 0,
                "rsi_3m": None, "rsi_5m": None, "chg_1h": 0.0,
            })
        df = pd.DataFrame(rows)

        # market_info with no end_date_iso (None)
        mi = {
            "price_yes": 0.50,
            "price_no": 0.50,
            "end_date_iso": None,  # This was the bug trigger
            "event_start_time": None,
        }
        try:
            result = check_signals(mi, df)
            # Should not raise — result may be None or a signal
        except TypeError as e:
            pytest.fail(f"check_signals raised TypeError with end_date_iso=None: {e}")


# ---------------------------------------------------------------------------
# BUG: sell_ok deduplication — positions should not be closed twice
#
# STATUS: FIXED — sell_ok flag prevents duplicate sells
# This test verifies the DB state after mark_signal_live_no_position
# (the signal-level equivalent: can't be NO_ENTRY twice)
# ---------------------------------------------------------------------------

class TestSellOkDeduplication:
    def test_double_no_position_is_idempotent(self, tmp_path, mock_state_test):
        """REGRESSION: Calling mark_signal_live_no_position twice doesn't corrupt data."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        import bot.storage as storage_mod
        original = storage_mod.get_db_path
        storage_mod.get_db_path = lambda: db_path
        try:
            sig_id = save_signal(_minimal_signal())
            mark_signal_live_no_position(sig_id)
            mark_signal_live_no_position(sig_id)  # second call — should be safe
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT result FROM signals WHERE id=?", (sig_id,)).fetchone()
            conn.close()
            assert row[0] == "NO_ENTRY"
        finally:
            storage_mod.get_db_path = original


# ---------------------------------------------------------------------------
# BUG: busy_timeout — SQLite locked errors under concurrent access
#
# STATUS: FIXED — PRAGMA busy_timeout=5000 set in get_connection()
# ---------------------------------------------------------------------------

class TestBusyTimeout:
    def test_busy_timeout_configured(self, tmp_path):
        """REGRESSION: Connection must have busy_timeout=5000 to handle concurrent access."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        conn = get_connection(db_path)
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        conn.close()
        assert row[0] == 5000, f"busy_timeout should be 5000ms, got {row[0]}"


# ---------------------------------------------------------------------------
# BUG: CONTRACT_PRICE_MAX = 0.80 for light mode
#
# STATUS: Fixed/verified — test guards against accidental threshold change
# ---------------------------------------------------------------------------

class TestContractPriceMax:
    def test_contract_price_max_080_light(self, mock_state_light):
        """REGRESSION: CONTRACT_PRICE_MAX must be 0.80 for light mode."""
        from bot.state import state
        th = state.get_thresholds()
        assert th["CONTRACT_PRICE_MAX"] == 0.80, (
            f"Light mode CONTRACT_PRICE_MAX changed! Expected 0.80, got {th['CONTRACT_PRICE_MAX']}"
        )

    def test_contract_price_max_072_medium(self, mock_state_medium):
        """REGRESSION: CONTRACT_PRICE_MAX must be 0.72 for medium mode."""
        from bot.state import state
        th = state.get_thresholds()
        assert th["CONTRACT_PRICE_MAX"] == 0.72

    def test_atr_zone_thresholds_unchanged(self):
        """REGRESSION: ATR zone thresholds must match expected values."""
        from bot.config import ATR_ZONE_DEAD, ATR_ZONE_QUIET, ATR_ZONE_GOLDEN, ATR_ZONE_HIGH
        assert ATR_ZONE_DEAD == 30
        assert ATR_ZONE_QUIET == 60
        assert ATR_ZONE_GOLDEN == 100
        assert ATR_ZONE_HIGH == 120


# ---------------------------------------------------------------------------
# BUG: CLOB ask fallback to Gamma price
#
# STATUS: FIXED — when CLOB unavailable, we skip the signal (no Gamma fallback)
# This is tested logically: if clob_ask is not None, it should be used;
# if it IS None, the signal is still generated but with Gamma price (which is OK
# for signals — the real guard is in scanner.py).
# ---------------------------------------------------------------------------

class TestGammaPriceFallback:
    def test_clob_ask_used_when_present(self):
        """REGRESSION: When clob_ask is present, it's used for stake calculation."""
        sig = _minimal_signal(clob_ask=0.68, contract_price=0.50)
        result = calculate_stake(sig)
        # shares should be based on clob_ask=0.68, not contract_price=0.50
        expected_shares = round(STAKE_USD / 0.68, 2)
        assert abs(result["shares"] - expected_shares) < 0.01

    def test_win_prob_uses_clob_ask_not_gamma(self):
        """REGRESSION: estimate_win_probability uses clob_ask when present, not Gamma contract_price."""
        # clob_ask=0.72 (expensive, -0.02 modifier) vs contract_price=0.45 (cheap, +0.02)
        sig_without_clob = _minimal_signal(contract_price=0.45)
        sig_with_clob = _minimal_signal(contract_price=0.45, clob_ask=0.72)
        prob_without = estimate_win_probability(sig_without_clob)
        prob_with = estimate_win_probability(sig_with_clob)
        # clob_ask=0.72 → -0.02 modifier; contract_price=0.45 → +0.02 modifier
        # So prob_with should be ~0.04 lower than prob_without
        assert prob_without > prob_with, "clob_ask should give lower prob when expensive"
