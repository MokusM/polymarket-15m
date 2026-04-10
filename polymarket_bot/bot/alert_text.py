"""Telegram alert text — 5-indicator confluence model (v3)."""

from datetime import datetime
from html import escape as html_esc
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

VOTE_ICONS = {"UP": "\U0001f7e2", "DOWN": "\U0001f534", None: "\u26aa"}

# ---------------------------------------------------------------------------
#  Trading sessions (per PolySigma course, ET timezone)
# ---------------------------------------------------------------------------

SESSIONS = {
    "ASIA_NIGHT": {
        "label": "ASIA / NIGHT",
        "hours": "20:00 \u2014 06:00 ET",
        "quality": 4,
        "volume": "\u0421\u0435\u0440\u0435\u0434\u043d\u0456\u0439",
        "spread": "\u041d\u043e\u0440\u043c\u0430\u043b\u044c\u043d\u0438\u0439",
        "emoji": "\U0001f319",
        "tip": "\u0425\u043e\u0440\u043e\u0448\u0456 \u0443\u043c\u043e\u0432\u0438. VWAP \u043c\u0435\u043d\u0448 \u0442\u043e\u0447\u043d\u0438\u0439 \u2014 \u043e\u0431\u0435\u0440\u0435\u0436\u043d\u043e.",
        "sizing": "12%",
    },
    "LONDON_NY": {
        "label": "LONDON + NY OVERLAP",
        "hours": "06:00 \u2014 10:00 ET",
        "quality": 5,
        "volume": "\u041c\u0430\u043a\u0441\u0438\u043c\u0430\u043b\u044c\u043d\u0438\u0439",
        "spread": "\u041c\u0456\u043d\u0456\u043c\u0430\u043b\u044c\u043d\u0438\u0439",
        "emoji": "\U0001f3c6",
        "tip": "\u041d\u0430\u0439\u043a\u0440\u0430\u0449\u0438\u0439 \u0447\u0430\u0441! \u041f\u043e\u0432\u043d\u0438\u0439 \u0440\u043e\u0437\u043c\u0456\u0440 \u043f\u043e\u0437\u0438\u0446\u0456\u0457, \u0432\u0441\u0456 \u0456\u043d\u0434\u0438\u043a\u0430\u0442\u043e\u0440\u0438 \u043d\u0430\u0434\u0456\u0439\u043d\u0456.",
        "sizing": "12\u201315%",
    },
    "NY_MIDDAY": {
        "label": "NY MIDDAY",
        "hours": "10:00 \u2014 15:00 ET",
        "quality": 3,
        "volume": "\u0421\u0435\u0440\u0435\u0434\u043d\u0456\u0439",
        "spread": "\u041d\u043e\u0440\u043c\u0430\u043b\u044c\u043d\u0438\u0439",
        "emoji": "\U0001f3d9\ufe0f",
        "tip": "\u0421\u0435\u0440\u0435\u0434\u043d\u0456 \u0443\u043c\u043e\u0432\u0438. \u041b\u043e\u043d\u0434\u043e\u043d \u0437\u0433\u043e\u0440\u0442\u0430\u0454\u0442\u044c\u0441\u044f, \u0432\u0438\u0431\u0456\u0440\u043a\u043e\u0432\u0456 \u0432\u0445\u043e\u0434\u0438.",
        "sizing": "10\u201312%",
    },
    "WINDOW_15": {
        "label": "15:00 WINDOW",
        "hours": "15:00 \u2014 16:00 ET",
        "quality": 4,
        "volume": "\u0412\u0438\u0441\u043e\u043a\u0438\u0439",
        "spread": "\u041d\u043e\u0440\u043c\u0430\u043b\u044c\u043d\u0438\u0439",
        "emoji": "\u23f0",
        "tip": "\u0425\u043e\u0440\u043e\u0448\u0430 \u043c\u043e\u0436\u043b\u0438\u0432\u0456\u0441\u0442\u044c. \u0421\u043b\u0456\u0434\u043a\u0443\u0439 \u0437\u0430 \u0441\u0435\u0442\u0430\u043f\u0430\u043c\u0438.",
        "sizing": "12%",
    },
    "PACIFIC": {
        "label": "PACIFIC",
        "hours": "16:00 \u2014 20:00 ET",
        "quality": 1,
        "volume": "\u041d\u0438\u0437\u044c\u043a\u0438\u0439",
        "spread": "\u0428\u0438\u0440\u043e\u043a\u0438\u0439",
        "emoji": "\U0001f305",
        "tip": "\u041f\u043e\u0433\u0430\u043d\u0456 \u0443\u043c\u043e\u0432\u0438. \u041b\u0438\u0448\u0435 \u0441\u043f\u043e\u0441\u0442\u0435\u0440\u0435\u0436\u0435\u043d\u043d\u044f, \u043d\u0435 \u0442\u043e\u0440\u0433\u0443\u0432\u0430\u0442\u0438.",
        "sizing": "\u2014",
    },
}


def get_current_session_key() -> str:
    hour = datetime.now(ET).hour
    if hour >= 20 or hour < 6:
        return "ASIA_NIGHT"
    if hour < 10:
        return "LONDON_NY"
    if hour < 15:
        return "NY_MIDDAY"
    if hour < 16:
        return "WINDOW_15"
    return "PACIFIC"


