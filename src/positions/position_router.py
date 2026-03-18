"""Position router — FastAPI endpoints for derivative position management + WebSocket."""
import asyncio
import json
import logging
import os
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from typing import Optional

logger = logging.getLogger("positions.router")

router = APIRouter(prefix="/api/derivatives", tags=["derivatives"])

# Module-level references set by init_position_system()
_pm = None  # PositionManager
_exit_engine = None  # ExitEngine
_ai_advisor = None  # AIAdvisor
_ws_clients: list[WebSocket] = []

# ── Auth ────────────────────────────────────────────────────────────────────
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def _verify_api_key(api_key: str = Depends(_api_key_header)):
    """Verify API key. Skip in dev mode."""
    configured_key = os.environ.get("LOBSTERMINAL_API_KEY", "dev-local-only")
    if configured_key == "dev-local-only":
        return
    if not api_key or api_key != configured_key:
        raise HTTPException(401, "Invalid or missing API key")


def init_position_system(pm, exit_engine=None, ai_advisor=None):
    """Called by server.py lifespan to inject dependencies."""
    global _pm, _exit_engine, _ai_advisor
    _pm = pm
    _exit_engine = exit_engine
    _ai_advisor = ai_advisor


def _require_pm():
    if _pm is None:
        raise HTTPException(503, "Position system not initialized")
    return _pm


# ── Pydantic models ─────────────────────────────────────────────────────────

VALID_STRATEGY_TYPES = {"spot_plus_hedge", "cross_platform_arb", "pure_prediction"}
VALID_LEG_TYPES = {"spot_buy", "spot_sell", "prediction_yes", "prediction_no", "stock_advisory"}
VALID_PLATFORMS = {"polymarket", "kalshi", "coinbase_spot", "predictit", "robinhood"}

class LegRequest(BaseModel):
    platform: str = Field(..., description="Platform name")
    type: str = Field(..., description="Leg type")
    asset_id: str = Field(..., min_length=1)
    asset_label: str = ""
    entry_price: float = Field(..., gt=0)
    cost: float = Field(..., gt=0)
    expiry: str = "2026-12-31"

class RuleRequest(BaseModel):
    type: str = Field(..., min_length=1)
    params: dict = Field(default_factory=dict)

class CreatePackageRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    strategy_type: str
    legs: list[LegRequest] = Field(..., min_length=1)
    exit_rules: list[RuleRequest] = []
    ai_strategy: str = "balanced"

class UpdatePackageRequest(BaseModel):
    name: Optional[str] = None
    exit_rules: Optional[list[RuleRequest]] = None
    ai_strategy: Optional[str] = None

class CreateRuleRequest(BaseModel):
    type: str = Field(..., min_length=1)
    params: dict = Field(default_factory=dict)

class UpdateRuleRequest(BaseModel):
    params: Optional[dict] = None
    active: Optional[bool] = None

class ConfirmStockRequest(BaseModel):
    entry_price: float = Field(..., gt=0)
    quantity: float = Field(..., gt=0)


def _validate_create_request(req: CreatePackageRequest):
    """Validate package creation inputs."""
    if req.strategy_type not in VALID_STRATEGY_TYPES:
        raise HTTPException(400, f"Invalid strategy_type: {req.strategy_type}. Must be one of {VALID_STRATEGY_TYPES}")
    for i, leg in enumerate(req.legs):
        if leg.platform not in VALID_PLATFORMS:
            raise HTTPException(400, f"Leg {i}: invalid platform '{leg.platform}'. Must be one of {VALID_PLATFORMS}")
        if leg.type not in VALID_LEG_TYPES:
            raise HTTPException(400, f"Leg {i}: invalid type '{leg.type}'. Must be one of {VALID_LEG_TYPES}")


# ── Package CRUD ─────────────────────────────────────────────────────────────

@router.get("/packages")
async def list_packages(status: Optional[str] = None, _=Depends(_verify_api_key)):
    pm = _require_pm()
    return {"packages": pm.list_packages(status)}

@router.get("/packages/{pkg_id}")
async def get_package(pkg_id: str, _=Depends(_verify_api_key)):
    pm = _require_pm()
    pkg = pm.get_package(pkg_id)
    if not pkg:
        raise HTTPException(404, "Package not found")
    return pkg

