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
