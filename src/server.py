"""Lobsterminal — Backend Server"""
import asyncio
import json
import logging
import os

# Load .env file if present (keys for trading platforms, AI, etc.)
try:
    from positions.wallet_config import load_env_file
    load_env_file()
except ImportError:
    _env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(_env_file):
        with open(_env_file) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())
import random
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
import yfinance as yf
import httpx
import feedparser

logger = logging.getLogger(__name__)

# --- Arbitrage imports ---
try:
    from arbitrage_router import router as arbitrage_router, init_scanner, get_scanner, broadcast_update
    from adapters.registry import AdapterRegistry
    from adapters.kalshi import KalshiAdapter
    from adapters.polymarket import PolymarketAdapter
    from adapters.predictit import PredictItAdapter
    from adapters.limitless import LimitlessAdapter
    from adapters.opinion_labs import OpinionLabsAdapter
    from adapters.robinhood import RobinhoodAdapter
    from adapters.coinbase import CoinbaseAdapter
    from adapters.crypto_spot import CryptoSpotAdapter
    _ARBITRAGE_AVAILABLE = True
except (ImportError, SyntaxError) as _arb_err:
    logger.warning("Arbitrage modules not available: %s", _arb_err)
    _ARBITRAGE_AVAILABLE = False

# --- Position system imports ---
try:
    from positions.position_router import router as position_router, init_position_system
    from positions.position_manager import PositionManager
    from positions.exit_engine import ExitEngine
    from positions.ai_advisor import AIAdvisor
    from positions.wallet_config import is_paper_mode, get_paper_balance, get_configured_platforms
    from execution.base_executor import BaseExecutor
    from execution.paper_executor import PaperExecutor
    from execution.polymarket_executor import PolymarketExecutor
    from execution.kalshi_executor import KalshiExecutor
    from execution.coinbase_spot_executor import CoinbaseSpotExecutor
    from execution.predictit_executor import PredictItExecutor
    from execution.limitless_executor import LimitlessExecutor
    from execution.opinion_labs_executor import OpinionLabsExecutor
    from execution.robinhood_executor import RobinhoodExecutor
    from execution.crypto_spot_executor import CryptoSpotExecutor
    from execution.kraken_cli import KrakenCLIExecutor
    from positions.trade_journal import TradeJournal
    from positions.auto_trader import AutoTrader
    from positions.probability_model import ProbabilityModel
    from positions.insider_tracker import InsiderTracker
    from positions.decision_log import DecisionLogger
    from positions.news_scanner import NewsScanner
    from positions.news_ai import NewsAI
    _POSITIONS_AVAILABLE = True
except (ImportError, SyntaxError) as _pos_err:
    logger.warning("Position system not available: %s", _pos_err)
    _POSITIONS_AVAILABLE = False

# --- Political synthetic analysis ---
try:
    from political.analyzer import PoliticalAnalyzer
    from political.router import router as political_router, init_political_router
    _POLITICAL_AVAILABLE = True
except (ImportError, SyntaxError) as _pol_err:
    logger.warning("Political analysis not available: %s", _pol_err)
    _POLITICAL_AVAILABLE = False

# --- Eval logger ---
try:
    from eval_logger import EvalLogger
    from eval_router import router as eval_router_mod, init_eval_router
    _EVAL_AVAILABLE = True
except (ImportError, SyntaxError) as _eval_err:
    logger.warning("Eval logger not available: %s", _eval_err)
    _EVAL_AVAILABLE = False

# --- C1 fix: API Key Authentication ---
API_KEY = os.environ.get("LOBSTERMINAL_API_KEY", "dev-local-only")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str = Security(api_key_header)):
    """Verify API key for protected endpoints. Skip auth for localhost dev."""
    if API_KEY == "dev-local-only":
        return  # No auth in dev mode
    if not api_key or api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# --- C2 fix: Symbol validation ---
_SYMBOL_RE = re.compile(r'^[A-Z]{1,5}(\.[A-Z]{1,4})?$')


def _validate_symbol(symbol: str) -> str:
    """Validate and normalize a ticker symbol. Supports US tickers and .HK suffix."""
    sym = symbol.strip().upper()
    if not _SYMBOL_RE.match(sym):
        raise HTTPException(status_code=400, detail=f"Invalid ticker symbol: {symbol}")
    return sym


class PositionRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=8, pattern=r'^[A-Za-z]{1,5}(\.[A-Za-z]{1,4})?$')
    shares: float = Field(..., gt=0)
    avgCost: float = Field(..., gt=0)

# --- Config ---
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
if not FINNHUB_KEY:
    logger.warning("FINNHUB_API_KEY not set — news will use mock data")
DATA_DIR = Path(__file__).parent / "data"
STATIC_DIR = Path(__file__).parent / "static"

# Default watchlist
DEFAULT_SYMBOLS = ["SPY", "QQQ", "DIA", "AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META"]

# --- C4 fix: Thread-safe price cache ---
_cache_lock = threading.Lock()
price_cache: dict = {}
cache_time: float = 0
CACHE_TTL = 15  # seconds


_auto_trader_ref = None  # Module-level ref so scan loop can notify trader
_probability_model = None  # Module-level ref for consensus probability model


