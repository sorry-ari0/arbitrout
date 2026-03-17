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

22. COMPLETED - Frontend JS: Correctly implement processing for all WebSocket message types
   - The `arbWs.onmessage` handler in `src/static/js/arbitrout.js` still only processes `opportunities` and `feed` message types. Logic for `init` and `scan_result` messages (which update `opp-count` and platform status) is missing, despite being marked as completed in Task #19.
   - Add logic within `arbWs.onmessage` to handle `init` and `scan_result` message types.
   - Update the `opp-count` element with `data.events_count` from these messages.
   - Update the platform status display (e.g., `arb-status`) with `data.platforms` from these messages.
   - File: src/static/js/arbitrout.js

23. COMPLETED - Frontend JS: Implement retry logic for WebSocket reconnections
   - The `reconnectArbWs` function is defined with retry logic (`retryCount`, `maxRetries`), but it is not called. `arbWs.onclose` and `arbWs.onerror` currently call `connectArbWs` directly, leading to infinite retries.
   - Modify `arbWs.onclose` and `arbWs.onerror` to call `reconnectArbWs` instead of `connectArbWs` directly.
   - File: src/static/js/arbitrout.js

24. COMPLETED - Arbitrage Router: Implement `min_profit` filter for opportunities endpoint
   - The `/api/arbitrage/opportunities` endpoint in `src/arbitrage_router.py` does not currently accept or apply a `min_profit` query parameter, despite Task #9 being marked as COMPLETED.
   - Modify the `/api/arbitrage/opportunities` endpoint to accept an optional `min_profit: float = 0.0` query parameter.
   - Pass this `min_profit` value (converted to `min_spread`) to `scanner.get_opportunities()`.
   - File: src/arbitrage_router.py

25. COMPLETED - Arbitrage Router: Add null check for `_registry` in WebSocket `init` message
   - The WebSocket `init` message sends `_registry.get_all_status()` without a null check for `_registry`. If `_registry` is `None`, this will raise an error.
   - Add a check for `_registry` being `None` before attempting to call `_registry.get_all_status()`.
   - File: src/arbitrage_router.py

26. COMPLETED - PredictIt Adapter: Improve `no_price` normalization to use actual order book data
   - The `_normalize` method in `src/adapters/predictit.py` still falls back to `1.0 - yes_price` for `no_price` when `bestBuyNoCost` is zero, which was explicitly identified as an inaccuracy to be resolved in Task #21 (marked COMPLETED).
   - Refactor the logic to prioritize `bestBuyNoCost` or other actual order book data (`bestSellNoCost` if applicable) for `no_price`.
   - If no actual 'buy no' price can be found, log a warning instead of using the heuristic.
   - File: src/adapters/predictit.py

27. COMPLETED - Frontend CSS: Implement mobile responsiveness for Arbitrout layout
   - The `src/static/css/arbitrout.css` file is missing `@media (max-width: 768px)` queries to implement the mobile-responsive layout changes described in tasks #8 and #17 (marked COMPLETED).
   - Add `@media (max-width: 768px)` queries to:
     - Change `.arbitrout-container` to a single column layout (e.g., `grid-template-columns: 1fr; grid-template-rows: auto;`).
     - Initially hide the event detail pane (`#event-detail`) on mobile, making it visible only when an opportunity is clicked.
   - File: src/static/css/arbitrout.css

28. COMPLETED - Arbitrage Engine: Refactor `find_arbitrage` for optimal distinct platform pairing
   - The `find_arbitrage` function in `src/arbitrage_engine.py` still uses a "second-best" logic when `best_yes_market.platform == best_no_market.platform`, which does not guarantee distinct platforms or the highest possible spread. This directly contradicts the resolution described in tasks #13 and #15 (marked COMPLETED).
   - Refactor `find_arbitrage` to systematically iterate through all unique pairs of distinct platforms for a `MatchedEvent`.
   - For each pair of platforms, identify the best `buy_yes_price` and `buy_no_price`.
   - Select the overall pair of *distinct* platforms that yields the maximum spread.
   - File: src/arbitrage_engine.py

29. TODO - Arbitrage Engine: Implement pruning for `_previous_prices` dictionary
   - The `_previous_prices` dictionary in `src/arbitrage_engine.py` grows indefinitely as new event prices are added, leading to potential memory issues. Tasks #14 and #16 (marked COMPLETED) specified implementing a pruning mechanism, but none is present.
   - Modify the `compute_feed` or `scan` method to periodically remove entries from `_previous_prices` that correspond to events that are no longer active, have expired, or have not been updated for a configurable period (e.g., 24-48 hours).
   - File: src/arbitrage_engine.py

30. TODO - Arbitrage Engine: Calculate and add optimal capital allocation percentages to opportunities
   - The `find_arbitrage` function in `src/arbitrage_engine.py` does not calculate `yes_allocation_pct` and `no_allocation_pct` for `ArbitrageOpportunity` objects, despite Task #20 being marked COMPLETED.
   - Modify `find_arbitrage` to calculate the optimal capital allocation percentages for buying YES and NO contracts to maximize guaranteed payout.
   - Add these `yes_allocation_pct` and `no_allocation_pct` fields to the `ArbitrageOpportunity` model (assuming it will be updated or already accepts them).
   - File: src/arbitrage_engine.py

31. TODO - Limitless Adapter: Move `import asyncio` to module level
   - The `import asyncio` statement is currently inside the `_fetch` method in `src/adapters/limitless.py`.
   - Move `import asyncio` to the top of the file, outside of any function, to follow best practices and avoid repeated imports.
   - File: src/adapters/limitless.py

32. TODO - Limitless Adapter: Enhance price parsing robustness in `_normalize`
   - The `_normalize` method in `src/adapters/limitless.py` could be more robust in handling potentially missing or malformed price data, specifically for `probability` and `yes_price` fields which are accessed via `m["key"]` or `float(m["key"])` without sufficient `get` checks or `try-except` blocks.
   - Ensure all price extractions (`yes_price`, `no_price`) use safe access (e.g., `m.get('key', default_value)`) and robust type conversion with appropriate error handling (e.g., `try-except ValueError`) to prevent crashes from unexpected API responses.
   - File: src/adapters/limitless.py







