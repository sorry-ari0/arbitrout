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
        unique_platforms = list(set(m.platform for m in markets))

        if len(unique_platforms) < 2:
            continue

        best_overall_spread = -1.0
        selected_buy_yes_market = None
        selected_buy_no_market = None

        # Group markets by platform for easier lookup of best prices per platform
        markets_by_platform = {p: [m for m in markets if m.platform == p] for p in unique_platforms}

        for platform1 in unique_platforms:
            # Find the market with the best (lowest) 'yes' price on platform1
            best_yes_market_on_p1 = min(markets_by_platform[platform1], key=lambda m: m.yes_price)

            for platform2 in unique_platforms:
                if platform1 == platform2:
                    continue  # Platforms for buying YES and NO must be distinct

                # Find the market with the best (lowest) 'no' price on platform2
                best_no_market_on_p2 = min(markets_by_platform[platform2], key=lambda m: m.no_price)

                current_spread = 1.0 - (best_yes_market_on_p1.yes_price + best_no_market_on_p2.no_price)

                if current_spread > best_overall_spread:
                    best_overall_spread = current_spread
                    selected_buy_yes_market = best_yes_market_on_p1
                    selected_buy_no_market = best_no_market_on_p2

        # If a valid opportunity was found across distinct platforms
        if selected_buy_yes_market and selected_buy_no_market:
            spread = best_overall_spread
            profit_pct = spread * 100.0
            combined_vol = sum(m.volume for m in markets) # Keep original combined_vol logic based on all markets in the match

            if spread < min_spread:
                continue
            if combined_vol < min_volume:
                continue

            opportunities.append(ArbitrageOpportunity(
                matched_event=match,
                buy_yes_platform=selected_buy_yes_market.platform,
                buy_yes_price=selected_buy_yes_market.yes_price,
                buy_no_platform=selected_buy_no_market.platform,
                buy_no_price=selected_buy_no_market.no_price,
                spread=spread,
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
