"""Kalshi exchange executor — RSA keypair auth, 0% fees currently.
NOTE: The implementation below uses raw httpx for clarity. For production,
replace with `kalshi-python` SDK which handles RSA request signing automatically.
The raw httpx Bearer auth shown here works only for Kalshi's demo/sandbox environment.
"""
import logging, os
import httpx
from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.kalshi")
KALSHI_API = "https://trading-api.kalshi.com/trade-api/v2"


class KalshiExecutor(BaseExecutor):
    def __init__(self):
        self._api_key = os.environ.get("KALSHI_API_KEY", "")
        self._rsa_key = os.environ.get("KALSHI_RSA_PRIVATE_KEY", "")
        self._http = None

    def is_configured(self): return bool(self._api_key and self._rsa_key)

    async def _get_http(self):
        if not self._http or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=15.0, base_url=KALSHI_API,
                                           headers={"Authorization":f"Bearer {self._api_key}"})
        return self._http

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        try:
            ticker, side = asset_id.rsplit(":", 1)
            http = await self._get_http()
            r = await http.post("/portfolio/orders", json={"ticker":ticker,"action":"buy","side":side.lower(),"type":"market","count":int(amount_usd*100)})
            r.raise_for_status(); d = r.json().get("order",{})
            return ExecutionResult(True, d.get("order_id",""), float(d.get("avg_price",0))/100, float(d.get("count",0)), 0.0, None)
        except Exception as e: return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        try:
            ticker, side = asset_id.rsplit(":", 1)
            http = await self._get_http()
            r = await http.post("/portfolio/orders", json={"ticker":ticker,"action":"sell","side":side.lower(),"type":"market","count":int(quantity)})
            r.raise_for_status(); d = r.json().get("order",{})
            return ExecutionResult(True, d.get("order_id",""), float(d.get("avg_price",0))/100, float(d.get("count",0)), 0.0, None)
        except Exception as e: return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def get_balance(self) -> BalanceResult:
        try:
            r = await (await self._get_http()).get("/portfolio/balance"); r.raise_for_status()
            d = r.json(); return BalanceResult(float(d.get("available_balance",0))/100, float(d.get("portfolio_value",0))/100)
        except Exception as e:
            logger.warning("Kalshi get_balance failed: %s", e)
            return BalanceResult(0,0)

    async def get_positions(self) -> list[PositionInfo]: return []

    async def get_current_price(self, asset_id: str) -> float:
        try:
            ticker = asset_id.split(":")[0] if ":" in asset_id else asset_id
            r = await (await self._get_http()).get(f"/markets/{ticker}")
            if r.status_code == 200: return float(r.json().get("market",{}).get("last_price",50))/100
        except Exception as e:
            logger.warning("Kalshi get_current_price failed: %s", e)
        return 0.0

    async def close(self):
        if self._http and not self._http.is_closed: await self._http.aclose()
