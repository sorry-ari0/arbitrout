"""Political synthetic analysis API endpoints."""
import logging
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger("political.router")

router = APIRouter(prefix="/api/political", tags=["political"])

_analyzer = None


def init_political_router(analyzer):
    """Called by server.py to inject the analyzer instance."""
    global _analyzer
    _analyzer = analyzer


class AnalyzeRequest(BaseModel):
    cluster_id: str


@router.get("/clusters")
async def get_clusters():
    """List active political clusters with contract classifications."""
    if _analyzer is None:
        return JSONResponse(content=[])
    return JSONResponse(content=_analyzer.get_clusters())


@router.get("/strategies")
async def get_strategies():
    """List current LLM-recommended strategies."""
    if _analyzer is None:
        return JSONResponse(content=[])
    return JSONResponse(content=_analyzer.get_opportunities())


@router.get("/strategies/{cluster_id}")
async def get_strategy_by_cluster(cluster_id: str):
    """Detailed strategy for a specific cluster."""
    if _analyzer is None:
        return JSONResponse(content={"error": "Not initialized"}, status_code=503)
    opps = [o for o in _analyzer.get_opportunities() if o.get("cluster_id") == cluster_id]
    return JSONResponse(content=opps)


@router.post("/analyze")
async def force_analyze(req: AnalyzeRequest):
    """Force re-analysis of a cluster (bypasses cache)."""
    if _analyzer is None:
        return JSONResponse(content={"error": "Not initialized"}, status_code=503)
    result = await _analyzer.analyze_cluster_by_id(req.cluster_id)
    return JSONResponse(content=result)


_eval_logger = None


def set_eval_logger(eval_log):
    """Set eval logger for political-specific eval endpoints."""
    global _eval_logger
    _eval_logger = eval_log


@router.get("/eval")
async def get_political_eval_summary():
    """Strategy performance summary for political synthetics."""
    if _analyzer is None or _eval_logger is None:
        return JSONResponse(content={})
    summary = _eval_logger.get_summary()
    return JSONResponse(content=summary.get("political_synthetic", {}))


@router.get("/eval/missed")
async def get_political_eval_missed():
    """Skipped political strategies that would have been profitable."""
    if _eval_logger is None:
        return JSONResponse(content=[])
    return JSONResponse(content=_eval_logger.get_missed_opportunities(
        strategy_type="political_synthetic"))
