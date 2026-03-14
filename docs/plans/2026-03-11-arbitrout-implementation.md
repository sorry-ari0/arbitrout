# Arbitrout — Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan.

**Goal:** Add prediction market arbitrage scanning to the existing Lobsterminal app. Scan 7 platforms (Kalshi, Polymarket, PredictIt, Limitless, Opinion Labs, Robinhood, Coinbase) for cross-platform arbitrage opportunities and display them in a real-time dashboard accessible via a tab switcher.

**Architecture:** Single FastAPI app serves two modes via top-level tab switcher. API adapters fetch odds from all platforms, an event matcher groups identical events cross-platform, and an arbitrage engine finds profitable spreads. WebSocket pushes live updates.

**Tech Stack:** Python 3.11+, FastAPI, httpx (API calls), Scrapling (scrape fallback), vanilla JS, CSS Grid

**Constraints:**
- Each new file MUST be under 300 lines (Aider + qwen2.5-coder:7b limit)
- Section-based editing — files need clear `# ===` section boundaries
- Test via `curl.exe -s http://127.0.0.1:8500/api/...` (Windows)
- Commit after each task

**New Dependencies (add to requirements.txt):**
```
thefuzz>=0.22.0
scikit-learn>=1.4.0
```

---

## Chunk 1 — Foundation

### Task 1.1: Data Models

**Files:** `src/adapters/__init__.py`, `src/adapters/models.py`

**Steps:**
- [ ] Create `src/adapters/` directory
- [ ] Create `src/adapters/__init__.py`
- [ ] Create `src/adapters/models.py` with dataclasses
- [ ] Commit

**Code — `src/adapters/__init__.py`:**
```python
"""Arbitrout platform adapters package."""
from .models import NormalizedEvent, MatchedEvent, ArbitrageOpportunity
from .registry import AdapterRegistry

__all__ = [
    "NormalizedEvent",
    "MatchedEvent",
    "ArbitrageOpportunity",
    "AdapterRegistry",
]
```

**Code — `src/adapters/models.py`:**
```python
"""Arbitrout data models — shared across all adapters and engines."""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


# ============================================================
# NORMALIZED EVENT
# ============================================================
@dataclass
class NormalizedEvent:
    """A single market from one platform, normalized to common schema."""
    platform: str           # "kalshi", "polymarket", etc.
    event_id: str           # platform-specific ID
    title: str              # "Will Bitcoin exceed $100K by Dec 2026?"
    category: str           # crypto, politics, sports, economics, weather, culture
    yes_price: float        # 0.0 - 1.0
    no_price: float         # 0.0 - 1.0
    volume: int             # trading volume (dollar or contract count)
    expiry: str             # ISO date string or "ongoing"
    url: str                # direct link to market on platform
    last_updated: str = ""  # ISO datetime string

    def __post_init__(self):
        if not self.last_updated:
            self.last_updated = datetime.now(timezone.utc).isoformat()
        # Clamp prices to [0, 1]
        self.yes_price = max(0.0, min(1.0, self.yes_price))
        self.no_price = max(0.0, min(1.0, self.no_price))

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
# MATCHED EVENT
# ============================================================
@dataclass
class MatchedEvent:
    """Same real-world event found on multiple platforms."""
    match_id: str                       # unique ID for this match group
    canonical_title: str                # best title to display
    category: str
    expiry: str
    markets: list[NormalizedEvent] = field(default_factory=list)
    match_type: str = "auto"            # "auto" or "manual"

    @property
    def platform_count(self) -> int:
        return len(set(m.platform for m in self.markets))

    def to_dict(self) -> dict:
        return {
            "match_id": self.match_id,
            "canonical_title": self.canonical_title,
            "category": self.category,
            "expiry": self.expiry,
            "match_type": self.match_type,
            "platform_count": self.platform_count,
            "markets": [m.to_dict() for m in self.markets],
        }


# ============================================================
# ARBITRAGE OPPORTUNITY
# ============================================================
@dataclass
class ArbitrageOpportunity:
    """A profitable spread found across two platforms."""
    matched_event: MatchedEvent
    buy_yes_platform: str       # platform with cheapest YES
    buy_yes_price: float
    buy_no_platform: str        # platform with cheapest NO
    buy_no_price: float
    spread: float               # 1.0 - (yes + no) = profit per $1
    profit_pct: float           # spread * 100
    combined_volume: int

    def to_dict(self) -> dict:
        return {
            "matched_event": self.matched_event.to_dict(),
            "buy_yes_platform": self.buy_yes_platform,
            "buy_yes_price": self.buy_yes_price,
            "buy_no_platform": self.buy_no_platform,
            "buy_no_price": self.buy_no_price,
            "spread": round(self.spread, 4),
            "profit_pct": round(self.profit_pct, 2),
            "combined_volume": self.combined_volume,
        }
```

**Test:**
```bash
cd src && python -c "from adapters.models import NormalizedEvent, MatchedEvent, ArbitrageOpportunity; e = NormalizedEvent('kalshi','k1','Test?','politics',0.6,0.4,1000,'2026-12-31','https://kalshi.com'); print(e.to_dict())"
```

---

### Task 1.2: Base Adapter ABC + Registry

**Files:** `src/adapters/base.py`, `src/adapters/registry.py`

**Steps:**
- [ ] Create `src/adapters/base.py` with BaseAdapter ABC
- [ ] Create `src/adapters/registry.py` with AdapterRegistry
- [ ] Commit

**Code — `src/adapters/base.py`:**
```python
"""Base adapter — abstract class all platform adapters inherit from."""
import logging
import time
from abc import ABC, abstractmethod

import httpx

from .models import NormalizedEvent


# ============================================================
# BASE ADAPTER
# ============================================================
class BaseAdapter(ABC):
    """Abstract base for all prediction market platform adapters."""

    PLATFORM_NAME: str = ""
    BASE_URL: str = ""
    RATE_LIMIT_SECONDS: float = 1.0  # min seconds between requests

    def __init__(self):
        self.logger = logging.getLogger(f"adapters.{self.PLATFORM_NAME}")
        self._last_request_time: float = 0
        self._cache: list[NormalizedEvent] = []
        self._cache_time: float = 0
        self._status: str = "idle"  # idle, fetching, ok, error, blocked
        self._error_msg: str = ""
        self._client: httpx.AsyncClient | None = None

    # ============================================================
    # PUBLIC INTERFACE
    # ============================================================
    async def fetch_events(self) -> list[NormalizedEvent]:
        """Fetch events with caching and error handling."""
        self._status = "fetching"
        try:
            # Rate limiting
            now = time.time()
            wait = self.RATE_LIMIT_SECONDS - (now - self._last_request_time)
            if wait > 0:
                import asyncio
                await asyncio.sleep(wait)

            events = await self._fetch()
            self._last_request_time = time.time()
            self._cache = events
            self._cache_time = time.time()
            self._status = "ok"
            self._error_msg = ""
            self.logger.info("Fetched %d events from %s", len(events), self.PLATFORM_NAME)
            return events
        except Exception as exc:
            self._status = "error"
            self._error_msg = str(exc)[:200]
            self.logger.warning("Fetch failed for %s: %s", self.PLATFORM_NAME, exc)
            return self._cache  # return stale cache on error

    def get_cached(self) -> list[NormalizedEvent]:
        """Return last cached events without fetching."""
        return self._cache

    def get_status(self) -> dict:
        """Return adapter status for /api/arbitrage/platforms."""
        return {
            "platform": self.PLATFORM_NAME,
            "status": self._status,
            "cached_events": len(self._cache),
            "cache_age_seconds": round(time.time() - self._cache_time, 1) if self._cache_time else None,
            "error": self._error_msg or None,
        }

    # ============================================================
    # HTTP CLIENT
    # ============================================================
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create a shared httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                follow_redirects=True,
                headers={"User-Agent": "Arbitrout/1.0"},
            )
        return self._client

    async def close(self):
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ============================================================
    # ABSTRACT — subclasses implement this
    # ============================================================
    @abstractmethod
    async def _fetch(self) -> list[NormalizedEvent]:
        """Fetch and normalize events from the platform. Must be overridden."""
        ...
```

**Code — `src/adapters/registry.py`:**
```python
"""Adapter registry — manages all platform adapters."""
import asyncio
import logging

from .base import BaseAdapter
from .models import NormalizedEvent

logger = logging.getLogger("adapters.registry")


# ============================================================
# ADAPTER REGISTRY
# ============================================================
class AdapterRegistry:
    """Central registry for all platform adapters."""

    def __init__(self):
        self._adapters: dict[str, BaseAdapter] = {}

    def register(self, adapter: BaseAdapter):
        """Register a platform adapter."""
        name = adapter.PLATFORM_NAME
        if not name:
            raise ValueError("Adapter must have a PLATFORM_NAME")
        self._adapters[name] = adapter
        logger.info("Registered adapter: %s", name)

    def get(self, platform: str) -> BaseAdapter | None:
        """Get adapter by platform name."""
        return self._adapters.get(platform)

    def list_platforms(self) -> list[str]:
        """Return list of registered platform names."""
        return list(self._adapters.keys())

    async def fetch_all(self) -> list[NormalizedEvent]:
        """Fetch events from ALL adapters concurrently."""
        if not self._adapters:
            return []

        tasks = [
            adapter.fetch_events()
            for adapter in self._adapters.values()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_events: list[NormalizedEvent] = []
        for i, result in enumerate(results):
            name = list(self._adapters.keys())[i]
            if isinstance(result, Exception):
                logger.warning("Adapter %s raised: %s", name, result)
            elif isinstance(result, list):
                all_events.extend(result)

        logger.info("Total events fetched: %d from %d adapters",
                     len(all_events), len(self._adapters))
        return all_events

    def get_all_cached(self) -> list[NormalizedEvent]:
        """Return cached events from all adapters (no network)."""
        events: list[NormalizedEvent] = []
        for adapter in self._adapters.values():
            events.extend(adapter.get_cached())
        return events

    def get_all_status(self) -> list[dict]:
        """Return status of all adapters."""
        return [a.get_status() for a in self._adapters.values()]

    async def close_all(self):
        """Close all adapter HTTP clients."""
        for adapter in self._adapters.values():
            await adapter.close()
```

**Test:**
```bash
cd src && python -c "
from adapters.registry import AdapterRegistry
from adapters.base import BaseAdapter
from adapters.models import NormalizedEvent
r = AdapterRegistry()
print('Platforms:', r.list_platforms())
print('Registry OK')
"
```

---

### Task 1.3: Update requirements.txt

**Files:** `src/requirements.txt`

**Steps:**
- [ ] Add thefuzz and scikit-learn to requirements.txt
- [ ] Commit

**Edit — append to `src/requirements.txt`:**
```
thefuzz>=0.22.0
scikit-learn>=1.4.0
```

**Test:**
```bash
cd src && pip install -r requirements.txt --dry-run 2>&1 | head -5
```

---

## Chunk 2 — API Adapters

### Task 2.1: Kalshi Adapter

**Files:** `src/adapters/kalshi.py`

**Steps:**
- [ ] Create `src/adapters/kalshi.py`
- [ ] Commit

