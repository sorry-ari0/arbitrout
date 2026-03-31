"""
Portfolio Management with Direct Indexing.

Provides deployment of equal-weight fractional-share portfolios,
tax-loss harvesting via correlated proxy swaps, and periodic
rebalancing scaffolding.  All state lives in an in-memory mock
database (MOCK_DB) that resets on restart.

Routes are exposed through an APIRouter mounted by the main app.
APScheduler runs a daily TLH sweep and a quarterly rebalance check
in the background.
"""

from __future__ import annotations

import json
import uuid
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yfinance as yf
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

try:
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError:  # Optional in stripped-down test/dev environments
    BackgroundScheduler = None

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Persistent database (JSON file)
# ---------------------------------------------------------------------------

_DB_FILE = Path(__file__).parent / "data" / "portfolios.json"


def _load_db() -> dict[str, dict]:
    """Load portfolio database from disk."""
    if _DB_FILE.exists():
        try:
            return json.loads(_DB_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Corrupt portfolios.json, starting fresh")
    return {}


def _save_db(db: dict[str, dict]):
    """Persist portfolio database to disk with atomic write (C5 fix)."""
    _DB_FILE.parent.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(_DB_FILE.parent), suffix=".tmp")
    try:
        os.write(fd, json.dumps(db, indent=2).encode())
        os.close(fd)
        os.replace(tmp, str(_DB_FILE))
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _migrate_db(db: dict) -> dict:
    """Migrate old single-portfolio format to multi-portfolio format."""
    if _USER_INDEX_KEY in db:
        return db
    new_db = {_USER_INDEX_KEY: {}}
    for key, value in db.items():
        if key.startswith("__"):
            continue
        if "positions" in value:
            pid = str(uuid.uuid4())
            tickers = []
            n = len(value.get("positions", []))
            for pos in value.get("positions", []):
                tickers.append({
                    "symbol": pos["ticker"],
                    "weight": round(1.0 / n, 6) if n > 0 else 1.0,
                    "custom_weight": False,
                })
            new_db[pid] = {
                "id": pid,
                "name": "Portfolio 1",
                "user_id": key,
                "created_at": value.get("deployed_at", datetime.now(timezone.utc).isoformat()),
                "source": "migrated",
                "deployed": True,
                "tickers": tickers,
                "positions": value.get("positions", []),
                "original_prompt": value.get("original_prompt", ""),
                "deployed_at": value.get("deployed_at", ""),
                "total_invested": value.get("total_invested", 0),
                "_harvest_log": value.get("_harvest_log", {}),
            }
            new_db[_USER_INDEX_KEY][key] = [pid]
        else:
            new_db[key] = value
    return new_db


def _get_user_portfolios(user_id: str) -> list[str]:
    index = MOCK_DB.get(_USER_INDEX_KEY, {})
    return index.get(user_id, [])

def _count_user_portfolios(user_id: str) -> int:
    return len(_get_user_portfolios(user_id))

def _recalculate_weights(tickers: list[dict]) -> list[dict]:
    if not tickers:
        return tickers
    has_custom = any(t.get("custom_weight", False) for t in tickers)
    if not has_custom:
        n = len(tickers)
        for t in tickers:
            t["weight"] = round(1.0 / n, 6)
        return tickers
    custom_tickers = [t for t in tickers if t.get("custom_weight", False)]
    non_custom = [t for t in tickers if not t.get("custom_weight", False)]
    custom_sum = sum(t["weight"] for t in custom_tickers)
    if custom_sum >= 1.0:
        scale = 0.8 / custom_sum
        for t in custom_tickers:
            t["weight"] = round(t["weight"] * scale, 6)
        remaining = 0.2
    else:
        remaining = 1.0 - custom_sum
    if non_custom:
        each = remaining / len(non_custom)
        for t in non_custom:
            t["weight"] = round(each, 6)
    return tickers


# C4 fix: thread lock for MOCK_DB (accessed by async handlers AND scheduler threads)
_db_lock = threading.Lock()
MAX_PORTFOLIOS = 20
WARN_PORTFOLIOS = 15
_USER_INDEX_KEY = "__user_index__"
MOCK_DB: dict[str, dict] = _migrate_db(_load_db())

