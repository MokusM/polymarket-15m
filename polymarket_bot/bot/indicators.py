import pandas as pd
import numpy as np
from bot.config import (
    RSI_PERIOD,
    EMA_SHORT_PERIOD,
    EMA_LONG_PERIOD,
    EMA_SLOPE_LOOKBACK_BARS,
    MACD_FAST,
    MACD_SLOW,
    MACD_SIGNAL,
    PIVOT_HL_PERIOD,
    ATR_PERIOD,
    VOLUME_AVG_PERIOD,
    VOLUME_SPIKE_MULTIPLIER,
    ATR_ZONE_DEAD,
    ATR_ZONE_QUIET,
    ATR_ZONE_GOLDEN,
    ATR_ZONE_HIGH,
)


def calculate_ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def calculate_rsi(series: pd.Series, length: int) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(window=length).mean()


def calculate_macd(
    series: pd.Series, fast: int, slow: int, signal: int
) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = calculate_ema(series, fast)
    ema_slow = calculate_ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = calculate_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calculate_vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    cum_tp_vol = (typical * df["volume"]).cumsum()
    cum_vol = df["volume"].cumsum()
    return cum_tp_vol / cum_vol


def classify_atr_zone(atr: float) -> str:
    if pd.isna(atr) or atr < ATR_ZONE_DEAD:
        return "dead"
    if atr < ATR_ZONE_QUIET:
        return "quiet"
    if atr < ATR_ZONE_GOLDEN:
        return "golden"
    if atr < ATR_ZONE_HIGH:
        return "high"
    return "extreme"


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Додає до DataFrame з 1m свічками повний набір індикаторів курсу:
    RSI, EMA 9/21, MACD(12/26/9), VWAP, Pivots HL(10), ATR + zone, Volume state.
    """
    if df.empty or len(df) < 30:
        return df

    df = df.set_index("timestamp")

    # EMA 9 / 21
    df["ema_9"] = calculate_ema(df["close"], EMA_SHORT_PERIOD)
    df["ema_21"] = calculate_ema(df["close"], EMA_LONG_PERIOD)
    df["ema_9_slope"] = df["ema_9"].diff(periods=EMA_SLOPE_LOOKBACK_BARS)

    # RSI (14, 1m)
    df["rsi_1m"] = calculate_rsi(df["close"], RSI_PERIOD)

    # MACD (12, 26, 9)
    df["macd_line"], df["macd_signal"], df["macd_hist"] = calculate_macd(
        df["close"], MACD_FAST, MACD_SLOW, MACD_SIGNAL
    )

    # VWAP (rolling from start of candle window)
    df["vwap"] = calculate_vwap(df)

    # Pivots HL — 10-bar high/low shifted by 1 (breakout = price crosses previous channel)
    df["pivot_high"] = df["high"].rolling(PIVOT_HL_PERIOD).max().shift(1)
    df["pivot_low"] = df["low"].rolling(PIVOT_HL_PERIOD).min().shift(1)

    # ATR (14)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - df["close"].shift(1)).abs(),
            (df["low"] - df["close"].shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()
    df["atr_zone"] = df["atr"].apply(classify_atr_zone)

    # 1h context
    df["chg_1h"] = df["close"].pct_change(60) * 100

    # Volume state
    df["vol_sma"] = calculate_sma(df["volume"], VOLUME_AVG_PERIOD)
    conditions = [
        (df["volume"] > df["vol_sma"] * VOLUME_SPIKE_MULTIPLIER),
        (df["volume"] < df["vol_sma"] * 0.5),
    ]
    choices = ["spike", "stabilization"]
    df["volume_state"] = np.select(conditions, choices, default="normal")

    # Consecutive closes in same direction
    _diff = df["close"].diff()
    _dir = _diff.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    _cc, _count, _prev = [], 0, 0
    for _d in _dir:
        if _d != 0 and _d == _prev:
            _count += 1
        elif _d != 0:
            _count = 1
        else:
            _count = 0
        _cc.append(_count)
        _prev = _d if _d != 0 else _prev
    df["consecutive_closes"] = _cc

    # MTF RSI: resample 1m → 3m/5m, forward-fill back to 1m resolution
    for tf_min, col in [(3, "rsi_3m"), (5, "rsi_5m")]:
        try:
            close_tf = df["close"].resample(f"{tf_min}min").last().dropna()
            if len(close_tf) >= RSI_PERIOD:
                rsi_tf = calculate_rsi(close_tf, RSI_PERIOD)
                df[col] = rsi_tf.reindex(df.index, method="ffill")
            else:
                df[col] = float("nan")
        except Exception:
            df[col] = float("nan")

    return df.reset_index()
