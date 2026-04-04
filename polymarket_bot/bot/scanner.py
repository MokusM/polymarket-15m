import asyncio
import logging
from datetime import datetime

import httpx
import pandas as pd

from bot.config import (
    SCAN_INTERVAL_SECONDS,
    COOLDOWN_SECONDS,
    MAX_SIGNALS_PER_ROUND_PER_SIDE,
    REPEAT_ALERTS_AFTER_COOLDOWN,
    NOTIFY_SESSION_CHANGE,
    OBI_MIN_RATIO,
    OBI_LEVELS,
    CLOB_SPREAD_MAX,
    CONTRACT_PRICE_HIGH_MIN,
    GAP_STRICT_USD,
    ALT_SCAN_ENABLED,
)
from bot.exchange_client import ExchangeClient
from bot.polymarket_client import PolymarketClient
from bot.indicators import add_indicators
from bot.signals import check_signals, check_alt_signals
from bot.state import state
from bot.storage import save_signal, save_shadow_signal, save_signal_snapshot, save_alt_signal, resolve_alt_signals
from bot.telegram_bot import send_alert, send_info_message
from bot.alert_text import get_current_session_key, format_session_alert_html

logger = logging.getLogger(__name__)


def _parse_signal_key(key: str) -> tuple[str, str] | None:
    """Ключ виду '{market_id}_{UP|DOWN}'."""
    if "_" not in key:
        return None
    direction = key.rsplit("_", 1)[-1]
    if direction not in ("UP", "DOWN"):
        return None
    market_id = key[: -(len(direction) + 1)]
    return market_id, direction


