"""Coinbase Advanced Trade executor — spot crypto buy/sell via official SDK.

Uses coinbase-advanced-py SDK with EC key authentication for proper
request signing. Supports market orders (IOC) and provides USDC
management for funding Polymarket wallets.

Auth: Set COINBASE_API_KEY and COINBASE_API_SECRET env vars, or provide
a key file path via COINBASE_KEY_FILE.
"""
import asyncio
import functools
import logging
import os
import uuid

from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.coinbase_spot")


class CoinbaseSpotExecutor(BaseExecutor):
    def __init__(self):
        self._api_key = os.environ.get("COINBASE_ADV_API_KEY", "")
        self._secret = os.environ.get("COINBASE_ADV_API_SECRET", "")
        self._key_file = os.environ.get("COINBASE_KEY_FILE", "")
        self._client = None

    def is_configured(self) -> bool:
        return bool((self._api_key and self._secret) or self._key_file)

    def _get_client(self):
        """Lazy-init the Coinbase REST client with proper auth."""
        if self._client is not None:
            return self._client

        if not self.is_configured():
            raise RuntimeError(
                "Coinbase not configured — set COINBASE_ADV_API_KEY + "
                "COINBASE_ADV_API_SECRET, or COINBASE_KEY_FILE"
            )

        from coinbase.rest import RESTClient

        if self._key_file:
            self._client = RESTClient(key_file=self._key_file)
        else:
            self._client = RESTClient(
                api_key=self._api_key,
                api_secret=self._secret,
            )

        logger.info("Coinbase Advanced Trade client initialized")
        return self._client

    async def _run_sync(self, func, *args, **kwargs):
        """Run synchronous SDK call in thread executor."""
        loop = asyncio.get_running_loop()
        if kwargs:
            call = functools.partial(func, *args, **kwargs)
            return await loop.run_in_executor(None, call)
        elif args:
            return await loop.run_in_executor(None, func, *args)
        else:
            return await loop.run_in_executor(None, func)

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        """Market buy crypto with USD amount."""
        try:
            client = self._get_client()
            product_id = f"{asset_id.upper()}-USD"

            result = await self._run_sync(
                client.market_order_buy,
                client_order_id=str(uuid.uuid4()),
                product_id=product_id,
                quote_size=str(round(amount_usd, 2)),
            )

            success_resp = getattr(result, "success_response", None)
            if success_resp:
                order_id = getattr(success_resp, "order_id", "")
                return ExecutionResult(True, order_id, 0, 0, 0, None)

            # Parse dict response
            if isinstance(result, dict):
                sr = result.get("success_response", result)
                order_id = sr.get("order_id", "")
                return ExecutionResult(True, order_id, 0, 0, 0, None)

            return ExecutionResult(True, "", 0, 0, 0, None)
        except Exception as e:
            logger.error("Coinbase buy failed for %s: %s", asset_id, e)
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        """Market sell crypto by quantity."""
        try:
            client = self._get_client()
            product_id = f"{asset_id.upper()}-USD"

            result = await self._run_sync(
                client.market_order_sell,
                client_order_id=str(uuid.uuid4()),
                product_id=product_id,
                base_size=str(round(quantity, 8)),
            )

            success_resp = getattr(result, "success_response", None)
            if success_resp:
                order_id = getattr(success_resp, "order_id", "")
                return ExecutionResult(True, order_id, 0, 0, 0, None)

            if isinstance(result, dict):
                sr = result.get("success_response", result)
                order_id = sr.get("order_id", "")
                return ExecutionResult(True, order_id, 0, 0, 0, None)

            return ExecutionResult(True, "", 0, 0, 0, None)
        except Exception as e:
            logger.error("Coinbase sell failed for %s: %s", asset_id, e)
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def get_balance(self) -> BalanceResult:
        """Get USD and USDC balances."""
        try:
            client = self._get_client()
            result = await self._run_sync(client.get_accounts)

            accounts = getattr(result, "accounts", None)
            if accounts is None and isinstance(result, dict):
                accounts = result.get("accounts", [])

            total = 0.0
            for acct in (accounts or []):
                currency = getattr(acct, "currency", "") if hasattr(acct, "currency") else acct.get("currency", "")
                if currency in ("USD", "USDC"):
                    avail = getattr(acct, "available_balance", None)
                    if avail and hasattr(avail, "value"):
                        total += float(avail.value)
                    elif isinstance(avail, dict):
                        total += float(avail.get("value", 0))
                    elif isinstance(avail, (int, float)):
                        total += float(avail)

            return BalanceResult(total, total)
        except Exception as e:
            logger.warning("Coinbase get_balance failed: %s", e)
            return BalanceResult(0, 0)

    async def get_positions(self) -> list[PositionInfo]:
        """Get non-zero crypto balances as positions."""
        try:
            client = self._get_client()
            result = await self._run_sync(client.get_accounts)

            accounts = getattr(result, "accounts", None)
            if accounts is None and isinstance(result, dict):
                accounts = result.get("accounts", [])

            positions = []
            for acct in (accounts or []):
                currency = getattr(acct, "currency", "") if hasattr(acct, "currency") else acct.get("currency", "")
                if currency in ("USD", "USDC"):
                    continue
                avail = getattr(acct, "available_balance", None)
                if avail and hasattr(avail, "value"):
                    qty = float(avail.value)
                elif isinstance(avail, dict):
                    qty = float(avail.get("value", 0))
                else:
                    qty = 0
                if qty > 0:
                    price = await self.get_current_price(currency)
                    positions.append(PositionInfo(currency, qty, 0, price, qty * price))

            return positions
        except Exception as e:
            logger.warning("Coinbase get_positions failed: %s", e)
            return []

    async def get_current_price(self, asset_id: str) -> float:
        """Get current price from Coinbase or CoinGecko fallback."""
        try:
            client = self._get_client()
            product_id = f"{asset_id.upper()}-USD"
            result = await self._run_sync(client.get_product, product_id)

            price = getattr(result, "price", None)
            if price:
                return float(price)

            if isinstance(result, dict):
                return float(result.get("price", 0))
        except Exception:
            pass

        # Fallback: CoinGecko
        coin_map = {
            "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
            "DOGE": "dogecoin", "XRP": "ripple", "MATIC": "matic-network",
            "USDC": "usd-coin",
        }
        cid = coin_map.get(asset_id.upper(), asset_id.lower())
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": cid, "vs_currencies": "usd"},
                )
                if r.status_code == 200:
                    return float(r.json().get(cid, {}).get("usd", 0))
        except Exception as e:
            logger.warning("Coinbase/CoinGecko price failed for %s: %s", asset_id, e)
        return 0.0

    async def close(self):
        """No persistent connections to close with the SDK."""
        self._client = None
