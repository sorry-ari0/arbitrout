"""Base executor ABC and shared dataclasses for all platform executors."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict

@dataclass
class ExecutionResult:
    success: bool
    tx_id: str | None
    filled_price: float
    filled_quantity: float
    fees: float
    error: str | None
    def to_dict(self) -> dict: return asdict(self)

@dataclass
class BalanceResult:
    available: float
    total: float

@dataclass
class PositionInfo:
    asset_id: str
    quantity: float
    avg_entry_price: float
    current_price: float
    unrealized_pnl: float

class BaseExecutor(ABC):
    @abstractmethod
    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult: ...
    @abstractmethod
    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult: ...
    @abstractmethod
    async def get_balance(self) -> BalanceResult: ...
    @abstractmethod
    async def get_positions(self) -> list[PositionInfo]: ...
    @abstractmethod
    async def get_current_price(self, asset_id: str) -> float: ...
    @abstractmethod
    def is_configured(self) -> bool: ...

    # --- Optional limit order methods (override for 0% maker fees) ---

    async def buy_limit(self, asset_id: str, amount_usd: float, price: float) -> ExecutionResult:
        """Place a GTC limit buy order. Default: falls back to market buy."""
        return await self.buy(asset_id, amount_usd)

    async def sell_limit(self, asset_id: str, quantity: float, price: float) -> ExecutionResult:
        """Place a GTC limit sell order. Default: falls back to market sell."""
        return await self.sell(asset_id, quantity)

    async def check_order_status(self, order_id: str) -> dict:
        """Check status of a limit order. Returns dict with 'status' key.
        Possible statuses: 'open', 'filled', 'partially_filled', 'cancelled', 'unknown'.
        Default: assumes filled (for executors that only do market orders)."""
        return {"status": "filled", "order_id": order_id}

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open limit order. Returns True if cancelled. Default: no-op."""
        return True
