"""Arbitrage API router — all /api/arbitrage/* endpoints."""
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
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


# ============================================================
# ENDPOINTS
# ============================================================
@router.get("/opportunities")
async def get_opportunities(min_profit: float = Query(0, ge=0)):
    """Current arbitrage opportunities, sorted by profit %."""
    scanner = get_scanner()
    opportunities = scanner.get_opportunities()
    filtered_opportunities = [op for op in opportunities if op['profit_pct'] >= min_profit]
    return JSONResponse(content=filtered_opportunities)


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
