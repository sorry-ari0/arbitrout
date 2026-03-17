"""Arbitrage engine — finds cross-platform spread opportunities."""
import json
import logging
import time
from pathlib import Path

from adapters.models import NormalizedEvent, MatchedEvent, ArbitrageOpportunity
from adapters.registry import AdapterRegistry
from event_matcher import match_events

logger = logging.getLogger("arbitrage_engine")

DATA_DIR = Path(__file__).parent / "data" / "arbitrage"


# ============================================================
# ARBITRAGE CALCULATOR
# ============================================================
def find_arbitrage(matched: list[MatchedEvent],
                   min_spread: float = 0.0,
                   min_volume: int = 0) -> list[ArbitrageOpportunity]:
    """Find arbitrage opportunities across matched events.

    For each MatchedEvent with markets on >=2 platforms:
      best_yes = min(yes_price) across platforms
      best_no  = min(no_price) across platforms
      spread   = 1.0 - (best_yes + best_no)
      If spread > 0 => arbitrage exists.
    """
    opportunities: list[ArbitrageOpportunity] = []

    for match in matched:
        if match.platform_count < 2:
            continue

        markets = match.markets
        
        max_spread_for_match = -1.0 # Initialize with a value lower than any possible valid spread
        best_yes_market_for_match = None
        best_no_market_for_match = None

        # Iterate through all combinations of two markets to find the optimal cross-platform arbitrage
        # We need to ensure buy_yes_platform != buy_no_platform
        # and maximize the spread.
        for buy_yes_candidate in markets:
            for buy_no_candidate in markets:
                if buy_yes_candidate.platform == buy_no_candidate.platform:
                    continue # Platforms must be distinct for an arbitrage opportunity

                current_spread = 1.0 - (buy_yes_candidate.yes_price + buy_no_candidate.no_price)
                
                if current_spread > max_spread_for_match:
                    max_spread_for_match = current_spread
                    best_yes_market_for_match = buy_yes_candidate
                    best_no_market_for_match = buy_no_candidate
        
        # If no valid cross-platform arbitrage was found for this match (e.g., all markets are on one platform,
        # which should be caught by platform_count < 2, or no profitable distinct pair)
        if best_yes_market_for_match is None or best_no_market_for_match is None:
            continue

        profit_pct = max_spread_for_match * 100.0
        combined_vol = sum(m.volume for m in markets) # Volume for ALL markets in the matched event

        if max_spread_for_match < min_spread:
            continue
        if combined_vol < min_volume:
            continue

        opportunities.append(ArbitrageOpportunity(
            matched_event=match,
            buy_yes_platform=best_yes_market_for_match.platform,
            buy_yes_price=best_yes_market_for_match.yes_price,
            buy_no_platform=best_no_market_for_match.platform,
            buy_no_price=best_no_market_for_match.no_price,
            spread=max_spread_for_match,
            profit_pct=profit_pct,
            combined_volume=combined_vol,
        ))

    # Sort by profit descending
    opportunities.sort(key=lambda o: o.profit_pct, reverse=True)
    return opportunities


# ============================================================
# SAVED MARKETS
# ============================================================
def _saved_file() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "saved_markets.json"


def load_saved() -> list[dict]:
    f = _saved_file()
    if f.exists():
        try:
            return json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return []


def save_market(event_data: dict) -> list[dict]:
    """Bookmark a matched event."""
    saved = load_saved()
    # Avoid duplicates by match_id
    mid = event_data.get("match_id", "")
    if mid and any(s.get("match_id") == mid for s in saved):
        return saved
    event_data["saved_at"] = time.time()
    saved.append(event_data)
    _saved_file().write_text(json.dumps(saved, indent=2))
    return saved


def unsave_market(match_id: str) -> list[dict]:
    """Remove a bookmark."""
    saved = load_saved()
    saved = [s for s in saved if s.get("match_id") != match_id]
    _saved_file().write_text(json.dumps(saved, indent=2))
    return saved


# ============================================================
# FEED: RECENT PRICE CHANGES
# ============================================================
_previous_prices: dict[str, float] = {}  # "platform:event_id" -> yes_price


def compute_feed(events: list[NormalizedEvent], max_items: int = 50) -> list[dict]:
    """Compute recent price changes for the live feed pane."""
    global _previous_prices
    feed: list[dict] = []

    for ev in events:
        key = f"{ev.platform}:{ev.event_id}"
        prev = _previous_prices.get(key)
        _previous_prices[key] = ev.yes_price

        if prev is not None and prev != ev.yes_price:
            change = ev.yes_price - prev
            feed.append({
                "platform": ev.platform,
                "event_id": ev.event_id,
                "title": ev.title[:80],
                "yes_price": ev.yes_price,
                "previous": prev,
                "change": round(change, 4),
                "change_pct": round(change / prev * 100, 2) if prev > 0 else 0,
                "timestamp": ev.last_updated,
            })

    # Sort by absolute change descending
    feed.sort(key=lambda f: abs(f["change"]), reverse=True)
    return feed[:max_items]


# ============================================================
# FULL SCAN ORCHESTRATOR
# ============================================================
class ArbitrageScanner:
    """Orchestrates the full scan: fetch -> match -> arbitrage."""

    def __init__(self, registry: AdapterRegistry):
        self.registry = registry
        self._last_events: list[NormalizedEvent] = []
        self._last_matched: list[MatchedEvent] = []
        self._last_opportunities: list[ArbitrageOpportunity] = []
        self._last_feed: list[dict] = []
        self._last_scan_time: float = 0

    async def scan(self) -> dict:
        """Run a full scan cycle. Returns summary."""
        # 1. Fetch from all platforms
        events = await self.registry.fetch_all()
        self._last_events = events

        # 2. Match events
        matched = match_events(events)
        self._last_matched = matched

        # 3. Find arbitrage
        opportunities = find_arbitrage(matched)
        self._last_opportunities = opportunities

        # 4. Compute feed
        feed = compute_feed(events)
        self._last_feed = feed

        self._last_scan_time = time.time()

        # 5. Cache to disk
        self._save_cache(events)

        return {
            "events_count": len(events),
            "matched_count": len(matched),
            "multi_platform_matches": sum(1 for m in matched if m.platform_count >= 2),
            "opportunities_count": len(opportunities),
            "feed_changes": len(feed),
            "scan_time": self._last_scan_time,
        }

    def get_opportunities(self) -> list[dict]:
        return [o.to_dict() for o in self._last_opportunities]

    def get_events(self) -> list[dict]:
        return [m.to_dict() for m in self._last_matched]

    def get_feed(self) -> list[dict]:
        return self._last_feed

    def _save_cache(self, events: list[NormalizedEvent]):
        """Persist latest events to disk for offline viewing."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            cache = [e.to_dict() for e in events]
            (DATA_DIR / "cache.json").write_text(json.dumps(cache, indent=2))
        except Exception as exc:
            logger.warning("Cache save failed: %s", exc)