# ---------------------------------------------------------------------------
# Proxy map for tax-loss harvesting (correlated alternatives)
# ---------------------------------------------------------------------------

PROXY_MAP: dict[str, str] = {
    # Big-tech peers
    "AAPL": "MSFT",
    "MSFT": "AAPL",
    "GOOGL": "META",
    "META": "GOOGL",
    "AMZN": "SHOP",
    "SHOP": "AMZN",
    "NVDA": "AMD",
    "AMD": "NVDA",
    # Financials
    "JPM": "BAC",
    "BAC": "JPM",
    "GS": "MS",
    "MS": "GS",
    "C": "WFC",
    "WFC": "C",
    # Energy
    "XOM": "CVX",
    "CVX": "XOM",
    "COP": "EOG",
    "EOG": "COP",
    # Consumer / retail
    "WMT": "COST",
    "COST": "WMT",
    "HD": "LOW",
    "LOW": "HD",
    # Telecom / media
    "DIS": "CMCSA",
    "CMCSA": "DIS",
    "NFLX": "PARA",
    "PARA": "NFLX",
    # Semiconductors (secondary pair)
    "INTC": "TXN",
    "TXN": "INTC",
    "QCOM": "AVGO",
    "AVGO": "QCOM",
    # Cloud / SaaS
    "CRM": "NOW",
    "NOW": "CRM",
    "SNOW": "DDOG",
    "DDOG": "SNOW",
    # Healthcare / pharma
    "JNJ": "PFE",
    "PFE": "JNJ",
    "UNH": "CI",
    "CI": "UNH",
    "ABBV": "MRK",
    "MRK": "ABBV",
    # Industrials
    "CAT": "DE",
    "DE": "CAT",
    "UPS": "FDX",
    "FDX": "UPS",
    # EV / auto
    "TSLA": "RIVN",
    "RIVN": "TSLA",
}

# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


class DeployRequest(BaseModel):
    """Body for the deploy endpoint."""

    tickers: list[str] = Field(..., min_length=1, max_length=50, description="List of stock tickers to buy (max 50)")
    amount: float = Field(..., gt=0, description="Total dollar amount to deploy")
    user_id: str = Field(..., min_length=1, description="Unique user identifier")
    original_prompt: str = Field(default="", description="The natural-language prompt that generated this portfolio")


class PositionOut(BaseModel):
    """A single portfolio position."""

    ticker: str
    shares: float
    purchase_price: float
    current_price: float
    allocated_amount: float
    gain_loss_pct: float


class PortfolioOut(BaseModel):
    """Full portfolio response."""

    user_id: str
    positions: list[PositionOut]
    original_prompt: str
    deployed_at: str
    total_invested: float


class HarvestResult(BaseModel):
    """Result of a tax-loss harvest run."""

    harvested: list[dict]
    orders: list[dict]


class RebalanceResult(BaseModel):
    """Result of a rebalance check."""

    user_id: str
    positions: list[PositionOut]
    total_invested: float
    rebalance_needed: bool


class CreatePortfolioRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1, max_length=100)
    tickers: list[str] = Field(default_factory=list)


class UpdatePortfolioRequest(BaseModel):
    name: str | None = None
    tickers: list[str] | None = None


class AddTickersRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1)


class WeightUpdate(BaseModel):
    symbol: str
    weight: float = Field(..., gt=0, le=1.0)


class UpdateWeightsRequest(BaseModel):
    weights: list[WeightUpdate]


class RemoveTickersRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1)


