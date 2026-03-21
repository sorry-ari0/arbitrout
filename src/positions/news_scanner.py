"""News scanner — RSS feed monitor with AI-powered trade signal pipeline.

Background loop that:
1. Fetches RSS feeds from crypto, macro, and finance sources
2. Deduplicates headlines (hash + fuzzy overlap)
3. Pass 1: AI scans headlines against active Polymarket markets
4. Pass 2: Fetches full article text for high-confidence matches
5. Executes trades for breaking/high-confidence signals

Respects position limits, cooldowns, and daily trade caps.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

try:
    import httpx
except ImportError:
    httpx = None

try:
    import feedparser
except ImportError:
    feedparser = None

from .position_manager import create_package, create_leg, create_exit_rule

logger = logging.getLogger("positions.news_scanner")

# ── Position limits ───────────────────────────────────────────────────────────
# Global cap: $2000 total across auto_trader + news scanner combined.
# News scanner reserves 3 slots and $600 from the global $2000 budget.
# These limits are checked against the SHARED position_manager, so the
# effective news budget is: min(MAX_TOTAL_EXPOSURE, global_cap - auto_exposure)
MAX_TRADE_SIZE = 200.0
MIN_TRADE_SIZE = 5.0
MAX_CONCURRENT = 10          # Global max (auto_trader capped at 7)
MAX_TOTAL_EXPOSURE = 2000.0  # Global max (auto_trader capped at $1400)

# ── Timing ───────────────────────────────────────────────────────────────────
COOLDOWN_SECONDS = 15 * 60      # 15 min per-market cooldown
DAILY_TRADE_CAP = 5
MARKET_CACHE_TTL = 10 * 60      # Refresh market cache every 10 min
FUZZY_DEDUP_WINDOW = 30 * 60    # 30 min fuzzy dedup window
HASH_DEDUP_WINDOW = 24 * 3600   # 24h hash dedup window

# ── RSS Feeds ────────────────────────────────────────────────────────────────
RSS_FEEDS = [
    # Crypto
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "category": "crypto"},
    {"name": "CoinTelegraph", "url": "https://cointelegraph.com/rss", "category": "crypto"},
    {"name": "The Block", "url": "https://www.theblock.co/rss.xml", "category": "crypto"},
    {"name": "Decrypt", "url": "https://decrypt.co/feed", "category": "crypto"},
    # Politics (fast-breaking — critical for prediction markets)
    {"name": "Politico", "url": "https://rss.politico.com/politics-news.xml", "category": "politics"},
    {"name": "The Hill", "url": "https://thehill.com/feed/", "category": "politics"},
    {"name": "Axios", "url": "https://api.axios.com/feed/", "category": "macro"},
    # Macro / general news
    {"name": "BBC", "url": "https://feeds.bbci.co.uk/news/rss.xml", "category": "macro"},
    {"name": "Google News Biz", "url": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FtVnVHZ0pWVXlnQVAB", "category": "macro"},
    {"name": "NPR News", "url": "https://feeds.npr.org/1001/rss.xml", "category": "macro"},
    # Finance / markets
    {"name": "Bloomberg Markets", "url": "https://feeds.bloomberg.com/markets/news.rss", "category": "finance"},
    {"name": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex", "category": "finance"},
    {"name": "CNBC", "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "category": "finance"},
    {"name": "MarketWatch", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories", "category": "finance"},
]

# ── Persistence ──────────────────────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "positions")
CACHE_FILE = os.path.join(DATA_DIR, "news_cache.json")

GAMMA_API = "https://gamma-api.polymarket.com"


class NewsScanner:
    """Background news scanner that monitors RSS feeds and triggers AI-driven trades."""

    def __init__(self, position_manager, news_ai, auto_trader=None,
                 decision_logger=None, interval: float = 150.0):
        self.pm = position_manager
        self.news_ai = news_ai
        self.auto_trader = auto_trader
        self.dlog = decision_logger
        self.interval = interval

        self._task: asyncio.Task | None = None
        self._running = False
        self._http: httpx.AsyncClient | None = None

        # Headline dedup state
        self._seen_hashes: dict[str, float] = {}       # hash → timestamp
        self._recent_headlines: list[dict] = []         # [{words: set, ts: float}]
        self._matched_headlines: dict[str, list[dict]] = {}  # condition_id → [{headline, source, ...}]

        # Trade safeguards
        self.daily_trades: dict[str, int] = {}          # date_str → count
        self.cooldowns: dict[str, float] = {}           # market_id → timestamp

        # Market cache
        self._market_cache: list[dict] = []
        self._market_cache_ts: float = 0

        # Stats
        self._headlines_processed = 0
        self._trades_executed = 0
        self._cycles_run = 0

        # Load persisted state
        self._load_cache()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        """Start the news scanner background loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info("News scanner started (interval=%.0fs)", self.interval)

    def stop(self):
        """Stop the scanner loop and close HTTP client."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        if self._http and not self._http.is_closed:
            asyncio.ensure_future(self._http.aclose())
            self._http = None
        logger.info("News scanner stopped (cycles=%d, headlines=%d, trades=%d)",
                     self._cycles_run, self._headlines_processed, self._trades_executed)

    async def _loop(self):
        """Main scan loop."""
        await asyncio.sleep(15)  # Let server fully start
        while self._running:
            try:
                await self._scan_cycle()
                self._cycles_run += 1
            except Exception as e:
                logger.error("News scanner cycle error: %s", e)
            await asyncio.sleep(self.interval)

    # ── HTTP Client ──────────────────────────────────────────────────────────

    async def _get_http(self) -> httpx.AsyncClient:
        """Get or create shared HTTP client."""
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=15.0,
                headers={"User-Agent": "ArbitroutNewsScanner/1.0"},
                follow_redirects=True,
            )
        return self._http

    # ── Main Scan Cycle ──────────────────────────────────────────────────────

    async def _scan_cycle(self):
        """One full scan cycle: fetch RSS → dedup → AI pass 1 → AI pass 2 → execute."""
        if not feedparser:
            logger.warning("News scanner: feedparser not installed, skipping")
            return
        if not httpx:
            logger.warning("News scanner: httpx not installed, skipping")
            return

        # Prune expired state
        self._prune_state()

        # Prune stale headline matches (>48h)
        cutoff_48h = time.time() - 172800
        for cid in list(self._matched_headlines.keys()):
            self._matched_headlines[cid] = [
                h for h in self._matched_headlines[cid] if h.get("timestamp", 0) > cutoff_48h
            ]
            if not self._matched_headlines[cid]:
                del self._matched_headlines[cid]

        # Refresh market cache if stale
        await self._refresh_market_cache()

        if not self._market_cache:
            logger.info("News scanner: no markets cached, skipping cycle")
            return

        # Fetch all RSS feeds concurrently
        loop = asyncio.get_event_loop()
        tasks = []
        for feed in RSS_FEEDS:
            tasks.append(loop.run_in_executor(None, feedparser.parse, feed["url"]))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect and deduplicate headlines
        new_headlines = []
        now = time.time()
        for feed_info, result in zip(RSS_FEEDS, results):
            if isinstance(result, Exception):
                logger.warning("News scanner: failed to fetch %s: %s", feed_info["name"], result)
                continue

            for entry in getattr(result, "entries", [])[:15]:  # Max 15 per feed
                title = (entry.get("title") or "").strip()
                link = entry.get("link", "")
                if not title or len(title) < 10:
                    continue

                # Hash dedup
                h = hashlib.sha256(f"{title}|{feed_info['name']}".lower().encode()).hexdigest()[:16]
                if h in self._seen_hashes:
                    continue

                # Fuzzy dedup: >80% word overlap with any headline from last 30 min
                title_words = set(re.sub(r'[^\w\s]', '', title.lower()).split())
                if self._fuzzy_duplicate(title_words, now):
                    continue

                # Mark as seen
                self._seen_hashes[h] = now
                self._recent_headlines.append({"words": title_words, "ts": now})

                new_headlines.append({
                    "title": title,
                    "url": link,
                    "source": feed_info["name"],
                    "category": feed_info["category"],
                })

        self._headlines_processed += len(new_headlines)

        if not new_headlines:
            logger.debug("News scanner: no new headlines this cycle")
            self._save_cache()
            return

        logger.info("News scanner: %d new headlines from %d feeds", len(new_headlines), len(RSS_FEEDS))

        # Index headlines for Pass 1
        for i, h in enumerate(new_headlines):
            h["index"] = i

        # Small delay before AI call to reduce rate contention with exit engine
        await asyncio.sleep(5)

        # Pass 1: AI headline scan — find relevant headlines matched to markets
        try:
            scan_results = await self.news_ai.scan_headlines(new_headlines, self._market_cache)
        except Exception as e:
            logger.error("News scanner: AI headline scan failed: %s", e)
            if self.dlog:
                self.dlog.log_news_headline(
                    f"[CYCLE {self._cycles_run}] AI scan failed: {e}",
                    "system", "error", "AI_ERROR"
                )
            self._save_cache()
            return

        # Log cycle summary regardless of results
        if self.dlog:
            relevant_indices = {r.get("headline_index") for r in scan_results} if scan_results else set()
            for h in new_headlines:
                action = "RELEVANT" if h["index"] in relevant_indices else "SKIP"
                self.dlog.log_news_headline(h["title"], h["source"], h["category"], action)

        if not scan_results:
            logger.info("News scanner: AI found no relevant headlines (%d scanned)", len(new_headlines))
            self._save_cache()
            return

        # Process relevant results — map AI response back to headline/market objects
        for result in scan_results:
            headline_idx = result.get("headline_index", -1)
            if headline_idx < 0 or headline_idx >= len(new_headlines):
                continue
            headline = new_headlines[headline_idx]

            # Find matched market by title (fuzzy — AI may abbreviate)
            market_title = result.get("market_title", "")
            market = self._find_market(market_title)
            if not market:
                logger.debug("News scanner: AI matched to unknown market: %s", market_title[:60])
                continue

            confidence = result.get("confidence", 0)
            urgency = result.get("urgency", "LOW")
            # Normalize urgency to match spec: HIGH → "breaking", MEDIUM/LOW → "normal"
            if urgency == "HIGH":
                urgency = "breaking"
            else:
                urgency = "normal"
            # Normalize confidence from 1-100 to 1-10 scale
            confidence = max(1, min(10, confidence // 10))

            title = headline.get("title", "?")
            market_id = market.get("condition_id", "")

            # Cache headline match for exit engine news validation (BEFORE gating)
            side = result.get("side", "")
            if side.upper() == "NO":
                sentiment = "negative"
            elif side.upper() == "YES":
                sentiment = "positive"
            else:
                sentiment = "neutral"
            if market_id:
                if market_id not in self._matched_headlines:
                    self._matched_headlines[market_id] = []
                self._matched_headlines[market_id].append({
                    "headline": title,
                    "source": headline.get("source", "unknown"),
                    "timestamp": time.time(),
                    "confidence": confidence,
                    "sentiment": sentiment,
                    "market_title": market.get("question", market.get("title", "")),
                })
                # Cap at 500 total entries to bound memory
                total = sum(len(v) for v in self._matched_headlines.values())
                if total > 500:
                    oldest_cid, oldest_idx = None, None
                    oldest_ts = float("inf")
                    for cid, entries in self._matched_headlines.items():
                        for i, e in enumerate(entries):
                            if e.get("timestamp", 0) < oldest_ts:
                                oldest_ts = e["timestamp"]
                                oldest_cid, oldest_idx = cid, i
                    if oldest_cid is not None:
                        self._matched_headlines[oldest_cid].pop(oldest_idx)
                        if not self._matched_headlines[oldest_cid]:
                            del self._matched_headlines[oldest_cid]

            # Gate: confidence >= 5 or breaking news (relaxed from 7 — research showed
            # 7,143 headlines → 0 trades executed due to overly conservative filters)
            if confidence < 5 and urgency != "breaking":
                continue

            # Check safeguards before committing to deep analysis
            if self._check_daily_cap():
                logger.info("News scanner: daily trade cap reached, skipping deep analysis")
                break

            if market_id and self._has_open_position(market_id):
                logger.info("News scanner: already hold position on %s, skipping", market_id[:12])
                continue

            if market_id and self._check_cooldown(market_id):
                logger.info("News scanner: market %s on cooldown, skipping", market_id[:12])
                continue

            # Pass 2: Fetch article and run deep analysis
            article_url = headline.get("url", "")
            article_text = ""
            if article_url:
                try:
                    article_text = await self._fetch_article(article_url)
                except Exception as e:
                    logger.warning("News scanner: article fetch failed for %s: %s", article_url, e)

            # Build portfolio state for AI context
            open_pkgs = self.pm.list_packages("open")
            portfolio_state = {
                "open_positions": len(open_pkgs),
                "total_exposure": round(sum(p.get("total_cost", 0) for p in open_pkgs), 2),
                "max_exposure": MAX_TOTAL_EXPOSURE,
                "max_concurrent": MAX_CONCURRENT,
                "daily_trades_today": self.daily_trades.get(self._today_str(), 0),
                "daily_cap": DAILY_TRADE_CAP,
            }

            try:
                analysis = await self.news_ai.deep_analysis(
                    article_text=article_text,
                    headline=title,
                    market=market,
                    portfolio=portfolio_state,
                )
            except Exception as e:
                logger.error("News scanner: deep analysis failed: %s", e)
                continue

            if not analysis:
                continue

            # Route based on analysis result
            action = analysis.get("action", "NO_TRADE")
            # Deep analysis returns confidence on 1-100 scale, normalize
            raw_conf = analysis.get("confidence", 0)
            final_confidence = max(1, min(10, raw_conf // 10)) if raw_conf > 10 else raw_conf
            side = analysis.get("side", "YES") or "YES"
            reasoning = analysis.get("reasoning", "")

            # Log the deep dive result
            if self.dlog:
                self.dlog.log_news_signal(
                    title=title[:100], market=market.get("title", "?"),
                    side=side, confidence=final_confidence,
                    urgency=urgency, article_fetched=bool(article_text),
                    deep_dive_result=action,
                )

            if action == "NO_TRADE":
                logger.info("News scanner: AI says NO_TRADE for '%s': %s", title[:60], reasoning[:100])
                continue

            # High confidence → execute immediately (news edge decays fast)
            # Lowered from 8→7: research showed 0 trades executed at old thresholds
            if final_confidence >= 7:
                await self._execute_news_trade(
                    headline=headline,
                    market=market,
                    confidence=final_confidence,
                    side=side,
                    reasoning=reasoning,
                    urgency=urgency,
                )

            # Moderate confidence → queue for auto_trader or execute breaking
            # Lowered from 7→5: let more signals through to auto_trader scoring
            elif final_confidence >= 5:
                if urgency == "breaking":
                    # Breaking news at conf 5+ → execute directly (time-sensitive)
                    await self._execute_news_trade(
                        headline=headline,
                        market=market,
                        confidence=final_confidence,
                        side=side,
                        reasoning=reasoning,
                        urgency=urgency,
                    )
                elif self.auto_trader:
                    opp = self._build_opportunity(headline, market, analysis)
                    try:
                        await self.auto_trader.add_news_opportunity(opp)
                        logger.info("News scanner: queued opportunity for '%s' (confidence=%d)",
                                    title[:60], final_confidence)
                    except Exception as e:
                        logger.warning("News scanner: failed to queue opportunity: %s", e)
                else:
                    # No auto_trader — execute directly
                    await self._execute_news_trade(
                        headline=headline,
                        market=market,
                        confidence=final_confidence,
                        side=side,
                        reasoning=reasoning,
                            urgency=urgency,
                        )

            # Brief delay between analyses to avoid rate limiting
            await asyncio.sleep(2)

        self._save_cache()

    # ── Article Fetching ─────────────────────────────────────────────────────

    async def _fetch_article(self, url: str) -> str:
        """Fetch article text — httpx first, fall back to scrapling if too short."""
        # Try httpx first (lighter)
        try:
            http = await self._get_http()
            r = await http.get(url, timeout=5.0, follow_redirects=True)
            text = self._extract_text(r.text)
            if len(text) > 500:
                return text[:2000]
        except Exception:
            pass

        # Fall back to scrapling
        try:
            from scrapling import Fetcher
            loop = asyncio.get_event_loop()
            fetcher = Fetcher(auto_match=True)
            page = await loop.run_in_executor(None, fetcher.get, url)
            return page.get_text()[:2000]
        except Exception:
            return ""

    @staticmethod
    def _extract_text(html: str) -> str:
        """Strip HTML tags and collapse whitespace."""
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    # ── Trade Execution ──────────────────────────────────────────────────────

    async def _execute_news_trade(self, headline: dict, market: dict,
                                   confidence: int, side: str, reasoning: str,
                                   urgency: str):
        """Execute a news-driven trade directly."""
        title = headline.get("title", "?")
        market_id = market.get("condition_id", "")
        market_title = market.get("title", "?")
        yes_price = market.get("yes_price", 0.5)

        # Position limit checks
        if self._check_position_limits():
            logger.info("News scanner: position limits reached, skipping trade for '%s'", title[:60])
            return

        if self._check_daily_cap():
            logger.info("News scanner: daily cap reached, skipping trade for '%s'", title[:60])
            return

        if market_id and self._check_cooldown(market_id):
            logger.info("News scanner: market on cooldown, skipping trade for '%s'", title[:60])
            return

        if market_id and self._has_open_position(market_id):
            logger.info("News scanner: already hold position on market, skipping")
            return

        # Set cooldown BEFORE executing (prevents double-entry on fast cycles)
        if market_id:
            self.cooldowns[market_id] = time.time()

        # Size: confidence/10 * MAX_TRADE_SIZE
        trade_size = (confidence / 10) * MAX_TRADE_SIZE
        trade_size = max(MIN_TRADE_SIZE, min(trade_size, MAX_TRADE_SIZE))

        # Clamp to remaining budget
        open_pkgs = self.pm.list_packages("open")
        total_exposure = sum(p.get("total_cost", 0) for p in open_pkgs)
        remaining_budget = MAX_TOTAL_EXPOSURE - total_exposure
        trade_size = min(trade_size, remaining_budget)
        if trade_size < MIN_TRADE_SIZE:
            logger.info("News scanner: insufficient budget ($%.2f remaining)", remaining_budget)
            return

        # Determine entry price based on side
        if side.upper() == "YES":
            entry_price = yes_price if yes_price > 0 else 0.5
            leg_type = "prediction_yes"
        else:
            entry_price = (1.0 - yes_price) if yes_price > 0 else 0.5
            leg_type = "prediction_no"

        # Create package
        try:
            pkg = create_package(f"News: {title[:60]}", "news_driven")
        except ValueError:
            # Fallback if news_driven not yet in STRATEGY_TYPES
            pkg = create_package(f"News: {title[:60]}", "pure_prediction")

        pkg["legs"].append(create_leg(
            platform="polymarket",
            leg_type=leg_type,
            asset_id=f"{market_id}:{side.upper()}",
            asset_label=f"{side.upper()} — {market_title[:50]}",
            entry_price=entry_price,
            cost=round(trade_size, 2),
            expiry="2026-12-31",
        ))

        # Exit rules — news trades: widened from trade journal analysis
        # Stop loss widened from -10% to -35%, trailing stop from 8% to 15%
        pkg["exit_rules"].append(create_exit_rule("target_profit", {"target_pct": 15}))
        pkg["exit_rules"].append(create_exit_rule("stop_loss", {"stop_pct": -35}))
        pkg["exit_rules"].append(create_exit_rule("trailing_stop", {"current": 15, "bound_min": 8, "bound_max": 30}))
        pkg["_use_brackets"] = True  # GTC target sell at 0% maker fee
        pkg["_use_limit_orders"] = True

        # Store news metadata
        pkg["_news_source"] = headline.get("source", "")
        pkg["_news_urgency"] = urgency
        pkg["_news_confidence"] = confidence
        pkg["_news_reasoning"] = reasoning[:300]

        # Execute
        try:
            result = await self.pm.execute_package(pkg)
            if result.get("success"):
                self._trades_executed += 1
                today = self._today_str()
                self.daily_trades[today] = self.daily_trades.get(today, 0) + 1
                logger.info("News scanner OPENED: '%s' | side=%s confidence=%d size=$%.2f urgency=%s",
                            title[:60], side, confidence, trade_size, urgency)
                if self.dlog:
                    self.dlog.log_news_trade(
                        pkg_id=pkg.get("id", ""), title=title,
                        market=market_title, side=side,
                        confidence=confidence, urgency=urgency,
                        size=round(trade_size, 2), reasoning=reasoning,
                    )
            else:
                logger.warning("News scanner: execution failed for '%s': %s",
                               title[:60], result.get("error"))
        except Exception as e:
            logger.error("News scanner: exception executing trade: %s", e)

    def _build_opportunity(self, headline: dict, market: dict, analysis: dict) -> dict:
        """Build an opportunity dict compatible with auto_trader format."""
        yes_price = market.get("yes_price", 0.5)
        side = analysis.get("side", "YES").upper()
        confidence = analysis.get("confidence", 7)

        return {
            "title": headline.get("title", ""),
            "canonical_title": market.get("title", ""),
            "buy_yes_platform": "polymarket",
            "buy_yes_price": yes_price,
            "buy_no_platform": "polymarket",
            "buy_no_price": 1.0 - yes_price,
            "buy_yes_market_id": market.get("condition_id", ""),
            "buy_no_market_id": market.get("condition_id", ""),
            "profit_pct": round((confidence / 10) * 20, 1),  # Estimated based on confidence
            "expiry": "",
            "volume": 0,
            "source": "news_scanner",
            "news_source": headline.get("source", ""),
            "news_category": headline.get("category", ""),
            "news_confidence": confidence,
            "news_side": side,
            "news_reasoning": analysis.get("reasoning", "")[:200],
            # Signal decay: track when signal was created for urgency scoring
            # Research: news signals have minutes-to-hours half-life
            "signal_created_at": time.time(),
        }

    # ── Market Cache ─────────────────────────────────────────────────────────

    async def _refresh_market_cache(self):
        """Fetch top 200 Polymarket markets by volume, cached for 10 minutes."""
        if time.time() - self._market_cache_ts < MARKET_CACHE_TTL:
            return  # Cache is fresh

        if not httpx:
            return

        try:
            http = await self._get_http()
            all_markets = []

            for offset in [0, 100]:
                try:
                    r = await http.get(f"{GAMMA_API}/markets", params={
                        "closed": "false",
                        "limit": "100",
                        "offset": str(offset),
                        "order": "volume",
                        "ascending": "false",
                    })
                    if r.status_code == 200:
                        batch = r.json()
                        if isinstance(batch, list):
                            all_markets.extend(batch)
                except Exception as e:
                    logger.warning("News scanner: market fetch failed at offset %d: %s", offset, e)
                await asyncio.sleep(1)

            # Filter out markets the news scanner can't match (sports, weather, etc.)
            _NOISE_PATTERNS = (
                "temperature", "weather", "nba", "nfl", "mlb", "nhl", "premier league",
                "la liga", "serie a", "bundesliga", "ligue 1", "champions league",
                "eredivisie", "copa", "afl", "ipl", "cricket", "tennis", "golf",
                "set 1", "set 2", "exact score", "o/u", "over/under", "match result",
                "win on 20", "esports", "tsa passengers", "relegated",
                "spread:", "mavericks", "lakers", "celtics", "warriors",
            )

            # Parse into simplified format
            markets = []
            for m in all_markets:
                title = m.get("question", m.get("title", ""))
                condition_id = m.get("conditionId", m.get("id", ""))
                if not title or not condition_id:
                    continue
                # Skip noise markets
                if any(p in title.lower() for p in _NOISE_PATTERNS):
                    continue

                # Parse outcomePrices
                raw_prices = m.get("outcomePrices", "[]")
                if isinstance(raw_prices, str):
                    try:
                        parsed = json.loads(raw_prices)
                    except Exception:
                        parsed = []
                else:
                    parsed = raw_prices

                try:
                    yes_price = float(parsed[0]) if parsed and parsed[0] else 0.5
                except (ValueError, TypeError, IndexError):
                    yes_price = 0.5

                markets.append({
                    "title": title,
                    "yes_price": yes_price,
                    "condition_id": condition_id,
                })

            self._market_cache = markets
            self._market_cache_ts = time.time()
            logger.info("News scanner: cached %d markets from Polymarket", len(markets))

        except Exception as e:
            logger.error("News scanner: market cache refresh failed: %s", e)

    def _find_market(self, market_title: str) -> dict | None:
        """Find a cached market by title (fuzzy — AI may abbreviate)."""
        if not market_title:
            return None
        title_lower = market_title.lower().strip()
        # Exact match first
        for m in self._market_cache:
            if m["title"].lower().strip() == title_lower:
                return m
        # Substring match
        for m in self._market_cache:
            mt = m["title"].lower()
            if title_lower in mt or mt in title_lower:
                return m
        # Word overlap match (>60%)
        title_words = set(title_lower.split())
        best_match = None
        best_overlap = 0.0
        for m in self._market_cache:
            m_words = set(m["title"].lower().split())
            if not m_words or not title_words:
                continue
            overlap = len(title_words & m_words) / min(len(title_words), len(m_words))
            if overlap > best_overlap:
                best_overlap = overlap
                best_match = m
        if best_overlap >= 0.6:
            return best_match
        return None

    # ── Deduplication ────────────────────────────────────────────────────────

    def _fuzzy_duplicate(self, title_words: set, now: float) -> bool:
        """Check if >80% of words overlap with any headline from the last 30 minutes."""
        if not title_words:
            return False

        cutoff = now - FUZZY_DEDUP_WINDOW
        for item in self._recent_headlines:
            if item["ts"] < cutoff:
                continue
            existing_words = item["words"]
            if not existing_words:
                continue
            overlap = len(title_words & existing_words)
            smaller = min(len(title_words), len(existing_words))
            if smaller > 0 and (overlap / smaller) > 0.80:
                return True
        return False

    # ── Safeguards ───────────────────────────────────────────────────────────

    def _check_cooldown(self, market_id: str) -> bool:
        """Returns True if market has a cooldown within the last 15 minutes."""
        ts = self.cooldowns.get(market_id)
        if ts is None:
            return False
        return (time.time() - ts) < COOLDOWN_SECONDS

    def _check_daily_cap(self) -> bool:
        """Returns True if daily trade cap has been reached."""
        today = self._today_str()
        return self.daily_trades.get(today, 0) >= DAILY_TRADE_CAP

    def _check_position_limits(self) -> bool:
        """Returns True if position limits would prevent a new trade."""
        open_pkgs = self.pm.list_packages("open")
        if len(open_pkgs) >= MAX_CONCURRENT:
            return True
        total_exposure = sum(p.get("total_cost", 0) for p in open_pkgs)
        if total_exposure >= MAX_TOTAL_EXPOSURE:
            return True
        return False

    def _has_open_position(self, market_id: str) -> bool:
        """Check if we already hold a position on this market."""
        open_pkgs = self.pm.list_packages("open")
        for pkg in open_pkgs:
            for leg in pkg.get("legs", []):
                if leg.get("status") != "open":
                    continue
                asset_id = leg.get("asset_id", "")
                condition_id = asset_id.split(":")[0] if ":" in asset_id else asset_id
                if condition_id == market_id:
                    return True
        return False

    # ── Persistence ──────────────────────────────────────────────────────────

    def get_recent_headlines(self, condition_id: str, hours: int = 24) -> list[dict]:
        """Return cached headlines matching this market from the last N hours."""
        cutoff = time.time() - (hours * 3600)
        entries = self._matched_headlines.get(condition_id, [])
        return [e for e in entries if e.get("timestamp", 0) > cutoff]

    def _load_cache(self):
        """Load persisted state from news_cache.json."""
        try:
            if os.path.exists(CACHE_FILE):
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._seen_hashes = data.get("seen_hashes", {})
                self.daily_trades = data.get("daily_trades", {})
                self.cooldowns = data.get("cooldowns", {})
                logger.info("News scanner: loaded cache (%d hashes, %d cooldowns)",
                            len(self._seen_hashes), len(self.cooldowns))
        except Exception as e:
            logger.warning("News scanner: failed to load cache: %s", e)

    def _save_cache(self):
        """Persist state to news_cache.json using atomic write."""
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        data = {
            "seen_hashes": self._seen_hashes,
            "daily_trades": self.daily_trades,
            "cooldowns": self.cooldowns,
        }
        tmp_path = CACHE_FILE + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, CACHE_FILE)
        except Exception as e:
            logger.error("News scanner: failed to save cache: %s", e)
            # Clean up temp file on failure
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _prune_state(self):
        """Prune expired hashes, cooldowns, and old daily_trades."""
        now = time.time()
        today = self._today_str()

        # Prune hashes older than 24h
        cutoff_hash = now - HASH_DEDUP_WINDOW
        self._seen_hashes = {h: ts for h, ts in self._seen_hashes.items() if ts > cutoff_hash}

        # Prune recent headlines older than fuzzy window
        cutoff_fuzzy = now - FUZZY_DEDUP_WINDOW
        self._recent_headlines = [item for item in self._recent_headlines if item["ts"] > cutoff_fuzzy]

        # Prune cooldowns older than 15 min
        cutoff_cool = now - COOLDOWN_SECONDS
        self.cooldowns = {mid: ts for mid, ts in self.cooldowns.items() if ts > cutoff_cool}

        # Prune daily_trades older than today
        self.daily_trades = {d: c for d, c in self.daily_trades.items() if d == today}

    @staticmethod
    def _today_str() -> str:
        """UTC date string for today."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── Stats ────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Return scanner statistics."""
        today = self._today_str()
        return {
            "running": self._running,
            "cycles_run": self._cycles_run,
            "headlines_processed": self._headlines_processed,
            "trades_executed": self._trades_executed,
            "daily_trades_today": self.daily_trades.get(today, 0),
            "daily_trade_cap": DAILY_TRADE_CAP,
            "cached_hashes": len(self._seen_hashes),
            "cached_markets": len(self._market_cache),
            "active_cooldowns": sum(1 for ts in self.cooldowns.values()
                                    if (time.time() - ts) < COOLDOWN_SECONDS),
            "scan_interval_sec": self.interval,
        }
