# Project: Arbitrout (Prediction Market Arbitrage Scanner)
Status: ACTIVE
Phase: BUILD
Last Updated: 2026-03-16
Repo: https://github.com/sorry-ari0/arbitrout.git
Branch: main

## Overview
Arbitrout scans prediction markets (Polymarket, PredictIt, Limitless, Kalshi) for cross-platform arbitrage opportunities. It detects price discrepancies where buying YES on one platform and NO on another yields guaranteed profit. Integrated into the Lobsterminal financial terminal as a switchable tab.

## Architecture
- **Backend:** Python FastAPI server (src/server.py) on port 8501
- **Frontend:** Vanilla HTML/CSS/JS — Lobsterminal + Arbitrout tabs
- **Adapters:** src/adapters/ — one per platform, all extend BaseAdapter
- **Matching:** src/event_matcher.py — fuzzy entity matching (70% fuzzy + 30% entity ratio, threshold 0.72)
- **Engine:** src/arbitrage_engine.py — finds spreads where yes + no < 1.0
- **Router:** src/arbitrage_router.py — FastAPI router at /api/arbitrage/
- **Auto-scan:** Background task scans every 60s, JS polls every 15s
- **AI:** Ollama local models for Lobsterminal features (screener, swarm)
- **GPU:** Intel Arc 140V (~7GB VRAM) — one 8B model at a time

## Git Workflow (MANDATORY)
1. ALWAYS create a feature branch: `git checkout -b feature/task-description`
2. Make changes and commit with descriptive messages
3. Push branch: `git push -u origin feature/task-description`
4. Create PR: `gh pr create --title "..." --body "..."`
5. NEVER push directly to main
6. NEVER modify any Smalltalk-Fam repos or other projects
7. Update project.md after completing tasks

## Platform Status
| Platform | Status | Events |
|----------|--------|--------|
| Polymarket | Working | ~100 |
| PredictIt | Working | ~852 |
| Limitless | Working | ~200 |
| Kalshi | Needs API key | 0 |
| Robinhood | Scraping returns 0 | 0 |
| Coinbase | Scraping returns 0 | 0 |
| Opinion Labs | Not implemented | 0 |

## Key Files
| File | Lines | Purpose |
|------|-------|---------|
| src/server.py | 731 | FastAPI backend + auto-scan loop |
| src/arbitrage_engine.py | 213 | Cross-platform spread detection |
| src/arbitrage_router.py | 193 | API routes + WebSocket |
| src/event_matcher.py | 286 | Fuzzy entity matching |
| src/adapters/models.py | 102 | NormalizedEvent, ArbitrageOpportunity |
| src/adapters/polymarket.py | 140 | Polymarket CLOB API |
| src/adapters/predictit.py | 89 | PredictIt API |
| src/adapters/limitless.py | 136 | Limitless Exchange API |
| src/static/js/arbitrout.js | 533 | Arbitrout frontend |
| src/static/js/app.js | 2165 | Lobsterminal frontend |
| src/static/css/arbitrout.css | 328 | Arbitrout styles |

## Task Management
Tasks are in `tasks.md`. Read ONLY the task you're working on — don't load the full file.
When stuck on a task for more than 2 attempts:
1. Log the error to `.learnings/` folder
2. Mark task as BLOCKED with reason
3. Move to the next TODO task
4. Come back to BLOCKED tasks later with fresh context

## Completed Features
- [x] Multi-platform event fetching (Polymarket, PredictIt, Limitless)
- [x] Entity-based fuzzy matching across platforms
- [x] YES/NO cross-platform arbitrage detection
- [x] Trade ratio calculation (allocation percentages)
- [x] Auto-scan background loop (60s interval)
- [x] Real-time JS polling (15s) + WebSocket
- [x] Bookmark/save opportunities
- [x] Lobsterminal tab switching with splash screens
- [x] Pixel art trout mascot with grey wire phone