async def _auto_scan_loop():
    """Background task: auto-scan for arbitrage every 10 seconds.

    After each scan, immediately notifies the auto trader so it can
    evaluate and execute on opportunities within seconds — not minutes.

    Reduced from 60s to 10s: with authenticated Kalshi API (1-3s fetch)
    and cached Polymarket data, scans complete in 2-5s. The 10s interval
    gives 6x more opportunity detection on medium-duration markets
    without stacking scans.
    """
    await asyncio.sleep(5)  # wait for server to fully start
    _scan_in_progress = False
    while True:
        if _scan_in_progress:
            await asyncio.sleep(2)
            continue
        _scan_in_progress = True
        try:
            scanner = get_scanner()
            result = await scanner.scan()
            opp_count = result.get("opportunities_count", 0)
            logger.info(
                "Auto-scan: %d events, %d matched, %d opportunities",
                result.get("events_count", 0),
                result.get("multi_platform_matches", 0),
                opp_count,
            )
            # Broadcast updated feed + opportunities to all WS clients
            await broadcast_update({
                "type": "scan_result",
                "summary": result,
                "opportunities": scanner.get_opportunities(),
                "feed": scanner.get_feed(),
            })
            # Update probability model with matched events
            if _probability_model and hasattr(scanner, 'get_matched_events'):
                matched = scanner.get_matched_events()
                if matched:
                    _probability_model.update_from_matched_events(
                        [e.to_dict() if hasattr(e, 'to_dict') else e for e in matched])
            # Wake the auto trader immediately — trade execution takes priority
            if _auto_trader_ref is not None:
                await _auto_trader_ref.notify_scan_complete()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Auto-scan error: %s", exc)
        finally:
            _scan_in_progress = False
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    DATA_DIR.mkdir(exist_ok=True)
    # Init watchlist file if missing
    wl_file = DATA_DIR / "watchlist.json"
    if not wl_file.exists():
        _atomic_write(wl_file, json.dumps(DEFAULT_SYMBOLS))
    portfolio_file = DATA_DIR / "portfolio.json"
    if not portfolio_file.exists():
        _atomic_write(portfolio_file, json.dumps([]))
    # Start portfolio scheduler
    from portfolio_manager import start_scheduler
    scheduler = start_scheduler()
    # Create decision logger early so arb scanner can use it too
    decision_log = DecisionLogger() if _POSITIONS_AVAILABLE else None
    # Init Arbitrage subsystem
    if _ARBITRAGE_AVAILABLE:
        arb_registry = AdapterRegistry()
        arb_registry.register(KalshiAdapter())
        arb_registry.register(PolymarketAdapter())
        arb_registry.register(PredictItAdapter())
        arb_registry.register(LimitlessAdapter())
        # Opinion Labs: not available in US — skip unless API key is set
        if os.environ.get("OPINION_LABS_API_KEY"):
            arb_registry.register(OpinionLabsAdapter())
        arb_registry.register(RobinhoodAdapter())
        arb_registry.register(CoinbaseAdapter())
        arb_registry.register(CryptoSpotAdapter())
        init_scanner(arb_registry, decision_logger=decision_log)
        (DATA_DIR / "arbitrage").mkdir(exist_ok=True)
        logger.info("Arbitrage subsystem initialized with %d adapters", len(arb_registry.list_platforms()))
        # Start auto-scan background task
        scan_task = asyncio.create_task(_auto_scan_loop())
    # Init Position system
    _exit_task = None
    _news_scanner = None
    news_ai = None
    if _POSITIONS_AVAILABLE:
        try:
            executors = {}
            poly_exec = PolymarketExecutor()
            if poly_exec.is_configured(): executors["polymarket"] = poly_exec
            kalshi_exec = KalshiExecutor()
            if kalshi_exec.is_configured(): executors["kalshi"] = kalshi_exec
            coinbase_exec = CoinbaseSpotExecutor()
            if coinbase_exec.is_configured():
                executors["coinbase_spot"] = coinbase_exec
                executors["coinbase"] = coinbase_exec  # Adapter uses "coinbase" as platform name
            predictit_exec = PredictItExecutor()
            if predictit_exec.is_configured(): executors["predictit"] = predictit_exec
            limitless_exec = LimitlessExecutor()
            if limitless_exec.is_configured(): executors["limitless"] = limitless_exec
            opinion_exec = OpinionLabsExecutor()
            if opinion_exec.is_configured(): executors["opinion_labs"] = opinion_exec
            robinhood_exec = RobinhoodExecutor()
            if robinhood_exec.is_configured(): executors["robinhood"] = robinhood_exec
            crypto_spot_exec = CryptoSpotExecutor()
            if crypto_spot_exec.is_configured(): executors["crypto_spot"] = crypto_spot_exec
            kraken_cli_exec = KrakenCLIExecutor()
            if kraken_cli_exec.is_configured(): executors["kraken"] = kraken_cli_exec

            if is_paper_mode():
                # Wrap all executors in PaperExecutor
                paper_executors = {}
                for name, exec_ in executors.items():
                    paper_executors[name] = PaperExecutor(exec_, starting_balance=get_paper_balance(), use_limit_orders=True)
                # Always ensure polymarket paper executor exists (price lookups are public, no keys needed)
                if "polymarket" not in paper_executors:
                    paper_executors["polymarket"] = PaperExecutor(PolymarketExecutor(), starting_balance=get_paper_balance(), use_limit_orders=True)
                executors = paper_executors
                logger.info("Position system running in PAPER TRADING mode (balance=$%.2f)", get_paper_balance())

            journal = TradeJournal(data_dir=DATA_DIR / "positions")
            pm = PositionManager(data_dir=DATA_DIR / "positions", executors=executors, trade_journal=journal)

            # Rebuild PaperExecutor position state from loaded packages
            # (PaperExecutor tracks positions in memory, lost on restart)
            if is_paper_mode():
                for pkg in pm.list_packages("open"):
                    for leg in pkg.get("legs", []):
                        if leg.get("status") != "open":
                            continue
                        executor = executors.get(leg.get("platform"))
                        if executor and hasattr(executor, 'positions'):
                            asset_id = leg.get("asset_id", "")
                            qty = leg.get("quantity", 0)
                            entry_price = leg.get("entry_price", 0)
                            if asset_id and qty > 0 and entry_price > 0:
                                # C7 fix: deduct leg cost from paper balance to prevent
                                # inflated balance bypassing exposure limits
                                leg_cost = leg.get("cost", qty * entry_price)
                                if asset_id in executor.positions:
                                    existing = executor.positions[asset_id]
                                    total = existing["quantity"] + qty
                                    existing["avg_entry_price"] = (
                                        existing["avg_entry_price"] * existing["quantity"] + entry_price * qty
                                    ) / total
                                    existing["quantity"] = total
                                else:
                                    executor.positions[asset_id] = {
                                        "quantity": qty, "avg_entry_price": entry_price
                                    }
                                executor.balance -= leg_cost
                rebuilt = sum(len(e.positions) for e in executors.values() if hasattr(e, 'positions'))
                logger.info("Rebuilt %d paper positions from %d open packages", rebuilt, len(pm.list_packages("open")))

            # AI advisor always created — checks for API keys dynamically
            # Live: Anthropic → Groq → Gemini → OpenRouter
            # Paper: Groq → Gemini → OpenRouter (skip Anthropic to save costs)
            ai = AIAdvisor(paper_mode=is_paper_mode())
            if decision_log is None:
                decision_log = DecisionLogger()
            exit_engine = ExitEngine(pm, ai_advisor=ai, decision_logger=decision_log)
            exit_engine.start()
            # Start auto trader (works with or without arbitrage scanner)
            arb_scanner = get_scanner() if _ARBITRAGE_AVAILABLE else None
            insider = InsiderTracker(data_dir=DATA_DIR / "positions")
            insider.start()
            global _auto_trader_ref, _probability_model
            _probability_model = ProbabilityModel()
            _auto_trader = AutoTrader(pm, scanner=arb_scanner, insider_tracker=insider,
                                       decision_logger=decision_log,
                                       probability_model=_probability_model)
            _auto_trader.start()
            _auto_trader_ref = _auto_trader
            logger.info("Auto trader started — reacts to arb scanner within seconds, 5min safety-net fallback")
            # News scanner — AI-powered RSS headline analysis + Scrapling deep dive
            news_ai = NewsAI(paper_mode=is_paper_mode())
            _news_scanner = NewsScanner(
                position_manager=pm,
                news_ai=news_ai,
                auto_trader=_auto_trader,
                decision_logger=decision_log,
            )
            _news_scanner.start()
            exit_engine._news_scanner = _news_scanner
            logger.info("News scanner started — will scan RSS feeds every 2.5 min")
            init_position_system(pm, exit_engine, ai, trade_journal=journal, auto_trader=_auto_trader, insider_tracker=insider)
            _exit_task = True
            logger.info("Position system initialized with %d executors", len(executors))

            # Multi-asset price feed (shared by sniper + market maker)
            _btc_sniper = None
            _price_feed = None
            try:
                from positions.price_feed import BinancePriceFeed
                from positions.btc_sniper import BtcSniper

                # Configure assets: BTC always, optionally ETH/SOL/XRP
                sniper_assets_str = os.environ.get("SNIPER_ASSETS", "BTC")
                sniper_assets = [a.strip().upper() for a in sniper_assets_str.split(",") if a.strip()]

                _price_feed = BinancePriceFeed(assets=sniper_assets)
                _price_feed.start()

                sniper_bankroll = float(os.environ.get("SNIPER_BANKROLL", "500"))
                sniper_mode = os.environ.get("SNIPER_MODE", "paper" if is_paper_mode() else "safe")
                _btc_sniper = BtcSniper(_price_feed, position_manager=pm,
                                         bankroll=sniper_bankroll, mode=sniper_mode,
                                         assets=sniper_assets)
                _btc_sniper.start()
                logger.info("Crypto sniper started (assets=%s, bankroll=$%.0f, mode=%s)",
                            ",".join(sniper_assets), sniper_bankroll, sniper_mode)
            except Exception as e:
                logger.warning("Sniper init failed (non-critical): %s", e)

            # Market maker with preemptive cancel + token merging (Phase 2)
            _market_maker = None
            try:
                from positions.market_maker import MarketMaker

                if _price_feed is None:
                    from positions.price_feed import BinancePriceFeed
                    _price_feed = BinancePriceFeed(assets=["BTC"])
                    _price_feed.start()

                mm_capital = float(os.environ.get("MM_CAPITAL", "1000"))
                _market_maker = MarketMaker(_price_feed, position_manager=pm,
                                            total_capital=mm_capital)
                _market_maker.start()
                logger.info("Market maker started (capital=$%.0f, preemptive cancel + token merge enabled)",
                            mm_capital)
            except Exception as e:
                logger.warning("Market maker init failed (non-critical): %s", e)

            # Polymarket WebSocket price feed — real-time prices for open positions
            try:
                from positions.polymarket_ws import PolymarketPriceFeed
                _poly_ws = PolymarketPriceFeed()
                # Subscribe to condition IDs of open positions
                open_cids = set()
                for p in pm.list_packages(status="open"):
                    for leg in p.get("legs", []):
                        if leg.get("status") == "open" and leg.get("platform") == "polymarket":
                            aid = leg.get("asset_id", "")
                            cid = aid.split(":")[0] if ":" in aid else aid
                            if cid:
                                open_cids.add(cid)
                if open_cids:
                    _poly_ws.subscribe(list(open_cids))
                _poly_ws.start()
                logger.info("Polymarket WS feed started, tracking %d positions", len(open_cids))
            except Exception as e:
                logger.warning("Polymarket WS feed init failed (non-critical): %s", e)

        except Exception as e:
            logger.error("Position system init failed: %s", e)

    # Political synthetic analyzer
    _political_analyzer = None
    if _POLITICAL_AVAILABLE and _ARBITRAGE_AVAILABLE:
        try:
            _political_analyzer = PoliticalAnalyzer(
                scanner=arb_scanner if _ARBITRAGE_AVAILABLE else None,
                ai_advisor=ai if _POSITIONS_AVAILABLE else None,
                decision_logger=decision_log if _POSITIONS_AVAILABLE else None,
                auto_trader=_auto_trader if _POSITIONS_AVAILABLE else None,
            )
            _political_analyzer.start()
            if _POSITIONS_AVAILABLE and _auto_trader:
                _auto_trader.set_political_analyzer(_political_analyzer)
            init_political_router(_political_analyzer)
            logger.info("Political analyzer started (15-min cycle)")
        except Exception as e:
            logger.warning("Political analyzer init failed: %s", e)

    # Weather scanner — NWS forecast edge on Kalshi weather markets
    try:
        from positions.weather_scanner import WeatherScanner
        _weather_scanner = WeatherScanner(
            decision_logger=decision_log if _POSITIONS_AVAILABLE else None
        )
        if _POSITIONS_AVAILABLE and _auto_trader:
            _auto_trader.set_weather_scanner(_weather_scanner)
        logger.info("Weather scanner initialized")
    except Exception as e:
        logger.warning("Weather scanner init failed: %s", e)

    # Eval logger for hindsight analysis
    _eval_log = None
    _backfill_task = None
    if _EVAL_AVAILABLE:
        _eval_log = EvalLogger()
        init_eval_router(_eval_log)
        if _POLITICAL_AVAILABLE and _political_analyzer:
            from political.router import set_eval_logger
            set_eval_logger(_eval_log)
        # Start hourly backfill task for skipped opportunity resolution checks
        async def _backfill_loop():
            while True:
                await asyncio.sleep(3600)  # 1 hour
                try:
                    unresolved = _eval_log.get_unresolved_skips()
                    if unresolved:
                        logger.info("Eval backfill: %d unresolved skipped opportunities", len(unresolved))
                except Exception as e:
                    logger.warning("Eval backfill error: %s", e)
        _backfill_task = asyncio.create_task(_backfill_loop())
        logger.info("Eval logger initialized with hourly backfill")

    from positions.calibration import CalibrationEngine
    _calibration_engine = CalibrationEngine(_eval_log, journal if _POSITIONS_AVAILABLE else None)

    async def _calibration_loop():
        """Run calibration report every 24 hours."""
        while True:
            await asyncio.sleep(86400)  # 24 hours
            try:
                path = _calibration_engine.save_report()
                logger.info("Calibration report generated: %s", path)
            except Exception as e:
                logger.error("Calibration report failed: %s", e)

    asyncio.create_task(_calibration_loop())
    app.state.calibration_engine = _calibration_engine

    logger.info("Lobsterminal started on port 8500")
    yield
    # Shutdown
    if _political_analyzer:
        _political_analyzer.stop()
    if _backfill_task:
        _backfill_task.cancel()
    if _POSITIONS_AVAILABLE and _exit_task:
        try:
            exit_engine.stop()
            if _auto_trader:
                _auto_trader.stop()
            if _news_scanner:
                _news_scanner.stop()
            if news_ai:
                await news_ai.close()
            if insider:
                insider.stop()
            # Shutdown new strategy modules
            if _btc_sniper:
                _btc_sniper.stop()
            if _market_maker:
                _market_maker.stop()
            if _price_feed:
                _price_feed.stop()
        except Exception:
            pass
    if scheduler:
        scheduler.shutdown(wait=False)
    if _ARBITRAGE_AVAILABLE:
        scan_task.cancel()
        await arb_registry.close_all()
    logger.info("Lobsterminal shutting down")


