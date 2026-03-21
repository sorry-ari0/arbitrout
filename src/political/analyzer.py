"""PoliticalAnalyzer — orchestrates the political synthetic analysis pipeline.

Runs on a 15-minute asyncio loop. Reuses events from arb scanner (no redundant fetch).
Groups political events into clusters, detects relationships, sends top combos to LLM,
and produces PoliticalOpportunity objects for auto trader consumption.
"""
import asyncio
import logging
import re
import time
from datetime import datetime, timezone

from political.cache import PoliticalCache
from political.classifier import classify_contract
from political.clustering import build_clusters
from political.models import (
    PoliticalCluster, PoliticalOpportunity, PoliticalLeg, PLATFORM_FEES,
)
from political.relationships import detect_relationships, build_leg_combinations
from political.strategy import build_cluster_prompt, parse_strategy_response, validate_strategy

logger = logging.getLogger("political.analyzer")

SCAN_INTERVAL = 900  # 15 minutes
MAX_CLUSTERS_PER_CYCLE = 10
MAX_COMBOS_PER_CLUSTER = 3

# Fast crypto relevance check — matches specific crypto asset names in titles
_CRYPTO_RELEVANCE_RE = re.compile(
    r"\b(bitcoin|btc|ethereum|eth|ether|solana|sol|xrp|ripple|"
    r"dogecoin|doge|cardano|ada|avalanche|avax|chainlink|link|"
    r"polkadot|dot|polygon|pol)\b",
    re.IGNORECASE,
)


def _is_crypto_relevant(title: str) -> bool:
    """Fast check if a title contains specific crypto asset names.

    Returns True only for specific asset names (BTC, ETH, etc.),
    NOT for generic 'crypto'/'cryptocurrency'.
    """
    return bool(_CRYPTO_RELEVANCE_RE.search(title))


