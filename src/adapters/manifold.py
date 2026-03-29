"""Manifold Markets adapter — public API, no auth."""
from .base import BaseAdapter
from .models import NormalizedEvent


# ============================================================
# CATEGORY MAPPING
# ============================================================
def _map_category(tags: list | None) -> str:
    if not tags:
        return "culture"
    for tag in tags:
        t = str(tag).lower().strip()
        if t in ["politics", "us politics", "world politics", "geopolitics"]:
            return "politics"
        if t in ["crypto", "blockchain", "bitcoin", "ethereum"]:
            return "crypto"
        if t in ["sports", "nfl", "nba", "mlb", "nhl", "soccer"]:
            return "sports"
        if t in ["science", "technology", "ai", "health", "future", "forecasts"]:
            return "science"
        if t in ["economics", "finance", "business", "inflation", "gdp"]:
            return "economics"
        if t in ["culture", "pop culture", "movies", "music", "gaming"]:
            return "culture"
    return "culture"


# ============================================================
# MANIFOLD ADAPTER
# ============================================================
class ManifoldAdapter(BaseAdapter):
    """Fetch markets from Manifold Markets public API."""

    PLATFORM_NAME = "manifold"
    BASE_URL = "https://manifold.markets/api/v0"
    RATE_LIMIT_SECONDS = 0.5

    async def _fetch(self) -> list[NormalizedEvent]:
        client = await self._get_client()
        events: list[NormalizedEvent] = []

        # Manifold's API returns markets directly.
        # Fetch only 'OPEN' markets, sorted by volume.
        resp = await client.get(
            f"{self.BASE_URL}/markets",
            params={
                "limit": 1000,
                "sort": "volume",
                "order": "desc",
            }
        )
        resp.raise_for_status()
        markets = resp.json()

        for m in markets:
            if m.get("outcomeType") == "BINARY" and m.get("state") == "OPEN":
                ev = self._normalize(m)
                if ev:
                    events.append(ev)
        return events

    def _normalize(self, m: dict) -> NormalizedEvent | None:
        """Convert Manifold market to NormalizedEvent."""
        title = m.get("question")
        if not title:
            return None

        # Manifold prices are probabilities from 0 to 1
        yes_price = float(m.get("probability", 0))
        no_price = round(1.0 - yes_price, 4)

        volume = int(float(m.get("volume", 0)))

        # Expiry is 'closeTime' in milliseconds since epoch
        expiry_ms = m.get("closeTime")
        expiry = "ongoing"
        if expiry_ms:
            import datetime
            expiry_dt = datetime.datetime.fromtimestamp(expiry_ms / 1000)
            expiry = expiry_dt.isoformat()[:10]

        url = m.get("url")
        if not url:
            url = f"https://manifold.markets/{m.get('creatorUsername')}/{m.get('slug')}"

        return NormalizedEvent(
            platform="manifold",
            event_id=str(m.get("id")),
            title=title,
            category=_map_category(m.get("groupSlugs", [])), # Manifold uses groupSlugs as categories
            yes_price=round(yes_price, 4),
            no_price=round(no_price, 4),
            volume=volume,
            expiry=expiry,
            url=url,
        )