app = FastAPI(title="Lobsterminal", lifespan=lifespan)

# S2 fix: CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8500", "http://localhost:8500"],
    allow_methods=["GET", "POST", "DELETE", "PATCH"],
    allow_headers=["X-API-Key"],
)

# --- Include GenA / Direct Indexing routers ---
from swarm_engine import router as swarm_router
from backtest_engine import router as backtest_router
from portfolio_manager import router as portfolio_router, portfolios_router
from strategy_engine import router as strategy_router

try:
    import dexter_client
except ImportError:
    dexter_client = None

try:
    import valuation_engine
except ImportError:
    valuation_engine = None

app.include_router(swarm_router)
app.include_router(backtest_router)
app.include_router(portfolio_router)
app.include_router(portfolios_router)
app.include_router(strategy_router)

if _ARBITRAGE_AVAILABLE:
    app.include_router(arbitrage_router)

if _POSITIONS_AVAILABLE:
    app.include_router(position_router)

if _POLITICAL_AVAILABLE:
    app.include_router(political_router)

if _EVAL_AVAILABLE:
    app.include_router(eval_router_mod)

# --- Research API Routes ---
try:
    import asyncio as _aio
    from research.company_researcher import research_company, research_batch
    from research.stock_universe import get_universe, get_ticker_count, refresh_us_universe
    from research.arbitrage_strategies import research_strategies

    @app.get("/api/research/company/{ticker}")
    async def get_company_research(ticker: str):
        loop = _aio.get_running_loop()
        result = await loop.run_in_executor(None, research_company, ticker.upper())
        if not result:
            return {"error": f"No research found for {ticker}"}
        return result

    @app.get("/api/research/batch")
    async def get_batch_research(tickers: str = ""):
        ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()][:10]
        if not ticker_list:
            return {"error": "Provide tickers as comma-separated list"}
        loop = _aio.get_running_loop()
        results = await loop.run_in_executor(None, research_batch, ticker_list)
        return {"results": results, "count": len(results)}

    @app.get("/api/research/universe")
    async def get_stock_universe(
        exchange: str = None, cap_tier: str = None, include_hkex: bool = False,
        offset: int = 0, limit: int = 200, search: str = None,
    ):
        loop = _aio.get_running_loop()
        universe = await loop.run_in_executor(None, lambda: get_universe(exchange=exchange, cap_tier=cap_tier, include_hkex=include_hkex))
        # Extract ticker strings from stock dicts
        tickers = [s["ticker"] if isinstance(s, dict) else s for s in universe]
        # Optional text search filter
        if search:
            q = search.upper()
            tickers = [t for t in tickers if q in t.upper()]
        total = len(tickers)
        # Paginate (no hard cap — client controls page size)
        page = tickers[offset:offset + limit]
        return {"tickers": page, "total": total, "offset": offset, "limit": limit, "ticker_count": get_ticker_count()}

    @app.get("/api/research/strategies")
    async def get_strategies(force: bool = False):
        loop = _aio.get_running_loop()
        strategies = await loop.run_in_executor(None, lambda: research_strategies(force=force))
        return {"strategies": strategies, "count": len(strategies)}

    @app.get("/api/research/universe/quotes")
    async def get_universe_quotes(
        tickers: str = "", offset: int = 0, limit: int = 50,
        exchange: str = None, include_hkex: bool = False,
    ):
        """Get quotes for a page of universe tickers. If tickers param provided, uses those;
        otherwise fetches from the full universe starting at offset."""
        if tickers:
            ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
        else:
            loop = _aio.get_running_loop()
            all_stocks = await loop.run_in_executor(None, lambda: get_universe(exchange=exchange, include_hkex=include_hkex))
            all_tickers = [s["ticker"] if isinstance(s, dict) else s for s in all_stocks]
            ticker_list = all_tickers[offset:offset + limit]
        if not ticker_list:
            return {"quotes": [], "total": 0}
        # Cap at 50 per request to avoid yfinance rate limits
        ticker_list = ticker_list[:50]
        quotes = await asyncio.get_event_loop().run_in_executor(None, _fetch_quotes_sync, ticker_list)
        return {"quotes": quotes, "count": len(quotes), "offset": offset}

    logger.info("Research API endpoints registered")
