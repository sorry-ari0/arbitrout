"""Arbitrage API router — all /api/arbitrage/* endpoints."""
import logging
import random # NEW: For sentiment analysis simulation
import time # NEW: For trending markets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
import csv
import io

from adapters.registry import AdapterRegistry
from arbitrage_engine import ArbitrageScanner, load_saved, save_market, unsave_market, load_watchlist_items, add_to_watchlist, remove_from_watchlist
from event_matcher import add_manual_link, remove_manual_link
from execution.crypto_hedger import CryptoHedger
from arbitrage_history import get_opportunity_history, get_market_history # NEW: Import market history getter
from utils.forex_rates import fetch_forex_rates

logger = logging.getLogger("arbitrage_router")

router = APIRouter(prefix="/api/arbitrage", tags=["arbitrage"])
forex_router = APIRouter(prefix="/api/forex", tags=["forex"])

# ============================================================
# GLOBAL STATE (set by server.py on startup)
# ============================================================
_scanner: ArbitrageScanner | None = None
_registry: AdapterRegistry | None = None
_hedger: CryptoHedger | None = None


def init_scanner(registry: AdapterRegistry, decision_logger=None):
    """Called by server.py to initialize the scanner."""
    global _scanner, _registry, _hedger
    _registry = registry
    _scanner = ArbitrageScanner(registry, decision_logger=decision_logger)
    _hedger = CryptoHedger(registry)


def get_scanner() -> ArbitrageScanner:
    if _scanner is None:
        raise RuntimeError("Scanner not initialized")
    return _scanner


def get_hedger() -> CryptoHedger:
    if _hedger is None:
        raise RuntimeError("CryptoHedger not initialized")
    return _hedger


# ============================================================
# REQUEST MODELS
# ============================================================
class LinkRequest(BaseModel):
    event_ids: list[str]  # ["kalshi:k1", "polymarket:p1"]


class SaveRequest(BaseModel):
    match_id: str
    canonical_title: str = ""
    category: str = ""

class WatchlistRequest(BaseModel): # NEW: Watchlist Request Model
    platform: str
    event_id: str
    title: str = ""
    category: str = ""


# ============================================================
# ARBITRAGE ENDPOINTS
# ============================================================
@router.get("/opportunities")
async def get_opportunities():
    """Current arbitrage opportunities, sorted by profit %."""
    scanner = get_scanner()
    return JSONResponse(content=scanner.get_opportunities())


@router.get("/events")
async def get_events():
    """All matched events across platforms."""
    scanner = get_scanner()
    return JSONResponse(content=scanner.get_events())


@router.get("/feed")
async def get_feed():
    """Recent odds changes across all platforms."""
    scanner = get_scanner()
    return JSONResponse(content=scanner.get_feed())


@router.get("/platforms")
async def get_platforms():
    """Platform adapter status (up/down/error)."""
    if _registry is None:
        return JSONResponse(content=[])
    return JSONResponse(content=_registry.get_all_status())


@router.post("/scan")
async def trigger_scan():
    """Manually trigger a full scan cycle."""
    scanner = get_scanner()
    result = await scanner.scan()
    return JSONResponse(content=result)


@router.post("/link")
async def create_link(req: LinkRequest):
    """Manually link events across platforms."""
    if len(req.event_ids) < 2:
        return JSONResponse(content={"error": "Need at least 2 event_ids"}, status_code=400)
    link = add_manual_link(req.event_ids)
    return JSONResponse(content=link)


@router.delete("/link/{link_id}")
async def delete_link(link_id: str):
    """Remove a manual link."""
    removed = remove_manual_link(link_id)
    return JSONResponse(content={"removed": removed, "link_id": link_id})


@router.get("/saved")
async def get_saved():
    """Get bookmarked markets."""
    return JSONResponse(content=load_saved())


@router.post("/saved")
async def add_saved(req: SaveRequest):
    """Bookmark a matched event."""
    saved = save_market(req.model_dump())
    return JSONResponse(content=saved)


@router.delete("/saved/{match_id}")
async def delete_saved(match_id: str):
    """Remove a bookmark."""
    saved = unsave_market(match_id)
    return JSONResponse(content=saved)


