"""
Signal generation v3: 5-indicator confluence model (per PolySigma course).

Indicators (кожен голосує UP / DOWN / NEUTRAL):
  1. RSI (14, 1m)       — oversold < 35 → UP, overbought > 65 → DOWN
  2. MACD (12/26/9)     — bullish crossover → UP, bearish → DOWN
  3. VWAP               — price above → UP, below → DOWN
  4. EMA 9/21           — golden cross → UP, death cross → DOWN
  5. Pivots HL (10)     — breakout above → UP, breakdown below → DOWN

Signal = мінімум MIN_CONFLUENCE (3/5) індикаторів в одному напрямку.
Фільтри: ATR zone, contract price 50-72¢, time left.
"""

import html as _html
import pandas as pd
from datetime import datetime, timezone
import logging
from typing import Optional

from bot.config import IGNORE_IF_TIME_LEFT_LT_MIN, IGNORE_IF_CONTRACT_PRICE_GT, ATR_MIN_USD, GAP_MIN_USD, GAP_STRICT_USD, TIME_STRICT_MAX_MIN
from bot.state import state

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Individual indicator votes → ("UP"/"DOWN"/None, human-readable label)
# ---------------------------------------------------------------------------

def _vote_rsi(rsi: float) -> tuple[Optional[str], str]:
    if pd.isna(rsi):
        return None, "n/a"
    if rsi < 35:
        return "UP", f"RSI {rsi:.1f} oversold"
    if rsi > 65:
        return "DOWN", f"RSI {rsi:.1f} overbought"
    return None, f"RSI {rsi:.1f} neutral"


def _vote_macd(macd_line: float, macd_signal: float, macd_hist: float) -> tuple[Optional[str], str]:
    if any(pd.isna(v) for v in (macd_line, macd_signal, macd_hist)):
        return None, "n/a"
    if macd_line > macd_signal and macd_hist > 0:
        return "UP", "bullish crossover"
    if macd_line < macd_signal and macd_hist < 0:
        return "DOWN", "bearish crossover"
    return None, "mixed"


def _vote_vwap(price: float, vwap: float) -> tuple[Optional[str], str]:
    if pd.isna(vwap) or vwap == 0:
        return None, "n/a"
    diff_pct = ((price - vwap) / vwap) * 100
    if price > vwap:
        return "UP", f"above VWAP (+{diff_pct:.2f}%)"
    if price < vwap:
        return "DOWN", f"below VWAP ({diff_pct:.2f}%)"
    return None, "at VWAP"


def _vote_ema(ema_9: float, ema_21: float) -> tuple[Optional[str], str]:
    if any(pd.isna(v) for v in (ema_9, ema_21)):
        return None, "n/a"
    diff = ema_9 - ema_21
    if diff > 0:
        return "UP", f"EMA9 > EMA21 (+{diff:.1f})"
    if diff < 0:
        return "DOWN", f"EMA9 < EMA21 ({diff:.1f})"
    return None, "EMA9 = EMA21"


def _vote_pivots(price: float, pivot_high: float, pivot_low: float) -> tuple[Optional[str], str]:
    if any(pd.isna(v) for v in (pivot_high, pivot_low)):
        return None, "n/a"
    if price >= pivot_high:
        return "UP", f"breakout (>{pivot_high:.0f})"
    if price <= pivot_low:
        return "DOWN", f"breakdown (<{pivot_low:.0f})"
    return None, f"in range ({pivot_low:.0f}–{pivot_high:.0f})"


# ---------------------------------------------------------------------------
#  Start price alignment
# ---------------------------------------------------------------------------

