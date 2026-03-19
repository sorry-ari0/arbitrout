"""Limitless Exchange executor — public API for prices, no auth needed for paper trading.

For live trading, Limitless would require wallet integration (not yet implemented).
In paper mode, PaperExecutor wraps this and uses get_current_price() for real prices.
"""
import logging
import httpx
from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.limitless")
LIMITLESS_API = "https://api.limitless.exchange"


class LimitlessExecutor(BaseExecutor):
    def __init__(self):
        self._http = None

    def is_configured(self) -> bool:
        # Always "configured" — public API, no auth needed for price lookups.
        # PaperExecutor wraps this for simulated trading.
        return True

    async def _get_http(self):
        if not self._http or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "Arbitrout/1.0"})
        return self._http

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        return ExecutionResult(False, None, 0, 0, 0, "Limitless live trading not implemented — use paper mode")

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        return ExecutionResult(False, None, 0, 0, 0, "Limitless live trading not implemented — use paper mode")

    async def get_balance(self) -> BalanceResult:
        return BalanceResult(0, 0)

    async def get_positions(self) -> list[PositionInfo]:
        return []

    async def get_current_price(self, asset_id: str) -> float:
        """Fetch current price from Limitless Exchange public API.

        asset_id format: '{market_id}:YES' or '{market_id}:NO'
        """
        try:
            market_id = asset_id.split(":")[0] if ":" in asset_id else asset_id
            side = asset_id.split(":")[1].upper() if ":" in asset_id else "YES"
            http = await self._get_http()

            r = await http.get(f"{LIMITLESS_API}/markets/{market_id}")
            if r.status_code != 200:
                return 0.0

            market = r.json()
            # Try prices array first [yes_price, no_price]
            prices = market.get("prices")
            if isinstance(prices, list) and len(prices) >= 2:
                return float(prices[1]) if side == "NO" else float(prices[0])

            # Try probability field
            prob = market.get("probability", 0)
            if prob:
                yes_price = float(prob)
                return (1.0 - yes_price) if side == "NO" else yes_price

        except Exception as e:
            logger.debug("Limitless price lookup failed for %s: %s", asset_id, e)
        return 0.0

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
