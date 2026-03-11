# Project: Lobsterminal
Status: ACTIVE
Phase: BUILD
Last Updated: 2026-03-11
Current Task: Autonomous dispatcher running — processing MOCK_UNIVERSE enrichment tasks (60-78)
Iteration: 4

## Architecture
- **Backend:** Python FastAPI server (src/server.py) on port 8500
- **Frontend:** Vanilla HTML/CSS/JS with TradingView Lightweight Charts
- **Data:** yfinance for quotes/history, RSS feeds for news (Yahoo Finance + Google News), WebSocket for real-time price streaming
- **Storage:** Local JSON files (watchlist.json, portfolio.json, portfolios.json) in src/data/
- **Style:** Lobsterminal-dark theme — #0a0a14 bg, #ff8c00 accent, monospace fonts, 6-pane grid layout
- **AI:** Ollama local models — qwen2.5-coder:7b (single-model mode for coding via Aider)
- **GPU:** Intel Arc 140V (~7GB VRAM) — one model at a time, single-model mode only

## Modules
| File | Lines | Purpose |
|------|-------|---------|
| server.py | 674 | FastAPI backend — quotes, history, watchlist, news (RSS), positions, WebSocket |
| swarm_engine.py | 868 | AI stock screener — Ollama intent parsing + mock universe filtering |
| backtest_engine.py | 324 | Portfolio backtesting — yfinance data + metrics (Sharpe, drawdown, score) |
| portfolio_manager.py | 641 | Direct indexing — deploy, tax-loss harvest, rebalance, APScheduler |
| app.js | 2165 | Frontend — market data, chart, RSI/MACD overlays, watchlist, news, screener, portfolio, backtest, keyboard |
| index.html | 208 | 6-pane Lobsterminal layout |
| terminal.css | ~200 | Dark trading theme |

## Agent Pipeline
| Agent | Model | Role |
|-------|-------|------|
| main (PM) | llama-agent:latest | Reads tasks, spawns subagents, reviews results |
| coder | qwen2.5-coder:7b | Runs task_dispatcher.py → Aider edits sections |
| auditor | llama-agent:latest | Reviews diffs for bugs (static + model review) |
| tester | llama-agent:latest | Hits API endpoints, verifies server works |
| researcher | llama-agent:latest | Investigates issues, reads files, finds root causes |

## Task Pipeline
```
tasks.md → task_dispatcher.py → Aider (section-based) → code_scanner.py → model audit → curl tests → mark done
                                      ↓                       ↓                  ↓
                                 FILE_MAP.md              scan_patterns.json   audit-log.md
```

**Autonomous loop:** `run-dispatcher-loop.ps1` (every 30min) or manual `python -u task_dispatcher.py`

## Completed Features (24 tasks done, 17 queued)
- [x] 6-pane grid layout (market, chart, watchlist, news, screener, portfolio)
- [x] Real-time quotes via yfinance with 15s cache + retry logic
- [x] Candlestick chart with period selector (1D-1Y)
- [x] RSI(14) indicator overlay (Wilder's smoothing)
- [x] MACD(12,26,9) with signal line and histogram
- [x] Watchlist with add/remove and JSON persistence
- [x] News feed via Yahoo Finance RSS + Google News RSS (fallback mock)
- [x] WebSocket real-time price streaming with exponential backoff
- [x] AI stock screener (Ollama LLM → structured rules → universe filter)
- [x] Portfolio deployment (equal-weight fractional shares)
- [x] Tax-loss harvesting with wash sale tracking
- [x] Backtesting engine (Sharpe ratio, max drawdown, asset score)
- [x] Command bar (/help, /add, /remove, /chart, /screen, /backtest)
- [x] Keyboard shortcuts (1-6 panes, j/k, Esc, ?, /)
- [x] Loading states and error display in all panes
- [x] Security scanning (36 patterns + multiline + obfuscation detection)
- [x] Scrapling research pass for qualitative screening criteria
- [x] Financial ratio explanations (RATIO_EXPLANATIONS dict)
- [x] Structured screening endpoint (bypass LLM with direct rules)
- [x] Metric picker UI (HTML + JavaScript filter builder)
- [x] pe_ratio + roe added to BIGCAP stocks (Task 60)
- [x] pe_ratio + roe added to MIDCAP stocks (Task 61)
- [x] pe_ratio + roe added to SMALLCAP stocks (Task 62)

## GPU Constraints
- Intel Arc 140V: ~7GB VRAM, one 8B model at a time
- **Single-model mode only** — architect mode (2 models) exceeds VRAM, causes extreme slowness
- qwen2.5-coder:7b: 6.5 tok/s gen (Aider --edit-format whole)
- Section-based editing: Aider processes ~40-57 lines per sub-section (BIGCAP/MIDCAP/SMALLCAP)
- Base timeout: 900s (15 min), extended to 1200s for sections >80 lines

## Known Limitations
- yfinance rate limits: retries with 2s delay, falls back to mock data
- Screener uses mock universe (86+ stocks with synthetic fundamentals, being enriched)
- Portfolio uses in-memory mock DB (resets on server restart, saved to JSON)
- Aider with 7B model uses --edit-format whole (can't produce reliable diffs)
- Aider summarizer fails after model shutdown (cosmetic, doesn't affect edits)