def _binance_open_at_polymarket_window_start(
    df: pd.DataFrame, event_start_iso: Optional[str]
) -> Optional[float]:
    if not event_start_iso or df.empty or "timestamp" not in df.columns:
        return None
    try:
        dt = datetime.fromisoformat(event_start_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts_naive = dt.astimezone(timezone.utc).replace(tzinfo=None)
        target = pd.Timestamp(ts_naive)
    except (ValueError, TypeError) as e:
        logger.debug("event_start_time parse: %s", e)
        return None

    ts_col = df["timestamp"]
    exact = df[ts_col == target]
    if not exact.empty:
        return float(exact.iloc[0]["open"])
    leq = df[ts_col <= target].sort_values("timestamp")
    if not leq.empty:
        return float(leq.iloc[-1]["open"])
    return None


# ---------------------------------------------------------------------------
#  Main signal check
# ---------------------------------------------------------------------------

def check_signals(market_info: dict, df: pd.DataFrame) -> dict | None:
    if df.empty or len(df) < 30:
        return None

    last = df.iloc[-1]
    price = float(last.get("close", 0))
    th = state.get_thresholds()

    # --- ATR zone filter ---
    atr = last.get("atr", 0)
    atr_zone = last.get("atr_zone", "dead")
    atr_threshold = th.get("ATR_MIN_USD", ATR_MIN_USD)
    if not pd.isna(atr) and atr < atr_threshold and state.mode != "test":
        logger.debug("ATR %.1f < %.1f — skip", atr, atr_threshold)
        return None
    if atr_zone == "dead" and not th.get("ALLOW_DEAD_ZONE", False):
        logger.debug("ATR zone=dead — skip")
        return None

    # --- Time left ---
    price_yes = market_info.get("price_yes", 0.0)
    price_no = market_info.get("price_no", 0.0)
    end_date_str = market_info.get("end_date_iso")
    if not end_date_str and state.mode != "test":
        return None

    try:
        dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        time_left_min = (dt - datetime.now(timezone.utc)).total_seconds() / 60.0
    except Exception as e:
        logger.error("Time calc error: %s", e)
        time_left_min = 0

    if state.mode != "test":
        if time_left_min < IGNORE_IF_TIME_LEFT_LT_MIN:
            return None
        if price_yes > IGNORE_IF_CONTRACT_PRICE_GT or price_no > IGNORE_IF_CONTRACT_PRICE_GT:
            return None

    if not (th["TIME_LEFT_MIN_MINUTES"] <= time_left_min <= th["TIME_LEFT_MAX_MINUTES"]):
        return None

    # --- 5 indicator votes ---
    rsi_vote, rsi_label = _vote_rsi(float(last.get("rsi_1m", 50)))
    macd_vote, macd_label = _vote_macd(
        float(last.get("macd_line", 0)),
        float(last.get("macd_signal", 0)),
        float(last.get("macd_hist", 0)),
    )
    vwap_vote, vwap_label = _vote_vwap(price, float(last.get("vwap", 0)))
    ema_9 = float(last.get("ema_9", 0))
    ema_21 = float(last.get("ema_21", 0))
    ema_vote, ema_label = _vote_ema(ema_9, ema_21)
    pivots_vote, pivots_label = _vote_pivots(
        price,
        float(last.get("pivot_high", 0)),
        float(last.get("pivot_low", 0)),
    )

    votes = {
        "RSI": {"direction": rsi_vote, "label": rsi_label},
        "MACD": {"direction": macd_vote, "label": macd_label},
        "VWAP": {"direction": vwap_vote, "label": vwap_label},
        "EMA": {"direction": ema_vote, "label": ema_label},
        "Pivots": {"direction": pivots_vote, "label": pivots_label},
    }

    up_count = sum(1 for v in votes.values() if v["direction"] == "UP")
    down_count = sum(1 for v in votes.values() if v["direction"] == "DOWN")

    min_conf = th.get("MIN_CONFLUENCE", 3)
    if up_count >= min_conf:
        direction = "UP"
        confluence = up_count
    elif down_count >= min_conf:
        direction = "DOWN"
        confluence = down_count
    else:
        return None

    # --- Contract price filter ---
    contract_price = price_yes if direction == "UP" else price_no
    if not (th["CONTRACT_PRICE_MIN"] <= contract_price <= th["CONTRACT_PRICE_MAX"]):
        return None

    # --- Start price (для відображення руху BTC у вікні) ---
    event_start_iso = market_info.get("event_start_time")
    start_price = _binance_open_at_polymarket_window_start(df, event_start_iso)
    start_price_source = "pm_window_binance_open"
    if start_price is None:
        if len(df) >= 15:
            start_price = float(df.iloc[-15]["open"])
            start_price_source = "rolling_15m_open"
        else:
            start_price = float(price)
            start_price_source = "current_fallback"

    delta = price - start_price
    delta_percent = (delta / start_price) * 100 if start_price > 0 else 0

    # --- GAP filter: BTC vs PTB (страйк-ціна маркету) ---
    ptb = market_info.get("ptb")
    if state.mode != "test":
        if ptb:
            gap = price - ptb  # позитивний = BTC вище страйку
            gap_ok = (gap >= GAP_MIN_USD) if direction == "UP" else (gap <= -GAP_MIN_USD)
            if not gap_ok:
                logger.debug(
                    "GAP %.1f недостатній для %s (PTB=%.0f, threshold=%.0f) — skip",
                    gap, direction, ptb, GAP_MIN_USD,
                )
                return None
        else:
            # PTB не спарсився — fallback на старий delta-GAP
            if abs(delta) < GAP_MIN_USD:
                logger.debug("GAP fallback: delta %.1f < %.1f — skip", abs(delta), GAP_MIN_USD)
                return None

    chg_1h = last.get("chg_1h", 0)
    chg_1h_val = float(chg_1h) if not pd.isna(chg_1h) else 0
    ema_9_slope = float(last.get("ema_9_slope", 0))

    # backwards-compat fields for storage columns
    ema_pos = "above" if ema_vote == "UP" else "below" if ema_vote == "DOWN" else "at"
    ema_position = f"{ema_pos} EMA9 ({ema_9_slope:+.1f})"

    gap_val = round(price - ptb, 2) if ptb else round(delta, 2)

    # --- Strict time-GAP filter: <5 min left → must have GAP ≥ $100 ---
    if state.mode != "test" and time_left_min < TIME_STRICT_MAX_MIN:
        if abs(gap_val) < GAP_STRICT_USD:
            logger.debug(
                "Time %.1f min < %.0f min, GAP %.1f < %.0f — skip",
                time_left_min, TIME_STRICT_MAX_MIN, abs(gap_val), GAP_STRICT_USD,
            )
            return None

    return {
        "direction": direction,
        "confluence": confluence,
        "votes": votes,
        "start_price": start_price,
        "current_price": price,
        "delta": delta,
        "delta_percent": delta_percent,
        "ptb": ptb,
        "gap": gap_val,
        "contract_price": contract_price,
        "rsi_1m": float(last.get("rsi_1m", 50)),
        "ema_position": ema_position,
        "volume_state": last.get("volume_state", "normal"),
        "time_left": round(time_left_min, 1),
        "start_price_source": start_price_source,
        "atr": round(float(atr), 2) if not pd.isna(atr) else 0,
        "atr_zone": atr_zone,
        "chg_1h": round(chg_1h_val, 3),
        "obi": 1.0,  # placeholder; scanner overwrites with real value
    }


# ---------------------------------------------------------------------------
#  Diagnostic: повний звіт по фільтрах без side effects
# ---------------------------------------------------------------------------

def diagnose_signals(market_info: dict, df: pd.DataFrame) -> str:
    """
    Повертає текстовий звіт про те, на якому фільтрі зупинився сигнал.
    Використовується командою /diagnose у Telegram.
    """
    ok = "✅"
    fail = "❌"
    lines = []

    if df.empty or len(df) < 30:
        return "❌ Недостатньо свічок (&lt; 30)"

    last = df.iloc[-1]
    price = float(last.get("close", 0))
    th = state.get_thresholds()

    lines.append(f"💹 BTC: <b>${price:,.0f}</b> | режим: <b>{state.mode.upper()}</b>")
    lines.append("")

    # ATR
    atr = last.get("atr", 0)
    atr_zone = last.get("atr_zone", "dead")
    atr_threshold = th.get("ATR_MIN_USD", ATR_MIN_USD)
    atr_ok = pd.isna(atr) or float(atr) >= atr_threshold or state.mode == "test"
    zone_ok = atr_zone != "dead" or th.get("ALLOW_DEAD_ZONE", False) or state.mode == "test"
    atr_v = float(atr) if not pd.isna(atr) else 0
    lines.append(
        f"{'✅' if atr_ok and zone_ok else '❌'} ATR: <b>${atr_v:.0f}</b> "
        f"(мін ${atr_threshold:.0f}) | зона: <b>{atr_zone}</b>"
    )

    # Time left
    end_date_str = market_info.get("end_date_iso")
    time_left_min = 0.0
    if end_date_str:
        try:
            from datetime import timezone as _tz
            dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            time_left_min = (dt - datetime.now(_tz.utc)).total_seconds() / 60.0
        except Exception:
            pass
    time_window_ok = th["TIME_LEFT_MIN_MINUTES"] <= time_left_min <= th["TIME_LEFT_MAX_MINUTES"]
    time_hard_ok = state.mode == "test" or time_left_min >= IGNORE_IF_TIME_LEFT_LT_MIN
    lines.append(
        f"{'✅' if time_window_ok and time_hard_ok else '❌'} Час: <b>{time_left_min:.1f} хв</b> "
        f"(вікно {th['TIME_LEFT_MIN_MINUTES']}–{th['TIME_LEFT_MAX_MINUTES']} хв)"
    )

    # Contract price (Gamma)
    price_yes = market_info.get("price_yes", 0.0)
    price_no = market_info.get("price_no", 0.0)
    cp_hard_ok = state.mode == "test" or (
        price_yes <= IGNORE_IF_CONTRACT_PRICE_GT and price_no <= IGNORE_IF_CONTRACT_PRICE_GT
    )
    lines.append(
        f"{'✅' if cp_hard_ok else '❌'} Gamma ціна: YES <b>{price_yes:.2f}</b> / NO <b>{price_no:.2f}</b> "
        f"(hard limit ≤{IGNORE_IF_CONTRACT_PRICE_GT})"
    )

    # 5 indicator votes
    rsi_vote, rsi_label = _vote_rsi(float(last.get("rsi_1m", 50)))
    macd_vote, macd_label = _vote_macd(
        float(last.get("macd_line", 0)),
        float(last.get("macd_signal", 0)),
        float(last.get("macd_hist", 0)),
    )
    vwap_vote, vwap_label = _vote_vwap(price, float(last.get("vwap", 0)))
    ema_vote, ema_label = _vote_ema(
        float(last.get("ema_9", 0)), float(last.get("ema_21", 0))
    )
    pivots_vote, pivots_label = _vote_pivots(
        price,
        float(last.get("pivot_high", 0)),
        float(last.get("pivot_low", 0)),
    )
    votes = {
        "RSI": (rsi_vote, rsi_label),
        "MACD": (macd_vote, macd_label),
        "VWAP": (vwap_vote, vwap_label),
        "EMA": (ema_vote, ema_label),
        "Pivots": (pivots_vote, pivots_label),
    }
    up_count = sum(1 for v, _ in votes.values() if v == "UP")
    down_count = sum(1 for v, _ in votes.values() if v == "DOWN")
    min_conf = th.get("MIN_CONFLUENCE", 3)
    conf_ok = up_count >= min_conf or down_count >= min_conf
    direction = "UP" if up_count >= min_conf else ("DOWN" if down_count >= min_conf else None)

    lines.append("")
    lines.append(f"<b>Індикатори</b> (потрібно ≥{min_conf} в один бік):")
    arrow = {"UP": "⬆️", "DOWN": "⬇️", None: "➖"}
    for name, (vote, label) in votes.items():
        lines.append(f"  {arrow.get(vote, '➖')} {name}: {_html.escape(label)}")
    lines.append(
        f"{'✅' if conf_ok else '❌'} Конфлюенс: UP={up_count} DOWN={down_count} "
        f"{'→ ' + (direction or 'NONE') if conf_ok else '→ немає сигналу'}"
    )

    if not conf_ok:
        return "\n".join(lines)

    # Contract price zone filter
    contract_price = price_yes if direction == "UP" else price_no
    cp_zone_ok = th["CONTRACT_PRICE_MIN"] <= contract_price <= th["CONTRACT_PRICE_MAX"]
    lines.append(
        f"{'✅' if cp_zone_ok else '❌'} Ціна контракту ({direction}): "
        f"<b>{contract_price:.2f}</b> (зона {th['CONTRACT_PRICE_MIN']:.2f}–{th['CONTRACT_PRICE_MAX']:.2f})"
    )

    # GAP
    ptb = market_info.get("ptb")
    if ptb:
        gap = price - ptb
        gap_needed = GAP_MIN_USD if direction == "UP" else -GAP_MIN_USD
        gap_ok = (gap >= GAP_MIN_USD) if direction == "UP" else (gap <= -GAP_MIN_USD)
        lines.append(
            f"{'✅' if gap_ok else '❌'} GAP: BTC ${price:,.0f} vs PTB ${ptb:,.0f} "
            f"= <b>{gap:+.0f}$</b> (мін {GAP_MIN_USD:+.0f}$)"
        )
        gap_val = round(gap, 2)
    else:
        lines.append("⚠️ PTB не спарсився — GAP через delta")
        gap_val = 0.0

    # Strict time-GAP
    strict_ok = True
    if state.mode != "test" and time_left_min < TIME_STRICT_MAX_MIN:
        strict_ok = abs(gap_val) >= GAP_STRICT_USD
        lines.append(
            f"{'✅' if strict_ok else '❌'} Strict GAP (час {time_left_min:.1f}&lt;{TIME_STRICT_MAX_MIN:.0f} хв): "
            f"|GAP| {abs(gap_val):.0f}$ {'≥' if strict_ok else '&lt;'} {GAP_STRICT_USD:.0f}$"
        )

    lines.append("")
    lines.append("<i>OBI та CLOB spread перевіряються в scanner (не тут)</i>")
    return "\n".join(lines)
