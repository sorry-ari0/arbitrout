"""Kalshi exchange executor — RSA-PSS auth via kalshi-python SDK.

Uses the official kalshi-python SDK which handles RSA-PSS request signing
automatically. Supports both limit and market orders. 0% trading fees.

Auth: requires KALSHI_API_KEY (key ID) and KALSHI_RSA_PRIVATE_KEY_PATH (path
to PEM file). Set KALSHI_DEMO=true to use the demo sandbox.

All SDK calls are synchronous — wrapped in run_in_executor to avoid blocking
the async event loop.
"""
import asyncio
import functools
import logging
import os
import uuid

from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.kalshi")

PROD_HOST = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_HOST = "https://demo-api.kalshi.co/trade-api/v2"


class KalshiExecutor(BaseExecutor):
    def __init__(self):
        self._api_key = os.environ.get("KALSHI_API_KEY", "")
        self._rsa_key_path = os.environ.get("KALSHI_RSA_PRIVATE_KEY_PATH", "")
        # Fallback: inline key content (for envs where file path isn't practical)
        self._rsa_key_inline = os.environ.get("KALSHI_RSA_PRIVATE_KEY", "")
        self._demo = os.environ.get("KALSHI_DEMO", "true").lower() == "true"
        self._client = None

    def is_configured(self) -> bool:
        return bool(self._api_key and (self._rsa_key_path or self._rsa_key_inline))

    def _get_client(self):
        """Lazy-init the Kalshi SDK client with RSA-PSS auth."""
        if self._client is not None:
            return self._client

        if not self.is_configured():
            raise RuntimeError(
                "Kalshi not configured — set KALSHI_API_KEY and "
                "KALSHI_RSA_PRIVATE_KEY_PATH (or KALSHI_RSA_PRIVATE_KEY)"
            )

        import kalshi_python

        config = kalshi_python.Configuration()
        config.host = DEMO_HOST if self._demo else PROD_HOST

        self._client = kalshi_python.KalshiClient(configuration=config)

        # Handle inline key by writing to temp file
        key_path = self._rsa_key_path
        if not key_path and self._rsa_key_inline:
            import atexit
            import tempfile
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".pem", delete=False, prefix="kalshi_rsa_"
            )
            tmp.write(self._rsa_key_inline)
            tmp.close()
            key_path = tmp.name
            # Clean up temp PEM file on process exit
            def _cleanup_pem(path=key_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            atexit.register(_cleanup_pem)

        self._client.set_kalshi_auth(self._api_key, key_path)
        env = "DEMO" if self._demo else "PRODUCTION"
        logger.info("Kalshi SDK client initialized (%s)", env)
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

    def _parse_asset_id(self, asset_id: str) -> tuple[str, str]:
        """Parse 'TICKER:YES' -> (ticker, 'yes'). Default side: yes."""
        if ":" in asset_id:
            ticker, side = asset_id.rsplit(":", 1)
            return ticker, side.lower()
        return asset_id, "yes"

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        """Place a market buy order on Kalshi."""
        try:
            from kalshi_python.models import CreateOrderRequest

            ticker, side = self._parse_asset_id(asset_id)
            client = self._get_client()

            # Get current price to calculate contract count
            market = await self._run_sync(client.get_market, ticker)
            market_data = market.market if hasattr(market, "market") else market
            yes_price_cents = getattr(market_data, "yes_price", None) or 50
            price_cents = yes_price_cents if side == "yes" else (100 - yes_price_cents)
            count = max(1, int(amount_usd / (price_cents / 100)))

            order_req = CreateOrderRequest(
                ticker=ticker,
                side=side,
                action="buy",
                type="market",
                count=count,
                client_order_id=str(uuid.uuid4()),
            )

            result = await self._run_sync(client.create_order, order_req)
            order = result.order if hasattr(result, "order") else result
            order_id = getattr(order, "order_id", "") or ""
            avg_price = getattr(order, "avg_price", price_cents)
            filled = getattr(order, "count", count)

            logger.info(
                "Kalshi BUY market: %s %s x%d @ %dc",
                ticker, side, count, avg_price,
            )

            return ExecutionResult(
                True, order_id, float(avg_price) / 100,
                float(filled), 0.0, None,
            )
        except Exception as e:
            logger.error("Kalshi buy failed for %s: %s", asset_id, e)
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        """Place a market sell order on Kalshi."""
        try:
            from kalshi_python.models import CreateOrderRequest

            ticker, side = self._parse_asset_id(asset_id)
            client = self._get_client()

            order_req = CreateOrderRequest(
                ticker=ticker,
                side=side,
                action="sell",
                type="market",
                count=int(quantity),
                client_order_id=str(uuid.uuid4()),
            )

            result = await self._run_sync(client.create_order, order_req)
            order = result.order if hasattr(result, "order") else result
            order_id = getattr(order, "order_id", "") or ""
            avg_price = getattr(order, "avg_price", 0)

            logger.info("Kalshi SELL market: %s %s x%d", ticker, side, int(quantity))

            return ExecutionResult(
                True, order_id, float(avg_price) / 100,
                float(int(quantity)), 0.0, None,
            )
        except Exception as e:
            logger.error("Kalshi sell failed for %s: %s", asset_id, e)
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def buy_limit(self, asset_id: str, amount_usd: float, price: float) -> ExecutionResult:
        """Place a limit buy order on Kalshi. Price in dollars (0.01-0.99)."""
        try:
            from kalshi_python.models import CreateOrderRequest

            ticker, side = self._parse_asset_id(asset_id)
            client = self._get_client()

            price_cents = max(1, min(99, int(round(price * 100))))
            count = max(1, int(amount_usd / price))

            order_req = CreateOrderRequest(
                ticker=ticker,
                side=side,
                action="buy",
                type="limit",
                count=count,
                yes_price=price_cents if side == "yes" else None,
                no_price=price_cents if side == "no" else None,
                client_order_id=str(uuid.uuid4()),
            )

            result = await self._run_sync(client.create_order, order_req)
            order = result.order if hasattr(result, "order") else result
            order_id = getattr(order, "order_id", "") or ""

            logger.info(
                "Kalshi BUY limit: %s %s x%d @ %dc",
                ticker, side, count, price_cents,
            )

            return ExecutionResult(True, order_id, price, float(count), 0.0, None)
        except Exception as e:
            logger.error("Kalshi buy_limit failed for %s: %s", asset_id, e)
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def sell_limit(self, asset_id: str, quantity: float, price: float) -> ExecutionResult:
        """Place a limit sell order on Kalshi. Price in dollars (0.01-0.99)."""
        try:
            from kalshi_python.models import CreateOrderRequest

            ticker, side = self._parse_asset_id(asset_id)
            client = self._get_client()

            price_cents = max(1, min(99, int(round(price * 100))))

            order_req = CreateOrderRequest(
                ticker=ticker,
                side=side,
                action="sell",
                type="limit",
                count=int(quantity),
                yes_price=price_cents if side == "yes" else None,
                no_price=price_cents if side == "no" else None,
                client_order_id=str(uuid.uuid4()),
            )

            result = await self._run_sync(client.create_order, order_req)
            order = result.order if hasattr(result, "order") else result
            order_id = getattr(order, "order_id", "") or ""

            logger.info(
                "Kalshi SELL limit: %s %s x%d @ %dc",
                ticker, side, int(quantity), price_cents,
            )

            return ExecutionResult(True, order_id, price, float(int(quantity)), 0.0, None)
        except Exception as e:
            logger.error("Kalshi sell_limit failed for %s: %s", asset_id, e)
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def check_order_status(self, order_id: str) -> dict:
        """Check status of an order on Kalshi."""
        try:
            client = self._get_client()
            result = await self._run_sync(client.get_order, order_id)
            order = result.order if hasattr(result, "order") else result

            status_map = {
                "resting": "open",
                "pending": "open",
                "executed": "filled",
                "canceled": "cancelled",
            }
            raw_status = getattr(order, "status", "unknown").lower()

            return {
                "status": status_map.get(raw_status, "unknown"),
                "order_id": order_id,
                "price": float(getattr(order, "yes_price", 0)) / 100,
                "size": float(getattr(order, "count", 0)),
                "size_matched": float(getattr(order, "count", 0)) if raw_status == "executed" else 0,
                "fee": 0.0,
            }
        except Exception as e:
            logger.warning("Kalshi check_order_status failed for %s: %s", order_id, e)
            return {"status": "unknown", "order_id": order_id, "error": str(e)}

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order on Kalshi."""
        try:
            client = self._get_client()
            await self._run_sync(client.cancel_order, order_id)
            logger.info("Kalshi order cancelled: %s", order_id)
            return True
        except Exception as e:
            logger.warning("Kalshi cancel_order failed for %s: %s", order_id, e)
            return False

    async def get_balance(self) -> BalanceResult:
        """Get Kalshi account balance in dollars."""
        try:
            client = self._get_client()
            result = await self._run_sync(client.get_balance)
            bal = result.balance if hasattr(result, "balance") else result
            available = float(getattr(bal, "available_balance", 0)) / 100
            total = float(getattr(bal, "portfolio_value", 0)) / 100
            return BalanceResult(available, total)
        except Exception as e:
            logger.warning("Kalshi get_balance failed: %s", e)
            return BalanceResult(0, 0)

    async def get_positions(self) -> list[PositionInfo]:
        """Get current Kalshi positions."""
        try:
            client = self._get_client()
            result = await self._run_sync(client.get_positions)
            positions_data = result.positions if hasattr(result, "positions") else []
            out = []
            for p in (positions_data or []):
                ticker = getattr(p, "ticker", "")
                qty = float(getattr(p, "total_traded", 0))
                if qty == 0:
                    continue
                market_price = float(getattr(p, "market_price", 50)) / 100
                avg_price = float(getattr(p, "average_price", 50)) / 100
                pnl = (market_price - avg_price) * qty
                out.append(PositionInfo(ticker, qty, avg_price, market_price, pnl))
            return out
        except Exception as e:
            logger.warning("Kalshi get_positions failed: %s", e)
            return []

    async def get_current_price(self, asset_id: str) -> float:
        """Get current price for a Kalshi market. Returns price in dollars."""
        try:
            ticker = asset_id.split(":")[0] if ":" in asset_id else asset_id
            _, side = self._parse_asset_id(asset_id)
            client = self._get_client()
            result = await self._run_sync(client.get_market, ticker)
            market = result.market if hasattr(result, "market") else result
            yes_price = float(getattr(market, "yes_price", 50))
            if side == "no":
                return (100 - yes_price) / 100
            return yes_price / 100
        except Exception as e:
            logger.warning("Kalshi get_current_price failed for %s: %s", asset_id, e)
            return 0.0

    async def close(self):
        """Clean up SDK client."""
        if self._client is not None:
            try:
                self._client.__exit__(None, None, None)
            except Exception:
                pass
            self._client = None
