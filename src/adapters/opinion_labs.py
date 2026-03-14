"""Opinion Labs adapter — REST API with apikey header auth, 15 req/sec."""
import os
from .base import BaseAdapter
from .models import NormalizedEvent


# ============================================================
# CATEGORY MAPPING
# ============================================================
def _guess_category(title: str, category_raw: str = "") -> str:
    text = f"{title} {category_raw}".lower()
    if any(w in text for w in ["president", "election", "congress", "trump", "biden", "vote", "political"]):
        return "politics"
    if any(w in text for w in ["bitcoin", "crypto", "ethereum", "btc"]):
        return "crypto"
    if any(w in text for w in ["gdp", "inflation", "fed", "rate", "economy", "recession"]):
        return "economics"
    if any(w in text for w in ["weather", "hurricane", "temperature", "climate"]):
        return "weather"
    if any(w in text for w in ["nfl", "nba", "mlb", "soccer", "sports"]):
        return "sports"
    return "culture"


# ============================================================
# OPINION LABS ADAPTER
# ============================================================
class OpinionLabsAdapter(BaseAdapter):
    """Fetch markets from Opinion Labs (opinion.trade) API."""

    PLATFORM_NAME = "opinion_labs"
    BASE_URL = "https://proxy.opinion.trade:8443/openapi"
    RATE_LIMIT_SECONDS = 0.1  # 15 req/sec allowed

    def __init__(self):
        super().__init__()
        self._api_key = os.environ.get("OPINION_LABS_API_KEY", "")

    # ============================================================
    # FETCH IMPLEMENTATION
    # ============================================================
    async def _fetch(self) -> list[NormalizedEvent]:
        client = await self._get_client()
        headers = {}
        if self._api_key:
            headers["apikey"] = self._api_key

        events: list[NormalizedEvent] = []

        # Try /markets endpoint
        resp = await client.get(
            f"{self.BASE_URL}/markets",
            headers=headers,
            params={"status": "active", "limit": 100},
        )
        resp.raise_for_status()
        data = resp.json()

        markets = data if isinstance(data, list) else data.get("markets", data.get("data", []))

        for m in markets:
            ev = self._normalize(m)
            if ev:
                events.append(ev)

        return events

    # ============================================================
    # NORMALIZATION
    # ============================================================
    def _normalize(self, m: dict) -> NormalizedEvent | None:
        """Convert Opinion Labs market to NormalizedEvent."""
        title = m.get("title", m.get("question", m.get("name", "")))
        if not title:
            return None

        market_id = str(m.get("id", m.get("marketId", "")))

        # Prices
        yes_price = 0.0
        no_price = 0.0

        if "yesPrice" in m:
            yes_price = float(m["yesPrice"])
        elif "probability" in m:
            yes_price = float(m["probability"])
        elif "lastPrice" in m:
            yes_price = float(m["lastPrice"])

        if "noPrice" in m:
            no_price = float(m["noPrice"])
        elif yes_price > 0:
            no_price = 1.0 - yes_price

        volume = 0
        raw_vol = m.get("volume", m.get("totalVolume", 0))
        try:
            volume = int(float(raw_vol or 0))
        except (ValueError, TypeError):
            pass

        expiry = m.get("closeDate", m.get("endDate", m.get("expiresAt", "ongoing")))
        if expiry and "T" in str(expiry):
            expiry = str(expiry)[:10]
        elif not expiry:
            expiry = "ongoing"

        category_raw = m.get("category", m.get("tags", ""))
        if isinstance(category_raw, list):
            category_raw = " ".join(category_raw)

        slug = m.get("slug", market_id)

        return NormalizedEvent(
            platform="opinion_labs",
            event_id=market_id,
            title=title,
            category=_guess_category(title, str(category_raw)),
            yes_price=round(yes_price, 4),
            no_price=round(no_price, 4),
            volume=volume,
            expiry=str(expiry),
            url=f"https://opinion.trade/markets/{slug}",
        )
