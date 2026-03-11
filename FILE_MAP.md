# Lobsterminal — File Map
# Used by task_dispatcher.py for targeted section editing.
# Updated: 2026-03-11 (MOCK_UNIVERSE split into sub-sections, single-model mode)

## src/server.py (674 lines)
- imports: 1-25
- verify_api_key: 27-37
- _validate_symbol: 40-49
- PositionRequest: 52-55
- config: 57-71
- lifespan: 74-93
- app_setup: 96-113
- get_health: 117-119
- get_ping: 122-124
- get_quotes: 127-145
- _fetch_quotes_sync: 148-189
- get_history: 192-198
- _fetch_history_sync: 201-221
- get_watchlist: 224-226
- add_to_watchlist: 229-236
- delete_watchlist: 239-245
- get_news: 248-261
- _fetch_rss_news: 264-289
- get_positions: 292-295
- add_position: 298-307
- delete_position: 310-316
- ws_prices: 325-359
- static_routes: 362-368
- _generate_mock_history: 380-415
- _atomic_write: 420-432
- _load_watchlist: 435-446
- _load_portfolio: 449-460
- _mock_quote: 463-476
- _mock_news: 479-491

## src/static/js/app.js (2165 lines)
- state: 9-22
- init: 27-43
- startClock: 48-57
- createEl: 62-76
- loadMarketData: 81-92
- renderMarketTable: 94-118
- selectSymbol: 120-130
- updateFooter: 132-142
- loadChart: 147-162
- renderChart: 164-290
- calculateRSI: 295-319
- calculateEMA: 321-329
- calculateMACD: 331-339
- setupPeriodButtons: 341-349
- loadWatchlist: 354-363
- renderWatchlist: 365-396
- setupWatchlistControls: 398-414
- loadNews: 419-429
- renderNews: 431-458
- connectWebSocket: 463-506
- updatePriceTick: 508-533
- setupCommandBar: 538-553
- handleCommand: 555-584
- setupKeyboard: 589-630
- focusPane: 632-637
- navigateList: 639-643
- toggleShortcuts: 645-647
- formatVolume: 652-657
- screenStocks: 662-700
- screener_listeners: 702-707
- deployPortfolio: 712-772
- loadPortfolio: 774-812
- portfolio_listener: 814-819
- runBacktest: 822-865
- backtest_listener: 867
- harvest_handler: 872-897

## src/static/index.html (208 lines)
- head: 1-9
- header: 12-25
- pane_market: 30-51
- pane_chart: 54-70
- pane_watchlist: 73-85
- pane_news: 88-96
- pane_screener: 99-111
- pane_portfolio: 114-128
- footer: 132-139
- shortcuts_overlay: 142-155

## src/swarm_engine.py (868 lines)
- imports: 1-33
- MOCK_UNIVERSE_BIGCAP: 39-96
- MOCK_UNIVERSE_MIDCAP: 97-138
- MOCK_UNIVERSE_SMALLCAP: 139-188
- ScreenRequest: 194-197
- ScreenResponse: 199-206
- SYSTEM_PROMPT: 212-278
- intent_parser: 280-408
- SECTOR_ALIASES: 415-437
- normalize_sector: 439-443
- swarm_evaluator: 449-628
- screen_via_fmp: 637-677
- fetch_fmp_fundamentals: 679-706
- screen_stocks: 708-770

## src/static/css/terminal.css
- Full file (edit entire file — CSS is usually small)
