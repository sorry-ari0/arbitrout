"""News AI — multi-provider LLM analysis of news headlines for trading signals.

Provider chain: Groq → Gemini → OpenRouter (paper), Anthropic → Groq → Gemini → OpenRouter (live).
Uses NEWS_*_API_KEY env vars with fallback to base API key vars.
Rate limited independently from exit advisor (max_calls_per_min).
"""
import logging
import os
import re
import time

import httpx

logger = logging.getLogger("positions.news_ai")

# Provider configs — each has a NEWS_ env var that falls back to the base env var.
# Live: Anthropic first (best quality for real money decisions)
# Paper: skip Anthropic (save costs), use free/cheap providers
NEWS_LIVE_PROVIDERS = [
    {
        "name": "anthropic",
        "env_var": "NEWS_ANTHROPIC_API_KEY",
        "fallback_env_var": "ANTHROPIC_API_KEY",
        "base_url": "https://api.anthropic.com/v1/messages",
        "model": "claude-sonnet-4-20250514",
        "style": "anthropic",
    },
    {
        "name": "groq",
        "env_var": "NEWS_GROQ_API_KEY",
        "fallback_env_var": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "model": "llama-3.3-70b-versatile",
        "style": "openai",
    },
    {
        "name": "gemini",
        "env_var": "NEWS_GEMINI_API_KEY",
        "fallback_env_var": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        "model": "gemini-2.0-flash",
        "style": "gemini",
    },
    {
        "name": "openrouter",
        "env_var": "NEWS_OPENROUTER_API_KEY",
        "fallback_env_var": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "meta-llama/llama-3.1-70b-instruct",
        "style": "openai",
    },
]

NEWS_PAPER_PROVIDERS = [
    {
        "name": "groq",
        "env_var": "NEWS_GROQ_API_KEY",
        "fallback_env_var": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "model": "llama-3.3-70b-versatile",
        "style": "openai",
    },
    {
        "name": "gemini",
        "env_var": "NEWS_GEMINI_API_KEY",
        "fallback_env_var": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        "model": "gemini-2.0-flash",
        "style": "gemini",
    },
    {
        "name": "openrouter",
        "env_var": "NEWS_OPENROUTER_API_KEY",
        "fallback_env_var": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "meta-llama/llama-3.1-70b-instruct",
        "style": "openai",
    },
]


def _resolve_api_key(provider: dict) -> str:
    """Get API key: check NEWS_ env var first, then fallback to base env var."""
    key = os.environ.get(provider["env_var"], "")
    if key:
        return key
    return os.environ.get(provider.get("fallback_env_var", ""), "")


