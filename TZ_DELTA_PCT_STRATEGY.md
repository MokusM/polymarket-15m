# ТЗ: Бот v2 — delta_pct стратегія

**Гілка:** `feat/delta-pct-strategy` (від `develop`)
**Директорія:** `D:\plou-v2\`
**Акаунт:** новий Polymarket гаманець
**Telegram:** новий бот (окремий token + chat_id)

---

## Концепція

Замість confluence-based фільтра (поточний бот) → **delta_pct** як основний фільтр.
Бектест на 22,164 сигналах показав: `|dp|>=0.2 + cc>=2 + pivots` = **91.6% ACC, EV +$0.222/долар**.

Поточний бот (conf=4) дає WR 79% при ~2 trades/day.
Новий фільтр дає WR 91% при ~10-17 trades/day — більше угод і вищий WR.

---

## Що змінити відносно поточного бота

### 1. signals.py — замінити логіку check_signals()

**Поточна логіка:**
```
5 індикаторів голосують → confluence >= 3 → сигнал
```

**Нова логіка:**
```python
# Основний фільтр: delta_pct >= 0.20%
delta_pct = abs((current_price - start_price) / start_price * 100)
if delta_pct < 0.20:
    return None

# Напрямок від delta (не від confluence)
direction = "UP" if current_price > start_price else "DOWN"

# Фільтр 1: consecutive_closes >= 2 в бік сигналу
cc = consecutive_closes  # вже рахується в indicators.py
if direction == "UP" and cc < 2:
    return None
if direction == "DOWN" and cc > -2:
    return None

# Фільтр 2: Pivots підтверджують
# UP: price >= pivot_high (breakout)
# DOWN: price <= pivot_low (breakdown)
if direction == "UP" and price < pivot_high:
    return None
if direction == "DOWN" and price > pivot_low:
    return None

# Проходить — генеруємо сигнал
```

**Що прибрати:**
- RSI/MACD/VWAP/EMA голосування для визначення напрямку
- MIN_CONFLUENCE поріг
- confluence поле (або залишити для інформації)

**Що залишити:**
- ATR zone фільтр (ATR_MIN_USD)
- Time left фільтр (3-12 хв)
- Contract price фільтр (0.45-0.80)
- GAP фільтр (замінюється delta_pct)
- filter_version A/B (можна прибрати)
- Всі індикатори рахуються і зберігаються (для аналізу)

### 2. state.py — нові пороги

```python
"light": {
    "DELTA_PCT_MIN": 0.20,      # мінімальний |delta_pct|
    "CC_MIN": 2,                 # мінімальний consecutive_closes
    "PIVOTS_REQUIRED": True,     # pivots breakout/breakdown обов'язковий
    "TIME_LEFT_MIN_MINUTES": 3,
    "TIME_LEFT_MAX_MINUTES": 12,
    "CONTRACT_PRICE_MIN": 0.45,
    "CONTRACT_PRICE_MAX": 0.80,
    "ATR_MIN_USD": 25,
    "ALLOW_DEAD_ZONE": False,
}
```

### 3. config.py — новий .env

```env
# SECRETS
TELEGRAM_TOKEN=<НОВИЙ_ТОКЕН>
CHAT_ID=<НОВИЙ_CHAT_ID>
POLYMARKET_PRIVATE_KEY=<НОВИЙ_КЛЮЧ>
POLYMARKET_CHAIN_ID=137
POLYMARKET_SIGNATURE_TYPE=2
POLYMARKET_FUNDER_ADDRESS=<НОВА_АДРЕСА>

# INSTANCE
DEFAULT_MODE=light
TELEGRAM_ENABLED=true
LIVE_TRADING=true
AUTO_APPROVE_LIVE=true
AUTO_APPROVE_PAPER=true

# BANKROLL
BANKROLL_USD=200
KELLY_FRACTION=0.5
MIN_STAKE_USD=1
MAX_STAKE_USD=10
STAKE_USD=10
MAX_OPEN_POSITIONS=1

# CLOB
CLOB_CROSS_SPREAD_BUY=true
CLOB_MAX_BUY_SLIPPAGE_ABS=0.05
CLOB_SPREAD_MAX=1

# SL / TP
SL_PERCENT=55
TP_PARTIAL_PRICE=1.01
TP_PARTIAL_SELL_PCT=0
TP_MID_PRICE=1.01
TP_FULL_PRICE=1.01
TP_FINAL_PRICE=1.01
POSITION_MONITOR_INTERVAL=3
BREAKEVEN_AFTER_ROI_PCT=0
TRAILING_BE_TRIGGER=0.90
TRAILING_BE_SL_PCT=10

# DB
DB_LABEL=v2
```

### 4. Tick snapshots — зберегти

Tick snapshots кожні 3 сек вже є в коді (від plouLight). Переконатись що працює.
Таблиця `tick_snapshots`: signal_id, seconds_after, contract_bid, contract_ask.

### 5. Stabilization фільтр

Залишити як в основному боті:
- `volume_state == "stabilization"` → ставка MIN_STAKE_USD ($1)
- Не блокуємо, збираємо дані

### 6. Settlement — зведений звіт

Залишити зведений звіт по маркету (як в основному боті).

### 7. Telegram команди

Залишити: /stop, /start, /list, /status, /mode, /balance, /reset, /diagnose

---

## Що НЕ міняти

- execution_client.py — ордери ідентичні
- position_manager.py — SL/TP/trailing BE ідентичні
- settlement.py — settlement ідентичний
- ws_settlement.py — WebSocket settlement ідентичний
- storage.py — таблиці ідентичні
- indicators.py — всі індикатори залишаються
- exchange_client.py — Binance candles ідентичні
- polymarket_client.py — market discovery ідентичний

---

## Етапи

1. **Створити гілку** `feat/delta-pct-strategy` від `develop`
2. **Змінити signals.py** — нова логіка фільтра
3. **Оновити state.py** — нові пороги
4. **Створити .env** з новими секретами
5. **Тест** — запустити, перевірити що генерує сигнали
6. **Перший день** — $10 ставка, спостерігати
7. **Тиждень** — аналіз WR vs бектест

---

## Очікувані результати

| Метрика | Поточний бот | Новий бот |
|---------|-------------|-----------|
| Фільтр | conf>=4 | |dp|>=0.2 + cc>=2 + pivots |
| WR (бектест) | 82-85% | **91.6%** |
| Trades/day | 2-3 | **10-17** |
| EV/trade | +$0.13/$ | **+$0.22/$** |
| Ставка | $25 | $10 (поки) |
| Прогноз/міс | ~$300 | **~$400-600** |

---

## Ризики

1. **Бектест ≠ live** — delta_pct рахувався по Gamma, live буде по CLOB
2. **Більше trades = більше комісій** — slippage на кожному ордері
3. **Pivots на live можуть відрізнятись** — залежить від lookback period
4. **Два боти = подвійний ризик** — якщо обидва в мінусі одночасно
