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
    RATE_LIMIT_SECONDS = 0.5  # API requires 300ms minimum between calls

    # ============================================================
    # FETCH IMPLEMENTATION
    # ============================================================
    async def _fetch(self) -> list[NormalizedEvent]:
        client = await self._get_client()
        events: list[NormalizedEvent] = []

        # API max limit is 25 per page, paginate to get more
        for page in range(1, 9):  # up to 200 markets (8 pages x 25)
            import asyncio
            resp = await client.get(
                f"{self.BASE_URL}/markets/active",
                params={"limit": 25, "page": page},
            )
            resp.raise_for_status()
            raw = resp.json()

            if not isinstance(raw, list):
                markets = raw.get("data", raw.get("markets", []))
            else:
                markets = raw

            if not markets:
                break

            for m in markets:
                ev = self._normalize(m)
                if ev:
                    events.append(ev)

            # Rate limit between pages
            await asyncio.sleep(0.3)

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

        # Prices — API returns prices: [yesPrice, noPrice]
        yes_price = 0.0
        no_price = 0.0

        prices = m.get("prices")
        if isinstance(prices, list) and len(prices) >= 2:
            try:
                yes_price = float(prices[0])
                no_price = float(prices[1])
            except (ValueError, TypeError):
                pass
        elif "probability" in m:
            try:
                yes_price = float(m.get("probability", 0.0))
                no_price = 1.0 - yes_price
            except (ValueError, TypeError):
                pass
        elif "yes_price" in m:
            try:
                yes_price = float(m.get("yes_price", 0.0))
                no_price = float(m.get("no_price", 1.0 - yes_price))
            except (ValueError, TypeError):
                pass

        # Volume — volumeFormatted is human-readable USDC
        volume = 0
        vol_formatted = m.get("volumeFormatted")
        if vol_formatted:
            try:
                volume = int(float(vol_formatted))
            except (ValueError, TypeError):
                pass
        if volume == 0:
            raw_vol = m.get("volume", 0)
            try:
                volume = int(float(raw_vol or 0) / 1_000_000)  # raw is 6-decimal USDC
            except (ValueError, TypeError):
                pass

        # Expiry
        expiry = m.get("expirationDate", m.get("closeDate", m.get("endDate", "ongoing")))
        if expiry and "T" in str(expiry):
            expiry = str(expiry)[:10]
        elif not expiry:
            expiry = "ongoing"

        slug = m.get("slug", market_id)
        tags = m.get("tags", [])
        categories = m.get("categories", [])
        if categories and not tags:
            tags = categories

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
