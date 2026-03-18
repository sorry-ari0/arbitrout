"""Crypto spot price adapter — fetches real-time prices from CoinGecko
and compares against prediction market crypto contracts."""
import logging
import math
from .base import BaseAdapter
from .models import NormalizedEvent

logger = logging.getLogger("adapters.crypto_spot")

# CoinGecko IDs for major cryptos
CRYPTO_MAP = {
    "bitcoin": {"symbol": "BTC", "thresholds": [50000, 75000, 100000, 125000, 150000, 200000]},
    "ethereum": {"symbol": "ETH", "thresholds": [2000, 3000, 4000, 5000, 7500, 10000]},
    "solana": {"symbol": "SOL", "thresholds": [100, 150, 200, 300, 500]},
    "dogecoin": {"symbol": "DOGE", "thresholds": [0.10, 0.25, 0.50, 1.00]},
    "ripple": {"symbol": "XRP", "thresholds": [0.50, 1.00, 2.00, 5.00]},
    "cardano": {"symbol": "ADA", "thresholds": [0.50, 1.00, 2.00, 5.00]},
    "avalanche-2": {"symbol": "AVAX", "thresholds": [20, 50, 100, 200]},
    "chainlink": {"symbol": "LINK", "thresholds": [10, 20, 50, 100]},
    "polkadot": {"symbol": "DOT", "thresholds": [5, 10, 20, 50]},
    "polygon-ecosystem-token": {"symbol": "POL", "thresholds": [0.50, 1.00, 2.00, 5.00]},
}


def _implied_probability(current_price: float, threshold: float, volatility: float = 0.6) -> float:
    """Estimate probability of price exceeding threshold using log-normal model.

    Simple Black-Scholes-esque estimate assuming ~6 month horizon.
    volatility is annualized (0.6 = 60% annual vol, typical for crypto).
    """
    if current_price <= 0 or threshold <= 0:
        return 0.5
    # Time horizon: ~0.5 years
    t = 0.5
    sigma_sqrt_t = volatility * math.sqrt(t)
    if sigma_sqrt_t == 0:
        return 1.0 if current_price >= threshold else 0.0

    # d2 from Black-Scholes (assuming risk-neutral, no drift)
    d2 = (math.log(current_price / threshold) + (-0.5 * volatility**2) * t) / sigma_sqrt_t

    # Normal CDF approximation
    return _norm_cdf(d2)


def _norm_cdf(x: float) -> float:
    """Approximate standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


class CryptoSpotAdapter(BaseAdapter):
    """Fetches crypto spot prices from CoinGecko and generates
    implied probability events for comparison with prediction markets."""

    PLATFORM_NAME = "crypto_spot"
    BASE_URL = "https://api.coingecko.com/api/v3"
    RATE_LIMIT_SECONDS = 6.0  # CoinGecko free tier: 10-30 req/min

    async def _fetch(self) -> list[NormalizedEvent]:
        """Fetch crypto prices and generate implied probability events."""
        client = await self._get_client()

        # Fetch prices for all tracked cryptos in one call
        ids = ",".join(CRYPTO_MAP.keys())
        try:
            resp = await client.get(
                f"{self.BASE_URL}/simple/price",
                params={
                    "ids": ids,
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                    "include_24hr_vol": "true",
                },
            )
            resp.raise_for_status()
            prices = resp.json()
        except Exception as exc:
            logger.warning("CoinGecko fetch failed: %s", exc)
            return []

        events: list[NormalizedEvent] = []

        for coin_id, info in CRYPTO_MAP.items():
            price_data = prices.get(coin_id)
            if not price_data:
                continue

            current_price = price_data.get("usd", 0)
            volume_24h = price_data.get("usd_24h_vol", 0)
            if not current_price:
                continue

            symbol = info["symbol"]

            # Generate events for each threshold
            for threshold in info["thresholds"]:
                prob = _implied_probability(current_price, threshold)

                # Format threshold for display
                if threshold >= 1000:
                    thresh_str = f"${threshold:,.0f}"
                elif threshold >= 1:
                    thresh_str = f"${threshold:.2f}"
                else:
                    thresh_str = f"${threshold:.4f}"

                direction = "above" if current_price < threshold else "already above"
                title = f"Will {symbol} exceed {thresh_str} by end of 2026?"

                events.append(NormalizedEvent(
                    platform="crypto_spot",
                    event_id=f"crypto-{symbol.lower()}-{threshold}",
                    title=title,
                    category="crypto",
                    yes_price=round(prob, 4),
                    no_price=round(1.0 - prob, 4),
                    volume=int(volume_24h) if volume_24h else 0,
                    expiry="2026-12-31",
                    url=f"https://www.coingecko.com/en/coins/{coin_id}",
                ))

        logger.info("CryptoSpot: generated %d implied-probability events from %d coins",
                     len(events), len(prices))
        return events
