"""AI advisor — multi-provider LLM review of exit proposals.

Provider chain: Groq → Gemini → OpenRouter → Anthropic → auto-execute fallback.
Uses whichever provider has an API key set. All calls use httpx for consistency.
Rate limited to max_calls_per_min across all providers.
"""
import asyncio
import logging
import os
import random
import re
import time

import httpx

logger = logging.getLogger("positions.ai_advisor")

# Provider configs — order depends on trading mode
# Live: Anthropic first (best quality for real money decisions)
# Paper: skip Anthropic (save costs), use free/cheap providers
LIVE_PROVIDERS = [
    {
        "name": "anthropic",
        "env_var": "ANTHROPIC_API_KEY",
        "base_url": "https://api.anthropic.com/v1/messages",
        "model": "claude-sonnet-4-20250514",
        "style": "anthropic",
    },
    {
        "name": "groq",
        "env_var": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "model": "llama-3.3-70b-versatile",
        "style": "openai",
    },
    {
        "name": "gemini",
        "env_var": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        "model": "gemini-2.0-flash",
        "style": "gemini",
    },
    {
        "name": "openrouter",
        "env_var": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "meta-llama/llama-3.1-70b-instruct",
        "style": "openai",
    },
]

PAPER_PROVIDERS = [
    {
        "name": "groq",
        "env_var": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "model": "llama-3.3-70b-versatile",
        "style": "openai",
    },
    {
        "name": "gemini",
        "env_var": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        "model": "gemini-2.0-flash",
        "style": "gemini",
    },
    {
        "name": "openrouter",
        "env_var": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "meta-llama/llama-3.1-70b-instruct",
        "style": "openai",
    },
]


