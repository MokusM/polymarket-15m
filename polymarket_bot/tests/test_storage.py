"""
test_storage.py — tests for bot/storage.py

Uses isolated temp SQLite databases (not live.db or test.db).
"""

import json
import sqlite3
import pytest

from bot.storage import (
    init_db,
    get_connection,
    save_signal,
    save_signal_snapshot,
    update_decision,
    update_result,
    mark_signal_live_no_position,
    get_unresolved_signals,
    get_recent_signals,
    _migrate_signals_columns,
)
from bot.config import STAKE_USD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_signal(direction="DOWN", confluence=3, contract_price=0.65) -> dict:
    """Minimal signal dict for save_signal()."""
    return {
        "market_id": "test-market-001",
        "direction": direction,
        "confluence": confluence,
        "contract_price": contract_price,
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
        "votes": {
            "RSI": {"direction": "DOWN", "label": "RSI 68.0 overbought"},
        },
    }


def _get_all_column_names(db_path: str, table: str) -> set:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in cursor.fetchall()}
    conn.close()
    return cols


# ---------------------------------------------------------------------------
# init_db / schema tests
# ---------------------------------------------------------------------------

class TestInitDB:
    def test_creates_signals_table(self, tmp_path):
        """init_db creates the signals table."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        cols = _get_all_column_names(db_path, "signals")
        assert "id" in cols
        assert "direction" in cols
        assert "contract_price" in cols

    def test_creates_signal_snapshots_table(self, tmp_path):
        """init_db creates signal_snapshots table."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        cols = _get_all_column_names(db_path, "signal_snapshots")
        assert "signal_id" in cols
        assert "btc_price" in cols
        assert "contract_price" in cols

    def test_migration_columns_present(self, tmp_path):
        """After init_db, migration columns are present."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        cols = _get_all_column_names(db_path, "signals")
        migration_cols = {
            "stake_usd", "bot_mode", "alert_html", "payload_json",
            "time_left", "telegram_message_id", "live_entry_status", "filter_version",
        }
        for col in migration_cols:
            assert col in cols, f"Migration column missing: {col}"

    def test_init_db_idempotent(self, tmp_path):
        """Calling init_db twice does not raise errors."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        init_db(db_path)  # second call — should not fail
        cols = _get_all_column_names(db_path, "signals")
        assert "id" in cols

    def test_schema_core_columns(self, tmp_path):
        """Core signals schema has expected columns."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        cols = _get_all_column_names(db_path, "signals")
        expected_core = {
            "id", "timestamp", "market_id", "start_price", "current_price",
            "delta", "delta_percent", "direction", "contract_price",
            "rsi_1m", "rsi_3m", "ema_position", "volume_state",
            "decision", "result", "pnl",
        }
        for col in expected_core:
            assert col in cols, f"Core column missing: {col}"

    def test_migration_is_additive(self, tmp_path):
        """Migration only adds columns that don't exist — doesn't break existing schema."""
        db_path = str(tmp_path / "test.db")
        # Create table manually without migration columns
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT
            )
        """)
        conn.commit()
        conn.close()
        # Now run full init_db — should add missing columns
        init_db(db_path)
        cols = _get_all_column_names(db_path, "signals")
        assert "filter_version" in cols
        assert "payload_json" in cols


# ---------------------------------------------------------------------------
# save_signal tests
# ---------------------------------------------------------------------------