except ImportError as _research_err:
    logger.warning("Research modules not available: %s", _research_err)

# --- API Routes ---

@app.get("/api/health")
async def health():
    fmp_status = "enabled" if os.environ.get("FMP_API_KEY") else "disabled (set FMP_API_KEY)"
    dexter_status = "enabled" if os.environ.get("FINANCIAL_DATASETS_API_KEY") else "free tier (AAPL, NVDA, MSFT)"
    return {"status": "ok", "time": time.time(), "fmp_api": fmp_status, "dexter_api": dexter_status}


@app.get("/api/ping")
async def ping():
    return {"ping": "pong", "timestamp": time.time()}


@app.get("/api/quotes")
async def get_quotes(_=Depends(verify_api_key)):
    """Get current quotes for watchlist symbols."""
    global price_cache, cache_time

    symbols = _load_watchlist()
    now = time.time()

    with _cache_lock:
        if now - cache_time < CACHE_TTL and price_cache:
            return JSONResponse(content=list(price_cache.values()))

    # I2 fix: run blocking yfinance in thread executor
    quotes = await asyncio.get_event_loop().run_in_executor(None, _fetch_quotes_sync, symbols)

    with _cache_lock:
        price_cache = {q["symbol"]: q for q in quotes}
        cache_time = time.time()
    return JSONResponse(content=quotes)


