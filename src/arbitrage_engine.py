"""Arbitrage engine — finds cross-platform spread opportunities.

Supports two types of opportunities:
1. Pure arbitrage: same event on different platforms, profit = 1 - (yes + no)
2. Synthetic derivatives: related events with different price targets (e.g., BTC >$71K
   and BTC >$74K). Combines positions to profit from a specific price range landing.
"""
import json
import logging
import re
import time
from pathlib import Path
import asyncio
import threading

from adapters.models import NormalizedEvent, MatchedEvent, ArbitrageOpportunity
from adapters.registry import AdapterRegistry
from event_matcher import match_events, _extract_crypto
from arbitrage_history import save_opportunity_history, save_market_history # NEW: Import market history saver
from execution.paper_executor import get_taker_fee_rate as get_polymarket_taker_fee_rate

logger = logging.getLogger("arbitrage_engine")

DATA_DIR = Path(__file__).parent / "data" / "arbitrage"


# ============================================================
# PLATFORM FEE RATES (for opportunity filtering)
# ============================================================
# Entry fees: maker (GTC limit orders) for Polymarket (0%), taker for others
_TAKER_FEES = {
    "polymarket": 0.0,      # 0% maker fee (all orders use GTC limit)
    "kalshi": 0.01,
    "predictit": 0.0,       # No entry fee; profit taxed at resolution
    "limitless": 0.01,
    "robinhood": 0.0,
    "coinbase_spot": 0.006,
    "kraken": 0.0026,
    "manifold": 0.0,        # NEW: Manifold markets are fee-free for trading, funds are converted to mana
    "metaculus": 0.0,       # NEW: Metaculus is community-driven, no direct trading fees
}
_DEFAULT_TAKER_FEE = 0.0
_PREDICTIT_PROFIT_TAX = 0.10  # 10% of profits at contract resolution
_PREDICTIT_WITHDRAWAL_FEE = 0.05  # 5% of withdrawal amount
_MIN_ACTIONABLE_LEG_PRICE = 0.01


def _leg_price(event: NormalizedEvent, side: str) -> float:
    """Return the selected leg price for an event."""
    return event.yes_price if side == "yes" else event.no_price


def _leg_is_actionable(event: NormalizedEvent, side: str) -> bool:
    """Require a non-zero quoted leg and some observed market activity."""
    return _leg_price(event, side) >= _MIN_ACTIONABLE_LEG_PRICE and event.volume > 0


def _event_taker_fee_rate(event: NormalizedEvent, side: str) -> float:
    """Return the modeled taker fee rate for a given market leg."""
    if event.platform == "polymarket":
        price = event.yes_price if side == "yes" else event.no_price
        return get_polymarket_taker_fee_rate(event.category, price)
    return _TAKER_FEES.get(event.platform, _DEFAULT_TAKER_FEE)


def _compute_fee_adjusted_profit(
    yes_market: NormalizedEvent,
    no_market: NormalizedEvent,
) -> tuple[float, float]:
    """Compute guaranteed profit after all platform fees.

    Returns (net_profit_pct, total_cost_with_fees).

    net_profit_pct uses the same basis as profit_pct (= net_spread * 100),
    so 15.4 means 15.4 cents net profit per $1 payout.

    For PredictIt: 10% tax on profits + 5% withdrawal fee.
    For others: taker_fee_rate * price at entry.
    """
    yes_price = yes_market.yes_price
    no_price = no_market.no_price
    yes_platform = yes_market.platform
    no_platform = no_market.platform

    yes_fee_rate = _event_taker_fee_rate(yes_market, "yes")
    no_fee_rate = _event_taker_fee_rate(no_market, "no")
    yes_fee = yes_price * yes_fee_rate
    no_fee = no_price * no_fee_rate
    total_cost = yes_price + no_price + yes_fee + no_fee

    # Resolution payouts — PredictIt takes 10% of profits + 5% of withdrawal
    yes_payout = 1.0
    if yes_platform == "predictit":
        after_tax = 1.0 - _PREDICTIT_PROFIT_TAX * (1.0 - yes_price)
        yes_payout = after_tax * (1.0 - _PREDICTIT_WITHDRAWAL_FEE)
    no_payout = 1.0
    if no_platform == "predictit":
        after_tax = 1.0 - _PREDICTIT_PROFIT_TAX * (1.0 - no_price)
        no_payout = after_tax * (1.0 - _PREDICTIT_WITHDRAWAL_FEE)

    # Guaranteed profit = worst-case scenario
    worst_payout = min(yes_payout, no_payout)
    worst_profit = worst_payout - total_cost
    # Use spread basis (same as profit_pct): net profit per $1 payout * 100
    net_pct = worst_profit * 100

    return net_pct, total_cost


def _best_pure_arb_pair(markets: list[NormalizedEvent]) -> tuple[NormalizedEvent, NormalizedEvent, float, float] | None:
    """Pick the best executable YES/NO pair across distinct platforms.

    The naive cheapest-quote pair is often a phantom because one selected leg has
    a zero quote or zero observed volume. Search all cross-platform pairs and keep
    the one with the best net profit after fees.
    """
    best_pair: tuple[NormalizedEvent, NormalizedEvent, float, float] | None = None

    for yes_market in markets:
        if not _leg_is_actionable(yes_market, "yes"):
            continue
        for no_market in markets:
            if yes_market.platform == no_market.platform:
                continue
            if not _leg_is_actionable(no_market, "no"):
                continue

            net_pct, _ = _compute_fee_adjusted_profit(yes_market, no_market)
            gross_pct = (1.0 - (yes_market.yes_price + no_market.no_price)) * 100.0
            candidate = (yes_market, no_market, gross_pct, net_pct)

            if best_pair is None:
                best_pair = candidate
                continue

            _, _, best_gross_pct, best_net_pct = best_pair
            if net_pct > best_net_pct + 1e-9:
                best_pair = candidate
            elif abs(net_pct - best_net_pct) <= 1e-9 and gross_pct > best_gross_pct:
                best_pair = candidate

    return best_pair


def _match_confidence(profit_pct: float) -> str:
    """Estimate confidence that a detected spread is a real arbitrage.

    Huge spreads (>30%) on prediction markets almost always indicate
    a false match (different contracts matched as the same event),
    not a genuine arbitrage opportunity.
    """
    if profit_pct > 50:
        return "very_low"
    if profit_pct > 30:
        return "low"
    if profit_pct > 15:
        return "medium"
    return "high"