class NewsAI:
    """LLM-powered news analysis for headline scanning and deep article review."""

    def __init__(self, paper_mode: bool = True, max_calls_per_min: int = 10):
        self._paper_mode = paper_mode
        self._max_calls = max_calls_per_min
        self._call_times: list[float] = []
        self._http: httpx.AsyncClient | None = None
        self._last_provider: str | None = None

    @property
    def is_available(self) -> bool:
        """Check if any AI provider has a key set."""
        providers = NEWS_PAPER_PROVIDERS if self._paper_mode else NEWS_LIVE_PROVIDERS
        return any(_resolve_api_key(p) for p in providers)

    def _get_available_providers(self) -> list[dict]:
        """Return providers that have API keys configured, in priority order.
        Live: Anthropic → Groq → Gemini → OpenRouter
        Paper: Groq → Gemini → OpenRouter (skip Anthropic to save costs)
        """
        providers = NEWS_PAPER_PROVIDERS if self._paper_mode else NEWS_LIVE_PROVIDERS
        return [p for p in providers if _resolve_api_key(p)]

    async def _get_http(self) -> httpx.AsyncClient:
        if not self._http or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    def _rate_check(self) -> bool:
        """Check if we're within rate limits."""
        now = time.time()
        self._call_times = [t for t in self._call_times if now - t < 60]
        return len(self._call_times) < self._max_calls

    # ------------------------------------------------------------------
    # Provider call methods — identical pattern to AIAdvisor
    # ------------------------------------------------------------------

    async def _call_openai_style(self, provider: dict, prompt: str, max_tokens: int = 500) -> str:
        """Call OpenAI-compatible API (Groq, OpenRouter)."""
        api_key = _resolve_api_key(provider)
        http = await self._get_http()

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if provider["name"] == "openrouter":
            headers["HTTP-Referer"] = "https://arbitrout.local"

        body = {
            "model": provider["model"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }

        r = await http.post(provider["base_url"], json=body, headers=headers)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]

    async def _call_gemini(self, provider: dict, prompt: str, max_tokens: int = 500) -> str:
        """Call Gemini REST API."""
        api_key = _resolve_api_key(provider)
        http = await self._get_http()

        url = provider["base_url"].format(model=provider["model"])
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.3},
        }

        r = await http.post(url, headers={"x-goog-api-key": api_key}, json=body)
        r.raise_for_status()
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    async def _call_anthropic(self, provider: dict, prompt: str, max_tokens: int = 500) -> str:
        """Call Anthropic Messages API."""
        api_key = _resolve_api_key(provider)
        http = await self._get_http()

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        body = {
            "model": provider["model"],
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }

        r = await http.post(provider["base_url"], json=body, headers=headers)
        r.raise_for_status()
        data = r.json()
        return data["content"][0]["text"]

    async def _call_provider(self, provider: dict, prompt: str, max_tokens: int = 500) -> str:
        """Route to the correct API style."""
        if provider["style"] == "openai":
            return await self._call_openai_style(provider, prompt, max_tokens)
        elif provider["style"] == "gemini":
            return await self._call_gemini(provider, prompt, max_tokens)
        elif provider["style"] == "anthropic":
            return await self._call_anthropic(provider, prompt, max_tokens)
        raise ValueError(f"Unknown API style: {provider['style']}")

    # ------------------------------------------------------------------
    # scan_headlines — classify headlines against markets
    # ------------------------------------------------------------------

    @staticmethod
    def _prioritize_headlines(headlines: list[dict], limit: int = 40) -> list[dict]:
        """Select headlines for AI scan, balanced across categories.

        Takes top N from each category to ensure diversity, then fills remaining
        slots by priority: crypto > politics > finance > macro.
        """
        by_cat: dict[str, list] = {}
        for h in headlines:
            cat = h.get("category", "macro")
            by_cat.setdefault(cat, []).append(h)

        # Guarantee slots per category (at least 8 each if available)
        min_per_cat = 8
        selected = []
        for cat in ("crypto", "politics", "finance", "macro"):
            selected.extend(by_cat.get(cat, [])[:min_per_cat])

        # Fill remaining slots by priority
        seen = {id(h) for h in selected}
        priority = {"crypto": 0, "politics": 1, "finance": 2, "macro": 3}
        remaining = [h for h in headlines if id(h) not in seen]
        remaining.sort(key=lambda h: priority.get(h.get("category", ""), 99))
        selected.extend(remaining[:limit - len(selected)])

        return selected[:limit]

    def _build_scan_prompt(self, headlines: list[dict], markets: list[dict]) -> str:
        """Build prompt for headline classification."""
        selected = self._prioritize_headlines(headlines)
        headline_lines = []
        for h in selected:
            source = h.get("source", "")
            summary = h.get("summary", "")
            src_part = f" ({source})" if source else ""
            sum_part = f" — {summary}" if summary else ""
            headline_lines.append(f"[{h['index']}] {h['title']}{src_part}{sum_part}")

        market_lines = []
        for m in markets[:200]:
            price_str = f" (YES=${m.get('yes_price', '?')})" if "yes_price" in m else ""
            market_lines.append(f"- {m['title']}{price_str}")

        return f"""You are a prediction market analyst looking for trading edge. Your job is to find headlines that create actionable trading signals for prediction markets — news that the market hasn't fully priced in yet.

HEADLINES:
{chr(10).join(headline_lines)}

ACTIVE MARKETS:
{chr(10).join(market_lines)}

For each headline, respond with exactly one line:
<index>: SKIP
or
<index>: RELEVANT <market_title> | <side YES or NO> | <confidence 1-100> | <urgency LOW MEDIUM HIGH> | <keywords>

Rules:
- RELEVANT if the headline materially affects ANY listed market's outcome — even indirectly
  Examples: Fed rate decisions affect crypto price markets, political news affects election/policy markets,
  geopolitical events affect commodity/currency markets, regulatory news affects crypto markets
- SKIP only if the headline is truly unrelated to any market (lifestyle, sports not on the list, local news)
- market_title: COPY the exact market title from the ACTIVE MARKETS list above. Do NOT rephrase or abbreviate.
- side: YES if the news makes the market condition more likely, NO if less likely
- confidence: how strongly this news shifts the probability (1-100). 60+ means clear directional impact.
- urgency: LOW (background context), MEDIUM (notable development), HIGH (breaking — markets likely haven't reacted yet)
- keywords: 2-4 key search terms to help match this signal to the market (e.g. "bitcoin,BTC,crypto,100000")
- Think like a trader: what news creates an edge before the market adjusts?
- When in doubt about relevance, mark RELEVANT with lower confidence — false negatives cost more than false positives"""

    def _parse_scan_response(self, text: str) -> list[dict]:
        """Parse scan response into list of relevant signals."""
        signals = []
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue

            # Match: <index>: RELEVANT <market_title> | <side> | <confidence> | <urgency> | <keywords>
            # Keywords field is optional for backwards compatibility
            # Handles both "0: RELEVANT ..." and "[0]: RELEVANT ..." formats
            match = re.match(
                r"\[?(\d+)\]?\s*:\s*RELEVANT\s+(.+?)\s*\|\s*(YES|NO)\s*\|\s*(\d+)\s*\|\s*(LOW|MEDIUM|HIGH)(?:\s*\|\s*(.+))?",
                line,
            )
            if match:
                keywords_raw = match.group(6)
                keywords = []
                if keywords_raw:
                    keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]
                signals.append({
                    "headline_index": int(match.group(1)),
                    "market_title": match.group(2).strip(),
                    "side": match.group(3),
                    "confidence": int(match.group(4)),
                    "urgency": match.group(5),
                    "search_keywords": keywords,
                })

        return signals

    async def scan_headlines(self, headlines: list[dict], markets: list[dict]) -> list[dict]:
        """Classify headlines against active markets via LLM chain.

        Args:
            headlines: [{title, source, summary, url, index}]
            markets: [{title, yes_price, condition_id}]

        Returns:
            List of relevant signals: [{headline_index, market_title, side, confidence, urgency}]
            Returns empty list if all providers fail.
        """
        providers = self._get_available_providers()
        if not providers:
            logger.info("No AI providers configured for news scan")
            return []

        if not self._rate_check():
            logger.warning("News AI rate limit exceeded — skipping scan")
            return []

        if not headlines or not markets:
            return []

        prompt = self._build_scan_prompt(headlines, markets)

        # Scan may return many lines — allow more tokens (40 headlines * ~30 tokens each with keywords)
        max_tokens = 1500

        for provider in providers:
            try:
                logger.info("Trying news scan via %s (%d headlines, %d markets)",
                            provider["name"], len(headlines[:30]), len(markets[:200]))
                text = await self._call_provider(provider, prompt, max_tokens)
                self._call_times.append(time.time())
                self._last_provider = provider["name"]

                signals = self._parse_scan_response(text)
                logger.info("News scan via %s: %d relevant signals from %d headlines",
                            provider["name"], len(signals), len(headlines[:30]))
                return signals

            except Exception as e:
                logger.warning("News scan via %s failed: %s — trying next provider", provider["name"], e)
                continue

        logger.warning("All AI providers failed for news scan")
        return []

    # ------------------------------------------------------------------
    # deep_analysis — full article analysis for trade decision
    # ------------------------------------------------------------------

    def _build_analysis_prompt(self, article_text: str, headline: str,
                                market: dict, portfolio: dict) -> str:
        """Build prompt for deep article analysis."""
        truncated = article_text[:2000]
        if len(article_text) > 2000:
            truncated += "\n[...truncated]"

        market_title = market.get("title", "Unknown")
        yes_price = market.get("yes_price", "?")
        condition_id = market.get("condition_id", "?")

        # Portfolio context
        portfolio_lines = []
        if portfolio:
            total_value = portfolio.get("total_value", 0)
            open_positions = portfolio.get("open_positions", 0)
            available_balance = portfolio.get("available_balance", 0)
            portfolio_lines.append(f"Total value: ${total_value:.2f}")
            portfolio_lines.append(f"Open positions: {open_positions}")
            portfolio_lines.append(f"Available balance: ${available_balance:.2f}")

            existing = portfolio.get("existing_position", None)
            if existing:
                portfolio_lines.append(
                    f"Existing position in this market: {existing.get('side', '?')} "
                    f"qty={existing.get('quantity', 0)} @ ${existing.get('avg_price', 0):.4f}"
                )
        portfolio_section = "\n".join(portfolio_lines) if portfolio_lines else "No portfolio data"

        return f"""You are a prediction market trading analyst. Analyze this news article and decide whether it justifies a trade on the specified market.

HEADLINE: {headline}

ARTICLE TEXT:
{truncated}

TARGET MARKET: {market_title}
Current YES price: ${yes_price}
Condition ID: {condition_id}

PORTFOLIO STATE:
{portfolio_section}

Respond with exactly one line:
TRADE <side YES or NO> | <confidence 1-100> | <reasoning in one sentence>
or
NO_TRADE | <reasoning in one sentence>

Rules:
- TRADE if the article provides actionable evidence that shifts probability
- Consider the current YES price — is there enough edge? Even 5-10% edge is valuable with 0% maker fees
- Consider portfolio exposure — avoid over-concentration
- confidence: how confident you are the trade will profit (1-100). 50+ means more likely to profit than not.
- News-driven trades have a time edge — if the article moves probability, lean toward TRADE rather than waiting"""

    def _parse_analysis_response(self, text: str) -> dict:
        """Parse deep analysis response into action dict."""
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue

            # Match: TRADE <side> | <confidence> | <reasoning>
            # Handles optional markdown formatting: **TRADE**, `TRADE`, etc.
            trade_match = re.match(
                r"[*`]*TRADE[*`]*\s+(YES|NO)\s*\|\s*(\d+)\s*\|\s*(.+)",
                line,
            )
            if trade_match:
                return {
                    "action": "TRADE",
                    "side": trade_match.group(1),
                    "confidence": int(trade_match.group(2)),
                    "reasoning": trade_match.group(3).strip(),
                }

            # Match: NO_TRADE | <reasoning>
            no_trade_match = re.match(r"[*`]*NO_TRADE[*`]*\s*\|\s*(.+)", line)
            if no_trade_match:
                return {
                    "action": "NO_TRADE",
                    "side": None,
                    "confidence": 0,
                    "reasoning": no_trade_match.group(1).strip(),
                }

        # Fallback: search entire text for TRADE/NO_TRADE patterns
        trade_search = re.search(
            r"TRADE\s+(YES|NO)\s*\|\s*(\d+)\s*\|\s*(.+?)(?:\n|$)",
            text,
        )
        if trade_search:
            return {
                "action": "TRADE",
                "side": trade_search.group(1),
                "confidence": int(trade_search.group(2)),
                "reasoning": trade_search.group(3).strip(),
            }

        no_trade_search = re.search(r"NO_TRADE\s*\|\s*(.+?)(?:\n|$)", text)
        if no_trade_search:
            return {
                "action": "NO_TRADE",
                "side": None,
                "confidence": 0,
                "reasoning": no_trade_search.group(1).strip(),
            }

        # Unparseable — default to no trade
        return {
            "action": "NO_TRADE",
            "side": None,
            "confidence": 0,
            "reasoning": f"Unparseable AI response: {text[:100]}",
        }

    async def deep_analysis(self, article_text: str, headline: str,
                            market: dict, portfolio: dict) -> dict:
        """Analyze full article for trade decision via LLM chain.

        Args:
            article_text: Full article text (truncated to 2000 chars internally)
            headline: Original headline string
            market: {title, yes_price, condition_id}
            portfolio: {total_value, open_positions, available_balance, existing_position}

        Returns:
            {action: "TRADE"|"NO_TRADE", side: "YES"|"NO"|None, confidence: int, reasoning: str}
        """
        providers = self._get_available_providers()
        if not providers:
            logger.info("No AI providers configured for deep analysis")
            return {
                "action": "NO_TRADE",
                "side": None,
                "confidence": 0,
                "reasoning": "No AI providers available",
            }

        if not self._rate_check():
            logger.warning("News AI rate limit exceeded — skipping deep analysis")
            return {
                "action": "NO_TRADE",
                "side": None,
                "confidence": 0,
                "reasoning": "Rate limit exceeded",
            }

        prompt = self._build_analysis_prompt(article_text, headline, market, portfolio)

        for provider in providers:
            try:
                logger.info("Trying deep analysis via %s for market: %s",
                            provider["name"], market.get("title", "?"))
                text = await self._call_provider(provider, prompt)
                self._call_times.append(time.time())
                self._last_provider = provider["name"]

                result = self._parse_analysis_response(text)
                logger.info("Deep analysis via %s: %s (confidence=%d)",
                            provider["name"], result["action"], result["confidence"])
                return result

            except Exception as e:
                logger.warning("Deep analysis via %s failed: %s — trying next provider",
                               provider["name"], e)
                continue

        logger.warning("All AI providers failed for deep analysis")
        return {
            "action": "NO_TRADE",
            "side": None,
            "confidence": 0,
            "reasoning": "All AI providers failed",
        }

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
