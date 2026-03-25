"""Utility to fetch current Forex rates from a public API."""
import logging
import time
from typing import Dict
import httpx

logger = logging.getLogger("forex_rates")

# Replace with a real API key if needed for more requests, or use a free tier one.
# ExchangeRate-API.com offers a free tier (1500 requests/month) without an API key for base USD.
EXCHANGE_RATE_API_BASE = "https://open.er-api.com/v6/latest/USD"

_forex_cache: Dict = {}
_last_fetch_time: float = 0
_CACHE_TTL_SECONDS = 3600  # Cache rates for 1 hour

async def fetch_forex_rates() -> Dict:
    """Fetches current forex rates with USD as base, caches results."""
    global _forex_cache, _last_fetch_time

    if time.time() - _last_fetch_time < _CACHE_TTL_SECONDS:
        return _forex_cache

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(EXCHANGE_RATE_API_BASE)
            response.raise_for_status()
            data = response.json()

            if data.get("result") == "success" and "rates" in data:
                _forex_cache = {
                    **data["rates"],
                    "time_last_update_unix": data.get("time_last_update_unix", time.time())
                }
                _last_fetch_time = time.time()
                logger.info("Successfully fetched latest forex rates.")
                return _forex_cache
            else:
                logger.error("Forex API returned an error: %s", data.get("error", "Unknown error"))
                return {}
    except httpx.HTTPStatusError as e:
        logger.error("HTTP error fetching forex rates: %s", e)
    except httpx.RequestError as e:
        logger.error("Network error fetching forex rates: %s", e)
    except Exception as e:
        logger.error("Unexpected error fetching forex rates: %s", e)
    
    # Return stale cache or empty if fetch failed
    return _forex_cache
