"""Arbitrage strategy research — scrapes Wikipedia and trading resources
for best arbitrage approaches, stores findings in data/strategy_research.json."""
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("research.strategies")

DATA_DIR = Path(__file__).parent.parent / "data"
STRATEGIES_FILE = DATA_DIR / "strategy_research.json"

# Wikipedia articles to scrape for strategy research
WIKIPEDIA_SOURCES = [
    ("Arbitrage", "https://en.wikipedia.org/wiki/Arbitrage"),
    ("Prediction market", "https://en.wikipedia.org/wiki/Prediction_market"),
    ("Theta (finance)", "https://en.wikipedia.org/wiki/Greeks_(finance)#Theta"),
    ("Kelly criterion", "https://en.wikipedia.org/wiki/Kelly_criterion"),
    ("Statistical arbitrage", "https://en.wikipedia.org/wiki/Statistical_arbitrage"),
    ("Pairs trading", "https://en.wikipedia.org/wiki/Pairs_trade"),
    ("Triangular arbitrage", "https://en.wikipedia.org/wiki/Triangular_arbitrage"),
    ("Covered interest arbitrage", "https://en.wikipedia.org/wiki/Covered_interest_arbitrage"),
    ("Merger arbitrage", "https://en.wikipedia.org/wiki/Merger_arbitrage"),
    ("Convertible arbitrage", "https://en.wikipedia.org/wiki/Convertible_arbitrage"),
]

# Pre-built strategy knowledge base (supplements scraping)
STRATEGY_DATABASE: list[dict[str, Any]] = [
    {
        "strategy_name": "Cross-platform prediction market arbitrage",
        "description": "Buy YES on platform A and NO on platform B for the same event when the combined cost is less than $1.00. Guaranteed profit equals 1 - (yes_price + no_price). Works because different platforms have different user bases with different opinions, creating price discrepancies.",
        "expected_edge_pct": "2-8%",
        "risk_factors": ["Counterparty risk (platform default)", "Resolution disputes", "Liquidity risk on exit", "Fee drag (trading + withdrawal)"],
        "implementation_notes": "Already implemented in arbitrage_engine.py. Key optimization: ensure distinct platforms for buy/sell legs. Use optimal capital allocation (Kelly or proportional).",
        "sources": ["arbitrout internal", "Polymarket docs"],
    },
    {
        "strategy_name": "Crypto spot vs prediction market hedging",
        "description": "When a prediction market offers contracts like 'BTC > $100k by July' at price P, compare against the implied probability from options markets or spot price + historical volatility. If the prediction market overprices the event, sell (or buy NO). Hedge with actual BTC spot position.",
        "expected_edge_pct": "5-15%",
        "risk_factors": ["Basis risk (prediction market vs options expiry mismatch)", "Crypto volatility", "Margin/collateral requirements", "Execution timing"],
        "implementation_notes": "Use Black-Scholes implied probability from crypto_spot adapter. Compare against prediction market YES prices. Edge = |implied_prob - market_price|. Hedge ratio depends on delta.",
        "sources": ["Options pricing theory", "Wikipedia: Black-Scholes model"],
    },
    {
        "strategy_name": "Theta decay harvesting",
        "description": "As prediction market contracts approach expiry, prices should converge to 0 or 1. Contracts trading at intermediate prices near expiry represent either slow-to-update markets or genuine uncertainty. When the outcome is nearly certain (>95% probability from external data), buying the winning side captures the remaining spread as time passes.",
        "expected_edge_pct": "5-20%",
        "risk_factors": ["Late resolution surprises", "Platform delays in settlement", "Low liquidity near expiry"],
        "implementation_notes": "Scan for contracts with expiry < 7 days. Compare market price against externally-derived probability. Flag when |external_prob - market_price| > 0.10. Prioritize high-volume markets for execution.",
        "sources": ["Options theta decay theory", "Wikipedia: Greeks (finance)"],
    },
    {
        "strategy_name": "Commodity futures vs prediction market contracts",
        "description": "Prediction markets offer contracts on commodity prices (e.g., 'Oil above $80 by Q3'). Compare against actual commodity futures curves. If futures imply a different probability than the prediction market price, arbitrage exists between the two markets.",
        "expected_edge_pct": "3-10%",
        "risk_factors": ["Futures margin requirements", "Rolling costs", "Basis between futures and prediction market settlement"],
        "implementation_notes": "Use commodities adapter for spot prices. Calculate forward price from futures curve. Derive implied probability of exceeding strike at expiry. Compare against prediction market prices.",
        "sources": ["Wikipedia: Commodity market", "CME Group education"],
    },
    {
        "strategy_name": "Kelly criterion for optimal bet sizing",
        "description": "The Kelly criterion determines the optimal fraction of capital to wager: f* = (bp - q) / b, where b = net odds (payout/wager - 1), p = probability of winning, q = 1-p. For prediction markets: if you estimate true probability p and the market offers odds b, Kelly tells you what fraction of bankroll to risk. Half-Kelly (f*/2) is common to reduce variance.",
        "expected_edge_pct": "Maximizes long-run growth rate",
        "risk_factors": ["Overestimating edge leads to over-betting (ruin risk)", "Assumes accurate probability estimates", "Ignores correlation between bets"],
        "implementation_notes": "For each arbitrage opportunity, calculate Kelly fraction using estimated edge. Cap at 5% of total bankroll per position. Use half-Kelly for conservative sizing. Track actual vs expected returns to calibrate probability estimates.",
        "sources": ["Wikipedia: Kelly criterion", "Ed Thorp papers"],
    },
    {
        "strategy_name": "Cross-exchange crypto arbitrage",
        "description": "Buy crypto on exchange A at lower price, sell on exchange B at higher price. Requires pre-funded accounts on both exchanges. Spread typically 0.1-0.5% for major pairs, higher for smaller coins. Speed is critical.",
        "expected_edge_pct": "0.1-1%",
        "risk_factors": ["Transfer time between exchanges", "Fee erosion", "Price movement during transfer", "Withdrawal limits", "Exchange counterparty risk"],
        "implementation_notes": "Monitor price feeds from multiple exchanges via WebSocket. Pre-fund both sides. Execute simultaneously. Only profitable at scale with low latency.",
        "sources": ["CoinGecko API docs", "Wikipedia: Arbitrage"],
    },
    {
        "strategy_name": "Triangular arbitrage in prediction markets",
        "description": "When three related prediction market contracts have prices that don't sum correctly. Example: 'Candidate A wins', 'Candidate B wins', 'Candidate C wins' — if YES prices sum to more than 1.0, there's arbitrage by selling all three.",
        "expected_edge_pct": "1-5%",
        "risk_factors": ["Execution risk across three legs", "Liquidity in each leg", "Platform fee structure"],
        "implementation_notes": "Extend event_matcher to detect multi-outcome events (same category, complementary outcomes). Check if sum of YES prices != 1.0.",
        "sources": ["Wikipedia: Triangular arbitrage", "Prediction market theory"],
    },
    {
        "strategy_name": "Statistical arbitrage via mean reversion",
        "description": "Track historical spread between correlated prediction market contracts. When spread exceeds 2 standard deviations from mean, bet on mean reversion.",
        "expected_edge_pct": "2-6%",
        "risk_factors": ["Regime change", "Sample size for statistics", "Transaction costs eating the edge"],
        "implementation_notes": "Store historical price pairs in arbitrage cache. Calculate rolling mean and std of spread. Signal when |spread - mean| > 2*std.",
        "sources": ["Wikipedia: Statistical arbitrage", "Pairs trading literature"],
    },
]


