"""
Financial Modeling Prep (FMP) API client via MCP.

Uses FMP's MCP server for stock discovery, company profiles, peer
comparison, and live quotes. Free tier supports: profile, peers,
search, and quote tools (screener requires paid plan).

Discovery strategy (free tier):
  1. search-name  -> find companies by keyword
  2. peers        -> expand universe from seed symbols
  3. profile      -> get CEO, sector, industry, market cap
  4. quote        -> live pricing
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger("fmp_client")

FMP_API_KEY = os.environ.get("FMP_API_KEY", "")
FMP_MCP_URL_TEMPLATE = "https://financialmodelingprep.com/mcp?apikey={key}"


def _enabled() -> bool:
    return bool(FMP_API_KEY)


def _mcp_url() -> str:
    return FMP_MCP_URL_TEMPLATE.format(key=FMP_API_KEY)


async def _call_tool(tool_name: str, args: dict) -> Any:
    """Call an FMP MCP tool and return parsed JSON data."""
    from fastmcp import Client

    try:
        client = Client(_mcp_url())
        async with client:
            result = await asyncio.wait_for(client.call_tool(tool_name, args), timeout=30)
            for c in result.content:
                if hasattr(c, "text"):
                    return json.loads(c.text)
            return None
    except asyncio.TimeoutError:
        logger.warning("FMP MCP %s timeout after 30s", tool_name)
        return None
    except Exception as e:
        logger.debug("FMP MCP %s failed: %s", tool_name, e)
        return None


def _run(coro):
    """Run an async coroutine from sync code."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Already in async context — create task in new thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result(timeout=60)
    else:
        return asyncio.run(coro)


# -----------------------------------------------------------------------
# Public API (sync wrappers around MCP tools)
# -----------------------------------------------------------------------

