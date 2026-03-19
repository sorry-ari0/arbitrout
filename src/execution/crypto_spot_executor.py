"""Crypto spot executor — real exchange trading via CCXT (113 exchanges).

Supports two asset_id formats:
  1. Direct spot: 'BTC/USDT' or 'ETH/USDT' — buys/sells the actual crypto
  2. Synthetic probability: 'crypto-btc-100000:YES' — implied probability from
     spot price (for cross-referencing with prediction markets)

Exchange priority (uses first one with API keys configured):
  Kraken → Coinbase → Binance → Bybit → OKX → KuCoin → Bitget

In paper mode, PaperExecutor wraps this and calls get_current_price() for
real exchange prices while simulating the trades.
"""
import asyncio
import logging
import math
import os
from datetime import date, datetime

from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.crypto_spot")

# Exchange configs: name → (env_key, env_secret, optional env_password)
EXCHANGE_CHAIN = [
    ("kraken",           "KRAKEN_API_KEY",      "KRAKEN_API_SECRET",      None),
    ("coinbaseadvanced", "COINBASE_ADV_API_KEY", "COINBASE_ADV_API_SECRET", None),
    ("binance",          "BINANCE_API_KEY",      "BINANCE_API_SECRET",      None),
    ("bybit",            "BYBIT_API_KEY",        "BYBIT_API_SECRET",        None),
    ("okx",              "OKX_API_KEY",          "OKX_API_SECRET",          "OKX_PASSPHRASE"),
    ("kucoin",           "KUCOIN_API_KEY",       "KUCOIN_API_SECRET",       "KUCOIN_PASSPHRASE"),
    ("bitget",           "BITGET_API_KEY",       "BITGET_API_SECRET",       "BITGET_PASSPHRASE"),
]

# Default trading pair suffix (most exchanges use USDT, Kraken/Coinbase use USD)
EXCHANGE_QUOTE = {
    "kraken": "USD",
    "coinbaseadvanced": "USD",
    "coinbase": "USD",
}

# Symbol mapping for synthetic probability asset_ids
SYMBOL_TO_COINGECKO = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "DOGE": "dogecoin", "XRP": "ripple", "ADA": "cardano",
    "AVAX": "avalanche-2", "LINK": "chainlink", "DOT": "polkadot",
    "POL": "polygon-ecosystem-token",
}

# Standard trading pairs per symbol
SYMBOL_PAIRS = {
    "BTC": "BTC/USDT", "ETH": "ETH/USDT", "SOL": "SOL/USDT",
    "DOGE": "DOGE/USDT", "XRP": "XRP/USDT", "ADA": "ADA/USDT",
    "AVAX": "AVAX/USDT", "LINK": "LINK/USDT", "DOT": "DOT/USDT",
    "POL": "POL/USDT",
}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _implied_probability(current_price: float, threshold: float,
                         volatility: float = 0.6, expiry_date: str = "2026-12-31") -> float:
    """Estimate probability of price exceeding threshold using log-normal model."""
    if current_price <= 0 or threshold <= 0:
        return 0.5
    try:
        exp = datetime.strptime(expiry_date, "%Y-%m-%d").date()
        days_remaining = (exp - date.today()).days
        t = max(days_remaining / 365.0, 1 / 365.0)
    except (ValueError, TypeError):
        t = 0.5
    sigma_sqrt_t = volatility * math.sqrt(t)
    if sigma_sqrt_t == 0:
        return 1.0 if current_price >= threshold else 0.0
    d2 = (math.log(current_price / threshold) + (-0.5 * volatility ** 2) * t) / sigma_sqrt_t
    return _norm_cdf(d2)


