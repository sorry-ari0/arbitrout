"""Adapter registry — manages all platform adapters."""
import asyncio
import logging

from .base import BaseAdapter
from .models import NormalizedEvent

logger = logging.getLogger("adapters.registry")


# ============================================================
# ADAPTER REGISTRY
# ============================================================
class AdapterRegistry:
    """Central registry for all platform adapters."""

    def __init__(self):
        self._adapters: dict[str, BaseAdapter] = {}

    def register(self, adapter: BaseAdapter):
        """Register a platform adapter."""
        name = adapter.PLATFORM_NAME
        if not name:
            raise ValueError("Adapter must have a PLATFORM_NAME")
        self._adapters[name] = adapter
        logger.info("Registered adapter: %s", name)

    def get(self, platform: str) -> BaseAdapter | None:
        """Get adapter by platform name."""
        return self._adapters.get(platform)

    def list_platforms(self) -> list[str]:
        """Return list of registered platform names."""
        return list(self._adapters.keys())

    async def fetch_all(self) -> list[NormalizedEvent]:
        """Fetch events from ALL adapters concurrently."""
        if not self._adapters:
            return []

        tasks = [
            adapter.fetch_events()
            for adapter in self._adapters.values()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_events: list[NormalizedEvent] = []
        for i, result in enumerate(results):
            name = list(self._adapters.keys())[i]
            if isinstance(result, Exception):
                logger.warning("Adapter %s raised: %s", name, result)
            elif isinstance(result, list):
                all_events.extend(result)

        logger.info("Total events fetched: %d from %d adapters",
                     len(all_events), len(self._adapters))
        return all_events

    def get_all_cached(self) -> list[NormalizedEvent]:
        """Return cached events from all adapters (no network)."""
        events: list[NormalizedEvent] = []
        for adapter in self._adapters.values():
            events.extend(adapter.get_cached())
        return events

    def get_all_status(self) -> list[dict]:
        """Return status of all adapters."""
        return [a.get_status() for a in self._adapters.values()]

    async def close_all(self):
        """Close all adapter HTTP clients."""
        for adapter in self._adapters.values():
            await adapter.close()
