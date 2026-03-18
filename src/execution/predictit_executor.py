"""PredictIt executor — session auth, 850-share cap per contract."""
import logging, os
import httpx
from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.predictit")
MAX_SHARES = 850


class PredictItExecutor(BaseExecutor):
    def __init__(self):
        self._session = os.environ.get("PREDICTIT_SESSION", "")
        self._http = None

    def is_configured(self): return bool(self._session)

    async def _get_http(self):
        if not self._http or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=15.0, base_url="https://www.predictit.org/api",
                                           cookies={"predictit_session": self._session})
        return self._http

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        try:
            cid, side = asset_id.rsplit(":", 1)
            price = await self.get_current_price(asset_id)
            qty = min(int(amount_usd / price) if price > 0 else 0, MAX_SHARES)
            r = await (await self._get_http()).post("/Trade/SubmitTrade",
                json={"contractId":int(cid),"pricePerShare":price,"quantity":qty,"tradeType":1 if side.upper()=="YES" else 2})
            r.raise_for_status(); d = r.json()
            return ExecutionResult(True, str(d.get("tradeId","")), price, float(qty), round(qty*price*0.05,4), None)
        except Exception as e: return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        try:
            cid, side = asset_id.rsplit(":", 1)
            price = await self.get_current_price(asset_id)
            r = await (await self._get_http()).post("/Trade/SubmitTrade",
                json={"contractId":int(cid),"pricePerShare":price,"quantity":int(quantity),"tradeType":3 if side.upper()=="YES" else 4})
            r.raise_for_status(); return ExecutionResult(True, "", price, float(int(quantity)), 0, None)
        except Exception as e: return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def get_balance(self) -> BalanceResult:
        try:
            r = await (await self._get_http()).get("/Profile/Shares"); r.raise_for_status()
            return BalanceResult(float(r.json().get("availableBalance",0)), 0)
        except: return BalanceResult(0,0)

    async def get_positions(self) -> list[PositionInfo]: return []

    async def get_current_price(self, asset_id: str) -> float:
        try:
            cid = asset_id.split(":")[0]
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"https://www.predictit.org/api/marketdata/markets/{cid}")
                if r.status_code == 200:
                    contracts = r.json().get("contracts",[{}])
                    if contracts: return float(contracts[0].get("lastTradePrice",0.5))
        except: pass
        return 0.0

    async def close(self):
        if self._http and not self._http.is_closed: await self._http.aclose()
