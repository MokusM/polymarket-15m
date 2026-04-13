# ТЗ: Multi-Strategy Architecture

**Дата:** 2026-04-12
**Гілка:** feat/multi-strategy (від develop, після мержа поточних змін)

---

## Концепція

Один бот, один сканер, один WebSocket — кілька стратегій з різними фільтрами і гаманцями.

```
                    ┌─────────────┐
                    │  WS Binance │ BTC/ETH/SOL real-time
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │   Scanner   │ candles + indicators + CLOB
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │   Signal    │ direction, confluence, gap, atr, etc.
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
        ┌─────▼─────┐ ┌───▼───┐ ┌──────▼──────┐
        │ Strategy 1│ │Strat 2│ │  Strategy 3 │
        │confluence │ │delta% │ │ contrarian  │
        │ conf>=4   │ │dp>=0.1│ │ buy cheap   │
        │ $25 stake │ │$10    │ │ $5          │
        │ wallet A  │ │wall B │ │ wallet C    │
        └─────┬─────┘ └───┬───┘ └──────┬──────┘
              │            │            │
        ┌─────▼─────┐ ┌───▼───┐ ┌──────▼──────┐
        │  Execute  │ │Execute│ │   Execute   │
        │ CLOB API  │ │CLOB   │ │  CLOB API   │
        └───────────┘ └───────┘ └─────────────┘
```

---

## Структура

### strategies.py (новий файл)

```python
STRATEGIES = [
    {
        "id": "confluence",
        "name": "Confluence v1",
        "enabled": True,
        "filter": lambda signal: (
            signal.get("confluence", 0) >= 4
            and signal.get("volume_state") != "stabilization"
        ),
        "stake_usd": 25,
        "wallet_key": "POLYMARKET_PRIVATE_KEY",  # з .env
        "telegram_chat_id": "CHAT_ID",  # той самий або окремий
        "assets": ["BTC"],
    },
    {
        "id": "delta_pct",
        "name": "Delta PCT v2",
        "enabled": True,
        "filter": lambda signal: (
            abs(signal.get("delta_percent", 0)) >= 0.10
            and abs(signal.get("consecutive_closes", 0)) >= 2
        ),
        "stake_usd": 10,
        "wallet_key": "POLYMARKET_PRIVATE_KEY_V2",
        "telegram_chat_id": "CHAT_ID_V2",
        "assets": ["BTC"],
    },
    {
        "id": "data_collector",
        "name": "Data Only",
        "enabled": True,
        "filter": lambda signal: signal.get("confluence", 0) >= 3,
        "stake_usd": 1,  # мінімум для реальних даних
        "wallet_key": "POLYMARKET_PRIVATE_KEY",  # той самий акаунт
        "telegram_chat_id": "CHAT_ID",
        "assets": ["BTC", "ETH", "SOL"],
    },
]
```

### Зміни в scanner.py

```python
# Замість одного send_alert:
for strategy in STRATEGIES:
    if not strategy["enabled"]:
        continue
    if signal.get("asset", "BTC") not in strategy["assets"]:
        continue
    if strategy["filter"](signal):
        # Зберегти сигнал з strategy_id
        sig_id = save_signal(signal, strategy_id=strategy["id"])
        # Виконати з відповідним гаманцем і ставкою
        await execute_for_strategy(sig_id, signal, strategy)
```

### Зміни в execution_client.py

```python
class ExecutionClientPool:
    """Пул клієнтів — один на гаманець."""
    
    def __init__(self):
        self.clients = {}  # wallet_key → ExecutionClient
    
    def get_client(self, wallet_key: str) -> ExecutionClient:
        if wallet_key not in self.clients:
            self.clients[wallet_key] = ExecutionClient(
                private_key=os.getenv(wallet_key)
            )
        return self.clients[wallet_key]
```

### Зміни в storage.py

```python
# signals таблиця: додати strategy_id колонку
# ALTER TABLE signals ADD COLUMN strategy_id TEXT

# positions таблиця: додати strategy_id
# ALTER TABLE positions ADD COLUMN strategy_id TEXT
```

### Зміни в .env

```env
# Wallet 1 (основний)
POLYMARKET_PRIVATE_KEY=0x...
CHAT_ID=445690246

# Wallet 2 (v2 стратегія)
POLYMARKET_PRIVATE_KEY_V2=0x...
CHAT_ID_V2=445690246  # або окремий бот

# Wallet 3 (якщо потрібен)
POLYMARKET_PRIVATE_KEY_V3=0x...
```

### Зміни в telegram_bot.py

```python
# /status показує всі стратегії
# /stop confluence — зупинити конкретну стратегію
# /start delta_pct — запустити конкретну
# /positions — показати позиції всіх стратегій з міткою
```

### Зміни в position_manager.py

```python
# monitor_positions_loop — перевіряє позиції всіх стратегій
# SL/trailing BE — по стратегії (різні налаштування)
```

---

## Що НЕ змінюється

- ws_binance.py — один WS для всіх
- indicators.py — одні індикатори
- signals.py — check_signals рахує все, стратегії фільтрують
- polymarket_client.py — один клієнт для market discovery
- settlement.py — settlement для всіх позицій

---

## Переваги

1. **Один сканер** — менше API запитів, менше шансів на rate limit
2. **Один процес** — простіше підтримувати
3. **Спільні дані** — всі стратегії бачать ті самі індикатори
4. **Легко додати стратегію** — dict в STRATEGIES, не новий бот
5. **A/B тест** — одні дані, різні фільтри, чесне порівняння
6. **Один Telegram** — або окремі канали для кожної стратегії

---

## Етапи реалізації

1. Змержити поточні зміни (WS, bugfixes) в develop
2. Створити feat/multi-strategy від develop
3. Додати strategies.py з конфігурацією
4. Рефакторити scanner: signal → strategy router
5. ExecutionClientPool для multi-wallet
6. storage: strategy_id в signals/positions
7. telegram: per-strategy /stop /start /status
8. Тест на 2 стратегіях (confluence + data_collector)
9. Додати delta_pct як третю стратегію
10. Мерж в develop

---

## Ризики

- Рефакторинг scanner — може зламати поточну торгівлю
- Multi-wallet — потрібен другий гаманець з депозитом
- Складність коду зростає — більше місць для багів
- Position limit — per-strategy per-asset, не глобальний

---

## Оцінка: ~2-3 дні роботи
