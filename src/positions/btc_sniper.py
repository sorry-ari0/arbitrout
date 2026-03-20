"""BTC 5-minute directional sniper — trades Polymarket's short-duration BTC markets.

Strategy:
- Stream real-time BTC price from Binance WebSocket
- At T-10 seconds before 5-min window close, compute directional signal
- If confidence > threshold, place maker limit order on the winning side
- Maker orders have 0% fee + earn daily USDC rebates from Polymarket
- FOK market order fallback if maker doesn't fill by T-5s

Research basis:
- At T-10s, ~85% of BTC direction is determined from spot price
- Polymarket odds lag real price by seconds
- Documented bot: 8,894 trades, ~$150K profit, 98% win rate
"""
import asyncio
import logging
import os
import time
from dataclasses import dataclass

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


class BtcSniper:
    """Autonomous BTC 5-minute directional sniper.

    Runs as an independent async loop. Uses BinancePriceFeed for real-time
    price data and PolymarketExecutor for order placement.
    """

    def __init__(self, price_feed: BinancePriceFeed, position_manager=None,
                 bankroll: float = DEFAULT_BANKROLL, mode: str = "safe"):
        self.feed = price_feed
        self.pm = position_manager
        self.bankroll = bankroll
        self.initial_bankroll = bankroll
        self.mode = mode  # "safe", "aggressive", "paper"
        self.stats = SniperStats()
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_window_ts: int = 0
        self._last_signal_score: float = 0.0
        # Decision log for analysis
        self._decision_log: list[dict] = []

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info("BTC sniper started (bankroll=$%.2f, mode=%s)", self.bankroll, self.mode)

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("BTC sniper stopped — stats: %d trades, %d wins, $%.2f P&L",
                     self.stats.trades_placed, self.stats.trades_won, self.stats.total_pnl)

    def get_stats(self) -> dict:
        return {
            "bankroll": round(self.bankroll, 2),
            "initial_bankroll": self.initial_bankroll,
            "mode": self.mode,
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
        """Main sniper loop — one iteration per 5-minute window."""
        # Wait for price feed to have data
        for _ in range(30):
            if self.feed.price > 0:
                break
            await asyncio.sleep(1)

        if self.feed.price <= 0:
            logger.error("BTC sniper: price feed has no data after 30s — aborting")
            return

        logger.info("BTC sniper: price feed active, BTC=$%.2f", self.feed.price)

        while self._running:
            try:
                await self._wait_for_entry_window()
                if not self._running:
                    break
                await self._execute_window()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("BTC sniper error: %s", e)
                await asyncio.sleep(5)

    async def _wait_for_entry_window(self):
        """Sleep until T-10 seconds before the next 5-minute window close."""
        while self._running:
            remaining = self.feed.seconds_until_window_close()

            if remaining <= ENTRY_WINDOW_SECONDS:
                # We're in the entry window
                now = time.time()
                window_ts = int(now) - (int(now) % 300)

                # Don't trade the same window twice
                if window_ts == self._last_window_ts:
                    # Wait for next window
                    await asyncio.sleep(remaining + 1)
                    continue

                return

            # Sleep until entry window (with margin)
            sleep_time = remaining - ENTRY_WINDOW_SECONDS - 1
            if sleep_time > 0:
                await asyncio.sleep(min(sleep_time, 30))  # Check every 30s max
            else:
                await asyncio.sleep(0.5)

    async def _execute_window(self):
        """Execute trading logic for the current 5-minute window.

        1. Poll signal every 2 seconds from T-10s to T-0s
        2. If confidence > threshold, place maker order
        3. If maker doesn't fill by T-5s, try FOK market order
        4. Track result after resolution
        """
        now = time.time()
        window_ts = int(now) - (int(now) % 300)
        close_time = window_ts + 300
        self._last_window_ts = window_ts

        # Bankroll check
        bet_size = self._calculate_bet_size()
        if bet_size < MIN_BET:
            self.stats.windows_skipped += 1
            self._log_decision(window_ts, "skip", "insufficient_bankroll",
                               {"bankroll": self.bankroll, "min_bet": MIN_BET})
            return

        # Price feed health check
        if self.feed.is_stale:
            self.stats.windows_skipped += 1
            self._log_decision(window_ts, "skip", "stale_price_feed",
                               {"price_age": self.feed.price_age})
            return

        best_signal: SniperSignal | None = None
        order_placed = False

        # Polling loop: evaluate signal every 2 seconds
        while self._running:
            remaining = close_time - time.time()
            if remaining <= 0:
                break

            signal = self.feed.compute_sniper_signal()
            if not signal:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # Spike detection: if score jumps significantly, enter immediately
            if (best_signal and
                abs(signal.score) - abs(self._last_signal_score) >= SPIKE_THRESHOLD and
                signal.confidence >= MIN_CONFIDENCE):
                logger.info("BTC sniper: spike detected (score %.2f -> %.2f), entering immediately",
                            self._last_signal_score, signal.score)
                best_signal = signal
                break

            self._last_signal_score = signal.score

            # Update best signal
            if not best_signal or signal.confidence > best_signal.confidence:
                best_signal = signal

            # At T-5s with good signal, commit
            if remaining <= FALLBACK_WINDOW_SECONDS and best_signal.confidence >= MIN_CONFIDENCE:
                break

            await asyncio.sleep(POLL_INTERVAL)

        if not best_signal or best_signal.confidence < MIN_CONFIDENCE:
            self.stats.windows_no_signal += 1
            self._log_decision(window_ts, "skip", "no_signal",
                               {"confidence": best_signal.confidence if best_signal else 0,
                                "min_required": MIN_CONFIDENCE})
            return

        # Place the trade
        direction = best_signal.direction  # "UP" or "DOWN"
        confidence = best_signal.confidence

        # Determine maker price based on confidence
        if confidence >= 0.7:
            maker_price = MAKER_PRICE_HIGH  # $0.95 — strong conviction
        elif confidence >= 0.5:
            maker_price = 0.92
        else:
            maker_price = MAKER_PRICE_LOW   # $0.90 — minimum viable

        logger.info("BTC sniper: window %d, direction=%s, confidence=%.1f%%, delta=%.4f%%, placing $%.2f @ $%.2f",
                     window_ts, direction, confidence * 100, best_signal.window_delta_pct, bet_size, maker_price)

        # Execute via position manager or directly via CLOB
        result = await self._place_sniper_order(window_ts, direction, bet_size, maker_price)

        if result.get("success"):
            self.stats.trades_placed += 1
            self._log_decision(window_ts, "trade", "placed", {
                "direction": direction,
                "confidence": confidence,
                "delta_pct": best_signal.window_delta_pct,
                "bet_size": bet_size,
                "maker_price": maker_price,
                "score": best_signal.score,
                "components": best_signal.components,
            })

            # Wait for resolution and track result
            asyncio.ensure_future(self._track_resolution(window_ts, direction, bet_size, maker_price))
        else:
            self._log_decision(window_ts, "error", "order_failed",
                               {"error": result.get("error", "unknown"),
                                "direction": direction, "confidence": confidence})

    async def _place_sniper_order(self, window_ts: int, direction: str,
                                  bet_size: float, price: float) -> dict:
        """Place a sniper order on Polymarket.

        Uses the position manager if available, otherwise returns paper result.
        """
        if self.mode == "paper" or not self.pm:
            # Paper trading — simulate
            self.bankroll -= bet_size
            return {"success": True, "mode": "paper", "bet_size": bet_size}

        # Build the asset_id for this window's market
        # The sniper needs to resolve the 5-min market's conditionId
        # Format: look up via Polymarket API using the window timestamp
        try:
            from .position_manager import create_package, create_leg, create_exit_rule

            slug = f"btc-updown-5m-{window_ts}"
            side = "YES" if direction == "UP" else "YES"  # UP token = YES, DOWN token = NO
            token_side = "UP" if direction == "UP" else "DOWN"

            # Resolve market conditionId from slug
            condition_id = await self._resolve_market_id(slug)
            if not condition_id:
                return {"success": False, "error": f"Cannot resolve market for {slug}"}

            # The UP/DOWN markets on Polymarket use separate conditionIds
            # Each 5-min window creates 2 markets: one UP, one DOWN
            # We need to buy YES on the correct one
            asset_id = f"{condition_id}:YES"

            pkg = create_package(f"Sniper: BTC 5m {direction} @{window_ts}", "btc_sniper")
            pkg["legs"].append(create_leg(
                platform="polymarket",
                leg_type="prediction_yes",
                asset_id=asset_id,
                asset_label=f"BTC 5m {direction} (window {window_ts})",
                entry_price=price,
                cost=bet_size,
                expiry="",  # Resolves in ~5 minutes
            ))
            # No exit rules — these resolve automatically in 5 minutes
            # The exit engine shouldn't touch them

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
                                bet_size: float, entry_price: float):
        """Wait for window resolution and update stats.

        Resolution: compare BTC price at window open vs close.
        If close >= open → UP wins, else DOWN wins.
        """
        close_time = window_ts + 300

        # Wait until after resolution (add 30s buffer for settlement)
        wait_time = close_time - time.time() + 30
        if wait_time > 0:
            await asyncio.sleep(wait_time)

        # Check result from price feed
        # The window's open price should be stored in the feed
        window = self.feed.window
        if window and window.window_ts == window_ts:
            open_price = window.open_price
            # Get close price from Binance (the price at window close)
            close_price = self.feed.price  # This is approximate — ideally query historical candle

            if close_price >= open_price:
                actual_direction = "UP"
            else:
                actual_direction = "DOWN"

            won = (direction == actual_direction)

            if won:
                # Payout: $1.00 per share, we bought at entry_price
                shares = bet_size / entry_price
                payout = shares * 1.0
                profit = payout - bet_size
                self.stats.trades_won += 1
                self.stats.total_pnl += profit
                self.bankroll += bet_size + profit  # Return cost + profit
                logger.info("BTC sniper WIN: %s (delta: $%.2f->$%.2f), profit=$%.2f",
                            direction, open_price, close_price, profit)
            else:
                self.stats.trades_lost += 1
                loss = -bet_size
                self.stats.total_pnl += loss
                logger.info("BTC sniper LOSS: predicted %s but was %s, loss=$%.2f",
                            direction, actual_direction, bet_size)
        else:
            # Can't verify — count as unknown
            logger.warning("BTC sniper: cannot verify resolution for window %d", window_ts)

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
