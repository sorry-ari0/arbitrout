"""Paper executor — wraps real executor for simulated trading. Real prices, fake money."""
import logging, time, uuid
from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.paper")


# Fee rates by order type:
#   Polymarket: 0% maker (limit orders) / ~2% taker (market orders)
#   Kalshi: ~1% taker
#   Coinbase: 0.4-0.6% maker / 0.6-1% taker
#   PredictIt: 5% on profits + 5% withdrawal
MAKER_FEE_RATES = {
    "polymarket": 0.0,        # 0% for limit orders
    "kalshi": 0.005,          # 0.5% maker
    "coinbase_spot": 0.004,   # 0.4% maker
    "predictit": 0.05,        # 5% on profits (simplified)
    "limitless": 0.005,       # ~0.5% estimated
    "opinion_labs": 0.01,     # ~1% estimated
    "robinhood": 0.0,         # 0% (Robinhood is commission-free)
    "crypto_spot": 0.0,       # Synthetic — no real fees
    "kraken": 0.0016,         # 0.16% maker
}
TAKER_FEE_RATES = {
    "polymarket": 0.02,       # ~2% for market orders
    "kalshi": 0.01,           # 1% taker
    "coinbase_spot": 0.006,   # 0.6% taker
    "predictit": 0.05,        # 5% on profits
    "limitless": 0.01,        # ~1% estimated
    "opinion_labs": 0.02,     # ~2% estimated
    "robinhood": 0.0,         # 0% commission-free
    "crypto_spot": 0.0,       # Synthetic — no real fees
    "kraken": 0.0026,         # 0.26% taker
}
DEFAULT_FEE_RATE = 0.0  # 0% default (maker — all orders now use GTC limit)


