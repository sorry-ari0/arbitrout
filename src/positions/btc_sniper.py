"""Crypto 5-minute directional sniper — trades Polymarket's short-duration markets.

Strategy:
- Stream real-time crypto prices from Binance WebSocket (BTC, ETH, SOL, XRP)
- Event-driven: evaluates signal on every tick within entry window (not polling)
- At T-10 seconds before 5-min window close, compute directional signal
- If confidence > threshold, place maker limit order on the winning side
- Maker orders have 0% fee + earn daily USDC rebates from Polymarket
- FOK market order fallback if maker doesn't fill by T-5s

Research basis:
- At T-10s, ~85% of crypto direction is determined from spot price
- Polymarket odds lag real price by seconds
- Documented bot: 8,894 trades, ~$150K profit, 98% win rate
"""
import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

from .price_feed import BinancePriceFeed, SniperSignal

logger = logging.getLogger("positions.btc_sniper")

# Sniper parameters
MIN_CONFIDENCE = 0.30           # Minimum confidence to place a trade
ENTRY_WINDOW_SECONDS = 10       # Start evaluating at T-10s
FALLBACK_WINDOW_SECONDS = 5     # FOK fallback at T-5s
MAKER_PRICE_HIGH = 0.95         # Maker limit price for strong signals
MAKER_PRICE_LOW = 0.90          # Maker limit price for weaker signals
POLL_INTERVAL = 2.0             # Signal evaluation every 2 seconds
SPIKE_THRESHOLD = 1.5           # Score jump threshold for immediate entry

# Position sizing
DEFAULT_BANKROLL = 500.0
MIN_BET = 1.0
SAFE_BET_FRACTION = 0.25        # 25% of bankroll per trade in safe mode


@dataclass
class SniperStats:
    """Running statistics for the sniper."""
    trades_placed: int = 0
    trades_won: int = 0
    trades_lost: int = 0
    total_pnl: float = 0.0
    total_fees: float = 0.0
    windows_skipped: int = 0
    windows_no_signal: int = 0


@dataclass
class AssetSniperState:
    """Per-asset state for multi-asset sniper."""
    asset: str
    last_window_ts: int = 0
    last_signal_score: float = 0.0
    best_signal: SniperSignal | None = None
    order_placed: bool = False


