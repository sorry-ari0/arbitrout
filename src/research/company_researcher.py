"""Company researcher — scrapes Wikipedia for CEO, founders, investors, etc."""
import asyncio
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("research.company")

DATA_DIR = Path(__file__).parent.parent / "data"
CACHE_FILE = DATA_DIR / "company_research_cache.json"
_cache_lock = threading.Lock()

# Ticker -> Wikipedia article title (imported from swarm_engine at runtime,
# but we keep a fallback copy here for standalone use)
_COMPANY_NAMES: dict[str, str] = {
    "AAPL": "Apple Inc.", "MSFT": "Microsoft", "GOOGL": "Alphabet Inc.",
    "AMZN": "Amazon (company)", "META": "Meta Platforms", "NVDA": "Nvidia",
    "TSLA": "Tesla, Inc.", "JPM": "JPMorgan Chase", "JNJ": "Johnson & Johnson",
    "UNH": "UnitedHealth Group", "XOM": "ExxonMobil", "CVX": "Chevron Corporation",
    "HD": "The Home Depot", "MCD": "McDonald's", "NKE": "Nike, Inc.",
    "COST": "Costco", "WMT": "Walmart", "DIS": "The Walt Disney Company",
    "NFLX": "Netflix", "CRM": "Salesforce", "ADBE": "Adobe Inc.",
    "INTC": "Intel", "AMD": "Advanced Micro Devices", "QCOM": "Qualcomm",
    "AVGO": "Broadcom Inc.", "PYPL": "PayPal", "SQ": "Block, Inc.",
    "SHOP": "Shopify", "UBER": "Uber", "ABNB": "Airbnb",
    "COIN": "Coinbase", "PLTR": "Palantir Technologies", "SNOW": "Snowflake Inc.",
    "NET": "Cloudflare", "CRWD": "CrowdStrike", "ZS": "Zscaler",
    "DDOG": "Datadog", "RIVN": "Rivian", "LCID": "Lucid Motors",
    "BAC": "Bank of America", "WFC": "Wells Fargo", "PFE": "Pfizer",
    "SOFI": "SoFi Technologies", "HOOD": "Robinhood Markets",
}


def _load_cache() -> dict[str, Any]:
    """Load research cache from disk (thread-safe)."""
    with _cache_lock:
        if CACHE_FILE.exists():
            try:
                return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {}


def _save_cache(cache: dict[str, Any]):
    """Persist research cache to disk (thread-safe)."""
    with _cache_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = str(CACHE_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(CACHE_FILE))


def _wiki_title_for_ticker(ticker: str) -> str:
    """Get the Wikipedia article title for a ticker."""
    # Try our local map first
    if ticker in _COMPANY_NAMES:
        return _COMPANY_NAMES[ticker]
    # Try importing from swarm_engine
    try:
        from swarm_engine import _COMPANY_NAMES as se_names
        if ticker in se_names:
            return se_names[ticker]
    except ImportError:
        pass
    return ""


def _extract_infobox(page) -> dict[str, str]:
    """Extract key-value pairs from a Wikipedia infobox table."""
    info = {}
    try:
        table = page.css_first("table.infobox")
        if not table:
            return info
        rows = table.css("tr")
        for row in rows:
            header = row.css_first("th")
            data = row.css_first("td")
            if header and data:
                key = header.text.strip().lower()
                val = data.text.strip()
                if val:
                    info[key] = val
    except Exception:
        pass
    return info


def _extract_field(infobox: dict, *keys: str) -> str:
    """Try multiple infobox keys, return first match."""
    for k in keys:
        for ib_key, ib_val in infobox.items():
            if k in ib_key:
                return ib_val
    return ""


def _parse_list(text: str) -> list[str]:
    """Split a comma/newline-separated infobox value into a list."""
    if not text:
        return []
    # Split on common separators
    items = re.split(r"[,\n;]", text)
    return [i.strip() for i in items if i.strip() and len(i.strip()) > 1]


