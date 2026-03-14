"""Coinbase prediction markets — tags Kalshi markets available on Coinbase,
with Scrapling fallback for direct scraping."""
import logging
from .base import BaseAdapter
from .models import NormalizedEvent

logger = logging.getLogger("adapters.coinbase")


# ============================================================
# COINBASE ADAPTER
# ============================================================
class CoinbaseAdapter(BaseAdapter):
    """Coinbase prediction markets (Kalshi-powered).

    Strategy: Fetch from Kalshi API and relabel markets that appear
    on Coinbase. Falls back to scraping coinbase.com if Kalshi fails.
    """

    PLATFORM_NAME = "coinbase"
    BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"
    RATE_LIMIT_SECONDS = 2.0

    # ============================================================
    # FETCH IMPLEMENTATION
    # ============================================================
    async def _fetch(self) -> list[NormalizedEvent]:
        events = await self._fetch_via_kalshi()
        if not events:
            events = await self._fetch_via_scrape()
        return events

    async def _fetch_via_kalshi(self) -> list[NormalizedEvent]:
        """Fetch Kalshi markets and relabel as Coinbase."""
        try:
            client = await self._get_client()
            resp = await client.get(
                f"{self.BASE_URL}/markets",
                params={"limit": 100, "status": "open"},
            )
            resp.raise_for_status()
            data = resp.json()
            markets = data.get("markets", [])

            events: list[NormalizedEvent] = []
            for m in markets:
                title = m.get("title", m.get("subtitle", ""))
                ticker = m.get("ticker", m.get("id", ""))
                yes_price = (m.get("yes_ask", 0) or 0) / 100.0
                no_price = (m.get("no_ask", 0) or 0) / 100.0
                if yes_price == 0 and m.get("yes_bid"):
                    yes_price = m["yes_bid"] / 100.0
                if no_price == 0 and yes_price > 0:
                    no_price = 1.0 - yes_price

                volume = m.get("volume", 0) or 0
                expiry = m.get("expiration_time", "ongoing")
                if expiry and "T" in str(expiry):
                    expiry = str(expiry)[:10]

                events.append(NormalizedEvent(
                    platform="coinbase",
                    event_id=f"cb-{ticker}",
                    title=title,
                    category="culture",  # refined by matcher
                    yes_price=round(yes_price, 4),
                    no_price=round(no_price, 4),
                    volume=int(volume),
                    expiry=expiry or "ongoing",
                    url="https://www.coinbase.com/prediction-markets",
                ))
            return events

        except Exception as exc:
            logger.warning("Coinbase/Kalshi fetch failed: %s", exc)
            return []

    async def _fetch_via_scrape(self) -> list[NormalizedEvent]:
        """Fallback: scrape Coinbase prediction markets with Scrapling."""
        import asyncio
        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._scrape_sync
            )
        except Exception as exc:
            logger.warning("Coinbase scrape failed: %s", exc)
            return []

    def _scrape_sync(self) -> list[NormalizedEvent]:
        """Sync scrape of Coinbase."""
        try:
            from scrapling import Fetcher
        except ImportError:
            return []

        events: list[NormalizedEvent] = []
        try:
            fetcher = Fetcher(auto_match=True)
            page = fetcher.get("https://www.coinbase.com/prediction-markets")
            if not page or page.status_code != 200:
                return []

            cards = page.css('[class*="market"], [class*="prediction"], a[href*="prediction"]')
            for card in cards[:50]:
                try:
                    title = card.text.strip()[:120]
                    if not title or len(title) < 5:
                        continue
                    events.append(NormalizedEvent(
                        platform="coinbase",
                        event_id=f"cb-scrape-{len(events)}",
                        title=title,
                        category="culture",
                        yes_price=0.5,
                        no_price=0.5,
                        volume=0,
                        expiry="ongoing",
                        url="https://www.coinbase.com/prediction-markets",
                    ))
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("Coinbase scrape error: %s", exc)

        return events
