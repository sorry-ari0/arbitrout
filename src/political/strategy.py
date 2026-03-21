"""LLM prompt building, response parsing, and validation for political strategies."""
import json
import logging
import re

from political.models import (
    PoliticalCluster, PoliticalSyntheticStrategy,
    SyntheticLeg, Scenario, PLATFORM_FEES,
)

logger = logging.getLogger("political.strategy")


def build_cluster_prompt(cluster: PoliticalCluster, relationships: list[dict],
                         spot_prices: dict[str, float] | None = None) -> str:
    """Build LLM prompt for a single cluster (political or crypto)."""
    contracts_text = []
    for i, c in enumerate(cluster.contracts, 1):
        contracts_text.append(
            f'  {i}. "{c.event.title}" | {c.event.platform} | '
            f'YES=${c.event.yes_price:.2f} NO=${c.event.no_price:.2f}'
        )

    rels_text = []
    for r in relationships:
        a, b = r["pair"]
        rels_text.append(f"  - ({a+1},{b+1}): {r['type']} — {r['details']}")

    # Determine header: crypto uses asset name, political uses race/state
    is_crypto = cluster.cluster_id.startswith("crypto-")
    if is_crypto:
        asset = cluster.cluster_id.split("-")[1].upper() if "-" in cluster.cluster_id else "CRYPTO"
        header_line = f"Asset: {asset}"
    else:
        header_line = f"Race: {cluster.race} {cluster.state or ''}"

    prompt = f"""You are a political prediction market analyst. For each cluster below,
analyze the contracts and recommend optimal synthetic positions.

IMPORTANT: All expected value and P&L figures must be AFTER platform fees.
Fee rates (round-trip): Polymarket=2%, Kalshi=1.5%, PredictIt=10%, Limitless=2%.

[CLUSTER:{cluster.cluster_id}]
{header_line}
Contracts:
{chr(10).join(contracts_text)}

Pre-classified relationships:
{chr(10).join(rels_text) if rels_text else '  (none detected)'}

For each recommended position, respond with this exact JSON structure:
{{
  "strategies": [{{
    "strategy_name": "human-readable name",
    "legs": [{{"contract": 1, "side": "YES", "weight": 0.5}}],
    "scenarios": [{{"outcome": "description", "probability": 0.6, "pnl_pct": 12.5}}],
    "expected_value_pct": 8.2,
    "win_probability": 0.65,
    "max_loss_pct": -45.0,
    "confidence": "high",
    "reasoning": "explanation"
  }}]
}}

Respond with ONLY valid JSON. No preamble, no explanation outside the JSON."""

    # Append crypto market context for crypto clusters
    if is_crypto:
        prompt += _build_crypto_context(cluster, spot_prices=spot_prices)

    return prompt


def _build_crypto_context(cluster: PoliticalCluster,
                          spot_prices: dict[str, float] | None = None) -> str:
    """Build crypto market context block for the LLM prompt."""
    assets = set()
    for c in cluster.contracts:
        if c.crypto_asset:
            assets.add(c.crypto_asset)

    price_lines = []
    for asset in sorted(assets):
        if spot_prices and asset in spot_prices:
            price_lines.append(f"- {asset}: ${spot_prices[asset]:,.2f}")
        else:
            price_lines.append(f"- {asset}: (price unavailable)")

    return f"""

## Crypto Market Context
Current spot prices:
{chr(10).join(price_lines)}

Annualized volatility: ~60% (crypto-wide assumption)

Strategy guidance for crypto contracts:
- Regulatory events (SEC, CFTC) typically cause 10-30% drawdowns on negative resolution
- Price target contracts have implied probability based on distance from current price
- Hedge legs should offset directional risk of other legs
- Prefer strategies where at least one scenario is profitable even if crypto drops 20%"""


def parse_strategy_response(response_text: str,
                             cluster: PoliticalCluster) -> list[PoliticalSyntheticStrategy]:
    """Parse LLM JSON response into PoliticalSyntheticStrategy objects."""
    # Strip markdown code fences if present
    cleaned = re.sub(r"```(?:json)?\s*", "", response_text).strip()
    cleaned = cleaned.rstrip("`")
    # Handle trailing commas
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Political strategy: invalid JSON response")
        return []

    if isinstance(data, dict) and "strategies" in data:
        raw_strategies = data["strategies"]
    elif isinstance(data, list):
        raw_strategies = data
    else:
        return []

    strategies = []
    for s in raw_strategies:
        try:
            legs_raw = s.get("legs", [])
            if not legs_raw:
                continue

            legs = []
            for leg in legs_raw:
                idx = leg.get("contract", 0)
                if idx < 1 or idx > len(cluster.contracts):
                    continue
                contract = cluster.contracts[idx - 1]
                legs.append(SyntheticLeg(
                    contract_idx=idx,
                    event_id=contract.event.event_id,
                    side=leg.get("side", "YES"),
                    weight=leg.get("weight", 1.0 / len(legs_raw)),
                ))

            if not legs:
                continue

            scenarios = [
                Scenario(
                    outcome=sc.get("outcome", ""),
                    probability=sc.get("probability", 0),
                    pnl_pct=sc.get("pnl_pct", 0),
                )
                for sc in s.get("scenarios", [])
            ]

            strategy = PoliticalSyntheticStrategy(
                cluster_id=cluster.cluster_id,
                strategy_name=s.get("strategy_name", "Unnamed"),
                legs=legs,
                scenarios=scenarios,
                expected_value_pct=s.get("expected_value_pct", 0),
                win_probability=s.get("win_probability", 0),
                max_loss_pct=s.get("max_loss_pct", 0),
                confidence=s.get("confidence", "low"),
                reasoning=s.get("reasoning", ""),
            )
            strategies.append(strategy)
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("Political strategy: failed to parse strategy entry: %s", e)
            continue

    return strategies


def validate_strategy(strategy: PoliticalSyntheticStrategy) -> bool:
    """Post-LLM validation. Returns True if strategy passes all checks."""
    if strategy.win_probability < 0.50:
        return False
    if strategy.max_loss_pct < -60.0:
        return False
    if strategy.expected_value_pct < 3.0:
        return False
    if strategy.confidence == "low":
        return False
    return True