**Code — `src/adapters/kalshi.py`:**
```python
"""Kalshi adapter — REST API, API key auth."""
import os
from .base import BaseAdapter
from .models import NormalizedEvent


# ============================================================
# CATEGORY MAPPING
# ============================================================
KALSHI_CATEGORY_MAP = {
    "Politics": "politics",
    "Economics": "economics",
    "Crypto": "crypto",
    "Climate": "weather",
    "Culture": "culture",
    "Sports": "sports",
    "Tech": "culture",
    "Science": "culture",
    "Finance": "economics",
}


def _map_category(raw: str) -> str:
    """Map Kalshi category string to our standard categories."""
    for key, val in KALSHI_CATEGORY_MAP.items():
        if key.lower() in raw.lower():
            return val
    return "culture"


# ============================================================
# KALSHI ADAPTER
# ============================================================
class KalshiAdapter(BaseAdapter):
    """Fetch markets from Kalshi trading API v2."""

    PLATFORM_NAME = "kalshi"
    BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"
    RATE_LIMIT_SECONDS = 1.0

    def __init__(self):
        super().__init__()
        self._api_key = os.environ.get("KALSHI_API_KEY", "")

    # ============================================================
    # FETCH IMPLEMENTATION
    # ============================================================
    async def _fetch(self) -> list[NormalizedEvent]:
        client = await self._get_client()
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        events: list[NormalizedEvent] = []
        cursor = None
        limit = 200

        # Paginate through markets (max 3 pages = 600 markets)
        for _ in range(3):
            params: dict = {"limit": limit, "status": "open"}
            if cursor:
                params["cursor"] = cursor

            resp = await client.get(
                f"{self.BASE_URL}/markets",
                headers=headers,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

            markets = data.get("markets", [])
            if not markets:
                break

            for m in markets:
                events.append(self._normalize(m))

            cursor = data.get("cursor")
            if not cursor:
                break

        return events

    # ============================================================
    # NORMALIZATION
    # ============================================================
    def _normalize(self, m: dict) -> NormalizedEvent:
        """Convert Kalshi market JSON to NormalizedEvent."""
        yes_price = (m.get("yes_ask", 0) or 0) / 100.0
        no_price = (m.get("no_ask", 0) or 0) / 100.0

        # Fallback: derive from yes_bid if ask not available
        if yes_price == 0 and m.get("yes_bid"):
            yes_price = m["yes_bid"] / 100.0
        if no_price == 0 and m.get("no_bid"):
            no_price = m["no_bid"] / 100.0
        # If still no no_price, derive from yes
        if no_price == 0 and yes_price > 0:
            no_price = 1.0 - yes_price

        volume = m.get("volume", 0) or 0
        expiry = m.get("expiration_time", m.get("close_time", "ongoing"))
        if expiry and "T" in str(expiry):
            expiry = str(expiry)[:10]  # keep date only

        category = _map_category(m.get("category", "") or m.get("series_ticker", ""))
        ticker = m.get("ticker", m.get("id", ""))

        return NormalizedEvent(
            platform="kalshi",
            event_id=ticker,
            title=m.get("title", m.get("subtitle", ticker)),
            category=category,
            yes_price=round(yes_price, 4),
            no_price=round(no_price, 4),
            volume=int(volume),
            expiry=expiry or "ongoing",
            url=f"https://kalshi.com/markets/{ticker}",
        )
```

**Test:**
```bash
curl.exe -s "https://trading-api.kalshi.com/trade-api/v2/markets?limit=2&status=open" | python -m json.tool | head -30
```

---

### Task 2.2: Polymarket Adapter

**Files:** `src/adapters/polymarket.py`

**Steps:**
- [ ] Create `src/adapters/polymarket.py`
- [ ] Commit

**Code — `src/adapters/polymarket.py`:**
```python
"""Polymarket adapter — Gamma API (no auth) + CLOB for prices."""
from .base import BaseAdapter
from .models import NormalizedEvent


# ============================================================
# CATEGORY MAPPING
# ============================================================
POLY_CATEGORY_MAP = {
    "politics": "politics",
    "crypto": "crypto",
    "sports": "sports",
    "pop culture": "culture",
    "science": "culture",
    "business": "economics",
    "economics": "economics",
    "finance": "economics",
    "world": "politics",
    "technology": "culture",
}


def _map_category(tags: list | None) -> str:
    if not tags:
        return "culture"
    for tag in tags:
        t = str(tag).lower().strip()
        if t in POLY_CATEGORY_MAP:
            return POLY_CATEGORY_MAP[t]
    return "culture"


# ============================================================
# POLYMARKET ADAPTER
# ============================================================
class PolymarketAdapter(BaseAdapter):
    """Fetch markets from Polymarket Gamma API."""

    PLATFORM_NAME = "polymarket"
    BASE_URL = "https://gamma-api.polymarket.com"
    CLOB_URL = "https://clob.polymarket.com"
    RATE_LIMIT_SECONDS = 0.5

    # ============================================================
    # FETCH IMPLEMENTATION
    # ============================================================
    async def _fetch(self) -> list[NormalizedEvent]:
        client = await self._get_client()
        events: list[NormalizedEvent] = []

        # Gamma API — get active markets
        resp = await client.get(
            f"{self.BASE_URL}/markets",
            params={
                "closed": "false",
                "limit": 100,
                "order": "volume",
                "ascending": "false",
            },
        )
        resp.raise_for_status()
        markets = resp.json()

        if not isinstance(markets, list):
            markets = markets.get("data", markets.get("markets", []))

        for m in markets:
            ev = self._normalize(m)
            if ev:
                events.append(ev)

        return events

    # ============================================================
    # NORMALIZATION
    # ============================================================
    def _normalize(self, m: dict) -> NormalizedEvent | None:
        """Convert Polymarket Gamma market to NormalizedEvent."""
        title = m.get("question", m.get("title", ""))
        if not title:
            return None

        # Prices: outcomePrices is a JSON string like "[\"0.85\",\"0.15\"]"
        yes_price = 0.0
        no_price = 0.0

        outcome_prices = m.get("outcomePrices", "")
        if isinstance(outcome_prices, str) and outcome_prices.startswith("["):
            import json
            try:
                prices = json.loads(outcome_prices)
                if len(prices) >= 2:
                    yes_price = float(prices[0])
                    no_price = float(prices[1])
            except (json.JSONDecodeError, ValueError, IndexError):
                pass

        if yes_price == 0 and no_price == 0:
            # Try bestBid/bestAsk fields
            yes_price = float(m.get("bestBid", 0) or 0)
            no_price = 1.0 - yes_price if yes_price > 0 else 0

        volume = 0
        raw_vol = m.get("volume", m.get("volumeNum", 0))
        try:
            volume = int(float(raw_vol or 0))
        except (ValueError, TypeError):
            pass

        # Expiry
        expiry = m.get("endDate", m.get("end_date_iso", "ongoing"))
        if expiry and "T" in str(expiry):
            expiry = str(expiry)[:10]

        slug = m.get("slug", m.get("id", ""))
        condition_id = m.get("conditionId", m.get("condition_id", slug))
        tags = m.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]

        return NormalizedEvent(
            platform="polymarket",
            event_id=str(condition_id),
            title=title,
            category=_map_category(tags),
            yes_price=round(yes_price, 4),
            no_price=round(no_price, 4),
            volume=volume,
            expiry=expiry or "ongoing",
            url=f"https://polymarket.com/event/{slug}" if slug else f"https://polymarket.com",
        )
```

**Test:**
```bash
curl.exe -s "https://gamma-api.polymarket.com/markets?closed=false&limit=2&order=volume&ascending=false" | python -m json.tool | head -40
```

---

### Task 2.3: PredictIt Adapter

**Files:** `src/adapters/predictit.py`

**Steps:**
- [ ] Create `src/adapters/predictit.py`
- [ ] Commit

**Code — `src/adapters/predictit.py`:**
```python
"""PredictIt adapter — public JSON API, no auth, ~1 req/min rate limit."""
from .base import BaseAdapter
from .models import NormalizedEvent


# ============================================================
# CATEGORY MAPPING
# ============================================================
def _guess_category(name: str, short_name: str) -> str:
    """Guess category from market name text."""
    text = f"{name} {short_name}".lower()
    if any(w in text for w in ["president", "election", "congress", "senate", "governor", "party", "vote", "democrat", "republican", "biden", "trump"]):
        return "politics"
    if any(w in text for w in ["bitcoin", "crypto", "ethereum", "btc"]):
        return "crypto"
    if any(w in text for w in ["gdp", "fed", "inflation", "unemployment", "interest rate", "recession"]):
        return "economics"
    if any(w in text for w in ["weather", "hurricane", "temperature", "climate"]):
        return "weather"
    if any(w in text for w in ["nfl", "nba", "mlb", "nhl", "super bowl", "world cup", "olympics"]):
        return "sports"
    return "politics"  # PredictIt is mostly political


# ============================================================
# PREDICTIT ADAPTER
# ============================================================
class PredictItAdapter(BaseAdapter):
    """Fetch all markets from PredictIt public API."""

    PLATFORM_NAME = "predictit"
    BASE_URL = "https://www.predictit.org/api/marketdata/all/"
    RATE_LIMIT_SECONDS = 60.0  # PredictIt rate limits to ~1 req/min

    # ============================================================
    # FETCH IMPLEMENTATION
    # ============================================================
    async def _fetch(self) -> list[NormalizedEvent]:
        client = await self._get_client()
        resp = await client.get(self.BASE_URL)
        resp.raise_for_status()
        data = resp.json()

        events: list[NormalizedEvent] = []
        markets = data.get("markets", [])

        for market in markets:
            contracts = market.get("contracts", [])
            market_name = market.get("name", "")
            market_id = market.get("id", "")
            market_url = market.get("url", f"https://www.predictit.org/markets/detail/{market_id}")

            for contract in contracts:
                status = contract.get("status", "")
                if status != "Open":
                    continue

                yes_price = contract.get("lastTradePrice", 0) or 0
                best_yes = contract.get("bestBuyYesCost", 0) or 0
                best_no = contract.get("bestBuyNoCost", 0) or 0

                # Prefer bestBuy costs (actual order book)
                if best_yes > 0:
                    yes_price = best_yes
                no_price = best_no if best_no > 0 else (1.0 - yes_price)

                volume = contract.get("totalSharesTraded", 0) or 0
                end_date = market.get("dateEnd", "ongoing")
                if end_date and end_date != "N/A" and "T" in str(end_date):
                    end_date = str(end_date)[:10]
                elif not end_date or end_date == "N/A":
                    end_date = "ongoing"

                contract_name = contract.get("name", contract.get("shortName", ""))
                title = f"{market_name}: {contract_name}" if contract_name != market_name else market_name

                events.append(NormalizedEvent(
                    platform="predictit",
                    event_id=str(contract.get("id", market_id)),
                    title=title,
                    category=_guess_category(market_name, contract_name),
                    yes_price=round(float(yes_price), 4),
                    no_price=round(float(no_price), 4),
                    volume=int(volume),
                    expiry=end_date,
                    url=market_url,
                ))

        return events
```

**Test:**
```bash
curl.exe -s "https://www.predictit.org/api/marketdata/all/" | python -c "import sys,json; d=json.load(sys.stdin); print(f'Markets: {len(d.get(\"markets\",[]))}'); m=d['markets'][0]; print(json.dumps(m,indent=2)[:500])"
```

---

### Task 2.4: Limitless Adapter

**Files:** `src/adapters/limitless.py`

**Steps:**
- [ ] Create `src/adapters/limitless.py`
- [ ] Commit

