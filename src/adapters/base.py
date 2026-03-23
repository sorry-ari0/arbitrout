"""Base adapter — abstract class all platform adapters inherit from."""
import logging
import time
from abc import ABC, abstractmethod

import httpx

from .models import NormalizedEvent


# ============================================================
# BASE ADAPTER
# ============================================================
class BaseAdapter(ABC):
    """Abstract base for all prediction market platform adapters."""

    PLATFORM_NAME: str = ""
    BASE_URL: str = ""
    RATE_LIMIT_SECONDS: float = 1.0  # min seconds between requests
    CACHE_TTL_SECONDS: float = 30.0  # return cached results if fresher than this

    def __init__(self):
        self.logger = logging.getLogger(f"adapters.{self.PLATFORM_NAME}")
        self._last_request_time: float = 0
        self._cache: list[NormalizedEvent] = []
        self._cache_time: float = 0
        self._status: str = "idle"  # idle, fetching, ok, error, blocked
        self._error_msg: str = ""
        self._client: httpx.AsyncClient | None = None

    # ============================================================
    # PUBLIC INTERFACE
    # ============================================================
    async def fetch_events(self) -> list[NormalizedEvent]:
        """Fetch events with caching and error handling."""
        # Return cached results if still fresh
        if self._cache and (time.time() - self._cache_time) < self.CACHE_TTL_SECONDS:
            return self._cache

        self._status = "fetching"
        try:
            # Rate limiting
            now = time.time()
            wait = self.RATE_LIMIT_SECONDS - (now - self._last_request_time)
            if wait > 0:
                import asyncio
                await asyncio.sleep(wait)

            events = await self._fetch()
            self._last_request_time = time.time()
            self._cache = events
            self._cache_time = time.time()
            self._status = "ok"
            self._error_msg = ""
            self.logger.info("Fetched %d events from %s", len(events), self.PLATFORM_NAME)
            return events
        except Exception as exc:
            self._status = "error"
            self._error_msg = str(exc)[:200]
            self.logger.warning("Fetch failed for %s: %s", self.PLATFORM_NAME, exc)
            return self._cache  # return stale cache on error

    def get_cached(self) -> list[NormalizedEvent]:
        """Return last cached events without fetching."""
        return self._cache

    def get_status(self) -> dict:
        """Return adapter status for /api/arbitrage/platforms."""
        return {
            "platform": self.PLATFORM_NAME,
            "status": self._status,
            "cached_events": len(self._cache),
            "cache_age_seconds": round(time.time() - self._cache_time, 1) if self._cache_time else None,
            "error": self._error_msg or None,
        }

    # ============================================================
    # HTTP CLIENT
    # ============================================================
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create a shared httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                follow_redirects=True,
                headers={"User-Agent": "Arbitrout/1.0"},
            )
        return self._client

    async def close(self):
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ============================================================
    # ABSTRACT — subclasses implement this
    # ============================================================
    @abstractmethod
    async def _fetch(self) -> list[NormalizedEvent]:
        """Fetch and normalize events from the platform. Must be overridden."""
        ...