# ============================================================
# GENERAL THRESHOLD EXTRACTION
# ============================================================
_THRESHOLD_PATTERNS = [
    # "7 or more corners", "43.5% or higher", "10 or more touchdowns"
    (r'(\d+(?:\.\d+)?)\s*%?\s*(?:or\s+(?:more|higher|greater|above))', "above"),
    # "fewer than 7", "under 43.5%", "less than 10"
    (r'(?:fewer|less|under)\s+(?:than\s+)?(\d+(?:\.\d+)?)\s*%?', "below"),
    # "at least 7", "minimum 7"
    (r'(?:at\s+least|minimum)\s+(\d+(?:\.\d+)?)\s*%?', "above"),
    # "over 7.5", "above 43.5%"
    (r'(?:over|above)\s+(\d+(?:\.\d+)?)\s*%?', "above"),
    # "below 7.5", "under 43.5%"
    (r'(?:below|under)\s+(\d+(?:\.\d+)?)\s*%?', "below"),
]


def _extract_threshold(title: str) -> dict:
    """Extract a numeric threshold from any market title.

    Handles sports ("7 or more corners"), ratings ("43.5% or higher"),
    and other threshold-based markets. Crypto targets are handled by
    _extract_crypto() and take priority.

    Returns {"value": float|None, "direction": "above"|"below"|None}
    """
    lower = title.lower()
    for pattern, direction in _THRESHOLD_PATTERNS:
        m = re.search(pattern, lower)
        if m:
            try:
                return {"value": float(m.group(1)), "direction": direction}
            except (ValueError, IndexError):
                pass
    return {"value": None, "direction": None}


# ============================================================
# ARBITRAGE CALCULATOR
# ============================================================
def _markets_have_same_target(markets: list[NormalizedEvent]) -> bool:
    """Check if all markets in a group target the same threshold.

    Returns False (= synthetic) when:
    - Crypto price targets differ by >0.5%
    - Market types differ (e.g., "between $74K-$76K" vs "above $74K")
    - Non-crypto numeric thresholds differ (e.g., "7+ corners" vs "9+ corners")
    """
    # --- Check crypto targets first ---
    cryptos = []
    for m in markets:
        crypto = _extract_crypto(m.title)
        if crypto["price"]:
            cryptos.append(crypto)
    if len(cryptos) >= 2:
        directions = set(c.get("direction") for c in cryptos if c.get("direction"))
        if "between" in directions and directions - {"between"}:
            return False
        prices = [c["price"] for c in cryptos]
        lo, hi = min(prices), max(prices)
        if hi > 0 and (lo / hi) < 0.995:
            return False
        return True

    # --- Check general numeric thresholds ---
    thresholds = []
    for m in markets:
        t = _extract_threshold(m.title)
        if t["value"] is not None:
            thresholds.append(t)
    if len(thresholds) >= 2:
        values = [t["value"] for t in thresholds]
        lo, hi = min(values), max(values)
        if hi > 0 and lo != hi:
            # Different thresholds detected (e.g., 7 vs 9 corners)
            return False

    return True  # Can't detect difference, assume same


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

    # Compute P(neither leg pays) directly from trade structure and
    # market-implied probabilities. No fragile string matching needed.
    #
    # p_in_range = P(price in [L, H])  — from range market
    # p_above_dir = P(price > T)       — from directional market
    p_in_range = range_market.yes_price if range_is_yes else range_market.no_price
    if dir_direction in ("above", "over"):
        p_above_dir = dir_market.yes_price
    elif dir_direction in ("below", "under"):
        p_above_dir = 1.0 - dir_market.yes_price
    else:
        p_above_dir = dir_market.yes_price

    # Determine "dir_NO_pays_when_below_T" — True when buying NO on "above T"
    dir_no_below = (dir_direction in ("above", "over", ""))

    if range_is_yes:
        # BUY YES on range [L,H] + BUY NO on directional.
        # Range pays: price ∈ [L, H]
        if dir_no_below:
            # Dir NO on "above T" pays when price ≤ T.
            # Neither pays when: price ∉ [L,H] AND price > T
            if dir_target >= range_high:
                loss_prob = p_above_dir
            elif dir_target <= range_low:
                loss_prob = max(0.0, p_above_dir - p_in_range)
            else:
                frac_above = (range_high - dir_target) / (range_high - range_low)
                loss_prob = max(0.0, p_above_dir - p_in_range * frac_above)
        else:
            # Dir NO on "below T" pays when price ≥ T.
            # Neither pays when: price ∉ [L,H] AND price < T
            if dir_target <= range_low:
                loss_prob = 1.0 - p_above_dir
            elif dir_target >= range_high:
                loss_prob = max(0.0, (1.0 - p_above_dir) - p_in_range)
            else:
                frac_below = (dir_target - range_low) / (range_high - range_low)
                loss_prob = max(0.0, (1.0 - p_above_dir) - p_in_range * frac_below)
    else:
        # BUY NO on range + BUY YES on directional.
        # Range NO pays: price ∉ [L, H]
        if dir_no_below:
            # Dir YES on "above T" pays when price > T (note: buying YES, not NO).
            # Neither pays when: price ∈ [L,H] AND price ≤ T
            if dir_target >= range_high:
                loss_prob = p_in_range
            elif dir_target <= range_low:
                loss_prob = 0.0
            else:
                frac = (dir_target - range_low) / (range_high - range_low)
                loss_prob = p_in_range * frac
        else:
            # Dir YES on "below T" pays when price < T.
            # Neither pays when: price ∈ [L,H] AND price ≥ T
            if dir_target <= range_low:
                loss_prob = p_in_range
            elif dir_target >= range_high:
                loss_prob = 0.0
            else:
                frac = (range_high - dir_target) / (range_high - range_low)
                loss_prob = p_in_range * frac

    if loss_prob > 0.40:
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
    # Reject if resolution dates differ by >3 days — mismatched expiry
    # breaks the hedge guarantee (one leg can resolve while the other is active).
    try:
        from datetime import datetime as _dt
        y_exp = yes_market.expiry[:10] if yes_market.expiry else ""
        n_exp = no_market.expiry[:10] if no_market.expiry else ""
        if y_exp and n_exp and y_exp != "ongoing" and n_exp != "ongoing":
            y_d = _dt.strptime(y_exp, "%Y-%m-%d")
            n_d = _dt.strptime(n_exp, "%Y-%m-%d")
            if abs((y_d - n_d).days) > 3:
                return None
    except (ValueError, TypeError):
        pass  # Can't parse — allow through, other gates will catch bad synthetics

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
        # YES wins when price > yes_target, NO wins when price < no_target
        # If yes_target < no_target: zones overlap → guaranteed profit (both win in middle)
        # If yes_target > no_target: gap in middle → loss zone
        is_overlapping = yes_wins_above < no_wins_below
        high_strike = max(yes_wins_above, no_wins_below)
        low_strike = min(yes_wins_above, no_wins_below)

        gap = abs(high_strike - low_strike)
        avg_strike = (high_strike + low_strike) / 2
        gap_pct = gap / avg_strike if avg_strike > 0 else 1.0

        if is_overlapping:
            # Guaranteed: all scenarios win, middle zone is a bonus
            loss_prob = 0.0
        else:
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
    # (Skip gap check for overlapping scenarios — they're guaranteed)
    if loss_prob > 0.40:
        return None
    if not (yes_wins_above and no_wins_below and yes_wins_above < no_wins_below):
        if gap_pct > 0.10:
            return None

    win_return_pct = round((1.0 - total_cost) / total_cost * 100, 1) if total_cost > 0 else 0

    # Check if this is an overlapping scenario (guaranteed profit)
    is_overlap = (yes_wins_above and no_wins_below
                  and yes_wins_above < no_wins_below)

    if is_overlap:
        # Guaranteed: YES wins above low_strike, NO wins below high_strike
        # Middle zone where both win = bonus payout
        bonus_return_pct = round((2.0 - total_cost) / total_cost * 100, 1) if total_cost > 0 else 0
        scenarios = {
            "above_high": {
                "condition": f"Price > ${high_strike:,.0f}",
                "yes_pays": 1.0, "no_pays": 0.0,
                "net": round(1.0 - total_cost, 4),
                "return_pct": win_return_pct,
            },
            "between": {
                "condition": f"${low_strike:,.0f} < Price < ${high_strike:,.0f}",
                "yes_pays": 1.0, "no_pays": 1.0,
                "net": round(2.0 - total_cost, 4),
                "return_pct": bonus_return_pct,
            },
            "below_low": {
                "condition": f"Price < ${low_strike:,.0f}",
                "yes_pays": 0.0, "no_pays": 1.0,
                "net": round(1.0 - total_cost, 4),
                "return_pct": win_return_pct,
            },
        }
        win_count = 3
        loss_count = 0
    else:
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