**Code — `src/adapters/limitless.py`:**
```python
"""Limitless Exchange adapter — public REST API, no auth."""
from .base import BaseAdapter
from .models import NormalizedEvent


# ============================================================
# CATEGORY MAPPING
# ============================================================
def _guess_category(title: str, tags: list | None) -> str:
    """Guess category from title and tags."""
    text = title.lower()
    if tags:
        text += " " + " ".join(str(t).lower() for t in tags)
    if any(w in text for w in ["president", "election", "congress", "trump", "biden", "political", "vote"]):
        return "politics"
    if any(w in text for w in ["bitcoin", "crypto", "ethereum", "btc", "eth"]):
        return "crypto"
    if any(w in text for w in ["gdp", "inflation", "fed", "rate", "recession", "economy"]):
        return "economics"
    if any(w in text for w in ["weather", "hurricane", "temperature"]):
        return "weather"
    if any(w in text for w in ["nfl", "nba", "mlb", "nhl", "soccer", "football", "basketball"]):
        return "sports"
    return "culture"


# ============================================================
# LIMITLESS ADAPTER
# ============================================================
class LimitlessAdapter(BaseAdapter):
    """Fetch markets from Limitless Exchange API."""

    PLATFORM_NAME = "limitless"
    BASE_URL = "https://api.limitless.exchange"
    RATE_LIMIT_SECONDS = 1.0

    # ============================================================
    # FETCH IMPLEMENTATION
    # ============================================================
    async def _fetch(self) -> list[NormalizedEvent]:
        client = await self._get_client()
        events: list[NormalizedEvent] = []

        # Step 1: get active market list
        resp = await client.get(f"{self.BASE_URL}/markets/browse-active")
        resp.raise_for_status()
        markets = resp.json()

        if not isinstance(markets, list):
            markets = markets.get("markets", markets.get("data", []))

        for m in markets:
            ev = self._normalize(m)
            if ev:
                events.append(ev)

        return events

    # ============================================================
    # NORMALIZATION
    # ============================================================
    def _normalize(self, m: dict) -> NormalizedEvent | None:
        """Convert Limitless market to NormalizedEvent."""
        title = m.get("title", m.get("question", ""))
        if not title:
            return None

        market_id = str(m.get("id", m.get("slug", "")))

        # Prices — Limitless uses probability or price fields
        yes_price = 0.0
        no_price = 0.0

        if "probability" in m:
            yes_price = float(m["probability"])
            no_price = 1.0 - yes_price
        elif "yes_price" in m:
            yes_price = float(m["yes_price"])
            no_price = float(m.get("no_price", 1.0 - yes_price))
        elif "lastPrice" in m:
            yes_price = float(m["lastPrice"])
            no_price = 1.0 - yes_price

        volume = 0
        raw_vol = m.get("volume", m.get("totalVolume", 0))
        try:
            volume = int(float(raw_vol or 0))
        except (ValueError, TypeError):
            pass

        expiry = m.get("closeDate", m.get("endDate", m.get("expiresAt", "ongoing")))
        if expiry and "T" in str(expiry):
            expiry = str(expiry)[:10]
        elif not expiry:
            expiry = "ongoing"

        slug = m.get("slug", market_id)
        tags = m.get("tags", [])

        return NormalizedEvent(
            platform="limitless",
            event_id=market_id,
            title=title,
            category=_guess_category(title, tags),
            yes_price=round(yes_price, 4),
            no_price=round(no_price, 4),
            volume=volume,
            expiry=str(expiry),
            url=f"https://limitless.exchange/markets/{slug}",
        )
```

**Test:**
```bash
curl.exe -s "https://api.limitless.exchange/markets/browse-active" | python -c "import sys,json; d=json.load(sys.stdin); print(type(d), len(d) if isinstance(d,list) else list(d.keys())[:5])"
```

---

### Task 2.5: Opinion Labs Adapter

**Files:** `src/adapters/opinion_labs.py`

**Steps:**
- [ ] Create `src/adapters/opinion_labs.py`
- [ ] Commit

**Code — `src/adapters/opinion_labs.py`:**
```python
"""Opinion Labs adapter — REST API with apikey header auth, 15 req/sec."""
import os
from .base import BaseAdapter
from .models import NormalizedEvent


# ============================================================
# CATEGORY MAPPING
# ============================================================
def _guess_category(title: str, category_raw: str = "") -> str:
    text = f"{title} {category_raw}".lower()
    if any(w in text for w in ["president", "election", "congress", "trump", "biden", "vote", "political"]):
        return "politics"
    if any(w in text for w in ["bitcoin", "crypto", "ethereum", "btc"]):
        return "crypto"
    if any(w in text for w in ["gdp", "inflation", "fed", "rate", "economy", "recession"]):
        return "economics"
    if any(w in text for w in ["weather", "hurricane", "temperature", "climate"]):
        return "weather"
    if any(w in text for w in ["nfl", "nba", "mlb", "soccer", "sports"]):
        return "sports"
    return "culture"


# ============================================================
# OPINION LABS ADAPTER
# ============================================================
class OpinionLabsAdapter(BaseAdapter):
    """Fetch markets from Opinion Labs (opinion.trade) API."""

    PLATFORM_NAME = "opinion_labs"
    BASE_URL = "https://proxy.opinion.trade:8443/openapi"
    RATE_LIMIT_SECONDS = 0.1  # 15 req/sec allowed

    def __init__(self):
        super().__init__()
        self._api_key = os.environ.get("OPINION_LABS_API_KEY", "")

    # ============================================================
    # FETCH IMPLEMENTATION
    # ============================================================
    async def _fetch(self) -> list[NormalizedEvent]:
        client = await self._get_client()
        headers = {}
        if self._api_key:
            headers["apikey"] = self._api_key

        events: list[NormalizedEvent] = []

        # Try /markets endpoint
        resp = await client.get(
            f"{self.BASE_URL}/markets",
            headers=headers,
            params={"status": "active", "limit": 100},
        )
        resp.raise_for_status()
        data = resp.json()

        markets = data if isinstance(data, list) else data.get("markets", data.get("data", []))

        for m in markets:
            ev = self._normalize(m)
            if ev:
                events.append(ev)

        return events

    # ============================================================
    # NORMALIZATION
    # ============================================================
    def _normalize(self, m: dict) -> NormalizedEvent | None:
        """Convert Opinion Labs market to NormalizedEvent."""
        title = m.get("title", m.get("question", m.get("name", "")))
        if not title:
            return None

        market_id = str(m.get("id", m.get("marketId", "")))

        # Prices
        yes_price = 0.0
        no_price = 0.0

        if "yesPrice" in m:
            yes_price = float(m["yesPrice"])
        elif "probability" in m:
            yes_price = float(m["probability"])
        elif "lastPrice" in m:
            yes_price = float(m["lastPrice"])

        if "noPrice" in m:
            no_price = float(m["noPrice"])
        elif yes_price > 0:
            no_price = 1.0 - yes_price

        volume = 0
        raw_vol = m.get("volume", m.get("totalVolume", 0))
        try:
            volume = int(float(raw_vol or 0))
        except (ValueError, TypeError):
            pass

        expiry = m.get("closeDate", m.get("endDate", m.get("expiresAt", "ongoing")))
        if expiry and "T" in str(expiry):
            expiry = str(expiry)[:10]
        elif not expiry:
            expiry = "ongoing"

        category_raw = m.get("category", m.get("tags", ""))
        if isinstance(category_raw, list):
            category_raw = " ".join(category_raw)

        slug = m.get("slug", market_id)

        return NormalizedEvent(
            platform="opinion_labs",
            event_id=market_id,
            title=title,
            category=_guess_category(title, str(category_raw)),
            yes_price=round(yes_price, 4),
            no_price=round(no_price, 4),
            volume=volume,
            expiry=str(expiry),
            url=f"https://opinion.trade/markets/{slug}",
        )
```

**Test:**
```bash
curl.exe -s "https://proxy.opinion.trade:8443/openapi/markets?status=active&limit=2" | python -m json.tool | head -30
```

---

## Chunk 3 — Scrape Adapters

### Task 3.1: Robinhood Adapter (Scrapling)

**Files:** `src/adapters/robinhood.py`

**Steps:**
- [ ] Create `src/adapters/robinhood.py`
- [ ] Commit

**Code — `src/adapters/robinhood.py`:**
```python
"""Robinhood prediction markets — scrape with Scrapling (no API)."""
import logging
from .base import BaseAdapter
from .models import NormalizedEvent

logger = logging.getLogger("adapters.robinhood")


# ============================================================
# CATEGORY MAPPING
# ============================================================
def _guess_category(title: str) -> str:
    text = title.lower()
    if any(w in text for w in ["president", "election", "congress", "trump", "biden", "vote"]):
        return "politics"
    if any(w in text for w in ["bitcoin", "crypto", "ethereum", "btc"]):
        return "crypto"
    if any(w in text for w in ["gdp", "inflation", "fed", "rate", "recession"]):
        return "economics"
    if any(w in text for w in ["weather", "hurricane", "temperature"]):
        return "weather"
    if any(w in text for w in ["nfl", "nba", "mlb", "nhl", "sports"]):
        return "sports"
    return "culture"


# ============================================================
# ROBINHOOD ADAPTER
# ============================================================
class RobinhoodAdapter(BaseAdapter):
    """Scrape Robinhood prediction markets with Scrapling."""

    PLATFORM_NAME = "robinhood"
    BASE_URL = "https://robinhood.com/prediction-markets/"
    RATE_LIMIT_SECONDS = 30.0  # be polite with scraping

    # ============================================================
    # FETCH IMPLEMENTATION
    # ============================================================
    async def _fetch(self) -> list[NormalizedEvent]:
        import asyncio
        # Run blocking Scrapling in thread
        events = await asyncio.get_event_loop().run_in_executor(
            None, self._scrape_sync
        )
        return events

    def _scrape_sync(self) -> list[NormalizedEvent]:
        """Synchronous scrape of Robinhood prediction markets page."""
        try:
            from scrapling import Fetcher
        except ImportError:
            logger.warning("scrapling not installed — Robinhood adapter disabled")
            return []

        events: list[NormalizedEvent] = []
        try:
            fetcher = Fetcher(auto_match=True)
            page = fetcher.get(self.BASE_URL)

            if not page or not page.status_code or page.status_code != 200:
                logger.warning("Robinhood scrape returned status %s", getattr(page, 'status_code', 'N/A'))
                return []

            # Look for market cards — Robinhood renders markets as cards/rows
            # Selector may need updating as Robinhood changes their DOM
            cards = page.css('[data-testid*="market"], .market-card, [class*="prediction"], [class*="market-row"]')
            if not cards:
                # Fallback: try finding any structured data
                cards = page.css('a[href*="/prediction-markets/"]')

            for card in cards:
                try:
                    title_el = card.css_first('h2, h3, [class*="title"], [class*="question"]')
                    title = title_el.text.strip() if title_el else card.text.strip()[:120]
                    if not title or len(title) < 5:
                        continue

                    # Try to extract price/probability
                    price_el = card.css_first('[class*="price"], [class*="probability"], [class*="percent"]')
                    yes_price = 0.5  # default
                    if price_el:
                        price_text = price_el.text.strip().replace('%', '').replace('$', '').replace('\u00a2', '')
                        try:
                            val = float(price_text)
                            yes_price = val / 100.0 if val > 1 else val
                        except ValueError:
                            pass

                    # Extract href for URL
                    href = card.attrib.get("href", "")
                    url = f"https://robinhood.com{href}" if href.startswith("/") else self.BASE_URL

                    slug = href.split("/")[-1] if href else title[:30].replace(" ", "-").lower()

                    events.append(NormalizedEvent(
                        platform="robinhood",
                        event_id=f"rh-{slug}",
                        title=title,
                        category=_guess_category(title),
                        yes_price=round(yes_price, 4),
                        no_price=round(1.0 - yes_price, 4),
                        volume=0,  # volume not available via scrape
                        expiry="ongoing",
                        url=url,
                    ))
                except Exception as exc:
                    logger.debug("Failed to parse Robinhood card: %s", exc)
                    continue

        except Exception as exc:
            logger.warning("Robinhood scrape failed: %s", exc)

        return events
```

