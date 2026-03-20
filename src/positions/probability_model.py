"""Consensus probability model — aggregates prices across platforms."""
import time


class ProbabilityModel:
    """Volume-weighted consensus probability from matched events."""

    def __init__(self):
        self._cache: dict[str, dict] = {}

    def update_from_matched_events(self, matched_events: list[dict]):
        """Build consensus from matched events with 2+ platforms."""
        for event in matched_events:
            title = event.get("canonical_title", "")
            if not title:
                continue
            markets = event.get("markets", [])
            if len(markets) < 2:
                continue

            prices, volumes = [], []
            for m in markets:
                yes = m.get("yes_price", 0)
                vol = m.get("volume", 1)
                if 0 < yes < 1:
                    prices.append(yes)
                    volumes.append(max(vol, 1))

            if len(prices) < 2:
                continue

            total_vol = sum(volumes)
            consensus = sum(p * v for p, v in zip(prices, volumes)) / total_vol

            deviations = []
            for p, m in zip(prices, markets):
                dev = abs(p - consensus)
                if dev > 0.10:
                    deviations.append({
                        "platform": m.get("platform", ""),
                        "price": p,
                        "deviation": round(dev, 3),
                    })

            self._cache[title] = {
                "consensus_yes": round(consensus, 4),
                "platform_count": len(prices),
                "max_deviation": round(max(abs(p - consensus) for p in prices), 4),
                "deviations": deviations,
                "updated_at": time.time(),
            }

    def get_consensus(self, title: str) -> dict | None:
        return self._cache.get(title)

    def has_deviation(self, title: str, threshold: float = 0.10) -> bool:
        data = self._cache.get(title)
        if not data:
            return False
        return data.get("max_deviation", 0) > threshold
