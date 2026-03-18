"""Polymarket CLOB executor — async buy/sell via Polygon chain."""
import logging, os
import httpx
from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.polymarket")
GAMMA_API = "https://gamma-api.polymarket.com"


class PolymarketExecutor(BaseExecutor):
    def __init__(self):
        self._private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        self._funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
        self._client = None
        self._http = None

    def is_configured(self) -> bool: return bool(self._private_key and self._funder)

    def _get_clob(self):
        if not self.is_configured(): raise RuntimeError("Polymarket not configured")
        if not self._client:
            from py_clob_client.client import ClobClient
            self._client = ClobClient(self._private_key, self._funder, "https://clob.polymarket.com", chain_id=137)
        return self._client

    async def _get_http(self):
        if not self._http or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=15.0, headers={"User-Agent":"Arbitrout/1.0"})
        return self._http

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        try:
            token_id, side = asset_id.rsplit(":", 1)
            order = self._get_clob().create_and_post_order({"token_id":token_id,"side":side.upper(),"size":amount_usd,"price":None,"type":"FOK"})
            return ExecutionResult(True, order.get("id",""), float(order.get("price",0)), float(order.get("size",amount_usd)), float(order.get("fee",0)), None)
        except Exception as e:
            logger.error("Polymarket buy failed: %s", e)
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        try:
            token_id, _ = asset_id.rsplit(":", 1)
            order = self._get_clob().create_and_post_order({"token_id":token_id,"side":"SELL","size":quantity,"price":None,"type":"FOK"})
            return ExecutionResult(True, order.get("id",""), float(order.get("price",0)), float(order.get("size",quantity)), float(order.get("fee",0)), None)
        except Exception as e:
            logger.error("Polymarket sell failed: %s", e)
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def get_balance(self) -> BalanceResult:
        try:
            b = self._get_clob().get_balance()
            return BalanceResult(float(b.get("available",0)), float(b.get("total",0)))
        except Exception as e:
            logger.warning("Polymarket get_balance failed: %s", e)
            return BalanceResult(0,0)

    async def get_positions(self) -> list[PositionInfo]:
        try:
            ps = self._get_clob().get_positions()
            return [PositionInfo(p.get("asset_id",""),float(p.get("size",0)),float(p.get("avg_price",0)),float(p.get("cur_price",0)),float(p.get("pnl",0))) for p in (ps if isinstance(ps,list) else [])]
        except Exception as e:
            logger.warning("Polymarket get_positions failed: %s", e)
            return []

    async def get_current_price(self, asset_id: str) -> float:
        try:
            tid = asset_id.split(":")[0] if ":" in asset_id else asset_id
            http = await self._get_http()
            r = await http.get(f"{GAMMA_API}/markets/{tid}")
            if r.status_code == 200: return float(r.json().get("outcomePrices",[0.5])[0])
        except Exception as e: logger.warning("Polymarket price failed: %s", e)
        return 0.0

    async def close(self):
        if self._http and not self._http.is_closed: await self._http.aclose()
