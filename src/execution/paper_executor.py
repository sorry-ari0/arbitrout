"""Paper executor — wraps real executor for simulated trading. Real prices, fake money."""
import logging, uuid
from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.paper")


class PaperExecutor:
    """NOT a BaseExecutor subclass — uses composition, not inheritance.
    Wraps a real executor to use its price feeds while simulating trades."""

    def __init__(self, real_executor: BaseExecutor, starting_balance: float = 10000.0):
        self.real = real_executor
        self.balance = starting_balance
        self.starting_balance = starting_balance
        self.positions: dict[str, dict] = {}
        self.trade_history: list[dict] = []

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        if amount_usd > self.balance:
            return ExecutionResult(False, None, 0, 0, 0, f"Insufficient paper balance: {self.balance:.2f} < {amount_usd:.2f}")
        price = await self.real.get_current_price(asset_id)
        if price <= 0:
            return ExecutionResult(False, None, 0, 0, 0, f"Invalid price for {asset_id}")
        qty = amount_usd / price
        self.balance -= amount_usd
        pos = self.positions.get(asset_id)
        if pos:
            total = pos["quantity"] + qty
            pos["avg_entry_price"] = (pos["avg_entry_price"] * pos["quantity"] + price * qty) / total
            pos["quantity"] = total
        else:
            self.positions[asset_id] = {"quantity": qty, "avg_entry_price": price}
        tx_id = f"paper_{uuid.uuid4().hex[:12]}"
        self.trade_history.append({"action":"buy","asset_id":asset_id,"price":price,"quantity":qty,"amount_usd":amount_usd,"tx_id":tx_id})
        return ExecutionResult(True, tx_id, price, qty, 0.0, None)

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        pos = self.positions.get(asset_id)
        if not pos or pos["quantity"] < quantity * 0.999:
            return ExecutionResult(False, None, 0, 0, 0, f"No position or insufficient quantity for {asset_id}")
        price = await self.real.get_current_price(asset_id)
        proceeds = quantity * price
        self.balance += proceeds
        pos["quantity"] -= quantity
        if pos["quantity"] < 1e-10: del self.positions[asset_id]
        tx_id = f"paper_{uuid.uuid4().hex[:12]}"
        self.trade_history.append({"action":"sell","asset_id":asset_id,"price":price,"quantity":quantity,"proceeds_usd":proceeds,"tx_id":tx_id})
        return ExecutionResult(True, tx_id, price, quantity, 0.0, None)

    async def get_balance(self) -> BalanceResult:
        pos_val = 0.0
        for aid, pos in self.positions.items():
            try: pos_val += pos["quantity"] * await self.real.get_current_price(aid)
            except: pos_val += pos["quantity"] * pos["avg_entry_price"]
        return BalanceResult(self.balance, self.balance + pos_val)

    async def get_positions(self) -> list[PositionInfo]:
        result = []
        for aid, pos in self.positions.items():
            try: price = await self.real.get_current_price(aid)
            except: price = pos["avg_entry_price"]
            result.append(PositionInfo(aid, pos["quantity"], pos["avg_entry_price"], price,
                                       (price - pos["avg_entry_price"]) * pos["quantity"]))
        return result

    async def get_current_price(self, asset_id: str) -> float:
        return await self.real.get_current_price(asset_id)

    def is_configured(self) -> bool: return True

    def get_stats(self) -> dict:
        pnl = self.balance - self.starting_balance
        sells = [t for t in self.trade_history if t["action"] == "sell"]
        wins = sum(1 for t in sells if t.get("price", 0) > 0)  # tracked at package level via position_manager
        return {"mode":"paper","starting_balance":self.starting_balance,"current_balance":self.balance,
                "total_pnl":round(pnl,2),"total_trades":len(self.trade_history),
                "win_rate":round(wins/len(sells),2) if sells else 0,"open_positions":len(self.positions)}
