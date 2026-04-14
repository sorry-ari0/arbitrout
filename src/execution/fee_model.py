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

# Maker-path fee rates — used when trades go via resting limit orders.
# Polymarket maker = 0 (docs). Kalshi maker = 0.0175 × C × p × (1−p) per fill
# (help.kalshi.com/trading/fees) — converted to a fraction-of-stake basis per fill
# as (rate × (1−p)) so `× stake` gives the actual dollar fee. Limitless charges
# ~1% on entry regardless. PredictIt has no maker path; we model it via the
# resolution-side profit tax + withdrawal fee for gating purposes.
PLATFORM_MAKER_FEE_FRAC_PER_FILL = {
    "polymarket": 0.0,
    "kalshi": 0.0175,
    "limitless": 0.01,
    "robinhood": 0.0,
    "manifold": 0.0,
    "metaculus": 0.0,
}


def compute_maker_round_trip_fee_frac(platform: str, price: float, category: str = "") -> float:
    """Return modeled round-trip (entry + exit) maker fees as a fraction of stake.

    Used to subtract fee drag from Kelly edge BEFORE sizing — per Hausch & Ziemba
    (1985), transaction costs must reduce the calculated edge before f* is computed,
    not after sizing via a fractional-Kelly multiplier.

    Returns 0.0 for unknown platforms (conservative — caller will not double-subtract).
    """
    p = max(0.0, min(1.0, float(price)))
    if platform == "polymarket":
        # Maker path = 0% on Polymarket regardless of category.
        return 0.0
    if platform == "kalshi":
        rate = PLATFORM_MAKER_FEE_FRAC_PER_FILL["kalshi"]
        # Kalshi fee per fill ≈ rate · C · p · (1−p) cents; as a fraction of stake (= C·p)
        # that's rate·(1−p). Round-trip ⇒ ×2. At p=0.85 this is ~0.5%; at p=0.50, ~1.75%.
        return 2.0 * rate * (1.0 - p)
    if platform == "predictit":
        # 10% profit tax on realized profit + 5% withdrawal on net balance.
        # Per-share fee on a winning hold-to-resolution trade at entry p:
        #   profit_per_share = (1 − p);  profit_tax = 0.10 · (1 − p)
        #   withdrawal_drag  = 0.05 · (net stake after tax)  ≈ 0.05 · p
        # Divide by stake (= p) to get fraction-of-stake drag.
        if p <= 0:
            return 0.0
        profit_tax_frac = PREDICTIT_PROFIT_TAX * (1.0 - p) / p
        withdrawal_drag_frac = PREDICTIT_WITHDRAWAL_FEE
        return profit_tax_frac + withdrawal_drag_frac
    per_fill = PLATFORM_MAKER_FEE_FRAC_PER_FILL.get(platform)
    if per_fill is None:
        return 0.0
    return 2.0 * per_fill


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
