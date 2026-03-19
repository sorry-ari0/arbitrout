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
        """Synchronous scrape of Robinhood prediction markets page.

        NOTE: Robinhood is a JS-rendered SPA. Scrapling Fetcher gets an empty
        shell (~8KB). We try StealthyFetcher (Playwright-based) first for
        full rendering, then fall back to basic Fetcher.
        """
        events: list[NormalizedEvent] = []

        # Try Playwright-based StealthyFetcher first (renders JS)
        try:
            from scrapling import StealthyFetcher
            fetcher = StealthyFetcher()
            page = fetcher.fetch(self.BASE_URL, wait_selector='[class*="market"], [class*="prediction"]', timeout=15000)
            events = self._parse_page(page)
            if events:
                return events
        except ImportError:
            logger.debug("scrapling StealthyFetcher not available — trying basic Fetcher")
        except Exception as exc:
            logger.debug("Robinhood StealthyFetcher failed: %s", exc)

        # Fallback: basic Fetcher (likely returns empty page for SPA)
        try:
            from scrapling import Fetcher
            fetcher = Fetcher(auto_match=True)
            page = fetcher.get(self.BASE_URL)
            status = getattr(page, 'status', None) or getattr(page, 'status_code', None)
            if not page or (status and status != 200):
                logger.warning("Robinhood scrape returned status %s", status)
                return []
            events = self._parse_page(page)
        except ImportError:
            logger.warning("scrapling not installed — Robinhood adapter disabled")
        except Exception as exc:
            logger.warning("Robinhood scrape failed: %s", exc)

        if not events:
            logger.info("Robinhood: 0 events (JS-rendered SPA — needs Playwright for scrapling.StealthyFetcher)")

        return events

    def _parse_page(self, page) -> list[NormalizedEvent]:
        """Parse a scrapled page for Robinhood market data."""
        events: list[NormalizedEvent] = []

        # Try multiple selector strategies
        cards = page.css('[data-testid*="market"], .market-card, [class*="prediction"], [class*="market-row"]')
        if not cards:
            cards = page.css('a[href*="/prediction-markets/"]')

        for card in cards:
            try:
                title_el = card.css_first('h2, h3, [class*="title"], [class*="question"]')
                title = title_el.text.strip() if title_el else card.text.strip()[:120]
                if not title or len(title) < 5:
                    continue

                price_el = card.css_first('[class*="price"], [class*="probability"], [class*="percent"]')
                yes_price = 0.5
                if price_el:
                    price_text = price_el.text.strip().replace('%', '').replace('$', '').replace('\u00a2', '')
                    try:
                        val = float(price_text)
                        yes_price = val / 100.0 if val > 1 else val
                    except ValueError:
                        pass

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
                    volume=0,
                    expiry="ongoing",
                    url=url,
                ))
            except Exception as exc:
                logger.debug("Failed to parse Robinhood card: %s", exc)
                continue

        return events
