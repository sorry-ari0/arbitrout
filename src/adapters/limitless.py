"""Limitless Exchange adapter — public REST API, no auth."""
from .base import BaseAdapter
from .models import NormalizedEvent


# ============================================================
# CATEGORY MAPPING
# ============================================================
def _guess_category(title: str, tags: list | None) -> str:
    """Guess category from title and tags."""
    text = title.lower()
    if tags:
        text += " " + " ".join(str(t).lower() for t in tags)
    if any(w in text for w in ["president", "election", "congress", "trump", "biden", "political", "vote"]):
        return "politics"
    if any(w in text for w in ["bitcoin", "crypto", "ethereum", "btc", "eth"]):
        return "crypto"
    if any(w in text for w in ["gdp", "inflation", "fed", "rate", "recession", "economy"]):
        return "economics"
    if any(w in text for w in ["weather", "hurricane", "temperature"]):
        return "weather"
    if any(w in text for w in ["nfl", "nba", "mlb", "nhl", "soccer", "football", "basketball"]):
        return "sports"
    return "culture"


# ============================================================
# LIMITLESS ADAPTER
# ============================================================
class LimitlessAdapter(BaseAdapter):
    """Fetch markets from Limitless Exchange API."""

    PLATFORM_NAME = "limitless"
    BASE_URL = "https://api.limitless.exchange"
    RATE_LIMIT_SECONDS = 1.0

    # ============================================================
    # FETCH IMPLEMENTATION
    # ============================================================
    async def _fetch(self) -> list[NormalizedEvent]:
        client = await self._get_client()
        events: list[NormalizedEvent] = []

        # Step 1: get active market list
        resp = await client.get(f"{self.BASE_URL}/markets/browse-active")
        resp.raise_for_status()
        markets = resp.json()

        if not isinstance(markets, list):
            markets = markets.get("markets", markets.get("data", []))

        for m in markets:
            ev = self._normalize(m)
            if ev:
                events.append(ev)

        return events

    # ============================================================
    # NORMALIZATION
    # ============================================================
    def _normalize(self, m: dict) -> NormalizedEvent | None:
        """Convert Limitless market to NormalizedEvent."""
        title = m.get("title", m.get("question", ""))
        if not title:
            return None

        market_id = str(m.get("id", m.get("slug", "")))

        # Prices — Limitless uses probability or price fields
        yes_price = 0.0
        no_price = 0.0

        if "probability" in m:
            yes_price = float(m["probability"])
            no_price = 1.0 - yes_price
        elif "yes_price" in m:
            yes_price = float(m["yes_price"])
            no_price = float(m.get("no_price", 1.0 - yes_price))
        elif "lastPrice" in m:
            yes_price = float(m["lastPrice"])
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

        slug = m.get("slug", market_id)
        tags = m.get("tags", [])

        return NormalizedEvent(
            platform="limitless",
            event_id=market_id,
            title=title,
            category=_guess_category(title, tags),
            yes_price=round(yes_price, 4),
            no_price=round(no_price, 4),
            volume=volume,
            expiry=str(expiry),
            url=f"https://limitless.exchange/markets/{slug}",
        )
