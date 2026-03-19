"""Polymarket CLOB executor — async buy/sell via Polygon chain.

Uses py_clob_client for order execution and Gamma API for price lookups.
All CLOB client calls are synchronous and wrapped in run_in_executor to
avoid blocking the async event loop (critical for exit engine safety overrides).

Buy and sell both use FOK market orders for guaranteed immediate fill.

Asset IDs use format: "{conditionId}:YES" or "{conditionId}:NO"
The CLOB uses token_ids (different from conditionId) — resolved via get_market().
"""
import asyncio
import functools
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

    async def _run_sync(self, func, *args, **kwargs):
        """Run a synchronous CLOB client method in a thread executor.

        All py_clob_client methods are synchronous (blocking HTTP).
        Running them in the default executor prevents blocking the async
        event loop, which is critical so that exit engine safety overrides
        are never delayed during order placement.
        """
        loop = asyncio.get_running_loop()
        if kwargs:
            call = functools.partial(func, *args, **kwargs)
            return await loop.run_in_executor(None, call)
        elif args:
            return await loop.run_in_executor(None, func, *args)
        else:
            return await loop.run_in_executor(None, func)

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
            market = await self._run_sync(clob.get_market, condition_id)
            tokens = market.get("clobTokenIds", [])
            if not tokens or len(tokens) < 2:
                raise ValueError(f"Cannot resolve token_ids for condition {condition_id}: {market}")
            self._token_id_cache[condition_id] = tokens
            logger.info("Resolved condition %s to tokens YES=%s NO=%s", condition_id[:12], tokens[0][:12], tokens[1][:12])

        idx = 0 if side.upper() == "YES" else 1
        return tokens[idx]

    async def _resolve_token_id_http(self, condition_id: str, side: str) -> str | None:
        """Resolve conditionId + side to token_id via public CLOB REST API (no auth needed).

        Fallback for get_current_price() when the CLOB SDK client is not configured.
        Uses GET /markets/{condition_id} which returns token metadata.
        """
        if condition_id in self._token_id_cache:
            tokens = self._token_id_cache[condition_id]
        else:
            http = await self._get_http()
            try:
                r = await http.get(f"{CLOB_HOST}/markets/{condition_id}")
                if r.status_code != 200:
                    logger.warning("CLOB market lookup failed for %s: HTTP %d", condition_id[:16], r.status_code)
                    return None
                market = r.json()
                # CLOB REST returns {"tokens": [{"token_id": "...", "outcome": "Yes"}, ...]}
                tokens_list = market.get("tokens", [])
                if len(tokens_list) >= 2:
                    tokens = [tokens_list[0]["token_id"], tokens_list[1]["token_id"]]
                else:
                    # Fallback: try clobTokenIds field (some responses use this)
                    tokens = market.get("clobTokenIds", [])
                if not tokens or len(tokens) < 2:
                    logger.warning("Cannot resolve tokens for %s: got %s", condition_id[:16], tokens_list or tokens)
                    return None
                self._token_id_cache[condition_id] = tokens
                logger.info("Resolved condition %s to tokens YES=%s NO=%s (via REST)",
                            condition_id[:12], tokens[0][:12], tokens[1][:12])
            except Exception as e:
                logger.warning("CLOB REST token resolution failed for %s: %s", condition_id[:16], e)
                return None

        idx = 0 if side.upper() in ("YES", "BUY") else 1
        return tokens[idx]

    def _parse_asset_id(self, asset_id: str) -> tuple[str, str]:
        """Parse 'conditionId:YES' -> (conditionId, 'YES')."""
        if ":" in asset_id:
            parts = asset_id.rsplit(":", 1)
            return parts[0], parts[1].upper()
        return asset_id, "YES"

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        """Buy shares using a FOK market order for guaranteed immediate fill.

        amount_usd is the dollar cost. Uses Fill-or-Kill so the position
        system doesn't need to handle pending/unfilled orders.
        Accepts taker fee (~2%) for guaranteed execution.
        """
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType, PartialCreateOrderOptions

            condition_id, side = self._parse_asset_id(asset_id)
            token_id = await self._resolve_token_id(condition_id, side)
            clob = self._get_clob()

            # Balance check before placing order
            balance_info = await self._run_sync(clob.get_balance_allowance)
            if isinstance(balance_info, dict):
                available_balance = float(balance_info.get("balance", 0))
            else:
                available_balance = 0
            if amount_usd > available_balance:
                return ExecutionResult(
                    False, None, 0, 0, 0,
                    f"Insufficient balance: need ${amount_usd:.2f} but only ${available_balance:.2f} available"
                )

            # Get current price for logging and amount calculation
            price = float(await self._run_sync(clob.get_midpoint, token_id) or 0)
            if price <= 0:
                return ExecutionResult(False, None, 0, 0, 0, f"Cannot get price for {asset_id}")

            # For buy market orders, amount = dollar amount to spend
            market_args = MarketOrderArgs(
                token_id=token_id,
                amount=round(amount_usd, 2),
                side="BUY",
            )

            # Get neg_risk flag — required for some markets
            neg_risk = await self._run_sync(clob.get_neg_risk, token_id)
            options = PartialCreateOrderOptions(neg_risk=neg_risk)

            expected_shares = round(amount_usd / price, 2)
            logger.info("Placing BUY market order (FOK): %s ~%.2f shares, $%.2f @ ~$%.4f (neg_risk=%s)",
                        asset_id, expected_shares, amount_usd, price, neg_risk)

            # create_market_order returns a SignedOrder — must also post it
            signed_order = await self._run_sync(clob.create_market_order, market_args, options)
            result = await self._run_sync(clob.post_order, signed_order, OrderType.FOK)

            order_id = result.get("orderID", result.get("id", ""))
            if not order_id:
                return ExecutionResult(False, None, 0, 0, 0, f"Order rejected: {result}")

            # Try to get actual fill details from the response
            fill_price = float(result.get("price", price))
            fill_quantity = float(result.get("size", result.get("amount", expected_shares)))
            fee = float(result.get("fee", 0))

            # Try querying the order for actual fill price verification
            try:
                order_details = await self._run_sync(clob.get_order, order_id)
                if order_details:
                    if "price" in order_details:
                        fill_price = float(order_details["price"])
                    if "size_matched" in order_details:
                        fill_quantity = float(order_details["size_matched"])
                    if "fee" in order_details:
                        fee = float(order_details["fee"])
            except Exception as e:
                logger.warning("Could not verify fill details for order %s: %s — using response/estimated values", order_id, e)

            return ExecutionResult(True, order_id, fill_price, fill_quantity, fee, None)

        except Exception as e:
            logger.error("Polymarket buy failed for %s: %s", asset_id, e)
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        """Sell shares using a market order (FOK) for immediate fill.

        Uses MarketOrderArgs for Fill-or-Kill execution.
        amount = shares to sell (for sells, amount is in shares).
        Verifies actual fill price via get_order after posting.
        """
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType, PartialCreateOrderOptions

            condition_id, side = self._parse_asset_id(asset_id)
            token_id = await self._resolve_token_id(condition_id, side)
            clob = self._get_clob()

            # Get current price for logging and fee estimation
            price = float(await self._run_sync(clob.get_midpoint, token_id) or 0)

            # For sell market orders, amount = number of shares to sell
            market_args = MarketOrderArgs(
                token_id=token_id,
                amount=round(quantity, 2),
                side="SELL",
            )

            # Get neg_risk flag — required for some markets
            neg_risk = await self._run_sync(clob.get_neg_risk, token_id)
            options = PartialCreateOrderOptions(neg_risk=neg_risk)

            logger.info("Placing SELL market order (FOK): %s %.2f shares @ ~$%.4f (neg_risk=%s)",
                        asset_id, quantity, price, neg_risk)

            # create_market_order returns a SignedOrder — must also post it
            signed_order = await self._run_sync(clob.create_market_order, market_args, options)
            result = await self._run_sync(clob.post_order, signed_order, OrderType.FOK)

            order_id = result.get("orderID", result.get("id", ""))
            if not order_id:
                return ExecutionResult(False, None, 0, 0, 0, f"Sell order rejected: {result}")

            fill_price = float(result.get("price", price))
            fill_quantity = float(result.get("size", result.get("amount", quantity)))
            fee = float(result.get("fee", 0))

            # Verify actual fill price by querying the order
            try:
                order_details = await self._run_sync(clob.get_order, order_id)
                if order_details:
                    if "price" in order_details:
                        verified_price = float(order_details["price"])
                        if verified_price != fill_price:
                            logger.info("Fill price verified via get_order: $%.4f (response had $%.4f)",
                                        verified_price, fill_price)
                            fill_price = verified_price
                    if "size_matched" in order_details:
                        fill_quantity = float(order_details["size_matched"])
                    if "fee" in order_details:
                        fee = float(order_details["fee"])
            except Exception as e:
                logger.warning("Could not verify fill price for order %s: %s — fill price may be inaccurate", order_id, e)

            return ExecutionResult(True, order_id, fill_price, fill_quantity, fee, None)

        except Exception as e:
            logger.error("Polymarket sell failed for %s: %s", asset_id, e)
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def get_balance(self) -> BalanceResult:
        try:
            clob = self._get_clob()
            b = await self._run_sync(clob.get_balance_allowance)
            if isinstance(b, dict):
                return BalanceResult(float(b.get("balance", 0)), float(b.get("balance", 0)))
            return BalanceResult(0, 0)
        except Exception as e:
            logger.warning("Polymarket balance check failed: %s", e)
            return BalanceResult(0, 0)

    async def get_positions(self) -> list[PositionInfo]:
        try:
            clob = self._get_clob()
            trades = await self._run_sync(clob.get_trades)
            # Simplified — the CLOB doesn't have a direct "positions" endpoint
            # Real position tracking happens in PositionManager via positions.json
            return []
        except Exception as e:
            logger.warning("Polymarket positions check failed: %s", e)
            return []

    async def get_current_price(self, asset_id: str) -> float:
        """Fetch current price from Polymarket CLOB (public, no auth needed).

        asset_id: 'conditionId:YES' or 'conditionId:NO' or just 'conditionId'.
        Returns the price for the specified side.

        Strategy:
        1. Resolve condition_id to CLOB token_id (cached after first lookup)
        2. Get midpoint price from CLOB REST API (/midpoint?token_id=X)
        3. Fallback: Gamma API with clob_token_ids param (not condition_id, which is broken)
        """
        try:
            import json as _json
            condition_id, side = self._parse_asset_id(asset_id)
            http = await self._get_http()

            # Step 1: Resolve to token_id (public CLOB REST, no auth)
            token_id = await self._resolve_token_id_http(condition_id, side)
            if not token_id:
                logger.warning("Price lookup failed for %s: cannot resolve token_id", asset_id)
                return 0.0

            # Step 2: Get midpoint from CLOB (most reliable, real-time)
            try:
                r = await http.get(f"{CLOB_HOST}/midpoint", params={"token_id": token_id})
                if r.status_code == 200:
                    data = r.json()
                    mid = float(data.get("mid", 0))
                    if mid > 0:
                        return mid
            except Exception as e:
                logger.debug("CLOB midpoint failed for %s: %s — trying Gamma fallback", asset_id, e)

            # Step 3: Fallback — Gamma API with clob_token_ids (NOT condition_id which is broken)
            try:
                r = await http.get(f"{GAMMA_API}/markets", params={"clob_token_ids": token_id})
                if r.status_code == 200:
                    data = r.json()
                    markets = data if isinstance(data, list) else [data]
                    if markets:
                        market = markets[0]
                        raw_prices = market.get("outcomePrices", "[]")
                        if isinstance(raw_prices, str):
                            try:
                                parsed = _json.loads(raw_prices)
                            except Exception:
                                parsed = []
                        else:
                            parsed = raw_prices
                        if parsed and len(parsed) >= 2:
                            if side in ("NO", "SELL"):
                                return float(parsed[1])
                            return float(parsed[0])
                        elif parsed and len(parsed) >= 1:
                            yes_price = float(parsed[0])
                            if side in ("NO", "SELL"):
                                return 1.0 - yes_price
                            return yes_price
            except Exception as e:
                logger.debug("Gamma fallback failed for %s: %s", asset_id, e)

            logger.warning("All price methods failed for %s (token_id=%s)", asset_id, token_id[:16])
        except Exception as e:
            logger.warning("Polymarket price failed for %s: %s", asset_id, e)
        return 0.0

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
