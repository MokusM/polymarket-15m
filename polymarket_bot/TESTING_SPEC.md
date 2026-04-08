# TESTING_SPEC — Polymarket Bot

**Оновлено:** 2026-04-06
**Гілка:** feat/filter-ab-test
**Стек:** Python 3.14, asyncio, aiogram, httpx, pandas, sqlite3
**Кодова база:** `polymarket_bot/bot/` (~5000 рядків, 15 модулів)
**Тестів немає — створюємо з нуля.**

## Структура проекту

```
polymarket_bot/
├── bot/
│   ├── config.py          — конфіг з .env (82 рядки)
│   ├── state.py           — режими light/medium/strict, пороги (130)
│   ├── indicators.py      — RSI, MACD, VWAP, EMA, Pivots, ATR (151)
│   ├── signals.py         — check_signals(), diagnose_signals() (447)
│   ├── risk.py            — calculate_stake(), edge estimation (127)
│   ├── storage.py         — SQLite CRUD: signals, positions, snapshots (448)
│   ├── scanner.py         — головний цикл сканування, CLOB fetch (323)
│   ├── execution_client.py — CLOB ордери, FAK, precision (624)
│   ├── telegram_bot.py    — Telegram UI, send_alert, execute (1071)
│   ├── settlement.py      — закриття позицій, PnL розрахунок (288)
│   ├── position_manager.py — TP/SL рівні, часткові продажі (706)
│   ├── exchange_client.py — Binance candles API (90)
│   ├── polymarket_client.py — Gamma API, market discovery (180)
│   ├── alert_text.py      — HTML форматування алертів (230)
│   └── ws_settlement.py   — WebSocket для settlement (187)
├── .env                   — секрети, ставки, режим
├── live.db                — продакшн БД
└── test.db                — тестова БД
```

## Рівні тестування

### Рівень 1 — Unit тести (КРИТИЧНІ)

#### 1.1 `indicators.py` — Технічні індикатори

**Ціль:** перевірити що кожен індикатор рахується правильно на відомих даних.

Тести:
- `test_rsi_oversold` — RSI < 30 на 14+ свічках з постійним зниженням
- `test_rsi_overbought` — RSI > 70 на свічках з постійним ростом
- `test_rsi_neutral` — RSI ~50 на бічному ринку
- `test_macd_bullish_crossover` — MACD line перетинає signal зверху
- `test_macd_bearish_crossover` — MACD line перетинає signal знизу
- `test_vwap_calculation` — VWAP = cumsum(price*volume) / cumsum(volume)
- `test_ema_golden_cross` — EMA9 > EMA21
- `test_ema_death_cross` — EMA9 < EMA21
- `test_pivot_high_low` — pivot_high/pivot_low на відомому патерні
- `test_atr_zone_classification` — dead/quiet/golden/high/extreme зони
- `test_consecutive_closes` — підрахунок послідовних свічок в одному напрямку
- `test_add_indicators_columns` — всі потрібні колонки присутні після add_indicators()

**Дані:** створити фікстури з pd.DataFrame (30-100 свічок з відомими OHLCV).

#### 1.2 `signals.py` — Генерація сигналів

**Ціль:** перевірити що check_signals() повертає правильний сигнал або None при різних умовах.

Тести:
- `test_signal_generated_conf3_down` — 3/5 індикаторів DOWN → сигнал
- `test_signal_generated_conf4_up` — 4/5 UP → сигнал з confluence=4
- `test_no_signal_conf2` — тільки 2/5 → None
- `test_atr_dead_zone_blocks` — ATR zone=dead → None
- `test_atr_below_minimum_blocks` — ATR < ATR_MIN_USD → None
- `test_time_left_too_short_blocks` — time_left < TIME_LEFT_MIN → None
- `test_time_left_too_long_blocks` — time_left > TIME_LEFT_MAX → None
- `test_contract_price_too_low` — cp < CONTRACT_PRICE_MIN → None
- `test_contract_price_too_high` — cp > CONTRACT_PRICE_MAX → None
- `test_gap_filter_blocks_small_move` — GAP < GAP_MIN_USD → None
- `test_gap_filter_passes_large_move` — GAP >= GAP_MIN_USD → сигнал
- `test_strict_gap_near_expiry` — time<5min + GAP<100 → None
- `test_filter_version_both` — gap>=80 + cc>=2 + macd_norm>=0.20 + ATR<150 → "both"
- `test_filter_version_current_only` — лише current фільтр → "current"
- `test_rsi_contrariant_not_in_confluence` — RSI < 35 при DOWN → RSI голосує UP, не рахується в DOWN confluence

