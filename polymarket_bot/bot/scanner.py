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
    CLOB_SPREAD_MAX,
    ALT_SCAN_ENABLED,
)
from bot.exchange_client import ExchangeClient
from bot.polymarket_client import PolymarketClient
from bot.indicators import add_indicators
from bot.signals import check_signals, check_alt_signals
from bot.state import state
from bot.storage import save_shadow_signal, save_signal_snapshot, save_alt_signal, resolve_alt_signals, resolve_shadow_signals, get_db_path
from bot.telegram_bot import send_info_message
from bot.strategy_router import route_signal
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
    def __init__(self, execution_clients: dict | None = None):
        self.exchange = ExchangeClient()
        self.poly = PolymarketClient()
        self.last_signal_time = {}
        self.signal_counts = {}
        self._last_session_key: str | None = None
        # Cache for /diagnose command
        self.last_df: pd.DataFrame = pd.DataFrame()
        self.last_markets: list = []
        # Multi-strategy
        self.execution_clients = execution_clients or {}
        self._route_cooldowns: dict = {}

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

                # 2. Отримуємо свіжі свічки (REST кожні 30 сек, WS оновлює між запитами)
                now = datetime.now().timestamp()
                _rest_interval = 30  # REST запит кожні 30 сек замість 3
                if not hasattr(self, '_last_rest_fetch') or now - self._last_rest_fetch >= _rest_interval or self.last_df.empty:
                    df = await self.exchange.get_btc_1m_candles(limit=100)
                    if df.empty:
                        logger.warning("Не вдалось отримати свічки зі біржі. Чекаємо...")
                        await asyncio.sleep(SCAN_INTERVAL_SECONDS)
                        continue
                    self._last_rest_df = df.copy()
                    self._last_rest_fetch = now
                else:
                    df = self._last_rest_df.copy()

                # Оновити останню свічку з WS (реальний час замість 30-сек затримки)
                from bot import ws_binance
                _ws_kline = ws_binance.get_kline("BTCUSDT")
                if _ws_kline and not df.empty:
                    df.iloc[-1, df.columns.get_loc("close")] = _ws_kline["close"]
                    df.iloc[-1, df.columns.get_loc("high")] = max(float(df.iloc[-1]["high"]), _ws_kline["high"])
                    df.iloc[-1, df.columns.get_loc("low")] = min(float(df.iloc[-1]["low"]), _ws_kline["low"])

                # 3. Рахуємо індикатори
                df_with_indicators = add_indicators(df)
                self.last_df = df_with_indicators
                self.last_markets = markets_with_prices

                # 3.5 Сесійний алерт при зміні торгової сесії
                if NOTIFY_SESSION_CHANGE:
                    await self._check_session_change(df_with_indicators)

                # 4. Перевіряємо ринки
                th = state.get_thresholds()
                for market_prices in markets_with_prices:
                    market_id = str(market_prices["market_id"])

                    # ── CLOB: фетчимо обидва токени одразу для price-gate і відображення ──
                    clob_yes_ask = clob_yes_bid = 0.0
                    clob_no_ask = clob_no_bid = 0.0
                    if state.mode != "test":
                        token_yes = market_prices.get("token_yes_id")
                        token_no = market_prices.get("token_no_id")
                        if token_yes:
                            clob_yes_ask, clob_yes_bid = await self._fetch_clob_best_prices(token_yes)
                        if token_no:
                            clob_no_ask, clob_no_bid = await self._fetch_clob_best_prices(token_no)
                        market_prices["clob_yes_ask"] = clob_yes_ask
                        market_prices["clob_no_ask"] = clob_no_ask

                    if state.mode != "test":
                        logger.info(
                            "CLOB %s: YES ask=%.2f NO ask=%.2f",
                            market_id[:12], clob_yes_ask, clob_no_ask,
                        )

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
                            market_id=market_id,
                            direction=_shadow.get("direction"),
                            contract_price=_shadow.get("contract_price"),
                            confluence=_shadow.get("confluence", 0),
                            reject_reason=_shadow.get("reject_reason", "unknown"),
                            time_left=_shadow.get("time_left"),
                            gap=_shadow.get("gap"),
                            atr=_shadow.get("atr"),
                            adx=adx_val,
                            btc_price=_shadow.get("btc_price"),
                            end_date_iso=market_prices.get("end_date_iso"),
                        )

                    if signal:
                        direction = signal["direction"]
                        clob_ask = clob_yes_ask if direction == "UP" else clob_no_ask
                        clob_bid = clob_yes_bid if direction == "UP" else clob_no_bid

                        # ── CLOB price zone filter (перша перевірка — до OBI/MTF) ──
                        if state.mode != "test":
                            if clob_ask <= 0:
                                # Retry once before skipping
                                token_id_retry = market_prices.get("token_yes_id") if direction == "UP" else market_prices.get("token_no_id")
                                if token_id_retry:
                                    clob_ask, clob_bid = await self._fetch_clob_best_prices(token_id_retry)
                                    logger.info("CLOB retry: ask=%.2f для %s", clob_ask, direction)
                            if clob_ask <= 0:
                                logger.info("SKIP: CLOB ask недоступний після retry — не використовуємо Gamma як fallback")
                                continue
                            if not (th["CONTRACT_PRICE_MIN"] <= clob_ask <= th["CONTRACT_PRICE_MAX"]):
                                logger.info(
                                    "SKIP CLOB ask %.2f поза зоною [%.2f–%.2f]",
                                    clob_ask, th["CONTRACT_PRICE_MIN"], th["CONTRACT_PRICE_MAX"],
                                )
                                continue

                            # spread gate
                            if clob_bid > 0:
                                spread = clob_ask - clob_bid
                                if spread > CLOB_SPREAD_MAX:
                                    logger.info(
                                        "SKIP CLOB spread %.3f > %.3f",
                                        spread, CLOB_SPREAD_MAX,
                                    )
                                    continue

                            # high-price GAP gate
                            if clob_ask > th["CONTRACT_PRICE_HIGH_MIN"]:
                                gap = signal.get("gap", 0)
                                if abs(gap) < th["GAP_STRICT_USD"]:
                                    logger.info(
                                        "SKIP CLOB ask %.2f > HIGH_MIN %.2f but GAP %.1f < %.0f",
                                        clob_ask, th["CONTRACT_PRICE_HIGH_MIN"], gap, th["GAP_STRICT_USD"],
                                    )
                                    continue

                            signal["clob_ask"] = clob_ask
                            signal["clob_bid"] = clob_bid

                        # ── MTF RSI filter ──
                        if th["MTF_RSI_FILTER_ENABLED"] and state.mode != "test":
                            rsi_3m = signal.get("rsi_3m")
                            rsi_5m = signal.get("rsi_5m")
                            if rsi_3m is not None and rsi_5m is not None:
                                if direction == "UP" and not (rsi_3m > 50 and rsi_5m > 50):
                                    logger.debug(
                                        "MTF RSI UP fail: rsi_3m=%.1f rsi_5m=%.1f — skip",
                                        rsi_3m, rsi_5m,
                                    )
                                    continue
                                if direction == "DOWN" and not (rsi_3m < 50 and rsi_5m < 50):
                                    logger.debug(
                                        "MTF RSI DOWN fail: rsi_3m=%.1f rsi_5m=%.1f — skip",
                                        rsi_3m, rsi_5m,
                                    )
                                    continue

                        # ── OBI filter ──
                        obi = 1.0
                        if state.mode != "test":
                            obi = await self.exchange.get_order_book_imbalance(
                                levels=th["OBI_LEVELS"]
                            )
                            obi_ratio = th["OBI_MIN_RATIO"]
                            if obi_ratio <= 0:
                                obi_pass = True
                            else:
                                obi_pass = (
                                    obi >= obi_ratio
                                    if direction == "UP"
                                    else obi <= (1.0 / obi_ratio)
                                )
                            if not obi_pass:
                                logger.debug(
                                    "OBI %.3f не відповідає напрямку %s (threshold=%.2f) — skip",
                                    obi, direction, obi_ratio,
                                )
                                continue
                        signal["obi"] = obi

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
                            signal["token_yes_id"] = market_prices.get("token_yes_id") or ""
                            signal["token_no_id"] = market_prices.get("token_no_id") or ""
                            signal["neg_risk"] = bool(
                                market_prices.get("neg_risk", False)
                            )
                            signal["market_title"] = (
                                market_prices.get("title")
                                or market_prices.get("question")
                                or ""
                            )

                            # Зберігаємо реальну CLOB ціну в contract_price
                            if signal.get("clob_ask"):
                                signal["contract_price"] = signal["clob_ask"]

                            # ── Збір додаткових даних (тільки для статистики) ──
                            try:
                                signal["funding_rate"] = await self.exchange.get_funding_rate()
                                # ADX з df_with_indicators (порахований в add_indicators)
                                if "adx" in df_with_indicators.columns:
                                    signal["adx"] = round(float(df_with_indicators["adx"].iloc[-1]), 2)

                                # taker_ratio
                                try:
                                    df_3m = await self.exchange.get_btc_candles("3m", 50)
                                    if not df_3m.empty:
                                        taker_vol = df_3m["taker_buy_base"].iloc[-5:].sum()
                                        total_vol = df_3m["volume"].iloc[-5:].sum()
                                        signal["taker_ratio"] = round(float(taker_vol / total_vol), 4) if total_vol > 0 else None
                                except Exception:
                                    pass
                            except Exception as e:
                                logger.debug("Extra data collection error (non-critical): %s", e)

                            # Route to all matching strategies
                            await route_signal(
                                signal, self, self.execution_clients,
                                self._route_cooldowns, COOLDOWN_SECONDS,
                            )

                            # Price snapshots для SL аналізу
                            token_id_snap = market_prices.get("token_yes_id") if direction == "UP" else market_prices.get("token_no_id")
                            if token_id_snap:
                                # Find latest signal_id from DB for snapshots
                                try:
                                    import sqlite3 as _sq
                                    _c = _sq.connect(get_db_path())
                                    _last_id = _c.execute("SELECT MAX(id) FROM signals").fetchone()[0]
                                    _c.close()
                                    if _last_id:
                                        asyncio.create_task(self._schedule_price_snapshots(_last_id, token_id_snap))
                                except Exception:
                                    pass

                            self.last_signal_time[key] = now
                            self.signal_counts[key] = count + 1
                            logger.info(f"✅ Сигнал {direction} для маркету {market_id}")

                # ── Resolve shadow signals (BTC напрямок після закриття вікна) ──
                try:
                    n = resolve_shadow_signals(df_with_indicators)
                    if n > 0:
                        logger.debug("Shadow resolved: %d", n)
                except Exception as e:
                    logger.debug("resolve_shadow_signals error: %s", e)

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

                        # Route ALT signal through strategy router
                        if sig.get("clob_ask") and sig.get("clob_ask") > 0:
                            alt_as_signal = {
                                **sig,
                                "asset": asset.upper(),
                                "market_slug": market.get("market_slug", ""),
                                "token_yes_id": market.get("token_yes_id", ""),
                                "token_no_id": market.get("token_no_id", ""),
                                "neg_risk": bool(market.get("neg_risk", False)),
                                "market_title": market.get("title") or market.get("question") or f"{asset} 15m",
                            }
                            await route_signal(
                                alt_as_signal, self, self.execution_clients,
                                self._route_cooldowns, COOLDOWN_SECONDS,
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
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    r = await client.get(
                        "https://clob.polymarket.com/book",
                        params={"token_id": token_id},
                    )
                    if r.status_code != 200:
                        raise ValueError(f"status {r.status_code}")
                    data = r.json()
                    asks = data.get("asks") or []
                    bids = data.get("bids") or []
                    best_ask = min(float(a["price"]) for a in asks) if asks else 0.0
                    best_bid = max(float(b["price"]) for b in bids) if bids else 0.0
                    return best_ask, best_bid
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(0.5)
                else:
                    logger.debug("_fetch_clob_best_prices(%s) failed після 3 спроб: %s", token_id[:12], e)
        return 0.0, 0.0

    async def _schedule_price_snapshots(self, signal_id: int, token_id: str) -> None:
        """Записує ціну контракту і BTC через 1/2/3/5/10 хв після сигналу."""
        from bot.storage import save_signal_snapshot
        logger.info("📸 Snapshot task started for #%s", signal_id)
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
                logger.info("📸 Snapshot #%s +%dmin: cp=%s btc=%s", signal_id, minutes, contract_price, btc_now)
            except Exception as e:
                logger.warning("Snapshot error #%s +%dmin: %s", signal_id, minutes, e)

    async def close(self):
        await self.exchange.close()
        await self.poly.close()
