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
    conn = sqlite3.connect(db_path if db_path is not None else get_db_path())
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


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
        ("live_entry_status", "TEXT"),  # NULL=paper; opened=CLOB fill; no_position=approve але позиції нема
        ("filter_version", "TEXT"),     # current / new / both — A/B тест фільтрів
        ("strategy_id", "TEXT"),        # confluence / data_collector / delta_pct
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
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS signal_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                minutes_after INTEGER,
                btc_price REAL,
                contract_price REAL,
                recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    except Exception as e:
        logger.error("Помилка ініціалізації БД: %s", e)
    finally:
        conn.close()
    init_shadow_signals_table(path)
    init_signal_snapshots_table(path)
    init_alt_signals_table(path)


def save_signal_snapshot(signal_id: int, minutes_after: int, btc_price: float | None, contract_price: float | None):
    try:
        conn = get_connection()
        conn.execute(
            "INSERT INTO signal_snapshots (signal_id, minutes_after, btc_price, contract_price) VALUES (?, ?, ?, ?)",
            (signal_id, minutes_after, btc_price, contract_price),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug("save_signal_snapshot error: %s", e)


def save_signal(signal: dict, db_path: str | None = None) -> int | None:
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
        conn = get_connection(db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO signals (
                market_id, start_price, current_price, delta, delta_percent,
                direction, contract_price, rsi_1m, rsi_3m, ema_position, volume_state,
                decision, stake_usd, bot_mode, time_left, payload_json, alert_html,
                filter_version, strategy_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                signal.get("_stake_usd") or stake,
                mode,
                time_left,
                payload_json,
                alert_html,
                signal.get("filter_version", "current"),
                signal.get("_strategy_id"),
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
    """Після Approve live: ордер не виконано / ліміт у стакані — одразу NO_ENTRY (settlement не дублює 💤)."""
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE signals SET live_entry_status = 'no_position', result = 'NO_ENTRY', pnl = 0.0 WHERE id = ?",
            (signal_id,),
        )
        conn.commit()
        logger.info("Сигнал #%s: no_position → NO_ENTRY (immediate)", signal_id)
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
                end_date_iso TEXT,
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
                bot_mode TEXT,
                resolved_direction TEXT
            )
        """)
        # Міграція існуючих таблиць
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(shadow_signals)")
        existing = {row[1] for row in cursor.fetchall()}
        for col, decl in [("end_date_iso", "TEXT"), ("resolved_direction", "TEXT")]:
            if col not in existing:
                conn.execute(f"ALTER TABLE shadow_signals ADD COLUMN {col} {decl}")
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
    end_date_iso: str | None = None,
    db_path: str | None = None,
) -> int | None:
    from bot.state import state
    path = db_path if db_path is not None else get_db_path()
    try:
        conn = sqlite3.connect(path)
        cursor = conn.execute("""
            INSERT INTO shadow_signals
            (market_id, end_date_iso, direction, contract_price, clob_ask, confluence,
             reject_reason, time_left, gap, atr, adx, btc_price, bot_mode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (market_id, end_date_iso, direction, contract_price, clob_ask, confluence,
              reject_reason, time_left, gap, atr, adx, btc_price, state.mode))
        conn.commit()
        return cursor.lastrowid
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


def resolve_shadow_signals(btc_df, db_path: str | None = None) -> int:
    """
    Для shadow_signals де ринок вже закрився — записує куди реально пішов BTC.
    btc_df: DataFrame з 1m свічками BTC (pandas).
    resolved_direction: "UP"/"DOWN" — фактичний напрямок BTC за вікно.
    """
    from datetime import datetime, timezone
    import pandas as pd
    path = db_path if db_path is not None else get_db_path()
    updated = 0
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        now_iso = datetime.now(timezone.utc).isoformat()

        rows = conn.execute("""
            SELECT id, end_date_iso FROM shadow_signals
            WHERE resolved_direction IS NULL AND end_date_iso IS NOT NULL AND end_date_iso < ?
        """, (now_iso,)).fetchall()

        if not rows or btc_df is None or btc_df.empty:
            conn.close()
            return 0

        # Поточна ціна BTC
        btc_now = float(btc_df.iloc[-1]["close"])
        btc_15m_ago = float(btc_df.iloc[-15]["close"]) if len(btc_df) >= 15 else btc_now
        actual_direction = "UP" if btc_now > btc_15m_ago else "DOWN"

        for row in rows:
            conn.execute(
                "UPDATE shadow_signals SET resolved_direction=? WHERE id=?",
                (actual_direction, row["id"])
            )
            updated += 1

        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Помилка resolve_shadow_signals: %s", e)
    return updated


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


def init_pending_hedges_table(db_path: str | None = None):
    """Table for arb/hedge strategy: track leg1 waiting for leg2."""
    path = db_path if db_path is not None else get_db_path()
    try:
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_hedges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT (datetime('now')),
                market_id TEXT NOT NULL,
                market_slug TEXT,
                asset TEXT,
                window TEXT,
                leg1_side TEXT,
                leg1_shares REAL,
                leg1_price REAL,
                leg1_cost REAL,
                leg1_order_id TEXT,
                leg2_side TEXT,
                leg2_shares REAL,
                leg2_price REAL,
                leg2_cost REAL,
                leg2_order_id TEXT,
                hedged_at TEXT,
                status TEXT DEFAULT 'open',
                wallet_key TEXT,
                chat_id TEXT
            )
            """
        )
        conn.commit()
        # Migrate: add new columns if missing
        existing = {r[1] for r in conn.execute("PRAGMA table_info(pending_hedges)").fetchall()}
        migrations = [
            ("safety_order_id", "TEXT"),
            ("safety_token_id", "TEXT"),
            ("opp_ask_at_entry", "REAL"),
            ("spread_at_entry", "REAL"),
            ("sol_price_at_entry", "REAL"),
            ("atr_at_entry", "REAL"),
            ("time_left_at_entry", "REAL"),
        ]
        for col, typ in migrations:
            if col not in existing:
                try:
                    conn.execute(f"ALTER TABLE pending_hedges ADD COLUMN {col} {typ}")
                except Exception:
                    pass
        conn.commit()
    except Exception as e:
        logger.error("init_pending_hedges_table: %s", e)
    finally:
        conn.close()


def save_pending_hedge(
    market_id: str, market_slug: str, asset: str, window: str,
    leg1_side: str, leg1_shares: float, leg1_price: float, leg1_cost: float,
    leg1_order_id: str, wallet_key: str, chat_id: str,
    safety_order_id: str | None = None, safety_token_id: str | None = None,
    opp_ask_at_entry: float | None = None, spread_at_entry: float | None = None,
    sol_price_at_entry: float | None = None, atr_at_entry: float | None = None,
    time_left_at_entry: float | None = None,
) -> int | None:
    try:
        conn = get_connection()
        cur = conn.execute(
            "INSERT INTO pending_hedges "
            "(market_id, market_slug, asset, window, leg1_side, leg1_shares, "
            "leg1_price, leg1_cost, leg1_order_id, wallet_key, chat_id, "
            "safety_order_id, safety_token_id, opp_ask_at_entry, spread_at_entry, "
            "sol_price_at_entry, atr_at_entry, time_left_at_entry) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (market_id, market_slug, asset, window, leg1_side, leg1_shares,
             leg1_price, leg1_cost, leg1_order_id, wallet_key, chat_id,
             safety_order_id, safety_token_id, opp_ask_at_entry, spread_at_entry,
             sol_price_at_entry, atr_at_entry, time_left_at_entry),
        )
        conn.commit()
        return cur.lastrowid
    except Exception as e:
        logger.error("save_pending_hedge: %s", e)
        return None
    finally:
        conn.close()


def update_hedge_safety_order(hedge_id: int, safety_order_id: str, safety_token_id: str):
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE pending_hedges SET safety_order_id=?, safety_token_id=? WHERE id=?",
            (safety_order_id, safety_token_id, hedge_id),
        )
        conn.commit()
    except Exception as e:
        logger.error("update_hedge_safety_order: %s", e)
    finally:
        conn.close()


def get_latest_pending_hedge(chat_id: str, wallet_key: str | None = None) -> dict | None:
    try:
        conn = get_connection()
        conn.row_factory = sqlite3.Row
        where = "status='open' AND chat_id=?"
        params = [str(chat_id)]
        if wallet_key:
            where += " AND wallet_key=?"
            params.append(wallet_key)
        row = conn.execute(
            f"SELECT * FROM pending_hedges WHERE {where} "
            f"ORDER BY id DESC LIMIT 1",
            params,
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("get_latest_pending_hedge: %s", e)
        return None
    finally:
        conn.close()


def mark_hedge_filled(
    hedge_id: int, leg2_side: str, leg2_shares: float,
    leg2_price: float, leg2_cost: float, leg2_order_id: str,
) -> None:
    try:
        conn = get_connection()
        from datetime import datetime, timezone
        conn.execute(
            "UPDATE pending_hedges SET leg2_side=?, leg2_shares=?, leg2_price=?, "
            "leg2_cost=?, leg2_order_id=?, hedged_at=?, status='hedged' WHERE id=?",
            (leg2_side, leg2_shares, leg2_price, leg2_cost, leg2_order_id,
             datetime.now(timezone.utc).isoformat(), hedge_id),
        )
        conn.commit()
    except Exception as e:
        logger.error("mark_hedge_filled: %s", e)
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
                created_at TEXT DEFAULT (datetime('now')),
                strategy_id TEXT
            )
            """
        )
        # Migration: add strategy_id if missing
        cursor = conn.execute("PRAGMA table_info(pending_orders)")
        existing = {row[1] for row in cursor.fetchall()}
        if "strategy_id" not in existing:
            conn.execute("ALTER TABLE pending_orders ADD COLUMN strategy_id TEXT")
        conn.commit()
    except Exception as e:
        logger.error("Помилка init_pending_orders_table: %s", e)
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
    strategy_id: str | None = None,
):
    try:
        conn = get_connection()
        conn.execute(
            """
            INSERT OR IGNORE INTO pending_orders
            (signal_id, order_id, token_id, market_id, market_slug,
             direction, limit_price, shares, stake_usd, neg_risk, expires_at, strategy_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (signal_id, order_id, token_id, market_id, market_slug,
             direction, limit_price, shares, stake_usd, int(neg_risk), expires_at, strategy_id),
        )
        conn.commit()
        logger.info("Збережено pending order: signal #%s order_id=%s strategy=%s", signal_id, order_id[:12], strategy_id or "-")
    except Exception as e:
        logger.error("Помилка save_pending_order: %s", e)
    finally:
        conn.close()


def get_pending_order_by_signal(signal_id: int) -> dict | None:
    try:
        conn = get_connection()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM pending_orders WHERE signal_id = ? LIMIT 1", (signal_id,)
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("Помилка get_pending_order_by_signal: %s", e)
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
