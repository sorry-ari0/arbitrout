"""Shared Binance WebSocket price feed for real-time crypto data.

Provides:
- Real-time spot prices via WebSocket trades stream (BTC, ETH, SOL, XRP)
- 1-minute candle history (last 10 candles per asset)
- 5-minute window open price tracking
- Micro momentum and tick trend signals for sniper
- Event-driven callbacks: on_tick fires on every trade for preemptive cancel

Single combined stream shared between sniper and market maker.
"""
import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger("positions.price_feed")

# Supported assets and their Binance pairs
SUPPORTED_ASSETS = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "SOL": "solusdt",
    "XRP": "xrpusdt",
}

BINANCE_WS_BASE = "wss://stream.binance.com:9443"


@dataclass
class Candle:
    """1-minute candle."""
    open_time: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool = False


@dataclass
class WindowState:
    """State for a 5-minute prediction market window."""
    window_ts: int          # Unix timestamp of window start (divisible by 300)
    open_price: float       # BTC price at window open
    current_price: float    # Latest BTC price
    high: float = 0.0
    low: float = float('inf')
    tick_count: int = 0


@dataclass
class SniperSignal:
    """Composite signal for a 5-min sniper."""
    asset: str              # "BTC", "ETH", etc.
    direction: str          # "UP" or "DOWN"
    confidence: float       # 0.0 to 1.0
    window_delta_pct: float # % change from window open
    score: float            # Raw weighted score
    components: dict = field(default_factory=dict)


@dataclass
class AssetState:
    """Per-asset tracking state."""
    symbol: str             # "BTC", "ETH", etc.
    price: float = 0.0
    price_time: float = 0.0
    candles: deque = field(default_factory=lambda: deque(maxlen=10))
    current_candle: Candle | None = None
    window: WindowState | None = None
    tick_samples: deque = field(default_factory=lambda: deque(maxlen=30))
    last_sample_time: float = 0.0

    @property
    def price_age(self) -> float:
        return time.time() - self.price_time if self.price_time else float('inf')

    @property
    def is_stale(self) -> bool:
        return self.price_age > 5.0


# Callback type: (asset: str, price: float, timestamp: float) -> None
TickCallback = Callable[[str, float, float], None]


