import json
import logging
import sqlite3

from bot.config import AUTO_APPROVE_PAPER, DB_PATH_TEST, DB_PATH_LIVE, STAKE_USD, LIVE_TRADING

logger = logging.getLogger(__name__)


def get_db_path() -> str:
    from bot.state import state
    if state.mode == "test":
        return DB_PATH_TEST
    return DB_PATH_LIVE


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    return sqlite3.connect(db_path if db_path is not None else get_db_path())


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
        (
            "live_entry_status",
            "TEXT",
        ),  # NULL=paper/невизначено; opened=CLOB fill; no_position=approve але позиції нема
    ]
    for col, decl in additions:
        if col not in existing:
            cursor.execute(f"ALTER TABLE signals ADD COLUMN {col} {decl}")


def init_db(db_path: str | None = None):
    path = db_path if db_path is not None else get_db_path()
    try:
        conn = sqlite3.connect(path)
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
        conn.close()


def save_signal(signal: dict) -> int | None:
    """Зберігає сигнал: повний JSON + той самий HTML, що й у Telegram; paper — auto approve."""
    from bot.alert_text import format_signal_alert_html
    from bot.risk import calculate_stake
    from bot.state import state

    mode = state.mode
    is_live_mode = LIVE_TRADING and state.mode != "test"
    # В live-режимі сигнал очікує на ручне підтвердження або обробку send_alert
    decision = "approve" if (AUTO_APPROVE_PAPER and not is_live_mode) else "pending"
    if is_live_mode:
        risk = calculate_stake(signal)
        stake = float(risk["stake_usd"]) if risk.get("edge", 0) > 0 else float(STAKE_USD)
    else:
        stake = float(STAKE_USD)
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
        conn.close()


def update_signal_live_fill(signal_id: int, stake_usd: float, contract_price: float):
    """Після реального fill: оновити stake і ціну контракту для коректного settlement / Telegram."""
    try:
        conn = get_connection()
        conn.execute(
            """
            UPDATE signals SET stake_usd = ?, contract_price = ?,
                live_entry_status = 'opened'
            WHERE id = ?
            """,
            (stake_usd, contract_price, signal_id),
        )
        conn.commit()
        logger.info(
            "Сигнал #%s: live fill stake=%.2f contract_price=%.4f",
            signal_id, stake_usd, contract_price,
        )
    except Exception as e:
        logger.error("Помилка update_signal_live_fill: %s", e)
    finally:
        conn.close()


def mark_signal_live_no_position(signal_id: int):
    """Після Approve live: ордер не виконано / ліміт у стакані — не рахувати paper LOSS у settlement."""
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE signals SET live_entry_status = 'no_position' WHERE id = ?",
            (signal_id,),
        )
        conn.commit()
        logger.info("Сигнал #%s: live_entry_status=no_position", signal_id)
    except Exception as e:
        logger.error("Помилка mark_signal_live_no_position: %s", e)
    finally:
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
        conn.close()


def signal_live_position_already_closed(signal_id: int) -> bool:
    """True, якщо для цього сигналу є закрита позиція в positions (монітор уже зафіксував вихід)."""
    if not signal_id:
        return False
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT 1 FROM positions WHERE signal_id = ? AND status = 'closed' LIMIT 1",
            (signal_id,),
        ).fetchone()
        return row is not None
    except Exception as e:
        logger.error("Помилка signal_live_position_already_closed: %s", e)
        return False
    finally:
        conn.close()


def get_recent_signals(limit: int = 10) -> list:
    """Повертає останні N сигналів з decision=approve, з market_title з payload_json."""
    try:
        conn = get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, timestamp, direction, contract_price, result, pnl,
                   stake_usd, time_left, payload_json
            FROM signals
            WHERE decision = 'approve'
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cursor.fetchall()
        out = []
        for row in rows:
            d = dict(row)
            try:
                payload = json.loads(d.get("payload_json") or "{}")
            except Exception:
                payload = {}
            d["market_title"] = payload.get("market_title") or payload.get("market_slug") or d.get("market_id", "")
            d["gap"] = payload.get("gap")
            d["confluence"] = payload.get("confluence")
            d["taker_ratio"] = payload.get("taker_ratio")
            out.append(d)
        return out
    except Exception as e:
        logger.error("Помилка get_recent_signals: %s", e)
        return []
    finally:
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
        conn.close()


def init_pending_orders_table(db_path: str | None = None):
    path = db_path if db_path is not None else get_db_path()
    try:
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL,
                order_id TEXT NOT NULL UNIQUE,
                token_id TEXT NOT NULL,
                market_id TEXT,
                market_slug TEXT,
                direction TEXT,
                limit_price REAL,
                shares REAL,
                stake_usd REAL,
                neg_risk INTEGER DEFAULT 0,
                expires_at TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()
    except Exception as e:
        logger.error("Помилка init_pending_orders_table: %s", e)
    finally:
        conn.close()


def save_pending_order(
    signal_id: int,
    order_id: str,
    token_id: str,
    market_id: str,
    market_slug: str,
    direction: str,
    limit_price: float,
    shares: float,
    stake_usd: float,
    neg_risk: bool,
    expires_at: str,
):
    try:
        conn = get_connection()
        conn.execute(
            """
            INSERT OR IGNORE INTO pending_orders
            (signal_id, order_id, token_id, market_id, market_slug,
             direction, limit_price, shares, stake_usd, neg_risk, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (signal_id, order_id, token_id, market_id, market_slug,
             direction, limit_price, shares, stake_usd, int(neg_risk), expires_at),
        )
        conn.commit()
        logger.info("Збережено pending order: signal #%s order_id=%s", signal_id, order_id[:12])
    except Exception as e:
        logger.error("Помилка save_pending_order: %s", e)
    finally:
        conn.close()


def get_pending_orders() -> list:
    try:
        conn = get_connection()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM pending_orders").fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("Помилка get_pending_orders: %s", e)
        return []
    finally:
        conn.close()


def delete_pending_order(order_id: str):
    try:
        conn = get_connection()
        conn.execute("DELETE FROM pending_orders WHERE order_id = ?", (order_id,))
        conn.commit()
    except Exception as e:
        logger.error("Помилка delete_pending_order: %s", e)
    finally:
        conn.close()


def mark_signal_live_pending(signal_id: int):
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE signals SET live_entry_status = 'pending_fill' WHERE id = ?",
            (signal_id,),
        )
        conn.commit()
        logger.info("Сигнал #%s: live_entry_status=pending_fill", signal_id)
    except Exception as e:
        logger.error("Помилка mark_signal_live_pending: %s", e)
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print("БД успішно ініціалізована.")
