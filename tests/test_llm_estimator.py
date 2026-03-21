"""Tests for LLM estimator module (TDD — no real API calls)."""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from positions.llm_estimator import LLMEstimator, EstimateResult


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def estimator():
    return LLMEstimator(
        anthropic_api_key="test-anthropic-key",
        gemini_api_key="test-gemini-key",
    )


@pytest.fixture
def platform_prices():
    return [
        {"platform": "polymarket", "yes_price": 0.50, "no_price": 0.50},
        {"platform": "kalshi", "yes_price": 0.48, "no_price": 0.52},
    ]


@pytest.fixture
def news_headlines():
    return ["Fed signals rate cut in June", "Inflation data comes in hot"]


def _make_claude_response(probability: float, confidence: str = "high", reasoning: str = "test") -> MagicMock:
    """Build a mock httpx response that looks like Claude's Messages API."""
    payload = {"probability": probability, "confidence": confidence, "reasoning": reasoning}
    text = json.dumps(payload)
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "content": [{"text": text}]
    }
    return resp


def _make_gemini_response(probability: float, confidence: str = "high", reasoning: str = "test") -> MagicMock:
    """Build a mock httpx response that looks like Gemini's generateContent API."""
    payload = {"probability": probability, "confidence": confidence, "reasoning": reasoning}
    text = json.dumps(payload)
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": text}]}}]
    }
    return resp


# ── Dataclass structure ───────────────────────────────────────────────────────

class TestEstimateResultStructure:
    def test_fields_exist(self):
        result = EstimateResult(
            consensus_prob=0.65,
            edge_pct=15.0,
            confidence="high",
            models={"claude": 0.65, "gemini": 0.65},
            should_boost=True,
            reasoning="test reasoning",
        )
        assert result.consensus_prob == 0.65
        assert result.edge_pct == 15.0
        assert result.confidence == "high"
        assert result.models == {"claude": 0.65, "gemini": 0.65}
        assert result.should_boost is True
        assert result.reasoning == "test reasoning"


# ── Both models agree → should_boost True when edge > 5% ─────────────────────

class TestBothModelsAgree:
    @pytest.mark.asyncio
    async def test_both_agree_large_edge_should_boost(self, estimator, platform_prices, news_headlines):
        """Claude=0.70, Gemini=0.68 → agree within 10%, consensus=0.69, edge vs yes=0.50 → 19% → boost."""
        claude_resp = _make_claude_response(0.70)
        gemini_resp = _make_gemini_response(0.68)

        with patch.object(estimator._http, "post", new=AsyncMock(side_effect=[claude_resp, gemini_resp])):
            result = await estimator.estimate("Will BTC hit 100k?", platform_prices, news_headlines)

        assert result is not None
        assert result.confidence == "high"
        assert result.models["claude"] == pytest.approx(0.70)
        assert result.models["gemini"] == pytest.approx(0.68)
        assert result.consensus_prob == pytest.approx(0.69)
        assert result.edge_pct == pytest.approx(19.0)
        assert result.should_boost is True

    @pytest.mark.asyncio
    async def test_both_agree_small_edge_no_boost(self, estimator, platform_prices, news_headlines):
        """Claude=0.52, Gemini=0.51 → agree within 10%, edge vs yes=0.50 → 1.5% < 5% → no boost."""
        claude_resp = _make_claude_response(0.52)
        gemini_resp = _make_gemini_response(0.51)

        with patch.object(estimator._http, "post", new=AsyncMock(side_effect=[claude_resp, gemini_resp])):
            result = await estimator.estimate("Will BTC hit 100k?", platform_prices, news_headlines)

        assert result is not None
        assert result.confidence == "high"
        assert result.should_boost is False

    @pytest.mark.asyncio
    async def test_medium_confidence_with_edge_boosts(self, estimator, platform_prices, news_headlines):
        """Models agree within 15% but not 10% → medium confidence, still boosts if edge > 5%."""
        claude_resp = _make_claude_response(0.70)
        gemini_resp = _make_gemini_response(0.58)  # diff = 0.12 → medium

        with patch.object(estimator._http, "post", new=AsyncMock(side_effect=[claude_resp, gemini_resp])):
            result = await estimator.estimate("Will BTC hit 100k?", platform_prices, news_headlines)

        assert result is not None
        assert result.confidence == "medium"
        assert result.should_boost is True  # edge is large and confidence is not "low"


# ── Models disagree (>15%) → should_boost False, confidence="low" ────────────

