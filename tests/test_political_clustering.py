"""Tests for political contract clustering (Task 3)."""
import sys
from pathlib import Path

# Add src to path so imports work
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from adapters.models import NormalizedEvent
from political.models import PoliticalContractInfo, PoliticalCluster
from political.clustering import build_clusters, _normalize_race


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


def _make_contract(title: str, race: str | None, state: str | None = None,
                   contract_type: str = "candidate_win",
                   platform: str = "polymarket") -> PoliticalContractInfo:
    """Create a PoliticalContractInfo for testing."""
    event = _make_event(title, platform=platform)
    return PoliticalContractInfo(
        event=event,
        contract_type=contract_type,
        candidates=[],
        party=None,
        race=race,
        state=state,
        threshold=None,
        direction=None,
    )


# ============================================================
# NORMALIZATION TESTS
# ============================================================
class TestNormalizeRace:
    """Tests for _normalize_race() helper."""

    def test_basic_normalization(self):
        """Two-letter state abbreviation is lowercased and sorted."""
        assert _normalize_race("TX Senate") == "senate-tx"

    def test_full_state_name_replaced(self):
        """Full state name 'Texas' is replaced with abbreviation 'tx'."""
        assert _normalize_race("Texas Senate") == "senate-tx"

    def test_word_order_irrelevant(self):
        """Different word orders produce the same key."""
        assert _normalize_race("Senate TX") == "senate-tx"
        assert _normalize_race("TX Senate") == "senate-tx"

    def test_filler_words_removed(self):
        """Filler words (the, race, election, seat, special) are stripped."""
        assert _normalize_race("The Texas Senate Race") == "senate-tx"
        assert _normalize_race("Special Election TX Senate") == "senate-tx"

    def test_punctuation_removed(self):
        """Punctuation in race string is stripped."""
        assert _normalize_race("TX Senate!") == "senate-tx"

    def test_case_insensitive(self):
        """Normalization is case insensitive."""
        assert _normalize_race("tx senate") == "senate-tx"
        assert _normalize_race("TX SENATE") == "senate-tx"


# ============================================================
# CLUSTERING TESTS
# ============================================================
class TestBuildClusters:
    """Tests for build_clusters()."""

    def test_same_race_grouped(self):
        """Two contracts with 'TX Senate' should form 1 cluster with 2 contracts."""
        c1 = _make_contract("Talarico wins TX Senate", race="TX Senate", state="TX")
        c2 = _make_contract("Cruz wins TX Senate", race="TX Senate", state="TX")

        clusters = build_clusters([c1, c2])

        assert len(clusters) == 1
        assert len(clusters[0].contracts) == 2
        assert clusters[0].cluster_id == "senate-tx-2026"

    def test_different_races_separate(self):
        """'TX Senate' and 'CA Governor' should form 2 separate clusters."""
        c1 = _make_contract("Talarico wins TX Senate", race="TX Senate", state="TX")
        c2 = _make_contract("Cruz wins TX Senate", race="TX Senate", state="TX")
        c3 = _make_contract("Smith wins CA Governor", race="CA Governor", state="CA")
        c4 = _make_contract("Jones wins CA Governor", race="CA Governor", state="CA")

        clusters = build_clusters([c1, c2, c3, c4])

        assert len(clusters) == 2
        cluster_ids = {c.cluster_id for c in clusters}
        assert "senate-tx-2026" in cluster_ids
        assert "ca-governor-2026" in cluster_ids

    def test_fuzzy_race_matching(self):
        """'TX Senate' and 'Texas Senate' should be grouped into 1 cluster."""
        c1 = _make_contract("Talarico wins TX Senate", race="TX Senate", state="TX")
        c2 = _make_contract("Cruz wins Texas Senate", race="Texas Senate", state="TX")

        clusters = build_clusters([c1, c2])

        assert len(clusters) == 1
        assert len(clusters[0].contracts) == 2
        assert clusters[0].cluster_id == "senate-tx-2026"

    def test_min_two_contracts(self):
        """A single contract should NOT form a cluster (minimum 2 required)."""
        c1 = _make_contract("Talarico wins TX Senate", race="TX Senate", state="TX")

        clusters = build_clusters([c1])

        assert len(clusters) == 0

    def test_none_race_excluded(self):
        """Contracts with race=None should be excluded from clustering."""
        c1 = _make_contract("Will TX have a runoff?", race=None, state="TX")
        c2 = _make_contract("Senate filibuster reform?", race=None, state=None)

        clusters = build_clusters([c1, c2])

        assert len(clusters) == 0

    def test_matched_events_deduplicated(self):
        """matched_events should contain deduplicated events by event_id."""
        event = _make_event("Talarico wins TX Senate")
        # Two contracts sharing the same event
        c1 = PoliticalContractInfo(
            event=event, contract_type="candidate_win",
            candidates=["Talarico"], race="TX Senate", state="TX",
        )
        c2 = PoliticalContractInfo(
            event=event, contract_type="margin_bracket",
            candidates=["Talarico"], race="TX Senate", state="TX",
            threshold=5.0, direction="above",
        )

        clusters = build_clusters([c1, c2])

        assert len(clusters) == 1
        # Same event used for both contracts → only 1 in matched_events
        assert len(clusters[0].matched_events) == 1
        assert clusters[0].matched_events[0].event_id == event.event_id

    def test_empty_input(self):
        """Empty contract list returns empty cluster list."""
        clusters = build_clusters([])
        assert len(clusters) == 0

    def test_cluster_preserves_race_and_state(self):
        """Cluster's race and state should come from the first contract."""
        c1 = _make_contract("Talarico wins TX Senate", race="TX Senate", state="TX")
        c2 = _make_contract("Cruz wins Texas Senate", race="Texas Senate", state="TX")

        clusters = build_clusters([c1, c2])

        assert clusters[0].race == "TX Senate"
        assert clusters[0].state == "TX"
