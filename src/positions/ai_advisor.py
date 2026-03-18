"""AI advisor — Claude API review of exit proposals with batching and guardrails.

Two-stage system: heuristic triggers detect conditions, AI advisor reviews
non-safety triggers before execution. Rate limited to max_calls_per_min.
"""
import logging
import os
import re
import time

logger = logging.getLogger("positions.ai_advisor")

DEFAULT_MODEL = "claude-sonnet-4-20250514"


class AIAdvisor:
    """Reviews exit proposals using Claude API with batching and guardrails."""

    def __init__(self, max_calls_per_min: int = 10):
        self._api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self._model = os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)
        self._client = None
        self._max_calls = max_calls_per_min
        self._call_times: list[float] = []

    def _get_client(self):
        """Lazy init Anthropic client."""
        if not self._client:
            if not self._api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            from anthropic import Anthropic
            self._client = Anthropic(api_key=self._api_key)
        return self._client

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
        """Build structured Claude prompt per spec."""
        context = self._build_context(pkg)
        proposal_text = "\n".join(
            f"- Trigger #{p.get('trigger_id', '?')} ({p.get('name', '?')}): {p.get('details', '')} → proposed action: {p.get('action', '?')}"
            for p in proposals
        )
        return f"""You are a derivatives trading risk advisor. Review the following exit proposals for a prediction market arbitrage package.

PORTFOLIO CONTEXT:
{context}

TRIGGERED EXIT PROPOSALS:
{proposal_text}

For each triggered rule, respond with exactly one line in this format:
<rule_id>: APPROVE | MODIFY <new_value> | REJECT <reason>

Rules:
- APPROVE: Execute the proposed exit action
- MODIFY <value>: Adjust the rule parameter to <value> (must be within rule bounds)
- REJECT <reason>: Do not execute, explain why

Consider: current market conditions, time to expiry, P&L trajectory, and risk/reward.
Be concise. One line per rule."""

    def _parse_response(self, text: str) -> dict:
        """Parse APPROVE/MODIFY/REJECT response per rule."""
        verdicts = {}
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line or ":" not in line:
                continue
            parts = line.split(":", 1)
            rule_id = parts[0].strip()
            rest = parts[1].strip()

            if rest.startswith("APPROVE"):
                verdicts[rule_id] = {"action": "APPROVE"}
            elif rest.startswith("MODIFY"):
                match = re.search(r"MODIFY\s+([\d.]+)", rest)
                value = float(match.group(1)) if match else None
                verdicts[rule_id] = {"action": "MODIFY", "value": value}
            elif rest.startswith("REJECT"):
                reason = rest[6:].strip()
                verdicts[rule_id] = {"action": "REJECT", "reason": reason}
            else:
                verdicts[rule_id] = {"action": "REJECT", "reason": f"Unparseable: {rest}"}

        return verdicts

    def _rate_check(self) -> bool:
        """Check if we're within rate limits."""
        now = time.time()
        self._call_times = [t for t in self._call_times if now - t < 60]
        return len(self._call_times) < self._max_calls

    async def review_proposals(self, pkg: dict, proposals: list[dict]) -> dict:
        """Review exit proposals via Claude API. Returns verdicts dict."""
        if not self._api_key:
            logger.warning("No ANTHROPIC_API_KEY — skipping AI review")
            return {}

        if not self._rate_check():
            logger.warning("Rate limit exceeded — skipping AI review")
            return {}

        prompt = self._build_prompt(pkg, proposals)

        try:
            import asyncio
            client = self._get_client()
            # Run sync API call in executor to not block event loop
            response = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: client.messages.create(
                    model=self._model,
                    max_tokens=500,
                    messages=[{"role": "user", "content": prompt}],
                )
            )
            self._call_times.append(time.time())

            text = response.content[0].text if response.content else ""
            verdicts = self._parse_response(text)
            logger.info("AI review for %s: %d verdicts", pkg.get("id", "?"), len(verdicts))
            return verdicts

        except Exception as e:
            logger.error("Claude API call failed: %s", e)
            raise
