import httpx
import logging
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Recurring BTC 15m: slug = btc-updown-15m-{unix}; unix = початок 15-хв вікна в America/New_York
# (див. Polymarket Gamma: GET /markets/slug/{slug})
BTC_15M_SLUG_PREFIX = "btc-updown-15m"
ET = ZoneInfo("America/New_York")


def _candidate_btc_15m_unix_starts() -> list[int]:
    """Поточне + сусідні 15-хв вікна ET (на переходах між вікнами)."""
    now = datetime.now(ET)
    minute_floor = (now.minute // 15) * 15
    base = now.replace(minute=minute_floor, second=0, microsecond=0)
    return sorted(
        {int((base + timedelta(minutes=m)).timestamp()) for m in (-15, 0, 15)}
    )


def _parse_ptb(question: str) -> Optional[float]:
    """Парсить страйк-ціну (PTB) з тексту питання: 'Will BTC be above $69,500...'"""
    if not question:
        return None
    m = re.search(r"\$([0-9,]+(?:\.[0-9]+)?)", question)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def _parse_clob_token_ids(market: dict[str, Any]) -> tuple[str, str]:
    """Parse YES/NO token IDs from clobTokenIds field."""
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    if isinstance(raw, list) and len(raw) >= 2:
        return str(raw[0]), str(raw[1])
    return "", ""


def _parse_outcome_prices(market: dict[str, Any]) -> tuple[float, float]:
    raw = market.get("outcomePrices", ["0", "0"])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = ["0", "0"]
    try:
        up = float(raw[0]) if len(raw) > 0 else 0.0
        down = float(raw[1]) if len(raw) > 1 else 0.0
    except (ValueError, TypeError):
        up, down = 0.0, 0.0
    return up, down


class PolymarketClient:
    def __init__(self):
        self.base_url = "https://gamma-api.polymarket.com"
        self.client = httpx.AsyncClient(base_url=self.base_url)

    async def get_market_by_slug(self, slug: str) -> Optional[dict[str, Any]]:
        """Один маркет за slug (документація: GET /markets/slug/{slug})."""
        try:
            r = await self.client.get(f"/markets/slug/{slug}")
            if r.status_code == httpx.codes.NOT_FOUND:
                return None
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            logger.warning("markets/slug/%s: %s", slug, e)
            return None

    async def get_active_btc_markets(self) -> list:
        """
        Лише «Bitcoin Up or Down — 15 Minutes»: окремий запит на кожен slug вікна,
        без /events?series_slug=… (на бекенді цей фільтр зараз не працює).
        """
        btc_markets: list[dict[str, Any]] = []
        seen_slugs: set[str] = set()

        for unix_start in _candidate_btc_15m_unix_starts():
            slug = f"{BTC_15M_SLUG_PREFIX}-{unix_start}"
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)

            market = await self.get_market_by_slug(slug)
            if not market:
                continue

            if market.get("closed") or not market.get("active"):
                continue

            # Лише серія btc-up-or-down-15m (подвійна перевірка)
            evs = market.get("events") or []
            series_ok = any(
                (e.get("seriesSlug") == "btc-up-or-down-15m")
                for e in evs
            )
            if not series_ok and not (market.get("slug") or "").startswith(
                f"{BTC_15M_SLUG_PREFIX}-"
            ):
                continue

            end_str = market.get("endDate") or market.get("endDateIso")
            if end_str:
                dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                time_left = (dt - datetime.now(timezone.utc)).total_seconds() / 60.0
                if time_left > 15.5 or time_left < 0:
                    continue

            price_yes, price_no = _parse_outcome_prices(market)
            token_yes_id, token_no_id = _parse_clob_token_ids(market)
            event = evs[0] if evs else {}
            event_id = event.get("id")
            title = event.get("title") or market.get("question") or ""

            logger.info(
                "🟢 BTC 15m маркет: %s (ID: %s, slug: %s)",
                market.get("question"),
                market.get("id"),
                market.get("slug"),
            )

            event_start = market.get("eventStartTime") or event.get("startTime")

            btc_markets.append(
                {
                    "event_id": event_id,
                    "market_id": market.get("id"),
                    "market_slug": market.get("slug") or slug,
                    "neg_risk": bool(market.get("negRisk", False)),
                    "title": title,
                    "price_yes": price_yes,
                    "price_no": price_no,
                    "end_date_iso": end_str,
                    "event_start_time": event_start,
                    "token_yes_id": token_yes_id,
                    "token_no_id": token_no_id,
                }
            )

        if not btc_markets:
            logger.info(
                "Активних BTC 15m маркетів за slug не знайдено "
                "(кандидати unix: %s)",
                _candidate_btc_15m_unix_starts(),
            )
        return btc_markets

    async def get_market_prices(self, market_id: str) -> Optional[dict[str, float]]:
        """Ціни Up/Down для settlement за numeric/string id маркету."""
        try:
            r = await self.client.get(f"/markets/{market_id}")
            if r.status_code == httpx.codes.NOT_FOUND:
                return None
            r.raise_for_status()
            market = r.json()
            up, down = _parse_outcome_prices(market)
            return {"price_yes": up, "price_no": down}
        except httpx.HTTPError as e:
            logger.error("Помилка get_market_prices(%s): %s", market_id, e)
            return None

    async def close(self):
        await self.client.aclose()
