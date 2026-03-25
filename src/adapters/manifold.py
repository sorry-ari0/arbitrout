"""Manifold Markets adapter — public REST API, no auth."""
from .base import BaseAdapter
from .models import NormalizedEvent

# Manifold's API has a rate limit of 100 requests per minute per IP address.
# Setting it conservatively to 1 second per request to be safe.
RATE_LIMIT_SECONDS = 1.0


# ============================================================
# CATEGORY MAPPING
# ============================================================
def _guess_category(group_slug: str, tags: list | None) -> str:
    """Guess category from group slug and tags."""
    text = group_slug.lower()
    if tags:
        text += " " + " ".join(str(t).lower() for t in tags)

    if any(w in text for w in ["politics", "election", "government", "us", "uk", "world"]):
        return "politics"
    if any(w in text for w in ["crypto", "bitcoin", "ethereum", "btc", "eth"]):
        return "crypto"
    if any(w in text for w in ["economy", "finance", "business", "gdp", "inflation"]):
        return "economics"
    if any(w in text for w in ["sports", "nba", "nfl", "mlb", "fifa"]):
        return "sports"
    if any(w in text for w in ["science", "ai", "technology", "health", "future"]):
        return "science"
    if any(w in text for w in ["culture", "movies", "music", "books", "games"]):
        return "culture"
    return "culture"


# ============================================================
# MANIFOLD ADAPTER
# ============================================================
class ManifoldAdapter(BaseAdapter):
    """Fetch markets from Manifold Markets public API."""

    PLATFORM_NAME = "manifold"
    BASE_URL = "https://manifold.markets/api/v0"
    RATE_LIMIT_SECONDS = RATE_LIMIT_SECONDS

    # ============================================================
    # FETCH IMPLEMENTATION
    # ============================================================
    async def _fetch(self) -> list[NormalizedEvent]:
        client = await self._get_client()
        events: list[NormalizedEvent] = []

        # Manifold markets API (simplified endpoint to get all markets)
        # Fetching 'open' markets sorted by 'newest' to get a diverse set.
        # Max limit is 1000, we'll try to get as many as reasonable without
        # hitting rate limits too hard.
        resp = await client.get(
            f"{self.BASE_URL}/markets",
            params={
                "limit": 500,  # Fetch up to 500 markets
                "sort": "liquidity",
                "order": "desc",
            },
        )
        resp.raise_for_status()
        markets = resp.json()

        for m in markets:
            ev = self._normalize(m)
            if ev:
                events.append(ev)

        return events

    # ============================================================
    # NORMALIZATION
    # ============================================================
    def _normalize(self, m: dict) -> NormalizedEvent | None:
        """Convert Manifold market to NormalizedEvent."""
        if m.get("outcomeType") != "BINARY":
            return None  # Only handle binary markets for now

        title = m.get("question")
        if not title:
            return None

        market_id = m.get("id")
        if not market_id:
            return None

        # Prices: 'probability' is the current YES price
        # Manifold doesn't explicitly have 'NO' price, assume 1 - YES
        yes_price = m.get("probability", 0.0)
        no_price = 1.0 - yes_price

        # Volume: Manifold API returns 'volume' (number of trades) and 'totalLiquidity'
        # Using 'volume' for now, can switch to 'totalLiquidity' (M$) if preferred
        volume = int(m.get("volume", 0))

        # Expiry: 'closeTime' is a Unix timestamp in milliseconds
        expiry = "ongoing"
        close_time_ms = m.get("closeTime")
        if close_time_ms:
            import datetime
            try:
                dt_object = datetime.datetime.fromtimestamp(close_time_ms / 1000)
                expiry = dt_object.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                pass

        url = m.get("url", f"https://manifold.markets/m/{m.get('slug', market_id)}")
        group_slug = m.get("groupSlugs", [])
        tags = m.get("tags", [])

        return NormalizedEvent(
            platform="manifold",
            event_id=str(market_id),
            title=title,
            category=_guess_category(group_slug[0] if group_slug else "", tags),
            yes_price=round(float(yes_price), 4),
            no_price=round(float(no_price), 4),
            volume=volume,
            expiry=expiry,
            url=url,
        )