def _fetch_quotes_sync(symbols: list[str]) -> list[dict]:
    """Fetch quotes synchronously (runs in thread pool). Retries once if rate-limited.

    Falls back to Yahoo chart API direct HTTP if yfinance is blocked.
    """
    for attempt in range(2):
        quotes = []
        try:
            tickers = yf.Tickers(" ".join(symbols))
            for sym in symbols:
                try:
                    t = tickers.tickers[sym]
                    info = t.fast_info
                    price = info.last_price or 0
                    prev = info.previous_close or price
                    change = price - prev
                    change_pct = (change / prev * 100) if prev else 0
                    quotes.append({
                        "symbol": sym,
                        "price": round(price, 2),
                        "change": round(change, 2),
                        "changePercent": round(change_pct, 2),
                        "volume": info.last_volume or 0,
                        "high": round(info.day_high or price, 2),
                        "low": round(info.day_low or price, 2),
                        "marketCap": info.market_cap or 0,
                    })
                except Exception:
                    quotes.append(_mock_quote(sym))
        except Exception:
            logger.exception("yfinance quotes failed (attempt %d)", attempt + 1)
            quotes = []

        # If we got real data, return it
        if quotes and any(q.get("marketCap", 0) > 0 for q in quotes):
            return quotes

        # Retry after a short delay if first attempt returned empty/mock-only
        if attempt == 0:
            logger.warning("yfinance returned empty data, retrying in 2s...")
            time.sleep(2)

    # Fallback: Yahoo v8 chart API direct
    logger.info("yfinance rate-limited, trying Yahoo chart API direct...")
    chart_quotes = _fetch_quotes_yahoo_chart(symbols)
    if chart_quotes and any(q.get("price", 0) > 0 for q in chart_quotes):
        return chart_quotes

    # Fallback: Finnhub quote API
    if FINNHUB_KEY:
        logger.info("Yahoo chart API failed, trying Finnhub...")
        fh_quotes = _fetch_quotes_finnhub(symbols)
        if fh_quotes and any(q.get("price", 0) > 0 for q in fh_quotes):
            return fh_quotes

    # Fallback: Scrapling (scrape Google Finance)
    logger.info("API sources exhausted, trying Scrapling scrape...")
    scrapling_quotes = _fetch_quotes_scrapling(symbols)
    if scrapling_quotes and any(q.get("price", 0) > 0 for q in scrapling_quotes):
        return scrapling_quotes

    # Last resort: return cached prices if we have any
    with _cache_lock:
        if price_cache:
            logger.warning("All live sources failed, returning stale cache")
            return [price_cache.get(s, _mock_quote(s)) for s in symbols]

    logger.warning("All quote sources exhausted and no cache, using mock data")
    return [_mock_quote(s) for s in symbols]


def _fetch_quotes_yahoo_chart(symbols: list[str]) -> list[dict]:
    """Fetch latest quotes via Yahoo Finance v8 chart API (bypasses yfinance rate limiter)."""
    quotes = []
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            for sym in symbols:
                try:
                    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=2d"
                    resp = client.get(url, headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    })
                    if resp.status_code != 200:
                        quotes.append(_mock_quote(sym))
                        continue
                    data = resp.json()
                    result = data.get("chart", {}).get("result", [])
                    if not result:
                        quotes.append(_mock_quote(sym))
                        continue
                    meta = result[0].get("meta", {})
                    price = meta.get("regularMarketPrice", 0)
                    prev = meta.get("chartPreviousClose", meta.get("previousClose", price))
                    change = round(price - prev, 2) if prev else 0
                    change_pct = round((change / prev * 100), 2) if prev else 0
                    high = meta.get("regularMarketDayHigh", price)
                    low = meta.get("regularMarketDayLow", price)
                    vol = meta.get("regularMarketVolume", 0)
                    quotes.append({
                        "symbol": sym,
                        "price": round(price, 2),
                        "change": change,
                        "changePercent": change_pct,
                        "volume": vol or 0,
                        "high": round(high or price, 2),
                        "low": round(low or price, 2),
                        "marketCap": 0,
                    })
                except Exception:
                    quotes.append(_mock_quote(sym))
    except Exception as e:
        logger.warning("Yahoo chart API quotes failed: %s", e)
    return quotes


def _fetch_quotes_finnhub(symbols: list[str]) -> list[dict]:
    """Fetch latest quotes via Finnhub API."""
    quotes = []
    try:
        with httpx.Client(timeout=10) as client:
            for sym in symbols:
                try:
                    url = f"https://finnhub.io/api/v1/quote?symbol={sym}"
                    resp = client.get(url, headers={"X-Finnhub-Token": FINNHUB_KEY})
                    if resp.status_code != 200:
                        quotes.append(_mock_quote(sym))
                        continue
                    data = resp.json()
                    price = data.get("c", 0)  # current
                    prev = data.get("pc", price)  # previous close
                    if not price or price == 0:
                        quotes.append(_mock_quote(sym))
                        continue
                    change = round(price - prev, 2)
                    change_pct = round((change / prev * 100), 2) if prev else 0
                    quotes.append({
                        "symbol": sym,
                        "price": round(price, 2),
                        "change": change,
                        "changePercent": change_pct,
                        "volume": 0,
                        "high": round(data.get("h", price), 2),
                        "low": round(data.get("l", price), 2),
                        "marketCap": 0,
                    })
                    time.sleep(0.04)  # Finnhub free: 30 calls/sec
                except Exception:
                    quotes.append(_mock_quote(sym))
    except Exception as e:
        logger.warning("Finnhub quotes failed: %s", e)
    return quotes


@app.get("/api/history/{symbol}")
async def get_history(symbol: str, period: str = "6mo", interval: str = "1d", _=Depends(verify_api_key)):
    """Get OHLCV history for charting. Falls back to generated data if yfinance is rate-limited."""
    sym = _validate_symbol(symbol)
    # I2 fix: run blocking yfinance in thread executor
    data = await asyncio.get_event_loop().run_in_executor(None, _fetch_history_sync, sym, period, interval)
    return JSONResponse(content=data)


def _fetch_history_sync(symbol: str, period: str, interval: str) -> list[dict]:
    """Fetch history synchronously (runs in thread pool).

    Falls back through: yfinance → Yahoo chart API → Finnhub → mock.
    """
    # 1. yfinance
    try:
        t = yf.Ticker(symbol)
        df = t.history(period=period, interval=interval)
        if not df.empty:
            data = []
            for idx, row in df.iterrows():
                data.append({
                    "time": idx.strftime("%Y-%m-%d"),
                    "open": round(row["Open"], 2),
                    "high": round(row["High"], 2),
                    "low": round(row["Low"], 2),
                    "close": round(row["Close"], 2),
                    "volume": int(row["Volume"]),
                })
            if data:
                _save_history_cache(symbol, period, data)
                return data
    except Exception:
        logger.debug("yfinance history failed for %s", symbol)

    # 2. Yahoo chart API direct
    try:
        data = _fetch_history_yahoo_chart(symbol, period, interval)
        if data:
            _save_history_cache(symbol, period, data)
            return data
    except Exception:
        logger.debug("Yahoo chart API history failed for %s", symbol)

    # 3. Finnhub candles
    if FINNHUB_KEY:
        try:
            data = _fetch_history_finnhub(symbol, period)
            if data:
                _save_history_cache(symbol, period, data)
                return data
        except Exception:
            logger.debug("Finnhub history failed for %s", symbol)

    # 4. Cached historical data from a previous successful fetch
    cached = _load_history_cache(symbol, period)
    if cached:
        logger.info("Using cached history for %s (%d points)", symbol, len(cached))
        return cached

    # 5. Mock fallback (only as absolute last resort)
    logger.warning("All history sources failed for %s, using mock", symbol)
    return _generate_mock_history(symbol)


