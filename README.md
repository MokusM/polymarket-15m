# Polymarket BTC 15m Scanner Bot

Async Python bot that monitors **"Bitcoin Up or Down — 15 Minutes"** markets on Polymarket.

Fetches 1m BTCUSDT candles from Binance, calculates 5 technical indicators (RSI, MACD, VWAP, EMA 9/21, Pivots HL), generates UP/DOWN signals via confluence model, and sends Telegram alerts with Approve/Reject/Skip buttons.

Includes paper-trading with PnL settlement and automatic result updates in Telegram messages.

## Setup

```bash
cd polymarket_bot
pip install -r requirements.txt
cp .env.example .env
# fill in TELEGRAM_TOKEN and CHAT_ID in .env
python main.py
```

## Telegram Commands

- `/mode` — switch between Light / Medium / Strict / Test modes
