import httpx
import pandas as pd
import logging

logger = logging.getLogger(__name__)

class ExchangeClient:
    """Клієнт для роботи з Binance API."""
    
    BASE_URL = "https://api.binance.com/api/v3"

    def __init__(self):
        self.client = httpx.AsyncClient(base_url=self.BASE_URL)

    async def get_btc_1m_candles(self, limit: int = 100) -> pd.DataFrame:
        """
        Отримує останні 1-хвилинні свічки для BTCUSDT.
        Повертає pandas DataFrame з колонками: timestamp, open, high, low, close, volume.
        """
        params = {
            "symbol": "BTCUSDT",
            "interval": "1m",
            "limit": limit
        }
        try:
            response = await self.client.get("/klines", params=params)
            response.raise_for_status()
            data = response.json()
            
            # Формат Binance klines: 
            # [0: Open time, 1: Open, 2: High, 3: Low, 4: Close, 5: Volume, ...]
            df = pd.DataFrame(data, columns=[
                "timestamp", "open", "high", "low", "close", "volume", 
                "close_time", "quote_asset_volume", "number_of_trades",
                "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore"
            ])
            
            # Залишаємо тільки потрібні колонки та приводимо до правильних типів
            df = df[["timestamp", "open", "high", "low", "close", "volume", "taker_buy_base_asset_volume"]]
            for col in ["open", "high", "low", "close", "volume", "taker_buy_base_asset_volume"]:
                df[col] = df[col].astype(float)
                
            # Перетворюємо timestamp в datetime об'єкти
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            
            return df
        except Exception as e:
            logger.error(f"Помилка отримання свічок з Binance: {e}")
            return pd.DataFrame() # Порожній датафрейм у разі помилки

    async def get_order_book_imbalance(
        self, symbol: str = "BTCUSDT", levels: int = 20
    ) -> float:
        """
        OBI = bid_volume / ask_volume з Binance стакану (top N levels).

        > 1.0 — переважають покупці (bullish)
        < 1.0 — переважають продавці (bearish)
        = 1.0 — neutral (fallback при помилці)
        """
        try:
            r = await self.client.get(
                "/depth", params={"symbol": symbol, "limit": levels}
            )
            r.raise_for_status()
            data = r.json()
            bid_vol = sum(float(b[1]) for b in data.get("bids", []))
            ask_vol = sum(float(a[1]) for a in data.get("asks", []))
            if ask_vol <= 0:
                return 1.0
            obi = round(bid_vol / ask_vol, 3)
            logger.debug("OBI %s: bid=%.2f ask=%.2f → %.3f", symbol, bid_vol, ask_vol, obi)
            return obi
        except Exception as e:
            logger.warning("OBI Binance error: %s", e)
            return 1.0

    async def close(self):
        await self.client.aclose()