def _fetch_history_yahoo_chart(symbol: str, period: str, interval: str) -> list[dict]:
    """Fetch OHLCV history via Yahoo v8 chart API."""
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={period}&interval={interval}"
        resp = client.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        if resp.status_code != 200:
            return []
        result = resp.json().get("chart", {}).get("result", [])
        if not result:
            return []
        timestamps = result[0].get("timestamp", [])
        quote = result[0].get("indicators", {}).get("quote", [{}])[0]
        opens = quote.get("open", [])
        highs = quote.get("high", [])
        lows = quote.get("low", [])
        closes = quote.get("close", [])
        volumes = quote.get("volume", [])
        if not timestamps or not closes:
            return []
        data = []
        for i, ts in enumerate(timestamps):
            if closes[i] is None:
                continue
            dt = datetime.utcfromtimestamp(ts)
            data.append({
                "time": dt.strftime("%Y-%m-%d"),
                "open": round(opens[i] or closes[i], 2),
                "high": round(highs[i] or closes[i], 2),
                "low": round(lows[i] or closes[i], 2),
                "close": round(closes[i], 2),
                "volume": int(volumes[i] or 0),
            })
        return data


def _fetch_history_finnhub(symbol: str, period: str) -> list[dict]:
    """Fetch daily OHLCV history via Finnhub candles API."""
    period_days = {"1d": 1, "5d": 5, "1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730, "5y": 1825}
    days = period_days.get(period, 365)
    end_ts = int(time.time())
    start_ts = end_ts - days * 86400

    with httpx.Client(timeout=15) as client:
        url = f"https://finnhub.io/api/v1/stock/candle?symbol={symbol}&resolution=D&from={start_ts}&to={end_ts}"
        resp = client.get(url, headers={"X-Finnhub-Token": FINNHUB_KEY})
        if resp.status_code != 200:
            return []
        d = resp.json()
        if d.get("s") != "ok" or not d.get("t"):
            return []
        data = []
        for i, ts in enumerate(d["t"]):
            dt = datetime.utcfromtimestamp(ts)
            data.append({
                "time": dt.strftime("%Y-%m-%d"),
                "open": round(d["o"][i], 2),
                "high": round(d["h"][i], 2),
                "low": round(d["l"][i], 2),
                "close": round(d["c"][i], 2),
                "volume": int(d["v"][i]),
            })
        return data


# --- History cache (persists across restarts) ---
HISTORY_CACHE_DIR = DATA_DIR / "history_cache"


def _save_history_cache(symbol: str, period: str, data: list[dict]):
    """Save fetched history to disk for offline fallback."""
    HISTORY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = HISTORY_CACHE_DIR / f"{symbol}_{period}.json"
    try:
        _atomic_write(cache_file, json.dumps({"symbol": symbol, "period": period, "fetched_at": time.time(), "data": data}))
    except Exception:
        pass


def _load_history_cache(symbol: str, period: str) -> list[dict]:
    """Load cached history from disk. Returns [] if not found or too stale (>7 days)."""
    cache_file = HISTORY_CACHE_DIR / f"{symbol}_{period}.json"
    if not cache_file.exists():
        return []
    try:
        cached = json.loads(cache_file.read_text())
        # Accept cache if less than 7 days old
        if time.time() - cached.get("fetched_at", 0) > 7 * 86400:
            return []
        return cached.get("data", [])
    except (json.JSONDecodeError, OSError, KeyError):
        return []


@app.get("/api/watchlist")
async def get_watchlist(_=Depends(verify_api_key)):
    return JSONResponse(content=_load_watchlist())


@app.post("/api/watchlist/{symbol}")
async def add_to_watchlist(symbol: str, _=Depends(verify_api_key)):
    sym = _validate_symbol(symbol)
    symbols = _load_watchlist()
    if sym not in symbols:
        symbols.append(sym)
        _save_watchlist(symbols)
    return JSONResponse(content=symbols)


@app.delete("/api/watchlist/{symbol}")
async def remove_from_watchlist(symbol: str, _=Depends(verify_api_key)):
    sym = _validate_symbol(symbol)
    symbols = _load_watchlist()
    symbols = [s for s in symbols if s != sym]
    _save_watchlist(symbols)
    return JSONResponse(content=symbols)


@app.get("/api/news")
async def get_news(symbol: str = "", _=Depends(verify_api_key)):
    """Get financial news. Tries Dexter (symbol-specific), then RSS, then mock."""
    if symbol:
        symbol = _validate_symbol(symbol)
        # Try Dexter for symbol-specific news first
        if dexter_client:
            try:
                dexter_news = await asyncio.get_event_loop().run_in_executor(
                    None, dexter_client.get_company_news, symbol, 15
                )
                if dexter_news:
                    articles = []
                    for n in dexter_news:
                        dt = 0
                        if n.get("date"):
                            try:
                                from datetime import datetime as _dt
                                dt = int(_dt.fromisoformat(n["date"].replace("Z", "+00:00")).timestamp())
                            except Exception:
                                pass
                        articles.append({
                            "headline": n.get("title", ""),
                            "source": n.get("source", "Financial Datasets"),
                            "datetime": dt,
                            "url": n.get("url", "#"),
                            "summary": (n.get("text", "") or "")[:200],
                        })
                    if articles:
                        return JSONResponse(content=articles[:20])
            except Exception:
                logger.debug("Dexter news failed for %s", symbol)
    try:
        articles = await asyncio.get_event_loop().run_in_executor(
            None, _fetch_rss_news, symbol
        )
        if articles:
            return JSONResponse(content=articles[:20])
    except Exception:
        logger.exception("RSS news fetch failed")
    return JSONResponse(content=_mock_news())


def _fetch_rss_news(symbol: str = "") -> list[dict]:
    """Fetch news from free RSS feeds (runs in thread pool)."""
    if symbol:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
    else:
        url = "https://news.google.com/rss/search?q=stock+market&hl=en-US"

    feed = feedparser.parse(url)
    articles = []
    for entry in feed.entries[:20]:
        # Parse published time to unix timestamp
        dt = 0
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                dt = int(time.mktime(entry.published_parsed))
            except Exception:
                pass

        articles.append({
            "headline": entry.get("title", ""),
            "source": feed.feed.get("title", "RSS"),
            "datetime": dt,
            "url": entry.get("link", "#"),
            "summary": entry.get("summary", "")[:200] if entry.get("summary") else "",
        })
    return articles


# I5 fix: renamed to /api/positions to avoid conflict with portfolio_manager's /api/portfolio routes
@app.get("/api/positions")
async def get_portfolio(_=Depends(verify_api_key)):
    return JSONResponse(content=_load_portfolio())


@app.post("/api/positions")
async def add_position(position: PositionRequest, _=Depends(verify_api_key)):
    portfolio = _load_portfolio()
    portfolio.append({
        "symbol": position.symbol.upper(),
        "shares": position.shares,
        "avgCost": position.avgCost,
    })
    _save_portfolio(portfolio)
    return JSONResponse(content=portfolio)


@app.delete("/api/positions/{symbol}")
async def remove_position(symbol: str, _=Depends(verify_api_key)):
    sym = _validate_symbol(symbol)
    portfolio = _load_portfolio()
    portfolio = [p for p in portfolio if p["symbol"] != sym]
    _save_portfolio(portfolio)
    return JSONResponse(content=portfolio)


# --- Dexter Financial Data Endpoints ---

@app.get("/api/dexter/ratios/{symbol}")
async def dexter_ratios(symbol: str, _=Depends(verify_api_key)):
    """Get key financial ratios snapshot from Financial Datasets API."""
    sym = _validate_symbol(symbol)
    if not dexter_client:
        raise HTTPException(status_code=501, detail="Dexter client not available")
    data = await asyncio.get_event_loop().run_in_executor(
        None, dexter_client.get_key_ratios, sym
    )
    if not data:
        raise HTTPException(status_code=404, detail=f"No ratios data for {sym}")
    return JSONResponse(content=data)


@app.get("/api/dexter/financials/{symbol}")
async def dexter_financials(symbol: str, period: str = "annual", limit: int = 4, _=Depends(verify_api_key)):
    """Get income statement, balance sheet, and cash flow from Financial Datasets API."""
    sym = _validate_symbol(symbol)
    if not dexter_client:
        raise HTTPException(status_code=501, detail="Dexter client not available")
    income = await asyncio.get_event_loop().run_in_executor(
        None, dexter_client.get_income_statements, sym, period, limit
    )
    balance = await asyncio.get_event_loop().run_in_executor(
        None, dexter_client.get_balance_sheets, sym, period, limit
    )
    cashflow = await asyncio.get_event_loop().run_in_executor(
        None, dexter_client.get_cash_flow_statements, sym, period, limit
    )
    return JSONResponse(content={
        "income_statements": income,
        "balance_sheets": balance,
        "cash_flow_statements": cashflow,
    })


@app.get("/api/dexter/insider-trades/{symbol}")
async def dexter_insider_trades(symbol: str, limit: int = 20, _=Depends(verify_api_key)):
    """Get insider trades (Form 4 filings) from Financial Datasets API."""
    sym = _validate_symbol(symbol)
    if not dexter_client:
        raise HTTPException(status_code=501, detail="Dexter client not available")
    trades = await asyncio.get_event_loop().run_in_executor(
        None, dexter_client.get_insider_trades, sym, limit
    )
    return JSONResponse(content=trades)


@app.get("/api/dexter/analyst-estimates/{symbol}")
async def dexter_analyst_estimates(symbol: str, period: str = "annual", _=Depends(verify_api_key)):
    """Get analyst estimates (EPS, revenue consensus) from Financial Datasets API."""
    sym = _validate_symbol(symbol)
    if not dexter_client:
        raise HTTPException(status_code=501, detail="Dexter client not available")
    estimates = await asyncio.get_event_loop().run_in_executor(
        None, dexter_client.get_analyst_estimates, sym, period
    )
    return JSONResponse(content=estimates)


@app.get("/api/dexter/news/{symbol}")
async def dexter_news(symbol: str, limit: int = 10, _=Depends(verify_api_key)):
    """Get company news from Financial Datasets API."""
    sym = _validate_symbol(symbol)
    if not dexter_client:
        raise HTTPException(status_code=501, detail="Dexter client not available")
    news = await asyncio.get_event_loop().run_in_executor(
        None, dexter_client.get_company_news, sym, limit
    )
    return JSONResponse(content=news)


@app.get("/api/dexter/filings/{symbol}")
async def dexter_filings(symbol: str, limit: int = 10, _=Depends(verify_api_key)):
    """Get SEC filings from Financial Datasets API."""
    sym = _validate_symbol(symbol)
    if not dexter_client:
        raise HTTPException(status_code=501, detail="Dexter client not available")
    filings = await asyncio.get_event_loop().run_in_executor(
        None, dexter_client.get_filings, sym, None, limit
    )
    return JSONResponse(content=filings)


@app.get("/api/dexter/segments/{symbol}")
async def dexter_segments(symbol: str, period: str = "annual", limit: int = 4, _=Depends(verify_api_key)):
    """Get segmented revenue breakdown from Financial Datasets API."""
    sym = _validate_symbol(symbol)
    if not dexter_client:
        raise HTTPException(status_code=501, detail="Dexter client not available")
    segments = await asyncio.get_event_loop().run_in_executor(
        None, dexter_client.get_segmented_revenues, sym, period, limit
    )
    return JSONResponse(content=segments)


@app.get("/api/dexter/dcf/{symbol}")
async def dexter_dcf(symbol: str, _=Depends(verify_api_key)):
    """Run DCF valuation using Dexter's workflow (sector WACC, FCF projection, sensitivity)."""
    sym = _validate_symbol(symbol)
    if not valuation_engine:
        raise HTTPException(status_code=501, detail="Valuation engine not available")
    result = await asyncio.get_event_loop().run_in_executor(
        None, valuation_engine.dcf_from_ticker, sym
    )
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])
    return JSONResponse(content=result)


