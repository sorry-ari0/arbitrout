# AI News Scanner — Design Spec

**Goal:** Add an AI-powered news scanner that scrapes headlines via RSS, analyzes them with LLMs, and opens prediction market positions when it detects market-moving events before prices fully react.

**Architecture:** Hybrid two-pass pipeline. Background loop fetches RSS feeds every 2-3 minutes, deduplicates headlines, runs a cheap batch LLM scan to find relevant ones, then Scrapling-fetches full articles for high-confidence leads before executing trades. Breaking news executes immediately; normal signals queue for auto trader.

**Tech Stack:** feedparser (RSS), scrapling (article fetch), httpx (LLM calls), existing AI provider chain (Groq/Gemini/OpenRouter)

---

## 1. Overview

The news scanner is a new subsystem that runs alongside the existing auto trader and exit engine. It provides a second source of trading signals — informational edge from breaking news, rather than price/spread analysis.

**Core insight:** When significant news breaks (e.g., "SEC approves Bitcoin ETF"), prediction market prices take minutes to hours to fully adjust. If we detect the news fast and match it to an open market, we can enter before the price moves.

**Interaction with existing system:**
- Shares position manager, decision logger, and position limits with auto trader
- Has its own AI advisor instance with separate API keys and rate limits
- Two execution paths: immediate (breaking) and queued (normal → auto trader)

## 2. New Files

| File | Purpose |
|------|---------|
| `src/positions/news_scanner.py` | RSS fetch loop, headline dedup, orchestrates two-pass pipeline, immediate execution for breaking signals |
| `src/positions/news_ai.py` | LLM prompts for headline batch scan + deep dive analysis, separate API keys and rate limits |
| `src/data/positions/news_cache.json` | Persisted set of seen headline hashes (24h rolling window, survives restarts). Atomic writes via .tmp + os.replace (same pattern as positions.json). Also stores daily trade counter and market cooldown timers. |

## 3. Modified Files

| File | Change |
|------|--------|
| `src/server.py` | Create `NewsScanner` + `NewsAI` in lifespan, pass position_manager and decision_logger, start/stop loop |
| `src/positions/auto_trader.py` | Accept `news_opportunities` via asyncio.Lock-protected queue, apply score boost for news signals |
| `src/positions/position_manager.py` | Add `"news_driven"` to `STRATEGY_TYPES` tuple for separate performance tracking |
| `src/positions/decision_log.py` | Add `log_news_headline()`, `log_news_signal()`, `log_news_trade()` methods |
| `src/.env.example` | Document `NEWS_GROQ_API_KEY`, `NEWS_GEMINI_API_KEY`, `NEWS_OPENROUTER_API_KEY` vars |
| `project.md` | Document news scanner subsystem |

## 4. RSS Feed Sources

Starting set — all free, no API keys:

**Crypto (3):**
- CoinDesk: `https://www.coindesk.com/arc/outboundfeeds/rss/`
- CoinTelegraph: `https://cointelegraph.com/rss`
- The Block: `https://www.theblock.co/rss.xml`

**Macro/Political (3):**
- Reuters World: `https://feeds.reuters.com/reuters/worldNews`
- AP News: `https://rsshub.app/apnews/topics/apf-topnews`
- BBC News: `https://feeds.bbci.co.uk/news/rss.xml`

**Markets/Finance (2):**
- Yahoo Finance: `https://finance.yahoo.com/news/rssindex`
- CNBC: `https://www.cnbc.com/id/100003114/device/rss/rss.html`

Feeds are configurable — a list of `{name, url, category}` dicts. Adding/removing feeds requires no code changes.

## 5. Two-Pass AI Pipeline

### Pass 1: Headline Batch Scan

Every 2-3 minutes:
1. Fetch all RSS feeds concurrently via `feedparser` (run in thread executor — feedparser is sync/blocking)
2. Hash each headline (title + source) against 24h rolling set → filter to new-only. Also apply fuzzy dedup: skip headlines where >80% of tokens overlap with a recently-seen headline (prevents duplicate signals from rewritten wire stories).
3. If no new headlines, skip cycle
4. Fetch available Polymarket markets: reuse a cached list from Gamma API (refreshed every 10 min, top 200 by volume). This is separate from the position manager's open positions — we need the full market universe to match headlines to tradeable markets.
5. Send single LLM call with:
   - New headlines, capped at 30 per batch (title, source, summary snippet)
   - Top 200 Polymarket markets by volume (title + YES price). Estimated prompt size: ~3K tokens.
   - Prompt instruction: for each headline, return one line:
     ```
     <headline_index>: SKIP
     <headline_index>: RELEVANT <market_title> | <side: YES/NO> | <confidence: 1-10> | <urgency: normal/breaking>
     ```
   - Also instruct: "If a headline is a follow-up to a recently-analyzed event, mark urgency as normal regardless of content."

