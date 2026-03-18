"""Position router — FastAPI endpoints for derivative position management + WebSocket."""
import asyncio
import json
import logging
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from typing import Optional

logger = logging.getLogger("positions.router")

router = APIRouter(prefix="/api/derivatives", tags=["derivatives"])

# Module-level references set by init_position_system()
_pm = None  # PositionManager
_exit_engine = None  # ExitEngine
_ai_advisor = None  # AIAdvisor
_trade_journal = None  # TradeJournal
_ws_clients: list[WebSocket] = []


_auto_trader = None  # AutoTrader
_insider_tracker = None  # InsiderTracker


def init_position_system(pm, exit_engine=None, ai_advisor=None, trade_journal=None, auto_trader=None, insider_tracker=None):
    """Called by server.py lifespan to inject dependencies."""
    global _pm, _exit_engine, _ai_advisor, _trade_journal, _auto_trader, _insider_tracker
    _pm = pm
    _exit_engine = exit_engine
    _ai_advisor = ai_advisor
    _trade_journal = trade_journal
    _auto_trader = auto_trader
    _insider_tracker = insider_tracker


def _require_pm():
    if _pm is None:
        raise HTTPException(503, "Position system not initialized")
    return _pm


# ── Pydantic models ─────────────────────────────────────────────────────────

class CreatePackageRequest(BaseModel):
    name: str
    strategy_type: str
    legs: list[dict]
    exit_rules: list[dict] = []
    ai_strategy: str = "balanced"

class UpdatePackageRequest(BaseModel):
    name: Optional[str] = None
    exit_rules: Optional[list[dict]] = None
    ai_strategy: Optional[str] = None

class CreateRuleRequest(BaseModel):
    type: str
    params: dict

class UpdateRuleRequest(BaseModel):
    params: Optional[dict] = None
    active: Optional[bool] = None


# ── Package CRUD ─────────────────────────────────────────────────────────────

@router.get("/packages")
async def list_packages(status: Optional[str] = None):
    pm = _require_pm()
    return {"packages": pm.list_packages(status)}

@router.get("/packages/{pkg_id}")
async def get_package(pkg_id: str):
    pm = _require_pm()
    pkg = pm.get_package(pkg_id)
    if not pkg:
        raise HTTPException(404, "Package not found")
    return pkg

@router.post("/packages")
async def create_package(req: CreatePackageRequest):
    pm = _require_pm()
    from .position_manager import create_package as _create, create_leg, create_exit_rule

    pkg = _create(req.name, req.strategy_type)
    pkg["ai_strategy"] = req.ai_strategy

    for leg_data in req.legs:
        leg = create_leg(
            platform=leg_data["platform"],
            leg_type=leg_data["type"],
            asset_id=leg_data["asset_id"],
            asset_label=leg_data.get("asset_label", leg_data["asset_id"]),
            entry_price=leg_data["entry_price"],
            cost=leg_data["cost"],
            expiry=leg_data.get("expiry", "2026-12-31"),
        )
        pkg["legs"].append(leg)

    for rule_data in req.exit_rules:
        rule = create_exit_rule(rule_data["type"], rule_data.get("params", {}))
        pkg["exit_rules"].append(rule)

    result = await pm.execute_package(pkg)
    if result["success"]:
        await _broadcast({"event": "package_created", "package_id": pkg["id"]})
    return result

@router.patch("/packages/{pkg_id}")
async def update_package(pkg_id: str, req: UpdatePackageRequest):
    pm = _require_pm()
    pkg = pm.get_package(pkg_id)
    if not pkg:
        raise HTTPException(404, "Package not found")
    if req.name is not None:
        pkg["name"] = req.name
    if req.exit_rules is not None:
        from .position_manager import create_exit_rule
        pkg["exit_rules"] = [create_exit_rule(r["type"], r.get("params", {})) for r in req.exit_rules]
    if req.ai_strategy is not None:
        pkg["ai_strategy"] = req.ai_strategy
    pm.save()
    return {"success": True}

@router.delete("/packages/{pkg_id}")
async def delete_package(pkg_id: str):
    pm = _require_pm()
    pkg = pm.get_package(pkg_id)
    if not pkg:
        raise HTTPException(404, "Package not found")
    # Force-close all legs
    for leg in pkg["legs"]:
        if leg["status"] == "open":
            await pm.exit_leg(pkg_id, leg["leg_id"], trigger="force_close")
    pm.close_package(pkg_id)
    await _broadcast({"event": "package_closed", "package_id": pkg_id})
    return {"success": True}


# ── Exit actions ─────────────────────────────────────────────────────────────

@router.post("/packages/{pkg_id}/exit")
async def full_exit(pkg_id: str):
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
async def exit_leg(pkg_id: str, leg_id: str):
    pm = _require_pm()
    result = await pm.exit_leg(pkg_id, leg_id, trigger="manual_leg_exit")
    await _broadcast({"event": "position_update", "package_id": pkg_id, "leg_id": leg_id})
    return result

@router.post("/packages/{pkg_id}/confirm-stock")
async def confirm_stock(pkg_id: str):
    pm = _require_pm()
    pkg = pm.get_package(pkg_id)
    if not pkg:
        raise HTTPException(404, "Package not found")
    for leg in pkg["legs"]:
        if leg.get("status") == "advisory":
            leg["status"] = "confirmed"
    pm.save()
    return {"success": True}


