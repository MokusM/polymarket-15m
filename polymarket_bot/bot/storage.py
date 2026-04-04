import json
import logging
import sqlite3

from bot.config import AUTO_APPROVE_PAPER, DB_PATH_TEST, DB_PATH_LIVE, STAKE_USD, MIN_STAKE_USD, LIVE_TRADING

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
    init_shadow_signals_table(path)
    init_signal_snapshots_table(path)
    init_alt_signals_table(path)


def save_signal(signal: dict) -> int | None:
    """Зберігає сигнал: повний JSON + той самий HTML, що й у Telegram; paper — auto approve."""
    from bot.alert_text import format_signal_alert_html
    from bot.risk import calculate_stake
    from bot.state import state

    mode = state.mode
    is_live_mode = LIVE_TRADING and state.mode != "test"
    # В live-режимі сигнал очікує на ручне підтвердження або обробку send_alert
    decision = "approve" if (AUTO_APPROVE_PAPER and not is_live_mode) else "pending"
    if state.mode != "test":
        risk = calculate_stake(signal)
        stake = float(risk["stake_usd"]) if risk.get("edge", 0) > 0 else float(MIN_STAKE_USD)
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


def init_shadow_signals_table(db_path: str | None = None):
    """Таблиця для сигналів що не пройшли фільтри — для аналізу гіпотез."""
    path = db_path if db_path is not None else get_db_path()
    try:
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shadow_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                market_id TEXT,
                direction TEXT,
                contract_price REAL,
                clob_ask REAL,
                confluence INTEGER,
                reject_reason TEXT,
                time_left REAL,
                gap REAL,
                atr REAL,
                adx REAL,
                btc_price REAL,
                bot_mode TEXT
            )
        """)
        conn.commit()
    except Exception as e:
        logger.error("Помилка init_shadow_signals_table: %s", e)
    finally:
        conn.close()


def save_shadow_signal(
    market_id: str,
    direction: str | None,
    contract_price: float | None,
    confluence: int,
    reject_reason: str,
    time_left: float | None = None,
    gap: float | None = None,
    atr: float | None = None,
    adx: float | None = None,
    btc_price: float | None = None,
    clob_ask: float | None = None,
    db_path: str | None = None,
):
    from bot.state import state
    path = db_path if db_path is not None else get_db_path()
    try:
        conn = sqlite3.connect(path)
        conn.execute("""
            INSERT INTO shadow_signals
            (market_id, direction, contract_price, clob_ask, confluence,
             reject_reason, time_left, gap, atr, adx, btc_price, bot_mode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (market_id, direction, contract_price, clob_ask, confluence,
              reject_reason, time_left, gap, atr, adx, btc_price, state.mode))
        conn.commit()
    except Exception as e:
        logger.error("Помилка save_shadow_signal: %s", e)
    finally:
        conn.close()


def init_signal_snapshots_table(db_path: str | None = None):
    """Таблиця трекінгу ціни після сигналу — для аналізу стоп-лосу і руху."""
    path = db_path if db_path is not None else get_db_path()
    try:
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signal_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL,
                minutes_after INTEGER NOT NULL,
                btc_price REAL,
                contract_price REAL,
                recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    except Exception as e:
        logger.error("Помилка init_signal_snapshots_table: %s", e)
    finally:
        conn.close()


def save_signal_snapshot(
    signal_id: int,
    minutes_after: int,
    btc_price: float | None,
    contract_price: float | None,
    db_path: str | None = None,
):
    path = db_path if db_path is not None else get_db_path()
    try:
        conn = sqlite3.connect(path)
        conn.execute("""
            INSERT INTO signal_snapshots (signal_id, minutes_after, btc_price, contract_price)
            VALUES (?, ?, ?, ?)
        """, (signal_id, minutes_after, btc_price, contract_price))
        conn.commit()
    except Exception as e:
        logger.error("Помилка save_signal_snapshot: %s", e)
    finally:
        conn.close()


