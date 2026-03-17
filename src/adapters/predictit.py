"""PredictIt adapter — public JSON API, no auth, ~1 req/min rate limit."""
from .base import BaseAdapter
from .models import NormalizedEvent
import logging
import asyncio

# ============================================================
# CATEGORY MAPPING
# ============================================================
def _guess_category(name: str, short_name: str) -> str:
    """Guess category from market name text."""
    text = f"{name} {short_name}".lower()
    if any(w in text for w in ["president", "election", "congress", "senate", "governor", "party", "vote", "democrat", "republican", "biden", "trump"]):
        return "politics"
    if any(w in text for w in ["bitcoin", "crypto", "ethereum", "btc"]):
        return "crypto"
    if any(w in text for w in ["gdp", "fed", "inflation", "unemployment", "interest rate", "recession"]):
        return "economics"
    if any(w in text for w in ["weather", "hurricane", "temperature", "climate"]):
        return "weather"
    if any(w in text for w in ["nfl", "nba", "mlb", "nhl", "super bowl", "world cup", "olympics"]):
        return "sports"
    return "politics"  # PredictIt is mostly political


# ============================================================
# PREDICTIT ADAPTER
# ============================================================
class PredictItAdapter(BaseAdapter):
    """Fetch all markets from PredictIt public API."""

    PLATFORM_NAME = "predictit"
    BASE_URL = "https://www.predictit.org/api/marketdata/all/"
    RATE_LIMIT_SECONDS = 60.0  # PredictIt rate limits to ~1 req/min

    # ============================================================
    # FETCH IMPLEMENTATION
    # ============================================================
    async def _fetch(self) -> list[NormalizedEvent]:
        client = await self._get_client()
        max_retries = 3
        delay = 2
        for attempt in range(max_retries + 1):
            try:
                resp = await client.get(self.BASE_URL)
                resp.raise_for_status()
                break
            except Exception as e:
                if attempt < max_retries:
                    logging.warning(f"Failed to fetch data (attempt {attempt + 1}/{max_retries + 1}): {str(e)}. Retrying in {delay} seconds...")
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    logging.error(f"Failed to fetch data after {max_retries + 1} attempts: {str(e)}")
                    raise
        data = resp.json()

        events: list[NormalizedEvent] = []
        markets = data.get("markets", [])

        for market in markets:
            contracts = market.get("contracts", [])
            market_name = market.get("name", "")
            market_id = market.get("id", "")
            market_url = market.get("url", f"https://www.predictit.org/markets/detail/{market_id}")

            for contract in contracts:
                status = contract.get("status", "")
                if status != "Open":
                    continue

                yes_price = contract.get("lastTradePrice", 0) or 0
                best_yes = contract.get("bestBuyYesCost", 0) or 0
                best_no = contract.get("bestBuyNoCost", 0) or 0

                # Prefer bestBuy costs (actual order book)
                if best_yes > 0:
                    yes_price = best_yes
                no_price = best_no if best_no > 0 else (1.0 - yes_price)

                volume = contract.get("totalSharesTraded", 0) or 0
                end_date = market.get("dateEnd", "ongoing")
                if end_date and end_date != "N/A" and "T" in str(end_date):
                    end_date = str(end_date)[:10]
                elif not end_date or end_date == "N/A":
                    end_date = "ongoing"

                contract_name = contract.get("name", contract.get("shortName", ""))
                title = f"{market_name}: {contract_name}" if contract_name != market_name else market_name

                events.append(NormalizedEvent(
                    platform="predictit",
                    event_id=str(contract.get("id", market_id)),
                    title=title,
                    category=_guess_category(market_name, contract_name),
                    yes_price=round(float(yes_price), 4),
                    no_price=round(float(no_price), 4),
                    volume=int(volume),
                    expiry=end_date,
                    url=market_url,
                ))

        return events