class DeployPortfolioRequest(BaseModel):
    amount: float = Field(..., gt=0)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _current_price(ticker: str) -> float:
    """Fetch the latest market price for *ticker* via yfinance.

    Tries ``fast_info`` first (cheaper API call), then falls back to
    ``info``, and finally to a hardcoded price map for common tickers.

    Raises ``ValueError`` if no price data can be retrieved.
    """
    t = yf.Ticker(ticker)

    # Try fast_info first (less likely to be rate-limited)
    try:
        fi = t.fast_info
        price = fi.last_price or fi.previous_close
        if price:
            return float(price)
    except Exception:
        pass

    # Try full info as fallback
    try:
        info = t.info
        price = (
            info.get("regularMarketPrice")
            or info.get("currentPrice")
            or info.get("regularMarketPreviousClose")
            or info.get("previousClose")
        )
        if price:
            return float(price)
    except Exception:
        pass

    # Hardcoded fallback for common tickers (mock prices)
    fallback_prices = {
        "AAPL": 240.0, "MSFT": 450.0, "GOOGL": 185.0, "AMZN": 210.0,
        "META": 580.0, "NVDA": 140.0, "TSLA": 340.0, "JPM": 210.0,
        "BAC": 38.0, "WFC": 55.0, "JNJ": 155.0, "UNH": 520.0,
        "PFE": 27.0, "XOM": 110.0, "CVX": 155.0, "HD": 380.0,
        "MCD": 290.0, "NKE": 95.0, "COST": 900.0, "WMT": 180.0,
        "DIS": 115.0, "NFLX": 680.0, "CRM": 300.0, "ADBE": 480.0,
        "INTC": 22.0, "AMD": 160.0, "QCOM": 170.0, "AVGO": 180.0,
        "SPY": 580.0, "QQQ": 500.0, "DIA": 430.0,
    }
    if ticker.upper() in fallback_prices:
        logger.warning("Using fallback price for %s (yfinance rate-limited)", ticker)
        return fallback_prices[ticker.upper()]

    raise ValueError(f"Could not retrieve price for {ticker}")


def deploy_portfolio(
    tickers: list[str],
    amount: float,
    user_id: str,
    original_prompt: str = "",
) -> dict:
    """Deploy an equal-weight fractional-share portfolio.

    Uses yfinance to look up current prices, divides *amount* equally
    across all *tickers*, computes fractional share counts, and
    persists the result in ``MOCK_DB``.

    Parameters
    ----------
    tickers:
        Stock symbols to purchase.
    amount:
        Total dollar amount to invest.
    user_id:
        Owner of the portfolio.
    original_prompt:
        The natural-language instruction that generated this portfolio
        (stored for future rebalance reference).

    Returns
    -------
    dict
        The newly created portfolio record.
    """
    allocation_per_ticker = amount / len(tickers)
    positions: list[dict] = []

    for ticker in tickers:
        price = _current_price(ticker.upper())
        shares = allocation_per_ticker / price
        positions.append(
            {
                "ticker": ticker.upper(),
                "shares": round(shares, 6),
                "purchase_price": round(price, 2),
                "current_price": round(price, 2),
                "allocated_amount": round(allocation_per_ticker, 2),
            }
        )

    portfolio = {
        "positions": positions,
        "original_prompt": original_prompt,
        "deployed_at": datetime.now(timezone.utc).isoformat(),
        "total_invested": round(amount, 2),
    }
    with _db_lock:
        MOCK_DB[user_id] = portfolio
        _save_db(MOCK_DB)
    logger.info("Deployed portfolio for %s: %d positions, $%.2f", user_id, len(positions), amount)
    return portfolio