class TestSaveSignal:
    def test_save_signal_returns_id(self, tmp_path, mock_state_test):
        """save_signal returns a positive integer ID."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        # Patch get_db_path to use our temp db
        import bot.storage as storage_mod
        original = storage_mod.get_db_path
        storage_mod.get_db_path = lambda: db_path
        try:
            signal_id = save_signal(_minimal_signal())
            assert signal_id is not None
            assert isinstance(signal_id, int)
            assert signal_id > 0
        finally:
            storage_mod.get_db_path = original

    def test_save_signal_autoincrement(self, tmp_path, mock_state_test):
        """Second signal gets id = first_id + 1."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        import bot.storage as storage_mod
        original = storage_mod.get_db_path
        storage_mod.get_db_path = lambda: db_path
        try:
            id1 = save_signal(_minimal_signal())
            id2 = save_signal(_minimal_signal(direction="UP"))
            assert id2 == id1 + 1
        finally:
            storage_mod.get_db_path = original

    def test_save_signal_stores_direction(self, tmp_path, mock_state_test):
        """Saved signal direction matches the input."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        import bot.storage as storage_mod
        original = storage_mod.get_db_path
        storage_mod.get_db_path = lambda: db_path
        try:
            sig_id = save_signal(_minimal_signal(direction="UP"))
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT direction FROM signals WHERE id=?", (sig_id,)).fetchone()
            conn.close()
            assert row[0] == "UP"
        finally:
            storage_mod.get_db_path = original

    def test_save_signal_stores_filter_version(self, tmp_path, mock_state_test):
        """filter_version is stored correctly."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        import bot.storage as storage_mod
        original = storage_mod.get_db_path
        storage_mod.get_db_path = lambda: db_path
        try:
            sig = _minimal_signal()
            sig["filter_version"] = "both"
            sig_id = save_signal(sig)
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT filter_version FROM signals WHERE id=?", (sig_id,)).fetchone()
            conn.close()
            assert row[0] == "both"
        finally:
            storage_mod.get_db_path = original

    def test_paper_auto_approve(self, tmp_path, mock_state_test, monkeypatch):
        """AUTO_APPROVE_PAPER=True (default), non-live mode → decision='approve'.
        Also tests that monkeypatching storage module's AUTO_APPROVE_PAPER works.
        """
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        import bot.storage as storage_mod
        original_path = storage_mod.get_db_path
        # Patch AUTO_APPROVE_PAPER directly in storage module (it imported it at module level)
        monkeypatch.setattr(storage_mod, "AUTO_APPROVE_PAPER", True)
        monkeypatch.setattr(storage_mod, "LIVE_TRADING", False)
        storage_mod.get_db_path = lambda: db_path
        try:
            sig_id = save_signal(_minimal_signal())
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT decision FROM signals WHERE id=?", (sig_id,)).fetchone()
            conn.close()
            assert row[0] == "approve"
        finally:
            storage_mod.get_db_path = original_path

    def test_save_signal_payload_json_valid(self, tmp_path, mock_state_test):
        """Saved payload_json parses back to a dict without error."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        import bot.storage as storage_mod
        original = storage_mod.get_db_path
        storage_mod.get_db_path = lambda: db_path
        try:
            sig_id = save_signal(_minimal_signal())
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT payload_json FROM signals WHERE id=?", (sig_id,)).fetchone()
            conn.close()
            payload = json.loads(row[0])
            assert isinstance(payload, dict)
        finally:
            storage_mod.get_db_path = original


# ---------------------------------------------------------------------------
# save_signal_snapshot tests
# ---------------------------------------------------------------------------

class TestSaveSignalSnapshot:
    def test_save_snapshot_and_read(self, tmp_path, mock_state_test):
        """save_signal_snapshot stores a row readable from DB."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        import bot.storage as storage_mod
        original = storage_mod.get_db_path
        storage_mod.get_db_path = lambda: db_path
        try:
            # First save a signal to get a valid signal_id
            sig_id = save_signal(_minimal_signal())
            save_signal_snapshot(sig_id, 5, 69_100.0, 0.65)
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT signal_id, minutes_after, btc_price, contract_price FROM signal_snapshots WHERE signal_id=?",
                (sig_id,)
            ).fetchone()
            conn.close()
            assert row is not None
            assert row[0] == sig_id
            assert row[1] == 5
            assert abs(row[2] - 69_100.0) < 0.01
            assert abs(row[3] - 0.65) < 0.001
        finally:
            storage_mod.get_db_path = original

    def test_save_snapshot_with_none_values(self, tmp_path, mock_state_test):
        """save_signal_snapshot doesn't crash with None btc_price or contract_price."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        import bot.storage as storage_mod
        original = storage_mod.get_db_path
        storage_mod.get_db_path = lambda: db_path
        try:
            sig_id = save_signal(_minimal_signal())
            # Should not raise
            save_signal_snapshot(sig_id, 10, None, None)
        finally:
            storage_mod.get_db_path = original


# ---------------------------------------------------------------------------
# update_decision / update_result tests
# ---------------------------------------------------------------------------

class TestUpdateOperations:
    def _setup(self, tmp_path, mock_state_test):
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        import bot.storage as storage_mod
        original = storage_mod.get_db_path
        storage_mod.get_db_path = lambda: db_path
        sig_id = save_signal(_minimal_signal())
        return db_path, sig_id, storage_mod, original

    def test_update_decision_approve(self, tmp_path, mock_state_test):
        """update_decision('approve') sets decision=approve."""
        db_path, sig_id, storage_mod, original = self._setup(tmp_path, mock_state_test)
        try:
            update_decision(sig_id, "approve")
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT decision FROM signals WHERE id=?", (sig_id,)).fetchone()
            conn.close()
            assert row[0] == "approve"
        finally:
            storage_mod.get_db_path = original

    def test_update_decision_reject(self, tmp_path, mock_state_test):
        """update_decision('reject') sets decision=reject."""
        db_path, sig_id, storage_mod, original = self._setup(tmp_path, mock_state_test)
        try:
            update_decision(sig_id, "reject")
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT decision FROM signals WHERE id=?", (sig_id,)).fetchone()
            conn.close()
            assert row[0] == "reject"
        finally:
            storage_mod.get_db_path = original

    def test_update_result_win(self, tmp_path, mock_state_test):
        """update_result('WIN', 5.0) stores WIN + pnl."""
        db_path, sig_id, storage_mod, original = self._setup(tmp_path, mock_state_test)
        try:
            update_result(sig_id, "WIN", 5.0)
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT result, pnl FROM signals WHERE id=?", (sig_id,)).fetchone()
            conn.close()
            assert row[0] == "WIN"
            assert abs(row[1] - 5.0) < 0.01
        finally:
            storage_mod.get_db_path = original

    def test_update_result_loss(self, tmp_path, mock_state_test):
        """update_result('LOSS', -10.0) stores LOSS + negative pnl."""
        db_path, sig_id, storage_mod, original = self._setup(tmp_path, mock_state_test)
        try:
            update_result(sig_id, "LOSS", -10.0)
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT result, pnl FROM signals WHERE id=?", (sig_id,)).fetchone()
            conn.close()
            assert row[0] == "LOSS"
            assert row[1] < 0
        finally:
            storage_mod.get_db_path = original


# ---------------------------------------------------------------------------
# mark_signal_live_no_position tests
# ---------------------------------------------------------------------------

class TestMarkNoPosition:
    def test_mark_no_position_sets_result(self, tmp_path, mock_state_test):
        """mark_signal_live_no_position sets result=NO_ENTRY, pnl=0."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        import bot.storage as storage_mod
        original = storage_mod.get_db_path
        storage_mod.get_db_path = lambda: db_path
        try:
            sig_id = save_signal(_minimal_signal())
            mark_signal_live_no_position(sig_id)
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT result, pnl, live_entry_status FROM signals WHERE id=?", (sig_id,)).fetchone()
            conn.close()
            assert row[0] == "NO_ENTRY"
            assert row[1] == 0.0
            assert row[2] == "no_position"
        finally:
            storage_mod.get_db_path = original

    def test_mark_no_position_excluded_from_unresolved(self, tmp_path, mock_state_test):
        """NO_ENTRY signals have result set → not returned by get_unresolved_signals."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        import bot.storage as storage_mod
        original = storage_mod.get_db_path
        storage_mod.get_db_path = lambda: db_path
        try:
            sig_id = save_signal(_minimal_signal())
            # Approve it first
            update_decision(sig_id, "approve")
            mark_signal_live_no_position(sig_id)
            unresolved = get_unresolved_signals()
            ids = [s["id"] for s in unresolved]
            assert sig_id not in ids
        finally:
            storage_mod.get_db_path = original


# ---------------------------------------------------------------------------
# DB connection — busy_timeout
# ---------------------------------------------------------------------------

class TestDBConnection:
    def test_busy_timeout_set(self, tmp_path):
        """Connection has PRAGMA busy_timeout=5000 set."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        conn = get_connection(db_path)
        # Query the timeout pragma
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        conn.close()
        assert row[0] == 5000