class TestModelsDisagree:
    @pytest.mark.asyncio
    async def test_large_disagreement_no_boost(self, estimator, platform_prices, news_headlines):
        """Claude=0.80, Gemini=0.40 → diff=0.40 > 15% → low confidence, no boost."""
        claude_resp = _make_claude_response(0.80)
        gemini_resp = _make_gemini_response(0.40)

        with patch.object(estimator._http, "post", new=AsyncMock(side_effect=[claude_resp, gemini_resp])):
            result = await estimator.estimate("Will BTC hit 100k?", platform_prices, news_headlines)

        assert result is not None
        assert result.confidence == "low"
        assert result.should_boost is False

    @pytest.mark.asyncio
    async def test_exactly_15pct_diff_is_medium(self, estimator, platform_prices, news_headlines):
        """Diff exactly 0.15 → medium (spec says 'within 15%' means <= 0.15 → medium)."""
        claude_resp = _make_claude_response(0.70)
        gemini_resp = _make_gemini_response(0.55)  # diff = 0.15, within 15% → medium

        with patch.object(estimator._http, "post", new=AsyncMock(side_effect=[claude_resp, gemini_resp])):
            result = await estimator.estimate("Will BTC hit 100k?", platform_prices, news_headlines)

        assert result is not None
        assert result.confidence == "medium"  # 0.15 is within 15%, so medium not low
        # edge = |0.625 - 0.50| = 12.5% > 5% AND confidence != "low" → should_boost=True
        assert result.should_boost is True


# ── One model fails → confidence="low", should_boost=False ───────────────────

class TestOneModelFails:
    @pytest.mark.asyncio
    async def test_claude_fails_gemini_succeeds(self, estimator, platform_prices, news_headlines):
        """Claude raises exception, Gemini succeeds → confidence="low", should_boost=False."""
        gemini_resp = _make_gemini_response(0.70)

        async def side_effect(*args, **kwargs):
            url = args[0] if args else kwargs.get("url", "")
            if "anthropic" in str(url):
                raise Exception("Connection timeout")
            return gemini_resp

        with patch.object(estimator._http, "post", new=AsyncMock(side_effect=side_effect)):
            result = await estimator.estimate("Will BTC hit 100k?", platform_prices, news_headlines)

        assert result is not None
        assert result.confidence == "low"
        assert result.should_boost is False
        assert "gemini" in result.models
        assert "claude" not in result.models

    @pytest.mark.asyncio
    async def test_gemini_fails_claude_succeeds(self, estimator, platform_prices, news_headlines):
        """Gemini raises exception, Claude succeeds → confidence="low", should_boost=False."""
        claude_resp = _make_claude_response(0.65)

        async def side_effect(*args, **kwargs):
            url = args[0] if args else kwargs.get("url", "")
            if "gemini" in str(url) or "googleapis" in str(url):
                raise Exception("API error")
            return claude_resp

        with patch.object(estimator._http, "post", new=AsyncMock(side_effect=side_effect)):
            result = await estimator.estimate("Will BTC hit 100k?", platform_prices, news_headlines)

        assert result is not None
        assert result.confidence == "low"
        assert result.should_boost is False
        assert "claude" in result.models
        assert "gemini" not in result.models


# ── Both fail → return None ───────────────────────────────────────────────────

class TestBothModelsFail:
    @pytest.mark.asyncio
    async def test_both_fail_returns_none(self, estimator, platform_prices, news_headlines):
        """Both Claude and Gemini raise exceptions → return None."""
        with patch.object(estimator._http, "post", new=AsyncMock(side_effect=Exception("Network error"))):
            result = await estimator.estimate("Will BTC hit 100k?", platform_prices, news_headlines)

        assert result is None


# ── Rate limiting ─────────────────────────────────────────────────────────────