@router.post("/packages")
async def create_package(req: CreatePackageRequest, _=Depends(_verify_api_key)):
    pm = _require_pm()
    _validate_create_request(req)
    from .position_manager import create_package as _create, create_leg, create_exit_rule

    pkg = _create(req.name, req.strategy_type)
    pkg["ai_strategy"] = req.ai_strategy

    for leg_data in req.legs:
        leg = create_leg(
            platform=leg_data.platform,
            leg_type=leg_data.type,
            asset_id=leg_data.asset_id,
            asset_label=leg_data.asset_label or leg_data.asset_id,
            entry_price=leg_data.entry_price,
            cost=leg_data.cost,
            expiry=leg_data.expiry,
        )
        pkg["legs"].append(leg)

    for rule_data in req.exit_rules:
        rule = create_exit_rule(rule_data.type, rule_data.params)
        pkg["exit_rules"].append(rule)

    result = await pm.execute_package(pkg)
    if result["success"]:
        await _broadcast({"event": "package_created", "package_id": pkg["id"]})
    return result

@router.patch("/packages/{pkg_id}")
async def update_package(pkg_id: str, req: UpdatePackageRequest, _=Depends(_verify_api_key)):
    pm = _require_pm()
    pkg = pm.get_package(pkg_id)
    if not pkg:
        raise HTTPException(404, "Package not found")
    if req.name is not None:
        pkg["name"] = req.name
    if req.exit_rules is not None:
        from .position_manager import create_exit_rule
        pkg["exit_rules"] = [create_exit_rule(r.type, r.params) for r in req.exit_rules]
    if req.ai_strategy is not None:
        pkg["ai_strategy"] = req.ai_strategy
    pm.save()
    return {"success": True}

@router.delete("/packages/{pkg_id}")
async def delete_package(pkg_id: str, _=Depends(_verify_api_key)):
    pm = _require_pm()
    pkg = pm.get_package(pkg_id)
    if not pkg:
        raise HTTPException(404, "Package not found")
    for leg in pkg["legs"]:
        if leg["status"] == "open":
            await pm.exit_leg(pkg_id, leg["leg_id"], trigger="force_close")
    pm.close_package(pkg_id)
    await _broadcast({"event": "package_closed", "package_id": pkg_id})
    return {"success": True}


# ── Exit actions ─────────────────────────────────────────────────────────────

@router.post("/packages/{pkg_id}/exit")
async def full_exit(pkg_id: str, _=Depends(_verify_api_key)):
    pm = _require_pm()
    pkg = pm.get_package(pkg_id)
    if not pkg:
        raise HTTPException(404, "Package not found")
    results = []
    for leg in pkg["legs"]:
        if leg["status"] == "open":
            r = await pm.exit_leg(pkg_id, leg["leg_id"], trigger="manual_full_exit")
            results.append(r)
    await _broadcast({"event": "package_closed", "package_id": pkg_id})
    return {"results": results}

@router.post("/packages/{pkg_id}/exit-leg/{leg_id}")
async def exit_leg(pkg_id: str, leg_id: str, _=Depends(_verify_api_key)):
    pm = _require_pm()
    result = await pm.exit_leg(pkg_id, leg_id, trigger="manual_leg_exit")
    await _broadcast({"event": "position_update", "package_id": pkg_id, "leg_id": leg_id})
    return result

@router.post("/packages/{pkg_id}/confirm-stock")
async def confirm_stock(pkg_id: str, req: ConfirmStockRequest, _=Depends(_verify_api_key)):
    pm = _require_pm()
    pkg = pm.get_package(pkg_id)
    if not pkg:
        raise HTTPException(404, "Package not found")
    for leg in pkg["legs"]:
        if leg.get("status") == "advisory":
            leg["status"] = "confirmed"
            leg["entry_price"] = req.entry_price
            leg["quantity"] = req.quantity
            leg["cost"] = req.entry_price * req.quantity
    pm.save()
    return {"success": True}


# ── Rule CRUD ────────────────────────────────────────────────────────────────

@router.get("/packages/{pkg_id}/rules")
async def list_rules(pkg_id: str, _=Depends(_verify_api_key)):
    pm = _require_pm()
    pkg = pm.get_package(pkg_id)
    if not pkg:
        raise HTTPException(404, "Package not found")
    return {"rules": pkg.get("exit_rules", [])}

