"""Cross-asset matcher for prediction markets versus synthetic spot hedges."""
import asyncio
from datetime import date, datetime
import logging
import re

from adapters.commodities import CommoditiesAdapter
from adapters.crypto_spot import CryptoSpotAdapter
from event_matcher import _extract_crypto

logger = logging.getLogger(__name__)


_COMMODITY_ALIASES = {
    "gold": "Gold",
    "silver": "Silver",
    "crude oil": "Crude Oil",
    "oil": "Crude Oil",
    "wti": "Crude Oil",
    "natural gas": "Natural Gas",
    "nat gas": "Natural Gas",
    "copper": "Copper",
    "corn": "Corn",
    "wheat": "Wheat",
    "soybeans": "Soybeans",
    "soybean": "Soybeans",
}

_ABOVE_WORDS = ("above", "over", "exceed", "surpass", "reach", "hit", "higher")
_BELOW_WORDS = ("below", "under", "fall", "drop", "lower", "dip")


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


def _extract_direction(title: str) -> str | None:
    lower = title.lower()
    if any(word in lower for word in _ABOVE_WORDS):
        return "above"
    if any(word in lower for word in _BELOW_WORDS):
        return "below"
    return None


def _extract_dollar_target(title: str) -> float | None:
    match = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)\s*(k|K)?", title)
    if not match:
        return None
    value = float(match.group(1).replace(",", ""))
    if match.group(2):
        value *= 1000.0
    return value


def _extract_commodity(title: str) -> dict | None:
    lower = title.lower()
    commodity_name = None
    for alias, canonical in _COMMODITY_ALIASES.items():
        if alias in lower:
            commodity_name = canonical
            break
    if not commodity_name:
        return None

    target = _extract_dollar_target(title)
    direction = _extract_direction(title)
    if target is None or direction is None:
        return None

    return {
        "asset_class": "commodity",
        "asset_id": commodity_name.lower().replace(" ", "-"),
        "asset_name": commodity_name,
        "direction": direction,
        "target_price": target,
    }


def _parse_contract(event) -> dict | None:
    crypto = _extract_crypto(event.title)
    if crypto.get("ticker") and crypto.get("price") and crypto.get("direction") in {"above", "below"}:
        return {
            "asset_class": "crypto",
            "asset_id": crypto["ticker"],
            "asset_name": crypto["ticker"],
            "direction": crypto["direction"],
            "target_price": float(crypto["price"]),
            "expiry": _parse_expiry(event.expiry),
        }

    commodity = _extract_commodity(event.title)
    if commodity:
        commodity["expiry"] = _parse_expiry(event.expiry)
        return commodity

    return None


def _price_close(a: float, b: float, tolerance: float = 0.03) -> bool:
    base = max(abs(a), abs(b), 1.0)
    return abs(a - b) / base <= tolerance


class CrossAssetMatcher:
    def __init__(self, registry, crypto_adapter=None, commodities_adapter=None):
        self.registry = registry
        self.crypto_adapter = crypto_adapter or CryptoSpotAdapter()
        self.commodities_adapter = commodities_adapter or CommoditiesAdapter()

    async def get_opportunities(
        self,
        min_profit: float = 0.02,
        max_expiry_gap_days: int = 200,
    ) -> list[dict]:
        prediction_events = await self.registry.fetch_all()
        reference_events = await self._fetch_reference_events()
        return self._match_events(
            prediction_events=prediction_events,
            reference_events=reference_events,
            min_profit=min_profit,
            max_expiry_gap_days=max_expiry_gap_days,
        )

    async def _fetch_reference_events(self):
        crypto_events, commodity_events = await asyncio.gather(
            self.crypto_adapter.fetch_events(),
            self.commodities_adapter.fetch_events(),
        )
        return list(crypto_events) + list(commodity_events)

    def _match_events(
        self,
        prediction_events,
        reference_events,
        min_profit: float,
        max_expiry_gap_days: int,
    ) -> list[dict]:
        opportunities: list[dict] = []
        parsed_references = [
            (reference, _parse_contract(reference))
            for reference in reference_events
        ]

        for prediction in prediction_events:
            parsed_prediction = _parse_contract(prediction)
            if not parsed_prediction:
                continue

            for reference, parsed_reference in parsed_references:
                if not parsed_reference:
                    continue
                if parsed_reference["asset_class"] != parsed_prediction["asset_class"]:
                    continue
                if parsed_reference["asset_id"] != parsed_prediction["asset_id"]:
                    continue
                if parsed_reference["direction"] != parsed_prediction["direction"]:
                    continue
                if not _price_close(parsed_prediction["target_price"], parsed_reference["target_price"]):
                    continue

                pred_expiry = parsed_prediction.get("expiry")
                ref_expiry = parsed_reference.get("expiry")
                if pred_expiry and ref_expiry:
                    if abs((pred_expiry - ref_expiry).days) > max_expiry_gap_days:
                        continue

                buy_yes_cost = prediction.yes_price + reference.no_price
                buy_no_cost = prediction.no_price + reference.yes_price
                if buy_yes_cost <= buy_no_cost:
                    prediction_side = "YES"
                    reference_side = "NO"
                    total_cost = buy_yes_cost
                else:
                    prediction_side = "NO"
                    reference_side = "YES"
                    total_cost = buy_no_cost

                spread = 1.0 - total_cost
                if spread < min_profit:
                    continue

                opportunities.append(
                    {
                        "prediction_platform": prediction.platform,
                        "prediction_event_id": prediction.event_id,
                        "prediction_title": prediction.title,
                        "prediction_volume": prediction.volume,
                        "reference_platform": reference.platform,
                        "reference_event_id": reference.event_id,
                        "reference_title": reference.title,
                        "reference_volume": reference.volume,
                        "asset_class": parsed_prediction["asset_class"],
                        "asset": parsed_prediction["asset_name"],
                        "direction": parsed_prediction["direction"],
                        "target_price": parsed_prediction["target_price"],
                        "expiry": prediction.expiry,
                        "spot_price": reference.spot_price,
                        "prediction_yes_price": round(prediction.yes_price, 4),
                        "reference_yes_price": round(reference.yes_price, 4),
                        "combined_cost": round(total_cost, 4),
                        "guaranteed_profit_pct": round(spread * 100.0, 2),
                        "model_gap_pct": round(abs(prediction.yes_price - reference.yes_price) * 100.0, 2),
                        "combined_volume": prediction.volume + reference.volume,
                        "prediction_side": prediction_side,
                        "reference_side": reference_side,
                        "hedge_instructions": (
                            f"Buy {prediction_side} on {prediction.platform}, "
                            f"hedge with {reference_side} on {reference.platform} "
                            f"for {parsed_prediction['asset_name']} {parsed_prediction['direction']} "
                            f"{parsed_prediction['target_price']:,.2f}."
                        ),
                    }
                )

        opportunities.sort(
            key=lambda item: (
                -item["guaranteed_profit_pct"],
                -item["model_gap_pct"],
            )
        )
        return opportunities
