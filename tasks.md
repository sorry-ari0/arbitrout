# Arbitrout Tasks
# Status: TODO | IN_PROGRESS | COMPLETED | BLOCKED

## Arbitrage Scanner Improvements

1. COMPLETED - Add retry logic to Polymarket adapter
   - Add exponential backoff retry on 429/500 status codes (3 retries, 2s/4s/8s delays)
   - Wrap the httpx.get call in a retry loop
   - Log warning on retry, error on final failure
   - File: src/adapters/polymarket.py

2. COMPLETED - Add retry logic to PredictIt adapter
   - Add exponential backbackoff retry on 429/500 status codes (3 retries, 2s/4s/8s delays)
   - Wrap the httpx.get call in a retry loop
   - Log warning on retry, error on final failure
   - File: src/adapters/predictit.py

3. COMPLETED - Add retry logic to Limitless adapter
   - Add exponential backoff retry on 429/500 status codes (3 retries, 2s/4s/8s delays)
   - Wrap the httpx.get call in a retry loop inside the pagination loop
   - Log warning on retry, error on final failure
   - File: src/adapters/limitless.py

4. COMPLETED - Add profit threshold filter to opportunities endpoint
   - Add optional query param min_profit to GET /api/arbitrage/opportunities
   - Filter results where profit_pct >= min_profit before returning
   - Default to 0 (show all) if not provided
   - File: src/arbitrage_router.py

5. COMPLETED - Add sorting controls to arbitrout frontend
   - Add a dropdown select above the opportunities list with options: Profit High-Low, Profit Low-High, Platform A-Z, Newest First
   - Sort feedItems array based on selection before rendering
   - File: src/static/js/arbitrout.js

6. COMPLETED - Add Bollinger Bands to Lobsterminal chart
   - Calculate 20-period SMA and 2x standard deviation bands
   - Add upper band, lower band, and middle SMA as line series on the chart
   - Use semi-transparent colors so they dont obscure candles
   - File: src/static/js/app.js

7. BLOCKED - Add arbitrage engine unit tests
   - Create tests/test_arbitrage.py with pytest
   - Test that two events with yes=0.40 and no=0.55 produce profit=0.05
   - Test that same-platform pairs are excluded
   - Test trade ratio calculation returns correct percentages
   - File: tests/test_arbitrage.py

8. COMPLETED - Make arbitrout layout responsive on mobile
   - Add media query for max-width 768px
   - Stack the 4-pane grid into single column
   - Hide detail pane on mobile until an opportunity is clicked
   - File: src/static/css/arbitrout.css

9. COMPLETED - Arbitrage Router: Implement `min_profit` filter in opportunities endpoint
   - The `get_opportunities` endpoint in `src/arbitrage_router.py` does not currently accept or apply a `min_profit` query parameter, despite the underlying `find_arbitrage` function supporting `min_spread`. This prevents users from filtering opportunities by a minimum profit percentage.
   - Modify the `/api/arbitrage/opportunities` endpoint to accept an optional `min_profit` query parameter (e.g., `min_profit: float = 0.0`).
   - Pass the received `min_profit` value (converted to `min_spread`) to `scanner.get_opportunities()`, which will then need to accept this parameter.
   - File: src/arbitrage_router.py

10. COMPLETED - Frontend CSS: Implement mobile responsiveness for Arbitrout layout
   - Task #8, "Make arbitrout layout responsive on mobile," is marked COMPLETED, but `src/static/css/arbitrout.css` currently lacks the necessary `@media` queries to stack the 4-pane grid into a single column or hide the detail pane on mobile until an opportunity is clicked.
   - Add `@media (max-width: 768px)` queries to implement the specified responsive layout changes.
   - File: src/static/css/arbitrout.css

11. COMPLETED - Frontend JS: Add sorting controls to Arbitrout opportunities list
   - Task #5, "Add sorting controls to arbitrout frontend," is marked COMPLETED, but `src/static/js/arbitrout.js` does not implement a dropdown select or logic to sort the opportunities list by criteria like Profit High-Low, Profit Low-High, Platform A-Z, or Newest First. The current display relies solely on the backend's default sorting.
   - Add a dropdown element to the UI to select sorting preferences.
   - Implement client-side sorting logic within `renderOpportunities` based on the selected criteria.
   - File: src/static/js/arbitrout.js

12. COMPLETED - Frontend JS: Enhance WebSocket client to process all server-sent data
   - The `arbWs.onmessage` handler in `src/static/js/arbitrout.js` only explicitly processes `opportunities` and `feed` message types. It does not handle the `init` message (which provides initial `events_count` and `platforms` status) or the `scan_result` message (which provides `summary` and updated `opportunities`/`feed` after a manual scan) for updating the UI elements like opportunity count and platform status.
   - Add logic within `arbWs.onmessage` to handle `init` and `scan_result` message types, updating relevant UI components (e.g., `opp-count`, platform status) with the received data.
   - File: src/static/js/arbitrout.js