def research_company(ticker: str, use_cache: bool = True) -> dict[str, Any]:
    """Scrape Wikipedia for company details.

    Returns dict with: ceo, founders, founding_year, headquarters, industry,
    key_investors, board_members, wikipedia_url, controversies.
    """
    ticker = ticker.upper()
    cache = _load_cache()

    # Check cache (valid for 7 days)
    if use_cache and ticker in cache:
        entry = cache[ticker]
        cached_at = entry.get("_cached_at", 0)
        if time.time() - cached_at < 7 * 86400:
            return entry

    result: dict[str, Any] = {
        "ticker": ticker,
        "ceo": "",
        "founders": [],
        "founding_year": "",
        "headquarters": "",
        "industry": "",
        "key_investors": [],
        "board_members": [],
        "wikipedia_url": "",
        "description": "",
    }

    wiki_title = _wiki_title_for_ticker(ticker)
    if not wiki_title:
        # Try searching Wikipedia
        wiki_title = f"{ticker} company"

    try:
        from scrapling import Fetcher
    except ImportError:
        logger.warning("scrapling not installed — cannot research %s", ticker)
        return result

    try:
        fetcher = Fetcher(auto_match=True)
        url = f"https://en.wikipedia.org/wiki/{wiki_title.replace(' ', '_')}"
        page = fetcher.get(url)
        status = getattr(page, "status", None) or getattr(page, "status_code", None)
        if not page or (status and status != 200):
            # Try search fallback
            search_url = f"https://en.wikipedia.org/w/index.php?search={wiki_title.replace(' ', '+')}&title=Special:Search"
            page = fetcher.get(search_url)
            if not page:
                return result

        result["wikipedia_url"] = url

        # Extract infobox data
        infobox = _extract_infobox(page)

        # CEO / Key people
        key_people = _extract_field(infobox, "key people", "ceo", "chairman", "president")
        if key_people:
            # First person listed is usually CEO
            people = _parse_list(key_people)
            if people:
                # Try to find one marked as CEO
                for p in people:
                    if "ceo" in p.lower() or "chief executive" in p.lower():
                        result["ceo"] = re.sub(r"\(.*?\)", "", p).strip()
                        break
                if not result["ceo"] and people:
                    result["ceo"] = re.sub(r"\(.*?\)", "", people[0]).strip()

        # Founders
        founders_text = _extract_field(infobox, "founder", "founded by")
        result["founders"] = _parse_list(founders_text)

        # Founding year
        founded = _extract_field(infobox, "founded", "inception")
        if founded:
            year_match = re.search(r"\b(1[89]\d{2}|20[0-2]\d)\b", founded)
            if year_match:
                result["founding_year"] = year_match.group(1)

        # Headquarters
        result["headquarters"] = _extract_field(infobox, "headquarters", "location", "hq")

        # Industry
        result["industry"] = _extract_field(infobox, "industry", "sector")

        # Key investors (from infobox or first paragraph)
        investors_text = _extract_field(infobox, "investor", "owner", "parent")
        result["key_investors"] = _parse_list(investors_text)

        # Board members
        board_text = _extract_field(infobox, "board", "director")
        result["board_members"] = _parse_list(board_text)

        # First paragraph as description
        first_p = page.css_first("div.mw-parser-output > p:not(.mw-empty-elt)")
        if first_p:
            desc = first_p.text.strip()
            # Remove citation brackets like [1] [2]
            desc = re.sub(r"\[\d+\]", "", desc).strip()
            result["description"] = desc[:500]

    except Exception as exc:
        logger.warning("Wikipedia scrape failed for %s: %s", ticker, exc)

    # Cache result
    result["_cached_at"] = time.time()
    cache[ticker] = result
    _save_cache(cache)

    return result


def research_batch(tickers: list[str], delay: float = 1.5) -> list[dict[str, Any]]:
    """Research multiple companies with rate limiting."""
    results = []
    for i, ticker in enumerate(tickers):
        result = research_company(ticker)
        results.append(result)
        if i < len(tickers) - 1:
            time.sleep(delay)
    return results


async def research_company_async(ticker: str) -> dict[str, Any]:
    """Async wrapper for research_company."""
    return await asyncio.get_running_loop().run_in_executor(
        None, research_company, ticker
    )


async def research_batch_async(tickers: list[str]) -> list[dict[str, Any]]:
    """Async wrapper for research_batch."""
    return await asyncio.get_running_loop().run_in_executor(
        None, research_batch, tickers
    )