def _build_threshold_synthetic_info(yes_market: NormalizedEvent,
                                     no_market: NormalizedEvent) -> dict | None:
    """Build synthetic info for non-crypto threshold-based markets.

    Handles cases like:
    - BUY YES "7+ corners" + BUY NO "9+ corners"
    - BUY YES "43.5% approval or higher" + BUY NO "45% approval or higher"

    For nested "above" thresholds (YES_threshold <= NO_threshold):
      All scenarios are profitable — at least one leg always wins.
      This is a guaranteed cross-threshold arbitrage with a bonus zone.

    Scenarios:
      value >= high_threshold: YES wins, NO loses → $1 payout
      low_threshold <= value < high_threshold: BOTH win → $2 payout (bonus!)
      value < low_threshold: YES loses, NO wins → $1 payout
    """
    # Reject mismatched resolution dates (>3 days apart)
    try:
        from datetime import datetime as _dt
        y_exp = yes_market.expiry[:10] if yes_market.expiry else ""
        n_exp = no_market.expiry[:10] if no_market.expiry else ""
        if y_exp and n_exp and y_exp != "ongoing" and n_exp != "ongoing":
            y_d = _dt.strptime(y_exp, "%Y-%m-%d")
            n_d = _dt.strptime(n_exp, "%Y-%m-%d")
            if abs((y_d - n_d).days) > 3:
                return None
    except (ValueError, TypeError):
        pass

    yes_t = _extract_threshold(yes_market.title)
    no_t = _extract_threshold(no_market.title)

    if yes_t["value"] is None or no_t["value"] is None:
        return None
    if yes_t["value"] == no_t["value"]:
        return None  # Same threshold, not a synthetic

    yes_val = yes_t["value"]
    no_val = no_t["value"]
    yes_dir = yes_t["direction"] or "above"
    no_dir = no_t["direction"] or "above"

    yes_cost = yes_market.yes_price
    no_cost = no_market.no_price
    total_cost = yes_cost + no_cost

    if total_cost >= 1.0:
        return None

    # Determine high and low thresholds
    # BUY YES on lower threshold + BUY NO on higher threshold = guaranteed
    # because at least one leg always covers the outcome
    if yes_dir == "above" and no_dir == "above":
        low_strike = min(yes_val, no_val)
        high_strike = max(yes_val, no_val)

        # Verify we're buying YES on lower and NO on higher
        # YES "7+" wins when >= 7; NO "9+" wins when < 9
        if yes_val <= no_val:
            # Correct: YES covers low, NO covers high
            pass
        else:
            # Reversed: YES is higher threshold than NO
            # YES "9+" wins when >= 9; NO "7+" wins when < 7
            # Gap between 7 and 9 where both lose!
            # This is a synthetic with loss zone
            pass

        win_return_pct = round((1.0 - total_cost) / total_cost * 100, 1) if total_cost > 0 else 0
        bonus_return_pct = round((2.0 - total_cost) / total_cost * 100, 1) if total_cost > 0 else 0

        if yes_val <= no_val:
            # Guaranteed: all scenarios win
            scenarios = {
                "above_high": {
                    "condition": f"Value >= {high_strike}",
                    "yes_pays": 1.0, "no_pays": 0.0,
                    "net": round(1.0 - total_cost, 4),
                    "return_pct": win_return_pct,
                },
                "between": {
                    "condition": f"{low_strike} <= Value < {high_strike}",
                    "yes_pays": 1.0, "no_pays": 1.0,
                    "net": round(2.0 - total_cost, 4),
                    "return_pct": bonus_return_pct,
                },
                "below_low": {
                    "condition": f"Value < {low_strike}",
                    "yes_pays": 0.0, "no_pays": 1.0,
                    "net": round(1.0 - total_cost, 4),
                    "return_pct": win_return_pct,
                },
            }
            win_count = 3
            loss_count = 0
            loss_prob = 0.0
        else:
            # Gap zone: YES needs >= high, NO needs < low
            scenarios = {
                "above_high": {
                    "condition": f"Value >= {high_strike}",
                    "yes_pays": 1.0, "no_pays": 0.0,
                    "net": round(1.0 - total_cost, 4),
                    "return_pct": win_return_pct,
                },
                "in_gap": {
                    "condition": f"{low_strike} <= Value < {high_strike}",
                    "yes_pays": 0.0, "no_pays": 0.0,
                    "net": round(-total_cost, 4),
                    "return_pct": -100.0,
                },
                "below_low": {
                    "condition": f"Value < {low_strike}",
                    "yes_pays": 0.0, "no_pays": 1.0,
                    "net": round(1.0 - total_cost, 4),
                    "return_pct": win_return_pct,
                },
            }
            win_count = 2
            loss_count = 1
            loss_prob = max(0, 1.0 - yes_cost - no_cost)
            if loss_prob > 0.60:
                return None
    else:
        # Mixed directions or "below" — use general scenario analysis
        return None

    gap = abs(high_strike - low_strike)
    avg = (high_strike + low_strike) / 2
    gap_pct = gap / avg if avg > 0 else 0

    return {
        "type": "cross_threshold",
        "high_strike": high_strike,
        "low_strike": low_strike,
        "yes_target": yes_val,
        "no_target": no_val,
        "yes_direction": yes_dir,
        "no_direction": no_dir,
        "yes_market_type": "threshold",
        "no_market_type": "threshold",
        "total_cost": round(total_cost, 4),
        "scenarios": scenarios,
        "win_conditions": win_count,
        "loss_conditions": loss_count,
        "loss_probability": round(loss_prob, 3),
        "gap_pct": round(gap_pct * 100, 1),
        "yes_price_range": None,
        "no_price_range": None,
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

        if is_synthetic:
            best_candidate = None
            for ym in markets:
                if not _leg_is_actionable(ym, "yes"):
                    continue
                for nm in markets:
                    if ym.platform == nm.platform:
                        continue
                    if not _leg_is_actionable(nm, "no"):
                        continue
                    si = _build_synthetic_info(ym, nm)
                    if si is None:
                        si = _build_threshold_synthetic_info(ym, nm)
                    if si is None:
                        continue
                    total_cost = ym.yes_price + nm.no_price
                    spread = 1.0 - total_cost
                    loss_prob = si.get("loss_probability", 0.33)
                    effective_profit_pct = spread * 100.0 * (1.0 - loss_prob)
                    pair_volume = ym.volume + nm.volume
                    candidate = (effective_profit_pct, pair_volume, ym, nm, si)
                    if best_candidate is None or candidate[:2] > best_candidate[:2]:
                        best_candidate = candidate
            if best_candidate is None:
                continue

            _, combined_vol, best_yes_market, best_no_market, synthetic_info = best_candidate
            if combined_vol < min_volume:
                continue
            total_cost = best_yes_market.yes_price + best_no_market.no_price

            spread = 1.0 - total_cost
            # Discount by win probability
            loss_prob = synthetic_info.get("loss_probability", 0.33)
            effective_profit_pct = spread * 100.0 * (1.0 - loss_prob)
            if effective_profit_pct < min_spread * 100:
                continue

            # Fee-adjusted profit for synthetics.
            # _compute_fee_adjusted_profit assumes guaranteed $1 payout (pure arb).
            # For synthetics, expected payout = win_prob * $1 + loss_prob * $0.
            # Compute fees directly instead.
            _yes_fee_rate = _event_taker_fee_rate(best_yes_market, "yes")
            _no_fee_rate = _event_taker_fee_rate(best_no_market, "no")
            _yes_fee = best_yes_market.yes_price * _yes_fee_rate
            _no_fee = best_no_market.no_price * _no_fee_rate
            total_cost_with_fees = total_cost + _yes_fee + _no_fee
            expected_payout = 1.0 * (1.0 - loss_prob)  # win → $1, lose → $0
            net_effective = (expected_payout - total_cost_with_fees) / total_cost_with_fees * 100
            if net_effective <= 0:
                continue

            # Synthetics use their own rejection criteria (loss prob, scenarios),
            # so don't apply the confidence filter here
            confidence = _match_confidence(effective_profit_pct)

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
                net_profit_pct=round(net_effective, 2),
                confidence=confidence,
                calculation_audit={
                    "yes_price": round(best_yes_market.yes_price, 4),
                    "no_price": round(best_no_market.no_price, 4),
                    "yes_platform": best_yes_market.platform,
                    "no_platform": best_no_market.platform,
                    "yes_volume": best_yes_market.volume,
                    "no_volume": best_no_market.volume,
                    "total_cost": round(total_cost, 4),
                    "gross_spread": round(spread, 4),
                    "gross_profit_pct": round(spread * 100, 2),
                    "yes_fee_rate": _yes_fee_rate,
                    "no_fee_rate": _no_fee_rate,
                    "loss_probability": round(loss_prob, 3),
                    "effective_profit_pct": round(effective_profit_pct, 2),
                    "net_profit_pct": round(net_effective, 2),
                },
            ))
        else:
            best_pair = _best_pure_arb_pair(markets)
            if best_pair is None:
                continue

            best_yes_market, best_no_market, profit_pct, net_pct = best_pair
            combined_vol = best_yes_market.volume + best_no_market.volume
            if combined_vol < min_volume:
                continue

            # Pure arb: guaranteed profit if spread > 0
            total_cost = best_yes_market.yes_price + best_no_market.no_price
            spread = 1.0 - total_cost

            if spread < min_spread:
                continue

            # Skip if guaranteed loss after fees
            if net_pct <= 0:
                continue

            confidence = _match_confidence(profit_pct)

            # Drop very_low confidence — almost certainly false matches
            if confidence == "very_low":
                continue

            _yes_fee_rate = _event_taker_fee_rate(best_yes_market, "yes")
            _no_fee_rate = _event_taker_fee_rate(best_no_market, "no")
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
                net_profit_pct=round(net_pct, 2),
                confidence=confidence,
                calculation_audit={
                    "yes_price": round(best_yes_market.yes_price, 4),
                    "no_price": round(best_no_market.no_price, 4),
                    "yes_platform": best_yes_market.platform,
                    "no_platform": best_no_market.platform,
                    "yes_volume": best_yes_market.volume,
                    "no_volume": best_no_market.volume,
                    "total_cost": round(total_cost, 4),
                    "gross_spread": round(spread, 4),
                    "gross_profit_pct": round(profit_pct, 2),
                    "yes_fee_rate": _yes_fee_rate,
                    "no_fee_rate": _no_fee_rate,
                    "net_profit_pct": round(net_pct, 2),
                },
            ))

    # Deduplicate by market pair (order-independent) and match_id
    seen: set[frozenset] = set()
    seen_match_ids: set[str] = set()
    deduped: list[ArbitrageOpportunity] = []
    for opp in opportunities:
        key = frozenset([opp.buy_yes_event_id, opp.buy_no_event_id])
        mid = opp.matched_event.match_id
        if key not in seen and mid not in seen_match_ids:
            seen.add(key)
            seen_match_ids.add(mid)
            deduped.append(opp)
    opportunities = deduped

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
# WATCHLIST (NEW)
# ============================================================
def _watchlist_file() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "watchlist.json"

