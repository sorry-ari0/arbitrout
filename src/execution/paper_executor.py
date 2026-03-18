"""Paper executor — wraps real executor for simulated trading. Real prices, fake money."""
import logging, uuid
from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.paper")


FEE_RATES = {
    "polymarket": 0.02,      # 2% per trade
    "kalshi": 0.01,           # 1% per trade
    "coinbase_spot": 0.005,   # 0.5% per trade
    "predictit": 0.05,        # 5% on profits (simplified as flat fee)
}
DEFAULT_FEE_RATE = 0.02  # 2% default


class PaperExecutor:
    """NOT a BaseExecutor subclass — uses composition, not inheritance.
    Wraps a real executor to use its price feeds while simulating trades."""

    def __init__(self, real_executor: BaseExecutor, starting_balance: float = 10000.0):
        self.real = real_executor
        self.balance = starting_balance
        self.starting_balance = starting_balance
        self.positions: dict[str, dict] = {}
        self.trade_history: list[dict] = []
        self.total_fees_paid = 0.0
        # Determine fee rate based on real executor type
        platform = getattr(real_executor, '__class__', type(real_executor)).__name__.lower()
        self.fee_rate = DEFAULT_FEE_RATE
        for name, rate in FEE_RATES.items():
            if name in platform:
                self.fee_rate = rate
                break

    async def buy(self, asset_id: str, amount_usd: float, fallback_price: float = 0) -> ExecutionResult:
        if amount_usd > self.balance:
            return ExecutionResult(False, None, 0, 0, 0, f"Insufficient paper balance: {self.balance:.2f} < {amount_usd:.2f}")
        price = await self.real.get_current_price(asset_id)
        if price <= 0 and fallback_price > 0:
            price = fallback_price
            logger.info("Using fallback price %.4f for %s", price, asset_id)
        if price <= 0:
            return ExecutionResult(False, None, 0, 0, 0, f"Invalid price for {asset_id}")
        fee = round(amount_usd * self.fee_rate, 4)
        total_cost = amount_usd + fee
        if total_cost > self.balance:
            return ExecutionResult(False, None, 0, 0, 0, f"Insufficient paper balance after fees: {self.balance:.2f} < {total_cost:.2f}")
        qty = amount_usd / price
        self.balance -= total_cost
        self.total_fees_paid += fee
        pos = self.positions.get(asset_id)
        if pos:
            total = pos["quantity"] + qty
            pos["avg_entry_price"] = (pos["avg_entry_price"] * pos["quantity"] + price * qty) / total
            pos["quantity"] = total
        else:
            self.positions[asset_id] = {"quantity": qty, "avg_entry_price": price}
        tx_id = f"paper_{uuid.uuid4().hex[:12]}"
        self.trade_history.append({"action":"buy","asset_id":asset_id,"price":price,"quantity":qty,"amount_usd":amount_usd,"fee":fee,"tx_id":tx_id})
        return ExecutionResult(True, tx_id, price, qty, fee, None)

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        pos = self.positions.get(asset_id)
        if not pos or pos["quantity"] < quantity * 0.999:
            return ExecutionResult(False, None, 0, 0, 0, f"No position or insufficient quantity for {asset_id}")
        price = await self.real.get_current_price(asset_id)
        if price <= 0:
            price = pos["avg_entry_price"]
            logger.info("Using entry price fallback %.4f for sell of %s", price, asset_id)
        proceeds = quantity * price
        fee = round(proceeds * self.fee_rate, 4)
        net_proceeds = proceeds - fee
        self.balance += net_proceeds
        self.total_fees_paid += fee
        pos["quantity"] -= quantity
        if pos["quantity"] < 1e-10: del self.positions[asset_id]
        tx_id = f"paper_{uuid.uuid4().hex[:12]}"
        self.trade_history.append({"action":"sell","asset_id":asset_id,"price":price,"quantity":quantity,"proceeds_usd":net_proceeds,"fee":fee,"tx_id":tx_id})
        return ExecutionResult(True, tx_id, price, quantity, fee, None)

    async def get_balance(self) -> BalanceResult:
        pos_val = 0.0
        for aid, pos in self.positions.items():
            try: pos_val += pos["quantity"] * await self.real.get_current_price(aid)
            except Exception: pos_val += pos["quantity"] * pos["avg_entry_price"]
        return BalanceResult(self.balance, self.balance + pos_val)

    async def get_positions(self) -> list[PositionInfo]:
        result = []
        for aid, pos in self.positions.items():
            try: price = await self.real.get_current_price(aid)
            except Exception: price = pos["avg_entry_price"]
            result.append(PositionInfo(aid, pos["quantity"], pos["avg_entry_price"], price,
                                       (price - pos["avg_entry_price"]) * pos["quantity"]))
        return result

    async def get_current_price(self, asset_id: str) -> float:
        price = await self.real.get_current_price(asset_id)
        if price <= 0:
            # Fallback to known position entry price
            pos = self.positions.get(asset_id)
            if pos:
                return pos["avg_entry_price"]
        return price

    def is_configured(self) -> bool: return True

    def get_stats(self) -> dict:
        pnl = self.balance - self.starting_balance
        sells = [t for t in self.trade_history if t["action"] == "sell"]
        # Win rate: compare sell price to buy price for same asset
        wins = 0
        for s in sells:
            buys = [b for b in self.trade_history if b["action"] == "buy" and b["asset_id"] == s["asset_id"]]
            if buys and s.get("price", 0) > buys[-1].get("price", 0):
                wins += 1
        return {"mode":"paper","starting_balance":self.starting_balance,"current_balance":round(self.balance,2),
                "total_pnl":round(pnl,2),"total_fees_paid":round(self.total_fees_paid,2),
                "fee_rate":self.fee_rate,"total_trades":len(self.trade_history),
                "win_rate":round(wins/len(sells),2) if sells else 0,"open_positions":len(self.positions)}
