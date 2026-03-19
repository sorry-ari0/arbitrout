"""Kalshi adapter — REST API with auth, public events+orderbook fallback."""
import asyncio
import logging
import os
from .base import BaseAdapter
from .models import NormalizedEvent

logger = logging.getLogger("adapters.kalshi")


# ============================================================
# CATEGORY MAPPING
# ============================================================
KALSHI_CATEGORY_MAP = {
    "Politics": "politics",
    "Elections": "politics",
    "Economics": "economics",
    "Financials": "economics",
    "Companies": "economics",
    "Crypto": "crypto",
    "Climate": "weather",
    "Weather": "weather",
    "Culture": "culture",
    "Sports": "sports",
    "Entertainment": "culture",
    "Tech": "culture",
    "Science": "culture",
    "Social": "culture",
    "Health": "culture",
    "Transportation": "culture",
    "World": "politics",
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
    """Fetch markets from Kalshi.

    Strategy:
    1. If KALSHI_API_KEY set: use authenticated trading API (has prices)
    2. Else: use public elections API events → per-event markets → orderbook
       for price discovery (no auth needed)
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
        # Try authenticated API first (has prices inline)
        if self._api_key:
            try:
                return await self._fetch_authenticated()
            except Exception as e:
                logger.warning("Kalshi auth API failed: %s — trying public", e)

        # Public API: events → markets → orderbook for prices
        return await self._fetch_public()

    async def _fetch_authenticated(self) -> list[NormalizedEvent]:
        """Fetch from authenticated trading API (has inline prices)."""
        client = await self._get_client()
        headers = {"Authorization": f"Bearer {self._api_key}"}
        events: list[NormalizedEvent] = []
        cursor = None

        for _ in range(3):
            params: dict = {"limit": 200, "status": "open"}
            if cursor:
                params["cursor"] = cursor
            resp = await client.get(
                f"{self.AUTH_URL}/markets", headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
            markets = data.get("markets", [])
            if not markets:
                break
            for m in markets:
                events.append(self._normalize_market(m))
            cursor = data.get("cursor")
            if not cursor:
                break

        return events

    async def _fetch_public(self) -> list[NormalizedEvent]:
        """Fetch from public API: events for titles, orderbook for prices.

        Flow: GET /events (paginated) → for each event, GET /markets?event_ticker=X
        → for interesting markets, GET /markets/{ticker}/orderbook for prices.
        """
        client = await self._get_client()

        # Step 1: Fetch events (paginated, up to 200)
        all_events = []
        cursor = None
        for _ in range(2):  # 2 pages × 100 = 200 events max
            params: dict = {"limit": 100, "status": "open"}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = await client.get(f"{self.PUBLIC_URL}/events", params=params)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning("Kalshi public events fetch failed: %s", e)
                break
            page_events = data.get("events", [])
            if not page_events:
                break
            all_events.extend(page_events)
            cursor = data.get("cursor")
            if not cursor:
                break

        if not all_events:
            return []

        # Step 2: For each event, fetch its markets
        # Prioritize categories that overlap with other platforms
        priority_cats = {"Politics", "Elections", "Economics", "Financials",
                         "World", "Companies", "Climate and Weather"}
        priority_events = [e for e in all_events if e.get("category") in priority_cats]
        other_events = [e for e in all_events if e.get("category") not in priority_cats]
        # Take all priority + first 30 others
        target_events = priority_events + other_events[:30]

        normalized: list[NormalizedEvent] = []
        sem = asyncio.Semaphore(5)  # Max 5 concurrent requests

        async def fetch_event_markets(ev: dict):
            event_ticker = ev.get("event_ticker", "")
            category = ev.get("category", "")
            event_title = ev.get("title", "")
            if not event_ticker:
                return

            async with sem:
                try:
                    resp = await client.get(
                        f"{self.PUBLIC_URL}/markets",
                        params={"event_ticker": event_ticker, "limit": 50})
                    resp.raise_for_status()
                    markets = resp.json().get("markets", [])
                except Exception:
                    return

                for m in markets:
                    ticker = m.get("ticker", "")
                    title = m.get("title") or m.get("subtitle") or event_title
                    # Skip multi-outcome combo titles (comma-separated)
                    if title.count(",") >= 2 and title.startswith(("yes ", "no ")):
                        continue

                    # If market has sub_title, use "Event: Sub" format
                    sub = m.get("subtitle") or m.get("sub_title", "")
                    if sub and sub != title:
                        display_title = f"{event_title}: {sub}" if event_title != title else f"{title}: {sub}"
                    else:
                        display_title = title

                    expiry = m.get("expiration_time", m.get("close_time", "ongoing"))
                    if expiry and "T" in str(expiry):
                        expiry = str(expiry)[:10]

                    # Try to get price from orderbook
                    yes_price, no_price = await self._get_orderbook_price(client, ticker)

                    normalized.append(NormalizedEvent(
                        platform="kalshi",
                        event_id=ticker,
                        title=display_title,
                        category=_map_category(category),
                        yes_price=round(yes_price, 4),
                        no_price=round(no_price, 4),
                        volume=int(m.get("volume", 0) or 0),
                        expiry=expiry or "ongoing",
                        url=f"https://kalshi.com/markets/{ticker}",
                    ))

        # Fetch markets for all target events concurrently
        tasks = [fetch_event_markets(ev) for ev in target_events]
        await asyncio.gather(*tasks, return_exceptions=True)

        return normalized

    async def _get_orderbook_price(self, client, ticker: str) -> tuple[float, float]:
        """Get best yes/no price from public orderbook. Returns (yes_price, no_price)."""
        try:
            resp = await client.get(f"{self.PUBLIC_URL}/markets/{ticker}/orderbook")
            resp.raise_for_status()
            data = resp.json()
            ob = data.get("orderbook_fp", data.get("orderbook", {}))

            # yes_dollars = bids to buy YES (sorted ascending by price)
            # no_dollars = bids to buy NO (sorted ascending by price)
            yes_bids = ob.get("yes_dollars", [])
            no_bids = ob.get("no_dollars", [])

            # Best YES price = cheapest YES ask = 1 - highest NO bid
            # Or use highest YES bid as approximation
            yes_price = 0.0
            no_price = 0.0

            if yes_bids:
                # Highest YES bid = last entry (sorted ascending)
                yes_price = float(yes_bids[-1][0])
            if no_bids:
                # Highest NO bid = last entry
                no_price = float(no_bids[-1][0])

            # If we have one but not the other, derive
            if yes_price > 0 and no_price == 0:
                no_price = 1.0 - yes_price
            elif no_price > 0 and yes_price == 0:
                yes_price = 1.0 - no_price

            return (yes_price, no_price)

        except Exception:
            return (0.0, 0.0)

    # ============================================================
    # NORMALIZATION (for authenticated API)
    # ============================================================
    def _normalize_market(self, m: dict) -> NormalizedEvent:
        """Convert Kalshi market JSON to NormalizedEvent (authenticated API)."""
        yes_price = (m.get("yes_ask", 0) or 0) / 100.0
        no_price = (m.get("no_ask", 0) or 0) / 100.0

        if yes_price == 0 and m.get("yes_bid"):
            yes_price = m["yes_bid"] / 100.0
        if no_price == 0 and m.get("no_bid"):
            no_price = m["no_bid"] / 100.0
        if no_price == 0 and yes_price > 0:
            no_price = 1.0 - yes_price

        volume = m.get("volume", 0) or 0
        expiry = m.get("expiration_time", m.get("close_time", "ongoing"))
        if expiry and "T" in str(expiry):
            expiry = str(expiry)[:10]

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