def load_watchlist_items() -> list[dict]:
    f = _watchlist_file()
    if f.exists():
        try:
            return json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return []

def add_to_watchlist(item_data: dict) -> list[dict]:
    """Add an individual market to the watchlist."""
    watchlist = load_watchlist_items()
    # Avoid duplicates by platform and event_id
    platform = item_data.get("platform", "")
    event_id = item_data.get("event_id", "")
    if platform and event_id and any(w.get("platform") == platform and w.get("event_id") == event_id for w in watchlist):
        return watchlist
    item_data["added_at"] = time.time()
    watchlist.append(item_data)
    _watchlist_file().write_text(json.dumps(watchlist, indent=2))
    return watchlist

def remove_from_watchlist(platform: str, event_id: str) -> bool:
    """Remove an individual market from the watchlist."""
    watchlist = load_watchlist_items()
    original_len = len(watchlist)
    watchlist = [w for w in watchlist if not (w.get("platform") == platform and w.get("event_id") == event_id)]
    if len(watchlist) < original_len:
        _watchlist_file().write_text(json.dumps(watchlist, indent=2))
        return True
    return False


# ============================================================
# FEED: RECENT PRICE CHANGES & ALERTS
# ============================================================
_previous_prices: dict[str, tuple[float, float]] = {}  # "platform:event_id" -> (yes_price, timestamp)
_previous_prices_lock = threading.Lock()
_MAX_PRICE_ENTRIES = 5000  # Cap to prevent unbounded memory growth
_PRICE_TTL_SECONDS = 86400  # Prune entries older than 24 hours

