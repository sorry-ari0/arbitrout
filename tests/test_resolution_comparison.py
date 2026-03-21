"""Tests for resolution criteria comparison (Task 3)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from arbitrage_engine import (
    ResolutionMatch,
    _normalize_title,
    _extract_key_terms,
    _jaccard_similarity,
    _key_terms_conflict,
    _compare_resolution,
    _compare_resolution_cached,
)


# ============================================================
# ResolutionMatch dataclass
# ============================================================

class TestResolutionMatchDataclass:
    def test_fields_exist(self):
        rm = ResolutionMatch(status="match", confidence=0.95, reasoning="Test")
        assert rm.status == "match"
        assert rm.confidence == 0.95
        assert rm.reasoning == "Test"

    def test_status_values(self):
        for status in ("match", "divergent", "uncertain"):
            rm = ResolutionMatch(status=status, confidence=0.5, reasoning="")
            assert rm.status == status

    def test_confidence_range(self):
        rm = ResolutionMatch(status="match", confidence=1.0, reasoning="")
        assert 0.0 <= rm.confidence <= 1.0
        rm2 = ResolutionMatch(status="divergent", confidence=0.0, reasoning="")
        assert 0.0 <= rm2.confidence <= 1.0


# ============================================================
# _normalize_title
# ============================================================

class TestNormalizeTitle:
    def test_lowercase(self):
        assert _normalize_title("BTC Will Hit 100K") == _normalize_title("btc will hit 100k")

    def test_strips_leading_will(self):
        result = _normalize_title("Will BTC reach 100k by 2025?")
        assert not result.startswith("will ")

    def test_strips_leading_will_the(self):
        result = _normalize_title("Will the Fed raise rates in 2025?")
        assert not result.startswith("will the ")

    def test_strips_leading_is(self):
        result = _normalize_title("Is inflation above 3% in 2025?")
        assert not result.startswith("is ")

    def test_removes_punctuation(self):
        result = _normalize_title("BTC > $100k by 2025?")
        assert "?" not in result
        assert ">" not in result

    def test_collapses_whitespace(self):
        result = _normalize_title("  BTC   hit   100k  ")
        assert "  " not in result
        assert result == result.strip()


# ============================================================
# _extract_key_terms
# ============================================================

class TestExtractKeyTerms:
    def test_extracts_years(self):
        terms = _extract_key_terms("Will BTC hit 100k by 2025?")
        assert "2025" in terms["dates"]

    def test_extracts_multiple_years(self):
        terms = _extract_key_terms("Between 2025 and 2026")
        assert "2025" in terms["dates"]
        assert "2026" in terms["dates"]

    def test_extracts_months(self):
        terms = _extract_key_terms("Will it happen in March 2025?")
        assert "march" in terms["dates"]

    def test_extracts_dollar_amounts(self):
        terms = _extract_key_terms("Will the bill cost $1,000,000?")
        assert any("1,000,000" in a for a in terms["amounts"])

    def test_extracts_entities(self):
        terms = _extract_key_terms("Will the Fed raise rates?")
        assert "Fed" in terms["entities"]

    def test_extracts_trump(self):
        terms = _extract_key_terms("Will Trump win in 2026?")
        assert "Trump" in terms["entities"]

    def test_empty_title(self):
        terms = _extract_key_terms("")
        assert terms["dates"] == []
        assert terms["amounts"] == []
        assert terms["entities"] == []


# ============================================================
# _jaccard_similarity
# ============================================================

class TestJaccardSimilarity:
    def test_identical(self):
        assert _jaccard_similarity("btc hit 100k", "btc hit 100k") == 1.0

    def test_completely_different(self):
        sim = _jaccard_similarity("apple orange banana", "dog cat fish")
        assert sim == 0.0

    def test_partial_overlap(self):
        sim = _jaccard_similarity("btc hit 100k", "btc reach 100k")
        assert 0 < sim < 1.0

    def test_both_empty(self):
        assert _jaccard_similarity("", "") == 1.0

    def test_one_empty(self):
        sim = _jaccard_similarity("btc hit 100k", "")
        assert sim == 0.0

    def test_symmetry(self):
        a = "btc hit 100k by 2025"
        b = "btc reach 100k"
        assert _jaccard_similarity(a, b) == _jaccard_similarity(b, a)


# ============================================================
# _key_terms_conflict
# ============================================================

class TestKeyTermsConflict:
    def test_no_conflict_no_terms(self):
        terms_a = {"dates": [], "amounts": [], "entities": []}
        terms_b = {"dates": [], "amounts": [], "entities": []}
        assert not _key_terms_conflict(terms_a, terms_b)

    def test_conflicting_amounts(self):
        terms_a = {"dates": [], "amounts": ["$1,000,000"], "entities": []}
        terms_b = {"dates": [], "amounts": ["$2,000,000"], "entities": []}
        assert _key_terms_conflict(terms_a, terms_b)

    def test_matching_amounts(self):
        terms_a = {"dates": [], "amounts": ["$1,000,000"], "entities": []}
        terms_b = {"dates": [], "amounts": ["$1,000,000"], "entities": []}
        assert not _key_terms_conflict(terms_a, terms_b)

    def test_conflicting_years(self):
        terms_a = {"dates": ["2025"], "amounts": [], "entities": []}
        terms_b = {"dates": ["2026"], "amounts": [], "entities": []}
        assert _key_terms_conflict(terms_a, terms_b)

    def test_matching_years(self):
        terms_a = {"dates": ["2025"], "amounts": [], "entities": []}
        terms_b = {"dates": ["2025"], "amounts": [], "entities": []}
        assert not _key_terms_conflict(terms_a, terms_b)

    def test_one_side_no_year(self):
        """If one side has no year, no conflict on years."""
        terms_a = {"dates": ["2025"], "amounts": [], "entities": []}
        terms_b = {"dates": [], "amounts": [], "entities": []}
        assert not _key_terms_conflict(terms_a, terms_b)


# ============================================================
# _compare_resolution — main function
# ============================================================

class TestCompareResolution:

    def test_identical_titles_match(self):
        result = _compare_resolution(
            "Will BTC reach $100k by 2025?",
            "Will BTC reach $100k by 2025?",
            "polymarket", "kalshi",
        )
        assert result.status == "match"
        assert result.confidence >= 0.9

    def test_near_identical_titles_match(self):
        """Minor wording differences should still match."""
        result = _compare_resolution(
            "Will Bitcoin hit $100,000 by end of 2025?",
            "Will Bitcoin hit $100,000 by end of 2025",
            "polymarket", "kalshi",
        )
        assert result.status == "match"

    def test_clearly_different_titles_divergent(self):
        result = _compare_resolution(
            "Will the Fed raise rates in 2025?",
            "Will Bitcoin reach $100k by 2026?",
            "polymarket", "kalshi",
        )
        assert result.status == "divergent"
        assert result.confidence >= 0.8

    def test_different_years_divergent(self):
        result = _compare_resolution(
            "Will BTC reach $100k by 2025?",
            "Will BTC reach $100k by 2026?",
            "polymarket", "kalshi",
        )
        assert result.status == "divergent"

    def test_different_dollar_amounts_divergent(self):
        result = _compare_resolution(
            "Will the bill cost more than $1,000,000?",
            "Will the bill cost more than $2,000,000?",
            "polymarket", "kalshi",
        )
        assert result.status == "divergent"

    def test_subtle_difference_uncertain_or_match(self):
        """Moderately similar titles with no key-term conflicts."""
        result = _compare_resolution(
            "Will the unemployment rate fall below 4 percent in 2025?",
            "Will the unemployment rate go below 4 percent in 2025?",
            "polymarket", "kalshi",
        )
        # Should be uncertain or match — not divergent (titles share most words)
        assert result.status in ("uncertain", "match")

    def test_returns_resolution_match_object(self):
        result = _compare_resolution(
            "Will Trump win in 2026?",
            "Will Trump win in 2026?",
            "polymarket", "kalshi",
        )
        assert isinstance(result, ResolutionMatch)
        assert result.status in ("match", "divergent", "uncertain")
        assert isinstance(result.confidence, float)
        assert isinstance(result.reasoning, str)

    def test_reasoning_non_empty(self):
        result = _compare_resolution(
            "Will BTC reach $100k?",
            "Will the Fed raise rates?",
            "polymarket", "kalshi",
        )
        assert len(result.reasoning) > 0


# ============================================================
# Cache behaviour
# ============================================================

class TestCacheConsistency:

    def test_same_pair_same_result(self):
        """Calling twice returns identical result."""
        r1 = _compare_resolution(
            "Will Trump win the 2026 election?",
            "Will Trump win the 2026 election?",
            "polymarket", "kalshi",
        )
        r2 = _compare_resolution(
            "Will Trump win the 2026 election?",
            "Will Trump win the 2026 election?",
            "polymarket", "kalshi",
        )
        assert r1.status == r2.status
        assert r1.confidence == r2.confidence
        assert r1.reasoning == r2.reasoning

    def test_order_independent(self):
        """Swapping title_a / title_b produces the same result."""
        title_a = "Will Bitcoin reach 100k by 2025?"
        title_b = "Will the Fed raise rates in 2026?"

        r1 = _compare_resolution(title_a, title_b, "polymarket", "kalshi")
        r2 = _compare_resolution(title_b, title_a, "kalshi", "polymarket")
        assert r1.status == r2.status
        assert r1.confidence == r2.confidence