@app.get("/api/dexter/score/{symbol}")
async def dexter_score(symbol: str, _=Depends(verify_api_key)):
    """Score a stock's fundamentals (0-100) across profitability, growth, valuation, health."""
    sym = _validate_symbol(symbol)
    if not valuation_engine:
        raise HTTPException(status_code=501, detail="Valuation engine not available")
    result = await asyncio.get_event_loop().run_in_executor(
        None, valuation_engine.score_from_ticker, sym
    )
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])
    return JSONResponse(content=result)


# --- WebSocket for real-time prices ---

connected_clients: set = set()
MAX_WS_CLIENTS = 50


@app.websocket("/ws/prices")
async def ws_prices(websocket: WebSocket):
    if len(connected_clients) >= MAX_WS_CLIENTS:
        await websocket.close(code=1013, reason="Max connections reached")
        return
    await websocket.accept()
    connected_clients.add(websocket)
    try:
        symbols = _load_watchlist()
        base_prices = {}
        with _cache_lock:
            for sym in symbols:
                cached = price_cache.get(sym)
                base_prices[sym] = cached["price"] if cached else SYMBOL_PRICES.get(sym, round(random.uniform(50, 500), 2))

        while True:
            for sym in symbols:
                # S8 fix: proportional price movement (0.03% of price)
                price = base_prices[sym]
                delta = random.gauss(0, price * 0.0003)
                base_prices[sym] = round(max(price * 0.5, price + delta), 2)
                await websocket.send_json({
                    "type": "trade",
                    "symbol": sym,
                    "price": base_prices[sym],
                    "volume": random.randint(100, 50000),
                    "timestamp": time.time(),
                })
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WebSocket error")
    finally:
        connected_clients.discard(websocket)


# --- Static files ---
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# --- Mock History Generator ---

# Realistic base prices per symbol
SYMBOL_PRICES = {
    "SPY": 580, "QQQ": 500, "DIA": 430, "AAPL": 240, "MSFT": 450,
    "GOOGL": 185, "AMZN": 210, "TSLA": 340, "NVDA": 140, "META": 580,
}


