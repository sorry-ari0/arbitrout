"""Polymarket adapter — Gamma API (no auth) + CLOB for prices."""
from .base import BaseAdapter
from .models import NormalizedEvent
import logging
import time
import random
import asyncio

# ============================================================
# CATEGORY MAPPING
# ============================================================
POLY_CATEGORY_MAP = {
    "politics": "politics",
    "crypto": "crypto",
    "sports": "sports",
    "pop culture": "culture",
    "science": "culture",
    "business": "economics",
    "economics": "economics",
    "finance": "economics",
    "world": "politics",
    "technology": "culture",
}


def _map_category(tags: list | None) -> str:
    if not tags:
        return "culture"
    for tag in tags:
        t = str(tag).lower().strip()
        if t in POLY_CATEGORY_MAP:
            return POLY_CATEGORY_MAP[t]
    return "culture"


# ============================================================
# POLYMARKET ADAPTER
# ============================================================
class PolymarketAdapter(BaseAdapter):
    """Fetch markets from Polymarket Gamma API."""

    PLATFORM_NAME = "polymarket"
    BASE_URL = "https://gamma-api.polymarket.com"
    CLOB_URL = "https://clob.polymarket.com"
    RATE_LIMIT_SECONDS = 0.5

    # ============================================================
    # FETCH IMPLEMENTATION
    # ============================================================
    async def _fetch(self) -> list[NormalizedEvent]:
        client = await self._get_client()
        events: list[NormalizedEvent] = []

        # Gamma API — get active markets
        delays = [2, 4, 8]
        for attempt in range(4):
            try:
                resp = await client.get(
                    f"{self.BASE_URL}/markets",
                    params={
                        "closed": "false",
                        "limit": 100,
                        "order": "volume",
                        "ascending": "false",
                    },
                )
                resp.raise_for_status()
                break
            except Exception as e:
                if attempt < 3:
                    logging.warning(f"Polymarket API request failed (attempt {attempt+1}/3), retrying in {delays[attempt]}s: {str(e)}")
                    await asyncio.sleep(delays[attempt] + random.random())
                else:
                    logging.error(f"Polymarket API request failed after 3 retries: {str(e)}")
                    return []
        else:
            return []

        markets = resp.json()

        if not isinstance(markets, list):
            markets = markets.get("data", markets.get("markets", []))

        for m in markets:
            ev = self._normalize(m)
            if ev:
                events.append(ev)

        return events

    # ============================================================
    # NORMALIZATION
    # ============================================================
    def _normalize(self, m: dict) -> NormalizedEvent | None:
        """Convert Polymarket Gamma market to NormalizedEvent."""
        title = m.get("question", m.get("title", ""))
        if not title:
            return None

        # Prices: outcomePrices is a JSON string like "[\"0.85\",\"0.15\"]"
        yes_price = 0.0
        no_price = 0.0

        outcome_prices = m.get("outcomePrices", "")
        if isinstance(outcome_prices, str) and outcome_prices.startswith("["):
            import json
            try:
                prices = json.loads(outcome_prices)
                if len(prices) >= 2:
                    yes_price = float(prices[0])
                    no_price = float(prices[1])
            except (json.JSONDecodeError, ValueError, IndexError):
                pass

        if yes_price == 0 and no_price == 0:
            # Try bestBid/bestAsk fields
            yes_price = float(m.get("bestBid", 0) or 0)
            no_price = 1.0 - yes_price if yes_price > 0 else 0

        volume = 0
        raw_vol = m.get("volume", m.get("volumeNum", 0))
        try:
            volume = int(float(raw_vol or 0))
        except (ValueError, TypeError):
            pass

        # Expiry
        expiry = m.get("endDate", m.get("end_date_iso", "ongoing"))
        if expiry and "T" in str(expiry):
            expiry = str(expiry)[:10]

        slug = m.get("slug", m.get("id", ""))
        condition_id = m.get("conditionId", m.get("condition_id", slug))
        # Prefer event slug for URL (event page, not individual market)
        event_slug = m.get("eventSlug", m.get("groupItemSlug", slug))
        tags = m.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]

        if event_slug:
            url = f"https://polymarket.com/event/{event_slug}"
        elif slug:
            url = f"https://polymarket.com/event/{slug}"
        else:
            url = "https://polymarket.com"

        return NormalizedEvent(
            platform="polymarket",
            event_id=str(condition_id),
            title=title,
            category=_map_category(tags),
            yes_price=round(yes_price, 4),
            no_price=round(no_price, 4),
            volume=volume,
            expiry=expiry or "ongoing",
            url=url,
        )