# ── Rule CRUD ────────────────────────────────────────────────────────────────

@router.get("/packages/{pkg_id}/rules")
async def list_rules(pkg_id: str):
    pm = _require_pm()
    pkg = pm.get_package(pkg_id)
    if not pkg:
        raise HTTPException(404, "Package not found")
    return {"rules": pkg.get("exit_rules", [])}

@router.post("/packages/{pkg_id}/rules")
async def create_rule(pkg_id: str, req: CreateRuleRequest):
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
async def update_rule(pkg_id: str, rule_id: str, req: UpdateRuleRequest):
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
async def delete_rule(pkg_id: str, rule_id: str):
    pm = _require_pm()
    pkg = pm.get_package(pkg_id)
    if not pkg:
        raise HTTPException(404, "Package not found")
    pkg["exit_rules"] = [r for r in pkg.get("exit_rules", []) if r["rule_id"] != rule_id]
    pm.save()
    return {"success": True}


# ── Dashboard ────────────────────────────────────────────────────────────────

@router.get("/dashboard")
async def dashboard():
    pm = _require_pm()
    return pm.get_dashboard_stats()

@router.get("/dashboard/alerts")
async def get_alerts():
    pm = _require_pm()
    return {"alerts": [a for a in pm.alerts if a["status"] == "pending"]}

@router.post("/dashboard/alerts/{alert_id}/approve")
async def approve_alert(alert_id: str):
    pm = _require_pm()
    alert = next((a for a in pm.alerts if a["id"] == alert_id), None)
    if not alert:
        raise HTTPException(404, "Alert not found")
    alert["status"] = "approved"
    # Execute the proposed action
    pkg = pm.get_package(alert["package_id"])
    if pkg:
        for leg in pkg["legs"]:
            if leg["status"] == "open":
                await pm.exit_leg(pkg["id"], leg["leg_id"], trigger=f"alert_approved:{alert['trigger_name']}")
    pm.save()
    return {"success": True}

@router.post("/dashboard/alerts/{alert_id}/reject")
async def reject_alert(alert_id: str):
    pm = _require_pm()
    alert = next((a for a in pm.alerts if a["id"] == alert_id), None)
    if not alert:
        raise HTTPException(404, "Alert not found")
    alert["status"] = "rejected"
    pm.save()
    return {"success": True}


# ── Balances & Config ────────────────────────────────────────────────────────

@router.get("/balances")
async def get_balances():
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
    from .wallet_config import get_safe_config
    config = get_safe_config()
    config["exit_engine_running"] = _exit_engine is not None and _exit_engine._running if _exit_engine else False
    config["auto_trader_running"] = _auto_trader is not None and _auto_trader._running if _auto_trader else False
    config["insider_tracker_running"] = _insider_tracker is not None and _insider_tracker._running if _insider_tracker else False
    return config


# ── WebSocket ────────────────────────────────────────────────────────────────

@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    try:
        while True:
            # Keep connection alive, listen for pings
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


# ── Trade Journal ─────────────────────────────────────────────────────────────

@router.get("/journal")
async def get_journal(limit: int = 20):
    if not _trade_journal:
        return {"entries": [], "message": "Trade journal not initialized"}
    return {"entries": _trade_journal.get_recent(limit)}

@router.get("/journal/performance")
async def get_journal_performance(mode: Optional[str] = None, strategy: Optional[str] = None):
    if not _trade_journal:
        return {"total_trades": 0, "message": "Trade journal not initialized"}
    return _trade_journal.get_performance(mode=mode, strategy=strategy)


# ── Auto Trader ──────────────────────────────────────────────────────────────

@router.get("/auto-trader")
async def get_auto_trader_stats():
    if not _auto_trader:
        return {"running": False, "message": "Auto trader not initialized"}
    return _auto_trader.get_stats()


# ── Insider Tracker ──────────────────────────────────────────────────────────

@router.get("/insiders")
async def get_insider_stats():
    if not _insider_tracker:
        return {"running": False, "message": "Insider tracker not initialized"}
    return _insider_tracker.get_stats()

@router.get("/insiders/signals")
async def get_insider_signals(condition_id: Optional[str] = None):
    if not _insider_tracker:
        return {"signals": {}, "message": "Insider tracker not initialized"}
    if condition_id:
        signal = _insider_tracker.get_insider_signal(condition_id)
        return {"signals": {condition_id: signal} if signal["has_signal"] else {}}
    return {"signals": _insider_tracker.get_market_signals()}

@router.get("/insiders/alerts")
async def get_insider_alerts():
    if not _insider_tracker:
        return {"alerts": [], "message": "Insider tracker not initialized"}
    return {"alerts": _insider_tracker._movement_alerts[-20:]}

@router.get("/insiders/accuracy")
async def get_insider_accuracy():
    if not _insider_tracker:
        return {"wallets": [], "message": "Insider tracker not initialized"}
    proven = sorted(
        [a for a in _insider_tracker._wallet_accuracy.values() if a["total"] >= 3],
        key=lambda a: a["accuracy"], reverse=True
    )
    return {"wallets": proven[:20], "total_tracked": len(_insider_tracker._wallet_accuracy)}
