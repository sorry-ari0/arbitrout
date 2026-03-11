# Arbitrout — Prediction Market Arbitrage Scanner

> **For agentic workers:** Use superpowers:executing-plans to implement this spec.

**Goal:** Scan 7 prediction market platforms for arbitrage opportunities and display them in a real-time dashboard alongside the existing Lobsterminal.

**Architecture:** Single FastAPI app serving two modes (Lobsterminal + Arbitrout) via a top-level tab switcher. Scrapling scrapes odds from all platforms, a matcher groups identical events, and an arbitrage scanner finds profitable spreads. WebSocket pushes live updates to the frontend.

**Tech Stack:** Python 3.11+, FastAPI, Scrapling, TradingView Lightweight Charts, vanilla JS, CSS Grid

---

## Platforms

| Platform | URL | Scrape Target | Notes |
|----------|-----|---------------|-------|
| Kalshi | kalshi.com | Market listing pages | Regulated US exchange |
| Polymarket | polymarket.com | Market pages | Crypto-settled |
| PredictIt | predictit.org | Market browse | Political focus |
| Robinhood Predictions | robinhood.com | Prediction markets section | May need auth |
| Coinbase | coinbase.com | Prediction markets | Newer feature |
| Limitless | limitless.exchange | Market pages | Newer platform |
| Opinion Labs | opinionlabs.io | Market pages | TBD — stub if unavailable |

All platforms scraped via Scrapling (JS rendering, anti-bot). No API keys needed for read-only phase.

---

## Data Model

### NormalizedEvent
```python
@dataclass
class NormalizedEvent:
    platform: str           # "kalshi", "polymarket", etc.
    event_id: str           # platform-specific ID
    title: str              # "Will Bitcoin exceed $100K by Dec 2026?"
    category: str           # crypto, politics, sports, economics, weather, culture
    yes_price: float        # 0.0 - 1.0
    no_price: float         # 0.0 - 1.0
    volume: int             # trading volume
    expiry: str             # ISO date or "ongoing"
    url: str                # direct link to market on platform
    last_updated: str       # ISO datetime
```

### MatchedEvent
```python
@dataclass
class MatchedEvent:
    canonical_title: str
    category: str
    expiry: str
    markets: list[NormalizedEvent]  # same event across platforms
    match_type: str                 # "auto" or "manual"
```

### ArbitrageOpportunity
```python
@dataclass
class ArbitrageOpportunity:
    matched_event: MatchedEvent
    buy_yes_platform: str       # platform with cheapest YES
    buy_no_platform: str        # platform with cheapest NO
    yes_price: float
    no_price: float
    spread: float               # 1.0 - (yes + no) = profit per $1
    profit_pct: float           # spread as percentage
    combined_volume: int
```

---

## Architecture

### Backend Modules

```
src/
├── server.py                    # FastAPI app (existing + Arbitrout routes)
├── arbitrage_engine.py          # Scanner, matcher, opportunity calculator
├── adapters/
│   ├── __init__.py
│   ├── base.py                  # BaseAdapter ABC
│   ├── kalshi.py
│   ├── polymarket.py
│   ├── predictit.py
│   ├── robinhood.py
│   ├── coinbase.py
│   ├── limitless.py
│   └── opinion_labs.py
├── swarm_engine.py              # (existing) Stock screener
├── backtest_engine.py           # (existing) Backtesting
├── portfolio_manager.py         # (existing) Portfolio management
├── strategy_engine.py           # (existing) Strategy templates
└── static/
    ├── index.html               # Updated: tab switcher + Arbitrout layout
    ├── js/
    │   ├── app.js               # (existing) Lobsterminal frontend
    │   └── arbitrout.js         # Arbitrout frontend
    ├── css/
    │   ├── terminal.css         # (existing) Lobsterminal styles
    │   └── arbitrout.css        # Arbitrout styles (teal accent)
    └── img/
        ├── lobster.svg          # (existing)
        └── trout.png            # 64x64 pixel art trout
```

### Data Flow

```
Scrapling Adapters (7x) → NormalizedEvents
    ↓
Event Matcher (fuzzy title + category + expiry)
    ↓
MatchedEvents (grouped cross-platform)
    ↓
Arbitrage Scanner (yes_A + no_B < 1.0?)
    ↓
ArbitrageOpportunities (sorted by profit %)
    ↓
WebSocket → Arbitrout Frontend
```