@router.get("/hedge-packages")
async def get_hedge_packages():
    """Current crypto hedge packages with P&L scenarios."""
    hedger = get_hedger()
    packages = await hedger.find_hedge_packages()
    return JSONResponse(content=packages)


@router.get("/history/{match_id}")
async def get_opportunity_history_endpoint(match_id: str):
    """Get historical profit data for a specific arbitrage opportunity."""
    history = get_opportunity_history(match_id)
    return JSONResponse(content=history)


@router.get("/market_history/{platform}/{event_id}") # NEW: Endpoint for individual market history
async def get_market_history_endpoint(platform: str, event_id: str):
    """Get historical price data for a specific individual market."""
    history = get_market_history(platform, event_id)
    return JSONResponse(content=history)


@router.get("/opportunities/export/csv")
async def export_opportunities_csv():
    """Export current arbitrage opportunities to a CSV file."""
    scanner = get_scanner()
    opportunities = scanner.get_opportunities()

    # Prepare CSV data
    output = io.StringIO()
    writer = csv.writer(output)

    # CSV Header
    header = [
        "Match ID", "Canonical Title", "Category", "Is Synthetic",
        "Buy YES Platform", "Buy YES Price", "Buy NO Platform", "Buy NO Price",
        "Spread (%)", "Net Profit (%)", "Combined Volume", "Confidence", "URL"
    ]
    writer.writerow(header)

    # CSV Rows
    for opp in opportunities:
        row = [
            opp.get("match_id"),
            opp.get("canonical_title") or opp.get("matched_event", {}).get("canonical_title"),
            opp.get("matched_event", {}).get("category"),
            opp.get("is_synthetic"),
            opp.get("buy_yes_platform"),
            opp.get("buy_yes_price"),
            opp.get("buy_no_platform"),
            opp.get("buy_no_price"),
            round(opp.get("profit_pct", 0), 2),
            round(opp.get("net_profit_pct", 0), 2),
            opp.get("combined_volume"),
            opp.get("confidence"),
            (opp.get("matched_event", {}).get("markets", [{}])[0].get("url") if opp.get("matched_event") else "")
        ]
        writer.writerow(row)

    csv_string = output.getvalue()
    
    # Return as a downloadable file
    return Response(
        content=csv_string,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=arbitrage_opportunities.csv"}
    )


@router.get("/news") # NEW: Endpoint for news, modified for sentiment
async def get_news_feed():
    """Get recent news items with sentiment analysis."""
    # This is a placeholder for actual news fetching and sentiment analysis
    # In a real system, this would come from a dedicated news processing module
    # and use an NLP library for sentiment.
    
    # Simulate news items (replace with actual news source)
    mock_news = [
        {
            "timestamp": time.time() - random.randint(0, 3600),
            "headline": "Tech giant announces record quarterly earnings, exceeding expectations",
            "url": "https://example.com/news1",
            "is_breaking": False,
            "matched_markets": [{"title": "AAPL Stock Price", "match_id": "auto-abcd123"}],
            "sentiment": "Positive" # NEW: Sentiment field
        },
        {
            "timestamp": time.time() - random.randint(0, 3600),
            "headline": "Unexpected inflation data sparks market uncertainty, indexes drop sharply",
            "url": "https://example.com/news2",
            "is_breaking": True,
            "matched_markets": [{"title": "S&P 500 End of Year", "match_id": "auto-efgh456"}],
            "sentiment": "Negative" # NEW: Sentiment field
        },
        {
            "timestamp": time.time() - random.randint(0, 3600),
            "headline": "Company X and Company Y announce merger discussions",
            "url": "https://example.com/news3",
            "is_breaking": False,
            "matched_markets": [],
            "sentiment": "Neutral" # NEW: Sentiment field
        },
        {
            "timestamp": time.time() - random.randint(0, 3600),
            "headline": "New crypto regulation bill proposed in Congress",
            "url": "https://example.com/news4",
            "is_breaking": True,
            "matched_markets": [{"title": "BTC Price Above $70K", "match_id": "auto-ijkl789"}],
            "sentiment": "Neutral" # NEW: Sentiment field
        },
    ]

    # Assign sentiment randomly for demo if not already set (e.g. from real service)
    sentiments = ["Positive", "Negative", "Neutral"]
    for item in mock_news:
        if "sentiment" not in item:
            item["sentiment"] = random.choice(sentiments)
    
    mock_news.sort(key=lambda x: x["timestamp"], reverse=True)
    return JSONResponse(content=mock_news)


