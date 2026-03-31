"""Theta scanner for near-expiry prediction market mispricings."""
from datetime import date, datetime
import logging
from statistics import fmean

from event_matcher import match_events

logger = logging.getLogger(__name__)


def _parse_expiry(expiry: str) -> date | None:
    if not expiry or expiry == "ongoing":
        return None
    text = str(expiry).strip()
    if "T" in text:
        text = text.split("T", 1)[0]
    try:
        return date.fromisoformat(text)
    except ValueError:
        try:
            return datetime.strptime(text, "%b %d, %Y").date()
        except ValueError:
            return None


class ThetaScanner:
    """Find near-expiry outliers versus cross-platform consensus pricing."""

    def __init__(self, registry):
        self.registry = registry

    async def get_theta_opportunities(
        self,
        max_days_to_expiry: int = 7,
        min_edge: float = 0.08,
        min_volume: int = 0,
    ) -> list[dict]:
        events = await self.registry.fetch_all()
        matched_events = match_events(events)
        return self._scan_matched_events(
            matched_events,
            as_of=date.today(),
            max_days_to_expiry=max_days_to_expiry,
            min_edge=min_edge,
            min_volume=min_volume,
        )

    def _scan_matched_events(
        self,
        matched_events,
        as_of: date,
        max_days_to_expiry: int,
        min_edge: float,
        min_volume: int,
    ) -> list[dict]:
        opportunities: list[dict] = []

        for matched in matched_events:
            expiry_date = _parse_expiry(matched.expiry)
            if expiry_date is None:
                continue

            days_to_expiry = (expiry_date - as_of).days
            if days_to_expiry < 0 or days_to_expiry > max_days_to_expiry:
                continue

            if len(matched.markets) < 2:
                continue

            for market in matched.markets:
                if market.volume < min_volume:
                    continue

                peer_prices = [
                    other.yes_price
                    for other in matched.markets
                    if other.platform != market.platform
                ]
                if not peer_prices:
                    continue

                consensus_yes = fmean(peer_prices)
                consensus_no = 1.0 - consensus_yes
                edge = abs(consensus_yes - market.yes_price)
                if edge < min_edge:
                    continue

                buy_side = "YES" if consensus_yes > market.yes_price else "NO"
                entry_price = market.yes_price if buy_side == "YES" else market.no_price
                expected_value = consensus_yes if buy_side == "YES" else consensus_no
                expected_edge_pct = max(0.0, (expected_value - entry_price) * 100.0)
                theta_capture_pct_per_day = round(
                    expected_edge_pct / max(days_to_expiry, 1),
                    2,
                )

                opportunities.append(
                    {
                        "match_id": matched.match_id,
                        "canonical_title": matched.canonical_title,
                        "platform": market.platform,
                        "event_id": market.event_id,
                        "expiry": matched.expiry,
                        "days_to_expiry": days_to_expiry,
                        "market_yes_price": round(market.yes_price, 4),
                        "market_no_price": round(market.no_price, 4),
                        "consensus_yes_price": round(consensus_yes, 4),
                        "consensus_no_price": round(consensus_no, 4),
                        "buy_side": buy_side,
                        "edge_pct": round(edge * 100.0, 2),
                        "expected_edge_pct": round(expected_edge_pct, 2),
                        "theta_capture_pct_per_day": theta_capture_pct_per_day,
                        "volume": market.volume,
                        "peer_platforms": sorted(
                            {other.platform for other in matched.markets if other.platform != market.platform}
                        ),
                    }
                )

        opportunities.sort(
            key=lambda item: (
                item["days_to_expiry"],
                -item["edge_pct"],
                -item["volume"],
            )
        )
        return opportunities