class AIAdvisor:
    """Reviews exit proposals using multi-provider LLM chain with fallback."""

    def __init__(self, paper_mode: bool = True, max_calls_per_min: int = 10):
        self._paper_mode = paper_mode
        self._max_calls = max_calls_per_min
        self._call_times: list[float] = []
        self._http: httpx.AsyncClient | None = None
        self._last_provider: str | None = None
        self._disabled_providers: dict[str, float] = {}  # name → disabled_until timestamp

    @property
    def is_available(self) -> bool:
        """Check if any AI provider has a key set."""
        providers = PAPER_PROVIDERS if self._paper_mode else LIVE_PROVIDERS
        return any(os.environ.get(p["env_var"], "") for p in providers)

    def _get_available_providers(self) -> list[dict]:
        """Return providers that have API keys configured, in priority order.
        Live: Anthropic → Groq → Gemini → OpenRouter
        Paper: Groq → Gemini → OpenRouter (skip Anthropic to save costs)
        Skips providers temporarily disabled due to 402/payment errors.
        """
        now = time.time()
        providers = PAPER_PROVIDERS if self._paper_mode else LIVE_PROVIDERS
        available = []
        for p in providers:
            if not os.environ.get(p["env_var"], ""):
                continue
            disabled_until = self._disabled_providers.get(p["name"], 0)
            if now < disabled_until:
                continue  # Still disabled
            available.append(p)
        return available

    async def _get_http(self) -> httpx.AsyncClient:
        if not self._http or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    def _build_context(self, pkg: dict) -> str:
        """Format package/legs/rules as text context for the prompt."""
        lines = [
            f"Package: {pkg.get('name', 'Unknown')} ({pkg.get('strategy_type', '')})",
            f"Status: {pkg.get('status', 'unknown')}",
            f"P&L: ${pkg.get('unrealized_pnl', 0):.2f} ({pkg.get('unrealized_pnl_pct', 0):.1f}%)",
            "",
            "Legs:",
        ]
        for leg in pkg.get("legs", []):
            lines.append(
                f"  - {leg.get('asset_label', leg.get('asset_id', '?'))} on {leg.get('platform', '?')}: "
                f"entry=${leg.get('entry_price', 0):.4f} → current=${leg.get('current_price', 0):.4f} "
                f"qty={leg.get('quantity', 0):.2f} ({leg.get('leg_status', '?')}) "
                f"expires={leg.get('expiry', '?')}"
            )

        lines.append("")
        lines.append("Exit Rules:")
        for rule in pkg.get("exit_rules", []):
            if rule.get("active"):
                params = rule.get("params", {})
                bounds = f" [bounds: {params.get('bound_min', '?')}-{params.get('bound_max', '?')}]" if "bound_min" in params else ""
                lines.append(f"  - {rule.get('type', '?')}: current={params.get('current', '?')}{bounds}")

        return "\n".join(lines)

    def _build_prompt(self, pkg: dict, proposals: list[dict]) -> str:
        """Build structured prompt for exit review."""
        context = self._build_context(pkg)
        proposal_text = "\n".join(
            f"- Trigger #{p.get('trigger_id', '?')} ({p.get('name', '?')}): {p.get('details', '')} → proposed action: {p.get('action', '?')}"
            for p in proposals
        )
        mode_note = "This is PAPER TRADING mode." if self._paper_mode else "This is LIVE TRADING — balance risk carefully."

        return f"""You are a prediction market exit execution advisor.

{mode_note}

PORTFOLIO CONTEXT:
{context}

TRIGGERED EXIT PROPOSALS:
{proposal_text}

For each triggered rule, respond with ONLY the trigger name and your verdict. One line per trigger. No preamble, no explanation, no numbering.

FORMAT (use ONLY the trigger name before the colon):
trigger_name: REJECT <short reason>
trigger_name: MODIFY <new_value>
trigger_name: APPROVE

EXAMPLE OUTPUT (for triggers named trailing_stop and time_decay):
time_decay: REJECT position has 5 days left and P&L is only -1%
trailing_stop: REJECT drawdown is only 8%, normal volatility

VERDICT RULES:
- REJECT = the trigger fired but the position should stay open. THIS IS YOUR DEFAULT. Most triggers should be rejected.
- MODIFY = adjust parameter (within bounds). Use sparingly.
- APPROVE = execute the exit. USE THIS ONLY when the trigger condition is genuinely met AND the position should close.
- MODIFY = adjust parameter (within bounds). Use sparingly.
- REJECT = the trigger fired but the position should stay open.

CRITICAL CONTEXT — PAPER TRADING PERFORMANCE DATA:
Out of 31 closed trades, every single automated exit lost money:
- trailing_stop: 5 exits, 0 wins, -$59 total
- AI-approved time_decay: 7 exits, 0 wins, -$29 total
- AI-approved negative_drift: 4 exits, 0 wins, -$55 total
- manual exits (human held positions): 13 exits, 3 wins, -$2 total (near break-even)
The #1 cause of losses is PREMATURE EXITS. Your default should be REJECT.

GUIDELINES:
- DEFAULT ACTION IS REJECT. Prediction markets resolve to $0 or $1 — patience wins, panic selling loses. Only approve exits when there is overwhelming evidence the position is wrong.
- target_hit: APPROVE. These are mechanical — the threshold was set for a reason.
- trailing_stop: ALWAYS REJECT. 0/8 wins in journal. Prediction markets resolve at $0/$1 — drawdowns are noise, not signal.
- time_decay: ALWAYS REJECT. Exiting early forfeits the thesis. Markets move most in final hours.
- negative_drift: ALWAYS REJECT. Temporary dips are normal volatility, not reasons to exit.
- stop_loss: ALWAYS REJECT. Prediction markets are binary — a drawdown is not a reason to exit. Stop-losses destroyed -$33.22 on a single trade.
- stale_position: REJECT if P&L is between -10% and +10% — the position may still be developing. APPROVE only if truly stagnant (>10 days, <2% absolute move).
- longshot_decay: APPROVE only if the position has lost >30% of its entry value.
- new_ath, vol_spike, spread_compression: REJECT — these are informational, not actionable.
- ANY trigger within 6 hours of expiry: REJECT. Prediction markets exhibit maximum gamma near settlement — the final hours contain 50%+ of total price movement. Exiting during this period forfeits the highest-value window. The ONLY exception is spread_inversion (safety override, handled automatically).
Do NOT include trigger numbers, parentheses, reasoning lines, or any text other than the verdict lines."""

    # Pattern to detect a valid verdict line: the part after the colon must
    # start with one of the three verdict keywords.
    _VERDICT_RE = re.compile(r"^\s*(APPROVE|MODIFY|REJECT)\b", re.IGNORECASE)

    # Extract trigger name from wrapped formats like "Trigger #5 (new_ath)"
    _WRAPPED_KEY_RE = re.compile(r"Trigger\s*#\d+\s*\((\w+)\)", re.IGNORECASE)

    # Valid trigger names — only these are accepted as verdict keys
    _KNOWN_TRIGGERS = {
        "target_hit", "trailing_stop", "partial_profit",
        "stop_loss", "new_ath", "correlation_break",
        "spread_inversion", "spread_compression", "volume_dry",
        "time_24h", "time_6h", "time_decay",
        "vol_spike", "vol_crush", "negative_drift",
        "platform_error", "liquidity_gap", "fee_spike",
        "stale_position", "longshot_decay",
        "political_event_resolved",
    }

    def _normalize_key(self, raw_key: str) -> str | None:
        """Normalize verdict key to just the trigger name.

        Returns None if the key is not a recognized trigger name.

        Handles:
        - "time_decay" → "time_decay" (already clean)
        - "Trigger #5 (new_ath)" → "new_ath"
        - "Trigger #12 (time_decay)" → "time_decay"
        - "- trailing_stop" → "trailing_stop" (strip bullets)
        - "time_decay (leg_abc123)" → "time_decay" (strip leg suffix)
        - "Here are my responses" → None (not a trigger)
        """
        m = self._WRAPPED_KEY_RE.search(raw_key)
        if m:
            name = m.group(1)
            return name if name in self._KNOWN_TRIGGERS else None
        # Strip leading bullets/dashes/numbers
        cleaned = re.sub(r"^[\s\-*\d.]+", "", raw_key).strip()
        # Strip leg suffix: "time_decay (leg_abc123)" → "time_decay"
        cleaned = re.sub(r"\s*\(leg_\w+\)", "", cleaned).strip()
        if cleaned in self._KNOWN_TRIGGERS:
            return cleaned
        return None

    def _parse_response(self, text: str) -> dict:
        """Parse APPROVE/MODIFY/REJECT response per rule.

        Lines that don't contain a recognised verdict keyword after the colon
        are silently skipped — this avoids counting LLM preamble such as
        "Here are my responses:" or "Note:" as false REJECT verdicts.

        Keys are normalized: "Trigger #5 (new_ath): APPROVE" → {"new_ath": {"action": "APPROVE"}}
        """
        verdicts = {}
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line or ":" not in line:
                continue
            parts = line.split(":", 1)
            raw_key = parts[0].strip()
            rest = parts[1].strip()

            # Skip lines whose content after the colon is not a verdict.
            # This filters out preamble ("Here are my responses:"),
            # commentary ("Note:", "Reasoning:", "Rationale:"), and any
            # other non-verdict prose the LLM may emit.
            if not self._VERDICT_RE.match(rest):
                logger.debug("Skipping non-verdict line: %s", line)
                continue

            rule_id = self._normalize_key(raw_key)
            if rule_id is None:
                logger.debug("Skipping unknown trigger key: %s", raw_key)
                continue

            if rest.upper().startswith("APPROVE"):
                verdicts[rule_id] = {"action": "APPROVE"}
            elif rest.upper().startswith("MODIFY"):
                match = re.search(r"MODIFY\s+([\d.]+)", rest, re.IGNORECASE)
                value = float(match.group(1)) if match else None
                verdicts[rule_id] = {"action": "MODIFY", "value": value}
            elif rest.upper().startswith("REJECT"):
                reason = rest[6:].strip()
                verdicts[rule_id] = {"action": "REJECT", "reason": reason}

        return verdicts

    def _rate_check(self) -> bool:
        """Check if we're within rate limits."""
        now = time.time()
        self._call_times = [t for t in self._call_times if now - t < 60]
        return len(self._call_times) < self._max_calls

    async def _call_openai_style(self, provider: dict, prompt: str) -> str:
        """Call OpenAI-compatible API (Groq, OpenRouter)."""
        api_key = os.environ.get(provider["env_var"], "")
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
            "max_tokens": 500,
            "temperature": 0.3,
        }

        r = await http.post(provider["base_url"], json=body, headers=headers)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]

    async def _call_gemini(self, provider: dict, prompt: str) -> str:
        """Call Gemini REST API."""
        api_key = os.environ.get(provider["env_var"], "")
        http = await self._get_http()

        url = provider["base_url"].format(model=provider["model"])
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 500, "temperature": 0.3},
        }

        r = await http.post(url, headers={"x-goog-api-key": api_key}, json=body)
        r.raise_for_status()
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    async def _call_anthropic(self, provider: dict, prompt: str) -> str:
        """Call Anthropic Messages API."""
        api_key = os.environ.get(provider["env_var"], "")
        http = await self._get_http()

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        body = {
            "model": provider["model"],
            "max_tokens": 500,
            "messages": [{"role": "user", "content": prompt}],
        }

        r = await http.post(provider["base_url"], json=body, headers=headers)
        r.raise_for_status()
        data = r.json()
        return data["content"][0]["text"]

    async def _call_provider(self, provider: dict, prompt: str) -> str:
        """Route to the correct API style."""
        if provider["style"] == "openai":
            return await self._call_openai_style(provider, prompt)
        elif provider["style"] == "gemini":
            return await self._call_gemini(provider, prompt)
        elif provider["style"] == "anthropic":
            return await self._call_anthropic(provider, prompt)
        raise ValueError(f"Unknown API style: {provider['style']}")

    def _build_batched_prompt(self, work: list[tuple[dict, list[dict]]],
                              news_context: dict[str, list] | None = None) -> str:
        """Build a single prompt reviewing triggers for multiple packages.

        Each package section is marked with [PKG:id] so the response can be
        parsed back into per-package verdicts. This avoids multiple API calls
        (and 429 rate limits) when reviewing several packages per tick.
        """
        mode_note = "This is PAPER TRADING mode." if self._paper_mode else "This is LIVE TRADING — balance risk carefully."
        sections = []
        for pkg, proposals in work:
            context = self._build_context(pkg)
            proposal_text = "\n".join(
                f"- {p.get('name', '?')}: {p.get('details', '')} → proposed action: {p.get('action', '?')}"
                for p in proposals
            )
            # Add news context if available
            news_text = ""
            pkg_id = pkg.get("id", "")
            if news_context and pkg_id in news_context:
                headlines = news_context[pkg_id]
                if headlines:
                    items = "\n".join(
                        f"  - [{h.get('source', '?')}] \"{h.get('headline', '?')}\" "
                        f"(confidence: {h.get('confidence', '?')}/10, {h.get('sentiment', 'neutral')})"
                        for h in headlines[:5]  # Cap at 5 headlines
                    )
                    news_text = f"\nRECENT NEWS (last 24h):\n{items}\nIf no NEGATIVE news exists, default to REJECT for trailing_stop, negative_drift, time_decay."
                else:
                    news_text = "\nRECENT NEWS: (none found) — no fundamental reason for price movement. Default to REJECT."
            sections.append(f"[PKG:{pkg_id}]\n{context}{news_text}\nTRIGGERS:\n{proposal_text}")

        combined = "\n\n---\n\n".join(sections)

        return f"""You are a prediction market exit execution advisor.

{mode_note}

Review exit triggers for {len(work)} packages below. For each package, provide verdicts grouped under the package marker.

{combined}

---

FORMAT — repeat the package marker, then one verdict per trigger:
[PKG:package_id]
trigger_name: REJECT <short reason>
trigger_name: APPROVE

VERDICT RULES:
- REJECT = the trigger fired but the position should stay open. THIS IS YOUR DEFAULT.
- MODIFY <new_value> = adjust parameter within bounds.
- APPROVE = execute the exit ONLY when genuinely warranted.

CRITICAL — PERFORMANCE DATA: Every automated exit lost money (17 exits, 0 wins, -$143). Default is REJECT.

GUIDELINES:
- DEFAULT IS REJECT. Prediction markets resolve to $0 or $1 — patience wins, panic selling loses.
- target_hit: APPROVE. Mechanical triggers.
- trailing_stop: ALWAYS REJECT. 0/8 wins. Drawdowns are noise on binary markets.
- time_decay: ALWAYS REJECT. Markets move most near settlement — exiting early forfeits the thesis.
- negative_drift: ALWAYS REJECT. Temporary dips are normal volatility.
- stop_loss: ALWAYS REJECT. Binary resolution means drawdowns are not terminal.
- stale_position: REJECT if P&L between -10% and +10%. APPROVE only if >10 days and <2% move.
- longshot_decay: APPROVE only if position lost >30% of entry value.
- new_ath, vol_spike, spread_compression: REJECT — informational only.
- ANY trigger within 6 hours of expiry: REJECT (max gamma near settlement, exiting forfeits highest-value window).
Do NOT include reasoning, just verdict lines grouped by package."""

    def _parse_batched_response(self, text: str, pkg_ids: list[str]) -> dict[str, dict]:
        """Parse a batched AI response into per-package verdicts.

        Looks for [PKG:id] markers. If no markers found, falls back to
        parsing the entire response as verdicts for the first package.
        """
        result: dict[str, dict] = {}

        # Split by package markers
        import re as _re
        sections = _re.split(r'\[PKG:([^\]]+)\]', text)

        # sections[0] = preamble (before first marker), then alternating id, content
        if len(sections) >= 3:
            for i in range(1, len(sections), 2):
                pkg_id = sections[i].strip()
                content = sections[i + 1] if i + 1 < len(sections) else ""
                verdicts = self._parse_response(content)
                if verdicts:
                    result[pkg_id] = verdicts
        else:
            # No markers found — assign all verdicts to first package
            if pkg_ids:
                verdicts = self._parse_response(text)
                if verdicts:
                    result[pkg_ids[0]] = verdicts

        return result

    async def review_proposals(self, pkg: dict, proposals: list[dict]) -> dict:
        """Review exit proposals via LLM chain. Returns verdicts dict.

        Tries providers in order: Groq → Gemini → OpenRouter → Anthropic.
        Returns empty dict if all fail (caller falls back to auto-execute).
        """
        providers = self._get_available_providers()
        if not providers:
            logger.info("No AI providers configured — will use auto-execute")
            return {}

        if not self._rate_check():
            logger.warning("Rate limit exceeded — skipping AI review")
            return {}

        prompt = self._build_prompt(pkg, proposals)

        for provider in providers:
            try:
                logger.info("Trying AI review via %s for %s", provider["name"], pkg.get("id", "?"))
                text = await self._call_provider(provider, prompt)
                self._call_times.append(time.time())
                self._last_provider = provider["name"]

                verdicts = self._parse_response(text)
                if verdicts:
                    logger.info("AI review via %s for %s: %d verdicts",
                                provider["name"], pkg.get("id", "?"), len(verdicts))
                    return verdicts
                logger.warning("AI review via %s returned no parseable verdicts", provider["name"])

            except Exception as e:
                err_str = str(e)
                # Disable provider for 1 hour on payment/auth failures (402, 401)
                if "402" in err_str or "Payment Required" in err_str:
                    self._disabled_providers[provider["name"]] = time.time() + 3600
                    logger.warning("AI provider %s has no credits (402) — disabled for 1 hour", provider["name"])
                elif "401" in err_str or "Unauthorized" in err_str:
                    self._disabled_providers[provider["name"]] = time.time() + 3600
                    logger.warning("AI provider %s unauthorized (401) — disabled for 1 hour", provider["name"])
                else:
                    logger.warning("AI review via %s failed: %s — trying next provider", provider["name"], e)
                continue

        logger.warning("All AI providers failed for %s — falling back to auto-execute", pkg.get("id", "?"))
        return {}

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
