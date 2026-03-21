"""LLM Estimator — stateless probability estimator using Claude and Gemini in parallel.

Queries both models simultaneously and returns a consensus probability estimate
used by the auto-trader for score boosting (Strategy 2: LLM Mispricing Detection).

Only called when probability_model detects >10% cross-platform price deviation.
Optional — not initialized if API keys are absent.
"""
import asyncio
import json
import logging
import re
from dataclasses import dataclass

try:
    import httpx
except ImportError:
    httpx = None

logger = logging.getLogger("positions.llm_estimator")

# Rate limit: max API calls per cycle (reset by caller between scan cycles)
MAX_CALLS_PER_CYCLE = 10

# Model identifiers
CLAUDE_MODEL = "claude-sonnet-4-20250514"
GEMINI_MODEL = "gemini-2.0-flash"

# API endpoints
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)

# Agreement thresholds
HIGH_AGREEMENT_THRESHOLD = 0.10   # |claude - gemini| < 0.10 → "high"
MEDIUM_AGREEMENT_THRESHOLD = 0.15  # |claude - gemini| < 0.15 → "medium", else "low"

# Boost thresholds
MIN_EDGE_FOR_BOOST = 5.0  # edge_pct must exceed 5% to boost


@dataclass
class EstimateResult:
    """Structured result from parallel LLM probability estimation."""
    consensus_prob: float       # Mean of model estimates
    edge_pct: float             # |consensus - best_market_price| as percentage
    confidence: str             # "high" / "medium" / "low"
    models: dict                # {"claude": 0.62, "gemini": 0.58}
    should_boost: bool          # True if confidence != "low" AND edge > 5%
    reasoning: str              # Combined reasoning from models