@router.post("/packages/{pkg_id}/rules")
async def create_rule(pkg_id: str, req: CreateRuleRequest, _=Depends(_verify_api_key)):
    pm = _require_pm()
    pkg = pm.get_package(pkg_id)
    if not pkg:
        raise HTTPException(404, "Package not found")
    from .position_manager import create_exit_rule
    rule = create_exit_rule(req.type, req.params)
    pkg["exit_rules"].append(rule)
    pm.save()
    return rule

@router.patch("/packages/{pkg_id}/rules/{rule_id}")
async def update_rule(pkg_id: str, rule_id: str, req: UpdateRuleRequest, _=Depends(_verify_api_key)):
    pm = _require_pm()
    pkg = pm.get_package(pkg_id)
    if not pkg:
        raise HTTPException(404, "Package not found")
    rule = next((r for r in pkg.get("exit_rules", []) if r["rule_id"] == rule_id), None)
    if not rule:
        raise HTTPException(404, "Rule not found")
    if req.params is not None:
        rule["params"].update(req.params)
    if req.active is not None:
        rule["active"] = req.active
    pm.save()
    return rule

@router.delete("/packages/{pkg_id}/rules/{rule_id}")
async def delete_rule(pkg_id: str, rule_id: str, _=Depends(_verify_api_key)):
    pm = _require_pm()
    pkg = pm.get_package(pkg_id)
    if not pkg:
        raise HTTPException(404, "Package not found")
    pkg["exit_rules"] = [r for r in pkg.get("exit_rules", []) if r["rule_id"] != rule_id]
    pm.save()
    return {"success": True}


# ── Dashboard ────────────────────────────────────────────────────────────────

@router.get("/dashboard")
async def dashboard(_=Depends(_verify_api_key)):
    pm = _require_pm()
    return pm.get_dashboard_stats()

@router.get("/dashboard/alerts")
async def get_alerts(_=Depends(_verify_api_key)):
    pm = _require_pm()
    return {"alerts": [a for a in pm.alerts if a["status"] == "pending"]}

@router.post("/dashboard/alerts/{alert_id}/approve")
async def approve_alert(alert_id: str, _=Depends(_verify_api_key)):
    pm = _require_pm()
    alert = next((a for a in pm.alerts if a["id"] == alert_id), None)
    if not alert:
        raise HTTPException(404, "Alert not found")
    alert["status"] = "approved"
    pkg = pm.get_package(alert["package_id"])
    if pkg:
        for leg in pkg["legs"]:
            if leg["status"] == "open":
                await pm.exit_leg(pkg["id"], leg["leg_id"], trigger=f"alert_approved:{alert['trigger_name']}")
    pm.save()
    return {"success": True}

@router.post("/dashboard/alerts/{alert_id}/reject")
async def reject_alert(alert_id: str, _=Depends(_verify_api_key)):
    pm = _require_pm()
    alert = next((a for a in pm.alerts if a["id"] == alert_id), None)
    if not alert:
        raise HTTPException(404, "Alert not found")
    alert["status"] = "rejected"
    pm.save()
    return {"success": True}


# ── Balances & Config ────────────────────────────────────────────────────────

@router.get("/balances")
async def get_balances(_=Depends(_verify_api_key)):
    pm = _require_pm()
    balances = {}
    for name, executor in pm.executors.items():
        try:
            bal = await executor.get_balance()
            balances[name] = {"available": bal.available, "total": bal.total}
        except Exception as e:
            balances[name] = {"error": str(e)}
    return {"balances": balances}

@router.get("/config")
async def get_config():
    from .wallet_config import is_paper_mode, get_configured_platforms, has_anthropic_key
    return {
        "paper_mode": is_paper_mode(),
        "platforms": get_configured_platforms(),
        "ai_enabled": has_anthropic_key(),
        "exit_engine_running": _exit_engine is not None and _exit_engine._running if _exit_engine else False,
    }


# ── WebSocket ────────────────────────────────────────────────────────────────

@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


async def _broadcast(data: dict):
    """Broadcast event to all connected WebSocket clients."""
    msg = json.dumps(data)
    disconnected = []
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in _ws_clients:
            _ws_clients.remove(ws)
