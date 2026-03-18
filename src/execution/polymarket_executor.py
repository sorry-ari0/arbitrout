"""Polymarket CLOB executor — async buy/sell via Polygon chain.

Uses py_clob_client for order execution and Gamma API for price lookups.
Supports both limit orders (maker, 0% fee) and market orders (taker, ~2% fee).

Asset IDs use format: "{conditionId}:YES" or "{conditionId}:NO"
The CLOB uses token_ids (different from conditionId) — resolved via get_market().
"""
import logging
import os

import httpx
from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.polymarket")
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet


class PolymarketExecutor(BaseExecutor):
    def __init__(self):
        self._private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        self._funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
        self._client = None
        self._creds = None
        self._http = None
        # Cache: conditionId -> [yes_token_id, no_token_id]
        self._token_id_cache: dict[str, list[str]] = {}

    def is_configured(self) -> bool:
        return bool(self._private_key and self._funder)

    def _get_clob(self):
        """Lazy-init the CLOB client with proper auth."""
        if not self.is_configured():
            raise RuntimeError("Polymarket not configured — set POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER_ADDRESS")
        if not self._client:
            from py_clob_client.client import ClobClient
            self._client = ClobClient(
                CLOB_HOST,
                chain_id=CHAIN_ID,
                key=self._private_key,
                funder=self._funder,
            )
            # Derive API credentials for Level 2 auth (required for trading)
            self._creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(self._creds)
            logger.info("Polymarket CLOB client initialized with Level 2 auth")
        return self._client

    async def _get_http(self):
        if not self._http or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "Arbitrout/1.0"})
        return self._http

    async def _resolve_token_id(self, condition_id: str, side: str) -> str:
        """Resolve conditionId + side to CLOB token_id.

        The CLOB uses token_ids (CTF tokens), not conditionIds. Each market
        has two token_ids: index 0 = YES, index 1 = NO.
        """
        if condition_id in self._token_id_cache:
            tokens = self._token_id_cache[condition_id]
        else:
            clob = self._get_clob()
            market = clob.get_market(condition_id)
            tokens = market.get("clobTokenIds", [])
            if not tokens or len(tokens) < 2:
                raise ValueError(f"Cannot resolve token_ids for condition {condition_id}: {market}")
            self._token_id_cache[condition_id] = tokens
            logger.info("Resolved condition %s to tokens YES=%s NO=%s", condition_id[:12], tokens[0][:12], tokens[1][:12])

        idx = 0 if side.upper() == "YES" else 1
        return tokens[idx]

    def _parse_asset_id(self, asset_id: str) -> tuple[str, str]:
        """Parse 'conditionId:YES' -> (conditionId, 'YES')."""
        if ":" in asset_id:
            parts = asset_id.rsplit(":", 1)
            return parts[0], parts[1].upper()
        return asset_id, "YES"

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        """Buy shares using a limit order at current best price (maker fee: 0%).

        amount_usd is the dollar cost. Shares = amount_usd / price.
        Uses GTC limit order at current midpoint for maker fee rate.
        """
        try:
            from py_clob_client.clob_types import OrderArgs

            condition_id, side = self._parse_asset_id(asset_id)
            token_id = await self._resolve_token_id(condition_id, side)
            clob = self._get_clob()

            # Get current price for limit order
            price = float(clob.get_midpoint(token_id) or 0)
            if price <= 0:
                return ExecutionResult(False, None, 0, 0, 0, f"Cannot get price for {asset_id}")

            # Get tick size and fee rate for this market
            tick_size = float(clob.get_tick_size(token_id))
            fee_rate_bps = clob.get_fee_rate_bps(token_id)

            # Round price to tick size
            price = round(price / tick_size) * tick_size
            price = round(price, 4)

            # Calculate shares from USD amount
            size = round(amount_usd / price, 2)
            if size < 1:
                return ExecutionResult(False, None, 0, 0, 0, f"Trade too small: {size} shares at ${price}")

            # Check neg_risk flag (required for some markets)
            neg_risk = clob.get_neg_risk(token_id)

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side="BUY",
                fee_rate_bps=fee_rate_bps,
            )

            logger.info("Placing BUY order: %s %s shares @ $%.4f (fee=%dbps, neg_risk=%s)",
                        asset_id, size, price, fee_rate_bps, neg_risk)

            result = clob.create_and_post_order(order_args)

            order_id = result.get("orderID", result.get("id", ""))
            if not order_id:
                return ExecutionResult(False, None, 0, 0, 0, f"Order rejected: {result}")

            # For limit orders, the fill may not be immediate
            # Return the order details — actual fill tracked by the position manager
            fee = round(amount_usd * (fee_rate_bps / 10000), 4)
            return ExecutionResult(True, order_id, price, size, fee, None)

        except Exception as e:
            logger.error("Polymarket buy failed for %s: %s", asset_id, e)
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        """Sell shares using a market order (FOK) for immediate fill.

        Uses MarketOrderArgs for Fill-or-Kill execution.
        amount = shares to sell (for sells, amount is in shares).
        """
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType

            condition_id, side = self._parse_asset_id(asset_id)
            token_id = await self._resolve_token_id(condition_id, side)
            clob = self._get_clob()

            # Get current price for logging and fee estimation
            price = float(clob.get_midpoint(token_id) or 0)

            # For sell market orders, amount = number of shares to sell
            market_args = MarketOrderArgs(
                token_id=token_id,
                amount=round(quantity, 2),
                side="SELL",
            )

            logger.info("Placing SELL order: %s %.2f shares @ ~$%.4f",
                        asset_id, quantity, price)

            # create_market_order returns a SignedOrder — must also post it
            signed_order = clob.create_market_order(market_args)
            result = clob.post_order(signed_order, orderType=OrderType.FOK)

            order_id = result.get("orderID", result.get("id", ""))
            if not order_id:
                return ExecutionResult(False, None, 0, 0, 0, f"Sell order rejected: {result}")

            fill_price = float(result.get("price", price))
            proceeds = quantity * fill_price
            fee = float(result.get("fee", 0))
            return ExecutionResult(True, order_id, fill_price, quantity, fee, None)

        except Exception as e:
            logger.error("Polymarket sell failed for %s: %s", asset_id, e)
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def get_balance(self) -> BalanceResult:
        try:
            clob = self._get_clob()
            b = clob.get_balance_allowance()
            if isinstance(b, dict):
                return BalanceResult(float(b.get("balance", 0)), float(b.get("balance", 0)))
            return BalanceResult(0, 0)
        except Exception as e:
            logger.warning("Polymarket balance check failed: %s", e)
            return BalanceResult(0, 0)

    async def get_positions(self) -> list[PositionInfo]:
        try:
            clob = self._get_clob()
            trades = clob.get_trades()
            # Simplified — the CLOB doesn't have a direct "positions" endpoint
            # Real position tracking happens in PositionManager via positions.json
            return []
        except Exception as e:
            logger.warning("Polymarket positions check failed: %s", e)
            return []

    async def get_current_price(self, asset_id: str) -> float:
        """Fetch current price from Polymarket Gamma API.

        asset_id: 'conditionId:YES' or 'conditionId:NO' or just 'conditionId'.
        Returns the price for the specified side.
        """
        try:
            import json as _json
            condition_id, side = self._parse_asset_id(asset_id)

            http = await self._get_http()
            # Use condition_id query param — /markets/{id} returns 422 for conditionIds
            r = await http.get(f"{GAMMA_API}/markets", params={"condition_id": condition_id})
            if r.status_code == 200:
                data = r.json()
                markets = data if isinstance(data, list) else [data]
                if markets:
                    market = markets[0]
                    raw_prices = market.get("outcomePrices", "[]")
                    # outcomePrices is a JSON string like '["0.475", "0.525"]'
                    if isinstance(raw_prices, str):
                        try:
                            parsed = _json.loads(raw_prices)
                        except Exception:
                            parsed = []
                    else:
                        parsed = raw_prices
                    if parsed and len(parsed) >= 1:
                        yes_price = float(parsed[0])
                        if side == "NO":
                            return 1.0 - yes_price
                        return yes_price
        except Exception as e:
            logger.warning("Polymarket price failed for %s: %s", asset_id, e)
        return 0.0

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