def _generate_mock_history(symbol: str, days: int = 130) -> list:
    """Generate realistic OHLCV data with trends, volatility, and volume patterns."""
    base = SYMBOL_PRICES.get(symbol.upper(), random.uniform(50, 400))
    data = []
    price = base * random.uniform(0.85, 0.95)  # start lower to show uptrend
    trend = random.uniform(-0.0005, 0.002)  # slight upward bias
    volatility = base * 0.015

    start_date = datetime.now() - timedelta(days=days)
    for i in range(days):
        d = start_date + timedelta(days=i)
        if d.weekday() >= 5:  # skip weekends
            continue
        # Random walk with trend and mean reversion
        drift = trend + 0.001 * (base - price) / base
        daily_return = drift + random.gauss(0, 1) * volatility / price
        price *= (1 + daily_return)
        price = max(price, base * 0.5)

        high_spread = abs(random.gauss(0, volatility * 0.6))
        low_spread = abs(random.gauss(0, volatility * 0.6))
        o = round(price * random.uniform(0.997, 1.003), 2)
        c = round(price, 2)
        h = round(max(o, c) + high_spread, 2)
        l = round(min(o, c) - low_spread, 2)
        # Volume: higher on volatile days
        base_vol = random.randint(5_000_000, 30_000_000)
        vol_multiplier = 1 + abs(daily_return) * 20
        v = int(base_vol * vol_multiplier)

        data.append({
            "time": d.strftime("%Y-%m-%d"),
            "open": o, "high": h, "low": l, "close": c,
            "volume": v,
        })
    return data


# --- Helpers ---

def _atomic_write(path: Path, content: str):
    """C5 fix: atomic file write — write to temp file then rename."""
    path.parent.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.write(fd, content.encode())
        os.close(fd)
        os.replace(tmp, str(path))
    except Exception:
        os.close(fd) if not os.get_inheritable(fd) else None
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _load_watchlist() -> list:
    f = DATA_DIR / "watchlist.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Corrupt watchlist.json, using defaults")
    return DEFAULT_SYMBOLS[:]


def _save_watchlist(symbols: list):
    _atomic_write(DATA_DIR / "watchlist.json", json.dumps(symbols))


def _load_portfolio() -> list:
    f = DATA_DIR / "portfolio.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Corrupt portfolio.json, starting fresh")
    return []


def _save_portfolio(portfolio: list):
    _atomic_write(DATA_DIR / "portfolio.json", json.dumps(portfolio))


def _fetch_quotes_scrapling(symbols: list[str]) -> list[dict]:
    """Fallback: scrape Google Finance for real-time quotes using Scrapling."""
    try:
        from scrapling import Fetcher
    except ImportError:
        logger.debug("Scrapling not available for quote fallback")
        return []

    quotes = []
    fetcher = Fetcher(auto_match=False)
    for sym in symbols:
        try:
            url = f"https://www.google.com/finance/quote/{sym}:NASDAQ"
            page = fetcher.get(url)
            if not page or not page.status == 200:
                # Try NYSE
                url = f"https://www.google.com/finance/quote/{sym}:NYSE"
                page = fetcher.get(url)
            if not page or not page.status == 200:
                continue

            # Google Finance price is in a div with data-last-price attribute
            price_el = page.css('[data-last-price]')
            if price_el:
                price = float(price_el[0].attrib.get('data-last-price', '0'))
            else:
                # Fallback: look for the main price span
                price_spans = page.css('div.YMlKec.fxKbKc')
                if price_spans:
                    price_text = price_spans[0].text.replace('$', '').replace(',', '').strip()
                    price = float(price_text)
                else:
                    continue

            if price <= 0:
                continue

            # Try to get change
            change = 0.0
            change_pct = 0.0
            change_els = page.css('div.JwB6zf')
            if change_els:
                change_text = change_els[0].text.replace('$', '').replace(',', '').replace('+', '').strip()
                try:
                    change = float(change_text)
                except (ValueError, TypeError):
                    pass
            pct_els = page.css('div.P2Luy.Ebnabc')
            if not pct_els:
                pct_els = page.css('div.JwB6zf + div')
            if pct_els:
                pct_text = pct_els[0].text.replace('%', '').replace('+', '').replace('(', '').replace(')', '').strip()
                try:
                    change_pct = float(pct_text)
                except (ValueError, TypeError):
                    pass

            quotes.append({
                "symbol": sym,
                "price": round(price, 2),
                "change": round(change, 2),
                "changePercent": round(change_pct, 2),
                "volume": 0,
                "high": round(price, 2),
                "low": round(price, 2),
                "marketCap": 0,
            })
        except Exception as exc:
            logger.debug("Scrapling quote failed for %s: %s", sym, exc)
            continue
    return quotes


def _mock_quote(symbol: str) -> dict:
    base = SYMBOL_PRICES.get(symbol.upper(), random.uniform(50, 400))
    price = round(base * random.uniform(0.98, 1.02), 2)
    change = round(random.uniform(-5, 5), 2)
    return {
        "symbol": symbol,
        "price": price,
        "change": change,
        "changePercent": round(change / price * 100, 2),
        "volume": random.randint(1_000_000, 50_000_000),
        "high": round(price + abs(change), 2),
        "low": round(price - abs(change), 2),
        "marketCap": random.randint(10_000_000_000, 3_000_000_000_000),
    }


def _mock_news() -> list:
    headlines = [
        {"headline": "Fed Signals Potential Rate Cut in Q2 2026", "source": "Reuters", "summary": "Federal Reserve officials hinted at easing monetary policy amid cooling inflation data."},
        {"headline": "NVDA Surges on Record AI Chip Demand", "source": "Bloomberg", "summary": "Nvidia reported quarterly revenue exceeding expectations driven by enterprise AI adoption."},
        {"headline": "Treasury Yields Fall as Economic Data Softens", "source": "CNBC", "summary": "10-year Treasury yields dropped to 3.8% following weaker-than-expected jobs report."},
        {"headline": "AAPL Announces Next-Gen M5 Chip Lineup", "source": "TechCrunch", "summary": "Apple revealed its M5 processor family with significant ML performance improvements."},
        {"headline": "Oil Prices Stabilize Above $75 on OPEC+ Decision", "source": "Reuters", "summary": "Crude prices held steady after OPEC+ agreed to maintain current production levels."},
        {"headline": "Crypto Market Sees Institutional Inflows", "source": "CoinDesk", "summary": "Bitcoin ETF inflows reached $2.1B this week as institutional adoption accelerates."},
        {"headline": "MSFT Cloud Revenue Beats Estimates by 12%", "source": "WSJ", "summary": "Microsoft Azure growth reaccelerated to 34% YoY driven by AI workload migration."},
        {"headline": "Global Supply Chain Disruptions Ease Further", "source": "FT", "summary": "Shipping costs and delivery times have returned to pre-pandemic levels across major trade routes."},
    ]
    now = int(time.time())
    return [{**h, "datetime": now - i * 3600, "url": "#"} for i, h in enumerate(headlines)]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8500, log_level="info")
