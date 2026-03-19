# Lobsterminal — File Map
# Used by task_dispatcher.py for targeted section editing.
# Updated: 2026-03-18 (data fallback chains, universe browser, full stock universe)

## src/server.py (1268 lines)
- imports: 1-37
- verify_api_key: 92-102
- _validate_symbol: 104-110
- PositionRequest: 112-130
- _auto_scan_loop: 134-153
- lifespan: 155-420
- health: 425-430
- ping: 432-436
- get_quotes: 437-455
- _fetch_quotes_sync: 457-526
- _fetch_quotes_yahoo_chart: 528-570
- _fetch_quotes_finnhub: 572-609
- get_history: 611-617
- _fetch_history_sync: 619-673
- _fetch_history_yahoo_chart: 675-710
- _fetch_history_finnhub: 712-743
- _save_history_cache: 745-753
- _load_history_cache: 755-769
- get_watchlist: 771-773
- add_to_watchlist: 776-784
- remove_from_watchlist: 786-793
- get_news: 795-835
- _fetch_rss_news: 837-865
- get_portfolio: 867-870
- add_position: 872-882
- remove_position: 884-893
- dexter_ratios: 895-907
- dexter_financials: 909-929
- dexter_insider_trades: 931-941
- dexter_analyst_estimates: 943-953
- dexter_news: 955-965
- dexter_filings: 967-977
- dexter_segments: 979-989
- dexter_dcf: 991-1003
- dexter_score: 1005-1023
- ws_prices: 1025-1064
- static_routes: 1066-1077
- _generate_mock_history: 1079-1117
- _atomic_write: 1119-1132
- _load_watchlist: 1134-1142
- _save_watchlist: 1144-1146
- _load_portfolio: 1148-1156
- _save_portfolio: 1158-1160
- _fetch_quotes_scrapling: 1162-1233
- _mock_quote: 1235-1249
- _mock_news: 1251-1268

## src/static/js/app.js (2306 lines)
- state: 18-30
- init: 40-58
- startClock: 60-72
- createEl: 74-90
- loadMarketData: 93-106
- renderMarketTable: 108-132
- selectSymbol: 134-146
- updateFooter: 148-162
- universeState: 163-172
- setupUniverseControls: 174-215
- switchMarketMode: 217-236
- loadUniversePage: 238-299
- loadChart: 301-316
- renderChart: 318-447
- calculateRSI: 449-473
- calculateEMA: 475-483
- calculateMACD: 485-493
- setupPeriodButtons: 495-506
- loadWatchlist: 508-517
- renderWatchlist: 519-550
- setupWatchlistControls: 552-571
- loadNews: 573-583
- renderNews: 585-615
- connectWebSocket: 617-660
- updatePriceTick: 662-690
- setupCommandBar: 692-707
- handleCommand: 709-756
- setupKeyboard: 758-799
- focusPane: 801-806
- navigateList: 808-812
- toggleShortcuts: 814-819
- formatVolume: 821-829
- screenStocks: 831-922
- loadStrategyTemplates: 924-948
- loadRatioExplanations: 951-972
- showRatioTooltip: 974-1003
- createStrategy: 1005-1173
- loadMetricPicker: 1175-1221
- addFilter: 1223-1275
- getActiveFilters: 1277-1295
- getUserId: 1297-1302
- loadPortfolios: 1304-1319
- renderPortfolioSelector: 1321-1333
- renderActivePortfolio: 1335-1450
- savePortfolioWeights: 1452-1487
- removeTickerFromPortfolio: 1489-1506
- createNewPortfolio: 1508-1532
- deleteActivePortfolio: 1534-1547
- deployActivePortfolio: 1549-1580
- addTickersToPortfolio: 1582-1602
- getSelectedScreenerTickers: 1604-1607
- showAddToPortfolioMenu: 1609-1705
- loadDexterRatios: 1707-1739
- loadInsiderTrades: 1741-1785
- loadAnalystEstimates: 1787-1827
- loadSecFilings: 1829-1869
- runDCF: 1871-1913
- showDCFDetail: 1915-1988
- loadFundScore: 1990-2014
- showScoreDetail: 2016-2064
- loadFinancials: 2066-2175
- loadSegments: 2177-2259
- runBacktest: 2261-2306

## src/static/js/arbitrout.js (959 lines)
- Full file (arbitrage dashboard frontend — event matching by event_id)

## src/static/index.html (266 lines)
- head: 1-9
- header: 12-25
- pane_market: 30-60 (includes universe controls bar)
- pane_chart: 63-79
- pane_watchlist: 82-94
- pane_news: 97-105
- pane_screener: 108-120
- pane_portfolio: 123-137
- footer: 141-148
- shortcuts_overlay: 151-164

## src/swarm_engine.py (1394 lines)
- imports: 1-42
- MOCK_UNIVERSE: 43-430
- ScreenRequest: 431-435
- ScreenResponse: 437-445
- SYSTEM_PROMPT: 450-520
- _parse_llm_json: 521-550
- _call_ollama: 552-571
- _call_groq: 573-597
- _call_gemini: 599-618
- _regex_intent_parser: 620-698
- intent_parser: 700-788
- SECTOR_ALIASES: 790-798
- _normalize_sector: 800-810
- swarm_evaluator: 812-986
- _screen_via_fmp: 988-1028
- _fetch_fmp_fundamentals: 1030-1067
- _load_full_universe_cache: 1069-1087
- _trigger_universe_fetch: 1089-1098
- _fetch_universe_fundamentals_bg: 1100-1154
- _save_universe_cache: 1156-1174
- get_universe_status: 1176-1198
- trigger_universe_refresh: 1200-1211
- screen_stocks: 1213-1394

## src/backtest_engine.py (558 lines)
- imports: 1-23
- constants: 25-32
- BacktestRequest: 38-51
- BacktestMetrics: 53-69
- BacktestResponse: 71-78
- _period_to_days: 85-95
- _yf_download: 97-128
- _yahoo_chart_api: 130-181
- _finnhub_candles: 183-225
- _alpha_vantage_daily: 227-275
- _scrapling_yahoo_history: 277-323
- fetch_historical_data: 325-383
- calculate_metrics: 385-446
- calculate_asset_score: 448-484
- router: 490
- run_backtest: 499-558

## src/event_matcher.py (608 lines)
- Entity extraction, two-phase matching, Union-Find clustering
- Phase 2.5 mega-cluster splitting for crypto price divergence

## src/arbitrage_engine.py (380 lines)
- Cross-platform spread detection, price feed computation
- Passes buy_yes_event_id and buy_no_event_id through to ArbitrageOpportunity

## src/adapters/models.py (112 lines)
- NormalizedEvent: 10-31
- MatchedEvent: 38-60
- ArbitrageOpportunity: 67-112 (includes buy_yes_event_id, buy_no_event_id, is_synthetic, synthetic_info)

## src/static/css/terminal.css
- Full file (edit entire file — CSS is usually small)
