"""Evaluation/hindsight analysis API endpoints."""
import logging
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger("eval_router")

router = APIRouter(prefix="/api/eval", tags=["eval"])

_eval_logger = None


def init_eval_router(eval_log):
    """Called by server.py to inject the eval logger."""
    global _eval_logger
    _eval_logger = eval_log


@router.get("/summary")
async def get_summary():
    """Overall performance by strategy_type."""
    if _eval_logger is None:
        return JSONResponse(content={})
    return JSONResponse(content=_eval_logger.get_summary())


@router.get("/missed")
async def get_missed(strategy_type: str | None = None,
                     min_hypothetical_pnl: float = Query(default=5.0)):
    """Skipped opportunities that would have been profitable."""
    if _eval_logger is None:
        return JSONResponse(content=[])
    return JSONResponse(content=_eval_logger.get_missed_opportunities(
        strategy_type=strategy_type, min_pnl=min_hypothetical_pnl))


@router.get("/calibration")
async def get_calibration():
    """For each action_reason, how often it led to a correct skip vs missed opportunity."""
    if _eval_logger is None:
        return JSONResponse(content={})
    return JSONResponse(content=_eval_logger.get_calibration())


@router.get("/details/{opportunity_id}")
async def get_details(opportunity_id: str):
    """Full entry for a specific opportunity."""
    if _eval_logger is None:
        return JSONResponse(content={"error": "Not initialized"}, status_code=503)
    details = _eval_logger.get_details(opportunity_id)
    if not details:
        return JSONResponse(content={"error": "Not found"}, status_code=404)
    return JSONResponse(content=details)