13. COMPLETED - Arbitrage Engine: Refactor arbitrage calculation for distinct platforms
   - The `find_arbitrage` function in `src/arbitrage_engine.py` attempts to correct for same-platform `best_yes` and `best_no` markets by finding "second-best" options. However, this logic is flawed and may still result in the selected `buy_yes_platform` and `buy_no_platform` being the same, or it might not consistently find the optimal cross-platform arbitrage. A more robust approach is needed to guarantee distinct platforms for the buy-yes and buy-no sides and maximize the spread.
   - Refactor the logic in `find_arbitrage` to systematically iterate through pairs of distinct platforms to ensure that `buy_yes_platform` and `buy_no_platform` are always different for an arbitrage opportunity, finding the highest possible spread.
   - File: src/arbitrage_engine.py

14. COMPLETED - Arbitrage Engine: Prune `_previous_prices` to prevent unbounded growth
   - The `_previous_prices` dictionary in `src/arbitrage_engine.py` is used to track historical prices for `compute_feed`. This dictionary is never explicitly pruned, which means it will continue to grow indefinitely as new events are encountered, potentially leading to unbounded memory consumption over long periods.
   - Implement a mechanism to periodically prune `_previous_prices`, for example, by removing entries for events that are no longer active or have not been updated for a very long time.
   - File: src/arbitrage_engine.py

15. COMPLETED - Arbitrage Engine: Fix `find_arbitrage` to guarantee distinct platforms and optimal spread.
   - The current logic attempts a "second-best" fix if the initially chosen best YES and NO markets are on the same platform, but this does not guarantee distinct platforms for the final `buy_yes_market` and `buy_no_market`, nor does it ensure the highest possible spread between *any* two distinct platforms.
   - Refactor `find_arbitrage` to systematically iterate through all unique pairs of platforms for a `MatchedEvent`, identify the best `buy_yes_price` and `buy_no_price` for each platform, and then select the pair of *distinct* platforms that yields the maximum spread.
   - File: src/arbitrage_engine.py

16. COMPLETED - Arbitrage Engine: Implement pruning for `_previous_prices` dictionary.
   - The `_previous_prices` dictionary in `src/arbitrage_engine.py` grows indefinitely, leading to potential memory issues over time.
   - Modify the `compute_feed` or `scan` method to periodically remove entries from `_previous_prices` that correspond to events that are no longer active, have expired, or have not been updated for a configurable period (e.g., 24-48 hours).
   - File: src/arbitrage_engine.py

17. COMPLETED - Frontend CSS: Implement mobile responsiveness for Arbitrout layout.
   - The `src/static/css/arbitrout.css` file currently lacks `@media` queries to adapt the layout for mobile screens.
   - Add `@media (max-width: 768px)` queries to:
     - Change `.arbitrout-container` to a single column layout (e.g., `grid-template-columns: 1fr; grid-template-rows: auto;`).
     - Initially hide the event detail pane (`#event-detail`) on mobile, making it visible only when an opportunity is clicked.
   - File: src/static/css/arbitrout.css

18. BLOCKED - Frontend JS: Add sorting controls and logic to Arbitrout opportunities list.
   - The UI is missing a dropdown to select sorting preferences for the arbitrage opportunities. The `renderOpportunities` function also lacks the logic to apply sorting client-side.
   - Add a dropdown UI element (e.g., `<select>`) in the opportunities panel header.
   - Implement client-side sorting logic within `renderOpportunities` or a helper function, allowing users to sort by criteria such as "Profit High-Low", "Profit Low-High", "Platform A-Z", and "Newest First" (using `matched_event.last_updated` or similar).
   - File: src/static/js/arbitrout.js

19. COMPLETED - Frontend JS: Enhance WebSocket client to process all server-sent data.
   - The `arbWs.onmessage` handler only processes `opportunities` and `feed` messages, ignoring `init` and `scan_result` messages that provide `events_count`, `platforms`, and `summary`.
   - Modify `arbWs.onmessage` to handle `init` and `scan_result` message types.
   - Update the `opp-count` element with `data.events_count` (from `init` or `scan_result`).
   - Update the platform status display (e.g., `arb-status`) with `data.platforms` (from `init` or `scan_result`).
   - File: src/static/js/arbitrout.js

20. COMPLETED - Arbitrage Engine: Calculate optimal capital allocation for arbitrage opportunities.
   - The frontend currently defaults to 50/50 capital allocation, which is not optimal for maximizing guaranteed profit. The backend should calculate and provide `yes_allocation_pct` and `no_allocation_pct`.
   - Modify the `find_arbitrage` function to calculate the optimal capital allocation percentages for buying YES and NO contracts, considering their respective prices, to guarantee a fixed payout.
   - Add these `yes_allocation_pct` and `no_allocation_pct` fields to the `ArbitrageOpportunity` model (assuming it exists or would be created).
   - File: src/arbitrage_engine.py

