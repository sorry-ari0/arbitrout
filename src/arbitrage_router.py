"""Arbitrage API router — all /api/arbitrage/* endpoints."""
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from adapters.registry import AdapterRegistry
from arbitrage_engine import ArbitrageScanner, load_saved, save_market, unsave_market
from event_matcher import add_manual_link, remove_manual_link

logger = logging.getLogger("arbitrage_router")

router = APIRouter(prefix="/api/arbitrage", tags=["arbitrage"])


# ============================================================
# GLOBAL STATE (set by server.py on startup)
# ============================================================
_scanner: ArbitrageScanner | None = None
_registry: AdapterRegistry | None = None


def init_scanner(registry: AdapterRegistry):
    """Called by server.py to initialize the scanner."""
    global _scanner, _registry
    _registry = registry
    _scanner = ArbitrageScanner(registry)


def get_scanner() -> ArbitrageScanner:
    if _scanner is None:
        raise RuntimeError("Scanner not initialized")
    return _scanner


# ============================================================
# REQUEST MODELS
# ============================================================
class LinkRequest(BaseModel):
    event_ids: list[str]  # ["kalshi:k1", "polymarket:p1"]


class SaveRequest(BaseModel):
    match_id: str
    canonical_title: str = ""
    category: str = ""

class ExecuteRequest(BaseModel):
    opportunity_id: str
    amount_usd: float
    auto_confirm: bool = False


# ============================================================
# ENDPOINTS
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


@router.post("/execute")
async def execute_arbitrage(req: ExecuteRequest):
    """Execute an arbitrage opportunity."""
    scanner = get_scanner()
    # Placeholder for re-fetching prices, verifying spread, calculating profit after fees
    # In a real implementation, this would involve more complex logic to
    # interact with platform adapters to get current prices, check liquidity,
    # calculate exact allocations, and then place trades.
    opportunity = next((o for o in scanner.get_opportunities() if o.get('id') == req.opportunity_id), None)

    if not opportunity:
        raise HTTPException(status_code=404, detail="Opportunity not found or no longer valid.")

    # Simulate pre-flight check and execution
    logger.info(f"Pre-flight check for opportunity {req.opportunity_id} with amount ${req.amount_usd:.2f}")
    current_profit_pct = opportunity.get('profit_pct', 0)
    expected_profit_usd = req.amount_usd * (current_profit_pct / 100)

    if current_profit_pct <= 0:
        raise HTTPException(status_code=400, detail="Spread no longer exists or is negative.")

    if not req.auto_confirm:
        # In a real scenario, this would return a confirmation prompt to the user
        # or require a separate confirmation step in a multi-step execution flow.
        logger.info("Manual confirmation required, but not implemented in this mock.")

    # Simulate execution
    logger.info(f"Executing trade for opportunity {req.opportunity_id} with ${req.amount_usd:.2f}...")
    # This is where actual trade placement would happen via platform adapters.

    return JSONResponse(content={
        "status": "completed",
        "opportunity_id": req.opportunity_id,
        "amount_usd": req.amount_usd,
        "expected_profit_pct": current_profit_pct,
        "expected_profit_usd": expected_profit_usd,
        "message": "Trade execution simulated successfully."
    })


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
