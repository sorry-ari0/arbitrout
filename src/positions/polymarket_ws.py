"""Polymarket CLOB WebSocket — real-time price updates for open positions.

Connects to Polymarket's WebSocket feed and maintains a live price cache
for tracked condition IDs. Used by exit engine and auto trader for
faster price discovery instead of polling the Gamma API.

Protocol: ws://ws-subscriptions-clob.polymarket.com/ws/market
Subscribe: {"type": "market", "assets_ids": ["0xabc..."]}
Messages: {"event_type": "price_change", "asset_id": "...", "price": "0.55", ...}
"""
import asyncio
import json
import logging
import time
from collections import defaultdict

logger = logging.getLogger("positions.polymarket_ws")

POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RECONNECT_BASE_DELAY = 2.0
RECONNECT_MAX_DELAY = 60.0
PRICE_STALE_SECONDS = 120  # Consider price stale after 2 min without update


class PolymarketPriceFeed:
    """Real-time price cache via Polymarket CLOB WebSocket."""

    def __init__(self):
        self._prices: dict[str, float] = {}  # asset_id → last price
        self._updated_at: dict[str, float] = {}  # asset_id → timestamp
        self._subscribed: set[str] = set()  # tracked condition IDs
        self._task: asyncio.Task | None = None
        self._running = False
        self._connected = False
        self._ws = None
        self._reconnect_delay = RECONNECT_BASE_DELAY
        self._on_price_callbacks: list = []

    def get_price(self, asset_id: str) -> float | None:
        """Get latest price for an asset. Returns None if not tracked or stale."""
        price = self._prices.get(asset_id)
        if price is None:
            return None
        updated = self._updated_at.get(asset_id, 0)
        if time.time() - updated > PRICE_STALE_SECONDS:
            return None  # Stale
        return price

    def get_prices(self) -> dict[str, float]:
        """Get all non-stale prices."""
        now = time.time()
        return {
            aid: price for aid, price in self._prices.items()
            if now - self._updated_at.get(aid, 0) <= PRICE_STALE_SECONDS
        }

    def subscribe(self, condition_ids: list[str]):
        """Add condition IDs to track. Triggers re-subscribe on active connection."""
        new_ids = set(condition_ids) - self._subscribed
        if new_ids:
            self._subscribed.update(new_ids)
            if self._connected and self._ws:
                asyncio.create_task(self._send_subscribe(list(new_ids)))

    def unsubscribe(self, condition_ids: list[str]):
        """Stop tracking condition IDs."""
        for cid in condition_ids:
            self._subscribed.discard(cid)
            self._prices.pop(cid, None)
            self._updated_at.pop(cid, None)

    def on_price(self, callback):
        """Register callback for price updates: callback(asset_id, price, timestamp)."""
        self._on_price_callbacks.append(callback)

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def tracked_count(self) -> int:
        return len(self._subscribed)

    def start(self):
        """Start the WebSocket connection loop."""
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._connection_loop())
        logger.info("Polymarket WS feed started, tracking %d assets", len(self._subscribed))

    def stop(self):
        """Stop the WebSocket connection."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        self._connected = False
        logger.info("Polymarket WS feed stopped")

    async def _connection_loop(self):
        """Reconnecting WebSocket loop with exponential backoff."""
        try:
            import websockets
        except ImportError:
            logger.warning("websockets package not available — Polymarket WS feed disabled")
            return

        while self._running:
            try:
                async with websockets.connect(
                    POLYMARKET_WS_URL,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._reconnect_delay = RECONNECT_BASE_DELAY
                    logger.info("Polymarket WS connected")

                    # Subscribe to tracked assets
                    if self._subscribed:
                        await self._send_subscribe(list(self._subscribed))

                    # Read messages
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            self._handle_message(msg)
                        except json.JSONDecodeError:
                            pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._ws = None
                if self._running:
                    logger.warning("Polymarket WS disconnected: %s (reconnecting in %.0fs)",
                                   e, self._reconnect_delay)
                    await asyncio.sleep(self._reconnect_delay)
                    self._reconnect_delay = min(
                        self._reconnect_delay * 2, RECONNECT_MAX_DELAY
                    )

        self._connected = False
        self._ws = None

    async def _send_subscribe(self, asset_ids: list[str]):
        """Send subscription message for given asset IDs."""
        if not self._ws:
            return
        try:
            # Polymarket CLOB WS expects token_id (condition_id based)
            # Subscribe format may vary — try both known formats
            msg = json.dumps({
                "type": "subscribe",
                "channel": "market",
                "assets_ids": asset_ids,
            })
            await self._ws.send(msg)
            logger.debug("Subscribed to %d assets", len(asset_ids))
        except Exception as e:
            logger.debug("Subscribe failed: %s", e)

    def _handle_message(self, msg: dict):
        """Process incoming WebSocket message."""
        event_type = msg.get("event_type", msg.get("type", ""))

        if event_type in ("price_change", "book", "trade", "last_trade_price"):
            asset_id = msg.get("asset_id", msg.get("market", ""))
            price = None

            # Try various price fields
            for field in ("price", "last_trade_price", "best_bid", "yes_price"):
                val = msg.get(field)
                if val is not None:
                    try:
                        price = float(val)
                        break
                    except (ValueError, TypeError):
                        pass

            if asset_id and price is not None and 0 < price < 1:
                self._prices[asset_id] = price
                self._updated_at[asset_id] = time.time()

                # Fire callbacks
                for cb in self._on_price_callbacks:
                    try:
                        cb(asset_id, price, time.time())
                    except Exception:
                        pass

    def get_stats(self) -> dict:
        """Return feed status for API/dashboard."""
        now = time.time()
        fresh = sum(1 for t in self._updated_at.values()
                    if now - t <= PRICE_STALE_SECONDS)
        return {
            "connected": self._connected,
            "tracked": len(self._subscribed),
            "cached_prices": len(self._prices),
            "fresh_prices": fresh,
            "stale_prices": len(self._prices) - fresh,
        }
