# Arbitrout Tasks
# Status: TODO | IN_PROGRESS | COMPLETED | BLOCKED

## Arbitrage Scanner Improvements

1. BLOCKED - Add retry logic to Polymarket adapter
   - Add exponential backoff retry on 429/500 status codes (3 retries, 2s/4s/8s delays)
   - Wrap the httpx.get call in a retry loop
   - Log warning on retry, error on final failure
   - File: src/adapters/polymarket.py

2. BLOCKED - Add retry logic to PredictIt adapter
   - Add exponential backoff retry on 429/500 status codes (3 retries, 2s/4s/8s delays)
   - Wrap the httpx.get call in a retry loop
   - Log warning on retry, error on final failure
   - File: src/adapters/predictit.py

3. TODO - Add retry logic to Limitless adapter
   - Add exponential backoff retry on 429/500 status codes (3 retries, 2s/4s/8s delays)
   - Wrap the httpx.get call in a retry loop inside the pagination loop
   - Log warning on retry, error on final failure
   - File: src/adapters/limitless.py

4. TODO - Add more stopwords to event matcher
   - Add common prediction market words to _STOPWORDS: market, prediction, contract, shares, event, odds, probability, chance, likelihood, outcome, result, winner, election, vote, poll
   - File: src/event_matcher.py

5. TODO - Add platform status endpoint to API
   - Add GET /api/arbitrage/platforms endpoint returning status of each adapter
   - Include last_fetch_time, event_count, is_healthy, last_error for each platform
   - Store status in a module-level dict updated during scans
   - File: src/arbitrage_router.py

6. TODO - Add profit threshold filter to opportunities endpoint
   - Add optional query param min_profit to GET /api/arbitrage/opportunities
   - Filter results where profit_pct >= min_profit before returning
   - Default to 0 (show all) if not provided
   - File: src/arbitrage_router.py

7. TODO - Add sorting controls to arbitrout frontend
   - Add a dropdown select above the opportunities list with options: Profit High-Low, Profit Low-High, Platform A-Z, Newest First
   - Sort feedItems array based on selection before rendering
   - File: src/static/js/arbitrout.js

8. TODO - Add Bollinger Bands to Lobsterminal chart
   - Calculate 20-period SMA and 2x standard deviation bands
   - Add upper band, lower band, and middle SMA as line series on the chart
   - Use semi-transparent colors so they dont obscure candles
   - File: src/static/js/app.js

9. TODO - Add arbitrage engine unit tests
   - Create tests/test_arbitrage.py with pytest
   - Test that two events with yes=0.40 and no=0.55 produce profit=0.05
   - Test that same-platform pairs are excluded
   - Test trade ratio calculation returns correct percentages
   - File: tests/test_arbitrage.py

10. TODO - Make arbitrout layout responsive on mobile
    - Add media query for max-width 768px
    - Stack the 4-pane grid into single column
    - Hide detail pane on mobile until an opportunity is clicked
    - File: src/static/css/arbitrout.css