class PoliticalAnalyzer:
    """Orchestrates political synthetic derivative analysis."""

    def __init__(self, scanner=None, ai_advisor=None, decision_logger=None,
                 auto_trader=None):
        self.scanner = scanner
        self.ai = ai_advisor
        self.dlog = decision_logger
        self._auto_trader = auto_trader
        self.cache = PoliticalCache(ttl_seconds=SCAN_INTERVAL, max_entries=200)
        self._opportunities: list[dict] = []
        self._clusters: list[PoliticalCluster] = []
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info("Political analyzer started (interval=%ds)", SCAN_INTERVAL)

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Political analyzer stopped")

    async def _loop(self):
        while self._running:
            try:
                await self._analyze_cycle()
            except Exception as e:
                logger.error("Political analyzer cycle error: %s", e)
            await asyncio.sleep(SCAN_INTERVAL)

    async def _analyze_cycle(self):
        """One full analysis cycle."""
        t0 = time.time()

        # Get events from arb scanner cache (no redundant fetch)
        if not self.scanner:
            return
        all_events_raw = self.scanner.get_events()
        if not all_events_raw:
            return

        # Filter to political/crypto events and reconstruct NormalizedEvents
        from adapters.models import NormalizedEvent
        filtered_events = []
        for ev_dict in all_events_raw:
            markets = ev_dict.get("markets", [])
            for m in markets:
                category = m.get("category", "")
                title = m.get("title", "")
                if category == "politics" or category == "crypto" or _is_crypto_relevant(title):
                    try:
                        ne = NormalizedEvent(
                            platform=m["platform"], event_id=m["event_id"],
                            title=m["title"], category=m["category"],
                            yes_price=m["yes_price"], no_price=m["no_price"],
                            volume=m.get("volume", 0),
                            expiry=m.get("expiry", "ongoing"),
                            url=m.get("url", ""),
                            last_updated=m.get("last_updated", ""),
                            spot_price=m.get("spot_price", 0.0),
                        )
                        filtered_events.append(ne)
                    except (KeyError, TypeError):
                        continue

        if not filtered_events:
            logger.debug("Political analyzer: no political/crypto events found")
            return

        # Classify all political/crypto events
        classified = [classify_contract(ev) for ev in filtered_events]

        # Build clusters
        clusters = build_clusters(classified)
        self._clusters = clusters
        if not clusters:
            logger.debug("Political analyzer: no clusters formed from %d events", len(filtered_events))
            return

        logger.info("Political analyzer: %d political/crypto events → %d clusters",
                     len(filtered_events), len(clusters))

        # Analyze top clusters
        new_opportunities = []
        for cluster in clusters[:MAX_CLUSTERS_PER_CYCLE]:
            opps = await self._analyze_cluster(cluster)
            new_opportunities.extend(opps)

        self._opportunities = [o.to_dict() for o in new_opportunities]
        elapsed_ms = int((time.time() - t0) * 1000)
        logger.info("Political analyzer: produced %d opportunities in %dms",
                     len(new_opportunities), elapsed_ms)

        # Log to decision log
        if self.dlog and new_opportunities:
            self.dlog._write({
                "type": "political_analysis_cycle",
                "political_events": len(filtered_events),
                "clusters": len(clusters),
                "opportunities": len(new_opportunities),
                "elapsed_ms": elapsed_ms,
            })

        # Wake auto trader if we found opportunities
        if new_opportunities and self._auto_trader:
            try:
                await self._auto_trader.notify_scan_complete()
            except Exception:
                pass

    async def _analyze_cluster(self, cluster: PoliticalCluster) -> list[PoliticalOpportunity]:
        """Analyze a single cluster: detect relationships, check cache, call LLM."""
        contract_ids = [c.event.event_id for c in cluster.contracts]
        current_prices = {c.event.event_id: c.event.yes_price for c in cluster.contracts}

        # Check cache
        cached = self.cache.get(contract_ids, current_prices)
        if cached is not None:
            logger.debug("Political analyzer: cache hit for %s", cluster.cluster_id)
            return cached  # returns list[PoliticalOpportunity]

        # Detect relationships
        relationships = detect_relationships(cluster.contracts)
        if not relationships:
            self.cache.set(contract_ids, [], current_prices)
            return []

        # Build candidate leg combinations
        combos = build_leg_combinations(cluster.contracts, relationships)
        if not combos:
            self.cache.set(contract_ids, [], current_prices)
            return []

        # LLM strategy analysis
        if not self.ai or not self.ai.is_available:
            logger.debug("Political analyzer: no AI available, skipping LLM analysis")
            self.cache.set(contract_ids, [], current_prices)
            return []

        # Build prompt with top combo's relationships
        top_combo = combos[0]
        # Extract spot prices from crypto contracts for the LLM prompt
        spot_prices = {}
        for c in cluster.contracts:
            if c.crypto_asset and c.event.spot_price > 0:
                spot_prices[c.crypto_asset] = c.event.spot_price
        prompt = build_cluster_prompt(cluster, top_combo["relationships"],
                                      spot_prices=spot_prices or None)

        # Call AI provider
        try:
            providers = self.ai._get_available_providers()
            response_text = None
            for provider in providers:
                try:
                    response_text = await self.ai._call_provider(provider, prompt)
                    break
                except Exception as e:
                    logger.warning("Political LLM via %s failed: %s", provider["name"], e)
                    continue

            if not response_text:
                logger.warning("Political analyzer: all AI providers failed for %s",
                               cluster.cluster_id)
                return []

        except Exception as e:
            logger.error("Political analyzer: LLM call failed: %s", e)
            return []

        # Parse and validate
        strategies = parse_strategy_response(response_text, cluster)
        valid_strategies = [s for s in strategies if validate_strategy(s)]

        if not valid_strategies:
            self.cache.set(contract_ids, [], current_prices)
            return []

        # Convert to PoliticalOpportunity
        opportunities = []
        for strategy in valid_strategies:
            legs = []
            total_fees = 0.0
            for sleg in strategy.legs:
                contract = cluster.contracts[sleg.contract_idx - 1]
                fee = PLATFORM_FEES.get(contract.event.platform, 2.0)
                legs.append(PoliticalLeg(
                    event=contract.event,
                    contract_info=contract,
                    side=sleg.side,
                    weight=sleg.weight,
                    platform_fee_pct=fee,
                ))
                total_fees += fee

            net_ev = strategy.expected_value_pct - total_fees
            if net_ev < 1.0:
                continue

            opp = PoliticalOpportunity(
                cluster_id=cluster.cluster_id,
                strategy=strategy,
                legs=legs,
                total_fee_pct=total_fees,
                net_expected_value_pct=round(net_ev, 2),
                platforms=list({l.event.platform for l in legs}),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            opportunities.append(opp)

        self.cache.set(contract_ids, opportunities, current_prices)
        return opportunities

    def get_opportunities(self) -> list[dict]:
        """Return current political opportunities as dicts."""
        return self._opportunities

    def get_clusters(self) -> list[dict]:
        """Return current clusters as dicts."""
        return [
            {
                "cluster_id": c.cluster_id,
                "race": c.race,
                "state": c.state,
                "contract_count": len(c.contracts),
                "contracts": [
                    {
                        "event_id": ci.event.event_id,
                        "platform": ci.event.platform,
                        "title": ci.event.title,
                        "contract_type": ci.contract_type,
                        "yes_price": ci.event.yes_price,
                        "no_price": ci.event.no_price,
                        "candidates": ci.candidates,
                        "party": ci.party,
                    }
                    for ci in c.contracts
                ],
            }
            for c in self._clusters
        ]

    async def analyze_cluster_by_id(self, cluster_id: str) -> list[dict]:
        """Force re-analysis of a specific cluster (bypasses cache)."""
        cluster = next((c for c in self._clusters if c.cluster_id == cluster_id), None)
        if not cluster:
            return []
        # Clear cache for this cluster
        contract_ids = [c.event.event_id for c in cluster.contracts]
        self.cache.set(contract_ids, None, {})  # invalidate
        opps = await self._analyze_cluster(cluster)
        return [o.to_dict() for o in opps]
