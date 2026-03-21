"""Political synthetic derivative data models.

All dataclasses used by the political analysis pipeline:
classification, clustering, strategy generation, and opportunity output.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from adapters.models import NormalizedEvent


# ============================================================
# CONTRACT CLASSIFICATION
# ============================================================
@dataclass
class PoliticalContractInfo:
    """Classification result for a single political contract."""
    event: NormalizedEvent          # source event (has event_id, platform, prices)
    contract_type: str              # candidate_win, party_outcome, margin_bracket,
                                    # vote_share, matchup, yes_no_binary, crypto_event
    candidates: list[str] = field(default_factory=list)  # extracted candidate names
    party: Optional[str] = None     # "dem", "gop", or None
    race: Optional[str] = None      # "TX Senate", "President", etc.
    state: Optional[str] = None     # two-letter state abbreviation
    threshold: Optional[float] = None   # for margin/vote_share brackets
    direction: Optional[str] = None     # "above", "below", "between"
    # Crypto extension fields (None for political contracts)
    crypto_asset: Optional[str] = None       # "BTC", "ETH", "SOL", etc.
    event_category: Optional[str] = None     # "regulatory", "price_target", "technical", "market_event"
    crypto_direction: Optional[str] = None   # "positive", "negative", "neutral"
    crypto_threshold: Optional[float] = None # dollar value for price_target contracts


# ============================================================
# CLUSTERING
# ============================================================
@dataclass
class PoliticalCluster:
    """A group of related political contracts (same race/state)."""
    cluster_id: str                     # unique cluster identifier
    race: Optional[str] = None          # e.g. "TX Senate"
    state: Optional[str] = None         # two-letter state abbreviation
    contracts: list[PoliticalContractInfo] = field(default_factory=list)
    matched_events: list[NormalizedEvent] = field(default_factory=list)


# ============================================================
# STRATEGY MODELS
# ============================================================
@dataclass
class SyntheticLeg:
    """One leg of a synthetic derivative strategy (index into cluster contracts)."""
    contract_idx: int       # index into the cluster's contracts list
    event_id: str           # platform event ID for execution
    side: str               # "yes" or "no"
    weight: float           # capital allocation weight (0.0 - 1.0)


@dataclass
class Scenario:
    """One possible outcome scenario with probability and P&L."""
    outcome: str            # description, e.g. "Talarico wins by >5%"
    probability: float      # 0.0 - 1.0
    pnl_pct: float          # expected P&L as percentage (can be negative)


@dataclass
class PoliticalSyntheticStrategy:
    """LLM-generated strategy for a political cluster."""
    cluster_id: str
    strategy_name: str              # e.g. "TX Senate margin decomposition"
    legs: list[SyntheticLeg] = field(default_factory=list)
    scenarios: list[Scenario] = field(default_factory=list)
    expected_value_pct: float = 0.0     # weighted-average P&L across scenarios
    win_probability: float = 0.0        # probability of positive outcome
    max_loss_pct: float = 0.0           # worst-case scenario loss
    confidence: str | float = 0.0        # LLM self-assessed confidence: float 0-1 or string "high"/"medium"/"low"
    reasoning: str = ""                 # LLM explanation of the trade thesis


# ============================================================
# OPPORTUNITY OUTPUT
# ============================================================
@dataclass
class PoliticalLeg:
    """One execution leg of a political opportunity (fully resolved from cluster)."""
    event: NormalizedEvent
    contract_info: PoliticalContractInfo
    side: str                   # "yes" or "no"
    weight: float               # capital allocation weight (0.0 - 1.0)
    platform_fee_pct: float     # fee for this platform


# Platform fee schedule (taker fees as percentage)
PLATFORM_FEES: dict[str, float] = {
    "polymarket": 2.0,
    "kalshi": 1.5,
    "predictit": 10.0,
    "limitless": 2.0,
}


@dataclass
class PoliticalOpportunity:
    """A fully-resolved political synthetic opportunity ready for the auto trader."""
    cluster_id: str
    strategy: PoliticalSyntheticStrategy
    legs: list[PoliticalLeg] = field(default_factory=list)
    total_fee_pct: float = 0.0              # weighted sum of platform fees
    net_expected_value_pct: float = 0.0     # expected_value - total_fee
    platforms: list[str] = field(default_factory=list)
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        """Convert to dict compatible with the auto trader opportunity format.

        The auto trader reads these fields:
          title, canonical_title, profit_pct, buy_yes_price, buy_no_price,
          buy_yes_platform, buy_no_platform, buy_yes_market_id, buy_no_market_id,
          volume, expiry, is_synthetic, opportunity_type
        """
        # Primary leg (highest weight) is the "YES" side; secondary is "NO" side.
        # If only one leg, it serves as both.
        sorted_legs = sorted(self.legs, key=lambda lg: lg.weight, reverse=True)
        primary = sorted_legs[0] if sorted_legs else None
        secondary = sorted_legs[1] if len(sorted_legs) > 1 else primary

        title = self.strategy.strategy_name
        canonical_title = title

        # Prices: use the primary/secondary leg event prices based on side
        buy_yes_price = 0.0
        buy_no_price = 0.0
        buy_yes_platform = ""
        buy_no_platform = ""
        buy_yes_market_id = ""
        buy_no_market_id = ""
        volume = 0
        expiry = ""

        if primary:
            if primary.side == "yes":
                buy_yes_price = primary.event.yes_price
            else:
                buy_yes_price = primary.event.no_price
            buy_yes_platform = primary.event.platform
            buy_yes_market_id = primary.event.event_id
            volume += primary.event.volume
            expiry = primary.event.expiry

        if secondary and secondary is not primary:
            if secondary.side == "yes":
                buy_no_price = secondary.event.yes_price
            else:
                buy_no_price = secondary.event.no_price
            buy_no_platform = secondary.event.platform
            buy_no_market_id = secondary.event.event_id
            volume += secondary.event.volume
            # Use latest expiry
            if secondary.event.expiry > expiry:
                expiry = secondary.event.expiry
        elif primary:
            # Single-leg: mirror primary for the NO side
            buy_no_price = primary.event.no_price if primary.side == "yes" else primary.event.yes_price
            buy_no_platform = primary.event.platform
            buy_no_market_id = primary.event.event_id

        return {
            "opportunity_type": "political_synthetic",
            "title": title,
            "canonical_title": canonical_title,
            "profit_pct": round(self.net_expected_value_pct, 2),
            "buy_yes_price": buy_yes_price,
            "buy_no_price": buy_no_price,
            "buy_yes_platform": buy_yes_platform,
            "buy_no_platform": buy_no_platform,
            "buy_yes_market_id": buy_yes_market_id,
            "buy_no_market_id": buy_no_market_id,
            "volume": volume,
            "expiry": expiry,
            "is_synthetic": True,
            "cluster_id": self.cluster_id,
            "total_fee_pct": round(self.total_fee_pct, 2),
            "strategy": {
                "name": self.strategy.strategy_name,
                "expected_value_pct": round(self.strategy.expected_value_pct, 2),
                "win_probability": round(self.strategy.win_probability, 2),
                "max_loss_pct": round(self.strategy.max_loss_pct, 2),
                "confidence": round(self.strategy.confidence, 2) if isinstance(self.strategy.confidence, (int, float)) else self.strategy.confidence,
                "reasoning": self.strategy.reasoning,
                "leg_count": len(self.legs),
                "scenarios": [
                    {"outcome": s.outcome, "probability": round(s.probability, 2),
                     "pnl_pct": round(s.pnl_pct, 2)}
                    for s in self.strategy.scenarios
                ],
            },
            "platforms": self.platforms,
            "created_at": self.created_at,
        }