**Test:**
```bash
cd src && python -c "
from adapters.robinhood import RobinhoodAdapter
a = RobinhoodAdapter()
print('Status:', a.get_status())
print('Robinhood adapter OK')
"
```

---

### Task 3.2: Coinbase Adapter (Kalshi Tag)

**Files:** `src/adapters/coinbase.py`

**Steps:**
- [ ] Create `src/adapters/coinbase.py`
- [ ] Commit

**Notes:** Coinbase prediction markets are powered by Kalshi. Rather than scraping, we tag Kalshi markets that are also available on Coinbase by looking for known Coinbase-listed tickers. If Kalshi is unavailable, fall back to Scrapling.

**Code — `src/adapters/coinbase.py`:**
```python
"""Coinbase prediction markets — tags Kalshi markets available on Coinbase,
with Scrapling fallback for direct scraping."""
import logging
from .base import BaseAdapter
from .models import NormalizedEvent

logger = logging.getLogger("adapters.coinbase")


# ============================================================
# COINBASE ADAPTER
# ============================================================
class CoinbaseAdapter(BaseAdapter):
    """Coinbase prediction markets (Kalshi-powered).

    Strategy: Fetch from Kalshi API and relabel markets that appear
    on Coinbase. Falls back to scraping coinbase.com if Kalshi fails.
    """

    PLATFORM_NAME = "coinbase"
    BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"
    RATE_LIMIT_SECONDS = 2.0

    # ============================================================
    # FETCH IMPLEMENTATION
    # ============================================================
    async def _fetch(self) -> list[NormalizedEvent]:
        events = await self._fetch_via_kalshi()
        if not events:
            events = await self._fetch_via_scrape()
        return events

    async def _fetch_via_kalshi(self) -> list[NormalizedEvent]:
        """Fetch Kalshi markets and relabel as Coinbase."""
        try:
            client = await self._get_client()
            resp = await client.get(
                f"{self.BASE_URL}/markets",
                params={"limit": 100, "status": "open"},
            )
            resp.raise_for_status()
            data = resp.json()
            markets = data.get("markets", [])

            events: list[NormalizedEvent] = []
            for m in markets:
                # Kalshi markets on Coinbase tend to be popular ones
                # For now, include all and mark as coinbase
                title = m.get("title", m.get("subtitle", ""))
                ticker = m.get("ticker", m.get("id", ""))
                yes_price = (m.get("yes_ask", 0) or 0) / 100.0
                no_price = (m.get("no_ask", 0) or 0) / 100.0
                if yes_price == 0 and m.get("yes_bid"):
                    yes_price = m["yes_bid"] / 100.0
                if no_price == 0 and yes_price > 0:
                    no_price = 1.0 - yes_price

                volume = m.get("volume", 0) or 0
                expiry = m.get("expiration_time", "ongoing")
                if expiry and "T" in str(expiry):
                    expiry = str(expiry)[:10]

                events.append(NormalizedEvent(
                    platform="coinbase",
                    event_id=f"cb-{ticker}",
                    title=title,
                    category="culture",  # refined by matcher
                    yes_price=round(yes_price, 4),
                    no_price=round(no_price, 4),
                    volume=int(volume),
                    expiry=expiry or "ongoing",
                    url=f"https://www.coinbase.com/prediction-markets",
                ))
            return events

        except Exception as exc:
            logger.warning("Coinbase/Kalshi fetch failed: %s", exc)
            return []

    async def _fetch_via_scrape(self) -> list[NormalizedEvent]:
        """Fallback: scrape Coinbase prediction markets with Scrapling."""
        import asyncio
        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._scrape_sync
            )
        except Exception as exc:
            logger.warning("Coinbase scrape failed: %s", exc)
            return []

    def _scrape_sync(self) -> list[NormalizedEvent]:
        """Sync scrape of Coinbase."""
        try:
            from scrapling import Fetcher
        except ImportError:
            return []

        events: list[NormalizedEvent] = []
        try:
            fetcher = Fetcher(auto_match=True)
            page = fetcher.get("https://www.coinbase.com/prediction-markets")
            if not page or page.status_code != 200:
                return []

            cards = page.css('[class*="market"], [class*="prediction"], a[href*="prediction"]')
            for card in cards[:50]:
                try:
                    title = card.text.strip()[:120]
                    if not title or len(title) < 5:
                        continue
                    events.append(NormalizedEvent(
                        platform="coinbase",
                        event_id=f"cb-scrape-{len(events)}",
                        title=title,
                        category="culture",
                        yes_price=0.5,
                        no_price=0.5,
                        volume=0,
                        expiry="ongoing",
                        url="https://www.coinbase.com/prediction-markets",
                    ))
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("Coinbase scrape error: %s", exc)

        return events
```

**Test:**
```bash
cd src && python -c "
from adapters.coinbase import CoinbaseAdapter
a = CoinbaseAdapter()
print('Status:', a.get_status())
print('Coinbase adapter OK')
"
```

---

## Chunk 4 — Event Matcher

### Task 4.1: Event Matcher Module

**Files:** `src/event_matcher.py`

**Steps:**
- [ ] Create `src/event_matcher.py` with fuzzy + TF-IDF matching
- [ ] Commit

**Code — `src/event_matcher.py`:**
```python
"""Event matcher — groups identical events across platforms."""
import hashlib
import json
import logging
import re
from pathlib import Path

from adapters.models import NormalizedEvent, MatchedEvent

logger = logging.getLogger("event_matcher")

DATA_DIR = Path(__file__).parent / "data" / "arbitrage"


# ============================================================
# TEXT NORMALIZATION
# ============================================================
_STRIP_PREFIXES = ["will ", "what ", "which ", "when ", "how ", "is ", "are ", "does "]
_STRIP_SUFFIXES = ["?", ".", "!"]


def _normalize_title(title: str) -> str:
    """Normalize a market title for comparison."""
    text = title.lower().strip()
    # Remove common prefixes
    for prefix in _STRIP_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):]
    # Remove trailing punctuation
    for suffix in _STRIP_SUFFIXES:
        text = text.rstrip(suffix)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _title_hash(title: str) -> str:
    """Short hash of normalized title for ID generation."""
    return hashlib.md5(_normalize_title(title).encode()).hexdigest()[:12]


# ============================================================
# FUZZY MATCHING
# ============================================================
def _fuzzy_score(a: str, b: str) -> float:
    """Score similarity between two titles using thefuzz."""
    try:
        from thefuzz import fuzz
        # Weighted: token_sort_ratio (word order insensitive) + partial_ratio
        score1 = fuzz.token_sort_ratio(a, b) / 100.0
        score2 = fuzz.partial_ratio(a, b) / 100.0
        return 0.6 * score1 + 0.4 * score2
    except ImportError:
        # Fallback: simple word overlap (Jaccard)
        words_a = set(a.lower().split())
        words_b = set(b.lower().split())
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        union = words_a | words_b
        return len(intersection) / len(union)


def _expiry_compatible(a: str, b: str, max_days: int = 7) -> bool:
    """Check if two expiry dates are within max_days of each other."""
    if a == "ongoing" or b == "ongoing":
        return True  # ongoing matches anything
    try:
        from datetime import datetime
        da = datetime.strptime(a[:10], "%Y-%m-%d")
        db = datetime.strptime(b[:10], "%Y-%m-%d")
        return abs((da - db).days) <= max_days
    except (ValueError, TypeError):
        return True  # if we can't parse, don't block match


# ============================================================
# MANUAL LINKS
# ============================================================
def _load_manual_links() -> list[dict]:
    """Load manually linked events from JSON file."""
    f = DATA_DIR / "manual_links.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_manual_links(links: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "manual_links.json").write_text(json.dumps(links, indent=2))


# ============================================================
# MATCH ENGINE
# ============================================================
MATCH_THRESHOLD = 0.70  # fuzzy score threshold


def match_events(events: list[NormalizedEvent]) -> list[MatchedEvent]:
    """Group events into MatchedEvent clusters.

    Algorithm:
    1. Apply manual links first (highest priority)
    2. Group by platform to avoid self-matching
    3. For each cross-platform pair, compute fuzzy score
    4. If score >= threshold AND category compatible AND expiry
       compatible, merge into same MatchedEvent
    """
    if not events:
        return []

    # --- Phase 1: Manual links ---
    manual_links = _load_manual_links()
    manual_groups: dict[str, list[NormalizedEvent]] = {}
    linked_ids: set[str] = set()

    for link in manual_links:
        link_id = link.get("link_id", "")
        event_ids = set(link.get("event_ids", []))
        group: list[NormalizedEvent] = []
        for ev in events:
            key = f"{ev.platform}:{ev.event_id}"
            if key in event_ids:
                group.append(ev)
                linked_ids.add(key)
        if len(group) >= 2:
            manual_groups[link_id] = group

    # --- Phase 2: Auto-match remaining events ---
    unlinked = [e for e in events if f"{e.platform}:{e.event_id}" not in linked_ids]

    # Normalize titles
    norm_titles = [_normalize_title(e.title) for e in unlinked]

    # Union-Find for clustering
    parent = list(range(len(unlinked)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Compare all cross-platform pairs
    for i in range(len(unlinked)):
        for j in range(i + 1, len(unlinked)):
            # Skip same-platform
            if unlinked[i].platform == unlinked[j].platform:
                continue
            # Fuzzy score
            score = _fuzzy_score(norm_titles[i], norm_titles[j])
            if score < MATCH_THRESHOLD:
                continue
            # Category check (same category, or one is "culture" catch-all)
            cat_i, cat_j = unlinked[i].category, unlinked[j].category
            if cat_i != cat_j and cat_i != "culture" and cat_j != "culture":
                continue
            # Expiry check
            if not _expiry_compatible(unlinked[i].expiry, unlinked[j].expiry):
                continue
            union(i, j)

    # Build clusters
    clusters: dict[int, list[int]] = {}
    for i in range(len(unlinked)):
        root = find(i)
        clusters.setdefault(root, []).append(i)

    # --- Phase 3: Build MatchedEvent objects ---
    results: list[MatchedEvent] = []

    # Manual groups
    for link_id, group in manual_groups.items():
        results.append(MatchedEvent(
            match_id=link_id,
            canonical_title=group[0].title,
            category=group[0].category,
            expiry=group[0].expiry,
            markets=group,
            match_type="manual",
        ))

    # Auto clusters (only multi-platform)
    for root, indices in clusters.items():
        cluster_events = [unlinked[i] for i in indices]
        platforms = set(e.platform for e in cluster_events)
        if len(platforms) < 2:
            # Single platform — still include as standalone for browsing
            for ev in cluster_events:
                results.append(MatchedEvent(
                    match_id=f"auto-{_title_hash(ev.title)}",
                    canonical_title=ev.title,
                    category=ev.category,
                    expiry=ev.expiry,
                    markets=[ev],
                    match_type="auto",
                ))
            continue

        # Pick best title (longest, most descriptive)
        best_title = max(cluster_events, key=lambda e: len(e.title)).title
        best_category = max(
            set(e.category for e in cluster_events),
            key=lambda c: sum(1 for e in cluster_events if e.category == c)
        )
        best_expiry = cluster_events[0].expiry

        results.append(MatchedEvent(
            match_id=f"auto-{_title_hash(best_title)}",
            canonical_title=best_title,
            category=best_category,
            expiry=best_expiry,
            markets=cluster_events,
            match_type="auto",
        ))

    return results


# ============================================================
# MANUAL LINK API
# ============================================================
def add_manual_link(event_ids: list[str]) -> dict:
    """Add a manual link between events. event_ids are 'platform:event_id' strings."""
    links = _load_manual_links()
    link_id = f"manual-{hashlib.md5(':'.join(sorted(event_ids)).encode()).hexdigest()[:8]}"
    # Check for duplicate
    for existing in links:
        if set(existing.get("event_ids", [])) == set(event_ids):
            return existing
    link = {"link_id": link_id, "event_ids": event_ids}
    links.append(link)
    _save_manual_links(links)
    return link


def remove_manual_link(link_id: str) -> bool:
    """Remove a manual link by ID."""
    links = _load_manual_links()
    before = len(links)
    links = [l for l in links if l.get("link_id") != link_id]
    if len(links) < before:
        _save_manual_links(links)
        return True
    return False
```