def init_alt_signals_table(db_path: str | None = None):
    """Таблиця для сигналів ETH/SOL — ті ж поля що і signals + asset + BTC кореляція."""
    path = db_path if db_path is not None else get_db_path()
    try:
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alt_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                asset TEXT NOT NULL,
                market_id TEXT,
                end_date_iso TEXT,
                start_price REAL,
                current_price REAL,
                delta REAL,
                delta_percent REAL,
                direction TEXT,
                contract_price REAL,
                clob_ask REAL,
                confluence INTEGER,
                rsi_1m REAL,
                ema_position TEXT,
                volume_state TEXT,
                gap REAL,
                gap_pct REAL,
                atr REAL,
                atr_zone TEXT,
                adx REAL,
                time_left REAL,
                bot_mode TEXT,
                btc_price REAL,
                btc_gap REAL,
                btc_gap_pct REAL,
                btc_aligned INTEGER,
                consecutive_closes INTEGER,
                speed_accel REAL,
                vwap_cross INTEGER,
                result TEXT,
                resolved_price REAL,
                payload_json TEXT
            )
        """)
        # Міграція існуючих таблиць
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(alt_signals)")
        existing = {row[1] for row in cursor.fetchall()}
        for col, decl in [
            ("end_date_iso", "TEXT"),
            ("result", "TEXT"),
            ("resolved_price", "REAL"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE alt_signals ADD COLUMN {col} {decl}")
        conn.commit()
    except Exception as e:
        logger.error("Помилка init_alt_signals_table: %s", e)
    finally:
        conn.close()


def save_alt_signal(signal: dict, db_path: str | None = None) -> int | None:
    """Зберігає ALT (ETH/SOL) сигнал для статистики."""
    from bot.state import state
    path = db_path if db_path is not None else get_db_path()
    try:
        conn = sqlite3.connect(path)
        payload = json.dumps(signal, ensure_ascii=False, default=str)
        cursor = conn.execute("""
            INSERT INTO alt_signals (
                asset, market_id, end_date_iso, start_price, current_price, delta, delta_percent,
                direction, contract_price, clob_ask, confluence,
                rsi_1m, ema_position, volume_state,
                gap, gap_pct, atr, atr_zone, adx, time_left, bot_mode,
                btc_price, btc_gap, btc_gap_pct, btc_aligned,
                consecutive_closes, speed_accel, vwap_cross, payload_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            signal.get("asset"),
            signal.get("market_id"),
            signal.get("end_date_iso"),
            signal.get("start_price"),
            signal.get("current_price"),
            signal.get("delta"),
            signal.get("delta_percent"),
            signal.get("direction"),
            signal.get("contract_price"),
            signal.get("clob_ask"),
            signal.get("confluence"),
            signal.get("rsi_1m"),
            signal.get("ema_position"),
            signal.get("volume_state"),
            signal.get("gap"),
            signal.get("gap_pct"),
            signal.get("atr"),
            signal.get("atr_zone"),
            signal.get("adx"),
            signal.get("time_left"),
            state.mode,
            signal.get("btc_price"),
            signal.get("btc_gap"),
            signal.get("btc_gap_pct"),
            signal.get("btc_aligned"),
            signal.get("consecutive_closes"),
            signal.get("speed_accel"),
            signal.get("vwap_cross"),
            payload,
        ))
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        logger.error("Помилка save_alt_signal: %s", e)
        return None
    finally:
        conn.close()


def resolve_alt_signals(current_prices: dict[str, float], db_path: str | None = None) -> int:
    """
    Визначає result для alt_signals де ринок вже закрився (end_date_iso < now).
    current_prices: {"ETH": 2050.0, "SOL": 80.5} — поточна ціна активу.
    WIN = ціна пішла в напрямку сигналу, LOSS = проти.
    Повертає кількість оновлених записів.
    """
    from datetime import datetime, timezone
    path = db_path if db_path is not None else get_db_path()
    updated = 0
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        now_iso = datetime.now(timezone.utc).isoformat()

        # Всі нерозв'язані сигнали де ринок вже закрився
        rows = conn.execute("""
            SELECT id, asset, direction, start_price, end_date_iso
            FROM alt_signals
            WHERE result IS NULL AND end_date_iso IS NOT NULL AND end_date_iso < ?
        """, (now_iso,)).fetchall()

        for row in rows:
            asset = row["asset"]
            resolved_price = current_prices.get(asset)
            if resolved_price is None:
                continue

            start = row["start_price"]
            direction = row["direction"]
            if start and start > 0:
                actual = "UP" if resolved_price > start else "DOWN"
                result = "WIN" if actual == direction else "LOSS"
                conn.execute("""
                    UPDATE alt_signals SET result=?, resolved_price=? WHERE id=?
                """, (result, resolved_price, row["id"]))
                updated += 1

        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Помилка resolve_alt_signals: %s", e)
    return updated


if __name__ == "__main__":
    init_db()
    print("БД успішно ініціалізована.")