**Mock:** `state.get_thresholds()` повертає фіксовані пороги light режиму.
**Market_info fixture:** `{"price_yes": 0.30, "price_no": 0.70, "end_date_iso": ...}`

#### 1.3 `risk.py` — Розрахунок ставки

Тести:
- `test_fixed_stake_no_edge` — edge <= 0 → STAKE_USD ($10)
- `test_fixed_stake_positive_edge` — edge > 0 → теж STAKE_USD (Kelly вимкнений)
- `test_test_mode_fixed_stake` — mode=test → FIXED_STAKE
- `test_win_prob_conf3` — confluence=3 → ~60% base
- `test_win_prob_conf4` — confluence=4 → ~70% base
- `test_win_prob_modifiers` — ATR zone golden +5%, expensive cp -2%
- `test_format_risk_line` — правильний текст для Telegram

#### 1.4 `storage.py` — Робота з БД

Тести (використовувати :memory: або tmpfile):
- `test_init_db_creates_tables` — signals, positions, signal_snapshots таблиці
- `test_save_signal_returns_id` — збереження і отримання ID
- `test_save_signal_snapshot` — запис snapshot і читання
- `test_update_decision` — approve/reject
- `test_update_result` — WIN/LOSS/NO_ENTRY
- `test_count_open_positions` — правильний підрахунок
- `test_migration_adds_columns` — нові колонки додаються до існуючої таблиці

### Рівень 2 — Integration тести (ВАЖЛИВІ)

#### 2.1 Scanner flow (без мережі)

**Ціль:** перевірити повний цикл: candles → indicators → check_signals → save_signal.

Тести:
- `test_scanner_generates_signal_on_strong_move` — підготувати df з сильним рухом, mock market_info → сигнал збережено в БД
- `test_scanner_skips_dead_zone` — ATR dead → жодного сигналу
- `test_scanner_cooldown_works` — другий сигнал для того ж market+direction блокується cooldown
- `test_scanner_clob_retry_on_failure` — mock CLOB 2 fails + 1 success → сигнал проходить
- `test_scanner_skip_when_clob_unavailable` — mock CLOB 3 fails → skip (не fallback на Gamma)
- `test_snapshots_scheduled_after_signal` — після save_signal створюється task для snapshots

**Mock:** `httpx.AsyncClient` для CLOB/Gamma/Binance відповідей.

#### 2.2 Execution flow

**Ціль:** перевірити що ордери розраховуються правильно.

Тести:
- `test_min_trade_confluence_blocks_low_conf` — conf=2 → не торгуємо
- `test_min_trade_confluence_reduces_stake_conf3` — conf=3 → $1 ставка
- `test_conf4_gets_full_stake` — conf=4 → $10 ставка
- `test_tick_size_zero_guard` — tick=0 → використовує 0.01
- `test_min_order_size_guard` — min_order > 5x stake → помилка
- `test_price_precision_2dp` — ціна округлюється до 2 знаків

#### 2.3 Settlement

Тести:
- `test_settle_win` — market resolved в бік позиції → WIN, PnL > 0
- `test_settle_loss` — market resolved проти → LOSS, PnL < 0
- `test_settle_no_entry` — немає позиції → NO_ENTRY

### Рівень 3 — Regression тести (ЗАХИСТ ВІД ВІДОМИХ БАГІВ)

