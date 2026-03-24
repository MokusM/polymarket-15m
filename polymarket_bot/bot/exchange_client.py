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
            df = df[["timestamp", "open", "high", "low", "close", "volume"]]
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
                
            # Перетворюємо timestamp в datetime об'єкти
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            
            return df
        except Exception as e:
            logger.error(f"Помилка отримання свічок з Binance: {e}")
            return pd.DataFrame() # Порожній датафрейм у разі помилки

    async def close(self):
        await self.client.aclose()
