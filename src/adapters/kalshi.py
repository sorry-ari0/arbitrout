"""Kalshi adapter — REST API, API key auth."""
import os
from .base import BaseAdapter
from .models import NormalizedEvent


# ============================================================
# CATEGORY MAPPING
# ============================================================
KALSHI_CATEGORY_MAP = {
    "Politics": "politics",
    "Economics": "economics",
    "Crypto": "crypto",
    "Climate": "weather",
    "Culture": "culture",
    "Sports": "sports",
    "Tech": "culture",
    "Science": "culture",
    "Finance": "economics",
}


def _map_category(raw: str) -> str:
    """Map Kalshi category string to our standard categories."""
    for key, val in KALSHI_CATEGORY_MAP.items():
        if key.lower() in raw.lower():
            return val
    return "culture"


# ============================================================
# KALSHI ADAPTER
# ============================================================
class KalshiAdapter(BaseAdapter):
    """Fetch markets from Kalshi trading API v2.

    Uses authenticated trading API when KALSHI_API_KEY is set,
    falls back to public elections API (no auth needed).
    """

    PLATFORM_NAME = "kalshi"
    AUTH_URL = "https://trading-api.kalshi.com/trade-api/v2"
    PUBLIC_URL = "https://api.elections.kalshi.com/trade-api/v2"
    RATE_LIMIT_SECONDS = 1.0

    def __init__(self):
        super().__init__()
        self._api_key = os.environ.get("KALSHI_API_KEY", "")

    # ============================================================
    # FETCH IMPLEMENTATION
    # ============================================================
    async def _fetch(self) -> list[NormalizedEvent]:
        # Try authenticated API first if key is set
        if self._api_key:
            try:
                return await self._fetch_paginated(self.AUTH_URL, auth=True)
            except Exception:
                pass  # Fall through to public API

        # Public elections API (no auth needed)
        return await self._fetch_paginated(self.PUBLIC_URL, auth=False)

    async def _fetch_paginated(self, base_url: str, auth: bool) -> list[NormalizedEvent]:
        client = await self._get_client()
        headers = {}
        if auth and self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        events: list[NormalizedEvent] = []
        cursor = None
        limit = 200

        # Paginate through markets (max 3 pages = 600 markets)
        for _ in range(3):
            params: dict = {"limit": limit, "status": "open"}
            if cursor:
                params["cursor"] = cursor

            resp = await client.get(
                f"{base_url}/markets",
                headers=headers,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

            markets = data.get("markets", [])
            if not markets:
                break

            for m in markets:
                events.append(self._normalize(m))

            cursor = data.get("cursor")
            if not cursor:
                break

        return events

    # ============================================================
    # NORMALIZATION
    # ============================================================
    def _normalize(self, m: dict) -> NormalizedEvent:
        """Convert Kalshi market JSON to NormalizedEvent."""
        yes_price = (m.get("yes_ask", 0) or 0) / 100.0
        no_price = (m.get("no_ask", 0) or 0) / 100.0

        # Fallback: derive from yes_bid if ask not available
        if yes_price == 0 and m.get("yes_bid"):
            yes_price = m["yes_bid"] / 100.0
        if no_price == 0 and m.get("no_bid"):
            no_price = m["no_bid"] / 100.0
        # If still no no_price, derive from yes
        if no_price == 0 and yes_price > 0:
            no_price = 1.0 - yes_price

        volume = m.get("volume", 0) or 0
        expiry = m.get("expiration_time", m.get("close_time", "ongoing"))
        if expiry and "T" in str(expiry):
            expiry = str(expiry)[:10]  # keep date only

        category = _map_category(m.get("category", "") or m.get("series_ticker", ""))
        ticker = m.get("ticker", m.get("id", ""))

        return NormalizedEvent(
            platform="kalshi",
            event_id=ticker,
            title=m.get("title", m.get("subtitle", ticker)),
            category=category,
            yes_price=round(yes_price, 4),
            no_price=round(no_price, 4),
            volume=int(volume),
            expiry=expiry or "ongoing",
            url=f"https://kalshi.com/markets/{ticker}",
        )
