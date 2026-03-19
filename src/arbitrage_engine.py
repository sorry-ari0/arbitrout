"""Arbitrage engine — finds cross-platform spread opportunities.

Supports two types of opportunities:
1. Pure arbitrage: same event on different platforms, profit = 1 - (yes + no)
2. Synthetic derivatives: related events with different price targets (e.g., BTC >$71K
   and BTC >$74K). Combines positions to profit from a specific price range landing.
"""
import json
import logging
import time
from pathlib import Path
import asyncio
import threading

from adapters.models import NormalizedEvent, MatchedEvent, ArbitrageOpportunity
from adapters.registry import AdapterRegistry
from event_matcher import match_events, _extract_crypto

logger = logging.getLogger("arbitrage_engine")

DATA_DIR = Path(__file__).parent / "data" / "arbitrage"


# ============================================================
# ARBITRAGE CALCULATOR
# ============================================================
def _markets_have_same_target(markets: list[NormalizedEvent]) -> bool:
    """Check if all markets in a group target the same crypto price."""
    prices = []
    for m in markets:
        crypto = _extract_crypto(m.title)
        if crypto["price"]:
            prices.append(crypto["price"])
    if len(prices) < 2:
        return True  # Can't tell, assume same
    # All prices within 2% of each other = same target
    lo, hi = min(prices), max(prices)
    if hi == 0:
        return True
    return (lo / hi) >= 0.98


def _build_synthetic_info(yes_market: NormalizedEvent,
                          no_market: NormalizedEvent) -> dict:
    """Build synthetic derivative details from two markets with different targets.

    Example: BTC >$74K (YES=13c) on Polymarket + BTC >$71K (NO=1.6c) on Limitless.
    These are NOT the same event — combining them creates a synthetic range bet.

    Scenarios for "buy YES on higher strike, buy NO on lower strike":
      - Price > high_strike: YES wins ($1), NO loses ($0) → net = $1 - cost
      - low_strike < Price < high_strike: YES loses ($0), NO wins ($1) → net = $1 - cost
      - Price < low_strike: both lose → net = -(yes_cost + no_cost)

    The "sweet spot" is the middle scenario — profitable if price lands in the range.
    """
    yes_crypto = _extract_crypto(yes_market.title)
    no_crypto = _extract_crypto(no_market.title)

    yes_target = yes_crypto.get("price") or 0
    no_target = no_crypto.get("price") or 0

    # Determine which is the higher vs lower strike
    if yes_target >= no_target:
        high_strike = yes_target
        low_strike = no_target
    else:
        high_strike = no_target
        low_strike = yes_target

    yes_cost = yes_market.yes_price
    no_cost = no_market.no_price
    total_cost = yes_cost + no_cost

    # Scenario payoffs (per $1 invested in each leg equally)
    scenarios = {}
    if high_strike > 0 and low_strike > 0:
        scenarios = {
            "above_both": {
                "condition": f"Price > ${high_strike:,.0f}",
                "yes_pays": 1.0, "no_pays": 0.0,
                "net": round(1.0 - total_cost, 4),
                "return_pct": round((1.0 - total_cost) / total_cost * 100, 1) if total_cost > 0 else 0,
            },
            "between": {
                "condition": f"${low_strike:,.0f} < Price < ${high_strike:,.0f}",
                "yes_pays": 0.0, "no_pays": 1.0,
                "net": round(1.0 - total_cost, 4),
                "return_pct": round((1.0 - total_cost) / total_cost * 100, 1) if total_cost > 0 else 0,
            },
            "below_both": {
                "condition": f"Price < ${low_strike:,.0f}",
                "yes_pays": 0.0, "no_pays": 0.0,
                "net": round(-total_cost, 4),
                "return_pct": -100.0,
            },
        }

    return {
        "type": "range_synthetic",
        "high_strike": high_strike,
        "low_strike": low_strike,
        "yes_target": yes_target,
        "no_target": no_target,
        "total_cost": round(total_cost, 4),
        "scenarios": scenarios,
        "win_conditions": 2,   # wins in 2 of 3 scenarios
        "loss_conditions": 1,  # loses only if price drops below both
    }


