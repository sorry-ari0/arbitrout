# Arbitrout Tasks
# Status: TODO | IN_PROGRESS | COMPLETED | BLOCKED

## Arbitrage Scanner Improvements

1. TODO - Add retry logic to Polymarket adapter
   - In src/adapters/polymarket.py, in the _fetch method, add `import asyncio` and `import logging` at the top of the file
   - Add `logger = logging.getLogger(__name__)` after the imports
   - Replace the single `resp = await client.get(...)` call (lines 52-60) and `resp.raise_for_status()` with a retry loop:
   - ```python
     for attempt in range(3):
         try:
             resp = await client.get(
                 f"{self.BASE_URL}/markets",
                 params={"closed": "false", "limit": 100, "order": "volume", "ascending": "false"},
             )
             resp.raise_for_status()
             break
         except Exception as e:
             if attempt < 2:
                 delay = [2, 4, 8][attempt]
                 logger.warning(f"Polymarket attempt {attempt+1} failed: {e}, retrying in {delay}s")
                 await asyncio.sleep(delay)
             else:
                 logger.error(f"Polymarket failed after 3 attempts: {e}")
                 return []
     ```
   - File: src/adapters/polymarket.py

2. TODO - Add retry logic to PredictIt adapter
   - In src/adapters/predictit.py, add `import asyncio` and `import logging` at the top of the file
   - Add `logger = logging.getLogger(__name__)` after the imports
   - Replace `resp = await client.get(self.BASE_URL)` and `resp.raise_for_status()` (lines 40-41) with a retry loop:
   - ```python
     for attempt in range(3):
         try:
             resp = await client.get(self.BASE_URL)
             resp.raise_for_status()
             break
         except Exception as e:
             if attempt < 2:
                 delay = [2, 4, 8][attempt]
                 logger.warning(f"PredictIt attempt {attempt+1} failed: {e}, retrying in {delay}s")
                 await asyncio.sleep(delay)
             else:
                 logger.error(f"PredictIt failed after 3 attempts: {e}")
                 return []
     ```
   - File: src/adapters/predictit.py

3. TODO - Add retry logic to Limitless adapter
   - In src/adapters/limitless.py, add `import logging` at the top of the file (asyncio is already imported inside the method)
   - Add `logger = logging.getLogger(__name__)` after the imports
   - Replace the `resp = await client.get(...)` and `resp.raise_for_status()` calls (lines 47-51) inside the pagination loop with a retry loop:
   - ```python
             for attempt in range(3):
                 try:
                     resp = await client.get(
                         f"{self.BASE_URL}/markets/active",
                         params={"limit": 25, "page": page},
                     )
                     resp.raise_for_status()
                     break
                 except Exception as e:
                     if attempt < 2:
                         delay = [2, 4, 8][attempt]
                         logger.warning(f"Limitless page {page} attempt {attempt+1} failed: {e}, retrying in {delay}s")
                         await asyncio.sleep(delay)
                     else:
                         logger.error(f"Limitless page {page} failed after 3 attempts: {e}")
                         break
     ```
   - File: src/adapters/limitless.py

4. TODO - Add profit threshold filter to opportunities endpoint
   - In src/arbitrage_router.py, add an optional query param min_profit to GET /api/arbitrage/opportunities
   - Filter results where profit_pct >= min_profit before returning
   - Default to 0 (show all) if not provided
   - File: src/arbitrage_router.py

5. TODO - Add sorting controls to arbitrout frontend
   - Add a dropdown select above the opportunities list with options: Profit High-Low, Profit Low-High, Platform A-Z, Newest First
   - Sort feedItems array based on selection before rendering
   - File: src/static/js/arbitrout.js

6. TODO - Add Bollinger Bands to Lobsterminal chart
   - Calculate 20-period SMA and 2x standard deviation bands
   - Add upper band, lower band, and middle SMA as line series on the chart
   - Use semi-transparent colors so they dont obscure candles
   - File: src/static/js/app.js

7. TODO - Add arbitrage engine unit tests
   - Create tests/test_arbitrage.py with pytest
   - Test that two events with yes=0.40 and no=0.55 produce profit=0.05
   - Test that same-platform pairs are excluded
   - Test trade ratio calculation returns correct percentages
   - File: tests/test_arbitrage.py

8. TODO - Make arbitrout layout responsive on mobile
   - Add media query for max-width 768px
   - Stack the 4-pane grid into single column
   - Hide detail pane on mobile until an opportunity is clicked
   - File: src/static/css/arbitrout.css
