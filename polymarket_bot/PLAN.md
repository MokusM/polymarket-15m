# Plan

## Pending

- [ ] **Edge/Kelly калібрування** — зараз edge перевірка вимкнена (ставка $10 фіксована при edge≤0). Після збору ~1 тижня даних порахувати реальний WR по режимах (light/medium/strict) з plouLight/Medium/Strict БД, підставити справжні win_prob в `estimate_win_probability()` і повернути gate `edge > 0`. Файл: `bot/risk.py`, `bot/telegram_bot.py`.

- [ ] **ExecutionClient retry при старті** — при `ConnectionTerminated` від Polymarket CLOB під час init робити N повторних спроб з затримкою, щоб `LIVE=OFF` не залишався після тимчасової мережевої помилки
- [ ] **Limit order fill tracking** — відстеження чи заповнився ліміт ордер у стакані (зараз deprioritized)
- [ ] **Multi-account support** (deferred)

## Done

- [x] MTF RSI filter (+4-8% accuracy у бектесті) — зараз вимкнено, повернути коли ринок активний
- [x] TOML config → прибрано, режими в `state.get_thresholds()`, `.env` тільки секрети
- [x] TP L1/L2/L3 full-close — закриває позицію в БД коли продає всі shares
- [x] Settlement PnL fix — WIN/LOSS замість CLOSED_EARLY для денного звіту
- [x] /filters команда — показує активні пороги поточного режиму
