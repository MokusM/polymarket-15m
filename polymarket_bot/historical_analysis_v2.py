"""
Extended analysis: 7 days of BTC 15m markets.
Classifies BTC regime (range / trend / high-vol) and shows stats per regime.
"""

import httpx
import pandas as pd
import numpy as np
import time
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
GAMMA = "https://gamma-api.polymarket.com"
BINANCE = "https://api.binance.com/api/v3"
SLUG_PREFIX = "btc-updown-15m"

HOURS_BACK = 168  # 7 days


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


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
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
    df["ema_21"] = df["close"].ewm(span=21, adjust=False).mean()

    # 1h trend: change over last 60 candles
    df["chg_1h"] = df["close"].pct_change(60) * 100
    # 4h trend
    df["chg_4h"] = df["close"].pct_change(240) * 100
    # rolling 1h volatility (std of returns)
    df["vol_1h"] = df["close"].pct_change().rolling(60).std() * 100
    return df


def classify_regime(row) -> str:
    chg = abs(row.get("chg_1h", 0))
    vol = row.get("vol_1h", 0)
    if pd.isna(chg) or pd.isna(vol):
        return "unknown"
    if vol < 0.03:
        return "dead"        # barely moving
    if chg < 0.15 and vol < 0.06:
        return "range"       # sideways chop
    if chg >= 0.4:
        return "strong_trend"
    if chg >= 0.15:
        return "mild_trend"
    return "range"


def print_section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def analyze_group(df: pd.DataFrame, label: str):
    total = len(df)
    if total == 0:
        print(f"  {label}: no data")
        return
    ups = (df["outcome"] == "UP").sum()
    up_pct = ups / total * 100
    print(f"  {label:>14} | {total:>4} mkts | UP {up_pct:>5.1f}% | DOWN {100-up_pct:>5.1f}%")


