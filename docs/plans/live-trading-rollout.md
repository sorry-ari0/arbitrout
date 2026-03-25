# Live Trading Rollout Plan

**Created:** 2026-03-24
**Status:** Phase 0 — Pre-flight (no funds)

---

## Phase 0 — Pre-flight (No Funds Required)

Make everything production-ready while still in paper mode.

### 0.1 Fix Kalshi Executor Auth ✅ (2026-03-24)
- [x] Replace Bearer token with RSA-PSS request signing via `kalshi-python` SDK
- [x] SDK uses cent-based prices (1-99) — mapped to decimal in executor
- [x] Added limit order support (`buy_limit`, `sell_limit`)
- [x] Added `check_order_status`, `cancel_order`, `get_positions`
- [ ] Test against `demo-api.kalshi.co` sandbox (needs API key)
- **File:** `src/execution/kalshi_executor.py`

### 0.2 Fix Coinbase Executor Auth ✅ (2026-03-24)
- [x] Replaced raw httpx headers with `coinbase-advanced-py` SDK
- [x] Proper EC key authentication via `RESTClient`
- [x] Balance, positions, price endpoints implemented
- [ ] Test with real API keys (needs `COINBASE_ADV_API_KEY` + `COINBASE_ADV_API_SECRET`)
- **File:** `src/execution/coinbase_spot_executor.py`

### 0.3 GTC Fill Monitoring ✅ (already implemented)
- [x] Exit engine calls `_resolve_pending_limit_orders()` every scan cycle
- [x] `position_manager.resolve_pending_order()` polls executor `check_order_status`
- [x] Paper executor simulates GTC fills when market >= limit price
- **File:** `src/positions/exit_engine.py:544`, `src/positions/position_manager.py`

### 0.4 Emergency Kill Switch ✅ (2026-03-24)
- [x] `POST /api/derivatives/kill-switch` — nuclear option (cancel all + close all + stop trading)
- [x] `POST /api/derivatives/kill-switch/pause` — stop auto-trader, keep positions
- [x] `POST /api/derivatives/kill-switch/resume` — restart auto-trader
- [x] Tested: pause/resume confirmed working
- **File:** `src/positions/position_router.py`

### 0.5 Wallet Balance Monitoring ✅ (2026-03-24)
- [x] `GET /api/derivatives/wallet-health` — per-platform balance + status
- [x] Reports configured/unconfigured, balance, paper_mode vs live
- [x] Tested: returns balances for all 6 paper executors
- [ ] Add background polling with alerts when balance < threshold (enhancement)
- **File:** `src/positions/position_router.py`

### 0.6 Update Fee Curves ✅ (2026-03-24)
- [x] Paper executor: updated from 2-category to 11-category Polymarket fee structure
- [x] Categories: crypto, sports, politics, finance, tech, entertainment, science, culture, climate, geopolitical (0%), other
- [x] Kalshi fees updated to 0% (current Kalshi fee structure)
- [x] Polymarket SDK handles fee rates automatically for live GTC orders (0% maker)
- **File:** `src/execution/paper_executor.py`

### 0.7 Slippage Protection ✅ (2026-03-24)
- [x] Pre-trade price check in `position_manager._execute_package_locked()`
- [x] Rejects if price moved >5% from expected (configurable via `MAX_SLIPPAGE_PCT` env var)
- [x] Polymarket executor already uses GTC maker orders at spread edge (inherent slippage control)
- **File:** `src/positions/position_manager.py`

### 0.8 Server Startup Bugs Fixed (2026-03-24)
- [x] `AdapterRegistry.register()` → auto-registers in `__init__` (removed manual calls)
- [x] `AdapterRegistry.list_platforms()` → `len(arb_registry._adapters)`
- [x] `AdapterRegistry.get("kalshi")` → search `_adapters` by `PLATFORM_NAME`
- **File:** `src/server.py`

---

## Phase 1 — Polymarket Live (Lowest Friction)

Prerequisites: Phase 0 complete, wallet funded.

### 1.1 Wallet Setup
- [ ] Create or import Polygon EOA wallet
- [ ] Export hex private key to `.env` as `POLYMARKET_PRIVATE_KEY`
- [ ] Set `POLYMARKET_FUNDER_ADDRESS` in `.env`
- [ ] Fund wallet: Coinbase → buy USDC → withdraw on Polygon network
- [ ] Get 0.5-1 MATIC for gas

### 1.2 Token Approvals (One-Time)
- [ ] Approve USDC.e for CTF Exchange (`0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`)
- [ ] Approve USDC.e for Neg Risk Exchange (`0xC5d563A36AE78145C45a50134d48A1215220f80a`)
- [ ] Approve USDC.e for Neg Risk Adapter (`0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`)
- [ ] Approve CTF tokens for all 3 exchange contracts
- [ ] Total cost: ~$0.30 gas

### 1.3 Parallel Testing
- [ ] Run paper + live executors side-by-side for 1 week
- [ ] Compare fill rates, execution quality, price accuracy
- [ ] Log actual vs simulated fees

### 1.4 Go Live
- [ ] Set `PAPER_TRADING=false`
- [ ] Start with $50-100 positions
- [ ] Monitor first 10 trades manually
- [ ] Scale up as confidence grows

---

## Phase 2 — Add Kalshi

Prerequisites: Phase 1 stable for 2+ weeks.

### 2.1 Production Auth
- [ ] Generate RSA key pair in Kalshi account settings
- [ ] Store private key securely (encrypted or env var)
- [ ] Validate against production API

### 2.2 Demo Testing
- [ ] Full order lifecycle on `demo-api.kalshi.co`
- [ ] Verify: place, check status, cancel, settlement

### 2.3 Rate Limit Tier
- [ ] Apply for Advanced tier (30 writes/sec) via Kalshi Typeform
- [ ] Basic tier (10 writes/sec) is fine for initial trading

### 2.4 Go Live
- [ ] 0% trading fees, fiat USD settlement
- [ ] Cross-platform arb opportunities between Polymarket + Kalshi

---

## Phase 3 — Coinbase USDC Pipeline

Prerequisites: Phase 1 or 2 live.

### 3.1 SDK Integration
- [ ] `coinbase-advanced-py` with EC key auth
- [ ] USDC buy/sell/transfer endpoints working

### 3.2 Automated Funding
- [ ] Fiat → USDC → Polygon withdrawal → Polymarket wallet
- [ ] Auto-sweep profits above threshold to cold storage

### 3.3 Multi-Exchange Hedging
- [ ] Crypto spot hedging via CCXT (Kraken/Coinbase)
- [ ] Synthetic derivative execution on real positions

---

## Reference

- **Research doc:** `docs/research/live-execution-guide.md` (30+ sources, code patterns, contract addresses)
- **Executor files:** `src/execution/` (11 executor files)
- **Wallet config:** `src/positions/wallet_config.py`
- **Current mode:** Paper trading (`PAPER_TRADING=true`)
- **Polymarket contract addresses:** See research doc section 1.6
- **Kalshi demo endpoint:** `https://demo-api.kalshi.co/trade-api/v2`
