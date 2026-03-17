"""
Swarm Engine — turns natural-language prompts into screened stock baskets.

Flow:
    1. User POSTs a free-text prompt to /api/generate-asset/screen
    2. intent_parser() asks a local Ollama LLM to extract structured screening
       rules from the prompt.
    3. swarm_evaluator() filters a 103-stock mock universe against those rules.
    4. The matching tickers, parsed rules, and counts are returned as JSON.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

try:
    import fmp_client
except ImportError:
    fmp_client = None

try:
    import dexter_client
except ImportError:
    dexter_client = None

from research import stock_universe

logger = logging.getLogger("swarm_engine")

# ---------------------------------------------------------------------------
# Mock universe — 83 real tickers with synthetic fundamentals
# ---------------------------------------------------------------------------

MOCK_UNIVERSE: dict[str, dict[str, Any]] = {
    # ... (unchanged)
}

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class ScreenRequest(BaseModel):
    """Incoming request body with a free-text screening prompt."""
    prompt: str = Field("", max_length=500, description="Natural-language stock screening prompt")
    rules: dict[str, Any] | None = Field(default=None, description="Structured screening rules that bypass LLM parsing")


class ScreenResponse(BaseModel):
    """Response containing matched tickers and the parsed rules."""
    tickers: list[str]
    rules: dict[str, Any]
    count: int
    universe_size: int = 103
    unresolved: list[str] = []
    notes: str = ""


# ... (unchanged)

def swarm_evaluator(rules: dict[str, Any], universe: dict[str, dict[str, Any]]) -> list[str]:
    # ... (unchanged)

# FastAPI router
# ---------------------------------------------------------------------------
def main():
    router = APIRouter(tags=["Swarm Engine"])

    def _screen_via_fmp(rules: dict) -> list[str] | None:
        # ... (unchanged)

    def _fetch_fmp_fundamentals(symbols: list[str]) -> dict[str, dict]:
        # ... (unchanged)

    @router.post(
        "/api/generate-asset/screen",
        response_model=ScreenResponse,
        summary="Screen stocks from a natural-language prompt",
    )
    async def screen_stocks(body: ScreenRequest) -> ScreenResponse:
        """Turn a natural-language prompt into a screened stock basket.

        Uses a 3-path fallback:
          1. FMP stock screener (covers ALL US stocks)
          2. S&P 500 fundamentals cache (via strategy_engine)
          3. 83-stock mock universe
        """
        # Use structured rules if provided, otherwise parse from prompt
        if body.rules:
            rules = dict(body.rules)
            # If prompt also provided, parse it and merge (structured rules take priority)
            if body.prompt and body.prompt.strip():
                parsed = await intent_parser(body.prompt)
                for k, v in parsed.items():
                    if k not in rules:
                        rules[k] = v
        elif body.prompt and body.prompt.strip():
            rules = await intent_parser(body.prompt)
        else:
            rules = {}

        # If rules is empty, show full mock universe with a note
        if not rules:
            try:
                universe = stock_universe.get_universe()
                all_tickers = [u['ticker'] for u in universe]
                return ScreenResponse(
                    tickers=all_tickers,
                    rules={},
                    count=len(all_tickers),
                    universe_size=len(universe),
                    notes="No specific filters applied — showing full universe.",
                )
            except Exception:
                all_tickers = list(MOCK_UNIVERSE.keys())
                return ScreenResponse(
                    tickers=all_tickers,
                    rules={},
                    count=len(all_tickers),
                    universe_size=len(MOCK_UNIVERSE),
                    notes="No specific filters applied — showing full universe.",
                )

        tickers = []
        universe_size = 0

        # Path 1: FMP screener (covers ALL US stocks)
        fmp_symbols = _screen_via_fmp(rules)
        if fmp_symbols:
            fmp_universe = _fetch_fmp_fundamentals(fmp_symbols)
            if fmp_universe:
                sp500_rules = dict(rules)
                if "min_market_cap" in sp500_rules:
                    sp500_rules["min_market_cap"] = sp500_rules["min_market_cap"] * 1e9
                if "max_market_cap" in sp500_rules:
                    sp500_rules["max_market_cap"] = sp500_rules["max_market_cap"] * 1e9
                if "min_fcf" in sp500_rules:
                    sp500_rules["min_fcf"] = sp500_rules["min_fcf"] * 1e6
                tickers = swarm_evaluator(sp500_rules, fmp_universe)
                universe_size = len(fmp_universe)

        # Path 2: SP500 cache fallback
        if not tickers:
            try:
                from strategy_engine import get_sp500_fundamentals, SP500_CACHE_FILE
                if SP500_CACHE_FILE.exists():
                    universe = get_sp500_fundamentals()
                    sp500_rules = dict(rules)
                    if "min_market_cap" in sp500_rules:
                        sp500_rules["min_market_cap"] = sp500_rules["min_market_cap"] * 1e9
                    if "max_market_cap" in sp500_rules:
                        sp500_rules["max_market_cap"] = sp500_rules["max_market_cap"] * 1e9
                    if "min_fcf" in sp500_rules:
                        sp500_rules["min_fcf"] = sp500_rules["min_fcf"] * 1e6
                    tickers = swarm_evaluator(sp500_rules, universe)
                    universe_size = len(universe)
            except Exception as e:
                logger.debug("SP500 cache fallback failed: %s", e)

        # Path 3: Mock universe fallback
        if not tickers:
            try:
                universe = stock_universe.get_universe()
                tickers = swarm_evaluator(rules, {u['ticker']: {'market_cap': 0, 'fcf': 0, 'debt_to_equity': 0, 'sector': '', 'revenue_growth': 0, 'industry': '', 'pe_ratio': 0, 'roe': 0} for u in universe})
                universe_size = len(universe)
            except Exception:
                tickers = swarm_evaluator(rules, MOCK_UNIVERSE)
                universe_size = len(MOCK_UNIVERSE)

        # ... (unchanged)
