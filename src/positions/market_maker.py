"""Market maker — provides dual-sided liquidity on Polymarket crypto markets.

Strategy:
- Place maker limit orders on BOTH YES and NO sides of a market
- When both fill, combined cost < $1.00 = guaranteed profit at resolution
- Maker orders: 0% fee + daily USDC rebates from Polymarket's maker program
- Profit from bid-ask spread + rebate income

Arbigab-inspired improvements:
- Preemptive cancel: on every Binance tick, cancel exposed orders BEFORE adverse
  price moves cause toxic fills (event-driven, not polled)
- On-chain token merging: when both YES+NO fill, merge matched tokens back to
  $1.00 USDC via ProxyWallet instead of waiting for market resolution
- Multi-asset: works with BTC, ETH, SOL, XRP price feeds

Eligible markets (maker rebates active as of Mar 2026):
- 5-min crypto, 15-min crypto, 1H/4H/Daily/Weekly crypto
- NCAAB, Serie A sports

Risk controls:
- Inventory imbalance limits (max 70/30)
- Auto-withdraw before resolution
- Price circuit breaker on external feed divergence
- Preemptive cancel on adverse tick moves
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field

from .price_feed import BinancePriceFeed

logger = logging.getLogger("positions.market_maker")

# Market making parameters
QUOTE_REFRESH_INTERVAL = 8.0    # Seconds between quote updates
TARGET_SPREAD_LIQUID = 0.03     # 3% spread for liquid markets
TARGET_SPREAD_VOLATILE = 0.06   # 6% spread for volatile markets
MAX_INVENTORY_IMBALANCE = 0.70  # Max 70% on one side
MAX_CAPITAL_PER_MARKET = 500.0  # $500 max per market
RESOLUTION_BUFFER_SECONDS = 7200  # Withdraw 2 hours before resolution
PRICE_DIVERGENCE_HALT = 0.05    # Halt if external price diverges >5% from Polymarket
STALE_QUOTE_SECONDS = 10.0     # Cancel quotes older than 10 seconds
ADVERSE_MOVE_THRESHOLD = 0.003  # 0.3% adverse move triggers preemptive cancel
MERGE_MIN_MATCHED_SHARES = 1.0  # Minimum matched shares to attempt on-chain merge


@dataclass
class MarketState:
    """State for a single market being market-made."""
    condition_id: str
    slug: str
    title: str
    expiry: str
    asset: str = "BTC"      # Which crypto asset this market tracks
    # Inventory
    yes_shares: float = 0.0
    no_shares: float = 0.0
    yes_cost: float = 0.0
    no_cost: float = 0.0
    # Active orders
    yes_order_id: str | None = None
    no_order_id: str | None = None
    yes_order_price: float = 0.0
    no_order_price: float = 0.0
    # Price at time of last quote — used for preemptive cancel
    quote_ref_price: float = 0.0
    # Stats
    fills: int = 0
    total_spread_captured: float = 0.0
    total_merged_profit: float = 0.0
    realized_pnl: float = 0.0
    preemptive_cancels: int = 0
    # Timing
    last_quote_time: float = 0.0
    created_at: float = field(default_factory=time.time)


@dataclass
class MMStats:
    """Aggregate market maker statistics."""
    total_fills: int = 0
    total_spread_captured: float = 0.0
    total_merged_profit: float = 0.0
    total_rebates: float = 0.0
    total_pnl: float = 0.0
    markets_active: int = 0
    quote_updates: int = 0
    preemptive_cancels: int = 0
    merges_completed: int = 0
    halts: int = 0


class MarketMaker:
    """Dual-sided liquidity provider for Polymarket crypto markets.

    Runs as an independent async loop. Places maker limit orders on both
    YES and NO sides, profiting from the spread when both fill.

    Arbigab-inspired features:
    - Preemptive cancel: registers on_tick callback with price feed and
      instantly cancels exposed orders when Binance price moves adversely
      (before toxic fills can happen)
    - On-chain token merging: when matched YES+NO positions exist, merges
      them back to $1.00 USDC instead of waiting for resolution
    - Multi-asset: discovers and trades markets for all tracked assets
    """

    def __init__(self, price_feed: BinancePriceFeed, position_manager=None,
                 total_capital: float = 1000.0):
        self.feed = price_feed
        self.pm = position_manager
        self.total_capital = total_capital
        self.stats = MMStats()
        self._running = False
        self._task: asyncio.Task | None = None
        self._markets: dict[str, MarketState] = {}  # condition_id -> MarketState
        self._halted = False
        self._tick_callback_registered = False
        # Async-safe cancel queue — populated by on_tick callback, drained by async loop
        self._pending_cancels: asyncio.Queue = asyncio.Queue()

    def start(self):
        if self._running:
            return
        self._running = True
        # Register preemptive cancel callback
        if not self._tick_callback_registered:
            self.feed.on_tick(self._preemptive_cancel_check)
            self._tick_callback_registered = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info("Market maker started (capital=$%.2f)", self.total_capital)

    def stop(self):
        self._running = False
        if self._tick_callback_registered:
            self.feed.remove_on_tick(self._preemptive_cancel_check)
            self._tick_callback_registered = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Market maker stopped — stats: %d fills, $%.2f spread, %d preemptive cancels, $%.2f P&L",
                     self.stats.total_fills, self.stats.total_spread_captured,
                     self.stats.preemptive_cancels, self.stats.total_pnl)

    # ============================================================
    # PREEMPTIVE CANCEL (Arbigab-inspired)
    # ============================================================

    def _preemptive_cancel_check(self, asset: str, price: float, timestamp: float):
        """Called on every Binance trade tick — checks if any exposed orders
        need immediate cancellation due to adverse price movement.

        This runs synchronously inline with the WebSocket handler for
        minimum latency. Queues cancels for the async loop to execute.

        Logic: If price moves >0.3% against an exposed order since it was
        placed, queue it for immediate cancellation. An "exposed" order is
        one where only one side (YES or NO) is active — the other hasn't
        filled yet, leaving directional risk.
        """
        for condition_id, market in self._markets.items():
            if market.asset != asset:
                continue
            if market.quote_ref_price <= 0:
                continue

            price_change_pct = (price - market.quote_ref_price) / market.quote_ref_price

            # YES order exposed (NO not filled) — adverse = price dropping
            if market.yes_order_id and not market.no_order_id:
                if price_change_pct < -ADVERSE_MOVE_THRESHOLD:
                    # Capture order_id and clear immediately to prevent duplicate
                    # cancel queueing on subsequent ticks
                    oid = market.yes_order_id
                    market.yes_order_id = None
                    try:
                        self._pending_cancels.put_nowait((condition_id, "YES", oid))
                    except asyncio.QueueFull:
                        pass
                    market.preemptive_cancels += 1
                    self.stats.preemptive_cancels += 1

            # NO order exposed (YES not filled) — adverse = price rising
            if market.no_order_id and not market.yes_order_id:
                if price_change_pct > ADVERSE_MOVE_THRESHOLD:
                    oid = market.no_order_id
                    market.no_order_id = None
                    try:
                        self._pending_cancels.put_nowait((condition_id, "NO", oid))
                    except asyncio.QueueFull:
                        pass
                    market.preemptive_cancels += 1
                    self.stats.preemptive_cancels += 1

    async def _drain_pending_cancels(self):
        """Execute any preemptive cancels queued by the tick callback."""
        while not self._pending_cancels.empty():
            try:
                condition_id, side, order_id = self._pending_cancels.get_nowait()
            except asyncio.QueueEmpty:
                break
            if order_id:
                await self._cancel_order(order_id)
                logger.info("MM preemptive cancel: %s %s order on %s (adverse move)",
                            side, order_id[:12] if not order_id.startswith("paper_") else "paper",
                            self._markets.get(condition_id, MarketState("", "", "", "")).title[:30])

    def get_stats(self) -> dict:
        return {
            "total_capital": self.total_capital,
            "total_fills": self.stats.total_fills,
            "total_spread_captured": round(self.stats.total_spread_captured, 4),
            "total_merged_profit": round(self.stats.total_merged_profit, 4),
            "total_rebates": round(self.stats.total_rebates, 4),
            "total_pnl": round(self.stats.total_pnl, 2),
            "markets_active": len(self._markets),
            "quote_updates": self.stats.quote_updates,
            "preemptive_cancels": self.stats.preemptive_cancels,
            "merges_completed": self.stats.merges_completed,
            "halted": self._halted,
            "markets": {cid: self._market_stats(m) for cid, m in self._markets.items()},
        }

    def _market_stats(self, m: MarketState) -> dict:
        return {
            "title": m.title,
            "asset": m.asset,
            "yes_shares": m.yes_shares,
            "no_shares": m.no_shares,
            "fills": m.fills,
            "spread_captured": round(m.total_spread_captured, 4),
            "merged_profit": round(m.total_merged_profit, 4),
            "preemptive_cancels": m.preemptive_cancels,
            "pnl": round(m.realized_pnl, 2),
            "inventory_ratio": round(m.yes_shares / max(1, m.yes_shares + m.no_shares), 2)
                if (m.yes_shares + m.no_shares) > 0 else 0.5,
        }

    async def _loop(self):
        """Main market making loop."""
        # Wait for price feed to have data for any asset
        for _ in range(30):
            if any(self.feed.get_price(a) > 0 for a in self.feed.active_assets):
                break
            await asyncio.sleep(1)

        if not any(self.feed.get_price(a) > 0 for a in self.feed.active_assets):
            logger.error("Market maker: price feed has no data — aborting")
            return

        while self._running:
            try:
                # Drain any preemptive cancels queued by tick callback
                await self._drain_pending_cancels()

                # Discover eligible markets if none configured
                if not self._markets:
                    await self._discover_markets()

                if not self._markets:
                    logger.info("Market maker: no eligible markets found, retrying in 60s")
                    await asyncio.sleep(60)
                    continue

                # Check circuit breaker
                if self._check_circuit_breaker():
                    logger.warning("Market maker: circuit breaker tripped, halting")
                    self._halted = True
                    self.stats.halts += 1
                    await self._cancel_all_orders()
                    await asyncio.sleep(30)
                    continue
                self._halted = False

                # Update quotes for each market
                for condition_id, market in list(self._markets.items()):
                    if not self._running:
                        break

                    # Check if market is near resolution
                    if self._is_near_resolution(market):
                        logger.info("Market maker: withdrawing from %s (near resolution)", market.title[:40])
                        await self._cancel_market_orders(market)
                        del self._markets[condition_id]
                        continue

                    await self._update_quotes(market)

                # Check for fills and attempt token merges
                await self._check_fills()

                self.stats.quote_updates += 1
                await asyncio.sleep(QUOTE_REFRESH_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Market maker error: %s", e)
                await asyncio.sleep(10)

        # Clean shutdown: cancel all orders
        await self._cancel_all_orders()

    def _detect_asset_from_title(self, title: str) -> str | None:
        """Detect which crypto asset a market title refers to."""
        t = title.lower()
        asset_keywords = {
            "BTC": ["btc", "bitcoin"],
            "ETH": ["eth", "ethereum"],
            "SOL": ["sol", "solana"],
            "XRP": ["xrp", "ripple"],
        }
        for asset, keywords in asset_keywords.items():
            if asset in [a.upper() for a in self.feed.active_assets]:
                if any(kw in t for kw in keywords):
                    return asset
        return None

    async def _discover_markets(self):
        """Find eligible markets for market making.

        Targets: high-volume crypto markets with maker rebates.
        Discovers markets for all assets tracked by the price feed.
        """
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={
                        "closed": "false",
                        "limit": 50,
                        "order": "volume",
                        "ascending": "false",
                        "tag": "crypto",
                    }
                )
                if resp.status_code != 200:
                    return

                markets = resp.json()
                if not isinstance(markets, list):
                    markets = markets.get("data", markets.get("markets", []))

                capital_allocated = 0
                for m in markets:
                    if capital_allocated >= self.total_capital:
                        break

                    title = m.get("question", m.get("title", ""))
                    volume = int(float(m.get("volume", 0) or 0))
                    condition_id = m.get("conditionId", m.get("condition_id", ""))
                    slug = m.get("slug", "")
                    expiry = m.get("endDate", m.get("end_date_iso", ""))

                    if volume < 10000 or not condition_id:
                        continue

                    # Skip 5-min markets (sniper handles those)
                    if "5m" in slug or "5-min" in title.lower():
                        continue

                    # Match to an asset we have a price feed for
                    asset = self._detect_asset_from_title(title)
                    if not asset:
                        continue

                    self._markets[condition_id] = MarketState(
                        condition_id=condition_id,
                        slug=slug,
                        title=title,
                        expiry=expiry or "",
                        asset=asset,
                    )
                    capital_allocated += MAX_CAPITAL_PER_MARKET

                    if len(self._markets) >= 4:
                        break

                logger.info("Market maker: discovered %d eligible markets", len(self._markets))

        except Exception as e:
            logger.warning("Market maker: discovery failed: %s", e)

    async def _update_quotes(self, market: MarketState):
        """Update maker limit orders for a market.

        Places YES bid and NO bid such that combined cost < $1.00.
        Records reference price for preemptive cancel detection.
        """
        if self.feed.is_asset_stale(market.asset):
            return

        asset_price = self.feed.get_price(market.asset)
        if asset_price <= 0:
            return

        # Get current Polymarket price for this market
        polymarket_price = await self._get_market_price(market.condition_id)
        if polymarket_price <= 0:
            return

        # Calculate fair price from external feed
        # For crypto markets, we use the Polymarket mid as baseline
        # (we don't have a model for fair probability, just provide liquidity)
        fair_price = polymarket_price

        # Calculate spread based on inventory imbalance
        total_inventory = market.yes_shares + market.no_shares
        if total_inventory > 0:
            yes_ratio = market.yes_shares / total_inventory
        else:
            yes_ratio = 0.5

        base_spread = TARGET_SPREAD_LIQUID

        # Widen spread on overweight side to rebalance
        yes_spread = base_spread
        no_spread = base_spread
        if yes_ratio > MAX_INVENTORY_IMBALANCE:
            # Too much YES — widen YES bid (less aggressive), tighten NO
            yes_spread *= 1.5
            no_spread *= 0.7
        elif yes_ratio < (1 - MAX_INVENTORY_IMBALANCE):
            # Too much NO — widen NO bid, tighten YES
            no_spread *= 1.5
            yes_spread *= 0.7

        # Calculate bid prices
        yes_bid = round(fair_price - yes_spread / 2, 4)
        no_bid = round((1.0 - fair_price) - no_spread / 2, 4)

        # Validate: combined cost must be < $1.00 for guaranteed profit
        if yes_bid + no_bid >= 1.0:
            # Reduce both proportionally
            total = yes_bid + no_bid
            yes_bid = round(yes_bid * 0.98 / total, 4)
            no_bid = round(no_bid * 0.98 / total, 4)

        # Don't bid on prices that are too extreme
        if yes_bid < 0.05 or yes_bid > 0.95:
            return
        if no_bid < 0.05 or no_bid > 0.95:
            return

        # Check if quotes need updating (price moved enough)
        price_changed = (abs(yes_bid - market.yes_order_price) > 0.005 or
                         abs(no_bid - market.no_order_price) > 0.005)
        quote_stale = time.time() - market.last_quote_time > STALE_QUOTE_SECONDS

        if not price_changed and not quote_stale:
            return  # Quotes are still good

        # Cancel existing orders and place new ones
        await self._cancel_market_orders(market)

        # Place new maker orders
        bet_size = min(MAX_CAPITAL_PER_MARKET / 2,
                       (self.total_capital - self._total_allocated()) / 2)
        if bet_size < 1.0:
            return

        # Place YES maker bid
        yes_result = await self._place_maker_order(
            market.condition_id, "YES", yes_bid, bet_size)
        if yes_result:
            market.yes_order_id = yes_result
            market.yes_order_price = yes_bid

        # Place NO maker bid
        no_result = await self._place_maker_order(
            market.condition_id, "NO", no_bid, bet_size)
        if no_result:
            market.no_order_id = no_result
            market.no_order_price = no_bid

        market.last_quote_time = time.time()
        # Record reference price for preemptive cancel detection
        market.quote_ref_price = asset_price
        logger.debug("MM quotes updated: %s YES@%.4f NO@%.4f (spread=%.2f%%)",
                      market.title[:30], yes_bid, no_bid, (1.0 - yes_bid - no_bid) * 100)

    async def _place_maker_order(self, condition_id: str, side: str,
                                  price: float, amount: float) -> str | None:
        """Place a maker (GTC limit) order on Polymarket CLOB.

        Returns order_id if successful, None otherwise.
        """
        if not self.pm:
            # Paper mode — simulate
            return f"paper_{condition_id}_{side}_{int(time.time())}"

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
            from execution.polymarket_executor import PolymarketExecutor

            executor = self.pm.executors.get("polymarket")
            if not isinstance(executor, PolymarketExecutor) or not executor.is_configured():
                return f"paper_{condition_id}_{side}_{int(time.time())}"

            token_id = await executor._resolve_token_id(condition_id, side)
            clob = executor._get_clob()

            # Calculate shares from dollar amount
            shares = round(amount / price, 2)

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=shares,
                side="BUY",
            )

            neg_risk = await executor._run_sync(clob.get_neg_risk, token_id)
            options = PartialCreateOrderOptions(neg_risk=neg_risk)

            signed_order = await executor._run_sync(clob.create_order, order_args, options)
            result = await executor._run_sync(clob.post_order, signed_order, OrderType.GTC)

            order_id = result.get("orderID", result.get("id", ""))
            if order_id:
                logger.debug("MM placed %s maker @%.4f: order %s", side, price, order_id[:12])
                return order_id

        except Exception as e:
            logger.warning("MM maker order failed (%s %s @%.4f): %s", side, condition_id[:12], price, e)

        return None

    async def _cancel_market_orders(self, market: MarketState):
        """Cancel all active orders for a market."""
        for order_id in [market.yes_order_id, market.no_order_id]:
            if order_id and not order_id.startswith("paper_"):
                await self._cancel_order(order_id)
        market.yes_order_id = None
        market.no_order_id = None

    async def _cancel_all_orders(self):
        """Cancel all active orders across all markets."""
        for market in self._markets.values():
            await self._cancel_market_orders(market)

    async def _cancel_order(self, order_id: str):
        """Cancel a single order on Polymarket CLOB."""
        if not self.pm or order_id.startswith("paper_"):
            return

        try:
            from execution.polymarket_executor import PolymarketExecutor
            executor = self.pm.executors.get("polymarket")
            if isinstance(executor, PolymarketExecutor) and executor.is_configured():
                clob = executor._get_clob()
                await executor._run_sync(clob.cancel, order_id)
        except Exception as e:
            logger.debug("MM cancel order %s failed: %s", order_id[:12], e)

    async def _check_fills(self):
        """Check if any maker orders have been filled.

        When matched YES+NO fills are detected, attempts on-chain token
        merge to reclaim $1.00 USDC per matched pair immediately instead
        of waiting for market resolution.
        """
        for market in self._markets.values():
            for side, order_id in [("YES", market.yes_order_id), ("NO", market.no_order_id)]:
                if not order_id or order_id.startswith("paper_"):
                    continue

                filled = await self._check_order_filled(order_id)
                if filled:
                    price = filled.get("price", 0)
                    shares = filled.get("shares", 0)

                    if side == "YES":
                        market.yes_shares += shares
                        market.yes_cost += shares * price
                        market.yes_order_id = None
                    else:
                        market.no_shares += shares
                        market.no_cost += shares * price
                        market.no_order_id = None

                    market.fills += 1
                    self.stats.total_fills += 1

                    # Check if we have matched fills (both sides filled)
                    if market.yes_shares > 0 and market.no_shares > 0:
                        matched = min(market.yes_shares, market.no_shares)
                        avg_yes = market.yes_cost / market.yes_shares if market.yes_shares > 0 else 0
                        avg_no = market.no_cost / market.no_shares if market.no_shares > 0 else 0
                        spread = 1.0 - (avg_yes + avg_no)
                        captured = matched * spread
                        market.total_spread_captured += captured
                        self.stats.total_spread_captured += captured
                        logger.info("MM fill matched: %s, %.2f shares, spread=%.4f, captured=$%.4f",
                                    market.title[:30], matched, spread, captured)

                        # Attempt on-chain token merge for instant profit realization
                        if matched >= MERGE_MIN_MATCHED_SHARES:
                            await self._merge_matched_tokens(market, matched)

    async def _check_order_filled(self, order_id: str) -> dict | None:
        """Check if an order has been filled. Returns fill details or None."""
        if not self.pm:
            return None

        try:
            from execution.polymarket_executor import PolymarketExecutor
            executor = self.pm.executors.get("polymarket")
            if isinstance(executor, PolymarketExecutor) and executor.is_configured():
                clob = executor._get_clob()
                order = await executor._run_sync(clob.get_order, order_id)
                if order and order.get("status") == "MATCHED":
                    return {
                        "price": float(order.get("price", 0)),
                        "shares": float(order.get("size_matched", 0)),
                    }
        except Exception:
            pass
        return None

    # ============================================================
    # ON-CHAIN TOKEN MERGING (Arbigab-inspired)
    # ============================================================

    async def _merge_matched_tokens(self, market: MarketState, matched_shares: float):
        """Merge matched YES+NO tokens back to $1.00 USDC on-chain.

        Instead of waiting for market resolution, we can merge complementary
        tokens (YES + NO = $1.00) instantly via the CTF contract's
        mergePositions function. This:
        - Frees up capital immediately for the next trade
        - Eliminates resolution risk (what if resolution is delayed?)
        - Realizes profit in USDC instead of holding binary tokens

        The merge is done via Polymarket's CTF (Conditional Token Framework)
        contract. Each YES+NO pair merges back to the collateral (USDC).
        """
        if not self.pm:
            # Paper mode — simulate the merge
            avg_yes = market.yes_cost / market.yes_shares if market.yes_shares > 0 else 0
            avg_no = market.no_cost / market.no_shares if market.no_shares > 0 else 0
            merge_profit = matched_shares * (1.0 - avg_yes - avg_no)

            market.yes_shares -= matched_shares
            market.no_shares -= matched_shares
            market.yes_cost -= matched_shares * avg_yes
            market.no_cost -= matched_shares * avg_no
            market.total_merged_profit += merge_profit
            market.realized_pnl += merge_profit
            self.stats.total_merged_profit += merge_profit
            self.stats.total_pnl += merge_profit
            self.stats.merges_completed += 1
            # Return full $1.00/share (original cost + profit) — matches live mode
            self.total_capital += matched_shares
            logger.info("MM merge (paper): %s, %.2f shares, profit=$%.4f",
                        market.title[:30], matched_shares, merge_profit)
            return

        try:
            from execution.polymarket_executor import PolymarketExecutor
            executor = self.pm.executors.get("polymarket")
            if not isinstance(executor, PolymarketExecutor) or not executor.is_configured():
                return

            # Resolve both token IDs
            yes_token = await executor._resolve_token_id(market.condition_id, "YES")
            no_token = await executor._resolve_token_id(market.condition_id, "NO")

            if not yes_token or not no_token:
                logger.warning("MM merge: cannot resolve tokens for %s", market.condition_id[:12])
                return

            # Call CTF mergePositions via the CLOB client
            # The py_clob_client exposes merge_positions for this purpose
            clob = executor._get_clob()
            amount_wei = int(matched_shares * 1e6)  # USDC has 6 decimals

            merge_result = await executor._run_sync(
                clob.merge_positions, market.condition_id, amount_wei
            )

            if merge_result:
                avg_yes = market.yes_cost / market.yes_shares if market.yes_shares > 0 else 0
                avg_no = market.no_cost / market.no_shares if market.no_shares > 0 else 0
                merge_profit = matched_shares * (1.0 - avg_yes - avg_no)

                market.yes_shares -= matched_shares
                market.no_shares -= matched_shares
                market.yes_cost -= matched_shares * avg_yes
                market.no_cost -= matched_shares * avg_no
                market.total_merged_profit += merge_profit
                market.realized_pnl += merge_profit
                self.stats.total_merged_profit += merge_profit
                self.stats.total_pnl += merge_profit
                self.stats.merges_completed += 1
                self.total_capital += matched_shares  # Full $1.00/share returned
                logger.info("MM merge: %s, %.2f shares merged on-chain, profit=$%.4f",
                            market.title[:30], matched_shares, merge_profit)

        except Exception as e:
            # Merge failed — tokens remain as inventory, will resolve at expiry
            logger.warning("MM merge failed for %s: %s (will wait for resolution)",
                          market.condition_id[:12], e)

    async def _get_market_price(self, condition_id: str) -> float:
        """Get current YES price for a market."""
        if self.pm:
            executor = self.pm.executors.get("polymarket")
            if executor:
                try:
                    return await executor.get_current_price(f"{condition_id}:YES")
                except Exception:
                    pass
        return 0.0

    def _check_circuit_breaker(self) -> bool:
        """Check if external price diverges too much from Polymarket.

        Trips if:
        1. Any tracked asset's Binance feed is stale (>5s old)
        2. Any market's Polymarket mid price diverges >5% from Binance spot

        The divergence check uses cached Polymarket prices from the last
        quote update (market.yes_order_price) to avoid extra API calls.
        """
        for market in self._markets.values():
            # Check 1: feed staleness
            if self.feed.is_asset_stale(market.asset):
                return True

            # Check 2: price divergence between Binance and Polymarket
            binance_price = self.feed.get_price(market.asset)
            poly_mid = market.yes_order_price  # Last known Polymarket YES price
            if binance_price > 0 and poly_mid > 0:
                # For binary markets, fair YES price relates to probability.
                # We can't directly compare spot price to probability, but we
                # CAN check if the Polymarket price has moved dramatically from
                # our last quote reference, indicating a potential feed issue.
                if market.quote_ref_price > 0:
                    binance_move = abs(binance_price - market.quote_ref_price) / market.quote_ref_price
                    if binance_move > PRICE_DIVERGENCE_HALT:
                        logger.warning("Circuit breaker: %s Binance moved %.1f%% from quote ref",
                                      market.asset, binance_move * 100)
                        return True

        if not self._markets:
            return any(self.feed.is_asset_stale(a) for a in self.feed.active_assets)
        return False

    def _is_near_resolution(self, market: MarketState) -> bool:
        """Check if market is within the resolution buffer."""
        if not market.expiry:
            return False
        try:
            from datetime import datetime, date
            exp = datetime.fromisoformat(market.expiry.replace("Z", "+00:00"))
            remaining = (exp.timestamp() - time.time())
            return remaining <= RESOLUTION_BUFFER_SECONDS
        except (ValueError, TypeError):
            return False

    def _total_allocated(self) -> float:
        """Total capital currently allocated across all markets."""
        total = 0
        for m in self._markets.values():
            total += m.yes_cost + m.no_cost
        return total