class TestRateLimiting:
    @pytest.mark.asyncio
    async def test_first_10_succeed_11th_returns_none(self, platform_prices, news_headlines):
        """Max 10 calls per cycle. The 11th call should return None without hitting the API."""
        est = LLMEstimator(anthropic_api_key="key-a", gemini_api_key="key-g")

        claude_resp = _make_claude_response(0.70)
        gemini_resp = _make_gemini_response(0.68)

        with patch.object(est._http, "post", new=AsyncMock(side_effect=[
            # Alternate claude/gemini responses for 10 calls (20 HTTP POSTs total)
            *([claude_resp, gemini_resp] * 10),
        ])):
            results = []
            for i in range(11):
                r = await est.estimate(f"Market {i}", platform_prices, news_headlines)
                results.append(r)

        # First 10 should succeed
        for i in range(10):
            assert results[i] is not None, f"Call {i+1} should have succeeded"
        # 11th should be None (rate limited)
        assert results[10] is None

    @pytest.mark.asyncio
    async def test_reset_cycle_resets_counter(self, platform_prices, news_headlines):
        """reset_cycle() resets the per-cycle counter, allowing more calls."""
        est = LLMEstimator(anthropic_api_key="key-a", gemini_api_key="key-g")

        claude_resp = _make_claude_response(0.70)
        gemini_resp = _make_gemini_response(0.68)

        with patch.object(est._http, "post", new=AsyncMock(return_value=None)) as mock_post:
            # Use fresh mocks that always return valid responses
            mock_post.return_value = MagicMock()
            mock_post.side_effect = None

            # Exhaust cycle (10 calls)
            for _ in range(10):
                est._cycle_count = 10  # Force counter to limit

            # 11th call should be None
            result = await est.estimate("Market 11", platform_prices, news_headlines)
            assert result is None

            # Reset and try again
            est.reset_cycle()
            assert est._cycle_count == 0

            # Now a new call should NOT be immediately rate-limited
            # (it may still fail due to mock, but it gets past the rate limit check)
            est._cycle_count = 0  # Ensure reset worked

        assert est._cycle_count == 0

    @pytest.mark.asyncio
    async def test_reset_cycle_allows_fresh_calls(self, platform_prices, news_headlines):
        """After reset_cycle(), a full new cycle of 10 calls is allowed."""
        est = LLMEstimator(anthropic_api_key="key-a", gemini_api_key="key-g")

        claude_resp = _make_claude_response(0.70)
        gemini_resp = _make_gemini_response(0.68)

        # Exhaust the first cycle
        with patch.object(est._http, "post", new=AsyncMock(side_effect=[
            *([claude_resp, gemini_resp] * 10),
        ])):
            for i in range(10):
                r = await est.estimate(f"Market {i}", platform_prices, news_headlines)
                assert r is not None

        # Should be at limit
        result = await est.estimate("Over limit", platform_prices, news_headlines)
        assert result is None

        # Reset and call again
        est.reset_cycle()

        with patch.object(est._http, "post", new=AsyncMock(side_effect=[claude_resp, gemini_resp])):
            result = await est.estimate("After reset", platform_prices, news_headlines)
        assert result is not None


# ── Edge calculation ──────────────────────────────────────────────────────────

class TestEdgeCalculation:
    @pytest.mark.asyncio
    async def test_edge_when_consensus_above_yes_price(self, estimator, news_headlines):
        """consensus=0.70, yes_price=0.50 → edge = |0.70 - 0.50| = 0.20 = 20%."""
        prices = [{"platform": "polymarket", "yes_price": 0.50, "no_price": 0.50}]
        claude_resp = _make_claude_response(0.70)
        gemini_resp = _make_gemini_response(0.70)

        with patch.object(estimator._http, "post", new=AsyncMock(side_effect=[claude_resp, gemini_resp])):
            result = await estimator.estimate("Test market", prices, news_headlines)

        assert result is not None
        assert result.edge_pct == pytest.approx(20.0)

    @pytest.mark.asyncio
    async def test_edge_when_consensus_below_yes_price(self, estimator, news_headlines):
        """consensus=0.30, yes_price=0.70 → buy NO side. best_market_price = 1 - 0.30 (no_price=0.30).
        edge = |0.30 - (1 - 0.30)| = |0.30 - 0.70| = 0.40 = 40%."""
        prices = [{"platform": "polymarket", "yes_price": 0.70, "no_price": 0.30}]
        claude_resp = _make_claude_response(0.30)
        gemini_resp = _make_gemini_response(0.30)

        with patch.object(estimator._http, "post", new=AsyncMock(side_effect=[claude_resp, gemini_resp])):
            result = await estimator.estimate("Test market", prices, news_headlines)

        assert result is not None
        assert result.edge_pct == pytest.approx(40.0)

    @pytest.mark.asyncio
    async def test_small_edge_no_boost_even_if_models_agree(self, estimator, news_headlines):
        """consensus=0.51, yes_price=0.50 → edge=1% < 5% → should_boost=False even if high confidence."""
        prices = [{"platform": "polymarket", "yes_price": 0.50, "no_price": 0.50}]
        claude_resp = _make_claude_response(0.51)
        gemini_resp = _make_gemini_response(0.51)

        with patch.object(estimator._http, "post", new=AsyncMock(side_effect=[claude_resp, gemini_resp])):
            result = await estimator.estimate("Test market", prices, news_headlines)

        assert result is not None
        assert result.confidence == "high"
        assert result.edge_pct == pytest.approx(1.0)
        assert result.should_boost is False


# ── Prompt building ───────────────────────────────────────────────────────────

