"""Coinbase Advanced Trade executor — spot crypto buy/sell. DISTINCT from adapters/coinbase.py.
NOTE: Production should use coinbase-advanced-py SDK for HMAC-SHA256 request signing.
The raw httpx implementation below is for development/paper trading only.
"""
import logging, os
import httpx
from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.coinbase_spot")


class CoinbaseSpotExecutor(BaseExecutor):
    def __init__(self):
        self._api_key = os.environ.get("COINBASE_ADV_API_KEY", "")
        self._secret = os.environ.get("COINBASE_ADV_API_SECRET", "")
        self._http = None

    def is_configured(self): return bool(self._api_key and self._secret)

    async def _get_http(self):
        if not self._http or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=15.0, base_url="https://api.coinbase.com/api/v3/brokerage",
                                           headers={"CB-ACCESS-KEY":self._api_key,"Content-Type":"application/json"})
        return self._http

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        try:
            http = await self._get_http()
            r = await http.post("/orders", json={"product_id":f"{asset_id.upper()}-USD","side":"BUY",
                "order_configuration":{"market_market_ioc":{"quote_size":str(amount_usd)}}})
            r.raise_for_status(); d = r.json().get("success_response", r.json())
            return ExecutionResult(True, d.get("order_id",""), float(d.get("average_filled_price",0)),
                                  float(d.get("filled_size",0)), float(d.get("total_fees",0)), None)
        except Exception as e: return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        try:
            http = await self._get_http()
            r = await http.post("/orders", json={"product_id":f"{asset_id.upper()}-USD","side":"SELL",
                "order_configuration":{"market_market_ioc":{"base_size":str(quantity)}}})
            r.raise_for_status(); d = r.json().get("success_response", r.json())
            return ExecutionResult(True, d.get("order_id",""), float(d.get("average_filled_price",0)),
                                  float(d.get("filled_size",0)), float(d.get("total_fees",0)), None)
        except Exception as e: return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def get_balance(self) -> BalanceResult:
        try:
            r = await (await self._get_http()).get("/accounts"); r.raise_for_status()
            usd = next((a for a in r.json().get("accounts",[]) if a.get("currency")=="USD"), None)
            return BalanceResult(float(usd["available_balance"]["value"]) if usd else 0, 0)
        except: return BalanceResult(0,0)

    async def get_positions(self) -> list[PositionInfo]: return []

    async def get_current_price(self, asset_id: str) -> float:
        coin_map = {"BTC":"bitcoin","ETH":"ethereum","SOL":"solana","DOGE":"dogecoin","XRP":"ripple"}
        cid = coin_map.get(asset_id.upper(), asset_id.lower())
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get("https://api.coingecko.com/api/v3/simple/price", params={"ids":cid,"vs_currencies":"usd"})
                if r.status_code == 200: return float(r.json().get(cid,{}).get("usd",0))
        except: pass
        return 0.0

    async def close(self):
        if self._http and not self._http.is_closed: await self._http.aclose()