class PaperExecutor:
    """NOT a BaseExecutor subclass — uses composition, not inheritance.
    Wraps a real executor to use its price feeds while simulating trades."""

    def __init__(self, real_executor: BaseExecutor, starting_balance: float = 10000.0,
                 use_limit_orders: bool = True):
        self.real = real_executor
        self.balance = starting_balance
        self.starting_balance = starting_balance
        self.positions: dict[str, dict] = {}
        self.trade_history: list[dict] = []
        self.total_fees_paid = 0.0
        self.use_limit_orders = use_limit_orders
        self._resting_orders: dict = {}  # order_id → resting order info
        # Determine fee rates: maker for entry (limit orders), taker for exit (market orders)
        platform = getattr(real_executor, '__class__', type(real_executor)).__name__.lower()
        self.buy_fee_rate = DEFAULT_FEE_RATE
        self.sell_fee_rate = DEFAULT_FEE_RATE
        for name, rate in MAKER_FEE_RATES.items():
            if name in platform:
                self.buy_fee_rate = rate if use_limit_orders else TAKER_FEE_RATES.get(name, DEFAULT_FEE_RATE)
                break
        for name, rate in MAKER_FEE_RATES.items():
            if name in platform:
                self.sell_fee_rate = rate if use_limit_orders else TAKER_FEE_RATES.get(name, DEFAULT_FEE_RATE)
                break
        # Keep fee_rate for backwards compat (average of buy/sell)
        self.fee_rate = self.buy_fee_rate
        self.order_type = "maker" if use_limit_orders else "taker"
        self.fee_rates = {"maker": self.buy_fee_rate}

    async def buy(self, asset_id: str, amount_usd: float, fallback_price: float = 0) -> ExecutionResult:
        if amount_usd > self.balance:
            return ExecutionResult(False, None, 0, 0, 0, f"Insufficient paper balance: {self.balance:.2f} < {amount_usd:.2f}")
        price = await self.real.get_current_price(asset_id)
        if price <= 0 and fallback_price > 0:
            price = fallback_price
            logger.info("Using fallback price %.4f for %s", price, asset_id)
        if price <= 0:
            return ExecutionResult(False, None, 0, 0, 0, f"Invalid price for {asset_id}")
        fee = round(amount_usd * self.buy_fee_rate, 4)
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

    async def sell(self, asset_id: str, quantity: float, last_known_price: float = 0) -> ExecutionResult:
        pos = self.positions.get(asset_id)
        if not pos or pos["quantity"] < quantity * 0.999:
            return ExecutionResult(False, None, 0, 0, 0, f"No position or insufficient quantity for {asset_id}")
        price = await self.real.get_current_price(asset_id)
        if price <= 0 and last_known_price > 0:
            price = last_known_price
            logger.info("Using last known price %.4f for sell of %s", price, asset_id)
        elif price <= 0:
            price = pos["avg_entry_price"]
            logger.warning("Using entry price fallback %.4f for sell of %s (no real price available)", price, asset_id)
        proceeds = quantity * price
        fee = round(proceeds * self.sell_fee_rate, 4)
        net_proceeds = proceeds - fee
        self.balance += net_proceeds
        self.total_fees_paid += fee
        pos["quantity"] -= quantity
        if pos["quantity"] < 1e-10: del self.positions[asset_id]
        tx_id = f"paper_{uuid.uuid4().hex[:12]}"
        self.trade_history.append({"action":"sell","asset_id":asset_id,"price":price,"quantity":quantity,"proceeds_usd":net_proceeds,"fee":fee,"tx_id":tx_id})
        return ExecutionResult(True, tx_id, price, quantity, fee, None)

    async def buy_limit(self, asset_id: str, amount_usd: float, price: float) -> ExecutionResult:
        """Simulate a limit buy using maker fee rate (0% for Polymarket).

        Uses the provided limit price instead of fetching current price.
        Computes fee inline using MAKER_FEE_RATES — does NOT mutate self.buy_fee_rate.
        """
        if amount_usd > self.balance:
            return ExecutionResult(False, None, 0, 0, 0, f"Insufficient paper balance: {self.balance:.2f} < {amount_usd:.2f}")
        if price <= 0:
            return ExecutionResult(False, None, 0, 0, 0, f"Invalid limit price for {asset_id}")

        # Look up maker fee rate for this platform
        platform = getattr(self.real, '__class__', type(self.real)).__name__.lower()
        maker_rate = DEFAULT_FEE_RATE
        for name, rate in MAKER_FEE_RATES.items():
            if name in platform:
                maker_rate = rate
                break

        fee = round(amount_usd * maker_rate, 4)
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
        self.trade_history.append({"action": "buy_limit", "asset_id": asset_id, "price": price,
                                   "quantity": qty, "amount_usd": amount_usd, "fee": fee, "tx_id": tx_id})
        return ExecutionResult(True, tx_id, price, qty, fee, None)

    async def sell_limit(self, asset_id: str, quantity: float, price: float) -> ExecutionResult:
        """Place a resting sell limit order — reserves position quantity.

        Does NOT fill immediately. The order rests until check_order_status
        detects that market price >= limit price (a buyer matches our ask).
        """
        pos = self.positions.get(asset_id)
        if not pos or pos.get("quantity", 0) < quantity * 0.999:
            return ExecutionResult(False, None, 0, 0, 0, f"No position or insufficient quantity for {asset_id}")
        if price <= 0:
            return ExecutionResult(False, None, 0, 0, 0, f"Invalid limit price for {asset_id}")

        # Reserve position quantity (prevent double-sell)
        pos["quantity"] -= quantity
        if pos["quantity"] < 1e-10:
            del self.positions[asset_id]

        tx_id = f"paper_{uuid.uuid4().hex[:12]}"
        self._resting_orders[tx_id] = {
            "asset_id": asset_id, "quantity": quantity, "limit_price": price,
            "placed_at": time.time(), "status": "open",
        }
        self.trade_history.append({
            "action": "sell_limit_placed", "asset_id": asset_id, "price": price,
            "quantity": quantity, "tx_id": tx_id,
        })
        return ExecutionResult(True, tx_id, price, quantity, 0.0, None)

    async def check_order_status(self, order_id: str) -> dict:
        """Check if a resting order has filled.

        Sell limit fills when market price >= limit price (buyer matches our ask).
        """
        resting = self._resting_orders.get(order_id)
        if not resting:
            # Legacy behavior for non-bracket pending orders
            return {"status": "filled", "price": 0, "size_matched": 0, "fee": 0.0}
        if resting["status"] != "open":
            return {"status": resting["status"], "price": resting.get("fill_price", 0),
                    "size_matched": resting["quantity"], "fee": resting.get("fee", 0.0)}

        # Sell limit fills when a buyer matches at >= our ask price
        try:
            current = await self.real.get_current_price(resting["asset_id"])
        except Exception:
            current = 0
        limit_price = resting["limit_price"]

        if current >= limit_price:
            # Fill at limit price (maker), not market price
            maker_rate = self.fee_rates.get("maker", 0)
            fee = round(resting["quantity"] * limit_price * maker_rate, 4)
            proceeds = resting["quantity"] * limit_price - fee
            self.balance += proceeds
            self.total_fees_paid += fee
            resting["status"] = "filled"
            resting["fill_price"] = limit_price
            resting["fee"] = fee
            qty = resting["quantity"]
            del self._resting_orders[order_id]
            return {"status": "filled", "price": limit_price,
                    "size_matched": qty, "fee": fee}

        return {"status": "open", "price": 0, "size_matched": 0, "fee": 0.0}

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a resting order and unreserve position quantity."""
        resting = self._resting_orders.pop(order_id, None)
        if resting and resting["status"] == "open":
            # Unreserve position quantity
            asset_id = resting["asset_id"]
            pos = self.positions.get(asset_id)
            if pos:
                pos["quantity"] = pos.get("quantity", 0) + resting["quantity"]
            else:
                self.positions[asset_id] = {"quantity": resting["quantity"], "avg_entry_price": 0}
        return True

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
        """Return real price or 0. Does NOT fall back to entry price —
        callers (exit engine) should keep the last known real price."""
        return await self.real.get_current_price(asset_id)

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
                "fee_rate":self.fee_rate,"order_type":self.order_type,"total_trades":len(self.trade_history),
                "win_rate":round(wins/len(sells),2) if sells else 0,"open_positions":len(self.positions)}
