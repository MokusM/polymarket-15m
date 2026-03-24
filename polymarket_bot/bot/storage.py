import json
import logging
import sqlite3

from bot.config import AUTO_APPROVE_PAPER, DB_PATH, STAKE_USD

logger = logging.getLogger(__name__)


def get_connection():
    return sqlite3.connect(DB_PATH)


def _migrate_signals_columns(cursor: sqlite3.Cursor) -> None:
    cursor.execute("PRAGMA table_info(signals)")
    existing = {row[1] for row in cursor.fetchall()}
    additions = [
        ("stake_usd", "REAL DEFAULT 10"),
        ("bot_mode", "TEXT"),
        ("alert_html", "TEXT"),
        ("payload_json", "TEXT"),
        ("time_left", "REAL"),
        ("telegram_message_id", "INTEGER"),
    ]
    for col, decl in additions:
        if col not in existing:
            cursor.execute(f"ALTER TABLE signals ADD COLUMN {col} {decl}")


def init_db():
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                market_id TEXT,
                start_price REAL,
                current_price REAL,
                delta REAL,
                delta_percent REAL,
                direction TEXT,
                contract_price REAL,
                rsi_1m REAL,
                rsi_3m REAL,
                ema_position TEXT,
                volume_state TEXT,
                decision TEXT DEFAULT 'pending',
                result TEXT,
                pnl REAL,
                stake_usd REAL DEFAULT 10,
                bot_mode TEXT,
                alert_html TEXT,
                payload_json TEXT,
                time_left REAL
            )
            """
        )
        _migrate_signals_columns(cursor)
        conn.commit()
    except Exception as e:
        logger.error("Помилка ініціалізації БД: %s", e)
    finally:
        if "conn" in locals() and conn:
            conn.close()


def save_signal(signal: dict) -> int | None:
    """Зберігає сигнал: повний JSON + той самий HTML, що й у Telegram; paper — auto approve."""
    from bot.alert_text import format_signal_alert_html
    from bot.state import state

    mode = state.mode
    decision = "approve" if AUTO_APPROVE_PAPER else "pending"
    stake = STAKE_USD
    time_left = signal.get("time_left")

    payload = {**signal, "bot_mode_saved": mode, "stake_usd": stake}
    try:
        payload_json = json.dumps(payload, default=str, ensure_ascii=False)
    except TypeError:
        payload_json = "{}"

    alert_html = format_signal_alert_html(signal, mode, stake)

    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO signals (
                market_id, start_price, current_price, delta, delta_percent,
                direction, contract_price, rsi_1m, rsi_3m, ema_position, volume_state,
                decision, stake_usd, bot_mode, time_left, payload_json, alert_html
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.get("market_id"),
                signal.get("start_price"),
                signal.get("current_price"),
                signal.get("delta"),
                signal.get("delta_percent"),
                signal.get("direction"),
                signal.get("contract_price"),
                signal.get("rsi_1m"),
                signal.get("rsi_3m"),
                signal.get("ema_position"),
                signal.get("volume_state"),
                decision,
                stake,
                mode,
                time_left,
                payload_json,
                alert_html,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        logger.error("Помилка збереження сигналу: %s", e)
        return None
    finally:
        if "conn" in locals() and conn:
            conn.close()


def update_decision(signal_id: int, decision: str):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE signals SET decision = ? WHERE id = ?", (decision, signal_id)
        )
        conn.commit()
        logger.info("Сигнал #%s отримав статус: %s", signal_id, decision)
    except Exception as e:
        logger.error("Помилка оновлення рішення: %s", e)
    finally:
        if "conn" in locals() and conn:
            conn.close()


def update_result(signal_id: int, result: str, pnl: float):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE signals SET result = ?, pnl = ? WHERE id = ?",
            (result, pnl, signal_id),
        )
        conn.commit()
    except Exception as e:
        logger.error("Помилка оновлення результату: %s", e)
    finally:
        if "conn" in locals() and conn:
            conn.close()


def update_telegram_message_id(signal_id: int, message_id: int):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE signals SET telegram_message_id = ? WHERE id = ?",
            (message_id, signal_id),
        )
        conn.commit()
    except Exception as e:
        logger.error("Помилка збереження message_id: %s", e)
    finally:
        if "conn" in locals() and conn:
            conn.close()


def get_unresolved_signals() -> list:
    try:
        conn = get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM signals WHERE decision = 'approve' AND result IS NULL"
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error("Помилка отримання незакритих сигналів: %s", e)
        return []
    finally:
        if "conn" in locals() and conn:
            conn.close()


if __name__ == "__main__":
    init_db()
    print("БД успішно ініціалізована.")