Тести на конкретні баги з історії:
- `test_no_gamma_fallback` — CLOB fail → не використовувати Gamma ціну
- `test_tick_zero_no_division_error` — tick=0 не викликає ZeroDivisionError
- `test_time_left_none_no_type_error` — time_left=None не викликає TypeError
- `test_rsi_contrariant_behavior` — RSI < 35 при DOWN сигналі → RSI голосує UP
- `test_conf0_excluded_from_stats` — conf=0 (ручні) не впливають на WR статистику
- `test_stake_always_fixed` — Kelly вимкнено, ставка завжди STAKE_USD
- `test_contract_price_max_080` — CONTRACT_PRICE_MAX=0.80 для light mode

### Рівень 4 — Data validation (АНАЛІТИКА)

Тести на даних з live.db:
- `test_all_conf4_wins_have_tp_exits` — кожен WIN conf=4 має хоча б 1 TP exit
- `test_no_entry_signals_have_no_positions` — NO_ENTRY сигнали не мають записів в positions
- `test_signal_payload_json_valid` — payload_json парситься без помилок
- `test_snapshots_have_valid_prices` — contract_price в snapshots > 0 і < 1.0

## Технічні вимоги

### Фреймворк
- `pytest` + `pytest-asyncio` для async тестів
- `pytest-cov` для покриття

### Структура файлів
```
tests/
├── conftest.py           — фікстури: db, df_candles, market_info, state mock
├── test_indicators.py    — Рівень 1.1
├── test_signals.py       — Рівень 1.2
├── test_risk.py          — Рівень 1.3
├── test_storage.py       — Рівень 1.4
├── test_scanner.py       — Рівень 2.1
├── test_execution.py     — Рівень 2.2
├── test_settlement.py    — Рівень 2.3
├── test_regressions.py   — Рівень 3
└── test_data_validation.py — Рівень 4
```

### Фікстури (conftest.py)

```python
@pytest.fixture
def sample_candles_bullish() -> pd.DataFrame:
    """100 свічок з чітким висхідним трендом для тестування UP сигналів."""

@pytest.fixture
def sample_candles_bearish() -> pd.DataFrame:
    """100 свічок з чітким низхідним трендом для тестування DOWN сигналів."""

@pytest.fixture
def sample_candles_sideways() -> pd.DataFrame:
    """100 свічок бічного руху — не повинен генерувати сигнал."""

@pytest.fixture
def market_info_active() -> dict:
    """Активний маркет з time_left=8 хв, price_yes=0.30, price_no=0.70."""

@pytest.fixture
def db_memory():
    """In-memory SQLite для тестування storage без файлів."""

@pytest.fixture
def mock_state_light(monkeypatch):
    """Mock state.mode='light' з відповідними порогами."""
```

### Запуск
```bash
cd polymarket_bot
pytest tests/ -v --cov=bot --cov-report=term-missing
```

### Пріоритет реалізації
1. **conftest.py** — фікстури (блокує все інше)
2. **test_signals.py** — найкритичніший модуль (вся торгова логіка)
3. **test_indicators.py** — фундамент сигналів
4. **test_risk.py** — ставки і edge
5. **test_storage.py** — цілісність даних
6. **test_regressions.py** — захист від повторних багів
7. Решта за потребою

## Критерії готовності
- [ ] Всі тести Рівня 1 проходять
- [ ] Покриття `signals.py` > 80%
- [ ] Покриття `indicators.py` > 80%
- [ ] Покриття `risk.py` > 90%
- [ ] Regression тести на всі відомі баги
- [ ] `pytest` запускається без помилок за < 10 секунд

---

## Детальний список тест-кейсів (по модулях)

### conftest.py — фікстури

```python
@pytest.fixture
def test_db(tmp_path):
    """Тимчасова SQLite БД, ізольована для кожного тесту."""

@pytest.fixture
def sample_df():
    """DataFrame 60 BTC 1m свічок з реалістичними цінами ~69000."""

@pytest.fixture
def sample_market():
    """dict market_info: event_start_iso, token_yes_id, token_no_id, outcome_prices."""

@pytest.fixture
def sample_signal():
    """dict сигналу: DOWN, confluence=4, GAP=135, ATR=82, cp=0.76."""

@pytest.fixture
def mock_execution_client():
    """AsyncMock ExecutionClient без реального CLOB."""
```

---