class BinancePriceFeed:
    """Real-time multi-asset price feed from Binance WebSocket.

    Maintains per-asset:
    - Latest spot price
    - 1-minute candle history (last 10)
    - 5-minute window state
    - 2-second tick samples for micro-trend detection

    Event-driven: fires on_tick callbacks on every trade for
    preemptive cancel and instant signal evaluation.
    """

    def __init__(self, assets: list[str] | None = None):
        self._assets = [a.upper() for a in (assets or ["BTC"])]
        self._running = False
        self._task: asyncio.Task | None = None
        self._kline_task: asyncio.Task | None = None

        # Per-asset state
        self._state: dict[str, AssetState] = {
            sym: AssetState(symbol=sym) for sym in self._assets
        }

        # Subscribers for price updates (legacy queue-based)
        self._subscribers: list[asyncio.Queue] = []

        # Event-driven tick callbacks (synchronous, called on each trade)
        self._on_tick_callbacks: list[TickCallback] = []

    # ============================================================
    # BACKWARD-COMPATIBLE BTC PROPERTIES
    # ============================================================

    @property
    def price(self) -> float:
        """BTC price (backward compat)."""
        return self._state.get("BTC", AssetState("BTC")).price

    @property
    def price_age(self) -> float:
        return self._state.get("BTC", AssetState("BTC")).price_age

    @property
    def is_stale(self) -> bool:
        return self._state.get("BTC", AssetState("BTC")).is_stale

    @property
    def window(self) -> WindowState | None:
        return self._state.get("BTC", AssetState("BTC")).window

    @property
    def candles(self) -> list[Candle]:
        s = self._state.get("BTC")
        return list(s.candles) if s else []

    # ============================================================
    # MULTI-ASSET ACCESS
    # ============================================================

    def get_asset(self, symbol: str) -> AssetState | None:
        """Get state for a specific asset."""
        return self._state.get(symbol.upper())

    def get_price(self, symbol: str) -> float:
        """Get latest price for an asset."""
        s = self._state.get(symbol.upper())
        return s.price if s else 0.0

    def is_asset_stale(self, symbol: str) -> bool:
        s = self._state.get(symbol.upper())
        return s.is_stale if s else True

    @property
    def active_assets(self) -> list[str]:
        return self._assets

    # ============================================================
    # SUBSCRIPTIONS
    # ============================================================

    def subscribe(self) -> asyncio.Queue:
        """Subscribe to price updates. Returns a queue that receives (asset, price, timestamp) tuples."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subscribers:
            self._subscribers.remove(q)

    def on_tick(self, callback: TickCallback):
        """Register a synchronous callback fired on every trade tick.

        Used by market maker for preemptive cancel — called inline
        in the WebSocket handler for minimum latency.
        """
        self._on_tick_callbacks.append(callback)

    def remove_on_tick(self, callback: TickCallback):
        if callback in self._on_tick_callbacks:
            self._on_tick_callbacks.remove(callback)

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._trade_stream())
        self._kline_task = asyncio.ensure_future(self._kline_stream())
        logger.info("Binance price feed started (assets: %s)", ", ".join(self._assets))

    def stop(self):
        self._running = False
        for task in [self._task, self._kline_task]:
            if task and not task.done():
                task.cancel()
        logger.info("Binance price feed stopped")

    # ============================================================
    # SIGNAL COMPUTATION
    # ============================================================

    def get_current_window(self, asset: str = "BTC") -> WindowState | None:
        """Get the current 5-minute window state for an asset, creating if needed."""
        state = self._state.get(asset.upper())
        if not state:
            return None

        now = time.time()
        window_ts = int(now) - (int(now) % 300)

        if state.window is None or state.window.window_ts != window_ts:
            if state.price > 0:
                state.window = WindowState(
                    window_ts=window_ts,
                    open_price=state.price,
                    current_price=state.price,
                    high=state.price,
                    low=state.price,
                )
            else:
                return None

        return state.window

    def compute_sniper_signal(self, asset: str = "BTC") -> SniperSignal | None:
        """Compute composite signal for the current 5-minute window.

        Returns None if insufficient data.
        """
        state = self._state.get(asset.upper())
        if not state:
            return None

        window = self.get_current_window(asset)
        if not window or window.open_price <= 0 or state.price <= 0:
            return None

        components = {}
        total_score = 0.0

        # 1. Window delta (weight 5-7) — the dominant signal
        delta_pct = (state.price - window.open_price) / window.open_price * 100
        abs_delta = abs(delta_pct)

        if abs_delta > 0.10:
            delta_weight = 7.0
        elif abs_delta > 0.05:
            delta_weight = 5.0 + (abs_delta - 0.05) / 0.05 * 2.0
        elif abs_delta > 0.02:
            delta_weight = 3.0 + (abs_delta - 0.02) / 0.03 * 2.0
        elif abs_delta > 0.001:
            delta_weight = 1.0 + (abs_delta - 0.001) / 0.019 * 2.0
        else:
            delta_weight = 0.0

        delta_signal = delta_weight if delta_pct > 0 else -delta_weight
        total_score += delta_signal
        components["window_delta"] = round(delta_signal, 2)
        components["delta_pct"] = round(delta_pct, 4)

        # 2. Micro momentum (weight 2) — direction of last two 1-min candle closes
        closed_candles = [c for c in state.candles if c.closed]
        if len(closed_candles) >= 2:
            c1, c2 = closed_candles[-2], closed_candles[-1]
            if c1.close > c1.open and c2.close > c2.open:
                momentum = 2.0  # Both bullish
            elif c1.close < c1.open and c2.close < c2.open:
                momentum = -2.0  # Both bearish
            elif c2.close > c2.open:
                momentum = 1.0  # Latest bullish
            elif c2.close < c2.open:
                momentum = -1.0  # Latest bearish
            else:
                momentum = 0.0
            total_score += momentum
            components["micro_momentum"] = round(momentum, 2)

        # 3. Tick trend (weight 2) — micro-trend from recent tick samples
        if len(state.tick_samples) >= 3:
            recent = list(state.tick_samples)[-5:]  # Last 5 samples (~10 seconds)
            if len(recent) >= 2:
                first_price = recent[0][1]
                last_price = recent[-1][1]
                tick_delta = (last_price - first_price) / first_price * 100
                if abs(tick_delta) > 0.01:
                    tick_signal = 2.0 if tick_delta > 0 else -2.0
                elif abs(tick_delta) > 0.005:
                    tick_signal = 1.0 if tick_delta > 0 else -1.0
                else:
                    tick_signal = 0.0
                total_score += tick_signal
                components["tick_trend"] = round(tick_signal, 2)

        # Confidence: normalize by max possible score (7 + 2 + 2 = 11, but 7 is practical max)
        confidence = min(abs(total_score) / 7.0, 1.0)
        direction = "UP" if total_score > 0 else "DOWN"

        return SniperSignal(
            asset=asset.upper(),
            direction=direction,
            confidence=round(confidence, 3),
            window_delta_pct=round(delta_pct, 4),
            score=round(total_score, 2),
            components=components,
        )

    def seconds_until_window_close(self) -> float:
        """Seconds remaining in the current 5-minute window."""
        now = time.time()
        window_ts = int(now) - (int(now) % 300)
        close_time = window_ts + 300
        return max(0, close_time - now)

    def current_window_slug(self, asset: str = "BTC") -> str:
        """Polymarket market slug for the current 5-minute window."""
        now = time.time()
        window_ts = int(now) - (int(now) % 300)
        prefix = asset.lower()
        return f"{prefix}-updown-5m-{window_ts}"

    # ============================================================
    # INTERNAL: TICK PROCESSING
    # ============================================================

    def _process_tick(self, asset: str, price: float, trade_time: float):
        """Process a single trade tick — updates state and fires callbacks.

        Called inline from WebSocket handler for minimum latency.
        """
        state = self._state.get(asset)
        if not state or price <= 0:
            return

        state.price = price
        state.price_time = time.time()

        # Update window state
        window = self.get_current_window(asset)
        if window:
            window.current_price = price
            window.high = max(window.high, price)
            window.low = min(window.low, price)
            window.tick_count += 1

        # Sample for tick trend (every 2 seconds)
        now = time.time()
        if now - state.last_sample_time >= 2.0:
            state.tick_samples.append((now, price))
            state.last_sample_time = now

        # Fire on_tick callbacks (synchronous, inline — for preemptive cancel)
        for cb in self._on_tick_callbacks:
            try:
                cb(asset, price, trade_time)
            except Exception as e:
                logger.debug("on_tick callback error: %s", e)

        # Notify queue subscribers (non-blocking)
        for q in self._subscribers:
            try:
                q.put_nowait((asset, price, trade_time))
            except asyncio.QueueFull:
                pass

    # ============================================================
    # WEBSOCKET STREAMS
    # ============================================================

    def _build_combined_stream_url(self, stream_type: str) -> str:
        """Build Binance combined stream URL for all assets.

        stream_type: 'trade' or 'kline_1m'
        """
        if len(self._assets) == 1:
            pair = SUPPORTED_ASSETS[self._assets[0]]
            suffix = "trade" if stream_type == "trade" else "kline_1m"
            return f"{BINANCE_WS_BASE}/ws/{pair}@{suffix}"

        # Combined stream for multiple assets
        streams = []
        for asset in self._assets:
            pair = SUPPORTED_ASSETS.get(asset)
            if pair:
                suffix = "trade" if stream_type == "trade" else "kline_1m"
                streams.append(f"{pair}@{suffix}")
        return f"{BINANCE_WS_BASE}/stream?streams={'/'.join(streams)}"

    def _resolve_asset_from_symbol(self, symbol: str) -> str | None:
        """Map Binance symbol (e.g. 'BTCUSDT') back to asset name ('BTC')."""
        sym = symbol.upper()
        for asset, pair in SUPPORTED_ASSETS.items():
            if pair.upper() == sym:
                return asset
        return None

    async def _trade_stream(self):
        """Connect to Binance trade stream for real-time prices."""
        try:
            import websockets
        except ImportError:
            logger.error("websockets package not installed — run: pip install websockets")
            return

        url = self._build_combined_stream_url("trade")
        is_combined = len(self._assets) > 1

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    logger.info("Connected to Binance trade stream (%s)", ", ".join(self._assets))
                    async for msg in ws:
                        if not self._running:
                            break
                        try:
                            raw = json.loads(msg)
                            # Combined stream wraps data in {"stream": "...", "data": {...}}
                            data = raw.get("data", raw) if is_combined else raw

                            price = float(data.get("p", 0))
                            trade_time = data.get("T", 0) / 1000.0
                            symbol = data.get("s", "")

                            asset = self._resolve_asset_from_symbol(symbol) if symbol else self._assets[0]
                            if asset and price > 0:
                                self._process_tick(asset, price, trade_time)

                        except (json.JSONDecodeError, ValueError, KeyError):
                            continue

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.warning("Binance trade stream error: %s — reconnecting in 5s", e)
                    await asyncio.sleep(5)

    async def _kline_stream(self):
        """Connect to Binance 1-minute kline stream for candle data."""
        try:
            import websockets
        except ImportError:
            return

        url = self._build_combined_stream_url("kline_1m")
        is_combined = len(self._assets) > 1

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    logger.info("Connected to Binance kline stream (%s)", ", ".join(self._assets))
                    async for msg in ws:
                        if not self._running:
                            break
                        try:
                            raw = json.loads(msg)
                            data = raw.get("data", raw) if is_combined else raw

                            k = data.get("k", {})
                            if not k:
                                continue

                            # Resolve asset from kline symbol
                            symbol = k.get("s", data.get("s", ""))
                            asset = self._resolve_asset_from_symbol(symbol) if symbol else self._assets[0]
                            state = self._state.get(asset) if asset else None
                            if not state:
                                continue

                            candle = Candle(
                                open_time=k.get("t", 0) / 1000.0,
                                open=float(k.get("o", 0)),
                                high=float(k.get("h", 0)),
                                low=float(k.get("l", 0)),
                                close=float(k.get("c", 0)),
                                volume=float(k.get("v", 0)),
                                closed=k.get("x", False),
                            )

                            if candle.closed:
                                state.candles.append(candle)
                            else:
                                state.current_candle = candle

                        except (json.JSONDecodeError, ValueError, KeyError):
                            continue

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.warning("Binance kline stream error: %s — reconnecting in 5s", e)
                    await asyncio.sleep(5)
