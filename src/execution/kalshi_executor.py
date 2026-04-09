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

try:
    import httpx
except ImportError:
    httpx = None

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
        self._executable_quote_source_counts = {
            "market_fields": 0,
            "orderbook_derived": 0,
            "midpoint_fallback": 0,
        }

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

    def _record_quote_source(self, source: str):
        self._executable_quote_source_counts[source] = self._executable_quote_source_counts.get(source, 0) + 1

    @staticmethod
    def _field_price(value, *, dollars: bool) -> float:
        price = float(value)
        return price if dollars else price / 100.0

    async def _fetch_public_orderbook(self, ticker: str) -> dict:
        if not httpx:
            return {}
        host = DEMO_HOST if self._demo else PROD_HOST
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{host}/markets/{ticker}/orderbook")
            if resp.status_code != 200:
                return {}
            data = resp.json()
            return data.get("orderbook_fp", data.get("orderbook", {}))

    @staticmethod
    def _best_bid(levels) -> float:
        best = 0.0
        for level in levels or []:
            if isinstance(level, dict):
                price = float(level.get("price", 0) or 0)
            else:
                price = float(level[0] if len(level) >= 1 else 0)
            best = max(best, price)
        return best

    async def _orderbook_executable_price(self, ticker: str, outcome: str, side: str) -> float:
        book = await self._fetch_public_orderbook(ticker)
        yes_bids = book.get("yes_dollars", book.get("yes", []))
        no_bids = book.get("no_dollars", book.get("no", []))
        if outcome == "yes":
            if side.lower() == "sell":
                return self._best_bid(yes_bids)
            opposite = self._best_bid(no_bids)
            return max(0.0, round(1.0 - opposite, 4)) if opposite > 0 else 0.0
        if side.lower() == "sell":
            return self._best_bid(no_bids)
        opposite = self._best_bid(yes_bids)
        return max(0.0, round(1.0 - opposite, 4)) if opposite > 0 else 0.0

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        """Place a limit buy order on Kalshi (maker, 0% fee)."""
        try:
            from kalshi_python.models import CreateOrderRequest

            ticker, side = self._parse_asset_id(asset_id)
            client = self._get_client()

            # Get current price to calculate contract count + limit price
            market = await self._run_sync(client.get_market, ticker)
            market_data = market.market if hasattr(market, "market") else market
            yes_price_cents = getattr(market_data, "yes_price", None) or 50
            price_cents = yes_price_cents if side == "yes" else (100 - yes_price_cents)
            count = max(1, int(amount_usd / (price_cents / 100)))

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
            avg_price = getattr(order, "avg_price", price_cents)
            filled = getattr(order, "count", count)

            logger.info(
                "Kalshi BUY limit: %s %s x%d @ %dc",
                ticker, side, count, price_cents,
            )

            return ExecutionResult(
                True, order_id, float(avg_price) / 100,
                float(filled), 0.0, None,
            )
        except Exception as e:
            logger.error("Kalshi buy failed for %s: %s", asset_id, e)
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        """Place a limit sell order on Kalshi (maker, 0% fee)."""
        try:
            from kalshi_python.models import CreateOrderRequest

            ticker, side = self._parse_asset_id(asset_id)
            client = self._get_client()

            # Get current price for limit
            market = await self._run_sync(client.get_market, ticker)
            market_data = market.market if hasattr(market, "market") else market
            yes_price_cents = getattr(market_data, "yes_price", None) or 50
            price_cents = yes_price_cents if side == "yes" else (100 - yes_price_cents)

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
            avg_price = getattr(order, "avg_price", price_cents)

            logger.info("Kalshi SELL limit: %s %s x%d @ %dc", ticker, side, int(quantity), price_cents)

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

    async def get_executable_price(self, asset_id: str, side: str = "buy",
                                   amount_usd: float = 0.0) -> float:
        """Prefer fixed-point market fields, then derive from the public orderbook."""
        try:
            ticker = asset_id.split(":")[0] if ":" in asset_id else asset_id
            _, outcome = self._parse_asset_id(asset_id)
            client = self._get_client()
            result = await self._run_sync(client.get_market, ticker)
            market = result.market if hasattr(result, "market") else result

            if outcome == "no":
                if side.lower() == "sell":
                    bid = getattr(market, "no_bid_dollars", None)
                    if bid is not None:
                        self._record_quote_source("market_fields")
                        return self._field_price(bid, dollars=True)
                    bid = getattr(market, "no_bid", None)
                    if bid is not None:
                        self._record_quote_source("market_fields")
                        return self._field_price(bid, dollars=False)
                ask = getattr(market, "no_ask_dollars", None)
                if ask is not None:
                    self._record_quote_source("market_fields")
                    return self._field_price(ask, dollars=True)
                ask = getattr(market, "no_ask", None)
                if ask is not None:
                    self._record_quote_source("market_fields")
                    return self._field_price(ask, dollars=False)
            else:
                if side.lower() == "sell":
                    bid = getattr(market, "yes_bid_dollars", None)
                    if bid is not None:
                        self._record_quote_source("market_fields")
                        return self._field_price(bid, dollars=True)
                    bid = getattr(market, "yes_bid", None)
                    if bid is not None:
                        self._record_quote_source("market_fields")
                        return self._field_price(bid, dollars=False)
                ask = getattr(market, "yes_ask_dollars", None)
                if ask is not None:
                    self._record_quote_source("market_fields")
                    return self._field_price(ask, dollars=True)
                ask = getattr(market, "yes_ask", None)
                if ask is not None:
                    self._record_quote_source("market_fields")
                    return self._field_price(ask, dollars=False)
        except Exception as e:
            logger.debug("Kalshi executable quote fallback for %s: %s", asset_id, e)
        try:
            ticker = asset_id.split(":")[0] if ":" in asset_id else asset_id
            _, outcome = self._parse_asset_id(asset_id)
            orderbook_price = await self._orderbook_executable_price(ticker, outcome, side)
            if orderbook_price > 0:
                self._record_quote_source("orderbook_derived")
                return orderbook_price
        except Exception as e:
            logger.debug("Kalshi orderbook-derived quote fallback for %s: %s", asset_id, e)
        self._record_quote_source("midpoint_fallback")
        return await self.get_current_price(asset_id)

    def get_quote_stats(self) -> dict:
        return dict(self._executable_quote_source_counts)

    async def close(self):
        """Clean up SDK client."""
        if self._client is not None:
            try:
                self._client.__exit__(None, None, None)
            except Exception:
                pass
            self._client = None