def _scrape_wikipedia_summary(url: str) -> str:
    """Scrape the introduction section from a Wikipedia article."""
    try:
        from scrapling import Fetcher
    except ImportError:
        return ""

    try:
        fetcher = Fetcher(auto_match=True)
        page = fetcher.get(url)
        status = getattr(page, "status", None) or getattr(page, "status_code", None)
        if not page or (status and status != 200):
            return ""

        paragraphs = page.css("div.mw-parser-output > p:not(.mw-empty-elt)")
        text_parts = []
        for p in paragraphs[:3]:
            text = p.text.strip()
            text = re.sub(r"\[\d+\]", "", text)
            if text and len(text) > 20:
                text_parts.append(text)
        return " ".join(text_parts)[:1000]
    except Exception as exc:
        logger.warning("Wikipedia scrape failed for %s: %s", url, exc)
        return ""


def research_strategies(force: bool = False) -> list[dict[str, Any]]:
    """Research arbitrage strategies — combines database with Wikipedia scraping.

    Returns list of strategy dicts saved to data/strategy_research.json.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Check cache (valid for 30 days)
    if not force and STRATEGIES_FILE.exists():
        try:
            stat = STRATEGIES_FILE.stat()
            if time.time() - stat.st_mtime < 30 * 86400:
                cached = json.loads(STRATEGIES_FILE.read_text(encoding="utf-8"))
                # Cache stores {"strategies": [...], ...} — return just the list
                if isinstance(cached, dict) and "strategies" in cached:
                    return cached["strategies"]
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    strategies = list(STRATEGY_DATABASE)

    # Enrich with Wikipedia research
    wiki_summaries: dict[str, str] = {}
    for topic, url in WIKIPEDIA_SOURCES:
        summary = _scrape_wikipedia_summary(url)
        if summary:
            wiki_summaries[topic] = summary
        time.sleep(1.5)

    for strategy in strategies:
        strategy["wikipedia_context"] = {}
        name_lower = strategy["strategy_name"].lower()
        for topic, summary in wiki_summaries.items():
            topic_lower = topic.lower()
            if any(kw in name_lower for kw in topic_lower.split()) or \
               any(kw in topic_lower for kw in name_lower.split()[:3]):
                strategy["wikipedia_context"][topic] = summary

    output = {
        "strategies": strategies,
        "wikipedia_summaries": wiki_summaries,
        "researched_at": time.time(),
    }

    tmp = str(STRATEGIES_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    os.replace(tmp, str(STRATEGIES_FILE))

    logger.info("Saved %d strategies with %d Wikipedia summaries",
                len(strategies), len(wiki_summaries))
    return strategies


async def research_strategies_async(force: bool = False) -> list[dict[str, Any]]:
    """Async wrapper for research_strategies."""
    return await asyncio.get_running_loop().run_in_executor(
        None, research_strategies, force
    )
