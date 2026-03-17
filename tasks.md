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

13. TODO - Arbitrage Engine: Refactor arbitrage calculation for distinct platforms
   - The `find_arbitrage` function in `src/arbitrage_engine.py` attempts to correct for same-platform `best_yes` and `best_no` markets by finding "second-best" options. However, this logic is flawed and may still result in the selected `buy_yes_platform` and `buy_no_platform` being the same, or it might not consistently find the optimal cross-platform arbitrage. A more robust approach is needed to guarantee distinct platforms for the buy-yes and buy-no sides and maximize the spread.
   - Refactor the logic in `find_arbitrage` to systematically iterate through pairs of distinct platforms to ensure that `buy_yes_platform` and `buy_no_platform` are always different for an arbitrage opportunity, finding the highest possible spread.
   - File: src/arbitrage_engine.py

14. TODO - Arbitrage Engine: Prune `_previous_prices` to prevent unbounded growth
   - The `_previous_prices` dictionary in `src/arbitrage_engine.py` is used to track historical prices for `compute_feed`. This dictionary is never explicitly pruned, which means it will continue to grow indefinitely as new events are encountered, potentially leading to unbounded memory consumption over long periods.
   - Implement a mechanism to periodically prune `_previous_prices`, for example, by removing entries for events that are no longer active or have not been updated for a very long time.
   - File: src/arbitrage_engine.py




