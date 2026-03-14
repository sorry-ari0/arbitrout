"""Robinhood prediction markets — scrape with Scrapling (no API)."""
import logging
from .base import BaseAdapter
from .models import NormalizedEvent

logger = logging.getLogger("adapters.robinhood")


# ============================================================
# CATEGORY MAPPING
# ============================================================
def _guess_category(title: str) -> str:
    text = title.lower()
    if any(w in text for w in ["president", "election", "congress", "trump", "biden", "vote"]):
        return "politics"
    if any(w in text for w in ["bitcoin", "crypto", "ethereum", "btc"]):
        return "crypto"
    if any(w in text for w in ["gdp", "inflation", "fed", "rate", "recession"]):
        return "economics"
    if any(w in text for w in ["weather", "hurricane", "temperature"]):
        return "weather"
    if any(w in text for w in ["nfl", "nba", "mlb", "nhl", "sports"]):
        return "sports"
    return "culture"


# ============================================================
# ROBINHOOD ADAPTER
# ============================================================
class RobinhoodAdapter(BaseAdapter):
    """Scrape Robinhood prediction markets with Scrapling."""

    PLATFORM_NAME = "robinhood"
    BASE_URL = "https://robinhood.com/prediction-markets/"
    RATE_LIMIT_SECONDS = 30.0  # be polite with scraping

    # ============================================================
    # FETCH IMPLEMENTATION
    # ============================================================
    async def _fetch(self) -> list[NormalizedEvent]:
        import asyncio
        # Run blocking Scrapling in thread
        events = await asyncio.get_event_loop().run_in_executor(
            None, self._scrape_sync
        )
        return events

    def _scrape_sync(self) -> list[NormalizedEvent]:
        """Synchronous scrape of Robinhood prediction markets page."""
        try:
            from scrapling import Fetcher
        except ImportError:
            logger.warning("scrapling not installed — Robinhood adapter disabled")
            return []

        events: list[NormalizedEvent] = []
        try:
            fetcher = Fetcher(auto_match=True)
            page = fetcher.get(self.BASE_URL)

            if not page or not page.status_code or page.status_code != 200:
                logger.warning("Robinhood scrape returned status %s", getattr(page, 'status_code', 'N/A'))
                return []

            # Look for market cards — Robinhood renders markets as cards/rows
            # Selector may need updating as Robinhood changes their DOM
            cards = page.css('[data-testid*="market"], .market-card, [class*="prediction"], [class*="market-row"]')
            if not cards:
                # Fallback: try finding any structured data
                cards = page.css('a[href*="/prediction-markets/"]')

            for card in cards:
                try:
                    title_el = card.css_first('h2, h3, [class*="title"], [class*="question"]')
                    title = title_el.text.strip() if title_el else card.text.strip()[:120]
                    if not title or len(title) < 5:
                        continue

                    # Try to extract price/probability
                    price_el = card.css_first('[class*="price"], [class*="probability"], [class*="percent"]')
                    yes_price = 0.5  # default
                    if price_el:
                        price_text = price_el.text.strip().replace('%', '').replace('$', '').replace('\u00a2', '')
                        try:
                            val = float(price_text)
                            yes_price = val / 100.0 if val > 1 else val
                        except ValueError:
                            pass

                    # Extract href for URL
                    href = card.attrib.get("href", "")
                    url = f"https://robinhood.com{href}" if href.startswith("/") else self.BASE_URL

                    slug = href.split("/")[-1] if href else title[:30].replace(" ", "-").lower()

                    events.append(NormalizedEvent(
                        platform="robinhood",
                        event_id=f"rh-{slug}",
                        title=title,
                        category=_guess_category(title),
                        yes_price=round(yes_price, 4),
                        no_price=round(1.0 - yes_price, 4),
                        volume=0,  # volume not available via scrape
                        expiry="ongoing",
                        url=url,
                    ))
                except Exception as exc:
                    logger.debug("Failed to parse Robinhood card: %s", exc)
                    continue

        except Exception as exc:
            logger.warning("Robinhood scrape failed: %s", exc)

        return events
