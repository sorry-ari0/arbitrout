# Arbitrout Tasks
# Status: TODO | IN_PROGRESS | COMPLETED | BLOCKED

## Arbitrage Scanner Improvements

1. TODO — Add error handling and retry logic to all adapters
   - Polymarket, PredictIt, Limitless adapters should retry on 429/500 with exponential backoff
   - Log failures without crashing the scan loop
   - File: src/adapters/*.py

2. TODO — Add Kalshi API adapter (requires API key setup)
   - Implement _fetch() with proper auth headers
   - Normalize events to NormalizedEvent format
   - File: src/adapters/kalshi.py

3. TODO — Improve event matching accuracy
   - Add more stopwords to _STOPWORDS set
   - Handle event title variations (e.g. "Will X win" vs "X to win")
   - Add unit tests for matching edge cases
   - File: src/event_matcher.py

4. TODO — Add historical arbitrage tracking
   - Store found opportunities with timestamps in a JSON file
   - Track when opportunities appear and disappear
   - Show historical profit if trades were taken
   - Files: src/arbitrage_router.py, src/static/js/arbitrout.js

5. TODO — Add notification system for high-profit opportunities
   - When profit > 3%, log prominently and flash in UI
   - Add sound notification option
   - File: src/static/js/arbitrout.js, src/arbitrage_router.py

6. TODO — Add platform status indicators to UI
   - Show which platforms are responding vs erroring
   - Display last successful fetch time per platform
   - Show event count per platform
   - Files: src/arbitrage_router.py, src/static/js/arbitrout.js

7. TODO — Add sorting and filtering to opportunities list
   - Sort by profit %, platform, category
   - Filter by minimum profit threshold
   - Filter by category (politics, sports, crypto, etc.)
   - File: src/static/js/arbitrout.js

8. TODO — Improve Lobsterminal chart indicators
   - Add Bollinger Bands overlay option
   - Add volume bars below chart
   - File: src/static/js/app.js

9. TODO — Add tests for arbitrage engine
   - Test spread calculation with known prices
   - Test YES/NO cross-platform detection
   - Test trade ratio calculation
   - File: tests/test_arbitrage.py (new)

10. TODO — Mobile responsive layout for Arbitrout
    - Make 4-pane grid collapse to single column on mobile
    - Swipeable tabs for scanner/saved/detail views
    - File: src/static/css/arbitrout.css