@router.get("/watchlist") # NEW: Watchlist endpoints
async def get_watchlist():
    """Get personal watchlist of individual markets."""
    return JSONResponse(content=load_watchlist_items())


@router.post("/watchlist")
async def add_watchlist(req: WatchlistRequest):
    """Add an individual market to the watchlist."""
    watchlist_item = req.model_dump()
    added = add_to_watchlist(watchlist_item)
    return JSONResponse(content=added)


@router.delete("/watchlist/{platform}/{event_id}")
async def delete_watchlist(platform: str, event_id: str):
    """Remove an individual market from the watchlist."""
    removed = remove_from_watchlist(platform, event_id)
    return JSONResponse(content={"removed": removed, "platform": platform, "event_id": event_id})


@router.get("/trending") # NEW: Trending markets endpoint
async def get_trending():
    """Get trending markets based on recent activity."""
    scanner = get_scanner()
    trending_markets = scanner.get_trending_markets()
    return JSONResponse(content=trending_markets)


@router.get("/scan-history")
async def get_scan_history(limit: int = 20):
    """Return recent scan results with timing breakdown."""
    scanner = get_scanner()
    return JSONResponse(content=scanner.get_scan_history(limit=min(limit, 100)))


@router.get("/scan-stats")
async def get_scan_stats():
    """Aggregate stats across recent scan history."""
    scanner = get_scanner()
    return JSONResponse(content=scanner.get_scan_stats())


# ============================================================
# FOREX ENDPOINTS
# ============================================================
@forex_router.get("/rates")
async def get_forex_rates():
    """Get current forex rates (USD base)."""
    rates = await fetch_forex_rates()
    return JSONResponse(content=rates)

# ============================================================
# WEBSOCKET
# ============================================================
_ws_clients: set[WebSocket] = set()
MAX_WS_CLIENTS = 20


@router.websocket("/ws")
async def ws_arbitrage(websocket: WebSocket):
    """WebSocket for real-time arbitrage updates.

    On connect: sends full state.
    On each scan: pushes diff (new opportunities, price changes).
    Client sends: {"action": "scan"} to trigger manual scan,
                  {"action": "subscribe"} to start receiving.
    """
    if len(_ws_clients) >= MAX_WS_CLIENTS:
        await websocket.close(code=1013, reason="Max connections")
        return

    await websocket.accept()
    _ws_clients.add(websocket)
    logger.info("Arbitrage WS client connected (%d total)", len(_ws_clients))

    try:
        scanner = get_scanner()

        # Send initial state
        await websocket.send_json({
            "type": "init",
            "opportunities": scanner.get_opportunities(),
            "feed": scanner.get_feed(),
            "events_count": len(scanner.get_events()),
            "platforms": _registry.get_all_status() if _registry else [],
        })

        while True:
            msg = await websocket.receive_json()
            action = msg.get("action", "")

            if action == "scan":
                result = await scanner.scan()
                await websocket.send_json({
                    "type": "scan_result",
                    "summary": result,
                    "opportunities": scanner.get_opportunities(),
                    "feed": scanner.get_feed(),
                })
            elif action == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("Arbitrage WS error: %s", exc)
    finally:
        _ws_clients.discard(websocket)
        logger.info("Arbitrage WS client disconnected (%d remain)", len(_ws_clients))


async def broadcast_update(data: dict):
    """Broadcast scan results to all connected WS clients."""
    dead: list[WebSocket] = []
    for ws in _ws_clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


async def broadcast_error(error_data: dict):
    """Broadcast system errors to all connected WS clients."""
    msg = {"type": "system_error", **error_data, "timestamp": time.time()}
    dead: list[WebSocket] = []
    for ws in _ws_clients:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)

