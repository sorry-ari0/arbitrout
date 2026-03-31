import asyncio
import time
import logging

from adapters.base import BaseAdapter
from adapters.kalshi import KalshiAdapter
from adapters.polymarket import PolymarketAdapter
from adapters.predictit import PredictItAdapter
from adapters.limitless import LimitlessAdapter
from adapters.coinbase import CoinbaseAdapter
from adapters.manifold import ManifoldAdapter
from adapters.models import NormalizedEvent

try:
    from adapters.metaculus import MetaculusAdapter
except ImportError:  # Optional adapter not present in this checkout
    MetaculusAdapter = None

logger = logging.getLogger("adapter_registry")

class AdapterRegistry:
    def __init__(self):
        self._adapters: dict[str, BaseAdapter] = {}
        self._last_fetch_times: dict[str, float] = {}
        self._status: dict[str, dict] = {}
        self._error_history: dict[str, list[dict]] = {}  # last 50 errors per adapter
        self._consecutive_errors: dict[str, int] = {}
        self._register_adapters()

    def _register_adapters(self):
        # Register instances of all known adapters here
        self.register_adapter(KalshiAdapter())
        self.register_adapter(PolymarketAdapter())
        self.register_adapter(PredictItAdapter())
        self.register_adapter(LimitlessAdapter())
        self.register_adapter(CoinbaseAdapter())
        self.register_adapter(ManifoldAdapter())
        if MetaculusAdapter is not None:
            self.register_adapter(MetaculusAdapter())


    def register_adapter(self, adapter: BaseAdapter):
        self._adapters[adapter.PLATFORM_NAME] = adapter
        self._status[adapter.PLATFORM_NAME] = {
            "name": adapter.PLATFORM_NAME,
            "status": "offline",
            "last_fetch": None,
            "last_error": None,
            "event_count": 0,
            "rate_limit_seconds": adapter.RATE_LIMIT_SECONDS,
        }
        logger.info("Registered adapter: %s", adapter.PLATFORM_NAME)

    async def fetch_all(self) -> list[NormalizedEvent]:
        all_events: list[NormalizedEvent] = []
        tasks = []

        for name, adapter in self._adapters.items():
            tasks.append(self._fetch_single_adapter(name, adapter))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for name, result in zip(self._adapters.keys(), results):
            if isinstance(result, Exception):
                logger.error("Error fetching from %s: %s", name, result)
                self._consecutive_errors[name] = self._consecutive_errors.get(name, 0) + 1
                err_entry = {"error": str(result), "timestamp": time.time(),
                             "consecutive": self._consecutive_errors[name]}
                hist = self._error_history.setdefault(name, [])
                hist.append(err_entry)
                if len(hist) > 50:
                    self._error_history[name] = hist[-50:]
                self._status[name].update(
                    {"status": "error", "last_error": str(result), "event_count": 0}
                )
            else:
                all_events.extend(result)
                self._consecutive_errors[name] = 0
                self._status[name].update(
                    {
                        "status": "online",
                        "last_fetch": time.time(),
                        "last_error": None,
                        "event_count": len(result),
                    }
                )
                self._last_fetch_times[name] = time.time()
        return all_events

    async def _fetch_single_adapter(self, name: str, adapter: BaseAdapter) -> list[NormalizedEvent]:
        # Enforce rate limiting
        last_fetch = self._last_fetch_times.get(name, 0)
        elapsed = time.time() - last_fetch
        if elapsed < adapter.RATE_LIMIT_SECONDS:
            await asyncio.sleep(adapter.RATE_LIMIT_SECONDS - elapsed)

        logger.info("Fetching from %s...", name)
        events = await adapter.fetch_events()
        logger.info("Fetched %d events from %s.", len(events), name)
        return events

    def get_adapter(self, name: str) -> BaseAdapter | None:
        return self._adapters.get(name)

    def get_all_status(self) -> list[dict]:
        now = time.time()
        status_list = []
        for name, status_data in self._status.items():
            current_status = status_data.copy()
            last_fetch_time = current_status.get("last_fetch")
            if last_fetch_time:
                if (now - last_fetch_time) > (current_status["rate_limit_seconds"] * 2):
                    if current_status["status"] == "online":
                        current_status["status"] = "stale"
                current_status["last_fetch_age_seconds"] = int(now - last_fetch_time)
            current_status["consecutive_errors"] = self._consecutive_errors.get(name, 0)
            # Count errors in last 24h
            cutoff_24h = now - 86400
            hist = self._error_history.get(name, [])
            current_status["errors_24h"] = sum(1 for e in hist if e["timestamp"] > cutoff_24h)
            status_list.append(current_status)
        return status_list
