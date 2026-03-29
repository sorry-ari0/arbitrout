"""Position router — FastAPI endpoints for derivative position management + WebSocket."""
import asyncio
import json
import logging
import time
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, Security, WebSocket, WebSocketDisconnect
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from typing import Optional
import os

logger = logging.getLogger("positions.router")

# Auth: reuse the same API key check as server.py
# NOTE: _API_KEY is read at import time. For live mode auto-generated keys,
# server.py sets os.environ before import, or we re-read dynamically.
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _get_api_key() -> str:
    """Get current API key (supports runtime changes from auto-generation)."""
    return os.environ.get("LOBSTERMINAL_API_KEY", "dev-local-only")


async def _verify_api_key(api_key: str = Security(_api_key_header)):
    current_key = _get_api_key()
    if current_key == "dev-local-only":
        return
    if not api_key or api_key != current_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

router = APIRouter(prefix="/api/derivatives", tags=["derivatives"], dependencies=[Depends(_verify_api_key)])

# Module-level references set by init_position_system()
_pm = None  # PositionManager
_exit_engine = None  # ExitEngine
_ai_advisor = None  # AIAdvisor
_trade_journal = None  # TradeJournal
_ws_clients: list[WebSocket] = []

_auto_trader = None  # AutoTrader
_insider_tracker = None  # InsiderTracker
_btc_sniper = None  # BtcSniper
_market_maker = None  # MarketMaker
_news_scanner = None  # NewsScanner

# Kill switch state
_trading_paused = False
_KILL_SWITCH_FILE = Path(__file__).parent.parent / "data" / "positions" / "kill_switch.json"


def _persist_kill_switch(active: bool, reason: str = "manual"):
    """Write/delete kill switch state file for persistence across restarts."""
    try:
        if active:
            _KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
            _KILL_SWITCH_FILE.write_text(json.dumps({
                "active": True, "activated_at": time.time(), "reason": reason
            }))
        else:
            _KILL_SWITCH_FILE.unlink(missing_ok=True)
    except Exception as e:
        logger.error("Failed to persist kill switch state: %s", e)


def is_kill_switch_active() -> bool:
    """Check if kill switch was activated in a previous session."""
    return _KILL_SWITCH_FILE.exists()


def init_position_system(pm, exit_engine=None, ai_advisor=None, trade_journal=None,
                         auto_trader=None, insider_tracker=None,
                         btc_sniper=None, market_maker=None, news_scanner=None):
    """Called by server.py lifespan to inject dependencies."""
    global _pm, _exit_engine, _ai_advisor, _trade_journal, _auto_trader, _insider_tracker
    global _btc_sniper, _market_maker, _news_scanner
    _pm = pm
    _exit_engine = exit_engine
    _ai_advisor = ai_advisor
    _trade_journal = trade_journal
    _auto_trader = auto_trader
    _insider_tracker = insider_tracker
    _btc_sniper = btc_sniper
    _market_maker = market_maker
    _news_scanner = news_scanner


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
    if _trading_paused:
        raise HTTPException(403, "Trading is paused — POST /kill-switch/resume to re-enable")
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
    """Return dashboard statistics, including per-platform executor status."""
    pm = _require_pm()
    stats = pm.get_dashboard_stats()

    # Add per-platform executor stats
    executor_details = {}
    for name, executor in pm.executors.items():
        try:
            bal = await executor.get_balance()
            executor_details[name] = {
                "active": True,
                "available_balance": bal.available,
                "total_balance": bal.total,
                "trade_count": await executor.get_trade_count() if hasattr(executor, 'get_trade_count') else 0,
                "is_paper_trading": getattr(executor, 'is_paper', False)
            }
        except Exception as e:
            logger.warning(f"Could not get executor details for {name}: {e}")
            executor_details[name] = {"active": False, "error": str(e)}
    
    stats["executor_details"] = executor_details
    return stats

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
    # Block approval of suppressed triggers — these are in _AI_EXIT_TRIGGERS
    # because journal data proved they destroy EV on prediction markets.
    from positions.exit_engine import AI_EXITS_ENABLED, _AI_EXIT_TRIGGERS
    trigger_name = alert.get("trigger_name", "")
    if not AI_EXITS_ENABLED and trigger_name in _AI_EXIT_TRIGGERS:
        raise HTTPException(403, f"Trigger '{trigger_name}' is suppressed (AI_EXITS_ENABLED=False)")
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
    # WebSocket auth: check API key from query param (FastAPI doesn't run deps on WS)
    current_key = _get_api_key()
    if current_key != "dev-local-only":
        key = ws.query_params.get("api_key", "")
        if key != current_key:
            await ws.close(code=1008, reason="Invalid API key")
            return
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


# ── Calibration ───────────────────────────────────────────────────────────────

@router.get("/calibration")
async def get_calibration(request: Request):
    """Return latest calibration report."""
    ce = getattr(request.app.state, "calibration_engine", None)
    if not ce:
        return {"error": "Calibration engine not initialized"}
    try:
        return ce.generate_report()
    except Exception as e:
        return {"error": str(e)}


# ── Kill Switch ──────────────────────────────────────────────────────────────