**Cost:** 1 LLM call per cycle regardless of headline count. Most headlines → SKIP.

### Pass 2: Deep Dive

Triggered only when Pass 1 returns confidence >= 7 OR urgency = breaking:

1. First try `httpx` GET for the article URL (5s timeout, cheaper than Scrapling). If response is HTML with sufficient text content (>500 chars after tag stripping), use it. Otherwise fall back to Scrapling `Fetcher` (10s timeout). Scrapling import is optional — if not installed, httpx-only mode.
2. Extract article text, truncate to ~2000 chars
3. Send LLM call with:
   - Full article text
   - Matched market details (current price, volume, expiry, days to expiry)
   - Current portfolio state (total exposure, open positions on this market if any)
   - Prompt instruction:
     ```
     TRADE <side: YES/NO> | <confidence: 1-10> | <reasoning: one sentence>
     NO_TRADE | <reasoning: one sentence>
     ```

**Fallback:** If Scrapling fails (paywall, 403, timeout), use Pass 1 headline-only analysis. Downgrade urgency from breaking to normal to be conservative.

## 6. Execution Routing

```
Pass 2 result:
  ├── NO_TRADE → log to decision_log.jsonl, done
  │
  ├── TRADE + breaking + confidence >= 8
  │       → Check position limits BEFORE executing:
  │           - open_count < MAX_CONCURRENT (10)
  │           - total_exposure + size < MAX_TOTAL_EXPOSURE ($2000)
  │           - cooldown not active for this market (set cooldown BEFORE execution, not after)
  │           - daily_news_trades < 5
  │       → IMMEDIATE execution via PositionManager.execute_package()
  │       → Exit rules: target 15%, stop -10%, trail 8% (tighter — news edge is short-lived)
  │       → Size: confidence/10 * MAX_TRADE_SIZE (e.g., conf 9 → $180)
  │       → Strategy type: "news_driven" (separate tracking in trade journal)
  │
  └── TRADE + normal + confidence >= 7
          → Queue in news_opportunities list (protected by asyncio.Lock)
          → AutoTrader picks up on next 5m cycle
          → News opps get 2x score boost (time-sensitive edge)
          → Standard exit rules: target 25%, stop -20%, trail 12%
          → Strategy type: "news_driven"
```

## 7. AI Provider Configuration (news_ai.py)

Same multi-provider chain as `ai_advisor.py` but with separate API keys:

```
NEWS_GROQ_API_KEY     → falls back to GROQ_API_KEY if not set
NEWS_GEMINI_API_KEY   → falls back to GEMINI_API_KEY if not set
NEWS_OPENROUTER_API_KEY → falls back to OPENROUTER_API_KEY if not set
```

**Rate limits:** Own counter, independent from exit advisor. Default: 10 calls/min.

**Provider priority (paper mode):** Groq → Gemini → OpenRouter (same as exit advisor paper chain).

**Provider priority (live mode):** Anthropic → Groq → Gemini → OpenRouter (same as exit advisor live chain). Uses `NEWS_ANTHROPIC_API_KEY` or falls back to `ANTHROPIC_API_KEY`.

## 8. Safeguards

| Safeguard | Detail |
|-----------|--------|
| **No duplicate markets** | Check open positions before executing. If already holding, skip (log as "reinforcing signal") |
| **News cooldown** | 15-minute cooldown per market. Set BEFORE execution (not after) to prevent race between concurrent headline processing. Stored as `{market_id: timestamp}` dict checked at top of execution path |
| **Daily cap** | Max 5 news-driven trades per day. Persisted in `news_cache.json` as `{"daily_trades": {"2026-03-18": 3}}` using UTC midnight reset |
| **Confidence floor** | Never trade below confidence 7 from Pass 2 |
| **Position limits** | Respects existing MAX_CONCURRENT (10), MAX_TOTAL_EXPOSURE ($2000), MIN_TRADE_SIZE ($5) |
| **Feed failure tolerance** | Individual feed failures don't stop the cycle. All feeds fail for 3 consecutive cycles → warning log |
| **AI failure fallback** | Pass 1 fails → skip cycle (headlines still "unseen" next cycle). Pass 2 fails → downgrade to headline-only, urgency → normal |
| **Scrapling failure** | Falls back to headline-only analysis from Pass 1 |

