# Arbitrout + Lobsterminal — File Map
# Updated: 2026-03-19 (added all new modules: executors, positions, news scanner)

## SUBSYSTEM 1: LOBSTERMINAL (Stock Terminal)

### src/server.py (1288 lines)
- imports: 1-37
- verify_api_key: 92-102
- _validate_symbol: 104-110
- PositionRequest: 112-130
- _auto_scan_loop: 134-153
- lifespan: 155-420
- health/ping/get_quotes: 425-455
- _fetch_quotes_sync/_yahoo_chart/_finnhub: 457-609
- get_history: 611-617
- _fetch_history_sync/_yahoo_chart/_finnhub: 619-743
- _save_history_cache/_load_history_cache: 745-769
- watchlist endpoints: 771-793
- get_news/_fetch_rss_news: 795-865
- portfolio endpoints: 867-893
- dexter endpoints: 895-1023
- ws_prices: 1025-1064
- static_routes: 1066-1077
- utility functions: 1079-1268
- arbitrage subsystem wiring: 1268-1288

### src/static/js/app.js (2306 lines)
- state/init: 18-58
- market rendering: 60-162
- universe browser: 163-299
- chart rendering: 301-493
- watchlist: 508-571
- news: 573-615
- websocket: 617-690
- command bar: 692-812
- screener: 831-972
- strategy builder: 1005-1275
- portfolio management: 1297-1705
- dexter analysis: 1707-2064
- financials/segments: 2066-2259
- backtest: 2261-2306

### src/swarm_engine.py (1394 lines)
- MOCK_UNIVERSE: 43-430
- intent_parser/LLM calls: 450-788
- swarm_evaluator/screening: 790-1394

### src/backtest_engine.py (558 lines)
- Data fetchers (Yahoo/Finnhub/Alpha Vantage/Scrapling): 97-383
- Metrics calculation: 385-484
- Backtest router: 490-558

## SUBSYSTEM 2: ARBITROUT (Prediction Market Arbitrage)

### src/static/js/arbitrout.js (966 lines)
- WebSocket handler (init/scan_result/opportunities/feed types)
- Opportunity rendering, feed display, saved markets

### src/event_matcher.py (648 lines)
- Entity extraction: 76-206 (crypto, names, countries, acronyms, key_terms)
- Two-phase matching: 212-327 (quick filter + overlap scoring)
- PredictIt title handling: 333-341
- Union-Find clustering + mega-cluster splitting: 456-538
- Manual link API: 591-611

### src/arbitrage_engine.py (674 lines)
- _markets_have_same_target: 27-52
- _build_range_synthetic_info: 55-195 (4-scenario analysis for range markets)
- _build_synthetic_info: 198-383 (3-scenario for directional synthetics)
- find_arbitrage: 386-477
- Saved markets / feed computation: 480-553
- ArbitrageScanner class: 558-627

### src/arbitrage_router.py (194 lines)
- WebSocket broadcast, REST endpoints for scan/opportunities/events/feed

### src/adapters/ (platform connectors)
- models.py (112): NormalizedEvent, MatchedEvent, ArbitrageOpportunity
- registry.py (73): AdapterRegistry.fetch_all()
- polymarket.py (140): Gamma API + CLOB
- predictit.py (89): REST API, multi-contract markets
- kalshi.py (120): REST API (requires API key)
- limitless.py (136): Paginated REST API
- coinbase.py (126): Scrapling + Kalshi fallback
- robinhood.py (115): Scrapling scraper
- opinion_labs.py (124): REST API (requires API key)
- crypto_spot.py (130): CoinGecko implied-probability events
- commodities.py (137): Commodity event generation

## SUBSYSTEM 3: POSITION MANAGEMENT

### src/positions/auto_trader.py (907 lines)
- Position limits, scoring (crypto/expiry/insider/favorite-longshot bias)
- Event-driven loop (asyncio.Event from arb scanner)
- News opportunity queue, Polymarket fallback scan
- MIN_SPREAD_PCT=5%, ROUND_TRIP_FEE_PCT=2%, MAX_CONCURRENT=7

### src/positions/exit_engine.py (586 lines)
- 20 heuristic triggers (profit, loss, spread, time, volatility, platform, research)
- Batched AI review (single LLM call for all packages per tick)
- Safety overrides bypass AI, cooldown per trigger type

### src/positions/ai_advisor.py (431 lines)
- Multi-provider LLM chain: Groq → Gemini → OpenRouter → (Anthropic for live)
- Batched prompt builder + parser for multiple packages
- Conservative exit guidance (no premature time_decay approvals)

### src/positions/position_manager.py (369 lines)
- Package CRUD, leg management, P&L tracking, alerts

### src/positions/position_router.py (386 lines)
- REST API for positions, packages, legs, auto-trade config

### src/positions/insider_tracker.py (514 lines)
- Polymarket top trader monitoring (Data API leaderboard)
- Wallet accuracy scoring, position tracking, signal generation

### src/positions/news_scanner.py (818 lines)
- RSS feed scanner (14 feeds), market matching, signal generation
- Cooldown system, hash dedup, breaking news fast-track

### src/positions/news_ai.py (526 lines)
- LLM headline analysis (Groq → Gemini → OpenRouter chain)
- Market impact scoring, relevance detection

### src/positions/trade_journal.py (215 lines)
- P&L recording, strategy performance tracking

### src/positions/decision_log.py (187 lines)
- Structured logging for all trading decisions (scans, skips, exits)

### src/positions/wallet_config.py (128 lines)
- Platform wallet addresses, API key management

## SUBSYSTEM 4: EXECUTION

### src/execution/base_executor.py (40 lines)
- Abstract interface: buy/sell/get_balance/get_current_price/get_positions

### src/execution/paper_executor.py (151 lines)
- In-memory paper trading with simulated fills

### src/execution/polymarket_executor.py (376 lines)
- CLOB API: condition→token resolution, midpoint pricing, order placement

### src/execution/kalshi_executor.py (66 lines)
- REST API with RSA key auth (requires KALSHI_API_KEY + KALSHI_RSA_PRIVATE_KEY)

### src/execution/predictit_executor.py (66 lines)
- Session auth, 850-share cap per contract

### src/execution/coinbase_spot_executor.py (69 lines)
- Coinbase Advanced Trade API (requires COINBASE_ADV_API_KEY)

### src/execution/crypto_spot_executor.py (368 lines)
- CoinGecko price feeds, implied-probability event generation

### src/execution/kraken_cli.py (279 lines)
- WSL Kraken CLI wrapper for spot crypto trades

### src/execution/limitless_executor.py (72 lines)
- Limitless exchange order placement

### src/execution/opinion_labs_executor.py (82 lines)
- Opinion Labs API executor

### src/execution/robinhood_executor.py (58 lines)
- Robinhood prediction market executor

## STUBS (Not Yet Implemented)

### src/cross_asset_matcher.py (24 lines)
- STUB: Returns empty list. Task 40.

### src/theta_scanner.py (25 lines)
- STUB: Returns empty list. Task 39.

## OTHER

### src/portfolio_manager.py (942 lines)
- APScheduler: daily TLH sweep, quarterly rebalance

### src/strategy_engine.py (1501 lines)
- Strategy templates, risk models

### src/valuation_engine.py (484 lines)
- DCF, fundamental score

### src/dexter_client.py (322 lines)
- FMP API client

### src/fmp_client.py (329 lines)
- Financial Modeling Prep API

### src/indicators.py (76 lines)
- RSI, EMA, MACD calculations

### src/static/index.html (266 lines)
- Combined Lobsterminal + Arbitrout dashboard