**Test:**
```bash
cd src && python -c "
from adapters.models import NormalizedEvent
from event_matcher import match_events

events = [
    NormalizedEvent('kalshi', 'k1', 'Will Bitcoin exceed 100K by December 2026?', 'crypto', 0.65, 0.35, 5000, '2026-12-31', 'https://kalshi.com/k1'),
    NormalizedEvent('polymarket', 'p1', 'Bitcoin to exceed \$100K by Dec 2026', 'crypto', 0.60, 0.40, 8000, '2026-12-31', 'https://polymarket.com/p1'),
    NormalizedEvent('predictit', 'pi1', 'Will Trump win 2028 election?', 'politics', 0.40, 0.60, 3000, '2028-11-05', 'https://predictit.org/pi1'),
]
matches = match_events(events)
for m in matches:
    print(f'{m.canonical_title} — {m.platform_count} platforms, type={m.match_type}')
"
```

---

## Chunk 5 — Arbitrage Engine

### Task 5.1: Arbitrage Engine

**Files:** `src/arbitrage_engine.py`

**Steps:**
- [ ] Create `src/arbitrage_engine.py`
- [ ] Commit

**Code — `src/arbitrage_engine.py`:**
```python
"""Arbitrage engine — finds cross-platform spread opportunities."""
import json
import logging
import time
from pathlib import Path

from adapters.models import NormalizedEvent, MatchedEvent, ArbitrageOpportunity
from adapters.registry import AdapterRegistry
from event_matcher import match_events

logger = logging.getLogger("arbitrage_engine")

DATA_DIR = Path(__file__).parent / "data" / "arbitrage"


# ============================================================
# ARBITRAGE CALCULATOR
# ============================================================
def find_arbitrage(matched: list[MatchedEvent],
                   min_spread: float = 0.0,
                   min_volume: int = 0) -> list[ArbitrageOpportunity]:
    """Find arbitrage opportunities across matched events.

    For each MatchedEvent with markets on >=2 platforms:
      best_yes = min(yes_price) across platforms
      best_no  = min(no_price) across platforms
      spread   = 1.0 - (best_yes + best_no)
      If spread > 0 => arbitrage exists.
    """
    opportunities: list[ArbitrageOpportunity] = []

    for match in matched:
        if match.platform_count < 2:
            continue

        markets = match.markets
        # Find cheapest YES and cheapest NO across different platforms
        best_yes_market = min(markets, key=lambda m: m.yes_price)
        best_no_market = min(markets, key=lambda m: m.no_price)

        # Skip if both are on the same platform (no arbitrage)
        if best_yes_market.platform == best_no_market.platform:
            # Try second-best on other platform
            other_yes = [m for m in markets if m.platform != best_no_market.platform]
            other_no = [m for m in markets if m.platform != best_yes_market.platform]
            if other_yes:
                best_yes_market = min(other_yes, key=lambda m: m.yes_price)
            elif other_no:
                best_no_market = min(other_no, key=lambda m: m.no_price)
            else:
                continue

        spread = 1.0 - (best_yes_market.yes_price + best_no_market.no_price)
        profit_pct = spread * 100.0
        combined_vol = sum(m.volume for m in markets)

        if spread < min_spread:
            continue
        if combined_vol < min_volume:
            continue

        opportunities.append(ArbitrageOpportunity(
            matched_event=match,
            buy_yes_platform=best_yes_market.platform,
            buy_yes_price=best_yes_market.yes_price,
            buy_no_platform=best_no_market.platform,
            buy_no_price=best_no_market.no_price,
            spread=spread,
            profit_pct=profit_pct,
            combined_volume=combined_vol,
        ))

    # Sort by profit descending
    opportunities.sort(key=lambda o: o.profit_pct, reverse=True)
    return opportunities


# ============================================================
# SAVED MARKETS
# ============================================================
def _saved_file() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "saved_markets.json"


def load_saved() -> list[dict]:
    f = _saved_file()
    if f.exists():
        try:
            return json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return []


def save_market(event_data: dict) -> list[dict]:
    """Bookmark a matched event."""
    saved = load_saved()
    # Avoid duplicates by match_id
    mid = event_data.get("match_id", "")
    if mid and any(s.get("match_id") == mid for s in saved):
        return saved
    event_data["saved_at"] = time.time()
    saved.append(event_data)
    _saved_file().write_text(json.dumps(saved, indent=2))
    return saved


def unsave_market(match_id: str) -> list[dict]:
    """Remove a bookmark."""
    saved = load_saved()
    saved = [s for s in saved if s.get("match_id") != match_id]
    _saved_file().write_text(json.dumps(saved, indent=2))
    return saved


# ============================================================
# FEED: RECENT PRICE CHANGES
# ============================================================
_previous_prices: dict[str, float] = {}  # "platform:event_id" -> yes_price


def compute_feed(events: list[NormalizedEvent], max_items: int = 50) -> list[dict]:
    """Compute recent price changes for the live feed pane."""
    global _previous_prices
    feed: list[dict] = []

    for ev in events:
        key = f"{ev.platform}:{ev.event_id}"
        prev = _previous_prices.get(key)
        _previous_prices[key] = ev.yes_price

        if prev is not None and prev != ev.yes_price:
            change = ev.yes_price - prev
            feed.append({
                "platform": ev.platform,
                "event_id": ev.event_id,
                "title": ev.title[:80],
                "yes_price": ev.yes_price,
                "previous": prev,
                "change": round(change, 4),
                "change_pct": round(change / prev * 100, 2) if prev > 0 else 0,
                "timestamp": ev.last_updated,
            })

    # Sort by absolute change descending
    feed.sort(key=lambda f: abs(f["change"]), reverse=True)
    return feed[:max_items]


# ============================================================
# FULL SCAN ORCHESTRATOR
# ============================================================
class ArbitrageScanner:
    """Orchestrates the full scan: fetch -> match -> arbitrage."""

    def __init__(self, registry: AdapterRegistry):
        self.registry = registry
        self._last_events: list[NormalizedEvent] = []
        self._last_matched: list[MatchedEvent] = []
        self._last_opportunities: list[ArbitrageOpportunity] = []
        self._last_feed: list[dict] = []
        self._last_scan_time: float = 0

    async def scan(self) -> dict:
        """Run a full scan cycle. Returns summary."""
        # 1. Fetch from all platforms
        events = await self.registry.fetch_all()
        self._last_events = events

        # 2. Match events
        matched = match_events(events)
        self._last_matched = matched

        # 3. Find arbitrage
        opportunities = find_arbitrage(matched)
        self._last_opportunities = opportunities

        # 4. Compute feed
        feed = compute_feed(events)
        self._last_feed = feed

        self._last_scan_time = time.time()

        # 5. Cache to disk
        self._save_cache(events)

        return {
            "events_count": len(events),
            "matched_count": len(matched),
            "multi_platform_matches": sum(1 for m in matched if m.platform_count >= 2),
            "opportunities_count": len(opportunities),
            "feed_changes": len(feed),
            "scan_time": self._last_scan_time,
        }

    def get_opportunities(self) -> list[dict]:
        return [o.to_dict() for o in self._last_opportunities]

    def get_events(self) -> list[dict]:
        return [m.to_dict() for m in self._last_matched]

    def get_feed(self) -> list[dict]:
        return self._last_feed

    def _save_cache(self, events: list[NormalizedEvent]):
        """Persist latest events to disk for offline viewing."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            cache = [e.to_dict() for e in events]
            (DATA_DIR / "cache.json").write_text(json.dumps(cache, indent=2))
        except Exception as exc:
            logger.warning("Cache save failed: %s", exc)
```

**Test:**
```bash
cd src && python -c "
from adapters.models import NormalizedEvent, MatchedEvent
from arbitrage_engine import find_arbitrage

m1 = NormalizedEvent('kalshi','k1','BTC 100K','crypto',0.55,0.45,5000,'2026-12-31','#')
m2 = NormalizedEvent('polymarket','p1','BTC 100K','crypto',0.60,0.35,8000,'2026-12-31','#')
matched = MatchedEvent(match_id='test1',canonical_title='BTC 100K',category='crypto',expiry='2026-12-31',markets=[m1,m2])
opps = find_arbitrage([matched])
for o in opps:
    print(f'Spread: {o.spread:.4f} ({o.profit_pct:.2f}%) — YES@{o.buy_yes_platform} {o.buy_yes_price} + NO@{o.buy_no_platform} {o.buy_no_price}')
"
```

---

## Chunk 6 — Backend API

### Task 6.1: Arbitrage Router

**Files:** `src/arbitrage_router.py`

**Steps:**
- [ ] Create `src/arbitrage_router.py` with all Arbitrout API endpoints
- [ ] Commit