class CryptoSpotExecutor(BaseExecutor):
    """Real crypto spot trading via CCXT, with synthetic probability support.

    Lazily initializes the first available exchange from EXCHANGE_CHAIN.
    Falls back to CoinGecko for prices if no exchange is configured.
    """

    def __init__(self):
        self._exchange = None
        self._exchange_name = None
        self._initialized = False

    def _detect_exchange(self) -> tuple[str, dict] | None:
        """Find the first exchange with API keys configured."""
        for name, key_env, secret_env, pass_env in EXCHANGE_CHAIN:
            api_key = os.environ.get(key_env, "")
            api_secret = os.environ.get(secret_env, "")
            if api_key and api_secret:
                config = {
                    "apiKey": api_key,
                    "secret": api_secret,
                    "enableRateLimit": True,
                }
                if pass_env:
                    passphrase = os.environ.get(pass_env, "")
                    if passphrase:
                        config["password"] = passphrase
                return name, config
        return None

    async def _get_exchange(self):
        """Lazy-init the CCXT async exchange client."""
        if self._exchange and not getattr(self._exchange, '_closed', False):
            return self._exchange

        try:
            import ccxt.async_support as ccxt
        except ImportError:
            logger.error("ccxt not installed — run: pip install ccxt")
            return None

        detected = self._detect_exchange()
        if not detected:
            logger.info("No crypto exchange API keys configured — price-only mode")
            return None

        name, config = detected
        exchange_class = getattr(ccxt, name, None)
        if not exchange_class:
            logger.error("CCXT exchange '%s' not found", name)
            return None

        self._exchange = exchange_class(config)
        self._exchange_name = name
        self._initialized = True
        logger.info("Crypto spot executor initialized with %s", name)
        return self._exchange

    def is_configured(self) -> bool:
        # Always configured — can at least fetch prices via CCXT public API
        return True

    def _resolve_pair(self, asset_id: str) -> str:
        """Resolve asset_id to a trading pair.

        'BTC' → 'BTC/USDT' (or BTC/USD for Kraken/Coinbase)
        'BTC/USDT' → 'BTC/USDT' (pass through)
        'crypto-btc-100000:YES' → 'BTC/USDT' (extract symbol)
        """
        # Already a pair
        if "/" in asset_id:
            return asset_id.upper()

        # Synthetic probability format: crypto-btc-100000:YES
        if asset_id.startswith("crypto-"):
            parts = asset_id.split(":")[0].split("-")
            if len(parts) >= 2:
                symbol = parts[1].upper()
            else:
                return asset_id.upper()
        else:
            symbol = asset_id.split(":")[0].upper()

        # Use exchange-specific quote currency
        quote = EXCHANGE_QUOTE.get(self._exchange_name, "USDT")

        # Check known pairs first
        if symbol in SYMBOL_PAIRS:
            pair = SYMBOL_PAIRS[symbol]
            if quote != "USDT":
                pair = f"{symbol}/{quote}"
            return pair

        return f"{symbol}/{quote}"

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        """Buy crypto spot via CCXT market order."""
        exchange = await self._get_exchange()
        if not exchange:
            return ExecutionResult(False, None, 0, 0, 0,
                                   "No crypto exchange configured — set API keys for Kraken/Binance/etc.")
        try:
            pair = self._resolve_pair(asset_id)
            ticker = await exchange.fetch_ticker(pair)
            price = ticker.get("last", 0) or ticker.get("ask", 0)
            if price <= 0:
                return ExecutionResult(False, None, 0, 0, 0, f"Cannot get price for {pair}")

            amount = amount_usd / price
            order = await exchange.create_market_buy_order(pair, amount)

            fill_price = float(order.get("average", order.get("price", price)))
            fill_qty = float(order.get("filled", order.get("amount", amount)))
            fees = float(order.get("fee", {}).get("cost", 0))
            order_id = order.get("id", "")

            logger.info("Crypto spot BUY %s: %.6f @ $%.2f on %s (order=%s)",
                        pair, fill_qty, fill_price, self._exchange_name, order_id)
            return ExecutionResult(True, order_id, fill_price, fill_qty, fees, None)

        except Exception as e:
            logger.error("Crypto spot buy failed for %s: %s", asset_id, e)
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        """Sell crypto spot via CCXT market order."""
        exchange = await self._get_exchange()
        if not exchange:
            return ExecutionResult(False, None, 0, 0, 0,
                                   "No crypto exchange configured")
        try:
            pair = self._resolve_pair(asset_id)
            order = await exchange.create_market_sell_order(pair, quantity)

            fill_price = float(order.get("average", order.get("price", 0)))
            fill_qty = float(order.get("filled", order.get("amount", quantity)))
            fees = float(order.get("fee", {}).get("cost", 0))
            order_id = order.get("id", "")

            logger.info("Crypto spot SELL %s: %.6f @ $%.2f on %s (order=%s)",
                        pair, fill_qty, fill_price, self._exchange_name, order_id)
            return ExecutionResult(True, order_id, fill_price, fill_qty, fees, None)

        except Exception as e:
            logger.error("Crypto spot sell failed for %s: %s", asset_id, e)
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def get_balance(self) -> BalanceResult:
        """Fetch total balance from exchange."""
        exchange = await self._get_exchange()
        if not exchange:
            return BalanceResult(0, 0)
        try:
            balance = await exchange.fetch_balance()
            # Sum up USD/USDT free balance
            free = float(balance.get("free", {}).get("USDT", 0))
            free += float(balance.get("free", {}).get("USD", 0))
            total = float(balance.get("total", {}).get("USDT", 0))
            total += float(balance.get("total", {}).get("USD", 0))
            return BalanceResult(free, total)
        except Exception as e:
            logger.warning("Crypto spot balance failed: %s", e)
            return BalanceResult(0, 0)

    async def get_positions(self) -> list[PositionInfo]:
        """Fetch open positions (non-zero balances)."""
        exchange = await self._get_exchange()
        if not exchange:
            return []
        try:
            balance = await exchange.fetch_balance()
            positions = []
            for symbol, qty in balance.get("total", {}).items():
                qty = float(qty)
                if qty > 0 and symbol not in ("USD", "USDT", "USDC"):
                    try:
                        pair = f"{symbol}/USDT"
                        ticker = await exchange.fetch_ticker(pair)
                        price = float(ticker.get("last", 0))
                        positions.append(PositionInfo(symbol, qty, 0, price, qty * price))
                    except Exception:
                        pass
            return positions
        except Exception as e:
            logger.warning("Crypto spot positions failed: %s", e)
            return []

    async def get_current_price(self, asset_id: str) -> float:
        """Fetch current price. Handles both formats:

        1. Direct: 'BTC/USDT' or 'BTC' → spot price in USD
        2. Synthetic: 'crypto-btc-100000:YES' → implied probability (0-1)
        """
        is_synthetic = asset_id.startswith("crypto-") and "-" in asset_id.split(":")[0][7:]

        if is_synthetic:
            return await self._get_synthetic_price(asset_id)
        return await self._get_spot_price(asset_id)

    async def _get_spot_price(self, asset_id: str) -> float:
        """Get real spot price from exchange or CoinGecko fallback."""
        # Try CCXT exchange first (most reliable, real-time)
        exchange = await self._get_exchange()
        if exchange:
            try:
                pair = self._resolve_pair(asset_id)
                ticker = await exchange.fetch_ticker(pair)
                price = float(ticker.get("last", 0))
                if price > 0:
                    return price
            except Exception as e:
                logger.debug("CCXT price failed for %s: %s", asset_id, e)

        # Fallback: CCXT public API (no auth needed)
        try:
            import ccxt.async_support as ccxt_async
            # Use Kraken public API (no auth needed for tickers)
            pub = ccxt_async.kraken({"enableRateLimit": True})
            try:
                pair = self._resolve_pair(asset_id)
                # Kraken uses USD not USDT for major pairs
                if "USDT" in pair:
                    pair = pair.replace("/USDT", "/USD")
                ticker = await pub.fetch_ticker(pair)
                price = float(ticker.get("last", 0))
                if price > 0:
                    return price
            finally:
                await pub.close()
        except Exception as e:
            logger.debug("CCXT public price failed for %s: %s", asset_id, e)

        # Final fallback: CoinGecko
        return await self._coingecko_price(asset_id)

    async def _get_synthetic_price(self, asset_id: str) -> float:
        """Compute implied probability for synthetic markets."""
        try:
            base = asset_id.split(":")[0]
            side = asset_id.split(":")[1].upper() if ":" in asset_id else "YES"

            parts = base.split("-")
            if len(parts) < 3:
                return 0.0

            symbol = parts[1].upper()
            threshold = float(parts[2])

            # Get real spot price
            spot_price = await self._get_spot_price(symbol)
            if spot_price <= 0:
                return 0.0

            prob = _implied_probability(spot_price, threshold)
            return round(1.0 - prob, 4) if side == "NO" else round(prob, 4)

        except Exception as e:
            logger.debug("Synthetic price failed for %s: %s", asset_id, e)
        return 0.0

    async def _coingecko_price(self, asset_id: str) -> float:
        """Fallback price from CoinGecko free API."""
        import httpx
        symbol = asset_id.split("/")[0].split(":")[0].upper()
        if symbol.startswith("CRYPTO-"):
            symbol = symbol.split("-")[1].upper() if "-" in symbol else symbol

        coingecko_id = SYMBOL_TO_COINGECKO.get(symbol)
        if not coingecko_id:
            return 0.0

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get("https://api.coingecko.com/api/v3/simple/price",
                                     params={"ids": coingecko_id, "vs_currencies": "usd"})
                if r.status_code == 200:
                    return float(r.json().get(coingecko_id, {}).get("usd", 0))
        except Exception as e:
            logger.debug("CoinGecko price failed for %s: %s", symbol, e)
        return 0.0

    async def close(self):
        if self._exchange:
            try:
                await self._exchange.close()
            except Exception:
                pass
            self._exchange = None
