"""Tests for political data models (Task 1) and contract classifier (Task 2)."""
import sys
from pathlib import Path

# Add src to path so imports work
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from adapters.models import NormalizedEvent
from political.models import (
    PoliticalContractInfo, PoliticalCluster, SyntheticLeg, Scenario,
    PoliticalSyntheticStrategy, PoliticalLeg, PoliticalOpportunity,
    PLATFORM_FEES,
)
from political.classifier import (
    classify_contract, _extract_state, _extract_party, _clean_race,
)


# ============================================================
# HELPERS
# ============================================================
def _make_event(title: str, platform: str = "polymarket",
                yes: float = 0.55, no: float = 0.45,
                volume: int = 5000) -> NormalizedEvent:
    """Create a NormalizedEvent for testing."""
    return NormalizedEvent(
        platform=platform,
        event_id=f"{platform}-{hash(title) % 10000}",
        title=title,
        category="politics",
        yes_price=yes,
        no_price=no,
        volume=volume,
        expiry="2026-11-03",
        url=f"https://{platform}.com/test",
    )


# ============================================================
# TASK 1: MODEL TESTS
# ============================================================
class TestPoliticalModels:
    """Tests for political data model creation and serialization."""

    def test_contract_info_creation(self):
        """PoliticalContractInfo can be created with all fields."""
        event = _make_event("Talarico wins TX Senate")
        info = PoliticalContractInfo(
            event=event,
            contract_type="candidate_win",
            candidates=["Talarico"],
            party=None,
            race="TX Senate",
            state="TX",
            threshold=None,
            direction=None,
        )
        assert info.contract_type == "candidate_win"
        assert info.candidates == ["Talarico"]
        assert info.race == "TX Senate"
        assert info.state == "TX"
        assert info.event.platform == "polymarket"

    def test_strategy_creation(self):
        """PoliticalSyntheticStrategy holds legs, scenarios, and metrics."""
        leg = SyntheticLeg(contract_idx=0, event_id="pm-1234", side="yes", weight=0.6)
        scenario = Scenario(outcome="Talarico wins", probability=0.55, pnl_pct=12.0)
        strategy = PoliticalSyntheticStrategy(
            cluster_id="cluster-tx-senate",
            strategy_name="TX Senate margin decomposition",
            legs=[leg],
            scenarios=[scenario],
            expected_value_pct=6.6,
            win_probability=0.55,
            max_loss_pct=-100.0,
            confidence=0.72,
            reasoning="Margin bracket is mispriced relative to win probability",
        )
        assert strategy.strategy_name == "TX Senate margin decomposition"
        assert len(strategy.legs) == 1
        assert strategy.legs[0].side == "yes"
        assert len(strategy.scenarios) == 1
        assert strategy.expected_value_pct == 6.6

    def test_opportunity_to_dict(self):
        """PoliticalOpportunity.to_dict() produces auto-trader-compatible dict."""
        event_a = _make_event("Talarico wins TX Senate", platform="polymarket",
                              yes=0.55, no=0.45, volume=10000)
        event_b = _make_event("Cruz wins TX Senate", platform="kalshi",
                              yes=0.40, no=0.60, volume=8000)

        info_a = PoliticalContractInfo(event=event_a, contract_type="candidate_win",
                                        candidates=["Talarico"], race="TX Senate", state="TX")
        info_b = PoliticalContractInfo(event=event_b, contract_type="candidate_win",
                                        candidates=["Cruz"], race="TX Senate", state="TX")

        leg_s = SyntheticLeg(contract_idx=0, event_id=event_a.event_id, side="yes", weight=0.6)
        scenario = Scenario(outcome="Talarico wins", probability=0.55, pnl_pct=12.0)
        strategy = PoliticalSyntheticStrategy(
            cluster_id="cluster-tx-senate",
            strategy_name="TX Senate conditional hedge",
            legs=[leg_s],
            scenarios=[scenario],
            expected_value_pct=8.5,
            win_probability=0.55,
            max_loss_pct=-100.0,
            confidence=0.70,
            reasoning="Hedge on correlated outcomes",
        )

        leg_a = PoliticalLeg(event=event_a, contract_info=info_a, side="yes",
                             weight=0.6, platform_fee_pct=2.0)
        leg_b = PoliticalLeg(event=event_b, contract_info=info_b, side="no",
                             weight=0.4, platform_fee_pct=1.5)

        opp = PoliticalOpportunity(
            cluster_id="cluster-tx-senate",
            strategy=strategy,
            legs=[leg_a, leg_b],
            total_fee_pct=1.8,
            net_expected_value_pct=6.7,
            platforms=["polymarket", "kalshi"],
        )

        d = opp.to_dict()

        # Auto-trader required fields
        assert d["opportunity_type"] == "political_synthetic"
        assert d["is_synthetic"] is True
        assert d["title"] == "TX Senate conditional hedge"
        assert d["canonical_title"] == "TX Senate conditional hedge"
        assert d["profit_pct"] == 6.7
        assert d["buy_yes_platform"] == "polymarket"
        assert d["buy_no_platform"] == "kalshi"
        assert d["buy_yes_market_id"] == event_a.event_id
        assert d["buy_no_market_id"] == event_b.event_id
        assert d["buy_yes_price"] == 0.55  # primary leg (weight 0.6) YES price
        assert d["buy_no_price"] == 0.60   # secondary leg (weight 0.4) NO price
        assert d["volume"] == 18000
        assert d["expiry"] == "2026-11-03"
        assert "strategy" in d
        assert d["strategy"]["confidence"] == 0.70
        assert len(d["platforms"]) == 2