**Code — `src/arbitrage_router.py`:**
```python
"""Arbitrage API router — all /api/arbitrage/* endpoints."""
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from adapters.registry import AdapterRegistry
from arbitrage_engine import ArbitrageScanner, load_saved, save_market, unsave_market
from event_matcher import add_manual_link, remove_manual_link

logger = logging.getLogger("arbitrage_router")

router = APIRouter(prefix="/api/arbitrage", tags=["arbitrage"])


# ============================================================
# GLOBAL STATE (set by server.py on startup)
# ============================================================
_scanner: ArbitrageScanner | None = None
_registry: AdapterRegistry | None = None


def init_scanner(registry: AdapterRegistry):
    """Called by server.py to initialize the scanner."""
    global _scanner, _registry
    _registry = registry
    _scanner = ArbitrageScanner(registry)


def get_scanner() -> ArbitrageScanner:
    if _scanner is None:
        raise RuntimeError("Scanner not initialized")
    return _scanner


# ============================================================
# REQUEST MODELS
# ============================================================
class LinkRequest(BaseModel):
    event_ids: list[str]  # ["kalshi:k1", "polymarket:p1"]


class SaveRequest(BaseModel):
    match_id: str
    canonical_title: str = ""
    category: str = ""


# ============================================================
# ENDPOINTS
# ============================================================
@router.get("/opportunities")
async def get_opportunities():
    """Current arbitrage opportunities, sorted by profit %."""
    scanner = get_scanner()
    return JSONResponse(content=scanner.get_opportunities())


@router.get("/events")
async def get_events():
    """All matched events across platforms."""
    scanner = get_scanner()
    return JSONResponse(content=scanner.get_events())


@router.get("/feed")
async def get_feed():
    """Recent odds changes across all platforms."""
    scanner = get_scanner()
    return JSONResponse(content=scanner.get_feed())


@router.get("/platforms")
async def get_platforms():
    """Platform adapter status (up/down/error)."""
    if _registry is None:
        return JSONResponse(content=[])
    return JSONResponse(content=_registry.get_all_status())


@router.post("/scan")
async def trigger_scan():
    """Manually trigger a full scan cycle."""
    scanner = get_scanner()
    result = await scanner.scan()
    return JSONResponse(content=result)


@router.post("/link")
async def create_link(req: LinkRequest):
    """Manually link events across platforms."""
    if len(req.event_ids) < 2:
        return JSONResponse(content={"error": "Need at least 2 event_ids"}, status_code=400)
    link = add_manual_link(req.event_ids)
    return JSONResponse(content=link)


@router.delete("/link/{link_id}")
async def delete_link(link_id: str):
    """Remove a manual link."""
    removed = remove_manual_link(link_id)
    return JSONResponse(content={"removed": removed, "link_id": link_id})


@router.get("/saved")
async def get_saved():
    """Get bookmarked markets."""
    return JSONResponse(content=load_saved())


@router.post("/saved")
async def add_saved(req: SaveRequest):
    """Bookmark a matched event."""
    saved = save_market(req.model_dump())
    return JSONResponse(content=saved)


@router.delete("/saved/{match_id}")
async def delete_saved(match_id: str):
    """Remove a bookmark."""
    saved = unsave_market(match_id)
    return JSONResponse(content=saved)


# ============================================================
# WEBSOCKET
# ============================================================
_ws_clients: set[WebSocket] = set()
MAX_WS_CLIENTS = 20


@router.websocket("/ws")
async def ws_arbitrage(websocket: WebSocket):
    """WebSocket for real-time arbitrage updates.

    On connect: sends full state.
    On each scan: pushes diff (new opportunities, price changes).
    Client sends: {"action": "scan"} to trigger manual scan,
                  {"action": "subscribe"} to start receiving.
    """
    if len(_ws_clients) >= MAX_WS_CLIENTS:
        await websocket.close(code=1013, reason="Max connections")
        return

    await websocket.accept()
    _ws_clients.add(websocket)
    logger.info("Arbitrage WS client connected (%d total)", len(_ws_clients))

    try:
        scanner = get_scanner()

        # Send initial state
        await websocket.send_json({
            "type": "init",
            "opportunities": scanner.get_opportunities(),
            "events_count": len(scanner.get_events()),
            "platforms": _registry.get_all_status() if _registry else [],
        })

        while True:
            msg = await websocket.receive_json()
            action = msg.get("action", "")

            if action == "scan":
                result = await scanner.scan()
                await websocket.send_json({
                    "type": "scan_result",
                    "summary": result,
                    "opportunities": scanner.get_opportunities(),
                    "feed": scanner.get_feed(),
                })
            elif action == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("Arbitrage WS error: %s", exc)
    finally:
        _ws_clients.discard(websocket)
        logger.info("Arbitrage WS client disconnected (%d remain)", len(_ws_clients))


async def broadcast_update(data: dict):
    """Broadcast scan results to all connected WS clients."""
    dead: list[WebSocket] = []
    for ws in _ws_clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)
```

**Test:**
```bash
curl.exe -s http://127.0.0.1:8500/api/arbitrage/platforms | python -m json.tool
curl.exe -s http://127.0.0.1:8500/api/arbitrage/opportunities | python -m json.tool
curl.exe -s -X POST http://127.0.0.1:8500/api/arbitrage/scan | python -m json.tool
```

---

### Task 6.2: Wire Arbitrage Router into server.py

**Files:** `src/server.py`

**Steps:**
- [ ] Add adapter imports and registry setup to server.py lifespan
- [ ] Include arbitrage_router
- [ ] Commit

**Edit — add to `src/server.py` imports section (after line 33, `logger = ...`):**
```python
# --- Arbitrage imports ---
try:
    from arbitrage_router import router as arbitrage_router, init_scanner
    from adapters.registry import AdapterRegistry
    from adapters.kalshi import KalshiAdapter
    from adapters.polymarket import PolymarketAdapter
    from adapters.predictit import PredictItAdapter
    from adapters.limitless import LimitlessAdapter
    from adapters.opinion_labs import OpinionLabsAdapter
    from adapters.robinhood import RobinhoodAdapter
    from adapters.coinbase import CoinbaseAdapter
    _ARBITRAGE_AVAILABLE = True
except ImportError as _arb_err:
    logger.warning("Arbitrage modules not available: %s", _arb_err)
    _ARBITRAGE_AVAILABLE = False
```

**Edit — add to lifespan function (before `yield`):**
```python
    # Init Arbitrage subsystem
    if _ARBITRAGE_AVAILABLE:
        arb_registry = AdapterRegistry()
        arb_registry.register(KalshiAdapter())
        arb_registry.register(PolymarketAdapter())
        arb_registry.register(PredictItAdapter())
        arb_registry.register(LimitlessAdapter())
        arb_registry.register(OpinionLabsAdapter())
        arb_registry.register(RobinhoodAdapter())
        arb_registry.register(CoinbaseAdapter())
        init_scanner(arb_registry)
        (DATA_DIR / "arbitrage").mkdir(exist_ok=True)
        logger.info("Arbitrage subsystem initialized with %d adapters", len(arb_registry.list_platforms()))
```

**Edit — add router include (after `app.include_router(strategy_router)`):**
```python
if _ARBITRAGE_AVAILABLE:
    app.include_router(arbitrage_router)
```

**Edit — add to lifespan shutdown (before `logger.info("Lobsterminal shutting down")`):**
```python
    if _ARBITRAGE_AVAILABLE:
        await arb_registry.close_all()
```

**Test:**
```bash
cd src && python -c "import server; print('Server imports OK')"
curl.exe -s http://127.0.0.1:8500/api/arbitrage/platforms
```

---

## Chunk 7 — Frontend

### Task 7.1: Arbitrout CSS Theme

**Files:**
- Create: `src/static/css/arbitrout.css`

- [ ] **Step 1: Create arbitrout.css**

```css
/* === ARBITROUT THEME === */
:root {
    --arb-accent: #00e5cc;
    --arb-bg: #0a0a14;
    --arb-card: #12121e;
    --arb-border: #1e1e30;
    --arb-green: #00e676;
    --arb-red: #ff1744;
    --arb-text: #e0e0e0;
    --arb-muted: #888;
}

/* === TAB SWITCHER === */
.tab-switcher {
    display: flex;
    gap: 0;
    background: var(--arb-bg);
    border-bottom: 1px solid var(--arb-border);
    padding: 0 16px;
    position: relative;
    z-index: 100;
}

.tab-switcher .tab-btn {
    background: none;
    border: none;
    color: var(--arb-muted);
    font-family: 'Courier New', monospace;
    font-size: 14px;
    font-weight: 700;
    letter-spacing: 2px;
    padding: 12px 24px;
    cursor: pointer;
    border-bottom: 3px solid transparent;
    transition: color 0.2s, border-color 0.2s;
    text-transform: uppercase;
}

.tab-switcher .tab-btn:hover {
    color: var(--arb-text);
}

.tab-switcher .tab-btn.active-lobster {
    color: #ff8c00;
    border-bottom-color: #ff8c00;
}

.tab-switcher .tab-btn.active-trout {
    color: var(--arb-accent);
    border-bottom-color: var(--arb-accent);
}

/* === SPLASH OVERLAY === */
.splash-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: var(--arb-bg);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    z-index: 9999;
    opacity: 1;
    transition: opacity 0.4s ease-out;
}

.splash-overlay.fade-out {
    opacity: 0;
    pointer-events: none;
}

.splash-title {
    font-family: 'Courier New', monospace;
    font-size: 28px;
    font-weight: 700;
    letter-spacing: 4px;
    margin-top: 20px;
}

.splash-title.teal {
    color: var(--arb-accent);
}

.splash-title.orange {
    color: #ff8c00;
}

.pixel-art-container {
    width: 256px;
    height: 256px;
    image-rendering: pixelated;
}

/* === ARBITROUT LAYOUT === */
.arbitrout-container {
    display: none;
    grid-template-columns: 1fr 1fr;
    grid-template-rows: 1fr 1fr;
    gap: 8px;
    padding: 8px;
    height: calc(100vh - 50px);
    background: var(--arb-bg);
}

.arbitrout-container.active {
    display: grid;
}

.arb-panel {
    background: var(--arb-card);
    border: 1px solid var(--arb-border);
    border-radius: 4px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
}

.arb-panel-header {
    padding: 8px 12px;
    border-bottom: 1px solid var(--arb-border);
    font-family: 'Courier New', monospace;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 2px;
    color: var(--arb-accent);
    text-transform: uppercase;
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.arb-panel-body {
    flex: 1;
    overflow-y: auto;
    padding: 8px;
}

/* === OPPORTUNITY ROWS === */
.opp-row {
    display: grid;
    grid-template-columns: 1fr auto auto auto;
    gap: 8px;
    padding: 8px;
    border-bottom: 1px solid var(--arb-border);
    cursor: pointer;
    transition: background 0.15s;
    align-items: center;
}

.opp-row:hover {
    background: rgba(0, 229, 204, 0.05);
}

.opp-title {
    font-family: 'Courier New', monospace;
    font-size: 12px;
    color: var(--arb-text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.opp-spread {
    font-family: 'Courier New', monospace;
    font-size: 13px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 3px;
}

.opp-spread.positive {
    color: var(--arb-green);
    background: rgba(0, 230, 118, 0.1);
}

.opp-platforms {
    font-family: 'Courier New', monospace;
    font-size: 10px;
    color: var(--arb-muted);
}

.opp-volume {
    font-family: 'Courier New', monospace;
    font-size: 10px;
    color: var(--arb-muted);
}

/* === PLATFORM PRICE ROWS === */
.platform-row {
    display: grid;
    grid-template-columns: 100px 1fr 1fr auto;
    gap: 8px;
    padding: 6px 8px;
    border-bottom: 1px solid var(--arb-border);
    font-family: 'Courier New', monospace;
    font-size: 12px;
}

.platform-name {
    color: var(--arb-accent);
    font-weight: 700;
}

.price-yes, .price-no {
    text-align: right;
}

.price-best {
    color: var(--arb-green);
    font-weight: 700;
}

/* === FEED ITEMS === */
.feed-item {
    padding: 6px 8px;
    border-bottom: 1px solid var(--arb-border);
    font-family: 'Courier New', monospace;
    font-size: 11px;
}

.feed-time {
    color: var(--arb-muted);
    font-size: 10px;
}

.feed-platform {
    color: var(--arb-accent);
}

.feed-price-up {
    color: var(--arb-green);
}

.feed-price-down {
    color: var(--arb-red);
}

/* === SAVED MARKETS === */
.saved-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 8px;
    border-bottom: 1px solid var(--arb-border);
    cursor: pointer;
}

.saved-row:hover {
    background: rgba(0, 229, 204, 0.05);
}

.saved-title {
    font-family: 'Courier New', monospace;
    font-size: 12px;
    color: var(--arb-text);
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.saved-remove {
    background: none;
    border: none;
    color: var(--arb-red);
    cursor: pointer;
    font-size: 14px;
    padding: 0 4px;
}

/* === STATUS BAR === */
.arb-status {
    display: flex;
    gap: 12px;
    padding: 4px 12px;
    background: var(--arb-bg);
    border-top: 1px solid var(--arb-border);
    font-family: 'Courier New', monospace;
    font-size: 10px;
    color: var(--arb-muted);
}

.status-dot {
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    margin-right: 4px;
    vertical-align: middle;
}

.status-dot.online { background: var(--arb-green); }
.status-dot.offline { background: var(--arb-red); }
.status-dot.stale { background: #ffa726; }

/* === EMPTY STATES === */
.arb-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: var(--arb-muted);
    font-family: 'Courier New', monospace;
    font-size: 12px;
}

/* === SCROLLBAR === */
.arb-panel-body::-webkit-scrollbar {
    width: 4px;
}

.arb-panel-body::-webkit-scrollbar-track {
    background: var(--arb-card);
}

.arb-panel-body::-webkit-scrollbar-thumb {
    background: var(--arb-border);
    border-radius: 2px;
}
```