@router.post("/kill-switch")
async def kill_switch():
    """Emergency: stop ALL trading subsystems, cancel all pending orders, close all positions.

    This is the nuclear option — halts all automated trading and attempts to
    exit every open position at market price (FOK). State is persisted to disk
    so a server restart cannot undo the kill switch.
    """
    global _trading_paused
    _require_pm()
    results = {"subsystems_stopped": [], "orders_cancelled": 0, "positions_closed": 0, "errors": []}

    # 1. Stop ALL trading subsystems
    for name, subsystem in [("auto_trader", _auto_trader),
                            ("exit_engine", _exit_engine),
                            ("btc_sniper", _btc_sniper),
                            ("market_maker", _market_maker),
                            ("news_scanner", _news_scanner)]:
        if subsystem and hasattr(subsystem, "stop"):
            try:
                subsystem.stop()
                results["subsystems_stopped"].append(name)
                logger.warning("KILL SWITCH: %s stopped", name)
            except Exception as e:
                results["errors"].append(f"Failed to stop {name}: {e}")

    # 2. Block manual trades
    _trading_paused = True

    # 3. Persist kill switch to disk (survives restart)
    _persist_kill_switch(True, reason="emergency_kill_switch")

    # 4. Cancel all pending limit orders
    for pkg in _pm.list_packages("open"):
        pending = pkg.get("_pending_limit_orders", {})
        for leg_id, info in list(pending.items()):
            executor = _pm.executors.get(info["platform"])
            if executor:
                try:
                    cancelled = await executor.cancel_order(info["order_id"])
                    if cancelled:
                        results["orders_cancelled"] += 1
                except Exception as e:
                    results["errors"].append(f"Cancel order {info['order_id']}: {e}")
        pkg.pop("_pending_limit_orders", None)

    # 5. Close all open positions at market (FOK — use_limit=False for guaranteed fill)
    for pkg in _pm.list_packages("open"):
        for leg in pkg.get("legs", []):
            if leg.get("status") != "open":
                continue
            try:
                exit_result = await _pm.exit_leg(pkg["id"], leg["leg_id"],
                                                  trigger="kill_switch", use_limit=False)
                if exit_result.get("success"):
                    results["positions_closed"] += 1
                else:
                    results["errors"].append(f"Exit {leg['leg_id']}: {exit_result.get('error')}")
            except Exception as e:
                results["errors"].append(f"Exit {leg['leg_id']}: {e}")

    _pm.save()
    logger.warning("KILL SWITCH COMPLETE: stopped %s, %d orders cancelled, %d positions closed, %d errors",
                    results["subsystems_stopped"], results["orders_cancelled"],
                    results["positions_closed"], len(results["errors"]))
    return results


@router.post("/kill-switch/pause")
async def pause_trading():
    """Pause ALL trading subsystems without closing positions. Reversible via /resume."""
    global _trading_paused
    _trading_paused = True

    stopped = []
    for name, subsystem in [("auto_trader", _auto_trader),
                            ("btc_sniper", _btc_sniper),
                            ("market_maker", _market_maker),
                            ("news_scanner", _news_scanner)]:
        if subsystem and hasattr(subsystem, "stop"):
            try:
                subsystem.stop()
                stopped.append(name)
            except Exception:
                pass

    logger.warning("Trading PAUSED via kill-switch/pause — stopped: %s", stopped)
    return {"paused": True, "stopped": stopped,
            "open_positions": len(_pm.list_packages("open")) if _pm else 0}


@router.post("/kill-switch/resume")
async def resume_trading():
    """Resume all trading subsystems after a pause or kill switch."""
    global _trading_paused
    _trading_paused = False

    # Clear persistent kill switch state
    _persist_kill_switch(False)

    resumed = []
    for name, subsystem in [("auto_trader", _auto_trader),
                            ("btc_sniper", _btc_sniper),
                            ("market_maker", _market_maker),
                            ("news_scanner", _news_scanner)]:
        if subsystem and hasattr(subsystem, "start"):
            try:
                subsystem.start()
                resumed.append(name)
            except Exception:
                pass

    # Restart exit engine (critical for monitoring open positions)
    if _exit_engine and hasattr(_exit_engine, "start"):
        try:
            _exit_engine.start()
            resumed.append("exit_engine")
        except Exception:
            pass

    logger.info("Trading RESUMED via kill-switch/resume — started: %s", resumed)
    return {"resumed": True, "started": resumed,
            "open_positions": len(_pm.list_packages("open")) if _pm else 0}


# ── Wallet Health ────────────────────────────────────────────────────────────

@router.get("/wallet-health")
async def get_wallet_health():
    """Check wallet balances and connection status for all configured platforms."""
    _require_pm()
    health = {}
    for platform_name, executor in _pm.executors.items():
        entry = {"configured": False, "balance": 0, "status": "unconfigured"}
        try:
            if hasattr(executor, "is_configured") and executor.is_configured():
                entry["configured"] = True
                bal = await executor.get_balance()
                entry["balance"] = round(bal.available, 2)
                entry["total"] = round(bal.total, 2)
                entry["status"] = "ok" if bal.available > 0 else "zero_balance"
            elif hasattr(executor, "real") and hasattr(executor.real, "is_configured"):
                # Paper executor wrapping a real one
                entry["configured"] = executor.real.is_configured()
                entry["status"] = "paper_mode"
                bal = await executor.get_balance()
                entry["balance"] = round(bal.available, 2)
                entry["total"] = round(bal.total, 2)
        except Exception as e:
            entry["status"] = "error"
            entry["error"] = str(e)
        health[platform_name] = entry

    return health
