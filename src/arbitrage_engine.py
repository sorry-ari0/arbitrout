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
    """Check if all markets in a group target the same crypto price and type.

    Returns False (= synthetic) when:
    - Price targets differ by >2%
    - Market types differ (e.g., "between $74K-$76K" vs "above $74K")
    """
    cryptos = []
    for m in markets:
        crypto = _extract_crypto(m.title)
        if crypto["price"]:
            cryptos.append(crypto)
    if len(cryptos) < 2:
        return True  # Can't tell, assume same

    # Check if any markets are range ("between") vs directional ("above"/"below")
    directions = set(c.get("direction") for c in cryptos if c.get("direction"))
    if "between" in directions and directions - {"between"}:
        return False  # Mix of range and directional = synthetic

    # All prices within 2% of each other = same target
    prices = [c["price"] for c in cryptos]
    lo, hi = min(prices), max(prices)
    if hi == 0:
        return True
    return (lo / hi) >= 0.98


def _build_range_synthetic_info(yes_market: NormalizedEvent,
                                no_market: NormalizedEvent,
                                yes_crypto: dict,
                                no_crypto: dict) -> dict | None:
    """Build synthetic info when one leg is a range ("between") market.

    Range markets have 4 scenarios, not 3:
    Example: BUY YES "BTC between $74K-$76K" + BUY NO "BTC above $73.2K"
      1. Price > range_high ($76K): YES loses, NO loses (BTC IS above $73.2K) → BOTH LOSE
      2. range_low < Price < range_high ($74K-$76K): YES wins, NO loses → ONE WINS
      3. directional_strike < Price < range_low ($73.2K-$74K): YES loses, NO loses → BOTH LOSE
      4. Price < directional_strike ($73.2K): YES loses, NO wins → ONE WINS

    We must check all 4 scenarios and reject if loss scenarios are most probable.
    """
    yes_dir = yes_crypto.get("direction", "")
    no_dir = no_crypto.get("direction", "")

    # Identify which is the range market and which is directional
    if yes_dir == "between":
        range_market, range_crypto = yes_market, yes_crypto
        dir_market, dir_crypto = no_market, no_crypto
        range_is_yes = True
    else:
        range_market, range_crypto = no_market, no_crypto
        dir_market, dir_crypto = yes_market, yes_crypto
        range_is_yes = False

    range_low = range_crypto.get("price_low")
    range_high = range_crypto.get("price_high")
    dir_target = dir_crypto.get("price") or 0
    dir_direction = dir_crypto.get("direction", "") or "above"

    if not range_low or not range_high or not dir_target:
        return None

    # Costs
    if range_is_yes:
        range_cost = yes_market.yes_price   # BUY YES on range
        dir_cost = no_market.no_price       # BUY NO on directional
    else:
        range_cost = no_market.no_price     # BUY NO on range
        dir_cost = yes_market.yes_price     # BUY YES on directional

    total_cost = range_cost + dir_cost
    if total_cost >= 1.0:
        return None

    # Determine when each leg pays out
    # Range YES pays when price is IN [range_low, range_high]
    # Range NO pays when price is OUTSIDE [range_low, range_high]
    # Directional "above X" YES pays when price > X; NO pays when price < X
    # Directional "below X" YES pays when price < X; NO pays when price > X

    # Build 4 scenarios based on the price zones
    # Sort all boundary prices to create zones
    boundaries = sorted(set([range_low, range_high, dir_target]))

    # Create zones: below lowest, between each pair, above highest
    zones = []
    zones.append(("below", boundaries[0], f"Price < ${boundaries[0]:,.0f}"))
    for i in range(len(boundaries) - 1):
        zones.append(("between", (boundaries[i], boundaries[i+1]),
                      f"${boundaries[i]:,.0f} < Price < ${boundaries[i+1]:,.0f}"))
    zones.append(("above", boundaries[-1], f"Price > ${boundaries[-1]:,.0f}"))

    scenarios = {}
    win_count = 0
    loss_count = 0

    for zone_type, zone_val, condition in zones:
        # Determine a representative price for this zone
        if zone_type == "below":
            rep_price = zone_val - 1
        elif zone_type == "above":
            rep_price = zone_val + 1
        else:
            rep_price = (zone_val[0] + zone_val[1]) / 2

        # Does the range leg pay?
        in_range = range_low <= rep_price <= range_high
        if range_is_yes:
            range_pays = 1.0 if in_range else 0.0  # BUY YES on range
        else:
            range_pays = 1.0 if not in_range else 0.0  # BUY NO on range

        # Does the directional leg pay?
        if dir_direction in ("above", "over"):
            dir_condition_true = rep_price > dir_target
        elif dir_direction in ("below", "under"):
            dir_condition_true = rep_price < dir_target
        else:
            dir_condition_true = rep_price > dir_target

        if range_is_yes:
            # BUY NO on directional → pays when condition is FALSE
            dir_pays = 1.0 if not dir_condition_true else 0.0
        else:
            # BUY YES on directional → pays when condition is TRUE
            dir_pays = 1.0 if dir_condition_true else 0.0

        total_payout = range_pays + dir_pays
        net = round(total_payout - total_cost, 4)
        return_pct = round(net / total_cost * 100, 1) if total_cost > 0 else 0

        scenario_key = condition.replace(" ", "_").replace("$", "").replace(",", "")[:30]
        scenarios[scenario_key] = {
            "condition": condition,
            "range_pays": range_pays,
            "dir_pays": dir_pays,
            "net": net,
            "return_pct": return_pct,
        }

        if return_pct > 0:
            win_count += 1
        else:
            loss_count += 1

    # Must win in more scenarios than lose
    if win_count <= loss_count:
        return None

    # Estimate loss probability using market-implied probabilities
    # For range: YES price ≈ P(in range), NO price ≈ P(outside range)
    # For directional "above X": YES price ≈ P(above X), NO price ≈ P(below X)
    loss_prob = 0.0
    for s in scenarios.values():
        if s["return_pct"] <= 0:
            # Estimate zone probability from market prices
            # This is rough but better than assuming equal probability
            loss_prob += 1.0 / len(scenarios)

    if loss_prob > 0.60:
        return None

    # Calculate the gap between range boundary and directional strike
    if dir_target < range_low:
        gap = range_low - dir_target
    elif dir_target > range_high:
        gap = dir_target - range_high
    else:
        gap = 0  # directional strike is inside the range
    avg_price = (range_low + range_high) / 2
    gap_pct = gap / avg_price if avg_price > 0 else 1.0

    win_return_pct = round((1.0 - total_cost) / total_cost * 100, 1) if total_cost > 0 else 0

    return {
        "type": "range_vs_directional",
        "high_strike": range_high,
        "low_strike": range_low if dir_target >= range_low else dir_target,
        "yes_target": yes_crypto.get("price", 0),
        "no_target": no_crypto.get("price", 0),
        "yes_direction": yes_crypto.get("direction", ""),
        "no_direction": no_crypto.get("direction", ""),
        "yes_market_type": "range" if range_is_yes else "directional",
        "no_market_type": "range" if not range_is_yes else "directional",
        "total_cost": round(total_cost, 4),
        "scenarios": scenarios,
        "win_conditions": win_count,
        "loss_conditions": loss_count,
        "loss_probability": round(loss_prob, 3),
        "gap_pct": round(gap_pct * 100, 1),
        "yes_price_range": [yes_crypto.get("price_low"), yes_crypto.get("price_high")],
        "no_price_range": [no_crypto.get("price_low"), no_crypto.get("price_high")],
    }


