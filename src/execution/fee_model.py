"""Shared platform fee helpers for arb screening and execution gating."""
from __future__ import annotations

PLATFORM_ENTRY_FEE_RATES = {
    "polymarket": 0.0,      # maker path; taker handled separately by category helper
    "kalshi": 0.01,
    "predictit": 0.0,       # profit taxed at resolution instead of entry
    "limitless": 0.01,
    "robinhood": 0.0,
    "coinbase_spot": 0.006,
    "kraken": 0.0026,
    "manifold": 0.0,
    "metaculus": 0.0,
}
DEFAULT_ENTRY_FEE_RATE = 0.0
PREDICTIT_PROFIT_TAX = 0.10
PREDICTIT_WITHDRAWAL_FEE = 0.05


def get_platform_entry_fee_rate(platform: str, price: float, category: str = "") -> float:
    """Return the modeled entry fee rate for a leg at the given price."""
    if platform == "polymarket":
        from execution.paper_executor import get_taker_fee_rate

        return get_taker_fee_rate(category, price)
    return PLATFORM_ENTRY_FEE_RATES.get(platform, DEFAULT_ENTRY_FEE_RATE)


def predictit_payout_after_fees(entry_price: float) -> float:
    """Return the $1 binary payout net of PredictIt's profit/withdrawal drag."""
    after_tax = 1.0 - PREDICTIT_PROFIT_TAX * (1.0 - entry_price)
    return after_tax * (1.0 - PREDICTIT_WITHDRAWAL_FEE)


def compute_binary_leg_payout(platform: str, entry_price: float) -> float:
    """Return net payout of a winning binary leg after platform-specific resolution fees."""
    if platform == "predictit":
        return predictit_payout_after_fees(entry_price)
    return 1.0


def compute_cross_platform_net_edge_pct(
    yes_platform: str,
    yes_price: float,
    no_platform: str,
    no_price: float,
    yes_category: str = "",
    no_category: str = "",
) -> float:
    """Guaranteed cross-platform net edge in spread points (cents per $1 payout)."""
    yes_fee_rate = get_platform_entry_fee_rate(yes_platform, yes_price, yes_category)
    no_fee_rate = get_platform_entry_fee_rate(no_platform, no_price, no_category)
    total_cost = yes_price + no_price + yes_price * yes_fee_rate + no_price * no_fee_rate
    worst_payout = min(
        compute_binary_leg_payout(yes_platform, yes_price),
        compute_binary_leg_payout(no_platform, no_price),
    )
    return (worst_payout - total_cost) * 100.0