### test_signals.py

#### Голосування індикаторів

| Тест | Умова | Очікування |
|------|-------|------------|
| `test_vote_rsi_bearish` | RSI > 60 | DOWN vote |
| `test_vote_rsi_bullish` | RSI < 40 | UP vote |
| `test_vote_rsi_neutral` | RSI 40–60 | neutral, не рахується |
| `test_vote_macd_bearish` | bearish crossover | DOWN vote |
| `test_vote_macd_bullish` | bullish crossover | UP vote |
| `test_vote_macd_no_cross` | histogram не змінив знак | neutral |
| `test_vote_vwap_below` | price < VWAP | DOWN vote |
| `test_vote_vwap_above` | price > VWAP | UP vote |
| `test_vote_ema_bearish` | EMA9 < EMA21 | DOWN vote |
| `test_vote_ema_bullish` | EMA9 > EMA21 | UP vote |
| `test_vote_pivots_breakdown` | price < pivot support | DOWN vote |
| `test_vote_pivots_breakout` | price > pivot resistance | UP vote |

#### Confluence threshold

| Тест | Голоси | Очікування |
|------|--------|------------|
| `test_confluence_2_no_signal` | 2 | None |
| `test_confluence_3_signal` | 3 | сигнал |
| `test_confluence_4_signal` | 4 | confluence=4 |
| `test_confluence_5_signal` | 5 | confluence=5 |
| `test_confluence_split_no_signal` | 2 UP + 2 DOWN | None |

#### Фільтри

| Тест | Умова | Очікування |
|------|-------|------------|
| `test_filter_time_left_too_early` | time_left > TIME_LEFT_MAX | None |
| `test_filter_time_left_too_late` | time_left < TIME_LEFT_MIN | None |
| `test_filter_atr_dead_zone` | ATR < ATR_MIN | None |
| `test_filter_contract_price_too_low` | cp < CONTRACT_PRICE_MIN | None |
| `test_filter_contract_price_too_high` | cp > CONTRACT_PRICE_MAX | None |
| `test_filter_gap_too_small` | gap < GAP_MIN_USD | None |
| `test_filter_gap_ok` | gap >= 100 | сигнал |
| `test_filter_strict_gap_late` | time_left < 5 + gap < 100 | None |
| `test_filter_strict_gap_early` | time_left >= 5 + gap = 50 | OK |

#### A/B filter_version

| Тест | Умови | Очікуваний filter_version |
|------|-------|--------------------------|
| `test_fv_both` | gap>=80 + cc>=2 + macd_norm>=0.20 | "both" |
| `test_fv_current_only` | тільки поточний фільтр | "current" |
| `test_fv_new_only` | тільки новий фільтр | "new" |

---

### test_indicators.py

#### RSI

| Тест | Умова | Очікування |
|------|-------|------------|
| `test_rsi_formula_wilder` | Wilder's EMA alpha | збігається з еталоном |
| `test_rsi_range` | будь-які дані | 0–100 |
| `test_rsi_overbought` | сильний up-тренд | RSI > 70 |
| `test_rsi_oversold` | сильний down-тренд | RSI < 30 |
| `test_rsi_short_series` | < 14 свічок | NaN або fallback |

#### VWAP

| Тест | Умова | Очікування |
|------|-------|------------|
| `test_vwap_cumulative` | 3 свічки | накопичувально, не скидається |
| `test_vwap_zero_volume` | volume = 0 | без ZeroDivisionError |
| `test_vwap_single_candle` | 1 свічка | VWAP = typical price |

#### MACD

| Тест | Умова | Очікування |
|------|-------|------------|
| `test_macd_crossover_detected` | визначений crossover | True |
| `test_macd_histogram_sign_change` | зміна знаку hist | crossover True |
| `test_macd_no_false_cross` | без зміни знаку | crossover False |

#### ATR zone

| Тест | ATR | Zone |
|------|-----|------|
| `test_atr_zone_dead` | 15 | dead |
| `test_atr_zone_quiet` | 35 | quiet |
| `test_atr_zone_golden` | 75 | golden |
| `test_atr_zone_high` | 130 | high |
| `test_atr_zone_extreme` | 250 | extreme |