class BtcSniper:
    """Autonomous crypto 5-minute directional sniper.

    Runs as an independent async loop. Uses BinancePriceFeed for real-time
    price data and PolymarketExecutor for order placement.

    Supports multiple assets (BTC, ETH, SOL, XRP) simultaneously.
    Event-driven: evaluates signal on every Binance tick within the entry
    window instead of fixed 2-second polling.
    """

    def __init__(self, price_feed: BinancePriceFeed, position_manager=None,
                 bankroll: float = DEFAULT_BANKROLL, mode: str = "safe",
                 assets: list[str] | None = None):
        self.feed = price_feed
        self.pm = position_manager
        self.bankroll = bankroll
        self.initial_bankroll = bankroll
        self.mode = mode  # "safe", "aggressive", "paper"
        self.assets = [a.upper() for a in (assets or ["BTC"])]
        self.stats = SniperStats()
        self._running = False
        self._task: asyncio.Task | None = None
        # Per-asset state
        self._asset_state: dict[str, AssetSniperState] = {
            a: AssetSniperState(asset=a) for a in self.assets
        }
        # Event-driven: tick event signals new data available
        self._tick_event = asyncio.Event()
        self._tick_callback_registered = False
        # Decision log for analysis
        self._decision_log: list[dict] = []

    def start(self):
        if self._running:
            return
        self._running = True
        # Register tick callback for event-driven evaluation
        if not self._tick_callback_registered:
            self.feed.on_tick(self._on_price_tick)
            self._tick_callback_registered = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info("Sniper started (assets=%s, bankroll=$%.2f, mode=%s)",
                     ",".join(self.assets), self.bankroll, self.mode)

    def stop(self):
        self._running = False
        if self._tick_callback_registered:
            self.feed.remove_on_tick(self._on_price_tick)
            self._tick_callback_registered = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Sniper stopped — stats: %d trades, %d wins, $%.2f P&L",
                     self.stats.trades_placed, self.stats.trades_won, self.stats.total_pnl)

    def _on_price_tick(self, asset: str, price: float, timestamp: float):
        """Callback from price feed — fires on every trade tick.

        Sets an asyncio event to wake the sniper loop immediately
        instead of waiting for the 2-second poll interval.
        """
        if asset in self.assets:
            self._tick_event.set()

    def get_stats(self) -> dict:
        return {
            "bankroll": round(self.bankroll, 2),
            "initial_bankroll": self.initial_bankroll,
            "mode": self.mode,
            "assets": self.assets,
            "trades_placed": self.stats.trades_placed,
            "trades_won": self.stats.trades_won,
            "trades_lost": self.stats.trades_lost,
            "win_rate": round(self.stats.trades_won / max(1, self.stats.trades_placed) * 100, 1),
            "total_pnl": round(self.stats.total_pnl, 2),
            "total_fees": round(self.stats.total_fees, 2),
            "windows_skipped": self.stats.windows_skipped,
            "windows_no_signal": self.stats.windows_no_signal,
            "recent_decisions": self._decision_log[-10:],
        }

    async def _loop(self):
        """Main sniper loop — one iteration per 5-minute window, per asset."""
        # Wait for price feed to have data for at least one asset
        for _ in range(30):
            if any(self.feed.get_price(a) > 0 for a in self.assets):
                break
            await asyncio.sleep(1)

        active = [a for a in self.assets if self.feed.get_price(a) > 0]
        if not active:
            logger.error("Sniper: price feed has no data after 30s — aborting")
            return

        for a in active:
            logger.info("Sniper: %s feed active, $%.2f", a, self.feed.get_price(a))

        while self._running:
            try:
                await self._wait_for_entry_window()
                if not self._running:
                    break
                # Execute window for each asset that has data
                for asset in self.assets:
                    if self.feed.get_price(asset) > 0:
                        await self._execute_window(asset)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Sniper error: %s", e)
                await asyncio.sleep(5)

    async def _wait_for_entry_window(self):
        """Sleep until T-10 seconds before the next 5-minute window close."""
        while self._running:
            remaining = self.feed.seconds_until_window_close()

            if remaining <= ENTRY_WINDOW_SECONDS:
                now = time.time()
                window_ts = int(now) - (int(now) % 300)

                # Don't trade the same window twice (check all assets)
                all_traded = all(
                    self._asset_state[a].last_window_ts == window_ts
                    for a in self.assets
                )
                if all_traded:
                    await asyncio.sleep(remaining + 1)
                    continue

                return

            sleep_time = remaining - ENTRY_WINDOW_SECONDS - 1
            if sleep_time > 0:
                await asyncio.sleep(min(sleep_time, 30))
            else:
                await asyncio.sleep(0.5)

    async def _execute_window(self, asset: str):
        """Execute trading logic for the current 5-minute window on one asset.

        Event-driven: wakes on every Binance tick instead of fixed 2s polling.
        Falls back to 2s polling if no ticks arrive.
        """
        now = time.time()
        window_ts = int(now) - (int(now) % 300)
        close_time = window_ts + 300

        astate = self._asset_state[asset]

        # Skip if already traded this window for this asset
        if astate.last_window_ts == window_ts:
            return
        astate.last_window_ts = window_ts
        astate.best_signal = None
        astate.order_placed = False

        # Bankroll check
        bet_size = self._calculate_bet_size()
        if bet_size < MIN_BET:
            self.stats.windows_skipped += 1
            self._log_decision(window_ts, "skip", "insufficient_bankroll",
                               {"asset": asset, "bankroll": self.bankroll, "min_bet": MIN_BET})
            return

        # Price feed health check
        if self.feed.is_asset_stale(asset):
            self.stats.windows_skipped += 1
            self._log_decision(window_ts, "skip", "stale_price_feed",
                               {"asset": asset, "price_age": self.feed.get_asset(asset).price_age})
            return

        # Event-driven evaluation loop
        while self._running:
            remaining = close_time - time.time()
            if remaining <= 0:
                break

            signal = self.feed.compute_sniper_signal(asset)
            if not signal:
                # Wait for next tick or timeout after 2s
                self._tick_event.clear()
                try:
                    await asyncio.wait_for(self._tick_event.wait(), timeout=POLL_INTERVAL)
                except asyncio.TimeoutError:
                    pass
                continue

            # Spike detection
            if (astate.best_signal and
                abs(signal.score) - abs(astate.last_signal_score) >= SPIKE_THRESHOLD and
                signal.confidence >= MIN_CONFIDENCE):
                logger.info("Sniper [%s]: spike detected (score %.2f -> %.2f), entering immediately",
                            asset, astate.last_signal_score, signal.score)
                astate.best_signal = signal
                break

            astate.last_signal_score = signal.score

            if not astate.best_signal or signal.confidence > astate.best_signal.confidence:
                astate.best_signal = signal

            # At T-5s with good signal, commit
            if remaining <= FALLBACK_WINDOW_SECONDS and astate.best_signal.confidence >= MIN_CONFIDENCE:
                break

            # Wait for next tick (event-driven) with 2s fallback
            self._tick_event.clear()
            try:
                await asyncio.wait_for(self._tick_event.wait(), timeout=POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass

        best_signal = astate.best_signal
        if not best_signal or best_signal.confidence < MIN_CONFIDENCE:
            self.stats.windows_no_signal += 1
            self._log_decision(window_ts, "skip", "no_signal",
                               {"asset": asset,
                                "confidence": best_signal.confidence if best_signal else 0,
                                "min_required": MIN_CONFIDENCE})
            return

        direction = best_signal.direction
        confidence = best_signal.confidence

        if confidence >= 0.7:
            maker_price = MAKER_PRICE_HIGH
        elif confidence >= 0.5:
            maker_price = 0.92
        else:
            maker_price = MAKER_PRICE_LOW

        logger.info("Sniper [%s]: window %d, direction=%s, confidence=%.1f%%, delta=%.4f%%, $%.2f @ $%.2f",
                     asset, window_ts, direction, confidence * 100, best_signal.window_delta_pct,
                     bet_size, maker_price)

        result = await self._place_sniper_order(window_ts, direction, bet_size, maker_price, asset)

        if result.get("success"):
            self.stats.trades_placed += 1
            self._log_decision(window_ts, "trade", "placed", {
                "asset": asset,
                "direction": direction,
                "confidence": confidence,
                "delta_pct": best_signal.window_delta_pct,
                "bet_size": bet_size,
                "maker_price": maker_price,
                "score": best_signal.score,
                "components": best_signal.components,
            })
            asyncio.ensure_future(self._track_resolution(
                window_ts, direction, bet_size, maker_price, asset))
        else:
            self._log_decision(window_ts, "error", "order_failed",
                               {"asset": asset, "error": result.get("error", "unknown"),
                                "direction": direction, "confidence": confidence})

    async def _place_sniper_order(self, window_ts: int, direction: str,
                                  bet_size: float, price: float,
                                  asset: str = "BTC") -> dict:
        """Place a sniper order on Polymarket.

        Uses the position manager if available, otherwise returns paper result.
        """
        if self.mode == "paper" or not self.pm:
            self.bankroll -= bet_size
            return {"success": True, "mode": "paper", "bet_size": bet_size}

        try:
            from .position_manager import create_package, create_leg, create_exit_rule

            slug = self.feed.current_window_slug(asset)

            # Resolve market conditionId from slug
            condition_id = await self._resolve_market_id(slug)
            if not condition_id:
                return {"success": False, "error": f"Cannot resolve market for {slug}"}

            asset_id = f"{condition_id}:YES"

            pkg = create_package(f"Sniper: {asset} 5m {direction} @{window_ts}", "btc_sniper")
            pkg["legs"].append(create_leg(
                platform="polymarket",
                leg_type="prediction_yes",
                asset_id=asset_id,
                asset_label=f"{asset} 5m {direction} (window {window_ts})",
                entry_price=price,
                cost=bet_size,
                expiry="",
            ))

            result = await self.pm.execute_package(pkg)
            return result

        except Exception as e:
            logger.error("Sniper order failed: %s", e)
            return {"success": False, "error": str(e)}

    async def _resolve_market_id(self, slug: str) -> str | None:
        """Resolve a Polymarket 5-min BTC market slug to conditionId.

        Uses Gamma API to find the market by slug.
        """
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"slug": slug, "limit": 1}
                )
                if resp.status_code == 200:
                    markets = resp.json()
                    if isinstance(markets, list) and markets:
                        return markets[0].get("conditionId", markets[0].get("condition_id", ""))
                    elif isinstance(markets, dict):
                        data = markets.get("data", markets.get("markets", []))
                        if data:
                            return data[0].get("conditionId", data[0].get("condition_id", ""))
        except Exception as e:
            logger.warning("Cannot resolve market %s: %s", slug, e)
        return None

    async def _track_resolution(self, window_ts: int, direction: str,
                                bet_size: float, entry_price: float,
                                asset: str = "BTC"):
        """Wait for window resolution and update stats.

        Resolution: compare price at window open vs close.
        If close >= open → UP wins, else DOWN wins.
        """
        close_time = window_ts + 300

        wait_time = close_time - time.time() + 30
        if wait_time > 0:
            await asyncio.sleep(wait_time)

        asset_state = self.feed.get_asset(asset)
        if asset_state and asset_state.window and asset_state.window.window_ts == window_ts:
            open_price = asset_state.window.open_price
            close_price = asset_state.price

            actual_direction = "UP" if close_price >= open_price else "DOWN"
            won = (direction == actual_direction)

            if won:
                shares = bet_size / entry_price
                payout = shares * 1.0
                profit = payout - bet_size
                self.stats.trades_won += 1
                self.stats.total_pnl += profit
                self.bankroll += bet_size + profit
                logger.info("Sniper [%s] WIN: %s ($%.2f->$%.2f), profit=$%.2f",
                            asset, direction, open_price, close_price, profit)
            else:
                self.stats.trades_lost += 1
                self.stats.total_pnl -= bet_size
                logger.info("Sniper [%s] LOSS: predicted %s but was %s, loss=$%.2f",
                            asset, direction, actual_direction, bet_size)
        else:
            logger.warning("Sniper [%s]: cannot verify resolution for window %d", asset, window_ts)

    def _calculate_bet_size(self) -> float:
        """Calculate bet size based on mode and bankroll."""
        if self.mode == "safe":
            return max(MIN_BET, min(self.bankroll * SAFE_BET_FRACTION, self.bankroll))
        elif self.mode == "aggressive":
            # Bet only gains above initial investment
            gains = self.bankroll - self.initial_bankroll
            if gains <= MIN_BET:
                return max(MIN_BET, min(self.bankroll * 0.10, self.bankroll))  # 10% minimum
            return max(MIN_BET, gains)
        else:
            # Paper mode — fixed $10
            return min(10.0, self.bankroll)

    def _log_decision(self, window_ts: int, action: str, reason: str, details: dict | None = None):
        """Log a decision for analysis."""
        entry = {
            "window_ts": window_ts,
            "time": time.time(),
            "action": action,
            "reason": reason,
            **(details or {}),
        }
        self._decision_log.append(entry)
        # Keep last 500 decisions
        if len(self._decision_log) > 500:
            self._decision_log = self._decision_log[-500:]
