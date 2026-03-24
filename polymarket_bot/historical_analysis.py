"""
Аналіз історичних 15-хв BTC маркетів Polymarket за останні ~2 доби.
Витягує результати (Up/Down) і зіставляє з Binance 1m даними.
"""

import httpx
import pandas as pd
import time
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
GAMMA = "https://gamma-api.polymarket.com"
BINANCE = "https://api.binance.com/api/v3"
SLUG_PREFIX = "btc-updown-15m"

HOURS_BACK = 48


def generate_slugs(hours_back: int) -> list[tuple[int, str]]:
    now = datetime.now(ET)
    minute_floor = (now.minute // 15) * 15
    current = now.replace(minute=minute_floor, second=0, microsecond=0)
    start = current - timedelta(hours=hours_back)
    slugs = []
    t = start
    while t < current:
        unix = int(t.timestamp())
        slugs.append((unix, f"{SLUG_PREFIX}-{unix}"))
        t += timedelta(minutes=15)
    return slugs


def fetch_market(client: httpx.Client, slug: str) -> dict | None:
    try:
        r = client.get(f"{GAMMA}/markets/slug/{slug}", timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def parse_outcome(market: dict) -> str | None:
    import json as _json
    raw = market.get("outcomePrices", [])
    if isinstance(raw, str):
        try:
            raw = _json.loads(raw)
        except Exception:
            return None
    try:
        up = float(raw[0])
        down = float(raw[1])
    except (IndexError, ValueError, TypeError):
        return None
    if up == 1.0 and down == 0.0:
        return "UP"
    if up == 0.0 and down == 1.0:
        return "DOWN"
    return None


def fetch_binance_candles(start_ms: int, end_ms: int) -> pd.DataFrame:
    client = httpx.Client()
    all_data = []
    cursor = start_ms
    while cursor < end_ms:
        r = client.get(
            f"{BINANCE}/klines",
            params={"symbol": "BTCUSDT", "interval": "1m", "startTime": cursor, "limit": 1000},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        all_data.extend(data)
        cursor = data[-1][0] + 60_000
        time.sleep(0.2)
    client.close()
    if not all_data:
        return pd.DataFrame()
    df = pd.DataFrame(all_data, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "qav", "trades", "tbbav", "tbqav", "ignore",
    ])
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


def add_basic_indicators(df: pd.DataFrame) -> pd.DataFrame:
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
    rs = avg_gain / avg_loss
    df["rsi"] = 100 - (100 / (1 + rs))

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"] - df["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=14, adjust=False).mean()
    df["ema_9"] = df["close"].ewm(span=9, adjust=False).mean()
    return df


def main():
    print(f"Generating slugs for last {HOURS_BACK}h...")
    slugs = generate_slugs(HOURS_BACK)
    print(f"Total candidate slugs: {len(slugs)}")

    now_utc = datetime.now(timezone.utc)
    start_ms = int((now_utc - timedelta(hours=HOURS_BACK + 1)).timestamp() * 1000)
    end_ms = int(now_utc.timestamp() * 1000)

    print("Fetching Binance 1m candles...")
    candles = fetch_binance_candles(start_ms, end_ms)
    if candles.empty:
        print("ERROR: no Binance data")
        sys.exit(1)
    candles = add_basic_indicators(candles)
    print(f"  Got {len(candles)} candles")

    print("Fetching Polymarket markets...")
    client = httpx.Client()
    results = []
    found = 0
    for i, (unix_ts, slug) in enumerate(slugs):
        market = fetch_market(client, slug)
        if market:
            outcome = parse_outcome(market)
            if outcome:
                window_start = datetime.fromtimestamp(unix_ts, tz=ET).astimezone(timezone.utc)
                window_start_naive = window_start.replace(tzinfo=None)
                target_ts = pd.Timestamp(window_start_naive)

                mask = candles["timestamp"] == target_ts
                if mask.any():
                    row = candles[mask].iloc[0]
                else:
                    leq = candles[candles["timestamp"] <= target_ts]
                    if leq.empty:
                        continue
                    row = leq.iloc[-1]

                window_candles = candles[
                    (candles["timestamp"] >= target_ts)
                    & (candles["timestamp"] < target_ts + pd.Timedelta(minutes=15))
                ]
                if window_candles.empty:
                    continue

                open_price = float(row["open"])
                close_price = float(window_candles.iloc[-1]["close"])
                high = float(window_candles["high"].max())
                low = float(window_candles["low"].min())
                rsi_at_start = float(row["rsi"]) if not pd.isna(row["rsi"]) else 50
                atr_at_start = float(row["atr"]) if not pd.isna(row["atr"]) else 0
                price_vs_ema = float(row["close"]) - float(row["ema_9"])
                delta_pct = (close_price - open_price) / open_price * 100
                range_pct = (high - low) / open_price * 100

                hour_utc = window_start.hour

                results.append({
                    "unix": unix_ts,
                    "hour_utc": hour_utc,
                    "outcome": outcome,
                    "open": open_price,
                    "close": close_price,
                    "delta_pct": delta_pct,
                    "range_pct": range_pct,
                    "rsi": rsi_at_start,
                    "atr": atr_at_start,
                    "price_vs_ema9": price_vs_ema,
                })
                found += 1
        if (i + 1) % 20 == 0:
            print(f"  checked {i+1}/{len(slugs)}, found {found} resolved")
        time.sleep(0.15)
    client.close()

    if not results:
        print("No resolved markets found")
        sys.exit(1)

    df = pd.DataFrame(results)
    total = len(df)
    ups = (df["outcome"] == "UP").sum()
    downs = (df["outcome"] == "DOWN").sum()

    print(f"\n{'='*60}")
    print(f"RESOLVED MARKETS: {total} (UP: {ups}, DOWN: {downs})")
    print(f"UP rate: {ups/total*100:.1f}%")
    print(f"{'='*60}")

    print("\n--- BY HOUR (UTC) ---")
    hourly = df.groupby("hour_utc").agg(
        total=("outcome", "count"),
        up=("outcome", lambda x: (x == "UP").sum()),
    ).reset_index()
    hourly["up_pct"] = (hourly["up"] / hourly["total"] * 100).round(1)
    for _, r in hourly.iterrows():
        bar = "#" * int(r["up_pct"] / 5)
        print(f"  {int(r['hour_utc']):02d}:00 | {int(r['total']):>3} mkts | UP {r['up_pct']:>5.1f}% | {bar}")

    print("\n--- BY ATR BUCKET ---")
    df["atr_bucket"] = pd.cut(df["atr"], bins=[0, 20, 35, 50, 80, 500], labels=["<20", "20-35", "35-50", "50-80", ">80"])
    atr_g = df.groupby("atr_bucket", observed=True).agg(
        total=("outcome", "count"),
        up=("outcome", lambda x: (x == "UP").sum()),
    ).reset_index()
    atr_g["up_pct"] = (atr_g["up"] / atr_g["total"] * 100).round(1)
    for _, r in atr_g.iterrows():
        print(f"  ATR {r['atr_bucket']:>5} | {int(r['total']):>3} mkts | UP {r['up_pct']:>5.1f}%")

    print("\n--- BY RSI BUCKET (at window start) ---")
    df["rsi_bucket"] = pd.cut(df["rsi"], bins=[0, 30, 40, 50, 60, 70, 100], labels=["<30", "30-40", "40-50", "50-60", "60-70", ">70"])
    rsi_g = df.groupby("rsi_bucket", observed=True).agg(
        total=("outcome", "count"),
        up=("outcome", lambda x: (x == "UP").sum()),
    ).reset_index()
    rsi_g["up_pct"] = (rsi_g["up"] / rsi_g["total"] * 100).round(1)
    for _, r in rsi_g.iterrows():
        print(f"  RSI {r['rsi_bucket']:>5} | {int(r['total']):>3} mkts | UP {r['up_pct']:>5.1f}%")

    print("\n--- BY PRICE VS EMA9 ---")
    df["ema_side"] = df["price_vs_ema9"].apply(lambda x: "below" if x < 0 else "above")
    ema_g = df.groupby("ema_side").agg(
        total=("outcome", "count"),
        up=("outcome", lambda x: (x == "UP").sum()),
    ).reset_index()
    ema_g["up_pct"] = (ema_g["up"] / ema_g["total"] * 100).round(1)
    for _, r in ema_g.iterrows():
        print(f"  Price {r['ema_side']:>5} EMA9 | {int(r['total']):>3} mkts | UP {r['up_pct']:>5.1f}%")

    print("\n--- BY RANGE (volatility within 15m window) ---")
    df["range_bucket"] = pd.cut(df["range_pct"], bins=[0, 0.05, 0.10, 0.20, 0.40, 100], labels=["<0.05%", "0.05-0.10%", "0.10-0.20%", "0.20-0.40%", ">0.40%"])
    range_g = df.groupby("range_bucket", observed=True).agg(
        total=("outcome", "count"),
        up=("outcome", lambda x: (x == "UP").sum()),
    ).reset_index()
    range_g["up_pct"] = (range_g["up"] / range_g["total"] * 100).round(1)
    for _, r in range_g.iterrows():
        print(f"  Range {r['range_bucket']:>10} | {int(r['total']):>3} mkts | UP {r['up_pct']:>5.1f}%")

    print("\n--- CONSECUTIVE STREAKS ---")
    outcomes = df.sort_values("unix")["outcome"].tolist()
    streak_str = "".join("U" if o == "UP" else "D" for o in outcomes)
    max_u = max((len(s) for s in streak_str.split("D") if s), default=0)
    max_d = max((len(s) for s in streak_str.split("U") if s), default=0)
    print(f"  Max consecutive UP:   {max_u}")
    print(f"  Max consecutive DOWN: {max_d}")
    last_20 = streak_str[-40:]
    print(f"  Last 40: ...{last_20}")

    print("\n--- KEY INSIGHT: RSI extreme + outcome ---")
    rsi_low = df[df["rsi"] < 35]
    rsi_high = df[df["rsi"] > 65]
    if len(rsi_low) > 0:
        up_rate = (rsi_low["outcome"] == "UP").mean() * 100
        print(f"  RSI < 35 (oversold):  {len(rsi_low)} windows, UP rate: {up_rate:.1f}%")
    if len(rsi_high) > 0:
        up_rate = (rsi_high["outcome"] == "UP").mean() * 100
        print(f"  RSI > 65 (overbought): {len(rsi_high)} windows, UP rate: {up_rate:.1f}%")

    rsi_mid = df[(df["rsi"] >= 45) & (df["rsi"] <= 55)]
    if len(rsi_mid) > 0:
        up_rate = (rsi_mid["outcome"] == "UP").mean() * 100
        print(f"  RSI 45-55 (neutral):  {len(rsi_mid)} windows, UP rate: {up_rate:.1f}%")


if __name__ == "__main__":
    main()