class LLMEstimator:
    """Queries Claude and Gemini in parallel to estimate market resolution probability.

    Stateless between cycles — caller must call reset_cycle() at the start of
    each scan cycle to reset the per-cycle rate limit counter.
    """

    def __init__(self, anthropic_api_key: str, gemini_api_key: str):
        self._anthropic_key = anthropic_api_key
        self._gemini_key = gemini_api_key
        self._cycle_count = 0

        if httpx is None:
            raise ImportError("httpx is required for LLMEstimator. Install with: pip install httpx")

        self._http = httpx.AsyncClient(timeout=20.0)

    def reset_cycle(self):
        """Reset the per-cycle request counter. Call at the start of each scan cycle."""
        self._cycle_count = 0
        logger.debug("LLM estimator: cycle counter reset")

    # ── Public API ────────────────────────────────────────────────────────────

    async def estimate(
        self,
        title: str,
        platform_prices: list[dict],
        news_headlines: list[str],
    ) -> "EstimateResult | None":
        """Estimate the true probability a market resolves YES.

        Queries Claude and Gemini in parallel. Returns None if rate limit is
        exceeded or both models fail.

        Args:
            title: Market question (e.g. "Will BTC exceed $100k by end of 2026?")
            platform_prices: List of dicts with keys: platform, yes_price, no_price
            news_headlines: Recent relevant news headlines (may be empty)

        Returns:
            EstimateResult or None
        """
        if self._cycle_count >= MAX_CALLS_PER_CYCLE:
            logger.debug("LLM estimator: rate limit reached (%d/%d)", self._cycle_count, MAX_CALLS_PER_CYCLE)
            return None

        self._cycle_count += 1
        prompt = self._build_prompt(title, platform_prices, news_headlines)

        # Query both models in parallel
        claude_task = asyncio.create_task(self._call_claude(prompt))
        gemini_task = asyncio.create_task(self._call_gemini(prompt))

        results = await asyncio.gather(claude_task, gemini_task, return_exceptions=True)
        claude_raw, gemini_raw = results

        # Parse results — exceptions count as failures
        claude_data = None
        gemini_data = None

        if isinstance(claude_raw, Exception):
            logger.warning("LLM estimator: Claude failed: %s", claude_raw)
        else:
            claude_data = self._parse_model_response(claude_raw)
            if claude_data is None:
                logger.warning("LLM estimator: Claude returned unparseable response")

        if isinstance(gemini_raw, Exception):
            logger.warning("LLM estimator: Gemini failed: %s", gemini_raw)
        else:
            gemini_data = self._parse_model_response(gemini_raw)
            if gemini_data is None:
                logger.warning("LLM estimator: Gemini returned unparseable response")

        # Both failed → return None
        if claude_data is None and gemini_data is None:
            logger.warning("LLM estimator: both models failed for '%s'", title[:60])
            return None

        # Build result from available data
        return self._build_result(
            title=title,
            platform_prices=platform_prices,
            claude_data=claude_data,
            gemini_data=gemini_data,
        )

    async def close(self):
        """Close the shared HTTP client."""
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    # ── Prompt Building ───────────────────────────────────────────────────────

    def _build_prompt(
        self,
        title: str,
        platform_prices: list[dict],
        news_headlines: list[str],
    ) -> str:
        """Build the probability estimation prompt."""
        # Format platform prices
        if platform_prices:
            price_lines = []
            for p in platform_prices:
                platform = p.get("platform", "unknown")
                yes = p.get("yes_price", 0.0)
                no = p.get("no_price", 0.0)
                price_lines.append(f"  - {platform}: YES={yes:.2f}, NO={no:.2f}")
            platform_prices_formatted = "\n".join(price_lines)
        else:
            platform_prices_formatted = "  (no prices available)"

        # Format news headlines
        if news_headlines:
            news_formatted = "\n".join(f"  - {h}" for h in news_headlines[:10])
        else:
            news_formatted = "  (no recent news)"

        return (
            "You are a calibrated probability estimator for prediction markets.\n\n"
            f"Market: {title}\n\n"
            "Current prices across platforms:\n"
            f"{platform_prices_formatted}\n\n"
            "Recent news (if any):\n"
            f"{news_formatted}\n\n"
            "Estimate the true probability this market resolves YES.\n"
            'Respond with JSON only: {"probability": 0.XX, "confidence": "high|medium|low", "reasoning": "one sentence"}'
        )

    # ── API Calls ─────────────────────────────────────────────────────────────

    async def _call_claude(self, prompt: str) -> str:
        """Call the Anthropic Claude Messages API. Returns raw text content."""
        headers = {
            "x-api-key": self._anthropic_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        body = {
            "model": CLAUDE_MODEL,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": prompt}],
        }

        r = await self._http.post(ANTHROPIC_URL, json=body, headers=headers)
        r.raise_for_status()
        data = r.json()
        return data["content"][0]["text"]

    async def _call_gemini(self, prompt: str) -> str:
        """Call the Gemini generateContent API. Returns raw text content."""
        url = GEMINI_URL_TEMPLATE.format(model=GEMINI_MODEL, key=self._gemini_key)
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": 256,
                "temperature": 0.3,
            },
        }

        r = await self._http.post(url, json=body)
        r.raise_for_status()
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    # ── Response Parsing ──────────────────────────────────────────────────────

    def _parse_model_response(self, raw: str) -> "dict | None":
        """Parse a model's JSON response. Returns dict with 'probability' key or None.

        Handles:
        - Clean JSON strings
        - JSON wrapped in markdown fences (```json ... ```)
        - Strips leading/trailing whitespace
        """
        if not raw or not isinstance(raw, str):
            return None

        text = raw.strip()

        # Strip markdown code fences if present
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if fence_match:
            text = fence_match.group(1).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try extracting JSON object from surrounding text
            json_match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
            if not json_match:
                logger.debug("LLM estimator: could not find JSON in response: %s", raw[:100])
                return None
            try:
                data = json.loads(json_match.group(0))
            except json.JSONDecodeError:
                logger.debug("LLM estimator: failed to parse extracted JSON: %s", json_match.group(0)[:100])
                return None

        if not isinstance(data, dict):
            return None

        # Validate probability field is present
        if "probability" not in data:
            logger.debug("LLM estimator: response missing 'probability' field: %s", data)
            return None

        # Clamp probability to [0, 1]
        try:
            prob = float(data["probability"])
        except (TypeError, ValueError):
            logger.debug("LLM estimator: invalid probability value: %s", data.get("probability"))
            return None

        data["probability"] = max(0.0, min(1.0, prob))
        return data

    # ── Result Assembly ───────────────────────────────────────────────────────

    def _build_result(
        self,
        title: str,
        platform_prices: list[dict],
        claude_data: "dict | None",
        gemini_data: "dict | None",
    ) -> EstimateResult:
        """Assemble EstimateResult from parsed model data."""
        models: dict[str, float] = {}
        reasonings: list[str] = []

        if claude_data is not None:
            models["claude"] = claude_data["probability"]
            if r := claude_data.get("reasoning", ""):
                reasonings.append(f"Claude: {r}")

        if gemini_data is not None:
            models["gemini"] = gemini_data["probability"]
            if r := gemini_data.get("reasoning", ""):
                reasonings.append(f"Gemini: {r}")

        # Determine confidence and consensus
        if len(models) == 2:
            # Both succeeded — compute consensus and agreement
            consensus_prob = self._compute_consensus(models["claude"], models["gemini"])
            confidence = self._agreement_level(models["claude"], models["gemini"])
        else:
            # Single model — confidence is always "low" per spec
            consensus_prob = next(iter(models.values()))
            confidence = "low"

        # Compute edge against best available market price
        edge_pct = self._compute_edge(consensus_prob, platform_prices)

        # Boost only if confidence is not "low" AND edge exceeds threshold
        should_boost = (confidence != "low") and (edge_pct > MIN_EDGE_FOR_BOOST)

        reasoning = "; ".join(reasonings) if reasonings else ""

        logger.info(
            "LLM estimator: '%s' → consensus=%.2f edge=%.1f%% confidence=%s boost=%s",
            title[:60], consensus_prob, edge_pct, confidence, should_boost,
        )

        return EstimateResult(
            consensus_prob=round(consensus_prob, 4),
            edge_pct=round(edge_pct, 2),
            confidence=confidence,
            models=models,
            should_boost=should_boost,
            reasoning=reasoning,
        )

    # ── Utility Methods ───────────────────────────────────────────────────────

    @staticmethod
    def _compute_consensus(claude_prob: float, gemini_prob: float) -> float:
        """Compute mean of two model estimates."""
        return (claude_prob + gemini_prob) / 2.0

    @staticmethod
    def _agreement_level(claude_prob: float, gemini_prob: float) -> str:
        """Classify model agreement based on absolute difference.

        Spec: "high" if models agree within 10%, "medium" within 15%, "low" otherwise.
        "Within 10%" means |diff| <= 0.10, "within 15%" means |diff| <= 0.15.

        |diff| <= 0.10 → "high"
        |diff| <= 0.15 → "medium"
        else           → "low"

        Note: round to 10 decimal places to avoid floating-point representation
        issues (e.g. 0.75 - 0.60 = 0.15000000000000002 in IEEE 754).
        """
        diff = round(abs(claude_prob - gemini_prob), 10)
        if diff <= HIGH_AGREEMENT_THRESHOLD:
            return "high"
        if diff <= MEDIUM_AGREEMENT_THRESHOLD:
            return "medium"
        return "low"

    @staticmethod
    def _compute_edge(consensus_prob: float, platform_prices: list[dict]) -> float:
        """Compute edge percentage against best available market price.

        Trade direction depends on whether consensus is above or below market:
        - consensus > yes_price → we'd buy YES → compare against yes_price
        - consensus <= yes_price → we'd buy NO → compare against implied YES from NO side
                                               i.e. best_market_price = 1 - no_price

        Uses the first available platform price. Returns 0.0 if no prices given.
        """
        if not platform_prices:
            return 0.0

        # Use first platform's prices (best available market price)
        p = platform_prices[0]
        yes_price = p.get("yes_price", 0.5)
        no_price = p.get("no_price", 1.0 - yes_price)

        if consensus_prob > yes_price:
            best_market_price = yes_price
        else:
            best_market_price = 1.0 - no_price

        edge = abs(consensus_prob - best_market_price)
        return edge * 100.0