---

### test_risk.py

#### Win probability

| Тест | confluence | base_prob |
|------|-----------|-----------|
| `test_win_prob_conf3` | 3 | 60% |
| `test_win_prob_conf4` | 4 | 70% |
| `test_win_prob_conf5` | 5 | 80% |

| Тест | Модифікатор | Ефект |
|------|-------------|-------|
| `test_win_prob_golden_atr` | atr_zone=golden | +5% |
| `test_win_prob_dead_atr` | atr_zone=dead | -5% |
| `test_win_prob_cheap_price` | cp=0.52 | +2% |
| `test_win_prob_expensive_price` | cp=0.72 | -2% |
| `test_win_prob_good_time` | time_left=7 | +2% |
| `test_win_prob_late_entry` | time_left=2 | -3% |
| `test_win_prob_clamp_min` | всі негативні | >= 0.50 |
| `test_win_prob_clamp_max` | всі позитивні | <= 0.90 |

#### Kelly (disabled)

| Тест | Умова | Очікування |
|------|-------|------------|
| `test_kelly_disabled_returns_fixed` | Kelly вимкнено | fixed stake |
| `test_stake_uses_clob_ask` | clob_ask > cp | clob_ask береться |
| `test_stake_test_mode` | mode=test | test значення |

---

### test_storage.py

| Тест | Умова | Очікування |
|------|-------|------------|
| `test_schema_all_columns` | init_db | всі 24 колонки присутні |
| `test_migration_idempotent` | init_db двічі | без помилок |
| `test_db_path_test_mode` | mode=test | test.db |
| `test_db_path_live_mode` | mode=live | live.db |
| `test_save_signal_returns_id` | save_signal | int id > 0 |
| `test_save_signal_autoincrement` | 2 сигнали | id +1 |
| `test_mark_no_position_sets_result` | mark_no_position | result=NO_ENTRY, pnl=0 |
| `test_mark_no_position_excluded` | get_unresolved | не повертає |
| `test_paper_auto_approve` | AUTO_APPROVE=true | decision=approve |

---

### test_known_bugs.py (Regression)

| Тест | Баг | Захист |
|------|-----|--------|
| `test_no_duplicate_sleep_notification` | 💤 дублювання | mark_no_position → NO_ENTRY → settlement пропускає |
| `test_rsi_vote_not_contrarian` | RSI контраріанський | RSI > 60 → DOWN (не UP) |
| `test_gamma_price_fallback` | CLOB ask замість Gamma | clob_ask ≠ None → clob_ask |
| `test_sl_price_zero_on_no_sl` | sl_price=0 на SL_PERCENT=0 | не закриває одразу |
| `test_settlement_closes_position` | позиція не закривалась | positions.result оновлюється |
| `test_sell_ok_deduplication` | подвійний sell | sell_ok flag → ігнорується |
| `test_busy_timeout` | SQLite locked | busy_timeout=5000 → retry |
| `test_per_position_isolation` | крос-позиційне SL | SL однієї не закриває іншу |

---

## Статус відомих багів

| Баг | Статус | Regression тест |
|-----|--------|-----------------|
| RSI vote контраріанський | ❌ відкритий | `test_rsi_vote_not_contrarian` |
| 💤 дублювання (settlement) | ✅ fixed 2026-04-06 | `test_no_duplicate_sleep_notification` |
| sell_ok deduplication | ✅ fixed | `test_sell_ok_deduplication` |
| busy_timeout SQLite | ✅ fixed | `test_busy_timeout` |
| per-position SL isolation | ✅ fixed | `test_per_position_isolation` |
| settlement closes position | ✅ fixed | `test_settlement_closes_position` |
| sl_price=0 при SL_PERCENT=0 | ✅ fixed | `test_sl_price_zero_on_no_sl` |

---

## Coverage цілі

| Модуль | Мін. coverage |
|--------|---------------|
| `signals.py` | 85% |
| `indicators.py` | 90% |
| `risk.py` | 95% |
| `storage.py` | 80% |
| `settlement.py` | 70% |