def tax_loss_harvest(user_id: str) -> dict:
    """Run tax-loss harvesting for a user's portfolio.

    Scans every position.  If the current market price is more than
    10 % below the purchase price, the position is flagged and two
    mock orders are created:

    1. **SELL** — liquidate the losing position.
    2. **BUY**  — acquire the correlated proxy from ``PROXY_MAP``
       with the same dollar amount.

    The original position is updated in place so that subsequent
    calls reflect the swap.

    Parameters
    ----------
    user_id:
        Owner of the portfolio to harvest.

    Returns
    -------
    dict
        ``{"harvested": [...], "orders": [...]}`` listing every
        position that was harvested and the corresponding orders.

    Raises
    ------
    KeyError
        If *user_id* is not found in ``MOCK_DB``.
    """
    with _db_lock:
        if user_id not in MOCK_DB:
            raise KeyError(f"User '{user_id}' not found")

        portfolio = MOCK_DB[user_id]

    # I9 fix: track recently harvested tickers to prevent wash sales
    harvest_log = portfolio.get("_harvest_log", {})
    now = datetime.now(timezone.utc)

    harvested: list[dict] = []
    orders: list[dict] = []

    for position in portfolio["positions"]:
        ticker = position["ticker"]

        # Refresh current price
        try:
            live_price = _current_price(ticker)
            position["current_price"] = round(live_price, 2)
        except ValueError:
            live_price = position["current_price"]

        purchase = position["purchase_price"]
        if purchase == 0:
            continue

        loss_pct = (live_price - purchase) / purchase

        if loss_pct < -0.10:
            proxy = PROXY_MAP.get(ticker)
            if proxy is None:
                logger.warning("No proxy mapped for %s — skipping TLH", ticker)
                continue

            # I9 fix: check wash sale rule (31 calendar days)
            last_harvest = harvest_log.get(proxy)
            if last_harvest:
                days_since = (now - datetime.fromisoformat(last_harvest)).days
                if days_since < 31:
                    logger.info("Skipping %s -> %s swap: wash sale risk (%d days since last harvest)", ticker, proxy, days_since)
                    continue

            sell_proceeds = round(position["shares"] * live_price, 2)

            harvested.append(
                {
                    "ticker": ticker,
                    "loss_pct": round(loss_pct * 100, 2),
                    "shares_sold": position["shares"],
                    "proceeds": sell_proceeds,
                }
            )

            # Mock SELL order
            orders.append(
                {
                    "action": "SELL",
                    "ticker": ticker,
                    "shares": position["shares"],
                    "price": live_price,
                    "total": sell_proceeds,
                    "reason": f"TLH: {ticker} down {abs(round(loss_pct * 100, 2))}%",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )

            # Mock BUY order for proxy
            try:
                proxy_price = _current_price(proxy)
            except ValueError:
                proxy_price = live_price  # fallback: assume similar price

            proxy_shares = round(sell_proceeds / proxy_price, 6)

            orders.append(
                {
                    "action": "BUY",
                    "ticker": proxy,
                    "shares": proxy_shares,
                    "price": round(proxy_price, 2),
                    "total": sell_proceeds,
                    "reason": f"TLH proxy swap: {ticker} -> {proxy}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )

            # Update the position in place to reflect the swap
            position["ticker"] = proxy
            position["shares"] = proxy_shares
            position["purchase_price"] = round(proxy_price, 2)
            position["current_price"] = round(proxy_price, 2)
            position["allocated_amount"] = sell_proceeds

            # I9 fix: record harvest for wash sale tracking
            harvest_log[ticker] = now.isoformat()

    if harvested:
        portfolio["_harvest_log"] = harvest_log
        with _db_lock:
            _save_db(MOCK_DB)
    logger.info("TLH for %s: %d positions harvested, %d orders", user_id, len(harvested), len(orders))
    return {"harvested": harvested, "orders": orders}


def rebalance(user_id: str) -> dict:
    """Check whether a portfolio needs rebalancing.

    In a full implementation this would re-run the original prompt
    through the swarm engine, compute a target allocation, and diff
    it against current holdings.  For now it refreshes live prices,
    flags ``rebalance_needed`` if any position has drifted more than
    20 % from its equal-weight target, and returns the current state.

    Parameters
    ----------
    user_id:
        Owner of the portfolio.

    Returns
    -------
    dict
        Current portfolio state plus a ``rebalance_needed`` flag.

    Raises
    ------
    KeyError
        If *user_id* is not found in ``MOCK_DB``.
    """
    if user_id not in MOCK_DB:
        raise KeyError(f"User '{user_id}' not found")

    portfolio = MOCK_DB[user_id]
    positions = portfolio["positions"]
    n = len(positions)
    if n == 0:
        return {
            "user_id": user_id,
            "positions": [],
            "total_invested": portfolio["total_invested"],
            "rebalance_needed": False,
        }

    # Refresh prices
    total_value = 0.0
    for pos in positions:
        try:
            live = _current_price(pos["ticker"])
            pos["current_price"] = round(live, 2)
        except ValueError:
            live = pos["current_price"]
        total_value += pos["shares"] * live

    target_weight = 1.0 / n
    rebalance_needed = False

    enriched_positions: list[dict] = []
    for pos in positions:
        current_value = pos["shares"] * pos["current_price"]
        actual_weight = current_value / total_value if total_value > 0 else 0
        drift = abs(actual_weight - target_weight) / target_weight if target_weight > 0 else 0
        if drift > 0.20:
            rebalance_needed = True

        gain_loss_pct = (
            ((pos["current_price"] - pos["purchase_price"]) / pos["purchase_price"] * 100)
            if pos["purchase_price"] > 0
            else 0.0
        )
        enriched_positions.append({**pos, "gain_loss_pct": round(gain_loss_pct, 2)})

    return {
        "user_id": user_id,
        "positions": enriched_positions,
        "total_invested": portfolio["total_invested"],
        "rebalance_needed": rebalance_needed,
    }


# ---------------------------------------------------------------------------
# FastAPI Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


@router.post("/deploy", response_model=PortfolioOut)
async def deploy_endpoint(req: DeployRequest):
    """Deploy a new equal-weight portfolio from a list of tickers.

    Fetches live prices via yfinance, calculates fractional share
    counts, and stores the portfolio in the mock database.
    """
    try:
        result = deploy_portfolio(
            tickers=req.tickers,
            amount=req.amount,
            user_id=req.user_id,
            original_prompt=req.original_prompt,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Enrich positions with gain_loss_pct (0 % at deploy time)
    enriched = [{**p, "gain_loss_pct": 0.0} for p in result["positions"]]
    return {
        "user_id": req.user_id,
        "positions": enriched,
        "original_prompt": result["original_prompt"],
        "deployed_at": result["deployed_at"],
        "total_invested": result["total_invested"],
    }


@router.post("/harvest/{user_id}", response_model=HarvestResult)
async def harvest_endpoint(user_id: str):
    """Run tax-loss harvesting on a user's portfolio.

    Sells any position that has declined more than 10 % and buys
    the correlated proxy ticker from PROXY_MAP.
    """
    try:
        return tax_loss_harvest(user_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Portfolio not found for user '{user_id}'")


@router.get("/{user_id}", response_model=RebalanceResult)
async def get_portfolio_endpoint(user_id: str):
    """Return the current portfolio state for a user.

    Refreshes live prices and includes a ``rebalance_needed`` flag
    when any position has drifted more than 20 % from equal weight.
    """
    try:
        return rebalance(user_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Portfolio not found for user '{user_id}'")


# ---------------------------------------------------------------------------
# Multi-Portfolio CRUD endpoints
# ---------------------------------------------------------------------------

portfolios_router = APIRouter(prefix="/api/portfolios", tags=["portfolios"])


@portfolios_router.get("")
async def list_portfolios(user_id: str):
    pids = _get_user_portfolios(user_id)
    result = []
    for pid in pids:
        p = MOCK_DB.get(pid)
        if p:
            result.append({
                "id": p["id"],
                "name": p["name"],
                "user_id": p["user_id"],
                "created_at": p["created_at"],
                "source": p.get("source", ""),
                "deployed": p.get("deployed", False),
                "ticker_count": len(p.get("tickers", [])),
                "tickers": p.get("tickers", []),
                "positions": p.get("positions", []),
            })
    return result


@portfolios_router.post("")
async def create_portfolio(req: CreatePortfolioRequest):
    count = _count_user_portfolios(req.user_id)
    if count >= MAX_PORTFOLIOS:
        raise HTTPException(status_code=400, detail=f"Portfolio limit reached ({MAX_PORTFOLIOS})")
    pid = str(uuid.uuid4())
    n = len(req.tickers)
    tickers = [{"symbol": t.upper(), "weight": round(1.0 / n, 6) if n > 0 else 1.0, "custom_weight": False} for t in req.tickers]
    portfolio = {
        "id": pid,
        "name": req.name,
        "user_id": req.user_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "manual",
        "deployed": False,
        "tickers": tickers,
        "positions": [],
        "original_prompt": "",
        "deployed_at": "",
        "total_invested": 0,
        "_harvest_log": {},
    }
    with _db_lock:
        MOCK_DB[pid] = portfolio
        index = MOCK_DB.setdefault(_USER_INDEX_KEY, {})
        user_list = index.setdefault(req.user_id, [])
        user_list.append(pid)
        _save_db(MOCK_DB)
    warning = None
    if count + 1 >= WARN_PORTFOLIOS:
        warning = f"You have {count + 1}/{MAX_PORTFOLIOS} portfolios"
    return {"portfolio": portfolio, "warning": warning}


@portfolios_router.put("/{portfolio_id}")
async def update_portfolio(portfolio_id: str, req: UpdatePortfolioRequest):
    if portfolio_id not in MOCK_DB:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    p = MOCK_DB[portfolio_id]
    if req.name is not None:
        p["name"] = req.name
    if req.tickers is not None:
        n = len(req.tickers)
        p["tickers"] = [{"symbol": t.upper(), "weight": round(1.0 / n, 6) if n > 0 else 1.0, "custom_weight": False} for t in req.tickers]
    with _db_lock:
        _save_db(MOCK_DB)
    return p


@portfolios_router.delete("/{portfolio_id}")
async def delete_portfolio(portfolio_id: str):
    if portfolio_id not in MOCK_DB:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    p = MOCK_DB[portfolio_id]
    user_id = p["user_id"]
    with _db_lock:
        del MOCK_DB[portfolio_id]
        index = MOCK_DB.get(_USER_INDEX_KEY, {})
        if user_id in index:
            index[user_id] = [pid for pid in index[user_id] if pid != portfolio_id]
        _save_db(MOCK_DB)
    return {"deleted": True, "id": portfolio_id}


@portfolios_router.post("/{portfolio_id}/add")
async def add_tickers(portfolio_id: str, req: AddTickersRequest):
    if portfolio_id not in MOCK_DB:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    p = MOCK_DB[portfolio_id]
    existing_symbols = {t["symbol"] for t in p["tickers"]}
    added = []
    skipped = []
    for sym in req.symbols:
        s = sym.upper()
        if s in existing_symbols:
            skipped.append(s)
        else:
            p["tickers"].append({"symbol": s, "weight": 0, "custom_weight": False})
            existing_symbols.add(s)
            added.append(s)
    if added:
        _recalculate_weights(p["tickers"])
        with _db_lock:
            _save_db(MOCK_DB)
    return {
        "portfolio_id": portfolio_id,
        "added": added,
        "skipped": skipped,
        "skipped_message": ", ".join(f"{s} already in portfolio" for s in skipped) if skipped else None,
        "tickers": p["tickers"],
        "ticker_count": len(p["tickers"]),
    }


@portfolios_router.put("/{portfolio_id}/weights")
async def update_weights(portfolio_id: str, req: UpdateWeightsRequest):
    if portfolio_id not in MOCK_DB:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    p = MOCK_DB[portfolio_id]
    ticker_map = {t["symbol"]: t for t in p["tickers"]}
    for w in req.weights:
        s = w.symbol.upper()
        if s in ticker_map:
            ticker_map[s]["weight"] = w.weight
            ticker_map[s]["custom_weight"] = True
    _recalculate_weights(p["tickers"])
    with _db_lock:
        _save_db(MOCK_DB)
    return {"portfolio_id": portfolio_id, "tickers": p["tickers"]}


@portfolios_router.post("/{portfolio_id}/remove")
async def remove_tickers(portfolio_id: str, req: RemoveTickersRequest):
    if portfolio_id not in MOCK_DB:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    p = MOCK_DB[portfolio_id]
    symbols_upper = {s.upper() for s in req.symbols}
    before = len(p["tickers"])
    p["tickers"] = [t for t in p["tickers"] if t["symbol"] not in symbols_upper]
    removed = before - len(p["tickers"])
    if p["tickers"]:
        _recalculate_weights(p["tickers"])
    with _db_lock:
        _save_db(MOCK_DB)
    return {"portfolio_id": portfolio_id, "removed": removed, "tickers": p["tickers"]}


@portfolios_router.post("/{portfolio_id}/deploy")
async def deploy_portfolio_endpoint(portfolio_id: str, req: DeployPortfolioRequest):
    if portfolio_id not in MOCK_DB:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    p = MOCK_DB[portfolio_id]
    tickers = p.get("tickers", [])
    if not tickers:
        raise HTTPException(status_code=400, detail="Portfolio has no tickers")
    positions = []
    for t in tickers:
        allocation = req.amount * t["weight"]
        try:
            price = _current_price(t["symbol"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        shares = allocation / price
        positions.append({
            "ticker": t["symbol"],
            "shares": round(shares, 6),
            "purchase_price": round(price, 2),
            "current_price": round(price, 2),
            "allocated_amount": round(allocation, 2),
        })
    p["positions"] = positions
    p["deployed"] = True
    p["deployed_at"] = datetime.now(timezone.utc).isoformat()
    p["total_invested"] = round(req.amount, 2)
    with _db_lock:
        _save_db(MOCK_DB)
    enriched = [{**pos, "gain_loss_pct": 0.0} for pos in positions]
    return {
        "portfolio_id": portfolio_id,
        "name": p["name"],
        "positions": enriched,
        "deployed_at": p["deployed_at"],
        "total_invested": p["total_invested"],
    }


# ---------------------------------------------------------------------------
# APScheduler — background jobs
# ---------------------------------------------------------------------------

_scheduler_started = False


def _daily_tlh_sweep():
    """Scheduled job: run tax-loss harvesting for every user."""
    logger.info("Scheduled TLH sweep starting for %d user(s)", len(MOCK_DB))
    for uid in list(MOCK_DB.keys()):
        try:
            result = tax_loss_harvest(uid)
            if result["harvested"]:
                logger.info("TLH sweep: %s — harvested %d positions", uid, len(result["harvested"]))
        except Exception:
            logger.exception("TLH sweep failed for user %s", uid)


def _quarterly_rebalance_check():
    """Scheduled job: flag portfolios that need rebalancing."""
    logger.info("Scheduled rebalance check for %d user(s)", len(MOCK_DB))
    for uid in list(MOCK_DB.keys()):
        try:
            result = rebalance(uid)
            if result["rebalance_needed"]:
                logger.info("Rebalance needed for user %s", uid)
        except Exception:
            logger.exception("Rebalance check failed for user %s", uid)


def start_scheduler() -> Optional[BackgroundScheduler]:
    """Start the APScheduler background scheduler.

    Registers two jobs:

    * **daily_tlh** — runs ``_daily_tlh_sweep`` every 24 hours.
    * **quarterly_rebalance** — runs ``_quarterly_rebalance_check``
      every 90 days.

    The ``_scheduler_started`` module flag prevents double-starting
    when the module is re-imported (e.g. during hot-reload).

    Returns the scheduler instance, or ``None`` if already running.
    """
    global _scheduler_started
    if _scheduler_started:
        return None

    if BackgroundScheduler is None:
        logger.warning("APScheduler not installed; portfolio background jobs disabled")
        return None

    scheduler = BackgroundScheduler(daemon=True)

    scheduler.add_job(
        _daily_tlh_sweep,
        trigger="interval",
        hours=24,
        id="daily_tlh",
        name="Daily Tax-Loss Harvest Sweep",
        replace_existing=True,
    )

    scheduler.add_job(
        _quarterly_rebalance_check,
        trigger="interval",
        days=90,
        id="quarterly_rebalance",
        name="Quarterly Rebalance Check",
        replace_existing=True,
    )

    scheduler.start()
    _scheduler_started = True
    logger.info("Portfolio scheduler started (daily TLH + quarterly rebalance)")
    return scheduler


# Scheduler is started by server.py lifespan, not on import
# This avoids side effects when importing for tests
