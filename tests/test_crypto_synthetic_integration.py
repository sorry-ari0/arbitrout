"""Integration test for the crypto synthetic hedging pipeline."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from adapters.models import NormalizedEvent
from political.classifier import classify_contract
from political.clustering import build_clusters
from political.relationships import detect_relationships, build_leg_combinations
from political.strategy import build_cluster_prompt


def _ev(title, platform="polymarket", event_id=None, category="crypto",
        yes=0.50, no=0.50):
    return NormalizedEvent(
        platform=platform, event_id=event_id or f"ev-{hash(title) % 10000}",
        title=title, category=category, yes_price=yes, no_price=no,
        volume=10000, expiry="2026-12-31",
        url=f"https://{platform}.com/test",
    )


class TestCryptoSyntheticPipeline:
    """Full pipeline integration test."""

    def test_classify_cluster_relate_prompt(self):
        """Crypto events flow through classify → cluster → relate → prompt."""
        events = [
            _ev("Will Bitcoin be above $150,000 by end of 2026?", event_id="btc-150k", yes=0.35),
            _ev("Will SEC classify Bitcoin as a security?", event_id="btc-sec", yes=0.15),
            _ev("Will Ethereum reach $5,000?", event_id="eth-5k", yes=0.55),
            _ev("Will Ethereum ETF be approved by SEC?", event_id="eth-etf", yes=0.65),
        ]

        # Classify
        classified = [classify_contract(ev) for ev in events]
        assert all(c.contract_type == "crypto_event" for c in classified)

        # Cluster
        clusters = build_clusters(classified)
        cluster_ids = {c.cluster_id for c in clusters}
        assert "crypto-btc-2026" in cluster_ids
        assert "crypto-eth-2026" in cluster_ids

        # Relationships within BTC cluster
        btc_cluster = next(c for c in clusters if c.cluster_id == "crypto-btc-2026")
        btc_rels = detect_relationships(btc_cluster.contracts)
        assert len(btc_rels) > 0
        btc_rel_types = {r["type"] for r in btc_rels}
        assert "crypto_regulatory_hedge" in btc_rel_types

        # Relationships within ETH cluster
        eth_cluster = next(c for c in clusters if c.cluster_id == "crypto-eth-2026")
        eth_rels = detect_relationships(eth_cluster.contracts)
        assert len(eth_rels) > 0
        eth_rel_types = {r["type"] for r in eth_rels}
        assert "crypto_event_catalyst" in eth_rel_types

        # Build prompt for BTC cluster
        btc_combos = build_leg_combinations(btc_cluster.contracts, btc_rels)
        prompt = build_cluster_prompt(btc_cluster, btc_rels)
        assert "Asset: BTC" in prompt
        assert "Crypto Market Context" in prompt
        assert "crypto_regulatory_hedge" in prompt

    def test_mixed_political_crypto(self):
        """Political and crypto events produce separate cluster types."""
        events = [
            _ev("Talarico wins TX Senate", category="politics", event_id="pol1", yes=0.55),
            _ev("Cruz wins TX Senate", category="politics", event_id="pol2", yes=0.40),
            _ev("Will Bitcoin be above $150,000?", event_id="btc1", yes=0.35),
            _ev("Will SEC classify Bitcoin as a security?", event_id="btc2", yes=0.15),
        ]

        classified = [classify_contract(ev) for ev in events]

        # Political events should NOT be classified as crypto
        assert classified[0].contract_type in ("candidate_win", "yes_no_binary")
        assert classified[1].contract_type in ("candidate_win", "yes_no_binary")
        # Crypto events should be classified as crypto_event
        assert classified[2].contract_type == "crypto_event"
        assert classified[3].contract_type == "crypto_event"

        clusters = build_clusters(classified)
        crypto_clusters = [c for c in clusters if c.cluster_id.startswith("crypto-")]
        political_clusters = [c for c in clusters if not c.cluster_id.startswith("crypto-")]

        assert len(crypto_clusters) >= 1
        # Political may or may not cluster depending on normalization

    def test_opportunity_dict_type(self):
        """Crypto opportunity to_dict() returns crypto_synthetic type."""
        from political.models import (
            PoliticalOpportunity, PoliticalSyntheticStrategy,
            PoliticalLeg, PoliticalContractInfo, SyntheticLeg, Scenario,
        )
        ev = _ev("BTC above $150K", event_id="btc-150k", yes=0.35)
        info = PoliticalContractInfo(
            event=ev, contract_type="crypto_event",
            crypto_asset="BTC", event_category="price_target",
            crypto_direction="positive", crypto_threshold=150000.0,
        )
        strategy = PoliticalSyntheticStrategy(
            cluster_id="crypto-btc-2026", strategy_name="BTC Regulatory Hedge",
            legs=[SyntheticLeg(contract_idx=1, event_id="btc-150k", side="yes", weight=1.0)],
            scenarios=[Scenario(outcome="BTC rallies", probability=0.5, pnl_pct=20.0)],
            expected_value_pct=8.0, win_probability=0.55,
            max_loss_pct=-40.0, confidence=0.7,
        )
        leg = PoliticalLeg(event=ev, contract_info=info, side="yes", weight=1.0, platform_fee_pct=2.0)
        opp = PoliticalOpportunity(
            cluster_id="crypto-btc-2026", strategy=strategy,
            legs=[leg], total_fee_pct=2.0, net_expected_value_pct=6.0,
            platforms=["polymarket"],
        )
        d = opp.to_dict()
        assert d["opportunity_type"] == "crypto_synthetic"
        assert d["is_synthetic"] is True
        assert d["cluster_id"] == "crypto-btc-2026"