21. COMPLETED - PredictIt Adapter: Improve price normalization for NO side using actual order book data.
   - The `PredictItAdapter` sometimes derives the `no_price` as `1.0 - yes_price` when `bestBuyNoCost` is zero. This is a heuristic and not an actual order book price, which can lead to inaccuracies in arbitrage calculations.
   - Adjust the `_normalize` method to preferentially use `bestBuyNoCost` for the `no_price` (representing the cost to buy NO shares). If `bestBuyNoCost` is unavailable or zero, consider if `bestSellNoCost` (price to sell NO shares) might be relevant if used consistently with `bestBuyYesCost` as bid/ask pairs, or log a warning if actual 'buy no' price cannot be found.
   - File: src/adapters/predictit.py

## Stock Analysis & Portfolio Research (Bloomberg Terminal)

22. BLOCKED - Add scrapling-based company research module
   - Create a new module `src/research/company_researcher.py` that uses the scrapling library (already installed v0.4.1)
   - Implement `research_company(ticker: str) -> dict` that scrapes Wikipedia for: CEO name, founders, founding year, headquarters, industry, key investors, board members, recent controversies
   - Use the existing `_COMPANY_NAMES` mapping in swarm_engine.py (maps ~50 tickers to Wikipedia article titles) as a starting point, but also support looking up unknown tickers by searching Wikipedia for "{company_name} company"
   - Cache results in a local JSON file `data/company_research_cache.json` to avoid re-scraping
   - Add a `research_batch(tickers: list) -> list[dict]` function that processes multiple tickers with 1-2 second delays between requests
   - File: src/research/company_researcher.py (new)

23. COMPLETED - Expand stock universe to full NASDAQ and NYSE listings
   - The current swarm_engine.py MOCK_UNIVERSE has only 103 hardcoded tickers with synthetic fundamentals
   - Create `src/research/stock_universe.py` that downloads full ticker lists from public sources:
     - NASDAQ: use the NASDAQ FTP file at `ftp.nasdaqtrader.com/symboldirectory/nasdaqtraded.txt` or the SEC EDGAR company tickers JSON at `https://www.sec.gov/files/company_tickers.json`
     - NYSE: included in the same SEC EDGAR file (covers all US exchanges)
   - Parse into a list of dicts with: ticker, company_name, exchange (NASDAQ/NYSE/AMEX), market_cap_tier (large/mid/small/micro)
   - Store in `data/us_stock_universe.json` and refresh weekly
   - Add a `get_universe(exchange=None, cap_tier=None) -> list` function that filters by exchange and market cap tier
   - Modify `swarm_engine.py` to use this universe instead of MOCK_UNIVERSE when the data file exists, falling back to MOCK_UNIVERSE if not
   - File: src/research/stock_universe.py (new), src/swarm_engine.py

24. BLOCKED - Add Hong Kong Stock Exchange (HKEX) listings to universe
   - Extend `src/research/stock_universe.py` to include HKEX stocks
   - Scrape HKEX stock list from `https://www.hkex.com.hk/Market-Data/Securities-Prices/Equities` or use the HKEX API
   - Hong Kong tickers use 4-digit codes (e.g., 0700.HK for Tencent, 9988.HK for Alibaba HK)
   - Add exchange="HKEX" support to `get_universe()` filter
   - Include at minimum the Hang Seng Index constituents (~80 stocks) and Hang Seng Composite (~500 stocks)
   - Map HKEX tickers to company names for Wikipedia research lookups
   - File: src/research/stock_universe.py

25. COMPLETED - Add CEO/founder/investor detail endpoint to Bloomberg Terminal API
   - Add GET `/api/research/company/{ticker}` endpoint that returns detailed company research
   - Call `company_researcher.research_company(ticker)` and return the scraped data as JSON
   - Include fields: ceo, founders (list), key_investors (list), founding_year, headquarters, industry, board_members (list), wikipedia_url
   - Add GET `/api/research/batch` endpoint that accepts `?tickers=AAPL,MSFT,GOOGL` and returns research for multiple companies
   - Add the research data to the swarm_engine screening results so when a user says "tech companies with female CEOs" the unresolved criteria can be checked against actual scraped data
   - File: src/server.py, src/research/company_researcher.py