# NEW: Track high-profit opportunities for alerts
_high_profit_alerts: dict[str, float] = {} # match_id -> profit_pct
_HIGH_PROFIT_THRESHOLD = 25.0 # % net profit for an alert
_ALERT_TTL_SECONDS = 3600 # Clear alerts after 1 hour

def compute_feed(events: list[NormalizedEvent],
                 current_opportunities: list[ArbitrageOpportunity] | None = None,
                 max_items: int = 50) -> list[dict]:
    """Compute recent price changes and new high-profit alerts for the live feed pane."""
    global _previous_prices, _high_profit_alerts
    current_opportunities = current_opportunities or []
    feed: list[dict] = []
    now = time.time()

    # Process price changes
    for ev in events:
        key = f"{ev.platform}:{ev.event_id}"
        with _previous_prices_lock:
            entry = _previous_prices.get(key)
            prev = entry[0] if entry else None
            _previous_prices[key] = (ev.yes_price, now)
        
        # NEW: Save individual market history
        save_market_history(ev.platform, ev.event_id, ev.yes_price, ev.no_price)

        if prev is not None and prev != ev.yes_price:
            change = ev.yes_price - prev
            feed.append({
                "type": "price_change",
                "platform": ev.platform,
                "event_id": ev.event_id,
                "title": ev.title[:80],
                "yes_price": ev.yes_price,
                "previous": prev,
                "change": round(change, 4),
                "change_pct": round(change / prev * 100, 2) if prev > 0 else 0,
                "timestamp": now, # Use current time for feed display
            })

    # Process high-profit opportunities for alerts
    new_alerts_detected = []
    for opp in current_opportunities:
        match_id = opp.matched_event.match_id
        net_profit = opp.net_profit_pct
        
        # If profit > threshold and it's a new alert or significant increase
        if net_profit >= _HIGH_PROFIT_THRESHOLD:
            if match_id not in _high_profit_alerts or net_profit > _high_profit_alerts[match_id] + 5.0: # 5% increase for re-alert
                new_alerts_detected.append(opp)
            _high_profit_alerts[match_id] = net_profit
        # If an alert was previously active but profit dropped below threshold, remove it
        elif match_id in _high_profit_alerts and net_profit < _HIGH_PROFIT_THRESHOLD:
            _high_profit_alerts.pop(match_id, None)

    # Add new alerts to the feed
    for opp in new_alerts_detected:
        feed.append({
            "type": "opportunity_alert",
            "platform": opp.buy_yes_platform,
            "event_id": opp.matched_event.match_id,
            "title": f"New Arb: {opp.matched_event.canonical_title} ({opp.net_profit_pct:.1f}%)",
            "yes_price": opp.buy_yes_price,
            "previous": None,
            "change": opp.net_profit_pct,
            "change_pct": opp.net_profit_pct,
            "timestamp": now,
            "match_id": opp.matched_event.match_id,
            "is_synthetic": opp.is_synthetic,
        })
    
    # Prune stale alerts
    _high_profit_alerts = {k: v for k, v in _high_profit_alerts.items() if (now - _previous_prices.get(k, (0,0))[1]) < _ALERT_TTL_SECONDS}


    # Prune stale price entries periodically (when over 80% of cap)
    with _previous_prices_lock:
        if len(_previous_prices) > _MAX_PRICE_ENTRIES * 0.8:
            cutoff = now - _PRICE_TTL_SECONDS
            _previous_prices = {k: v for k, v in _previous_prices.items() if v[1] > cutoff}
            # If still over cap after TTL prune, drop oldest entries
            if len(_previous_prices) > _MAX_PRICE_ENTRIES:
                sorted_keys = sorted(_previous_prices, key=lambda k: _previous_prices[k][1])
                for k in sorted_keys[:len(_previous_prices) - _MAX_PRICE_ENTRIES]:
                    del _previous_prices[k]

    # Sort feed items by timestamp (newest first)
    feed.sort(key=lambda f: f["timestamp"], reverse=True)
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
        self._scan_history: list[dict] = []  # Ring buffer of last 100 scans
        self._lock = threading.Lock()
        # Multi-outcome scan cache (TTL: 5 minutes)
        self._multi_outcome_cache: list[dict] = []
        self._multi_outcome_cache_time: float = 0
        self._multi_outcome_cache_ttl: float = 300.0
        # Portfolio NO scan cache (TTL: 5 minutes)
        self._portfolio_no_cache: list[dict] = []
        self._portfolio_no_cache_time: float = 0
        self._portfolio_no_cache_ttl: float = 300.0

    async def scan(self) -> dict:
        """Run a full scan cycle. Returns summary."""
        scan_start = time.time()

        # 1. Fetch from all platforms
        t1 = time.time()
        events = await self.registry.fetch_all()
        with self._lock:
            self._last_events = events
        fetch_ms = int((time.time() - t1) * 1000)

        # 2. Match events
        t2 = time.time()
        matched = match_events(events)
        with self._lock:
            self._last_matched = matched
        match_ms = int((time.time() - t2) * 1000)

        # 3. Find arbitrage
        t3 = time.time()
        opportunities = find_arbitrage(matched)
        with self._lock:
            self._last_opportunities = opportunities
        arb_ms = int((time.time() - t3) * 1000)

        # 4. Compute feed (now includes alerts)
        feed = compute_feed(events, opportunities)
        with self._lock:
            self._last_feed = feed

        logger.info("Scan timing: fetch=%dms match=%dms arb=%dms", fetch_ms, match_ms, arb_ms)

        with self._lock:
            self._last_scan_time = time.time()

        # 5. Cache to disk
        self._save_cache(events)
        
        # 6. Save opportunity history for tracking
        for opp in opportunities:
            save_opportunity_history(opp.matched_event.match_id, opp.profit_pct, opp.net_profit_pct)

        multi_platform = sum(1 for m in matched if m.platform_count >= 2)
        elapsed_ms = int((time.time() - scan_start) * 1000)

        # 7. Log scan summary and all detected opportunities
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
                    calculation_audit=opp.calculation_audit if opp.calculation_audit else None,
                )

        logger.info("Scan complete: %d events, %d matched (%d multi-platform), %d opportunities, %dms",
                     len(events), len(matched), multi_platform, len(opportunities), elapsed_ms)

        scan_result = {
            "events_count": len(events),
            "matched_count": len(matched),
            "multi_platform_matches": multi_platform,
            "opportunities_count": len(opportunities),
            "feed_changes": len(feed),
            "scan_time": self._last_scan_time,
            "fetch_ms": fetch_ms,
            "match_ms": match_ms,
            "arb_ms": arb_ms,
            "total_ms": elapsed_ms,
        }

        # Append to scan history ring buffer (keep last 100)
        with self._lock:
            self._scan_history.append(scan_result)
            if len(self._scan_history) > 100:
                self._scan_history = self._scan_history[-100:]

        return scan_result

    def get_opportunities(self) -> list[dict]:
        with self._lock:
            return [o.to_dict() for o in self._last_opportunities]

    def get_events(self) -> list[dict]:
        with self._lock:
            return [m.to_dict() for m in self._last_matched]

    def get_feed(self) -> list[dict]:
        with self._lock:
            return self._last_feed

    def get_scan_history(self, limit: int = 20) -> list[dict]:
        """Return recent scan results with timing breakdown."""
        with self._lock:
            return list(reversed(self._scan_history[-limit:]))

    def get_scan_stats(self) -> dict:
        """Aggregate stats across recent scan history."""
        with self._lock:
            history = self._scan_history[:]
        if not history:
            return {"scan_count": 0}
        total_ms = [s["total_ms"] for s in history]
        opps = [s["opportunities_count"] for s in history]
        return {
            "scan_count": len(history),
            "avg_total_ms": round(sum(total_ms) / len(total_ms)),
            "max_total_ms": max(total_ms),
            "min_total_ms": min(total_ms),
            "avg_opportunities": round(sum(opps) / len(opps), 1),
            "total_opportunities": sum(opps),
            "last_scan_time": history[-1].get("scan_time"),
        }

    def get_trending_markets(self, limit: int = 10) -> list[dict]: # NEW: Trending markets method
        """Identify trending markets based on recent price changes and volume spikes."""
        trending = []
        now = time.time()
        
        # Consider events with significant price changes or high recent volume
        # This is a simplified heuristic and can be expanded.
        all_events_map = {f"{e.platform}:{e.event_id}": e for e in self._last_events}

        # Filter price changes from the feed for recent, significant movements
        recent_price_changes = [
            item for item in self._last_feed
            if item["type"] == "price_change" and (now - item["timestamp"]) < 3600 # within last hour
        ]
        
        # Aggregate changes by market
        market_activity = {} # key -> {"event": event, "change_count": int, "max_change_pct": float, "last_updated": float}
        for item in recent_price_changes:
            key = f"{item['platform']}:{item['event_id']}"
            if key not in market_activity:
                market_activity[key] = {
                    "event": all_events_map.get(key),
                    "change_count": 0,
                    "max_change_pct": 0.0,
                    "last_updated": 0,
                    "volume": 0,
                }
            if market_activity[key]["event"] is not None:
                market_activity[key]["change_count"] += 1
                market_activity[key]["max_change_pct"] = max(market_activity[key]["max_change_pct"], abs(item["change_pct"]))
                market_activity[key]["last_updated"] = max(market_activity[key]["last_updated"], item["timestamp"])
                market_activity[key]["volume"] = market_activity[key]["event"].volume


        # Filter and sort trending markets
        for key, data in market_activity.items():
            if data["event"] and data["change_count"] >= 2 and data["volume"] > 1000: # at least 2 changes and decent volume
                trending.append({
                    "platform": data["event"].platform,
                    "event_id": data["event"].event_id,
                    "title": data["event"].title,
                    "category": data["event"].category,
                    "current_yes_price": data["event"].yes_price,
                    "current_no_price": data["event"].no_price,
                    "volume": data["volume"],
                    "change_count": data["change_count"],
                    "max_change_pct": data["max_change_pct"],
                    "last_updated": data["last_updated"],
                    "url": data["event"].url,
                })
        
        # Sort by a combination of change magnitude and recency
        trending.sort(key=lambda x: (x["max_change_pct"] * (now - x["last_updated"]) * -1), reverse=True) # Higher change, more recent = higher score
        
        return trending[:limit]


    async def scan_multi_outcome(self) -> list[dict]:
        """Scan for multi-outcome arbitrage opportunities on Polymarket.

        Multi-outcome arb: events with 3+ outcomes where the sum of all YES
        prices is < $1.00. Buy all outcomes → guaranteed profit at resolution
        since exactly one outcome resolves to $1.00.

        Example: "Who will win the NBA championship?" with 30 teams.
        If all YES prices sum to $0.94, buying all = $0.06 guaranteed profit.

        Uses maker orders (0% fee) to enter, so profit = $1.00 - sum(prices).
        Results cached for 5 minutes to avoid hammering the API every cycle.
        """
        # Return cached results if still fresh
        if time.time() - self._multi_outcome_cache_time < self._multi_outcome_cache_ttl:
            return self._multi_outcome_cache

        try:
            import httpx
        except ImportError:
            logger.warning("httpx not available for multi-outcome scan")
            return []

        opportunities = []
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                # Fetch Polymarket events (grouped markets)
                resp = await client.get(
                    "https://gamma-api.polymarket.com/events",
                    params={
                        "closed": "false",
                        "limit": 50,
                        "order": "volume",
                        "ascending": "false",
                    }
                )
                if resp.status_code != 200:
                    logger.warning("Multi-outcome scan: API returned %d", resp.status_code)
                    return []

                events = resp.json()
                if not isinstance(events, list):
                    events = events.get("data", events.get("events", []))

                for event in events:
                    event_title = event.get("title", "")
                    markets = event.get("markets", [])

                    # Only multi-outcome events (3+ markets under one event)
                    if len(markets) < 3:
                        continue

                    # Sum all YES prices
                    total_yes = 0.0
                    valid_markets = []
                    for m in markets:
                        # Parse outcomePrices
                        yes_price = 0.0
                        outcome_prices = m.get("outcomePrices", "")
                        if isinstance(outcome_prices, str) and outcome_prices.startswith("["):
                            try:
                                prices = json.loads(outcome_prices)
                                if prices:
                                    yes_price = float(prices[0])
                            except (json.JSONDecodeError, ValueError, IndexError):
                                pass
                        if yes_price <= 0:
                            yes_price = float(m.get("bestBid", 0) or 0)

                        if yes_price > 0:
                            condition_id = m.get("conditionId", m.get("condition_id", ""))
                            valid_markets.append({
                                "title": m.get("question", m.get("title", "")),
                                "condition_id": condition_id,
                                "yes_price": round(yes_price, 4),
                                "volume": int(float(m.get("volume", 0) or 0)),
                            })
                            total_yes += yes_price

                    if len(valid_markets) < 3:
                        continue

                    # ── Exhaustiveness check ──────────────────────────────────
                    # Multi-outcome arb ONLY works when outcomes are exhaustive
                    # (exactly one resolves to $1.00). If total_yes << 1.0, the
                    # event likely has unlisted outcomes (e.g., "None of the above")
                    # and the "spread" is phantom — it belongs to missing outcomes.
                    # Reject if > 30% of probability is unaccounted for.
                    if total_yes < 0.70:
                        logger.debug("Multi-outcome skip (non-exhaustive): %s | sum=%.4f | %d outcomes",
                                     event_title[:50], total_yes, len(valid_markets))
                        continue

                    # Arb exists when sum < $1.00 (minus fee buffer)
                    # Maker orders = 0% fee, so profit = 1.0 - total_yes
                    # But we need a buffer for execution risk
                    fee_buffer = 0.01  # 1 cent buffer for rounding/execution
                    spread = 1.0 - total_yes

                    if spread > fee_buffer:
                        profit_pct = round(spread / total_yes * 100, 2) if total_yes > 0 else 0

                        opp = {
                            "opportunity_type": "multi_outcome_arb",
                            "title": event_title,
                            "canonical_title": event_title,
                            "platform": "polymarket",
                            "outcomes": valid_markets,
                            "outcome_count": len(valid_markets),
                            "total_yes_price": round(total_yes, 4),
                            "spread": round(spread, 4),
                            "profit_pct": profit_pct,
                            "buy_yes_platform": "polymarket",
                            "buy_no_platform": "polymarket",
                            "buy_yes_price": round(total_yes / len(valid_markets), 4),  # avg
                            "buy_no_price": 0,
                            "buy_yes_market_id": valid_markets[0]["condition_id"],
                            "buy_no_market_id": "",
                            "expiry": event.get("endDate", ""),
                            "volume": sum(m["volume"] for m in valid_markets),
                        }
                        opportunities.append(opp)

                        logger.info("Multi-outcome arb: %s | %d outcomes | sum=%.4f | spread=%.4f (%.2f%%)",
                                    event_title[:50], len(valid_markets), total_yes, spread, profit_pct)

                        # Log if decision logger available
                        if self._dlog:
                            self._dlog.log_opportunity_detected(
                                title=event_title,
                                strategy_type="multi_outcome_arb",
                                spread_pct=profit_pct,
                                platforms=["polymarket"],
                                yes_price=round(total_yes, 4),
                                no_price=0,
                                is_synthetic=False,
                                volume=opp["volume"],
                                event_ids=[m["condition_id"] for m in valid_markets[:5]],
                            )

        except Exception as e:
            logger.warning("Multi-outcome scan error: %s", e)

        logger.info("Multi-outcome scan: %d opportunities from grouped events", len(opportunities))
        self._multi_outcome_cache = opportunities
        self._multi_outcome_cache_time = time.time()
        return opportunities

    async def scan_portfolio_no(self) -> list[dict]:
        """Scan for Portfolio NO opportunities on Polymarket.

        Portfolio NO: In multi-outcome events (tournaments, elections), buy NO
        on all non-favorites. Since exactly one outcome wins, all other NOs
        resolve to $1.00.

        Guaranteed profit when sum(YES prices of included outcomes) > 1.0:
          - Cost = count - sum(YES_i) = sum(NO_i)
          - Min payout = count - 1 (if one included outcome wins)
          - Profit = sum(YES_i) - 1.0  (guaranteed minimum)

        Uses the same Gamma API data as scan_multi_outcome (shared cache).
        """
        if time.time() - self._portfolio_no_cache_time < self._portfolio_no_cache_ttl:
            return self._portfolio_no_cache

        try:
            import httpx
        except ImportError:
            logger.warning("httpx not available for portfolio NO scan")
            return []

        opportunities = []
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    "https://gamma-api.polymarket.com/events",
                    params={
                        "closed": "false",
                        "limit": 50,
                        "order": "volume",
                        "ascending": "false",
                    }
                )
                if resp.status_code != 200:
                    logger.warning("Portfolio NO scan: API returned %d", resp.status_code)
                    return []

                events = resp.json()
                if not isinstance(events, list):
                    events = events.get("data", events.get("events", []))

                for event in events:
                    event_title = event.get("title", "")
                    markets = event.get("markets", [])

                    # Need 4+ outcomes for portfolio NO to make sense
                    if len(markets) < 4:
                        continue

                    # Parse all outcomes with prices
                    all_outcomes = []
                    for m in markets:
                        yes_price = 0.0
                        no_price = 0.0
                        outcome_prices = m.get("outcomePrices", "")
                        if isinstance(outcome_prices, str) and outcome_prices.startswith("["):
                            try:
                                prices = json.loads(outcome_prices)
                                if len(prices) >= 1:
                                    yes_price = float(prices[0])
                                if len(prices) >= 2:
                                    no_price = float(prices[1])
                            except (json.JSONDecodeError, ValueError, IndexError):
                                pass
                        if yes_price <= 0:
                            yes_price = float(m.get("bestBid", 0) or 0)
                        if no_price <= 0 and yes_price > 0:
                            no_price = round(1.0 - yes_price, 4)

                        if yes_price > 0:
                            all_outcomes.append({
                                "title": m.get("question", m.get("title", "")),
                                "condition_id": m.get("conditionId", m.get("condition_id", "")),
                                "yes_price": round(yes_price, 4),
                                "no_price": round(no_price, 4),
                                "volume": int(float(m.get("volume", 0) or 0)),
                            })

                    if len(all_outcomes) < 4:
                        continue

                    # Sort by YES price descending (favorites first)
                    all_outcomes.sort(key=lambda o: o["yes_price"], reverse=True)
                    total_yes = sum(o["yes_price"] for o in all_outcomes)

                    # Find optimal exclusion: remove favorites until remaining
                    # sum still > 1.0 (+ fee buffer for guaranteed profit)
                    fee_buffer = 0.005  # 0.5% buffer for slippage (0% maker fees)
                    threshold = 1.0 + fee_buffer

                    favorites = []
                    remaining = list(all_outcomes)
                    remaining_yes_sum = total_yes

                    for outcome in all_outcomes:
                        if remaining_yes_sum - outcome["yes_price"] >= threshold:
                            favorites.append(outcome)
                            remaining.remove(outcome)
                            remaining_yes_sum -= outcome["yes_price"]
                        else:
                            break  # Can't exclude more without losing guarantee

                    if remaining_yes_sum < threshold:
                        continue  # Not enough overround for guaranteed profit

                    if len(remaining) < 3:
                        continue  # Need at least 3 NO legs

                    # Calculate portfolio metrics
                    no_count = len(remaining)
                    total_no_cost = sum(o["no_price"] for o in remaining)
                    guaranteed_profit = remaining_yes_sum - 1.0  # per share-set
                    profit_pct = round(guaranteed_profit / total_no_cost * 100, 2) if total_no_cost > 0 else 0

                    # Skip tiny opportunities
                    if profit_pct < 1.0:
                        continue

                    opp = {
                        "opportunity_type": "portfolio_no",
                        "title": event_title,
                        "canonical_title": event_title,
                        "platform": "polymarket",
                        "favorites_excluded": favorites,
                        "no_targets": remaining,
                        "outcome_count": len(all_outcomes),
                        "no_count": no_count,
                        "favorites_count": len(favorites),
                        "total_yes_sum": round(total_yes, 4),
                        "remaining_yes_sum": round(remaining_yes_sum, 4),
                        "total_no_cost": round(total_no_cost, 4),
                        "guaranteed_profit": round(guaranteed_profit, 4),
                        "profit_pct": profit_pct,
                        "buy_yes_platform": "polymarket",
                        "buy_no_platform": "polymarket",
                        "buy_yes_price": 0,
                        "buy_no_price": round(total_no_cost / no_count, 4),
                        "buy_yes_market_id": "",
                        "buy_no_market_id": remaining[0]["condition_id"],
                        "expiry": event.get("endDate", ""),
                        "volume": sum(o["volume"] for o in remaining),
                    }
                    opportunities.append(opp)

                    logger.info(
                        "Portfolio NO: %s | %d NOs (excl %d favs) | sum=%.4f | profit=%.4f (%.2f%%)",
                        event_title[:50], no_count, len(favorites),
                        remaining_yes_sum, guaranteed_profit, profit_pct
                    )

                    if self._dlog:
                        self._dlog.log_opportunity_detected(
                            title=event_title,
                            strategy_type="portfolio_no",
                            spread_pct=profit_pct,
                            platforms=["polymarket"],
                            yes_price=round(remaining_yes_sum, 4),
                            no_price=round(total_no_cost, 4),
                            is_synthetic=False,
                            volume=opp["volume"],
                            event_ids=[o["condition_id"] for o in remaining[:5]],
                        )

        except Exception as e:
            logger.warning("Portfolio NO scan error: %s", e)

        logger.info("Portfolio NO scan: %d opportunities", len(opportunities))
        self._portfolio_no_cache = opportunities
        self._portfolio_no_cache_time = time.time()
        return opportunities

    def _save_cache(self, events: list[NormalizedEvent]):
        """Persist latest events to disk for offline viewing."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            cache = [e.to_dict() for e in events]
            (DATA_DIR / "cache.json").write_text(json.dumps(cache, indent=2))
        except Exception as exc:
            logger.warning("Cache save failed: %s", exc)