## 9. Decision Logging

New methods in `decision_log.py`:

- `log_news_headline(title, source, category, action: SKIP|RELEVANT, match_details)` — every headline processed
- `log_news_signal(title, market, side, confidence, urgency, article_fetched: bool, deep_dive_result)` — Pass 2 outcomes
- `log_news_trade(pkg_id, title, market, side, confidence, urgency, size, reasoning)` — executed trades

All news decisions are logged regardless of outcome. This is critical for later review and iteration on:
- Which sources produce actionable signals
- AI accuracy (did the trade work out?)
- False positives / missed opportunities

## 10. Data Flow Diagram

```
NewsScanner (every 2-3 min)
    │
    ├── feedparser → fetch 8 RSS feeds concurrently
    │       │
    │       └── deduplicate (24h rolling hash set, persisted to news_cache.json)
    │               │
    │               └── new headlines (typically 0-5 per cycle)
    │
    ├── Pass 1: NewsAI.scan_headlines(headlines, open_markets)
    │       │
    │       ├── SKIP → log to decision_log.jsonl, done
    │       │
    │       └── RELEVANT → {market, side, confidence, urgency}
    │
    ├── Pass 2 (confidence >= 7 or breaking):
    │       │
    │       ├── Scrapling.fetch(article_url) → extract text (~2000 chars)
    │       │
    │       └── NewsAI.deep_analysis(article, market, portfolio)
    │               │
    │               ├── NO_TRADE → log reasoning, done
    │               │
    │               └── TRADE → {side, confidence, reasoning}
    │
    └── Execute:
            │
            ├── breaking + conf >= 8 → PositionManager.execute_package()
            │       (immediate, tighter exit rules: 15%/-10%/8%)
            │
            └── normal + conf >= 7 → news_opportunities queue
                    │
                    └── AutoTrader picks up on next 5m cycle
                            (2x score boost, standard exit rules)
```

## 11. Server Integration

In `server.py` lifespan:

```python
from positions.news_scanner import NewsScanner
from positions.news_ai import NewsAI

news_ai = NewsAI(paper_mode=is_paper_mode())
news_scanner = NewsScanner(
    position_manager=pm,
    news_ai=news_ai,
    auto_trader=_auto_trader,
    decision_logger=decision_log,
)
news_scanner.start()
```

On shutdown: `news_scanner.stop()`, `news_ai.close()`.

## 12. Auto Trader Integration

`AutoTrader` gets a thread-safe queue for news opportunities:

```python
# Protected by asyncio.Lock — NewsScanner and AutoTrader run as concurrent asyncio tasks
self._news_lock = asyncio.Lock()
self._news_opportunities: list[dict] = []

async def add_news_opportunity(self, opp: dict):
    """Called by NewsScanner to queue a normal-urgency signal."""
    async with self._news_lock:
        self._news_opportunities.append(opp)

async def _drain_news_opportunities(self) -> list[dict]:
    """Called by AutoTrader at start of _scan_and_trade()."""
    async with self._news_lock:
        opps = list(self._news_opportunities)
        self._news_opportunities.clear()
        return opps
```

In `_scan_and_trade()`, after gathering Polymarket scan results:
```python
# Merge news opportunities with scan results
news_opps = await self._drain_news_opportunities()
for news_opp in news_opps:
    news_opp["_score"] *= 2.0  # News edge boost
    opportunities.append(news_opp)
```

News opportunities must include these fields to be compatible with auto trader scoring/execution:
```python
{
    "title": "SEC approves Bitcoin ETF",
    "buy_yes_market_id": "<conditionId>",
    "buy_no_market_id": "<conditionId>",
    "buy_yes_price": 0.45,
    "buy_no_price": 0.55,
    "buy_yes_platform": "polymarket",
    "buy_no_platform": "polymarket",
    "profit_pct": 12.0,
    "expiry": "2026-04-01",
    "days_to_expiry": 14,
    "volume": 150000,
    "conviction": 0.35,
    "_score": 15.0,        # pre-scored by news scanner
    "_news_driven": True,  # flag for strategy type
}
```