def _build_synthetic_info(yes_market: NormalizedEvent,
                          no_market: NormalizedEvent) -> dict | None:
    """Build synthetic derivative details from two markets with different targets.

    For a valid synthetic, we need two markets that cover DIFFERENT outcomes:
    - BUY YES "above $74K" + BUY NO "above $70K"
      = wins if Price > $74K (YES pays) OR Price < $70K (NO pays)
      = loses only if $70K < Price < $74K (gap between strikes)

    Returns None if the synthetic is invalid (same-direction doubling, no price data,
    or loss probability > 40%).
    """
    yes_crypto = _extract_crypto(yes_market.title)
    no_crypto = _extract_crypto(no_market.title)

    yes_target = yes_crypto.get("price") or 0
    no_target = no_crypto.get("price") or 0
    yes_dir = yes_crypto.get("direction", "")
    no_dir = no_crypto.get("direction", "")

    if not yes_target or not no_target:
        return None

    # Handle range ("between") markets with proper 4-scenario analysis
    # instead of the standard 3-scenario model
    if yes_dir == "between" or no_dir == "between":
        return _build_range_synthetic_info(
            yes_market, no_market, yes_crypto, no_crypto
        )

    # Reject if both bets are the same direction — that's doubling down, not hedging
    # e.g., YES "dip to $70K" (below) + NO "above $70K" (above) — both win on same move
    yes_effective = yes_dir or "above"
    no_effective = no_dir or "above"

    # For "buy YES" on market A: you WIN when direction condition is TRUE
    # For "buy NO" on market B: you WIN when direction condition is FALSE (opposite)
    # So "buy NO on 'above $70K'" wins when price is BELOW $70K
    #
    # Valid synthetic: the two winning conditions should cover DIFFERENT price ranges
    # YES wins when: yes_dir is true (e.g., price > yes_target if "above")
    # NO wins when: no_dir is false (e.g., price < no_target if "above")
    #
    # For this to create a range play:
    #   YES "above X" wins when price > X
    #   NO "above Y" wins when price < Y
    #   Valid if X > Y → wins above X and below Y, loses in gap [Y, X]
    #   Valid if Y > X → always wins (one leg covers the other) = pure arb
    #
    #   YES "below X" wins when price < X
    #   NO "above Y" wins when price < Y
    #   If X ≈ Y → both win on same condition = same-direction doubling = REJECT

    # Determine effective win conditions
    # YES side wins when direction is TRUE
    if yes_effective in ("above", "over"):
        yes_wins_above = yes_target  # wins when price > yes_target
        yes_wins_below = None
    elif yes_effective in ("below", "under"):
        yes_wins_above = None
        yes_wins_below = yes_target  # wins when price < yes_target
    else:
        yes_wins_above = yes_target
        yes_wins_below = None

    # NO side wins when direction is FALSE (opposite)
    if no_effective in ("above", "over"):
        no_wins_above = None
        no_wins_below = no_target  # NO "above X" wins when price < X
    elif no_effective in ("below", "under"):
        no_wins_above = no_target  # NO "below X" wins when price > X
        no_wins_below = None
    else:
        no_wins_above = None
        no_wins_below = no_target

    # Check for same-direction doubling: both legs win on same price move
    if yes_wins_below and no_wins_below:
        # Both win when price drops — just doubling down on bearish bet
        ratio = min(yes_wins_below, no_wins_below) / max(yes_wins_below, no_wins_below)
        if ratio > 0.85:
            return None  # Too similar, not a hedge
    if yes_wins_above and no_wins_above:
        # Both win when price rises — just doubling down on bullish bet
        ratio = min(yes_wins_above, no_wins_above) / max(yes_wins_above, no_wins_above)
        if ratio > 0.85:
            return None

    # Determine the gap (loss zone) and winning zones
    yes_cost = yes_market.yes_price
    no_cost = no_market.no_price
    total_cost = yes_cost + no_cost

    if total_cost >= 1.0:
        return None  # No profit possible

    # Build direction-aware scenarios
    if yes_wins_above and no_wins_below:
        # Classic straddle: YES wins high, NO wins low, gap in middle
        high_strike = yes_wins_above
        low_strike = no_wins_below
        if high_strike <= low_strike:
            # Overlapping — always one leg wins = near-guaranteed
            high_strike, low_strike = max(yes_wins_above, no_wins_below), min(yes_wins_above, no_wins_below)

        # Loss probability ≈ gap size relative to strikes
        gap = abs(high_strike - low_strike)
        avg_strike = (high_strike + low_strike) / 2
        gap_pct = gap / avg_strike if avg_strike > 0 else 1.0

        # Use market prices as probability proxies
        # YES price ≈ P(price > yes_target), NO price ≈ P(price < no_target)
        # Loss prob ≈ 1 - P(YES wins) - P(NO wins)
        loss_prob = max(0, 1.0 - yes_cost - no_cost)

    elif yes_wins_below and no_wins_above:
        # Inverse straddle: YES wins low, NO wins high
        high_strike = no_wins_above
        low_strike = yes_wins_below
        gap = abs(high_strike - low_strike)
        avg_strike = (high_strike + low_strike) / 2
        gap_pct = gap / avg_strike if avg_strike > 0 else 1.0
        loss_prob = max(0, 1.0 - yes_cost - no_cost)
    else:
        # Can't determine valid straddle structure
        high_strike = max(yes_target, no_target)
        low_strike = min(yes_target, no_target)
        gap_pct = abs(high_strike - low_strike) / ((high_strike + low_strike) / 2) if (high_strike + low_strike) > 0 else 1.0
        loss_prob = 0.5  # Unknown, assume risky

    # Reject if loss probability is too high (>40%) or gap is too wide (>10%)
    if loss_prob > 0.40:
        return None
    if gap_pct > 0.10:
        return None

    win_return_pct = round((1.0 - total_cost) / total_cost * 100, 1) if total_cost > 0 else 0

    scenarios = {
        "above_high": {
            "condition": f"Price > ${high_strike:,.0f}",
            "yes_pays": 1.0 if yes_wins_above else 0.0,
            "no_pays": 1.0 if no_wins_above else 0.0,
            "net": round((1.0 if (yes_wins_above or no_wins_above) else 0.0) - total_cost, 4),
            "return_pct": win_return_pct if (yes_wins_above or no_wins_above) else -100.0,
        },
        "in_gap": {
            "condition": f"${low_strike:,.0f} < Price < ${high_strike:,.0f}",
            "yes_pays": 0.0, "no_pays": 0.0,
            "net": round(-total_cost, 4),
            "return_pct": -100.0,
        },
        "below_low": {
            "condition": f"Price < ${low_strike:,.0f}",
            "yes_pays": 1.0 if yes_wins_below else 0.0,
            "no_pays": 1.0 if no_wins_below else 0.0,
            "net": round((1.0 if (yes_wins_below or no_wins_below) else 0.0) - total_cost, 4),
            "return_pct": win_return_pct if (yes_wins_below or no_wins_below) else -100.0,
        },
    }

    # Count actual winning scenarios
    win_count = sum(1 for s in scenarios.values() if s["return_pct"] > 0)
    loss_count = sum(1 for s in scenarios.values() if s["return_pct"] <= 0)

    if win_count < 2:
        return None  # Must win in at least 2 of 3 scenarios

    market_types = set(filter(None, [yes_dir, no_dir]))
    is_range_mix = "between" in market_types and market_types - {"between"}
    synth_type = "range_vs_directional" if is_range_mix else "range_synthetic"
    yes_type = "range" if yes_dir == "between" else ("directional" if yes_dir else "unknown")
    no_type = "range" if no_dir == "between" else ("directional" if no_dir else "unknown")

    return {
        "type": synth_type,
        "high_strike": high_strike,
        "low_strike": low_strike,
        "yes_target": yes_target,
        "no_target": no_target,
        "yes_direction": yes_dir,
        "no_direction": no_dir,
        "yes_market_type": yes_type,
        "no_market_type": no_type,
        "total_cost": round(total_cost, 4),
        "scenarios": scenarios,
        "win_conditions": win_count,
        "loss_conditions": loss_count,
        "loss_probability": round(loss_prob, 3),
        "gap_pct": round(gap_pct * 100, 1),
        "yes_price_range": [yes_crypto.get("price_low"), yes_crypto.get("price_high")],
        "no_price_range": [no_crypto.get("price_low"), no_crypto.get("price_high")],
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

        # Filter out zero-price markets (closed/no liquidity)
        markets = [m for m in match.markets
                   if not (m.yes_price == 0 and m.no_price == 0)]
        if len(markets) < 2:
            continue

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
            # Validate the synthetic — returns None if bad pairing
            synthetic_info = _build_synthetic_info(best_yes_market, best_no_market)
            if synthetic_info is None:
                continue  # Invalid synthetic (same-direction, high loss prob, etc.)

            spread = 1.0 - total_cost
            # Discount by win probability (win_conditions / 3 scenarios)
            win_ratio = synthetic_info.get("win_conditions", 2) / 3.0
            # Further discount by loss probability
            loss_prob = synthetic_info.get("loss_probability", 0.33)
            effective_profit_pct = spread * 100.0 * (1.0 - loss_prob)
            if effective_profit_pct < min_spread * 100:
                continue

            opportunities.append(ArbitrageOpportunity(
                matched_event=match,
                buy_yes_platform=best_yes_market.platform,
                buy_yes_price=best_yes_market.yes_price,
                buy_yes_event_id=best_yes_market.event_id,
                buy_no_platform=best_no_market.platform,
                buy_no_price=best_no_market.no_price,
                buy_no_event_id=best_no_market.event_id,
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
                buy_yes_event_id=best_yes_market.event_id,
                buy_no_platform=best_no_market.platform,
                buy_no_price=best_no_market.no_price,
                buy_no_event_id=best_no_market.event_id,
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
_previous_prices: dict[str, tuple[float, float]] = {}  # "platform:event_id" -> (yes_price, timestamp)
_previous_prices_lock = threading.Lock()
_MAX_PRICE_ENTRIES = 5000  # Cap to prevent unbounded memory growth
_PRICE_TTL_SECONDS = 86400  # Prune entries older than 24 hours


def compute_feed(events: list[NormalizedEvent], max_items: int = 50) -> list[dict]:
    """Compute recent price changes for the live feed pane."""
    global _previous_prices
    feed: list[dict] = []
    now = time.time()

    for ev in events:
        key = f"{ev.platform}:{ev.event_id}"
        with _previous_prices_lock:
            entry = _previous_prices.get(key)
            prev = entry[0] if entry else None
            _previous_prices[key] = (ev.yes_price, now)

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

    # Prune stale entries periodically (when over 80% of cap)
    with _previous_prices_lock:
        if len(_previous_prices) > _MAX_PRICE_ENTRIES * 0.8:
            cutoff = now - _PRICE_TTL_SECONDS
            _previous_prices = {k: v for k, v in _previous_prices.items() if v[1] > cutoff}
            # If still over cap after TTL prune, drop oldest entries
            if len(_previous_prices) > _MAX_PRICE_ENTRIES:
                sorted_keys = sorted(_previous_prices, key=lambda k: _previous_prices[k][1])
                for k in sorted_keys[:len(_previous_prices) - _MAX_PRICE_ENTRIES]:
                    del _previous_prices[k]

    # Sort by absolute change descending
    feed.sort(key=lambda f: abs(f["change"]), reverse=True)
    return feed[:max_items]


# ============================================================
# FULL SCAN ORCHESTRATOR
# ============================================================
class ArbitrageScanner:
    """Orchestrates the full scan: fetch -> match -> arbitrage."""

    def __init__(self, registry: AdapterRegistry, decision_logger=None):
        self.registry = registry
        self._dlog = decision_logger
        self._last_events: list[NormalizedEvent] = []
        self._last_matched: list[MatchedEvent] = []
        self._last_opportunities: list[ArbitrageOpportunity] = []
        self._last_feed: list[dict] = []
        self._last_scan_time: float = 0
        self._lock = threading.Lock()

    async def scan(self) -> dict:
        """Run a full scan cycle. Returns summary."""
        scan_start = time.time()

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

        multi_platform = sum(1 for m in matched if m.platform_count >= 2)
        elapsed_ms = int((time.time() - scan_start) * 1000)

        # 6. Log scan summary and all detected opportunities
        if self._dlog:
            # Platform breakdown for monitoring
            platform_counts: dict[str, int] = {}
            for e in events:
                platform_counts[e.platform] = platform_counts.get(e.platform, 0) + 1

            self._dlog.log_arb_scan_summary(
                events_count=len(events),
                matched_count=len(matched),
                multi_platform=multi_platform,
                opportunities_count=len(opportunities),
                elapsed_ms=elapsed_ms,
                platform_counts=platform_counts,
            )

            # Log every opportunity detected (for hindsight analysis)
            for opp in opportunities:
                self._dlog.log_opportunity_detected(
                    title=opp.matched_event.canonical_title,
                    strategy_type="synthetic_derivative" if opp.is_synthetic else "cross_platform_arb",
                    spread_pct=opp.profit_pct,
                    platforms=[opp.buy_yes_platform, opp.buy_no_platform],
                    yes_price=opp.buy_yes_price,
                    no_price=opp.buy_no_price,
                    is_synthetic=opp.is_synthetic,
                    volume=opp.combined_volume,
                    event_ids=[opp.buy_yes_event_id, opp.buy_no_event_id],
                )

        logger.info("Scan complete: %d events, %d matched (%d multi-platform), %d opportunities, %dms",
                     len(events), len(matched), multi_platform, len(opportunities), elapsed_ms)

        return {
            "events_count": len(events),
            "matched_count": len(matched),
            "multi_platform_matches": multi_platform,
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