class TestPromptBuilding:
    def test_prompt_contains_title(self, estimator, platform_prices, news_headlines):
        prompt = estimator._build_prompt("Will BTC hit 100k?", platform_prices, news_headlines)
        assert "Will BTC hit 100k?" in prompt

    def test_prompt_contains_platform_prices(self, estimator, platform_prices, news_headlines):
        prompt = estimator._build_prompt("Test?", platform_prices, news_headlines)
        assert "polymarket" in prompt
        assert "0.50" in prompt

    def test_prompt_contains_news_headlines(self, estimator, platform_prices, news_headlines):
        prompt = estimator._build_prompt("Test?", platform_prices, news_headlines)
        assert "Fed signals rate cut" in prompt

    def test_prompt_contains_json_instruction(self, estimator, platform_prices, news_headlines):
        prompt = estimator._build_prompt("Test?", platform_prices, news_headlines)
        assert "probability" in prompt.lower()
        assert "JSON" in prompt

    def test_prompt_empty_headlines(self, estimator, platform_prices):
        prompt = estimator._build_prompt("Test?", platform_prices, [])
        assert "Test?" in prompt


# ── JSON parsing edge cases ───────────────────────────────────────────────────

class TestJsonParsing:
    def test_parse_clean_json(self, estimator):
        raw = '{"probability": 0.65, "confidence": "high", "reasoning": "strong signal"}'
        result = estimator._parse_model_response(raw)
        assert result == {"probability": 0.65, "confidence": "high", "reasoning": "strong signal"}

    def test_parse_json_with_markdown_fences(self, estimator):
        raw = '```json\n{"probability": 0.65, "confidence": "medium", "reasoning": "ok"}\n```'
        result = estimator._parse_model_response(raw)
        assert result is not None
        assert result["probability"] == 0.65

    def test_parse_invalid_json_returns_none(self, estimator):
        raw = "This is not JSON at all"
        result = estimator._parse_model_response(raw)
        assert result is None

    def test_parse_json_missing_probability_returns_none(self, estimator):
        raw = '{"confidence": "high", "reasoning": "ok"}'
        result = estimator._parse_model_response(raw)
        assert result is None

    def test_parse_probability_out_of_range_clamped(self, estimator):
        """Probabilities outside [0,1] should be clamped."""
        raw = '{"probability": 1.5, "confidence": "high", "reasoning": "invalid"}'
        result = estimator._parse_model_response(raw)
        # Either clamped to 1.0 or returns None — implementation choice
        if result is not None:
            assert 0.0 <= result["probability"] <= 1.0


# ── Consensus calculation ─────────────────────────────────────────────────────

class TestConsensusCalculation:
    def test_consensus_is_mean(self, estimator):
        consensus = estimator._compute_consensus(0.60, 0.80)
        assert consensus == pytest.approx(0.70)

    def test_agreement_high(self, estimator):
        level = estimator._agreement_level(0.62, 0.70)  # diff=0.08 < 0.10
        assert level == "high"

    def test_agreement_medium(self, estimator):
        level = estimator._agreement_level(0.60, 0.72)  # diff=0.12 < 0.15
        assert level == "medium"

    def test_agreement_low(self, estimator):
        level = estimator._agreement_level(0.40, 0.80)  # diff=0.40 >= 0.15
        assert level == "low"

    def test_agreement_exactly_10pct_is_high(self, estimator):
        """Diff 0.10 is within 10% (spec: 'agree within 10%') → high."""
        level = estimator._agreement_level(0.60, 0.70)  # diff=0.10, within 10% → high
        assert level == "high"

    def test_agreement_exactly_15pct_is_medium(self, estimator):
        """Diff 0.15 is within 15% (spec: 'within 15%') → medium."""
        level = estimator._agreement_level(0.60, 0.75)  # diff=0.15, within 15% → medium
        assert level == "medium"

    def test_agreement_above_15pct_is_low(self, estimator):
        """Diff > 0.15 → low."""
        level = estimator._agreement_level(0.60, 0.76)  # diff=0.16 > 0.15 → low
        assert level == "low"


# ── Reasoning combination ─────────────────────────────────────────────────────

class TestReasoningCombination:
    @pytest.mark.asyncio
    async def test_reasoning_includes_both_models(self, estimator, platform_prices, news_headlines):
        """Combined reasoning should mention both models."""
        claude_resp = _make_claude_response(0.70, reasoning="Strong bullish signal")
        gemini_resp = _make_gemini_response(0.68, reasoning="Upward momentum detected")

        with patch.object(estimator._http, "post", new=AsyncMock(side_effect=[claude_resp, gemini_resp])):
            result = await estimator.estimate("Will BTC hit 100k?", platform_prices, news_headlines)

        assert result is not None
        assert "Strong bullish signal" in result.reasoning or "Upward momentum" in result.reasoning
