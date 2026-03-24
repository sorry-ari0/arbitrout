"""Kalshi anonymous whale tracker — detects large-trade, volume-spike, and
orderbook-tilt signals on Kalshi markets.

Kalshi has NO trader identity (no wallet addresses, no leaderboard API), so
all signals are behavioural / anonymous:
  1. Large individual trades (>$1K)
  2. Volume spikes relative to 7-day baseline
  3. Orderbook bid/ask imbalance near midpoint

Data sources:
  - GET /markets/trades  (public, paginated, no size filter on Kalshi)
  - GET /markets/{ticker}/orderbook  (public)
  - Volume from cached adapter data

Persistence: kalshi_whale_signals.json (rolling window + baselines)
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None

logger = logging.getLogger("positions.kalshi_whale_tracker")

# Kalshi public API
PUBLIC_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Thresholds
LARGE_TRADE_MIN_USD = 1000       # $1K+ counts as large
WHALE_TRADE_USD = 5000           # $5K+ is a whale-sized trade
MEGA_TRADE_USD = 10000           # $10K+ is mega whale
ROLLING_WINDOW = 3600            # 1-hour rolling window for trade tracking
VOLUME_SPIKE_THRESHOLD = 2.0     # 2x 7-day avg = spike
ORDERBOOK_TILT_THRESHOLD = 0.2   # ±0.2 tilt to count as directional
POLL_INTERVAL = 300              # 5 minutes between polls
CACHE_TTL = 600                  # Cache signals for 10 minutes
BASELINE_HISTORY_DAYS = 7        # 7-day rolling baseline for volume

# Signal strength weights
W_TRADE = 0.40
W_VOLUME = 0.35
W_ORDERBOOK = 0.25


class KalshiWhaleTracker:
    """Tracks anonymous whale activity on Kalshi markets."""

    def __init__(self, data_dir: Path, kalshi_adapter=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._persist_path = self.data_dir / "kalshi_whale_signals.json"
        self._kalshi_adapter = kalshi_adapter  # For getting watched tickers

        # Rolling trade data: {ticker: [{"size": float, "side": str, "ts": float}, ...]}
        self._large_trades: dict[str, list[dict]] = {}

        # Volume baselines: {ticker: {"samples": [(ts, vol_24h), ...], "avg_7d": float}}
        self._volume_baselines: dict[str, dict] = {}

        # Orderbook snapshots: {ticker: {"yes_depth": float, "no_depth": float, "tilt": float, "ts": float}}
        self._orderbook_snapshots: dict[str, dict] = {}

        # Cached signals: {ticker: signal_dict}
        self._signal_cache: dict[str, dict] = {}
        self._signal_cache_ts: dict[str, float] = {}

        self._task = None
        self._running = False
        self._scan_count = 0

        # Load persisted state
        self._load_state()

    # ================================================================
    # LIFECYCLE
    # ================================================================

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info("Kalshi whale tracker started (poll every %ds)", POLL_INTERVAL)

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        self._save_state()
        logger.info("Kalshi whale tracker stopped")

    async def _loop(self):
        await asyncio.sleep(10)  # Let server fully start
        while self._running:
            try:
                await self._poll()
                self._scan_count += 1
                if self._scan_count % 6 == 0:  # Save every ~30 min
                    self._save_state()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Kalshi whale poll error: %s", exc)
            await asyncio.sleep(POLL_INTERVAL)

    # ================================================================
    # DATA COLLECTION
    # ================================================================

    def _get_watched_tickers(self) -> list[str]:
        """Get tickers we're actively watching from the adapter cache."""
        tickers = []
        if self._kalshi_adapter and hasattr(self._kalshi_adapter, '_cache'):
            cache = self._kalshi_adapter._cache
            if cache:
                for event in cache:
                    eid = getattr(event, 'event_id', '') or ''
                    if eid:
                        tickers.append(eid)
        return tickers[:50]  # Cap at 50 most recent

    async def _poll(self):
        """Run one polling cycle: fetch trades + orderbooks for watched markets."""
        if httpx is None:
            return

        tickers = self._get_watched_tickers()
        if not tickers:
            return

        now = time.time()
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Fetch trades for watched tickers (batched)
            await self._poll_large_trades(client, tickers, now)

            # Fetch orderbooks for tickers with recent activity
            active_tickers = self._get_active_tickers(tickers)
            if active_tickers:
                await self._poll_orderbooks(client, active_tickers, now)

        # Prune old data
        self._prune_old_trades(now)

        # Invalidate stale signal cache
        for ticker in list(self._signal_cache_ts):
            if now - self._signal_cache_ts[ticker] > CACHE_TTL:
                self._signal_cache.pop(ticker, None)
                self._signal_cache_ts.pop(ticker, None)

        active_signals = sum(1 for s in self._signal_cache.values() if s.get("has_signal"))
        logger.info(
            "Kalshi whale scan #%d: %d tickers, %d with large trades, %d active signals",
            self._scan_count + 1,
            len(tickers),
            len(self._large_trades),
            active_signals,
        )

    async def _poll_large_trades(self, client: httpx.AsyncClient, tickers: list[str], now: float):
        """Fetch recent trades for watched tickers and filter for large ones."""
        sem = asyncio.Semaphore(5)

        async def fetch_ticker_trades(ticker: str):
            async with sem:
                try:
                    # Kalshi public trades endpoint — paginate to get recent trades
                    params = {"ticker": ticker, "limit": 100}
                    resp = await client.get(f"{PUBLIC_URL}/markets/trades", params=params)
                    if resp.status_code != 200:
                        return
                    data = resp.json()
                    trades = data.get("trades", [])

                    for t in trades:
                        # count_fp is the trade size in dollars (cents on auth API)
                        size = float(t.get("count", 0))
                        # Public API: count is number of contracts, yes_price is in cents
                        # Total value ≈ count * price / 100 (but count alone is decent proxy)
                        # Auth API has count_fp in dollars
                        cost = size  # contracts — we'll use raw count as size proxy
                        if "count_fp" in t:
                            cost = float(t["count_fp"])
                        elif "yes_price" in t:
                            price_cents = float(t.get("yes_price", 50))
                            cost = size * price_cents / 100.0

                        if cost < LARGE_TRADE_MIN_USD:
                            continue

                        # Parse timestamp
                        ts_str = t.get("created_time", "")
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                        except (ValueError, AttributeError):
                            ts = now

                        # Only keep trades within rolling window
                        if now - ts > ROLLING_WINDOW:
                            continue

                        side = t.get("taker_side", "").upper()
                        if side not in ("YES", "NO"):
                            side = "YES" if t.get("yes_price", 50) > 50 else "NO"

                        if ticker not in self._large_trades:
                            self._large_trades[ticker] = []

                        # Deduplicate by trade ID if available
                        trade_id = t.get("id", f"{ts}_{cost}_{side}")
                        existing_ids = {tr.get("id") for tr in self._large_trades[ticker]}
                        if trade_id not in existing_ids:
                            self._large_trades[ticker].append({
                                "id": trade_id,
                                "size": cost,
                                "side": side,
                                "ts": ts,
                            })
                except Exception as exc:
                    logger.debug("Trade fetch failed for %s: %s", ticker, exc)

        tasks = [fetch_ticker_trades(t) for t in tickers]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_orderbooks(self, client: httpx.AsyncClient, tickers: list[str], now: float):
        """Fetch orderbook snapshots for active tickers."""
        sem = asyncio.Semaphore(5)

        async def fetch_orderbook(ticker: str):
            async with sem:
                try:
                    resp = await client.get(f"{PUBLIC_URL}/markets/{ticker}/orderbook")
                    if resp.status_code != 200:
                        return
                    data = resp.json()
                    ob = data.get("orderbook_fp", data.get("orderbook", {}))

                    yes_bids = ob.get("yes_dollars", ob.get("yes", []))
                    no_bids = ob.get("no_dollars", ob.get("no", []))

                    # Sum depth within 10% of midpoint
                    yes_depth = self._sum_depth_near_mid(yes_bids)
                    no_depth = self._sum_depth_near_mid(no_bids)

                    total = yes_depth + no_depth
                    if total > 0:
                        tilt = (yes_depth - no_depth) / total  # -1 (heavy NO) to +1 (heavy YES)
                    else:
                        tilt = 0.0

                    self._orderbook_snapshots[ticker] = {
                        "yes_depth": yes_depth,
                        "no_depth": no_depth,
                        "tilt": round(tilt, 4),
                        "ts": now,
                    }
                except Exception as exc:
                    logger.debug("Orderbook fetch failed for %s: %s", ticker, exc)

        tasks = [fetch_orderbook(t) for t in tickers]
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _sum_depth_near_mid(levels: list) -> float:
        """Sum order depth within 10% of the midpoint price (40-60 cents)."""
        total = 0.0
        for level in levels:
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                price = float(level[0])
                qty = float(level[1])
                # Price is in dollars (0-1 range) for _fp endpoints
                if 0.40 <= price <= 0.60:
                    total += qty
            elif isinstance(level, dict):
                price = float(level.get("price", 0))
                qty = float(level.get("quantity", 0))
                if 0.40 <= price <= 0.60:
                    total += qty
        return total

    def _get_active_tickers(self, all_tickers: list[str]) -> list[str]:
        """Return tickers that have recent large trade activity or are new."""
        active = []
        for ticker in all_tickers:
            if ticker in self._large_trades and self._large_trades[ticker]:
                active.append(ticker)
            elif self._scan_count % 3 == 0:
                # Every 3rd scan, check orderbooks for all tickers to detect silent accumulation
                active.append(ticker)
        return active[:30]

    def _prune_old_trades(self, now: float):
        """Remove trades older than the rolling window."""
        cutoff = now - ROLLING_WINDOW
        for ticker in list(self._large_trades):
            self._large_trades[ticker] = [
                t for t in self._large_trades[ticker] if t["ts"] > cutoff
            ]
            if not self._large_trades[ticker]:
                del self._large_trades[ticker]

    # ================================================================
    # VOLUME BASELINE TRACKING
    # ================================================================

    def update_volume_baseline(self, ticker: str, volume_24h: float):
        """Update 7-day volume baseline for a ticker. Called from adapter cache."""
        now = time.time()
        if ticker not in self._volume_baselines:
            self._volume_baselines[ticker] = {"samples": [], "avg_7d": 0}

        baseline = self._volume_baselines[ticker]
        baseline["samples"].append((now, volume_24h))

        # Keep only 7 days of samples
        cutoff = now - BASELINE_HISTORY_DAYS * 86400
        baseline["samples"] = [(ts, v) for ts, v in baseline["samples"] if ts > cutoff]

        # Recalculate average
        if baseline["samples"]:
            baseline["avg_7d"] = sum(v for _, v in baseline["samples"]) / len(baseline["samples"])

    # ================================================================
    # SIGNAL GENERATION
    # ================================================================

    def get_whale_signal(self, ticker: str, volume_24h: float = 0) -> dict:
        """Get composite whale signal for a Kalshi market ticker.

        Args:
            ticker: Kalshi market ticker
            volume_24h: Current 24h volume (optional, for spike detection)

        Returns:
            Signal dict matching Polymarket insider signal shape for cross-platform use.
        """
        now = time.time()

        # Check cache
        if ticker in self._signal_cache and now - self._signal_cache_ts.get(ticker, 0) < CACHE_TTL:
            return self._signal_cache[ticker]

        # Update volume baseline if provided
        if volume_24h > 0:
            self.update_volume_baseline(ticker, volume_24h)

        # 1. Trade score
        trades = self._large_trades.get(ticker, [])
        trade_score, net_direction, trade_count, trade_volume = self._compute_trade_score(trades)

        # 2. Volume spike score
        volume_score, spike_ratio = self._compute_volume_score(ticker, volume_24h)

        # 3. Orderbook tilt score
        orderbook_score, tilt = self._compute_orderbook_score(ticker)

        # Composite strength
        strength = W_TRADE * trade_score + W_VOLUME * volume_score + W_ORDERBOOK * orderbook_score
        strength = round(min(1.0, strength), 3)

        has_signal = strength > 0.1 and (trade_count >= 2 or spike_ratio > VOLUME_SPIKE_THRESHOLD)

        signal = {
            "has_signal": has_signal,
            "signal_type": "anonymous_whale",
            "large_trade_count": trade_count,
            "large_trade_volume": round(trade_volume, 2),
            "net_direction": net_direction,
            "volume_spike_ratio": round(spike_ratio, 2),
            "orderbook_tilt": tilt,
            "signal_strength": strength,
            # Breakdown for debugging
            "_trade_score": round(trade_score, 3),
            "_volume_score": round(volume_score, 3),
            "_orderbook_score": round(orderbook_score, 3),
        }

        self._signal_cache[ticker] = signal
        self._signal_cache_ts[ticker] = now
        return signal

    def _compute_trade_score(self, trades: list[dict]) -> tuple[float, str, int, float]:
        """Compute trade-based signal score.

        Returns: (score, direction, count, total_volume)
        """
        if not trades:
            return 0.0, "NONE", 0, 0.0

        yes_count = sum(1 for t in trades if t["side"] == "YES")
        no_count = sum(1 for t in trades if t["side"] == "NO")
        total_count = len(trades)
        total_volume = sum(t["size"] for t in trades)

        # Direction consistency — unanimous is strongest
        if total_count > 0:
            majority = max(yes_count, no_count)
            consistency = majority / total_count
        else:
            consistency = 0

        if yes_count > no_count * 1.5:
            direction = "YES"
        elif no_count > yes_count * 1.5:
            direction = "NO"
        elif total_count > 0:
            direction = "MIXED"
        else:
            direction = "NONE"

        # Score: count saturation * direction consistency
        count_factor = min(total_count / 5, 1.0)
        score = count_factor * consistency

        # Bonus for whale-sized trades
        whale_count = sum(1 for t in trades if t["size"] >= WHALE_TRADE_USD)
        mega_count = sum(1 for t in trades if t["size"] >= MEGA_TRADE_USD)
        if mega_count > 0:
            score = min(1.0, score + 0.2)
        elif whale_count >= 2:
            score = min(1.0, score + 0.1)

        return score, direction, total_count, total_volume

    def _compute_volume_score(self, ticker: str, volume_24h: float) -> tuple[float, float]:
        """Compute volume spike score. Returns (score, spike_ratio)."""
        baseline = self._volume_baselines.get(ticker, {})
        avg_7d = baseline.get("avg_7d", 0)

        if avg_7d <= 0:
            if volume_24h > 0:
                # Seed baseline with current volume * 0.8 (conservative estimate)
                self._volume_baselines[ticker] = {
                    "samples": [(time.time(), volume_24h)],
                    "avg_7d": volume_24h * 0.8,
                }
                logger.info("Seeded volume baseline for %s: avg_7d=%.0f", ticker, volume_24h * 0.8)
            return 0.0, 0.0
        if volume_24h <= 0:
            return 0.0, 0.0

        spike_ratio = volume_24h / avg_7d

        if spike_ratio < 1.5:
            return 0.0, spike_ratio

        score = min(spike_ratio / 3.0, 1.0)
        return score, spike_ratio

    def _compute_orderbook_score(self, ticker: str) -> tuple[float, float]:
        """Compute orderbook imbalance score. Returns (score, tilt)."""
        snapshot = self._orderbook_snapshots.get(ticker, {})
        tilt = snapshot.get("tilt", 0.0)
        ts = snapshot.get("ts", 0)

        # Stale orderbooks (>10 min) don't count
        if time.time() - ts > CACHE_TTL:
            return 0.0, 0.0

        abs_tilt = abs(tilt)
        if abs_tilt < ORDERBOOK_TILT_THRESHOLD:
            return 0.0, tilt

        score = min(abs_tilt / 0.5, 1.0)
        return score, round(tilt, 4)

    # ================================================================
    # PERSISTENCE
    # ================================================================

    def _save_state(self):
        """Persist rolling data to disk."""
        try:
            state = {
                "volume_baselines": {},
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            # Only persist baselines (trades are ephemeral 1-hour window)
            for ticker, bl in self._volume_baselines.items():
                state["volume_baselines"][ticker] = {
                    "samples": bl["samples"][-48:],  # Keep max 48 samples (~2 per hour for 24h)
                    "avg_7d": bl["avg_7d"],
                }
            self._persist_path.write_text(json.dumps(state, indent=2))
        except Exception as exc:
            logger.warning("Failed to save Kalshi whale state: %s", exc)

    def _load_state(self):
        """Load persisted state from disk."""
        try:
            if self._persist_path.exists():
                state = json.loads(self._persist_path.read_text())
                for ticker, bl in state.get("volume_baselines", {}).items():
                    self._volume_baselines[ticker] = {
                        "samples": bl.get("samples", []),
                        "avg_7d": bl.get("avg_7d", 0),
                    }
                logger.info("Loaded Kalshi whale baselines for %d tickers", len(self._volume_baselines))
        except Exception as exc:
            logger.warning("Failed to load Kalshi whale state: %s", exc)

    # ================================================================
    # STATS
    # ================================================================

    def get_stats(self) -> dict:
        """Return tracker statistics for the API."""
        active_signals = {
            ticker: sig for ticker, sig in self._signal_cache.items()
            if sig.get("has_signal")
        }
        return {
            "scan_count": self._scan_count,
            "watched_tickers": len(self._get_watched_tickers()),
            "tickers_with_large_trades": len(self._large_trades),
            "volume_baselines_tracked": len(self._volume_baselines),
            "orderbook_snapshots": len(self._orderbook_snapshots),
            "active_signals": len(active_signals),
            "signals": active_signals,
        }
