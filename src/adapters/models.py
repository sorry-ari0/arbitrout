"""Arbitrout data models — shared across all adapters and engines."""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


# ============================================================
# NORMALIZED EVENT
# ============================================================
@dataclass
class NormalizedEvent:
    """A single market from one platform, normalized to common schema."""
    platform: str           # "kalshi", "polymarket", etc.
    event_id: str           # platform-specific ID
    title: str              # "Will Bitcoin exceed $100K by Dec 2026?"
    category: str           # crypto, politics, sports, economics, weather, culture
    yes_price: float        # 0.0 - 1.0
    no_price: float         # 0.0 - 1.0
    volume: int             # trading volume (dollar or contract count)
    expiry: str             # ISO date string or "ongoing"
    url: str                # direct link to market on platform
    last_updated: str = ""  # ISO datetime string

    def __post_init__(self):
        if not self.last_updated:
            self.last_updated = datetime.now(timezone.utc).isoformat()
        # Clamp prices to [0, 1]
        self.yes_price = max(0.0, min(1.0, self.yes_price))
        self.no_price = max(0.0, min(1.0, self.no_price))

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
# MATCHED EVENT
# ============================================================
@dataclass
class MatchedEvent:
    """Same real-world event found on multiple platforms."""
    match_id: str                       # unique ID for this match group
    canonical_title: str                # best title to display
    category: str
    expiry: str
    markets: list[NormalizedEvent] = field(default_factory=list)
    match_type: str = "auto"            # "auto" or "manual"

    @property
    def platform_count(self) -> int:
        return len(set(m.platform for m in self.markets))

    def to_dict(self) -> dict:
        return {
            "match_id": self.match_id,
            "canonical_title": self.canonical_title,
            "category": self.category,
            "expiry": self.expiry,
            "match_type": self.match_type,
            "platform_count": self.platform_count,
            "markets": [m.to_dict() for m in self.markets],
        }


# ============================================================
# ARBITRAGE OPPORTUNITY
# ============================================================
@dataclass
class ArbitrageOpportunity:
    """A profitable spread found across two platforms."""
    matched_event: MatchedEvent
    buy_yes_platform: str       # platform with cheapest YES
    buy_yes_price: float
    buy_no_platform: str        # platform with cheapest NO
    buy_no_price: float
    spread: float               # 1.0 - (yes + no) = profit per $1
    profit_pct: float           # spread * 100
    combined_volume: int
    buy_yes_event_id: str = ""  # event_id of the YES market
    buy_no_event_id: str = ""   # event_id of the NO market
    is_synthetic: bool = False          # True if markets have different price targets
    synthetic_info: dict = field(default_factory=dict)  # price targets, scenarios, etc.
    net_profit_pct: float = 0.0       # Guaranteed profit % after all platform fees
    confidence: str = "medium"         # "high", "medium", "low", "very_low"

    @property
    def yes_allocation_pct(self) -> float:
        """% of capital to allocate to YES contracts."""
        total = self.buy_yes_price + self.buy_no_price
        return round((self.buy_no_price / total) * 100, 1) if total > 0 else 50.0

    @property
    def no_allocation_pct(self) -> float:
        """% of capital to allocate to NO contracts."""
        total = self.buy_yes_price + self.buy_no_price
        return round((self.buy_yes_price / total) * 100, 1) if total > 0 else 50.0

    def to_dict(self) -> dict:
        d = {
            "matched_event": self.matched_event.to_dict(),
            "buy_yes_platform": self.buy_yes_platform,
            "buy_yes_price": self.buy_yes_price,
            "buy_yes_event_id": self.buy_yes_event_id,
            "buy_no_platform": self.buy_no_platform,
            "buy_no_price": self.buy_no_price,
            "buy_no_event_id": self.buy_no_event_id,
            "spread": round(self.spread, 4),
            "profit_pct": round(self.profit_pct, 2),
            "combined_volume": self.combined_volume,
            "yes_allocation_pct": self.yes_allocation_pct,
            "no_allocation_pct": self.no_allocation_pct,
            "is_synthetic": self.is_synthetic,
            "net_profit_pct": round(self.net_profit_pct, 2),
            "confidence": self.confidence,
        }
        if self.synthetic_info:
            d["synthetic_info"] = self.synthetic_info
        return d