- [ ] **Step 2: Verify CSS file**

```bash
wc -l src/static/css/arbitrout.css
```

Expected: ~270 lines

- [ ] **Step 3: Commit**

```bash
git add src/static/css/arbitrout.css
git commit -m "feat: add Arbitrout CSS theme (teal accent, 4-pane grid)"
```

---

### Task 7.2: Arbitrout Frontend JS

**Files:**
- Create: `src/static/js/arbitrout.js`

- [ ] **Step 1: Create arbitrout.js**

Uses safe DOM methods only (createElement, textContent, classList) — no innerHTML.

```javascript
// === ARBITROUT FRONTEND ===
// Prediction market arbitrage scanner UI

let arbMode = 'lobsterminal';
let arbPollingInterval = null;
let arbWs = null;
let selectedOpp = null;
let feedItems = [];

// === PIXEL ART (CSS grid of div cells) ===
function createPixelGrid(colorMap, scale) {
    // colorMap: 2D array of hex colors (null = transparent)
    const size = colorMap.length;
    const container = document.createElement('div');
    container.style.display = 'grid';
    container.style.gridTemplateColumns = 'repeat(' + size + ', ' + scale + 'px)';
    container.style.gridTemplateRows = 'repeat(' + size + ', ' + scale + 'px)';
    container.style.imageRendering = 'pixelated';

    for (let y = 0; y < size; y++) {
        for (let x = 0; x < size; x++) {
            const cell = document.createElement('div');
            const color = colorMap[y][x];
            if (color) {
                cell.style.backgroundColor = color;
            }
            container.appendChild(cell);
        }
    }
    return container;
}

function getTroutPixelArt() {
    // 16x16 pixel art trout, rendered at 4x scale = 64x64 visual
    const T = '#00e5cc'; // teal body
    const D = '#009688'; // dark teal
    const E = '#004d40'; // eye
    const W = '#e0f7fa'; // white belly
    const F = '#ff8a65'; // fin/tail accent
    const _ = null;
    const map = [
        [_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_],
        [_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_],
        [_,_,_,_,_,_,T,T,T,T,_,_,_,_,_,_],
        [_,_,_,_,_,T,T,T,T,T,T,_,_,_,_,_],
        [_,_,_,_,T,T,T,E,T,T,T,T,_,_,_,_],
        [_,_,_,T,T,T,T,T,T,T,T,T,T,F,_,_],
        [_,_,T,D,T,T,T,T,T,T,T,T,T,F,F,_],
        [_,_,T,D,W,W,T,T,T,T,T,T,F,F,_,_],
        [_,_,T,D,W,W,T,T,T,T,T,T,F,F,_,_],
        [_,_,T,D,T,T,T,T,T,T,T,T,T,F,F,_],
        [_,_,_,T,T,T,T,T,T,T,T,T,T,F,_,_],
        [_,_,_,_,T,T,T,T,T,T,T,T,_,_,_,_],
        [_,_,_,_,_,T,T,T,T,T,T,_,_,_,_,_],
        [_,_,_,_,_,_,T,T,T,T,_,_,_,_,_,_],
        [_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_],
        [_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_]
    ];
    return createPixelGrid(map, 4);
}

function getLobsterPixelArt() {
    const R = '#ff8c00'; // orange body
    const D = '#cc5500'; // dark orange
    const E = '#1a1a2e'; // eye
    const C = '#ff6600'; // claw
    const _ = null;
    const map = [
        [_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_],
        [_,_,C,_,_,_,_,_,_,_,_,_,_,C,_,_],
        [_,C,C,_,_,_,_,_,_,_,_,_,_,C,C,_],
        [_,C,_,_,_,_,R,R,R,R,_,_,_,_,C,_],
        [_,_,_,_,_,R,R,R,R,R,R,_,_,_,_,_],
        [_,_,_,_,R,R,E,R,R,E,R,R,_,_,_,_],
        [_,_,_,_,R,R,R,R,R,R,R,R,_,_,_,_],
        [_,_,_,_,_,D,R,R,R,R,D,_,_,_,_,_],
        [_,_,_,_,_,D,R,R,R,R,D,_,_,_,_,_],
        [_,_,_,_,_,D,R,R,R,R,D,_,_,_,_,_],
        [_,_,_,_,_,_,D,R,R,D,_,_,_,_,_,_],
        [_,_,_,_,_,R,_,D,D,_,R,_,_,_,_,_],
        [_,_,_,_,R,_,_,D,D,_,_,R,_,_,_,_],
        [_,_,_,R,_,_,_,_,_,_,_,_,R,_,_,_],
        [_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_],
        [_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_]
    ];
    return createPixelGrid(map, 4);
}

// === SPLASH SCREEN ===
function showSplash(mode) {
    const overlay = document.createElement('div');
    overlay.className = 'splash-overlay';

    const artContainer = document.createElement('div');
    artContainer.className = 'pixel-art-container';
    if (mode === 'arbitrout') {
        artContainer.appendChild(getTroutPixelArt());
    } else {
        artContainer.appendChild(getLobsterPixelArt());
    }
    overlay.appendChild(artContainer);

    const title = document.createElement('div');
    title.className = 'splash-title ' + (mode === 'arbitrout' ? 'teal' : 'orange');
    title.textContent = mode === 'arbitrout' ? 'ARBITROUT' : 'LOBSTERMINAL';
    overlay.appendChild(title);

    document.body.appendChild(overlay);

    setTimeout(function() {
        overlay.classList.add('fade-out');
        setTimeout(function() {
            if (overlay.parentNode) {
                overlay.parentNode.removeChild(overlay);
            }
        }, 500);
    }, 1200);
}

// === TAB SWITCHING ===
function switchMode(mode) {
    if (mode === arbMode) return;
    arbMode = mode;

    showSplash(mode);

    var lobster = document.getElementById('lobsterminal-container');
    var arb = document.getElementById('arbitrout-container');
    var tabLob = document.getElementById('tab-lobsterminal');
    var tabArb = document.getElementById('tab-arbitrout');

    if (mode === 'arbitrout') {
        if (lobster) lobster.style.display = 'none';
        if (arb) arb.classList.add('active');
        if (tabLob) { tabLob.classList.remove('active-lobster'); }
        if (tabArb) { tabArb.classList.add('active-trout'); }
        startArbPolling();
    } else {
        if (lobster) lobster.style.display = '';
        if (arb) arb.classList.remove('active');
        if (tabLob) { tabLob.classList.add('active-lobster'); }
        if (tabArb) { tabArb.classList.remove('active-trout'); }
        stopArbPolling();
    }
}

// === POLLING ===
function startArbPolling() {
    loadOpportunities();
    loadSavedMarkets();
    arbPollingInterval = setInterval(loadOpportunities, 15000);
    connectArbWs();
}

function stopArbPolling() {
    if (arbPollingInterval) {
        clearInterval(arbPollingInterval);
        arbPollingInterval = null;
    }
    if (arbWs) {
        arbWs.close();
        arbWs = null;
    }
}

// === WEBSOCKET ===
function connectArbWs() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    arbWs = new WebSocket(proto + '//' + location.host + '/ws/arbitrage');

    arbWs.onmessage = function(e) {
        var data = JSON.parse(e.data);
        if (data.type === 'opportunities') {
            renderOpportunities(data.data);
        } else if (data.type === 'feed') {
            addFeedItem(data.data);
        }
    };

    arbWs.onclose = function() {
        if (arbMode === 'arbitrout') {
            setTimeout(connectArbWs, 3000);
        }
    };
}

// === OPPORTUNITIES ===
function loadOpportunities() {
    fetch('/api/arbitrage/opportunities')
        .then(function(r) { return r.json(); })
        .then(function(data) { renderOpportunities(data); })
        .catch(function(err) { console.error('Arb fetch error:', err); });
}

function renderOpportunities(opps) {
    var container = document.getElementById('opp-list');
    if (!container) return;

    // Clear existing
    while (container.firstChild) {
        container.removeChild(container.firstChild);
    }

    if (!opps || opps.length === 0) {
        var empty = document.createElement('div');
        empty.className = 'arb-empty';
        empty.textContent = 'Scanning for opportunities...';
        container.appendChild(empty);
        return;
    }

    opps.forEach(function(opp) {
        var row = document.createElement('div');
        row.className = 'opp-row';
        row.addEventListener('click', function() { showEventDetail(opp); });

        var titleEl = document.createElement('div');
        titleEl.className = 'opp-title';
        titleEl.textContent = opp.canonical_title || opp.matched_event.canonical_title;
        row.appendChild(titleEl);

        var spreadEl = document.createElement('div');
        spreadEl.className = 'opp-spread positive';
        spreadEl.textContent = '+' + (opp.profit_pct || opp.spread * 100).toFixed(1) + '%';
        row.appendChild(spreadEl);

        var platEl = document.createElement('div');
        platEl.className = 'opp-platforms';
        platEl.textContent = (opp.buy_yes_platform || '') + ' / ' + (opp.buy_no_platform || '');
        row.appendChild(platEl);

        var volEl = document.createElement('div');
        volEl.className = 'opp-volume';
        volEl.textContent = '$' + ((opp.combined_volume || 0) / 1000).toFixed(0) + 'K';
        row.appendChild(volEl);

        container.appendChild(row);
    });
}

// === EVENT DETAIL ===
function showEventDetail(opp) {
    selectedOpp = opp;
    var container = document.getElementById('event-detail');
    if (!container) return;

    while (container.firstChild) {
        container.removeChild(container.firstChild);
    }

    var event = opp.matched_event || opp;
    var markets = event.markets || [];

    var headerEl = document.createElement('div');
    headerEl.style.padding = '8px';
    headerEl.style.borderBottom = '1px solid var(--arb-border)';

    var titleEl = document.createElement('div');
    titleEl.style.fontFamily = "'Courier New', monospace";
    titleEl.style.fontSize = '13px';
    titleEl.style.fontWeight = '700';
    titleEl.style.color = 'var(--arb-text)';
    titleEl.textContent = event.canonical_title || opp.canonical_title || '';
    headerEl.appendChild(titleEl);

    var metaEl = document.createElement('div');
    metaEl.style.fontFamily = "'Courier New', monospace";
    metaEl.style.fontSize = '10px';
    metaEl.style.color = 'var(--arb-muted)';
    metaEl.style.marginTop = '4px';
    metaEl.textContent = (event.category || '') + ' | Expires: ' + (event.expiry || 'ongoing');
    headerEl.appendChild(metaEl);

    container.appendChild(headerEl);

    // Column headers
    var colHeader = document.createElement('div');
    colHeader.className = 'platform-row';
    colHeader.style.color = 'var(--arb-muted)';
    colHeader.style.fontSize = '10px';
    var cols = ['PLATFORM', 'YES', 'NO', 'LINK'];
    cols.forEach(function(txt) {
        var c = document.createElement('div');
        c.textContent = txt;
        colHeader.appendChild(c);
    });
    container.appendChild(colHeader);

    // Find best prices
    var bestYes = 1, bestNo = 1;
    markets.forEach(function(m) {
        if (m.yes_price < bestYes) bestYes = m.yes_price;
        if (m.no_price < bestNo) bestNo = m.no_price;
    });

    markets.forEach(function(m) {
        var row = document.createElement('div');
        row.className = 'platform-row';

        var nameEl = document.createElement('div');
        nameEl.className = 'platform-name';
        nameEl.textContent = m.platform;
        row.appendChild(nameEl);

        var yesEl = document.createElement('div');
        yesEl.className = 'price-yes' + (m.yes_price === bestYes ? ' price-best' : '');
        yesEl.textContent = (m.yes_price * 100).toFixed(1) + '\u00A2';
        row.appendChild(yesEl);

        var noEl = document.createElement('div');
        noEl.className = 'price-no' + (m.no_price === bestNo ? ' price-best' : '');
        noEl.textContent = (m.no_price * 100).toFixed(1) + '\u00A2';
        row.appendChild(noEl);

        var linkEl = document.createElement('a');
        linkEl.href = m.url || '#';
        linkEl.target = '_blank';
        linkEl.rel = 'noopener';
        linkEl.textContent = '\u2197';
        linkEl.style.color = 'var(--arb-accent)';
        linkEl.style.textDecoration = 'none';
        row.appendChild(linkEl);

        container.appendChild(row);
    });

    // Save button
    var saveBtn = document.createElement('button');
    saveBtn.style.cssText = 'margin:8px;padding:6px 12px;background:var(--arb-accent);color:var(--arb-bg);border:none;border-radius:3px;cursor:pointer;font-family:monospace;font-size:11px;font-weight:700;';
    saveBtn.textContent = 'BOOKMARK';
    saveBtn.addEventListener('click', function() {
        saveMarket(event.canonical_title || opp.canonical_title);
    });
    container.appendChild(saveBtn);
}

// === FEED ===
function addFeedItem(item) {
    feedItems.unshift(item);
    if (feedItems.length > 100) feedItems = feedItems.slice(0, 100);
    renderFeed();
}

function renderFeed() {
    var container = document.getElementById('feed-list');
    if (!container) return;

    while (container.firstChild) {
        container.removeChild(container.firstChild);
    }

    if (feedItems.length === 0) {
        var empty = document.createElement('div');
        empty.className = 'arb-empty';
        empty.textContent = 'Waiting for price updates...';
        container.appendChild(empty);
        return;
    }

    feedItems.slice(0, 50).forEach(function(item) {
        var el = document.createElement('div');
        el.className = 'feed-item';

        var time = document.createElement('span');
        time.className = 'feed-time';
        time.textContent = (item.time || new Date().toLocaleTimeString()) + ' ';
        el.appendChild(time);

        var plat = document.createElement('span');
        plat.className = 'feed-platform';
        plat.textContent = '[' + (item.platform || '?') + '] ';
        el.appendChild(plat);

        var text = document.createElement('span');
        text.textContent = (item.title || '') + ' ';
        el.appendChild(text);

        if (item.direction) {
            var arrow = document.createElement('span');
            arrow.className = item.direction === 'up' ? 'feed-price-up' : 'feed-price-down';
            arrow.textContent = item.direction === 'up' ? '\u25B2' : '\u25BC';
            if (item.price) arrow.textContent += ' ' + item.price;
            el.appendChild(arrow);
        }

        container.appendChild(el);
    });
}

// === SAVED MARKETS ===
function loadSavedMarkets() {
    fetch('/api/arbitrage/saved')
        .then(function(r) { return r.json(); })
        .then(function(data) { renderSaved(data); })
        .catch(function() {});
}

function renderSaved(items) {
    var container = document.getElementById('saved-list');
    if (!container) return;

    while (container.firstChild) {
        container.removeChild(container.firstChild);
    }

    if (!items || items.length === 0) {
        var empty = document.createElement('div');
        empty.className = 'arb-empty';
        empty.textContent = 'No bookmarked markets';
        container.appendChild(empty);
        return;
    }

    items.forEach(function(item) {
        var row = document.createElement('div');
        row.className = 'saved-row';

        var titleEl = document.createElement('div');
        titleEl.className = 'saved-title';
        titleEl.textContent = item.title || item;
        row.appendChild(titleEl);

        var removeBtn = document.createElement('button');
        removeBtn.className = 'saved-remove';
        removeBtn.textContent = '\u00D7';
        removeBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            removeSaved(item.title || item);
        });
        row.appendChild(removeBtn);

        container.appendChild(row);
    });
}

function saveMarket(title) {
    fetch('/api/arbitrage/saved', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title: title})
    }).then(function() { loadSavedMarkets(); });
}

function removeSaved(title) {
    fetch('/api/arbitrage/saved', {
        method: 'DELETE',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title: title})
    }).then(function() { loadSavedMarkets(); });
}

// === INIT ===
document.addEventListener('DOMContentLoaded', function() {
    var tabLob = document.getElementById('tab-lobsterminal');
    var tabArb = document.getElementById('tab-arbitrout');

    if (tabLob) {
        tabLob.addEventListener('click', function() { switchMode('lobsterminal'); });
    }
    if (tabArb) {
        tabArb.addEventListener('click', function() { switchMode('arbitrout'); });
    }
});
```