def search_companies(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Search for companies by name/keyword via FMP search-name tool."""
    if not _enabled():
        return []
    data = _run(_call_tool("search-name", {"query": query, "limit": limit}))
    return data if isinstance(data, list) else []


def get_profile(symbol: str) -> dict[str, Any] | None:
    """Get detailed company profile via FMP profile-symbol tool."""
    if not _enabled():
        return None
    data = _run(_call_tool("profile-symbol", {"symbol": symbol}))
    if isinstance(data, list) and data:
        return data[0]
    return data if isinstance(data, dict) else None


def get_peers(symbol: str) -> list[dict[str, Any]]:
    """Get peer companies (same sector/cap range) via FMP peers tool."""
    if not _enabled():
        return []
    data = _run(_call_tool("peers", {"symbol": symbol}))
    return data if isinstance(data, list) else []


def get_quote(symbol: str) -> dict[str, Any] | None:
    """Get live quote via FMP quote tool."""
    if not _enabled():
        return None
    data = _run(_call_tool("quote", {"symbol": symbol}))
    if isinstance(data, list) and data:
        return data[0]
    return data if isinstance(data, dict) else None


def batch_profile(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch profiles for multiple symbols (one MCP call each)."""
    if not symbols or not _enabled():
        return {}
    profiles: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        p = get_profile(sym)
        if p:
            profiles[sym] = p
    return profiles


def discover_stocks(
    keywords: list[str] | None = None,
    seed_symbols: list[str] | None = None,
    limit: int = 50,
) -> list[str]:
    """Discover stock symbols using search + peers expansion.

    1. Search by each keyword to find matching companies
    2. For each seed symbol, find peers in same sector/cap range
    3. Return deduplicated list of US-traded symbols

    This replaces the paid screener with free-tier discovery.
    """
    if not _enabled():
        return []

    found: set[str] = set()

    # Search by keywords
    if keywords:
        for kw in keywords[:3]:  # Limit API calls
            results = search_companies(kw, limit=20)
            for r in results:
                sym = r.get("symbol", "")
                exchange = r.get("exchangeShortName", "") or r.get("exchange", "")
                # Only US-traded stocks
                if sym and exchange in ("NYSE", "NASDAQ", "AMEX", ""):
                    found.add(sym)

    # Expand from seed symbols via peers
    if seed_symbols:
        for seed in seed_symbols[:5]:  # Limit API calls
            peers = get_peers(seed)
            for p in peers:
                sym = p.get("symbol", "")
                if sym:
                    found.add(sym)

    return list(found)[:limit]


def screen_stocks(
    market_cap_more_than: float | None = None,
    market_cap_lower_than: float | None = None,
    sector: str | None = None,
    industry: str | None = None,
    beta_more_than: float | None = None,
    beta_lower_than: float | None = None,
    dividend_more_than: float | None = None,
    volume_more_than: int | None = None,
    exchange: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Screen stocks using free-tier discovery + profile filtering.

    Since the stock-screener tool requires a paid plan, this uses
    search + peers to discover candidates, then filters by profile data.
    """
    if not _enabled():
        return []

    # Build search keywords from sector/industry
    keywords = []
    if sector:
        keywords.append(sector)
    if industry:
        keywords.append(industry)
    if not keywords:
        keywords = ["stock"]  # Generic search

    # Seed symbols for peer expansion based on sector
    sector_seeds = {
        "Technology": ["AAPL", "MSFT"],
        "Healthcare": ["JNJ", "UNH"],
        "Industrials": ["CAT", "GE"],
        "Financial Services": ["JPM", "GS"],
        "Consumer Cyclical": ["AMZN", "TSLA"],
        "Consumer Defensive": ["WMT", "PG"],
        "Energy": ["XOM", "CVX"],
        "Communication Services": ["GOOGL", "META"],
        "Real Estate": ["AMT", "PLD"],
        "Utilities": ["NEE", "DUK"],
        "Basic Materials": ["LIN", "APD"],
    }
    seeds = sector_seeds.get(sector, ["SPY"])

    # Discover candidates
    symbols = discover_stocks(keywords=keywords, seed_symbols=seeds, limit=limit)
    if not symbols:
        return []

    # Fetch profiles and filter
    results = []
    for sym in symbols[:limit]:
        profile = get_profile(sym)
        if not profile:
            continue

        mcap = profile.get("mktCap", 0) or 0
        sym_beta = profile.get("beta", 0) or 0
        sym_sector = profile.get("sector", "") or ""
        sym_industry = profile.get("industry", "") or ""

        # Apply filters
        if market_cap_more_than and mcap < market_cap_more_than:
            continue
        if market_cap_lower_than and mcap > market_cap_lower_than:
            continue
        if sector and sector.lower() not in sym_sector.lower():
            continue
        if industry and industry.lower() not in sym_industry.lower():
            continue
        if beta_more_than and sym_beta < beta_more_than:
            continue
        if beta_lower_than and sym_beta > beta_lower_than:
            continue

        results.append({
            "symbol": profile.get("symbol", sym),
            "companyName": profile.get("companyName", ""),
            "marketCap": mcap,
            "sector": sym_sector,
            "industry": sym_industry,
            "beta": sym_beta,
            "price": profile.get("price", 0),
            "exchange": profile.get("exchangeShortName", ""),
            "country": profile.get("country", ""),
            "ceo": profile.get("ceo", ""),
        })

    logger.info("FMP discovery found %d stocks matching filters", len(results))
    return results


def get_ratios(symbol: str) -> dict[str, Any] | None:
    """Ratios not available on free MCP tier — returns None."""
    return None


def batch_ratios(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Ratios not available on free MCP tier — returns empty."""
    return {}


def get_key_metrics(symbol: str) -> dict[str, Any] | None:
    """Key metrics not available on free MCP tier — returns None."""
    return None


def fmp_to_fundamentals(profile: dict, ratios: dict | None = None) -> dict[str, Any]:
    """Convert FMP profile into our standard fundamentals format."""
    market_cap = profile.get("mktCap", 0) or profile.get("marketCap", 0) or 0
    beta = profile.get("beta", 1.0) or 1.0

    return {
        "sector": profile.get("sector", "") or "",
        "industry": profile.get("industry", "") or "",
        "market_cap": market_cap,
        "revenue_growth": 0,
        "pe_ratio": profile.get("peRatio", 0) or profile.get("pe", 0) or 0,
        "pb_ratio": 0,
        "dividend_yield": round((profile.get("lastDiv", 0) or 0) / profile.get("price") * 100, 2) if isinstance(profile.get("price"), (int, float)) and profile["price"] > 0 and profile.get("lastDiv") else 0,
        "roe": 0,
        "debt_to_equity": 0,
        "profit_margin": 0,
        "beta": beta,
        "fcf": 0,
        "summary": (profile.get("description", "") or "")[:200],
        "ebitda": 0,
        "total_debt": 0,
        "operating_cashflow": 0,
        "capex": 0,
        "net_income": 0,
        "depreciation": 0,
        "total_revenue": 0,
        "gross_profit": 0,
        "ceo": profile.get("ceo", "") or "",
        "company_name": profile.get("companyName", "") or "",
        "exchange": profile.get("exchangeShortName", "") or profile.get("exchange", "") or "",
        "country": profile.get("country", "") or "",
        "gross_margin": 0,
        "ebitda_margin": 0,
        "debt_to_ebitda": 0,
        "ffo_to_debt": 0,
        "focf_to_debt": 0,
    }


FMP_SECTOR_MAP: dict[str, str] = {
    "technology": "technology",
    "healthcare": "health care",
    "financial services": "financials",
    "consumer cyclical": "consumer discretionary",
    "consumer defensive": "consumer staples",
    "communication services": "communication services",
    "industrials": "industrials",
    "basic materials": "materials",
    "real estate": "real estate",
    "utilities": "utilities",
    "energy": "energy",
}


def normalize_fmp_sector(sector: str) -> str:
    return FMP_SECTOR_MAP.get(sector.lower().strip(), sector.lower().strip())
