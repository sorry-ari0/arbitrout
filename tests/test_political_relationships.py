"""Tests for political contract relationship detection."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from political.relationships import detect_relationships, build_leg_combinations
from political.models import PoliticalContractInfo, PoliticalCluster
from adapters.models import NormalizedEvent


def _make_info(title, contract_type, race="TX Senate", state="TX",
               candidates=None, party=None, threshold=None,
               platform="polymarket", event_id="ev1", yes=0.50, no=0.50):
    ev = NormalizedEvent(
        platform=platform, event_id=event_id, title=title,
        category="politics", yes_price=yes, no_price=no,
        volume=1000, expiry="2026-11-03", url=f"https://{platform}.com/{event_id}",
    )
    return PoliticalContractInfo(
        event=ev, contract_type=contract_type,
        candidates=candidates or [], party=party,
        race=race, state=state,
        threshold=threshold, direction=None,
    )


class TestDetectRelationships:
    def test_mispriced_correlation(self):
        """Same race, same outcome, different platforms, price diff >3%."""
        c1 = _make_info("A wins TX Senate", "candidate_win", candidates=["A"],
                        platform="polymarket", event_id="e1", yes=0.62)
        c2 = _make_info("A wins TX Senate", "candidate_win", candidates=["A"],
                        platform="kalshi", event_id="e2", yes=0.55)
        rels = detect_relationships([c1, c2])
        assert any(r["type"] == "mispriced_correlation" for r in rels)

    def test_candidate_party_link(self):
        """candidate_win + party_outcome for same race, candidate is that party."""
        c1 = _make_info("Talarico wins TX Senate", "candidate_win",
                        candidates=["Talarico"], party="dem", event_id="e1", yes=0.62)
        c2 = _make_info("Democrat wins TX Senate", "party_outcome",
                        party="dem", event_id="e2", yes=0.55)
        rels = detect_relationships([c1, c2])
        assert any(r["type"] == "candidate_party_link" for r in rels)

    def test_margin_decomposition(self):
        """candidate_win + margin_bracket for same candidate."""
        c1 = _make_info("Talarico wins TX Senate", "candidate_win",
                        candidates=["Talarico"], event_id="e1", yes=0.62)
        c2 = _make_info("Talarico wins by >5%", "margin_bracket",
                        candidates=["Talarico"], threshold=5.0, event_id="e2", yes=0.38)
        rels = detect_relationships([c1, c2])
        assert any(r["type"] == "margin_decomposition" for r in rels)

    def test_conditional_hedge(self):
        """Two candidate_win for same race, different candidates."""
        c1 = _make_info("Talarico wins TX Senate", "candidate_win",
                        candidates=["Talarico"], event_id="e1")
        c2 = _make_info("Cruz wins TX Senate", "candidate_win",
                        candidates=["Cruz"], event_id="e2")
        rels = detect_relationships([c1, c2])
        assert any(r["type"] == "conditional_hedge" for r in rels)

    def test_bracket_spread(self):
        """Two margin_bracket at different thresholds."""
        c1 = _make_info("Wins by >5%", "margin_bracket", threshold=5.0, event_id="e1")
        c2 = _make_info("Margin <2%", "margin_bracket", threshold=2.0, event_id="e2")
        rels = detect_relationships([c1, c2])
        assert any(r["type"] == "bracket_spread" for r in rels)

    def test_no_relationship_binary_only(self):
        """Two yes_no_binary on same platform shouldn't produce relationships."""
        c1 = _make_info("Shutdown?", "yes_no_binary", race=None, event_id="e1")
        c2 = _make_info("Recession?", "yes_no_binary", race=None, event_id="e2")
        rels = detect_relationships([c1, c2])
        assert len(rels) == 0


class TestBuildLegCombinations:
    def test_caps_at_4_legs(self):
        """Should not produce combinations with more than 4 legs."""
        contracts = [
            _make_info(f"C{i} wins TX", "candidate_win", candidates=[f"C{i}"],
                       event_id=f"e{i}", yes=0.5 + i * 0.02)
            for i in range(6)
        ]
        rels = detect_relationships(contracts)
        combos = build_leg_combinations(contracts, rels)
        for combo in combos:
            assert len(combo["contracts"]) <= 4

    def test_min_score_filter(self):
        """Combinations below min score should be filtered."""
        c1 = _make_info("A", "yes_no_binary", event_id="e1")
        c2 = _make_info("B", "yes_no_binary", event_id="e2")
        rels = detect_relationships([c1, c2])
        combos = build_leg_combinations([c1, c2], rels)
        # No real relationships → score too low → filtered
        assert len(combos) == 0


