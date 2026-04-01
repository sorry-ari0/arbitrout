"""Metaculus adapter — public API, no auth."""
from .base import BaseAdapter
from .models import NormalizedEvent


# ============================================================
# CATEGORY MAPPING
# ============================================================
def _map_category(tags: list | None) -> str:
    if not tags:
        return "science" # Metaculus is often science/tech focused
    for tag in tags:
        t = str(tag).lower().strip()
        if t in ["politics", "international relations", "geopolitics"]:
            return "politics"
        if t in ["crypto", "blockchain", "bitcoin", "ethereum"]:
            return "crypto"
        if t in ["sports"]:
            return "sports"
        if t in ["science", "technology", "ai", "medicine", "health", "space"]:
            return "science"
        if t in ["economics", "finance", "business", "market", "economy"]:
            return "economics"
        if t in ["culture", "society", "entertainment"]:
            return "culture"
    return "science"


# ============================================================
# METACULUS ADAPTER
# ============================================================
class MetaculusAdapter(BaseAdapter):
    """Fetch markets from Metaculus public API."""

    PLATFORM_NAME = "metaculus"
    BASE_URL = "https://www.metaculus.com/api2/questions"
    RATE_LIMIT_SECONDS = 1.0 # Be conservative with public APIs

    async def _fetch(self) -> list[NormalizedEvent]:
        client = await self._get_client()
        events: list[NormalizedEvent] = []

        # Fetch only 'open' questions, sorted by popularity or activity
        # Metaculus doesn't have a direct 'volume' concept like other markets,
        # but 'popular' or 'activity' can indicate more engagement.
        resp = await client.get(
            self.BASE_URL,
            params={
                "status": "open",
                "order_by": "-activity", # Sort by most active
                "limit": 100, # Fetch a reasonable number of questions
            }
        )
        resp.raise_for_status()
        data = resp.json()
        questions = data.get("results", [])

        for q in questions:
            # We are interested in binary questions for arbitrage
            if q.get("question_type") != "binary":
                continue
            
            ev = self._normalize(q)
            if ev:
                events.append(ev)
        return events

    def _normalize(self, q: dict) -> NormalizedEvent | None:
        """Convert Metaculus question to NormalizedEvent."""
        title = q.get("title")
        if not title:
            return None

        # Metaculus has a 'community_prediction' which is a probability (0-1)
        community_prediction = q.get("community_prediction", {}).get("value")
        
        yes_price = 0.0
        no_price = 0.0

        if community_prediction is not None:
            yes_price = float(community_prediction)
            no_price = round(1.0 - yes_price, 4)
        
        # If no community_prediction, we can't get current prices for arb
        if yes_price == 0 and no_price == 0:
            return None

        # Metaculus doesn't have a direct 'volume' equivalent for trading,
        # but 'number_of_forecasts' can serve as a proxy for activity/liquidity.
        volume = q.get("number_of_forecasts", 0)

        # Expiry is 'close_date' in ISO format
        expiry = q.get("close_date")
        if expiry and "T" in str(expiry):
            expiry = str(expiry)[:10]
        else:
            expiry = "ongoing"

        url = q.get("url")

        # Metaculus uses 'tags' for categorization
        tags = [t["name"] for t in q.get("tags", [])]

        return NormalizedEvent(
            platform="metaculus",
            event_id=str(q.get("id")),
            title=title,
            category=_map_category(tags),
            yes_price=round(yes_price, 4),
            no_price=round(no_price, 4),
            volume=int(volume),
            expiry=expiry,
            url=url,
        )