### Polling Strategy

When Arbitrout tab is active:
- All 7 platforms scraped simultaneously every 10-15 seconds
- Each adapter runs in its own asyncio task
- Results cached; if a scrape fails, last cached result used
- WebSocket pushes diffs (new opportunities, price changes)

When Arbitrout tab is inactive:
- Polling stops entirely (saves resources)

### API Endpoints (New)

```
GET  /api/arbitrage/opportunities     # Current arbitrage opportunities
GET  /api/arbitrage/events            # All matched events
GET  /api/arbitrage/feed              # Recent odds changes
GET  /api/arbitrage/platforms         # Platform status (up/down/blocked)
POST /api/arbitrage/link              # Manually link two markets
DELETE /api/arbitrage/link/{id}       # Remove manual link
GET  /api/arbitrage/saved             # Bookmarked markets
POST /api/arbitrage/saved             # Bookmark a market
WS   /ws/arbitrage                    # Real-time odds + opportunity stream
```

---

## Frontend

### Tab Switcher
- Top bar above terminal grid: **LOBSTERMINAL** | **ARBITROUT**
- Active tab: colored underline (orange for Lobsterminal, teal for Arbitrout)
- Switching shows/hides the corresponding layout
- Splash screen: 64x64 pixel art (lobster or trout) flashes for ~1.5s on switch

### Arbitrout Layout (4-pane grid)

```
┌─────────────────────┬──────────────────────┐
│                     │                      │
│   OPPORTUNITIES     │   EVENT DETAIL       │
│   (sorted by %)     │   (prices per        │
│                     │    platform)          │
│                     │                      │
├─────────────────────┼──────────────────────┤
│                     │                      │
│   MARKET FEED       │   SAVED MARKETS      │
│   (live odds        │   (bookmarked        │
│    changes)         │    events)           │
│                     │                      │
└─────────────────────┴──────────────────────┘
```

### Color Theme (Arbitrout)
- Same dark background: #0a0a14
- Accent: #00e5cc (teal) instead of #ff8c00 (orange)
- Positive spread: #00e676 (green)
- Negative/risky: #ff1744 (red)

---

## Event Matching

### Auto-Match Algorithm
1. Normalize titles: lowercase, strip "Will ", "What ", platform prefixes
2. TF-IDF vectorize all event titles
3. Cosine similarity between cross-platform pairs (threshold ≥ 0.75)
4. Secondary validation: category match + expiry within 7 days
5. Group into MatchedEvent objects

### Manual Linking
- User searches events across platforms
- Links two+ events as the same market
- Stored in `data/arbitrage/manual_links.json`
- Manual links override auto-match (higher confidence)

---

## Arbitrage Calculation

For each MatchedEvent with markets on ≥2 platforms:

```python
best_yes = min(m.yes_price for m in markets)
best_no = min(m.no_price for m in markets)
spread = 1.0 - (best_yes + best_no)

if spread > 0:
    # Arbitrage exists!
    profit_pct = spread * 100
    # Buy YES on cheapest-yes platform
    # Buy NO on cheapest-no platform
```

Display: sorted by profit_pct descending, filtered by minimum volume threshold.

---

## Storage

```
data/arbitrage/
├── manual_links.json      # User-defined event links
├── cache.json             # Last scraped odds (offline viewing)
├── saved_markets.json     # Bookmarked events
└── credentials.json       # (future) Encrypted platform creds
```

---

## Future: Execution Phase

Not in scope for v1, but the architecture supports:
- Secure credential storage (encrypted at rest, decrypted in memory)
- Payment flow integration (platform-specific deposit/withdrawal)
- One-click arbitrage execution (buy YES on A, buy NO on B simultaneously)
- Position tracking across platforms
- P&L dashboard

---

## Pixel Art: Trout Splash

Simple 64x64 static pixel art of a trout character:
- Trout sitting at a desk
- Looking at a small computer/phone screen
- Teal/cyan color palette
- Displays for ~1.5 seconds when switching to Arbitrout mode
- CSS fade-out transition
