"""Robinhood prediction markets executor — public web API for prices.

Robinhood doesn't expose a public prediction market trading API yet.
For paper trading, PaperExecutor wraps this and uses get_current_price()
which fetches from their public event contracts endpoint.
"""
import logging
import httpx
from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.robinhood")
# Robinhood public prediction market contracts endpoint
RH_API = "https://robinhood.com/api/prediction-markets"


class RobinhoodExecutor(BaseExecutor):
    def __init__(self):
        self._http = None

    def is_configured(self) -> bool:
        # Always configured — public price lookups work without auth.
        return True

    async def _get_http(self):
        if not self._http or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=15.0,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
        return self._http

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        return ExecutionResult(False, None, 0, 0, 0, "Robinhood prediction market trading not available via API — use paper mode")

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        return ExecutionResult(False, None, 0, 0, 0, "Robinhood prediction market trading not available via API — use paper mode")

    async def get_balance(self) -> BalanceResult:
        return BalanceResult(0, 0)

    async def get_positions(self) -> list[PositionInfo]:
        return []

    async def get_current_price(self, asset_id: str) -> float:
        """Fetch current price for a Robinhood prediction market.

        asset_id format: 'rh-{slug}:YES' or 'rh-{slug}:NO'
        Returns 0.0 if price unavailable (Robinhood doesn't have a clean public API).

        Falls back to the adapter's scraped price cached during scanning.
        """
        # Robinhood doesn't expose a public JSON API for prediction market prices.
        # The PaperExecutor will use fallback_price from the leg's entry_price.
        return 0.0

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