def format_session_alert_html(
    session_key: str,
    btc_price: float | None = None,
    atr: float | None = None,
    atr_zone: str | None = None,
    chg_1h: float | None = None,
) -> str:
    s = SESSIONS[session_key]
    now_et = datetime.now(ET).strftime("%H:%M ET")
    stars = "\u2b50" * s["quality"] + "\u2606" * (5 - s["quality"])

    lines = [
        f"\U0001f514 <b>\u0422\u043e\u0440\u0433\u043e\u0432\u0430 \u0441\u0435\u0441\u0456\u044f: {s['emoji']} {s['label']}</b>",
        f"\u23f0 {s['hours']} (\u0437\u0430\u0440\u0430\u0437 {now_et})",
        "",
        f"<b>\u042f\u043a\u0456\u0441\u0442\u044c: {stars} ({s['quality']}/5)</b>",
        f"\U0001f4ca \u041e\u0431'\u0454\u043c: {s['volume']} | \u0421\u043f\u0440\u0435\u0434: {s['spread']}",
        f"\U0001f4b0 \u0420\u043e\u0437\u043c\u0456\u0440 \u043f\u043e\u0437\u0438\u0446\u0456\u0457: {s['sizing']}",
        "",
        f"\u2705 <i>{s['tip']}</i>",
    ]

    if btc_price is not None:
        lines.append("")
        lines.append(f"BTC: ${btc_price:,.2f}")
    if atr is not None and atr_zone is not None:
        zone_label = ATR_ZONE_LABELS.get(atr_zone, atr_zone)
        lines.append(f"ATR: ${atr:.1f} ({zone_label})")
    if chg_1h is not None:
        lines.append(f"BTC 1h: {chg_1h:+.2f}%")

    return "\n".join(lines)

ATR_ZONE_LABELS = {
    "dead": "\U0001f480 DEAD (&lt;$30)",
    "quiet": "\U0001f634 QUIET ($30-60)",
    "golden": "\U0001f3c6 GOLDEN ($60-100)",
    "high": "\u26a1 HIGH ($100-120)",
    "extreme": "\U0001f30b EXTREME (&gt;$120)",
}

INDICATOR_ORDER = ("RSI", "MACD", "VWAP", "EMA", "Pivots")


def _assess_quality(signal: dict) -> tuple[int, str]:
    score = 0
    notes: list[str] = []

    confluence = signal.get("confluence", 0)
    atr_zone = signal.get("atr_zone", "dead")
    contract_price = signal.get("contract_price", 0.5)
    time_left = signal.get("time_left", 0)

    score += confluence

    if atr_zone == "golden":
        notes.append("golden ATR zone")
    elif atr_zone == "quiet":
        score -= 1
        notes.append("low volatility")
    elif atr_zone in ("high", "extreme"):
        notes.append("high volatility risk")

    if 0.50 <= contract_price <= 0.60:
        notes.append("good price zone")
    elif contract_price > 0.65:
        score -= 1
        notes.append("expensive contract")

    if 5 <= time_left <= 9:
        notes.append("optimal time")
    elif time_left > 10:
        notes.append("early entry")
    elif time_left < 4:
        notes.append("late entry")

    score = max(1, min(5, score))
    return score, " | ".join(notes) if notes else "standard"


def format_signal_alert_html(signal: dict, mode: str, stake_usd: float) -> str:
    direction = signal.get("direction", "?")
    confluence = signal.get("confluence", 0)
    votes = signal.get("votes") or {}

    contract_price = signal.get("contract_price", 0)
    contract_side = "YES" if direction == "UP" else "NO"
    time_left = signal.get("time_left", 0)
    current_price = signal.get("current_price", 0)
    delta = signal.get("delta", 0)
    atr = signal.get("atr", 0)
    atr_zone = signal.get("atr_zone", "unknown")
    volume_state = signal.get("volume_state", "?")

    _fv = signal.get("filter_version", "current")
    _fv_label = {"current": "", "new": " [NEW]", "both": " [BOTH]"}.get(_fv, "")

    dir_icon = "\U0001f534" if direction == "DOWN" else "\U0001f7e2"
    title = signal.get("market_title", "")

    # Індикатори одним рядком
    ind_parts = []
    for name in INDICATOR_ORDER:
        v = votes.get(name, {})
        vote_dir = v.get("direction")
        if vote_dir == direction:
            ind_parts.append(f"{name} \u2705")
        elif vote_dir is not None:
            ind_parts.append(f"{name} \u274c")
        else:
            ind_parts.append(f"{name} \u2796")
    indicators_line = " | ".join(ind_parts)

    title_line = f"\U0001f4cc {html_esc(title)}\n" if title else ""

    return (
        f"{dir_icon} <b>{direction}</b> conf={confluence}{_fv_label} | "
        f"{contract_side} @ {contract_price:.2f} | {time_left:.1f} min\n"
        f"{title_line}"
        f"BTC ${current_price:,.0f} ({delta:+.0f}) | ATR ${atr:.0f} {atr_zone} | vol: {volume_state}\n"
        f"\n"
        f"{indicators_line}\n"
    )