def find_arbitrage(matched: list[MatchedEvent],
                   min_spread: float = 0.0,
                   min_volume: int = 0) -> list[ArbitrageOpportunity]:
    """Find arbitrage opportunities across matched events.

    Two modes:
    1. Pure arb (same target): profit = 1.0 - (yes + no), guaranteed if > 0
    2. Synthetic derivative (different targets): wins in 2/3 scenarios, loses in 1
    """
    opportunities: list[ArbitrageOpportunity] = []

    for match in matched:
        if match.platform_count < 2:
            continue

        markets = match.markets
        is_synthetic = not _markets_have_same_target(markets)
        combined_vol = sum(m.volume for m in markets)

        if combined_vol < min_volume:
            continue

        # Find cheapest YES and cheapest NO across different platforms
        best_yes_market = min(markets, key=lambda m: m.yes_price)
        best_no_market = min(markets, key=lambda m: m.no_price)

        # Skip if both are on the same platform (no cross-platform play)
        if best_yes_market.platform == best_no_market.platform:
            other_yes = [m for m in markets if m.platform != best_no_market.platform]
            other_no = [m for m in markets if m.platform != best_yes_market.platform]
            if other_yes:
                best_yes_market = min(other_yes, key=lambda m: m.yes_price)
            elif other_no:
                best_no_market = min(other_no, key=lambda m: m.no_price)
            else:
                continue

        total_cost = best_yes_market.yes_price + best_no_market.no_price

        if is_synthetic:
            # Synthetic: wins in 2/3 scenarios, each paying $1
            # Expected value = (2/3 * ($1 - cost)) + (1/3 * (-cost))
            # = 2/3 - cost
            # Profit if total_cost < $1 (same math, but NOT "guaranteed")
            synthetic_info = _build_synthetic_info(best_yes_market, best_no_market)
            spread = 1.0 - total_cost
            # Discount synthetic profit to reflect that it's not guaranteed
            # Only 2/3 scenarios win vs 3/3 for pure arb
            effective_profit_pct = spread * 100.0 * 0.667  # discount by win probability
            if effective_profit_pct < min_spread * 100:
                continue

            opportunities.append(ArbitrageOpportunity(
                matched_event=match,
                buy_yes_platform=best_yes_market.platform,
                buy_yes_price=best_yes_market.yes_price,
                buy_no_platform=best_no_market.platform,
                buy_no_price=best_no_market.no_price,
                spread=spread,
                profit_pct=round(effective_profit_pct, 2),
                combined_volume=combined_vol,
                is_synthetic=True,
                synthetic_info=synthetic_info,
            ))
        else:
            # Pure arb: guaranteed profit if spread > 0
            spread = 1.0 - total_cost
            profit_pct = spread * 100.0

            if spread < min_spread:
                continue

            opportunities.append(ArbitrageOpportunity(
                matched_event=match,
                buy_yes_platform=best_yes_market.platform,
                buy_yes_price=best_yes_market.yes_price,
                buy_no_platform=best_no_market.platform,
                buy_no_price=best_no_market.no_price,
                spread=spread,
                profit_pct=profit_pct,
                combined_volume=combined_vol,
                is_synthetic=False,
            ))

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
_previous_prices_lock = threading.Lock()


def compute_feed(events: list[NormalizedEvent], max_items: int = 50) -> list[dict]:
    """Compute recent price changes for the live feed pane."""
    global _previous_prices
    feed: list[dict] = []

    for ev in events:
        key = f"{ev.platform}:{ev.event_id}"
        with _previous_prices_lock:
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
        self._lock = threading.Lock()

    async def scan(self) -> dict:
        """Run a full scan cycle. Returns summary."""
        # 1. Fetch from all platforms
        events = await self.registry.fetch_all()
        with self._lock:
            self._last_events = events

        # 2. Match events
        matched = match_events(events)
        with self._lock:
            self._last_matched = matched

        # 3. Find arbitrage
        opportunities = find_arbitrage(matched)
        with self._lock:
            self._last_opportunities = opportunities

        # 4. Compute feed
        feed = compute_feed(events)
        with self._lock:
            self._last_feed = feed

        with self._lock:
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
        with self._lock:
            return [o.to_dict() for o in self._last_opportunities]

    def get_events(self) -> list[dict]:
        with self._lock:
            return [m.to_dict() for m in self._last_matched]

    def get_feed(self) -> list[dict]:
        with self._lock:
            return self._last_feed

    def _save_cache(self, events: list[NormalizedEvent]):
        """Persist latest events to disk for offline viewing."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            cache = [e.to_dict() for e in events]
            (DATA_DIR / "cache.json").write_text(json.dumps(cache, indent=2))
        except Exception as exc:
            logger.warning("Cache save failed: %s", exc)