def main():
    print(f"Generating slugs for last {HOURS_BACK}h ({HOURS_BACK//24} days)...")
    slugs = generate_slugs(HOURS_BACK)
    print(f"Total candidate slugs: {len(slugs)}")

    now_utc = datetime.now(timezone.utc)
    start_ms = int((now_utc - timedelta(hours=HOURS_BACK + 1)).timestamp() * 1000)
    end_ms = int(now_utc.timestamp() * 1000)

    print("Fetching Binance 1m candles (this takes ~1 min)...")
    candles = fetch_binance_candles(start_ms, end_ms)
    if candles.empty:
        print("ERROR: no Binance data")
        sys.exit(1)
    candles = add_indicators(candles)
    print(f"  Got {len(candles)} candles ({len(candles)/60:.0f}h)")

    print(f"Fetching Polymarket markets (~{len(slugs)*0.15:.0f}s)...")
    client = httpx.Client()
    results = []
    found = 0
    for i, (unix_ts, slug) in enumerate(slugs):
        market = fetch_market(client, slug)
        if market:
            outcome = parse_outcome(market)
            if outcome:
                window_start = datetime.fromtimestamp(unix_ts, tz=ET).astimezone(timezone.utc)
                ws_naive = window_start.replace(tzinfo=None)
                target_ts = pd.Timestamp(ws_naive)

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
                ema9_vs_ema21 = float(row["ema_9"]) - float(row["ema_21"])
                delta_pct = (close_price - open_price) / open_price * 100
                range_pct = (high - low) / open_price * 100

                regime = classify_regime(row)

                results.append({
                    "unix": unix_ts,
                    "hour_utc": window_start.hour,
                    "outcome": outcome,
                    "open": open_price,
                    "close": close_price,
                    "delta_pct": delta_pct,
                    "range_pct": range_pct,
                    "rsi": rsi_at_start,
                    "atr": atr_at_start,
                    "price_vs_ema9": price_vs_ema,
                    "ema9_vs_ema21": ema9_vs_ema21,
                    "chg_1h": float(row["chg_1h"]) if not pd.isna(row["chg_1h"]) else 0,
                    "chg_4h": float(row["chg_4h"]) if not pd.isna(row["chg_4h"]) else 0,
                    "vol_1h": float(row["vol_1h"]) if not pd.isna(row["vol_1h"]) else 0,
                    "regime": regime,
                })
                found += 1
        if (i + 1) % 40 == 0:
            print(f"  checked {i+1}/{len(slugs)}, found {found} resolved")
        time.sleep(0.12)
    client.close()

    if not results:
        print("No resolved markets found")
        sys.exit(1)

    df = pd.DataFrame(results)
    total = len(df)
    ups = (df["outcome"] == "UP").sum()
    downs = total - ups

    print_section(f"TOTAL: {total} markets | UP {ups} ({ups/total*100:.1f}%) | DOWN {downs} ({downs/total*100:.1f}%)")

    # ---- REGIME ----
    print_section("BY REGIME (BTC market condition)")
    for regime in ["dead", "range", "mild_trend", "strong_trend", "unknown"]:
        sub = df[df["regime"] == regime]
        analyze_group(sub, regime)

    # ---- REGIME x RSI ----
    print_section("RSI EXTREME + REGIME")
    for regime in ["range", "mild_trend", "strong_trend"]:
        sub = df[df["regime"] == regime]
        if len(sub) < 5:
            continue
        print(f"\n  -- {regime.upper()} --")
        for label, cond in [
            ("RSI < 30", sub["rsi"] < 30),
            ("RSI 30-40", (sub["rsi"] >= 30) & (sub["rsi"] < 40)),
            ("RSI 40-50", (sub["rsi"] >= 40) & (sub["rsi"] < 50)),
            ("RSI 50-60", (sub["rsi"] >= 50) & (sub["rsi"] < 60)),
            ("RSI 60-70", (sub["rsi"] >= 60) & (sub["rsi"] < 70)),
            ("RSI > 70", sub["rsi"] >= 70),
        ]:
            g = sub[cond]
            if len(g) >= 3:
                up_r = (g["outcome"] == "UP").mean() * 100
                print(f"    {label:>10} | {len(g):>3} mkts | UP {up_r:>5.1f}%")

    # ---- REGIME x EMA ----
    print_section("PRICE vs EMA9 + REGIME")
    for regime in ["range", "mild_trend", "strong_trend"]:
        sub = df[df["regime"] == regime]
        if len(sub) < 5:
            continue
        print(f"\n  -- {regime.upper()} --")
        above = sub[sub["price_vs_ema9"] >= 0]
        below = sub[sub["price_vs_ema9"] < 0]
        if len(above) >= 3:
            up_r = (above["outcome"] == "UP").mean() * 100
            print(f"    above EMA9 | {len(above):>3} mkts | UP {up_r:>5.1f}%")
        if len(below) >= 3:
            up_r = (below["outcome"] == "UP").mean() * 100
            print(f"    below EMA9 | {len(below):>3} mkts | UP {up_r:>5.1f}%")

    # ---- EMA9 vs EMA21 (trend direction) ----
    print_section("EMA9 vs EMA21 (broader trend)")
    bullish = df[df["ema9_vs_ema21"] > 0]
    bearish = df[df["ema9_vs_ema21"] <= 0]
    analyze_group(bullish, "EMA9 > EMA21")
    analyze_group(bearish, "EMA9 < EMA21")

    # ---- 1H CHANGE direction + outcome ----
    print_section("1H BTC CHANGE + OUTCOME")
    for label, cond in [
        ("fell > 0.5%", df["chg_1h"] < -0.5),
        ("fell 0.2-0.5%", (df["chg_1h"] >= -0.5) & (df["chg_1h"] < -0.2)),
        ("flat -0.2..+0.2%", (df["chg_1h"] >= -0.2) & (df["chg_1h"] <= 0.2)),
        ("rose 0.2-0.5%", (df["chg_1h"] > 0.2) & (df["chg_1h"] <= 0.5)),
        ("rose > 0.5%", df["chg_1h"] > 0.5),
    ]:
        g = df[cond]
        if len(g) >= 3:
            up_r = (g["outcome"] == "UP").mean() * 100
            print(f"  {label:>16} | {len(g):>3} mkts | UP {up_r:>5.1f}%")

    # ---- 4H CHANGE ----
    print_section("4H BTC CHANGE + OUTCOME")
    for label, cond in [
        ("fell > 1%", df["chg_4h"] < -1),
        ("fell 0.3-1%", (df["chg_4h"] >= -1) & (df["chg_4h"] < -0.3)),
        ("flat", (df["chg_4h"] >= -0.3) & (df["chg_4h"] <= 0.3)),
        ("rose 0.3-1%", (df["chg_4h"] > 0.3) & (df["chg_4h"] <= 1)),
        ("rose > 1%", df["chg_4h"] > 1),
    ]:
        g = df[cond]
        if len(g) >= 3:
            up_r = (g["outcome"] == "UP").mean() * 100
            print(f"  {label:>14} | {len(g):>3} mkts | UP {up_r:>5.1f}%")

    # ---- RSI global ----
    print_section("RSI AT WINDOW START (global)")
    for label, cond in [
        ("<30", df["rsi"] < 30),
        ("30-40", (df["rsi"] >= 30) & (df["rsi"] < 40)),
        ("40-50", (df["rsi"] >= 40) & (df["rsi"] < 50)),
        ("50-60", (df["rsi"] >= 50) & (df["rsi"] < 60)),
        ("60-70", (df["rsi"] >= 60) & (df["rsi"] < 70)),
        (">70", df["rsi"] >= 70),
    ]:
        g = df[cond]
        if len(g) >= 3:
            up_r = (g["outcome"] == "UP").mean() * 100
            print(f"  RSI {label:>5} | {len(g):>3} mkts | UP {up_r:>5.1f}%")

    # ---- ATR ----
    print_section("ATR AT WINDOW START")
    df["atr_bucket"] = pd.cut(df["atr"], bins=[0, 15, 25, 40, 60, 100, 500],
                              labels=["<15", "15-25", "25-40", "40-60", "60-100", ">100"])
    for bucket in ["<15", "15-25", "25-40", "40-60", "60-100", ">100"]:
        g = df[df["atr_bucket"] == bucket]
        if len(g) >= 3:
            up_r = (g["outcome"] == "UP").mean() * 100
            print(f"  ATR {bucket:>6} | {len(g):>3} mkts | UP {up_r:>5.1f}%")

    # ---- HOUR ----
    print_section("BY HOUR UTC (all 7 days)")
    hourly = df.groupby("hour_utc").agg(
        total=("outcome", "count"),
        up=("outcome", lambda x: (x == "UP").sum()),
    ).reset_index()
    hourly["up_pct"] = (hourly["up"] / hourly["total"] * 100).round(1)
    for _, r in hourly.iterrows():
        bar = "#" * int(r["up_pct"] / 3)
        print(f"  {int(r['hour_utc']):02d}:00 | {int(r['total']):>3} mkts | UP {r['up_pct']:>5.1f}% | {bar}")

    # ---- COMBINED: best signals ----
    print_section("COMBINED: potential edge signals")

    # Momentum: price above EMA9 + RSI 50-70 + mild/strong trend
    combo1 = df[
        (df["price_vs_ema9"] > 0)
        & (df["rsi"] >= 50) & (df["rsi"] < 70)
        & (df["regime"].isin(["mild_trend", "strong_trend"]))
    ]
    if len(combo1) >= 3:
        up_r = (combo1["outcome"] == "UP").mean() * 100
        print(f"  Momentum UP (above EMA9, RSI 50-70, trend) | {len(combo1):>3} mkts | UP {up_r:>5.1f}%")

    # Momentum DOWN: price below EMA9 + RSI 30-50 + trend
    combo2 = df[
        (df["price_vs_ema9"] < 0)
        & (df["rsi"] >= 30) & (df["rsi"] < 50)
        & (df["regime"].isin(["mild_trend", "strong_trend"]))
    ]
    if len(combo2) >= 3:
        up_r = (combo2["outcome"] == "UP").mean() * 100
        print(f"  Momentum DN (below EMA9, RSI 30-50, trend) | {len(combo2):>3} mkts | UP {up_r:>5.1f}% (DOWN {100-up_r:.1f}%)")

    # Mean-rev: RSI > 70 (any regime)
    combo3 = df[df["rsi"] >= 70]
    if len(combo3) >= 3:
        up_r = (combo3["outcome"] == "UP").mean() * 100
        print(f"  Mean-rev DOWN (RSI >= 70)                  | {len(combo3):>3} mkts | UP {up_r:>5.1f}% (DOWN {100-up_r:.1f}%)")

    # Mean-rev: RSI < 30 (any regime)
    combo4 = df[df["rsi"] < 30]
    if len(combo4) >= 3:
        up_r = (combo4["outcome"] == "UP").mean() * 100
        print(f"  Mean-rev UP (RSI < 30)                     | {len(combo4):>3} mkts | UP {up_r:>5.1f}%")

    # Range: flat 1h + RSI extreme
    combo5 = df[
        (df["regime"] == "range")
        & ((df["rsi"] < 35) | (df["rsi"] > 65))
    ]
    if len(combo5) >= 3:
        up_r = (combo5["outcome"] == "UP").mean() * 100
        print(f"  Range + RSI extreme (<35 or >65)           | {len(combo5):>3} mkts | UP {up_r:>5.1f}%")

    # EMA golden cross: ema9 > ema21 + price above ema9
    combo6 = df[(df["ema9_vs_ema21"] > 0) & (df["price_vs_ema9"] > 0)]
    if len(combo6) >= 3:
        up_r = (combo6["outcome"] == "UP").mean() * 100
        print(f"  Bullish alignment (EMA9>21, price>EMA9)    | {len(combo6):>3} mkts | UP {up_r:>5.1f}%")

    combo7 = df[(df["ema9_vs_ema21"] < 0) & (df["price_vs_ema9"] < 0)]
    if len(combo7) >= 3:
        up_r = (combo7["outcome"] == "UP").mean() * 100
        print(f"  Bearish alignment (EMA9<21, price<EMA9)    | {len(combo7):>3} mkts | UP {up_r:>5.1f}% (DOWN {100-up_r:.1f}%)")

    print(f"\n  Total markets analyzed: {total}")
    print(f"  Period: {df['unix'].min()} - {df['unix'].max()}")
    d0 = datetime.fromtimestamp(df["unix"].min(), tz=timezone.utc)
    d1 = datetime.fromtimestamp(df["unix"].max(), tz=timezone.utc)
    print(f"  Dates: {d0:%Y-%m-%d %H:%M} -- {d1:%Y-%m-%d %H:%M} UTC")


if __name__ == "__main__":
    main()
