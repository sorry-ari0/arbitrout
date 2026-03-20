"""Shared Binance WebSocket price feed for real-time BTC data.

Provides:
- Real-time BTC/USDT spot price via WebSocket trades stream
- 1-minute candle history (last 10 candles)
- 5-minute window open price tracking
- Micro momentum and tick trend signals for sniper

Single connection shared between BTC sniper and market maker.
"""
import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger("positions.price_feed")

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BINANCE_KLINE_URL = "wss://stream.binance.com:9443/ws/btcusdt@kline_1m"


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
    """Composite signal for BTC 5-min sniper."""
    direction: str          # "UP" or "DOWN"
    confidence: float       # 0.0 to 1.0
    window_delta_pct: float # % change from window open
    score: float            # Raw weighted score
    components: dict = field(default_factory=dict)


class BinancePriceFeed:
    """Real-time BTC price feed from Binance WebSocket.

    Maintains:
    - Latest spot price
    - 1-minute candle history (last 10)
    - 5-minute window state
    - 2-second tick samples for micro-trend detection
    """

    def __init__(self):
        self._price: float = 0.0
        self._price_time: float = 0.0
        self._running = False
        self._task: asyncio.Task | None = None
        self._kline_task: asyncio.Task | None = None

        # 1-minute candle history
        self._candles: deque[Candle] = deque(maxlen=10)
        self._current_candle: Candle | None = None

        # 5-minute window tracking
        self._window: WindowState | None = None

        # Tick samples for micro-trend (2-second intervals)
        self._tick_samples: deque[tuple[float, float]] = deque(maxlen=30)  # (timestamp, price)
        self._last_sample_time: float = 0.0

        # Subscribers for price updates
        self._subscribers: list[asyncio.Queue] = []

    @property
    def price(self) -> float:
        return self._price

    @property
    def price_age(self) -> float:
        """Seconds since last price update."""
        return time.time() - self._price_time if self._price_time else float('inf')

    @property
    def is_stale(self) -> bool:
        """Price is stale if >5 seconds old."""
        return self.price_age > 5.0

    @property
    def window(self) -> WindowState | None:
        return self._window

    @property
    def candles(self) -> list[Candle]:
        return list(self._candles)

    def subscribe(self) -> asyncio.Queue:
        """Subscribe to price updates. Returns a queue that receives (price, timestamp) tuples."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subscribers:
            self._subscribers.remove(q)

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._trade_stream())
        self._kline_task = asyncio.ensure_future(self._kline_stream())
        logger.info("Binance price feed started")

    def stop(self):
        self._running = False
        for task in [self._task, self._kline_task]:
            if task and not task.done():
                task.cancel()
        logger.info("Binance price feed stopped")

    # ============================================================
    # SIGNAL COMPUTATION
    # ============================================================

    def get_current_window(self) -> WindowState | None:
        """Get the current 5-minute window state, creating if needed."""
        now = time.time()
        window_ts = int(now) - (int(now) % 300)

        if self._window is None or self._window.window_ts != window_ts:
            # New window — use current price as open
            if self._price > 0:
                self._window = WindowState(
                    window_ts=window_ts,
                    open_price=self._price,
                    current_price=self._price,
                    high=self._price,
                    low=self._price,
                )
            else:
                return None

        return self._window

    def compute_sniper_signal(self) -> SniperSignal | None:
        """Compute composite signal for the current 5-minute window.

        Returns None if insufficient data.
        """
        window = self.get_current_window()
        if not window or window.open_price <= 0 or self._price <= 0:
            return None

        components = {}
        total_score = 0.0

        # 1. Window delta (weight 5-7) — the dominant signal
        delta_pct = (self._price - window.open_price) / window.open_price * 100
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
        closed_candles = [c for c in self._candles if c.closed]
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
        if len(self._tick_samples) >= 3:
            recent = list(self._tick_samples)[-5:]  # Last 5 samples (~10 seconds)
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

    def current_window_slug(self) -> str:
        """Polymarket market slug for the current 5-minute window."""
        now = time.time()
        window_ts = int(now) - (int(now) % 300)
        return f"btc-updown-5m-{window_ts}"

    # ============================================================
    # WEBSOCKET STREAMS
    # ============================================================

    async def _trade_stream(self):
        """Connect to Binance trade stream for real-time BTC price."""
        try:
            import websockets
        except ImportError:
            logger.error("websockets package not installed — run: pip install websockets")
            return

        while self._running:
            try:
                async with websockets.connect(BINANCE_WS_URL, ping_interval=20) as ws:
                    logger.info("Connected to Binance trade stream")
                    async for msg in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(msg)
                            price = float(data.get("p", 0))
                            trade_time = data.get("T", 0) / 1000.0  # ms to seconds

                            if price > 0:
                                self._price = price
                                self._price_time = time.time()

                                # Update window state
                                window = self.get_current_window()
                                if window:
                                    window.current_price = price
                                    window.high = max(window.high, price)
                                    window.low = min(window.low, price)
                                    window.tick_count += 1

                                # Sample for tick trend (every 2 seconds)
                                now = time.time()
                                if now - self._last_sample_time >= 2.0:
                                    self._tick_samples.append((now, price))
                                    self._last_sample_time = now

                                # Notify subscribers (non-blocking)
                                for q in self._subscribers:
                                    try:
                                        q.put_nowait((price, trade_time))
                                    except asyncio.QueueFull:
                                        pass  # Subscriber is slow, skip

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
            return  # Already logged in trade_stream

        while self._running:
            try:
                async with websockets.connect(BINANCE_KLINE_URL, ping_interval=20) as ws:
                    logger.info("Connected to Binance kline stream")
                    async for msg in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(msg)
                            k = data.get("k", {})
                            if not k:
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
                                self._candles.append(candle)
                            else:
                                self._current_candle = candle

                        except (json.JSONDecodeError, ValueError, KeyError):
                            continue

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.warning("Binance kline stream error: %s — reconnecting in 5s", e)
                    await asyncio.sleep(5)