# ============================================================
# TASK 2: CLASSIFIER TESTS
# ============================================================
class TestClassifier:
    """Tests for classify_contract() and its helper functions."""

    # -- candidate_win --
    @pytest.mark.parametrize("title,expected_candidate,expected_race", [
        ("Talarico wins TX Senate", "Talarico", "TX Senate"),
        ("John Talarico wins TX Senate", "John Talarico", "TX Senate"),
        ("Ted Cruz to win Texas Senate", "Ted Cruz", "Texas Senate"),
        ("Jane Smith will win PA Governor", "Jane Smith", "PA Governor"),
        ("Bob Jones winning Ohio House", "Bob Jones", "Ohio House"),
    ])
    def test_candidate_win(self, title, expected_candidate, expected_race):
        event = _make_event(title)
        info = classify_contract(event)
        assert info.contract_type == "candidate_win"
        if expected_candidate:
            assert expected_candidate in info.candidates
        if expected_race:
            assert info.race == expected_race

    # -- party_outcome --
    @pytest.mark.parametrize("title,expected_party,expected_race", [
        ("Democratic candidate wins TX Senate", "dem", "TX Senate"),
        ("GOP holds Georgia Senate", "gop", "Georgia Senate"),
        ("Republican takes PA Governor", "gop", "PA Governor"),
        ("Dem wins Ohio House", "dem", "Ohio House"),
    ])
    def test_party_outcome(self, title, expected_party, expected_race):
        event = _make_event(title)
        info = classify_contract(event)
        assert info.contract_type == "party_outcome"
        assert info.party == expected_party
        assert info.race == expected_race

    # -- margin_bracket --
    @pytest.mark.parametrize("title,expected_threshold", [
        ("John Smith wins by >5%", 5.0),
        ("margin greater than 10%", 10.0),
        ("Ted Cruz wins by more than 3%", 3.0),
    ])
    def test_margin_bracket(self, title, expected_threshold):
        event = _make_event(title)
        info = classify_contract(event)
        assert info.contract_type == "margin_bracket"
        assert info.threshold == expected_threshold

    # -- vote_share --
    @pytest.mark.parametrize("title,expected_threshold", [
        ("Dem gets >48% in TX Senate", 48.0),
        ("John Smith gets above 52%", 52.0),
        ("vote share above 45%", 45.0),
    ])
    def test_vote_share(self, title, expected_threshold):
        event = _make_event(title)
        info = classify_contract(event)
        assert info.contract_type == "vote_share"
        assert info.threshold == expected_threshold

    # -- matchup --
    @pytest.mark.parametrize("title,expected_candidates", [
        ("Talarico vs Cruz", ["Talarico", "Cruz"]),
        ("John Smith versus Jane Doe", ["John Smith", "Jane Doe"]),
        ("Bob Jones v. Alice Brown", ["Bob Jones", "Alice Brown"]),
    ])
    def test_matchup(self, title, expected_candidates):
        event = _make_event(title)
        info = classify_contract(event)
        assert info.contract_type == "matchup"
        assert info.candidates == expected_candidates

    # -- yes_no_binary fallback --
    @pytest.mark.parametrize("title", [
        "Will TX have a runoff?",
        "Senate filibuster reform in 2026?",
        "Government shutdown before December?",
    ])
    def test_yes_no_binary(self, title):
        event = _make_event(title)
        info = classify_contract(event)
        assert info.contract_type == "yes_no_binary"

    # -- state extraction --
    def test_extract_state_abbreviation(self):
        assert _extract_state("Talarico wins TX Senate") == "TX"

    def test_extract_state_full_name(self):
        assert _extract_state("Democratic candidate wins Texas Senate") == "TX"

    def test_extract_state_none(self):
        assert _extract_state("Will there be a runoff?") is None

    # -- party extraction --
    def test_extract_party_dem(self):
        assert _extract_party("Democratic candidate wins") == "dem"

    def test_extract_party_gop(self):
        assert _extract_party("GOP holds Senate") == "gop"

    def test_extract_party_none(self):
        assert _extract_party("John Smith wins race") is None

    # -- race cleaning --
    def test_clean_race(self):
        assert _clean_race("  TX Senate?  ") == "TX Senate"
        assert _clean_race("PA  Governor...") == "PA Governor"

    # -- margin_bracket checked before candidate_win --
    def test_margin_before_candidate(self):
        """'wins by >5%' should be margin_bracket, not candidate_win."""
        event = _make_event("John Smith wins by >5%")
        info = classify_contract(event)
        assert info.contract_type == "margin_bracket"
        assert info.threshold == 5.0

    # -- event reference preserved --
    def test_event_preserved(self):
        """The original NormalizedEvent is accessible on the result."""
        event = _make_event("Will TX have a runoff?", platform="kalshi")
        info = classify_contract(event)
        assert info.event is event
        assert info.event.platform == "kalshi"

    # -- platform fees dict --
    def test_platform_fees(self):
        """PLATFORM_FEES contains expected platforms and values."""
        assert PLATFORM_FEES["polymarket"] == 2.0
        assert PLATFORM_FEES["kalshi"] == 1.5
        assert PLATFORM_FEES["predictit"] == 10.0
        assert PLATFORM_FEES["limitless"] == 2.0