class Scanner:
    def __init__(self):
        self.exchange = ExchangeClient()
        self.poly = PolymarketClient()
        self.last_signal_time = {}
        self.signal_counts = {}
        self._last_session_key: str | None = None
        # Cache for /diagnose command
        self.last_df: pd.DataFrame = pd.DataFrame()
        self.last_markets: list = []

    async def run(self):
        logger.info("Пошук активних ринків BTC...")
        
        while True:
            try:
                # 1. Оновлюємо список подій Polymarket 
                markets_with_prices = await self.poly.get_active_btc_markets()
                
                active_ids = {str(m["market_id"]) for m in markets_with_prices}

                if len(markets_with_prices) == 0:
                    logger.info("❌ Активних 15-хвилинних BTC маркетів на даний момент немає або вони відфільтровані.")
                else:
                    logger.info(f"✅ В логіку індикаторів передано {len(markets_with_prices)} маркетів.")

                # Маркет випав зі списку (нове 15-хв вікно) — скидаємо кулдаун/лічильники для старого id
                for k in list(self.last_signal_time.keys()):
                    parsed = _parse_signal_key(k)
                    if parsed and parsed[0] not in active_ids:
                        self.last_signal_time.pop(k, None)
                        self.signal_counts.pop(k, None)

                # 2. Отримуємо свіжі свічки
                df = await self.exchange.get_btc_1m_candles(limit=100)
                
                if df.empty:
                    logger.warning("Не вдалось отримати свічки зі біржі. Чекаємо...")
                    await asyncio.sleep(SCAN_INTERVAL_SECONDS)
                    continue

                # 3. Рахуємо індикатори
                df_with_indicators = add_indicators(df)
                self.last_df = df_with_indicators
                self.last_markets = markets_with_prices

                # 3.5 Сесійний алерт при зміні торгової сесії
                if NOTIFY_SESSION_CHANGE:
                    await self._check_session_change(df_with_indicators)

                # 4. Перевіряємо ринки
                for market_prices in markets_with_prices:
                    _shadow: dict = {}
                    signal = check_signals(market_prices, df_with_indicators, _shadow)

                    # Логуємо відхилені сигнали (якщо є confluence ≥ 2)
                    if signal is None and _shadow.get("confluence", 0) >= 2:
                        adx_val = None
                        if "adx" in df_with_indicators.columns:
                            try:
                                adx_val = round(float(df_with_indicators["adx"].iloc[-1]), 2)
                            except Exception:
                                pass
                        save_shadow_signal(
                            market_id=str(market_prices["market_id"]),
                            direction=_shadow.get("direction"),
                            contract_price=_shadow.get("contract_price"),
                            confluence=_shadow.get("confluence", 0),
                            reject_reason=_shadow.get("reject_reason", "unknown"),
                            time_left=_shadow.get("time_left"),
                            gap=_shadow.get("gap"),
                            atr=_shadow.get("atr"),
                            adx=adx_val,
                            btc_price=_shadow.get("btc_price"),
                        )

                    if signal:
                        direction = signal["direction"]
                        market_id = str(market_prices["market_id"])

                        # ── OBI filter (Binance stakan, skip in test mode) ──
                        obi = 1.0
                        if state.mode != "test":
                            obi = await self.exchange.get_order_book_imbalance(
                                levels=OBI_LEVELS
                            )
                            # UP: потрібен bid > ask (bullish); DOWN: потрібен ask > bid
                            # OBI_MIN_RATIO=0 → фільтр вимкнено
                            if OBI_MIN_RATIO <= 0:
                                obi_pass = True
                            else:
                                obi_pass = (
                                    obi >= OBI_MIN_RATIO
                                    if direction == "UP"
                                    else obi <= (1.0 / OBI_MIN_RATIO)
                                )
                            if not obi_pass:
                                logger.debug(
                                    "OBI %.3f не відповідає напрямку %s (threshold=%.2f) — skip",
                                    obi, direction, OBI_MIN_RATIO,
                                )
                                continue
                        signal["obi"] = obi

                        # ── CLOB: фетчимо реальну ціну для статистики + фільтри ──
                        clob_ask = clob_bid = 0.0
                        if state.mode != "test":
                            token_id = (
                                market_prices.get("token_yes_id")
                                if direction == "UP"
                                else market_prices.get("token_no_id")
                            )
                            if token_id:
                                clob_ask, clob_bid = await self._fetch_clob_best_prices(token_id)

                                # Priority 4: spread gate
                                if clob_ask > 0 and clob_bid > 0:
                                    spread = clob_ask - clob_bid
                                    if spread > CLOB_SPREAD_MAX:
                                        logger.debug(
                                            "CLOB spread %.3f > %.3f — skip",
                                            spread, CLOB_SPREAD_MAX,
                                        )
                                        save_shadow_signal(
                                            market_id=market_id, direction=direction,
                                            contract_price=signal.get("contract_price"),
                                            clob_ask=clob_ask, confluence=signal.get("confluence", 0),
                                            reject_reason="clob_spread_too_wide",
                                            time_left=signal.get("time_left"), gap=signal.get("gap"),
                                            atr=signal.get("atr"),
                                            adx=signal.get("adx"),
                                            btc_price=signal.get("current_price"),
                                        )
                                        continue

                                # Priority 1: high-price GAP gate
                                if clob_ask > CONTRACT_PRICE_HIGH_MIN:
                                    gap = signal.get("gap", 0)
                                    if abs(gap) < GAP_STRICT_USD:
                                        logger.debug(
                                            "CLOB ask %.2f > %.2f (FLB zone) but GAP %.1f < %.0f — skip",
                                            clob_ask, CONTRACT_PRICE_HIGH_MIN, gap, GAP_STRICT_USD,
                                        )
                                        save_shadow_signal(
                                            market_id=market_id, direction=direction,
                                            contract_price=signal.get("contract_price"),
                                            clob_ask=clob_ask, confluence=signal.get("confluence", 0),
                                            reject_reason="clob_ask_too_high",
                                            time_left=signal.get("time_left"), gap=gap,
                                            atr=signal.get("atr"),
                                            adx=signal.get("adx"),
                                            btc_price=signal.get("current_price"),
                                        )
                                        continue

                                # Зберігаємо реальну CLOB ціну в сигнал (для статистики)
                                if clob_ask > 0:
                                    # CLOB price zone check — Gamma могла пройти фільтр, але CLOB вища
                                    th = state.get_thresholds()
                                    if clob_ask > th["CONTRACT_PRICE_MAX"]:
                                        logger.debug(
                                            "CLOB ask %.2f > CONTRACT_PRICE_MAX %.2f — skip",
                                            clob_ask, th["CONTRACT_PRICE_MAX"],
                                        )
                                        save_shadow_signal(
                                            market_id=market_id, direction=direction,
                                            contract_price=signal.get("contract_price"),
                                            clob_ask=clob_ask, confluence=signal.get("confluence", 0),
                                            reject_reason="clob_ask_too_high",
                                            time_left=signal.get("time_left"), gap=signal.get("gap"),
                                            atr=signal.get("atr"),
                                            adx=signal.get("adx"),
                                            btc_price=signal.get("current_price"),
                                        )
                                        continue
                                    signal["clob_ask"] = clob_ask
                                    signal["clob_bid"] = clob_bid
                                    signal["clob_spread"] = round(clob_ask - clob_bid, 4)
                                    # contract_price = реальна CLOB ask (замість Gamma)
                                    signal["contract_price"] = clob_ask

                        key = f"{market_id}_{direction}"
                        now = datetime.now().timestamp()
                        last_time = self.last_signal_time.get(key, 0)
                        count = self.signal_counts.get(key, 0)

                        cooldown_ok = now - last_time >= COOLDOWN_SECONDS
                        if REPEAT_ALERTS_AFTER_COOLDOWN:
                            can_send = cooldown_ok
                        else:
                            can_send = cooldown_ok and (
                                count < MAX_SIGNALS_PER_ROUND_PER_SIDE
                            )

                        if can_send:
                            signal["market_id"] = market_id
                            signal["market_slug"] = market_prices.get("market_slug") or ""
                            signal["neg_risk"] = bool(
                                market_prices.get("neg_risk", False)
                            )
                            signal["market_title"] = (
                                market_prices.get("title")
                                or market_prices.get("question")
                                or ""
                            )

                            # ── Збір додаткових даних (тільки для статистики) ──
                            try:
                                from bot.indicators import calculate_rsi
                                df_3m = await self.exchange.get_btc_candles("3m", 50)
                                df_5m = await self.exchange.get_btc_candles("5m", 50)
                                if not df_3m.empty:
                                    signal["rsi_3m"] = round(float(calculate_rsi(df_3m["close"], 14).iloc[-1]), 2)
                                    taker_vol = df_3m["taker_buy_base"].iloc[-5:].sum()
                                    total_vol = df_3m["volume"].iloc[-5:].sum()
                                    signal["taker_ratio"] = round(float(taker_vol / total_vol), 4) if total_vol > 0 else None
                                if not df_5m.empty:
                                    signal["rsi_5m"] = round(float(calculate_rsi(df_5m["close"], 14).iloc[-1]), 2)
                                signal["funding_rate"] = await self.exchange.get_funding_rate()
                                # ADX з df_with_indicators (порахований в add_indicators)
                                if "adx" in df_with_indicators.columns:
                                    signal["adx"] = round(float(df_with_indicators["adx"].iloc[-1]), 2)

                                # ── Momentum поля (тільки статистика, без фільтрів) ──
                                try:
                                    _df = df_with_indicators
                                    _c = _df["close"].reset_index(drop=True)
                                    _o = _df["open"].reset_index(drop=True)
                                    _v = _df["volume"].reset_index(drop=True)

                                    # 1. consecutive_closes
                                    _direction = signal.get("direction", "UP")
                                    _streak = 0
                                    for _i in range(1, min(6, len(_c))):
                                        _bullish = float(_c.iloc[-_i]) > float(_o.iloc[-_i])
                                        if (_direction == "UP" and _bullish) or (_direction == "DOWN" and not _bullish):
                                            _streak += 1
                                        else:
                                            break
                                    signal["consecutive_closes"] = _streak

                                    # 2. speed_2m vs speed_5m
                                    if len(_c) >= 8:
                                        _speed_2m = abs(float(_c.iloc[-1]) - float(_c.iloc[-3])) / 2
                                        _speed_5m = abs(float(_c.iloc[-3]) - float(_c.iloc[-8])) / 5
                                        signal["speed_2m"] = round(_speed_2m, 2)
                                        signal["speed_5m"] = round(_speed_5m, 2)
                                        signal["speed_accel"] = round(_speed_2m - _speed_5m, 2)

                                    # 3. obv_slope
                                    if len(_c) >= 5:
                                        _diff = _c.diff()
                                        _sign = _diff.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
                                        _obv = (_v * _sign).cumsum()
                                        signal["obv_slope"] = round(float(_obv.iloc[-1]) - float(_obv.iloc[-4]), 2)

                                    # 4. vwap_cross
                                    if "vwap" in _df.columns and len(_c) >= 2:
                                        _vwap = _df["vwap"].reset_index(drop=True)
                                        _prev_above = bool(float(_c.iloc[-2]) > float(_vwap.iloc[-2]))
                                        _curr_above = bool(float(_c.iloc[-1]) > float(_vwap.iloc[-1]))
                                        signal["vwap_cross"] = _curr_above != _prev_above
                                except Exception as _me:
                                    logger.warning("Momentum fields error: %s", _me)
                            except Exception as e:
                                logger.debug("Extra data collection error (non-critical): %s", e)

                            sig_id = save_signal(signal)
                            if sig_id:
                                asyncio.create_task(send_alert(sig_id, signal))
                                self.last_signal_time[key] = now
                                self.signal_counts[key] = count + 1
                                logger.info(f"✅ Згенеровано сигнал #{sig_id}: {direction} для маркету {market_id}")

                                # ── Price snapshots: трекаємо ціну контракту через 1/2/3/5/10 хв ──
                                token_id_snap = (
                                    market_prices.get("token_yes_id") if direction == "UP"
                                    else market_prices.get("token_no_id")
                                )
                                if token_id_snap and state.mode != "test":
                                    asyncio.create_task(
                                        self._schedule_price_snapshots(sig_id, token_id_snap)
                                    )

                # ── ALT assets (ETH, SOL): збір даних паралельно з BTC ──
                if ALT_SCAN_ENABLED and state.mode != "test":
                    await self._scan_alt_assets(df)

            except Exception as e:
                logger.error(f"Непередбачена помилка в циклі сканування: {e}", exc_info=True)

            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    async def _scan_alt_assets(self, btc_df: pd.DataFrame) -> None:
        """Сканує ETH і SOL ринки і зберігає сигнали в alt_signals для статистики."""
        # Resolve завершені ринки
        try:
            current_prices = {}
            for asset in ("ETH", "SOL"):
                df_tmp = await self.exchange.get_1m_candles(f"{asset}USDT", limit=2)
                if not df_tmp.empty:
                    current_prices[asset] = float(df_tmp.iloc[-1]["close"])
            if current_prices:
                n = resolve_alt_signals(current_prices)
                if n > 0:
                    logger.info("ALT resolved %d signals", n)
        except Exception as e:
            logger.debug("resolve_alt_signals error: %s", e)

        for asset in ("ETH", "SOL"):
            try:
                symbol = f"{asset}USDT"
                markets = await self.poly.get_active_alt_markets(asset)
                if not markets:
                    continue

                df_raw = await self.exchange.get_1m_candles(symbol=symbol, limit=100)
                if df_raw.empty:
                    logger.debug("ALT %s: порожній датафрейм", asset)
                    continue

                df_ind = add_indicators(df_raw)

                for market in markets:
                    try:
                        sig = check_alt_signals(market, df_ind, asset, btc_df)
                        if sig is None:
                            continue

                        sig["market_id"] = str(market.get("market_id", ""))

                        # CLOB ask для реальної ціни
                        direction = sig["direction"]
                        token_id = (
                            market.get("token_yes_id") if direction == "UP"
                            else market.get("token_no_id")
                        )
                        if token_id:
                            clob_ask, _ = await self._fetch_clob_best_prices(token_id)
                            if clob_ask > 0:
                                sig["clob_ask"] = clob_ask

                        sig_id = save_alt_signal(sig)
                        logger.info(
                            "ALT %s #%s: %s | gap=%.2f%% | btc_aligned=%s | clob=%.3f",
                            asset, sig_id, direction,
                            sig.get("gap_pct", 0),
                            sig.get("btc_aligned"),
                            sig.get("clob_ask", 0),
                        )
                    except Exception as e:
                        logger.debug("ALT %s market error: %s", asset, e)

            except Exception as e:
                logger.warning("ALT %s scan error: %s", asset, e)

    async def _check_session_change(self, df: pd.DataFrame) -> None:
        current = get_current_session_key()
        if current == self._last_session_key:
            return
        self._last_session_key = current

        btc_price = atr = atr_zone = chg_1h = None
        if not df.empty:
            last = df.iloc[-1]
            btc_price = float(last.get("close", 0)) or None
            raw_atr = last.get("atr", None)
            if raw_atr is not None and not pd.isna(raw_atr):
                atr = float(raw_atr)
            atr_zone = last.get("atr_zone", None)
            raw_chg = last.get("chg_1h", None)
            if raw_chg is not None and not pd.isna(raw_chg):
                chg_1h = float(raw_chg)

        text = format_session_alert_html(current, btc_price, atr, atr_zone, chg_1h)
        asyncio.create_task(send_info_message(text))
        logger.info("Session change → %s", current)

    async def _fetch_clob_best_prices(self, token_id: str) -> tuple[float, float]:
        """Return (best_ask, best_bid) from CLOB public book API. Returns (0, 0) on error."""
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(
                    "https://clob.polymarket.com/book",
                    params={"token_id": token_id},
                )
                if r.status_code != 200:
                    return 0.0, 0.0
                data = r.json()
                asks = data.get("asks") or []
                bids = data.get("bids") or []
                # Polymarket CLOB sorts asks DESC (worst→best) and bids ASC (worst→best)
                # so best ask = asks[-1], best bid = bids[-1]
                best_ask = float(asks[-1]["price"]) if asks else 0.0
                best_bid = float(bids[-1]["price"]) if bids else 0.0
                return best_ask, best_bid
        except Exception as e:
            logger.debug("_fetch_clob_best_prices(%s): %s", token_id[:12], e)
            return 0.0, 0.0

    async def _schedule_price_snapshots(self, signal_id: int, token_id: str) -> None:
        """Записує ціну контракту і BTC через 1/2/3/5/10 хв після сигналу."""
        elapsed = 0
        for minutes in (1, 2, 3, 5, 10):
            await asyncio.sleep((minutes - elapsed) * 60)
            elapsed = minutes
            try:
                contract_price = None
                try:
                    async with httpx.AsyncClient(timeout=3.0) as c:
                        r = await c.get(
                            "https://clob.polymarket.com/book",
                            params={"token_id": token_id},
                        )
                        if r.status_code == 200:
                            asks = r.json().get("asks") or []
                            contract_price = float(asks[-1]["price"]) if asks else None
                except Exception:
                    pass
                df_now = await self.exchange.get_btc_1m_candles(limit=2)
                btc_now = float(df_now.iloc[-1]["close"]) if not df_now.empty else None
                save_signal_snapshot(signal_id, minutes, btc_now, contract_price)
            except Exception as e:
                logger.debug("Snapshot error #%s +%dmin: %s", signal_id, minutes, e)

    async def close(self):
        await self.exchange.close()
        await self.poly.close()