26. BLOCKED - Integrate scrapling research into portfolio prompt screening
   - When swarm_engine.py parses a prompt and gets `unresolved` criteria (e.g., "companies founded by immigrants", "CEOs with engineering backgrounds", "backed by Sequoia Capital"), it currently ignores them
   - After the initial fundamentals screening, for each passing ticker call `company_researcher.research_company()` to get qualitative data
   - Use a simple keyword/substring match against the research data to filter for unresolved criteria
   - Example: prompt "tech stocks with founder-led companies" -> screen fundamentals -> for each result, check if CEO name appears in founders list
   - Log which unresolved criteria could and could not be verified
   - File: src/swarm_engine.py, src/research/company_researcher.py

## Commodity, Crypto & Prediction Market Arbitrage

27. COMPLETED - Add commodity market adapter for arbitrage scanning
   - Create `src/adapters/commodities.py` that fetches commodity prices
   - Use a free API like Metals API, Open Exchange Rates, or scrape from TradingView/Yahoo Finance for: gold (XAU), silver (XAG), crude oil (WTI, Brent), natural gas, copper, corn, wheat, soybeans
   - Normalize to the same `NormalizedEvent` format used by other adapters with: event_name (e.g., "Gold Price > $2500 by Dec 2026"), platform ("Commodities"), yes_price, no_price
   - The adapter should compare current spot prices against prediction market questions about commodity prices to find arbitrage between real commodity futures and prediction market contracts
   - Register the adapter in server.py alongside existing adapters
   - File: src/adapters/commodities.py (new), src/server.py

28. BLOCKED - Add crypto spot price adapter for cross-platform arbitrage
   - Create `src/adapters/crypto_spot.py` that fetches real-time crypto prices from multiple exchanges
   - Use free APIs: CoinGecko (no key needed) for BTC, ETH, SOL, DOGE, XRP, ADA, AVAX, LINK, DOT, MATIC prices across exchanges
   - Normalize into `NormalizedEvent` format: compare prediction market contracts about crypto prices (e.g., "BTC > $100k by July") against actual spot prices and implied probabilities from options/futures
   - Calculate implied probability from current price vs strike: if BTC is at $95k and prediction market says "BTC > $100k" at $0.40, compare against historical volatility to find mispriced contracts
   - Register in server.py
   - File: src/adapters/crypto_spot.py (new), src/server.py

29. COMPLETED - Add theta decay detection for prediction markets near expiry
   - Add a `theta_scanner` module `src/theta_scanner.py` that identifies prediction market contracts approaching expiry where theta (time decay) creates arbitrage opportunities
   - For each prediction market event, check if `end_date` or `close_date` is within 7 days
   - Calculate implied probability vs current price: if an event is 95% likely to resolve YES (based on current real-world data) but the YES contract trades at $0.80, thats a $0.15 edge
   - Flag "in the money" contracts trading below fair value near expiry (high-confidence free money)
   - Flag "out of the money" contracts still trading above $0.05 near expiry (sell opportunity)
   - Add a `/api/arbitrage/theta` endpoint that returns theta opportunities sorted by days_to_expiry and edge_pct
   - File: src/theta_scanner.py (new), src/arbitrage_router.py

30. COMPLETED - Add prediction-to-real-asset arbitrage matching
   - Create `src/cross_asset_matcher.py` that finds combinations where prediction market contracts can be hedged with real tradeable assets
   - Example: Polymarket has "BTC > $100k by July" at $0.40 -> buy YES at $0.40 + short BTC futures at $100k strike = guaranteed profit if spread exceeds transaction costs
   - Example: Kalshi has "S&P 500 above 5500 by Q3" at $0.55 -> buy YES + buy SPY puts at 5500 strike = hedged position
   - Match prediction market events against: crypto prices (via Coinbase adapter), stock prices (via Robinhood adapter), commodity prices (via commodities adapter)
   - Calculate the net cost of the hedged position and the guaranteed profit/loss
   - Add `/api/arbitrage/cross-asset` endpoint returning matched opportunities with hedge instructions
   - File: src/cross_asset_matcher.py (new), src/arbitrage_router.py

31. BLOCKED - Research best arbitrage strategies before implementation
   - Create `src/research/arbitrage_strategies.py` that uses scrapling to research and document the best approaches for:
     - Prediction market arbitrage (academic papers, blog posts from experienced traders)
     - Crypto spot vs prediction market hedging
     - Commodity futures vs prediction market contracts
     - Theta decay harvesting strategies
     - Kelly criterion for optimal bet sizing
   - Scrape key sources: Wikipedia articles on "Arbitrage", "Prediction market", "Theta (finance)", "Kelly criterion"
   - Scrape trading strategy blogs: e.g., Polymarket strategy guides, Kalshi trading tips
   - Store findings in `data/strategy_research.json` with: strategy_name, description, expected_edge_pct, risk_factors, sources
   - This research should inform the implementation of tasks 27-30 above
   - File: src/research/arbitrage_strategies.py (new)


























