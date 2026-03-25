"""Adapter registry — centralizes all platform adapters."""
import asyncio
import logging
import time
from typing import Type

from httpx import AsyncClient

from .base import BaseAdapter
from .kalshi import KalshiAdapter
from .limitless import LimitlessAdapter
from .polymarket import PolymarketAdapter
from .predictit import PredictItAdapter
from .manifold import ManifoldAdapter  # NEW: Import ManifoldAdapter

logger = logging.getLogger("adapter_registry")


# ============================================================
# REGISTRY
# ============================================================
class AdapterRegistry:
    """Manages all available platform adapters and their polling."""

    def __init__(self):
        self._adapters: list[BaseAdapter] = []
        self._register_adapters()
        self._shared_client: AsyncClient | None = None
        self._last_fetch_times: dict[str, float] = {}  # platform_name -> timestamp
        self._status: dict[str, dict] = {} # platform_name -> {status: "ok"/"error", message: ""}

    def _register_adapters(self):
        """Initializes and registers all known adapters."""
        # NEW: Add ManifoldAdapter to the list of registered adapters
        adapter_classes: list[Type[BaseAdapter]] = [
            PolymarketAdapter,
            PredictItAdapter,
            KalshiAdapter,
            LimitlessAdapter,
            ManifoldAdapter,
        ]
        for AdapterClass in adapter_classes:
            try:
                adapter = AdapterClass()
                self._adapters.append(adapter)
                logger.info("Registered adapter: %s", adapter.PLATFORM_NAME)
            except Exception as e:
                logger.error("Failed to register adapter %s: %s", AdapterClass.PLATFORM_NAME, e)
                self._status[AdapterClass.PLATFORM_NAME] = {"status": "error", "message": str(e)}

    async def _get_shared_client(self) -> AsyncClient:
        """Get a shared httpx client instance."""
        if self._shared_client is None:
            self._shared_client = AsyncClient(timeout=20.0)
        return self._shared_client

    async def fetch_all(self) -> list:
        """Fetch events from all registered adapters concurrently, respecting rate limits."""
        all_events = []
        tasks = []
        for adapter in self._adapters:
            tasks.append(self._fetch_one_adapter(adapter))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for adapter, res in zip(self._adapters, results):
            if isinstance(res, Exception):
                logger.error("Error fetching from %s: %s", adapter.PLATFORM_NAME, res)
                self._status[adapter.PLATFORM_NAME] = {"status": "error", "message": str(res)}
            else:
                all_events.extend(res)
                self._status[adapter.PLATFORM_NAME] = {"status": "ok", "message": f"{len(res)} events fetched"}

        return all_events

    async def _fetch_one_adapter(self, adapter: BaseAdapter) -> list:
        """Fetch events for a single adapter, respecting its rate limit."""
        platform_name = adapter.PLATFORM_NAME
        last_fetch = self._last_fetch_times.get(platform_name, 0)
        time_since_last_fetch = time.time() - last_fetch
        sleep_needed = adapter.RATE_LIMIT_SECONDS - time_since_last_fetch

        if sleep_needed > 0:
            await asyncio.sleep(sleep_needed)

        # Ensure adapter uses the shared client
        adapter._set_client(await self._get_shared_client())
        events = await adapter._fetch()
        self._last_fetch_times[platform_name] = time.time()
        return events

    def get_all_status(self) -> list[dict]:
        """Return the status of all adapters."""
        status_list = []
        for adapter in self._adapters:
            platform_name = adapter.PLATFORM_NAME
            last_fetch = self._last_fetch_times.get(platform_name, 0)
            status_entry = self._status.get(platform_name, {"status": "unknown", "message": "No fetch attempted"})
            status_list.append({
                "platform": platform_name,
                "status": status_entry["status"],
                "message": status_entry["message"],
                "last_fetch": last_fetch,
                "rate_limit_seconds": adapter.RATE_LIMIT_SECONDS,
            })
        return status_list

    async def close(self):
        """Close the shared HTTP client."""
        if self._shared_client:
            await self._shared_client.aclose()
            self._shared_client = None

