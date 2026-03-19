"""Opinion Labs (opinion.trade) executor — public API for prices, API key for trading.

For paper trading, PaperExecutor wraps this and uses get_current_price() for real prices.
Live trading requires OPINION_LABS_API_KEY environment variable.
"""
import logging
import os
import httpx
from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.opinion_labs")
OPINION_API = "https://proxy.opinion.trade:8443/openapi"


class OpinionLabsExecutor(BaseExecutor):
    def __init__(self):
        self._api_key = os.environ.get("OPINION_LABS_API_KEY", "")
        self._http = None

    def is_configured(self) -> bool:
        # Always configured — public price lookups work without auth.
        # PaperExecutor wraps this for simulated trading.
        return True

    async def _get_http(self):
        if not self._http or self._http.is_closed:
            headers = {"User-Agent": "Arbitrout/1.0"}
            if self._api_key:
                headers["apikey"] = self._api_key
            self._http = httpx.AsyncClient(timeout=15.0, headers=headers)
        return self._http

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        if not self._api_key:
            return ExecutionResult(False, None, 0, 0, 0, "Opinion Labs API key not configured — use paper mode")
        return ExecutionResult(False, None, 0, 0, 0, "Opinion Labs live trading not yet implemented")

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        if not self._api_key:
            return ExecutionResult(False, None, 0, 0, 0, "Opinion Labs API key not configured — use paper mode")
        return ExecutionResult(False, None, 0, 0, 0, "Opinion Labs live trading not yet implemented")

    async def get_balance(self) -> BalanceResult:
        return BalanceResult(0, 0)

    async def get_positions(self) -> list[PositionInfo]:
        return []

    async def get_current_price(self, asset_id: str) -> float:
        """Fetch current price from Opinion Labs API.

        asset_id format: '{market_id}:YES' or '{market_id}:NO'
        """
        try:
            market_id = asset_id.split(":")[0] if ":" in asset_id else asset_id
            side = asset_id.split(":")[1].upper() if ":" in asset_id else "YES"
            http = await self._get_http()

            r = await http.get(f"{OPINION_API}/markets/{market_id}")
            if r.status_code != 200:
                return 0.0

            market = r.json()
            yes_price = 0.0

            if "yesPrice" in market:
                yes_price = float(market["yesPrice"])
            elif "probability" in market:
                yes_price = float(market["probability"])
            elif "lastPrice" in market:
                yes_price = float(market["lastPrice"])

            if yes_price > 0:
                return (1.0 - yes_price) if side == "NO" else yes_price

        except Exception as e:
            logger.debug("Opinion Labs price lookup failed for %s: %s", asset_id, e)
        return 0.0

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