def _make_crypto_info(title, crypto_asset, event_category, crypto_direction="positive",
                      crypto_threshold=None, platform="polymarket", event_id="ev1",
                      yes=0.50, no=0.50):
    """Helper for creating crypto_event PoliticalContractInfo."""
    ev = NormalizedEvent(
        platform=platform, event_id=event_id, title=title,
        category="crypto", yes_price=yes, no_price=no,
        volume=1000, expiry="2026-12-31", url=f"https://{platform}.com/{event_id}",
    )
    return PoliticalContractInfo(
        event=ev, contract_type="crypto_event",
        candidates=[], party=None, race=None, state=None,
        threshold=crypto_threshold, direction=None,
        crypto_asset=crypto_asset, event_category=event_category,
        crypto_direction=crypto_direction, crypto_threshold=crypto_threshold,
    )


class TestCryptoRelationships:
    """Tests for crypto-specific relationship types."""

    def test_crypto_regulatory_hedge(self):
        """price_target positive + regulatory negative → crypto_regulatory_hedge (3.0x)."""
        c1 = _make_crypto_info("BTC above $150K", "BTC", "price_target",
                               crypto_direction="positive", crypto_threshold=150000, event_id="e1")
        c2 = _make_crypto_info("SEC classifies BTC as security", "BTC", "regulatory",
                               crypto_direction="negative", event_id="e2")
        rels = detect_relationships([c1, c2])
        hedge_rels = [r for r in rels if r["type"] == "crypto_regulatory_hedge"]
        assert len(hedge_rels) == 1
        assert hedge_rels[0]["score"] == 3.0

    def test_crypto_price_spread(self):
        """Two price_target, same asset, different thresholds → crypto_price_spread (1.5x)."""
        c1 = _make_crypto_info("ETH above $5K", "ETH", "price_target",
                               crypto_threshold=5000, event_id="e1")
        c2 = _make_crypto_info("ETH above $3K", "ETH", "price_target",
                               crypto_threshold=3000, event_id="e2")
        rels = detect_relationships([c1, c2])
        spread_rels = [r for r in rels if r["type"] == "crypto_price_spread"]
        assert len(spread_rels) == 1
        assert spread_rels[0]["score"] == 1.5

    def test_cross_crypto_correlation(self):
        """Different assets, same event_category → cross_crypto_correlation (2.0x)."""
        c1 = _make_crypto_info("BTC above $150K", "BTC", "price_target",
                               crypto_threshold=150000, event_id="e1")
        c2 = _make_crypto_info("ETH above $5K", "ETH", "price_target",
                               crypto_threshold=5000, event_id="e2")
        rels = detect_relationships([c1, c2])
        cross_rels = [r for r in rels if r["type"] == "cross_crypto_correlation"]
        assert len(cross_rels) == 1
        assert cross_rels[0]["score"] == 2.0

    def test_crypto_event_catalyst(self):
        """regulatory + price_target, same asset → crypto_event_catalyst (2.5x)."""
        c1 = _make_crypto_info("ETH ETF approved", "ETH", "regulatory",
                               crypto_direction="positive", event_id="e1")
        c2 = _make_crypto_info("ETH above $7K", "ETH", "price_target",
                               crypto_threshold=7000, event_id="e2")
        rels = detect_relationships([c1, c2])
        cat_rels = [r for r in rels if r["type"] == "crypto_event_catalyst"]
        assert len(cat_rels) == 1
        assert cat_rels[0]["score"] == 2.5

    def test_same_threshold_no_price_spread(self):
        """Two price_target with identical thresholds → NO crypto_price_spread."""
        c1 = _make_crypto_info("BTC above $100K", "BTC", "price_target",
                               crypto_threshold=100000, event_id="e1")
        c2 = _make_crypto_info("BTC exceeds $100K", "BTC", "price_target",
                               crypto_threshold=100000, event_id="e2")
        rels = detect_relationships([c1, c2])
        spread_rels = [r for r in rels if r["type"] == "crypto_price_spread"]
        assert len(spread_rels) == 0

    def test_crypto_rels_dont_trigger_on_political(self):
        """Political contracts must NOT match crypto relationship types."""
        c1 = _make_info("A wins TX Senate", "candidate_win", candidates=["A"],
                        platform="polymarket", event_id="e1", yes=0.62)
        c2 = _make_info("B wins TX Senate", "candidate_win", candidates=["B"],
                        platform="polymarket", event_id="e2", yes=0.55)
        rels = detect_relationships([c1, c2])
        crypto_types = {"crypto_regulatory_hedge", "crypto_price_spread",
                        "cross_crypto_correlation", "crypto_event_catalyst"}
        for r in rels:
            assert r["type"] not in crypto_types