- [ ] **Step 2: Verify JS file**

```bash
wc -l src/static/js/arbitrout.js
```

Expected: ~290 lines

- [ ] **Step 3: Commit**

```bash
git add src/static/js/arbitrout.js
git commit -m "feat: add Arbitrout frontend JS (safe DOM, no innerHTML)"
```

---

### Task 7.3: Update index.html — Tab Switcher + Arbitrout Layout

**Files:**
- Modify: `src/static/index.html`

- [ ] **Step 1: Add CSS link and tab switcher HTML**

**Edit — add before the closing `</head>` tag:**
```html
    <link rel="stylesheet" href="/static/css/arbitrout.css">
```

**Edit — add immediately after `<body>` (before any existing content):**
```html
    <!-- Tab Switcher -->
    <div class="tab-switcher">
        <button id="tab-lobsterminal" class="tab-btn active-lobster">LOBSTERMINAL</button>
        <button id="tab-arbitrout" class="tab-btn">ARBITROUT</button>
    </div>
```

- [ ] **Step 2: Wrap existing Lobsterminal content**

Wrap ALL existing body content (after the tab switcher) in:
```html
    <div id="lobsterminal-container">
        <!-- existing content here -->
    </div>
```

- [ ] **Step 3: Add Arbitrout container before closing `</body>`**

```html
    <!-- Arbitrout Layout -->
    <div id="arbitrout-container" class="arbitrout-container">
        <div class="arb-panel">
            <div class="arb-panel-header">
                <span>OPPORTUNITIES</span>
                <span id="opp-count">0</span>
            </div>
            <div class="arb-panel-body" id="opp-list">
                <div class="arb-empty">Scanning for opportunities...</div>
            </div>
        </div>
        <div class="arb-panel">
            <div class="arb-panel-header">EVENT DETAIL</div>
            <div class="arb-panel-body" id="event-detail">
                <div class="arb-empty">Select an opportunity</div>
            </div>
        </div>
        <div class="arb-panel">
            <div class="arb-panel-header">MARKET FEED</div>
            <div class="arb-panel-body" id="feed-list">
                <div class="arb-empty">Waiting for price updates...</div>
            </div>
        </div>
        <div class="arb-panel">
            <div class="arb-panel-header">SAVED MARKETS</div>
            <div class="arb-panel-body" id="saved-list">
                <div class="arb-empty">No bookmarked markets</div>
            </div>
        </div>
    </div>

    <!-- Arbitrout Status Bar -->
    <div class="arb-status" id="arb-status" style="display:none;">
        <span id="arb-status-text">ARBITROUT SCANNING</span>
    </div>

    <script src="/static/js/arbitrout.js"></script>
```

- [ ] **Step 4: Test**

Open `http://127.0.0.1:8500/` in browser — should see LOBSTERMINAL | ARBITROUT tabs at top. Clicking ARBITROUT shows splash + 4-pane grid.

- [ ] **Step 5: Commit**

```bash
git add src/static/index.html
git commit -m "feat: add tab switcher + Arbitrout layout to index.html"
```

---

## Chunk 8 — Data Directory + Final Wiring

### Task 8.1: Create Data Directory + Manual Links

**Files:**
- Create: `src/data/arbitrage/manual_links.json`
- Create: `src/data/arbitrage/saved_markets.json`
- Create: `src/data/arbitrage/cache.json`

- [ ] **Step 1: Create data files**

```bash
mkdir -p src/data/arbitrage
echo "[]" > src/data/arbitrage/manual_links.json
echo "[]" > src/data/arbitrage/saved_markets.json
echo "{}" > src/data/arbitrage/cache.json
```

- [ ] **Step 2: Update .gitignore**

**Edit — add to .gitignore:**
```
src/data/arbitrage/cache.json
```

(manual_links.json and saved_markets.json should be tracked)

- [ ] **Step 3: Commit**

```bash
git add src/data/arbitrage/manual_links.json src/data/arbitrage/saved_markets.json .gitignore
git commit -m "feat: add Arbitrout data directory scaffolding"
```

---

### Task 8.2: End-to-End Verification

- [ ] **Step 1: Start server**

```bash
cd src && python server.py
```

- [ ] **Step 2: Test all endpoints**

```bash
curl.exe -s http://127.0.0.1:8500/api/arbitrage/platforms
curl.exe -s http://127.0.0.1:8500/api/arbitrage/opportunities
curl.exe -s http://127.0.0.1:8500/api/arbitrage/events
curl.exe -s http://127.0.0.1:8500/api/arbitrage/saved
```

Expected: JSON responses from each (platforms should show status for all 7 adapters).

- [ ] **Step 3: Test frontend**

Open browser to `http://127.0.0.1:8500/` — verify:
1. Tab switcher visible at top
2. Click ARBITROUT → trout splash appears ~1.5s → 4-pane grid appears
3. Click LOBSTERMINAL → lobster splash appears ~1.5s → original terminal
4. Opportunities panel shows "Scanning for opportunities..."

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "feat: Arbitrout v1 complete — prediction market arbitrage scanner"
```
